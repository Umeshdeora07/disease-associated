"""Routes of the knowledge blueprint (``/knowledge/...``): ClinVar releases, decision rules and
re-evaluation runs (SPEC §8.2).

Clinicians and administrators may view everything; importing and activating releases, importing
rule sets and full recomputation are for administrators (SPEC §8.1, enforced server-side by
:func:`~ge_ehr.security.roles_required`; templates hide what a role cannot use). Every change goes
through the services — :func:`ge_ehr.services.clinvar.import_release` and the re-evaluation entry
points of :mod:`ge_ehr.services.reevaluation` — which validate, audit and log the run. Routes
translate between HTTP and the services, commit, flash a summary and redirect (POST-redirect-GET);
an invalid submission is re-rendered with its messages and HTTP 400.

An import is committed before its activation starts, so a failed re-evaluation (recorded as a
failed run by the engine, SPEC §7.6) leaves the imported release in place for a later activation.

Owned by the UI-B work package.
"""

from __future__ import annotations

import json
import logging
import tempfile
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from flask import Blueprint, abort, current_app, flash, redirect, render_template, request, url_for
from flask_login import current_user
from werkzeug.datastructures import FileStorage
from werkzeug.utils import secure_filename
from werkzeug.wrappers import Response

from .. import constants as C
from ..constants import CLINICAL_ROLES
from ..extensions import db
from ..models import KbRelease, ReevaluationRun
from ..security import roles_required
from ..services import clinvar, reevaluation
from ..services.rules import RuleDefinitionError, parse_rule_set_json
from ..templating import format_duration_ms
from . import queries
from .forms import (
    CLINVAR_SUFFIXES,
    OVERLAY_SUFFIXES,
    ActivateReleaseForm,
    ImportReleaseForm,
    ImportRulesForm,
)

logger = logging.getLogger(__name__)

bp = Blueprint("knowledge", __name__, url_prefix="/knowledge")

#: Variant changes listed per page (compare page and run page).
CHANGES_PER_PAGE = 50


@bp.context_processor
def knowledge_helpers() -> dict[str, Any]:
    """Helpers for the knowledge templates."""
    return {
        "proportion_segments": queries.proportion_segments,
        "outcome_fields": queries.OUTCOME_FIELDS,
        "secondary_outcomes": queries.OUTCOME_FIELDS_SECONDARY,
        "preview_labels": queries.PREVIEW_STATUS_LABELS,
    }


def _per_page() -> int:
    return int(current_app.config["ITEMS_PER_PAGE"])


def _page() -> int:
    return request.args.get("page", 1, type=int)


def _plural(count: int, singular: str, plural: str | None = None) -> str:
    return f"{count:,} {singular if count == 1 else (plural or singular + 's')}"


def _run_outcome(run: ReevaluationRun) -> str:
    """One sentence with a run's work and outcome counters."""
    alerts = _plural(run.alerts_created, "alert")
    notes = f" and {_plural(run.notes_created, 'note')}" if run.notes_created else ""
    return (
        f"Re-evaluation run #{run.id} finished in {format_duration_ms(run.duration_ms)}: "
        f"{_plural(run.patients_reevaluated, 'patient')} re-evaluated, {_plural(run.records_examined, 'record')} "
        f"examined; {alerts}{notes} created, {run.alerts_superseded:,} superseded, {run.alerts_confirmed:,} "
        f"confirmed; {_plural(run.review_notices, 'review notice')}, {_plural(run.notifications, 'notification')}."
    )


# ---------------------------------------------------------------------------
# Releases
# ---------------------------------------------------------------------------


@bp.route("/releases")
@roles_required(*CLINICAL_ROLES)
def releases() -> str:
    """Imported knowledge-base releases with their activation state."""
    pagination = db.paginate(queries.releases_statement(), page=_page(), per_page=_per_page(), error_out=False)
    return render_template(
        "knowledge/releases.html",
        pagination=pagination,
        activations=queries.latest_activations(release.id for release in pagination.items),
        stored_keys=queries.stored_key_count(),
    )


def _matching_suffix(filename: str, suffixes: Sequence[str]) -> str:
    """The longest of ``suffixes`` that ``filename`` ends with (the form guarantees one does)."""
    name = filename.lower()
    return max((suffix for suffix in suffixes if name.endswith(suffix)), key=len)


def _save_upload(storage: FileStorage, folder: Path, suffixes: Sequence[str], default_stem: str) -> Path:
    """Save an upload under its sanitised name (the import records the file's basename).

    A name that sanitising empties or strips of its suffix becomes ``default_stem`` + suffix.
    """
    suffix = _matching_suffix(storage.filename or "", suffixes)
    name = secure_filename(storage.filename or "")
    if not name.lower().endswith(suffix) or len(name) <= len(suffix):
        name = default_stem + suffix
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / name
    storage.save(path)
    return path


def _render_import_release(form: ImportReleaseForm, *, duplicate: KbRelease | None = None, status: int = 200) -> tuple[str, int]:
    return (
        render_template(
            "knowledge/import_release.html",
            form=form,
            duplicate=duplicate,
            current=KbRelease.current(),
            stored_keys=queries.stored_key_count(),
        ),
        status,
    )


@bp.route("/releases/import", methods=["GET", "POST"])
@roles_required("admin")
def import_release() -> Response | tuple[str, int]:
    """Import a ClinVar(-format) VCF as a new release, optionally activating it right away."""
    form = ImportReleaseForm()
    if not form.validate_on_submit():
        return _render_import_release(form, status=400 if form.is_submitted() else 200)
    with tempfile.TemporaryDirectory(prefix="ge-ehr-release-") as folder:
        vcf_path = _save_upload(form.vcf_file.data, Path(folder), CLINVAR_SUFFIXES, "clinvar_release")
        overlay = form.expert_panel_csv.data
        overlay_path = (
            _save_upload(overlay, Path(folder) / "overlay", OVERLAY_SUFFIXES, "expert_panel_overlay")
            if isinstance(overlay, FileStorage) and overlay.filename
            else None
        )
        try:
            release = clinvar.import_release(
                vcf_path,
                label=form.label.data or None,
                release_date=form.release_date.data,
                expert_panel_csv=overlay_path,
                user=current_user,
                notes=form.notes.data or None,
            )
        except (ValueError, FileNotFoundError) as exc:
            db.session.rollback()
            form.attach_import_error(str(exc))
            return _render_import_release(form, duplicate=clinvar.release_for_file(vcf_path), status=400)
    db.session.commit()
    kind = "synthetic ClinVar-format" if release.is_synthetic else "ClinVar"
    imported = f"Imported {kind} release “{release.label}”: {release.record_count:,} panel records."
    logger.info("Release %s (id %d) imported by %s", release.label, release.id, current_user.username)
    if form.activate.data:
        return _activate(release, form.strategy.data, prefix=imported)
    flash(f"{imported} It is not active yet: compare it with the current release, then activate it.", "success")
    return redirect(url_for("knowledge.view_release", release_id=release.id))


def _activate(release: KbRelease, strategy: str, *, prefix: str = "") -> Response:
    """Activate a committed release, commit, and redirect to the run (or back to the release on failure)."""
    label, release_id = release.label, release.id
    lead = f"{prefix} " if prefix else ""
    try:
        run = reevaluation.activate_release(release, strategy=strategy, user=current_user)
    except ValueError as exc:
        db.session.rollback()
        flash(f"{lead}{exc}", "warning")
        return redirect(url_for("knowledge.view_release", release_id=release_id))
    except Exception as exc:
        # The engine has rolled back and recorded a failed run (SPEC §7.6); show what happened.
        db.session.rollback()
        logger.exception("Activation of knowledge-base release %s failed", release_id)
        flash(
            f"{lead}Activation of “{label}” failed: {str(exc) or type(exc).__name__}. The release was not made "
            "current and no alert changed; the failed run is listed under Re-evaluation runs.",
            "danger",
        )
        return redirect(url_for("knowledge.view_release", release_id=release_id))
    db.session.commit()
    flash(f"{lead}“{label}” is now the current release. {_run_outcome(run)}", "success")
    return redirect(url_for("knowledge.view_run", run_id=run.id))


@bp.route("/releases/<int:release_id>")
@roles_required(*CLINICAL_ROLES)
def view_release(release_id: int) -> str:
    """A release: counts by gene and class, coverage of stored variants, activation history."""
    release = db.get_or_404(KbRelease, release_id, description=f"Knowledge-base release {release_id} does not exist.")
    return render_template(
        "knowledge/release.html",
        release=release,
        detail=queries.release_detail(release),
        form=ActivateReleaseForm(),
    )


@bp.route("/releases/<int:release_id>/activate", methods=["POST"])
@roles_required("admin")
def activate_release(release_id: int) -> Response:
    """Make a stored release current and re-evaluate the affected patients."""
    release = db.get_or_404(KbRelease, release_id, description=f"Knowledge-base release {release_id} does not exist.")
    form = ActivateReleaseForm()
    if not form.validate_on_submit():
        flash(" ".join(form.strategy.errors) or "The activation request was invalid; please try again.", "danger")
        return redirect(url_for("knowledge.view_release", release_id=release.id))
    if release.is_current:
        flash(f"“{release.label}” is already the current release; nothing to do.", "info")
        return redirect(url_for("knowledge.view_release", release_id=release.id))
    return _activate(release, form.strategy.data)


def _default_pair(releases: Sequence[KbRelease]) -> tuple[KbRelease | None, KbRelease | None]:
    """The most useful comparison: current → newest never-activated release, else the last activation."""
    current = next((release for release in releases if release.is_current), None)
    pending = [release for release in releases if release.sequence is None and not release.is_current]
    if current is not None and pending:
        return current, max(pending, key=lambda release: release.id)
    activated = sorted((release for release in releases if release.sequence is not None), key=lambda r: r.sequence)
    if len(activated) >= 2:
        return activated[-2], activated[-1]
    if len(releases) >= 2:
        ordered = sorted(releases, key=lambda release: release.id)
        return ordered[-2], ordered[-1]
    return None, None


def _release_arg(name: str, by_id: dict[int, KbRelease], default: KbRelease | None) -> KbRelease | None:
    value = request.args.get(name, "").strip()
    if not value:
        return default
    if not value.isdigit() or int(value) not in by_id:
        abort(404, description=f"Knowledge-base release {value!r} does not exist.")
    return by_id[int(value)]


@bp.route("/releases/compare")
@roles_required(*CLINICAL_ROLES)
def compare_releases() -> tuple[str, int]:
    """Classification changes between two releases, limited to stored variant keys unless ``all=1``."""
    releases = queries.all_releases()
    by_id = {release.id: release for release in releases}
    default_old, default_new = _default_pair(releases)
    old = _release_arg("old", by_id, default_old)
    new = _release_arg("new", by_id, default_new)
    all_keys = request.args.get("all") == "1"
    category = request.args.get("category", "").strip()
    category = category if category in C.CHANGE_CATEGORIES else None
    context: dict[str, Any] = {
        "releases": releases,
        "old": old,
        "new": new,
        "all_keys": all_keys,
        "category": category,
        "comparison": None,
        "error": None,
    }
    status = 200
    if old is not None and new is not None:
        if old.id == new.id:
            context["error"] = "Choose two different releases to compare."
            status = 400
        else:
            comparison = queries.compare_releases(old, new, all_keys=all_keys, category=category)
            pagination = queries.paginate_sequence(comparison.changes, page=_page(), per_page=CHANGES_PER_PAGE)
            context.update(
                comparison=comparison,
                pagination=pagination,
                rows=queries.change_rows(pagination.items, comparison),
            )
    return render_template("knowledge/compare.html", **context), status


# ---------------------------------------------------------------------------
# Rules
# ---------------------------------------------------------------------------


@bp.route("/rules")
@roles_required(*CLINICAL_ROLES)
def rules() -> str:
    """The current rule set with each rule's version history."""
    return render_template("knowledge/rules.html", cards=queries.rule_cards())


def _apply_rule_set(form: ImportRulesForm, doc: dict[str, Any]) -> Response | None:
    """Import the rule set and re-evaluate; None (with the error attached to the form) when it is rejected."""
    try:
        run = reevaluation.apply_rule_set(doc, user=current_user, strategy=form.strategy.data)
    except RuleDefinitionError as exc:
        db.session.rollback()
        form.attach_rule_error(str(exc))
        return None
    except Exception as exc:
        # Recorded as a failed run by the engine (SPEC §7.6); no rule version was stored.
        db.session.rollback()
        logger.exception("Importing rule set %r failed", doc.get("label"))
        flash(
            f"Importing the rule set failed: {str(exc) or type(exc).__name__}. No rule version was changed; "
            "the failed run is listed below.",
            "danger",
        )
        return redirect(url_for("knowledge.runs"))
    db.session.commit()
    name = f"rule set “{doc['label']}”" if doc.get("label") else "the rule set"
    changes = run.rule_change or []
    if changes:
        revised = ", ".join(
            f"{change['code']} v{change['old_version']} → v{change['new_version']}"
            if change["old_version"] is not None
            else f"{change['code']} v{change['new_version']} (new)"
            for change in changes
        )
        flash(f"Imported {name}: {revised}. {_run_outcome(run)}", "success")
    else:
        flash(
            f"Imported {name}: every version in it was already stored, so no current rule changed "
            f"(run #{run.id} logged).",
            "info",
        )
    return redirect(url_for("knowledge.view_run", run_id=run.id))


@bp.route("/rules/import", methods=["GET", "POST"])
@roles_required("admin")
def import_rules() -> Response | tuple[str, int]:
    """Validate (and optionally import) a rule-set JSON document from a file or pasted text."""
    form = ImportRulesForm()
    preview: queries.RuleSetPreview | None = None
    if form.validate_on_submit():
        try:
            doc = parse_rule_set_json(form.document())
        except RuleDefinitionError as exc:
            form.attach_rule_error(f"{form.source_name}: {exc}")
        else:
            if form.validate_only.data:
                preview = queries.rule_set_preview(doc)
                # File inputs cannot be refilled, so the validated document moves to the text box.
                form.rules_json.data = json.dumps(doc, indent=2, ensure_ascii=False)
            else:
                outcome = _apply_rule_set(form, doc)
                if outcome is not None:
                    return outcome
    status = 400 if form.is_submitted() and preview is None else 200
    return render_template("knowledge/import_rules.html", form=form, preview=preview), status


# ---------------------------------------------------------------------------
# Re-evaluation runs
# ---------------------------------------------------------------------------


@bp.route("/runs")
@roles_required(*CLINICAL_ROLES)
def runs() -> str:
    """Logged re-evaluation runs, newest first, filterable by trigger and status."""
    trigger = request.args.get("trigger", "").strip()
    status = request.args.get("status", "").strip()
    filters = {
        "trigger": trigger if trigger in C.TRIGGERS else "",
        "status": status if status in C.RUN_STATUSES else "",
    }
    pagination = db.paginate(
        queries.runs_statement(trigger=filters["trigger"] or None, status=filters["status"] or None),
        page=_page(),
        per_page=_per_page(),
        error_out=False,
    )
    return render_template("knowledge/runs.html", pagination=pagination, filters=filters)


@bp.route("/runs/<int:run_id>")
@roles_required(*CLINICAL_ROLES)
def view_run(run_id: int) -> str:
    """A run: counters, per-category/per-rule summary, variant changes and affected patients."""
    run = queries.load_run(run_id)
    changes = db.paginate(queries.run_changes_statement(run), page=_page(), per_page=CHANGES_PER_PAGE, error_out=False)
    affected, affected_total = queries.affected_patients(run)
    return render_template(
        "knowledge/run.html",
        run=run,
        summary=queries.run_summary(run),
        changes=changes,
        carriers=queries.carriers_by_key(change.variant_key_id for change in changes.items),
        carriers_shown=queries.CARRIERS_SHOWN,
        affected=affected,
        affected_total=affected_total,
    )


@bp.route("/recompute", methods=["POST"])
@roles_required("admin")
def recompute() -> Response:
    """Re-evaluate every patient against the current release and rules (full recomputation)."""
    try:
        run = reevaluation.full_recompute(user=current_user)
    except Exception as exc:
        # Recorded as a failed run by the engine (SPEC §7.6).
        db.session.rollback()
        logger.exception("Full recomputation failed")
        flash(
            f"Full recomputation failed: {str(exc) or type(exc).__name__}. No alert changed; the failed run is "
            "listed below.",
            "danger",
        )
        return redirect(url_for("knowledge.runs"))
    db.session.commit()
    flash(f"Full recomputation complete. {_run_outcome(run)}", "success")
    return redirect(url_for("knowledge.view_run", run_id=run.id))
