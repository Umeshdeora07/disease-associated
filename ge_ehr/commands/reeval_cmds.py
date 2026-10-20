"""``flask reeval ...`` — run and inspect re-evaluations (SPEC §7).

* ``flask reeval full [--yes]`` — reconcile every patient against the current release and rules
  (:func:`ge_ehr.services.reevaluation.full_recompute`); asks for confirmation unless ``--yes``.
* ``flask reeval runs [--limit N]`` — the most recent re-evaluation runs.
* ``flask reeval show RUN_ID`` — one run: counters, rule changes, per-category and per-gene counts
  and the changed variants.

``full`` commits its run. On failure the service has already rolled back and committed a
``failed`` run; the command rolls back whatever is left and ends with a
:class:`click.ClickException` (exit status 1). The inspection commands never write.

Owned by the Reeval work package.
"""

from __future__ import annotations

import logging
from typing import Any

import click
from flask import Flask
from flask.cli import AppGroup
from sqlalchemy import func, select
from sqlalchemy.orm import joinedload, selectinload

from .. import constants as C
from ..cli import echo_table
from ..extensions import db
from ..models import KbRelease, Patient, PatientVariant, ReevaluationRun, VariantChange
from ..services import reevaluation

logger = logging.getLogger(__name__)

DEFAULT_RUN_LIMIT = 20
TIME_FORMAT = "%Y-%m-%d %H:%M"

reeval_cli = AppGroup("reeval", help="Run full recomputations and inspect re-evaluation runs.")


def _duration(run: ReevaluationRun) -> str | None:
    return f"{run.duration_ms:,.0f} ms" if run.duration_ms is not None else None


def _release_label(release: KbRelease | None) -> str:
    return release.label if release is not None else "-"


def _echo_counters(run: ReevaluationRun) -> None:
    click.echo(
        f"  Work:     {run.stored_keys_compared:,} stored keys compared, {run.changed_variants:,} changed, "
        f"{run.carriers_affected:,} carriers affected, {run.patients_reevaluated:,} patients re-evaluated, "
        f"{run.records_examined:,} records examined"
    )
    click.echo(
        f"  Outcome:  {run.alerts_created:,} alerts and {run.notes_created:,} notes created, "
        f"{run.alerts_superseded:,} superseded, {run.alerts_confirmed:,} confirmed; "
        f"{run.review_notices:,} review notices, {run.notifications:,} notifications"
    )


@reeval_cli.command("full")
@click.option("--yes", is_flag=True, help="Do not ask for confirmation.")
def full_command(yes: bool) -> None:
    """Re-evaluate every patient against the current release and rules."""
    release = KbRelease.current()
    patients = db.session.scalar(select(func.count(Patient.id))) or 0
    variants = db.session.scalar(select(func.count(PatientVariant.id))) or 0
    if not yes:
        click.confirm(
            f"Re-evaluate all {patients:,} patient(s) ({variants:,} variant record(s)) against "
            f"{release.label if release is not None else 'no knowledge-base release'} "
            "and the current rules?",
            abort=True,
        )
    try:
        run = reevaluation.full_recompute()
        db.session.commit()
    except Exception as exc:
        db.session.rollback()
        logger.exception("Full re-evaluation failed")
        raise click.ClickException(
            f"Full re-evaluation failed: {str(exc) or type(exc).__name__}. No alert was changed; "
            "the failed run is listed by 'flask reeval runs'."
        ) from None
    click.echo(f"Full re-evaluation run #{run.id} ({run.status}) finished in {_duration(run)}.")
    _echo_counters(run)


@reeval_cli.command("runs")
@click.option(
    "--limit",
    type=click.IntRange(min=1),
    default=DEFAULT_RUN_LIMIT,
    show_default=True,
    help="Number of runs to list (most recent first).",
)
def runs_command(limit: int) -> None:
    """List the most recent re-evaluation runs."""
    statement = (
        select(ReevaluationRun)
        .options(joinedload(ReevaluationRun.new_release))
        .order_by(ReevaluationRun.id.desc())
        .limit(limit)
    )
    runs = db.session.scalars(statement).all()
    if not runs:
        click.echo("No re-evaluation runs yet.")
        return
    echo_table(
        (
            "ID",
            "STARTED (UTC)",
            "TRIGGER",
            "STRATEGY",
            "STATUS",
            "RELEASE",
            "DURATION",
            "CHANGED",
            "PATIENTS",
            "RECORDS",
            "CREATED",
            "SUPERSEDED",
            "NOTICES",
            "NOTIFIED",
        ),
        (
            (
                run.id,
                run.started_at.strftime(TIME_FORMAT),
                run.trigger,
                run.strategy,
                run.status,
                run.new_release.label if run.new_release is not None else None,
                _duration(run),
                run.changed_variants,
                run.patients_reevaluated,
                run.records_examined,
                run.alerts_created + run.notes_created,
                run.alerts_superseded,
                run.review_notices,
                run.notifications,
            )
            for run in runs
        ),
    )


def _echo_categories(summary: dict[str, Any]) -> None:
    categories = summary.get("categories") or {}
    if not any(counts.get("changed_variants") for counts in categories.values()):
        return
    click.echo("Change categories:")
    echo_table(
        ("CATEGORY", "VARIANTS", "CARRIERS", "CREATED", "SUPERSEDED", "CONFIRMED", "NOTICES", "NOTIFIED"),
        (
            (
                C.CHANGE_CATEGORY_LABELS.get(category, category),
                counts.get("changed_variants", 0),
                counts.get("carriers", 0),
                counts.get("alerts_created", 0) + counts.get("notes_created", 0),
                counts.get("alerts_superseded", 0),
                counts.get("alerts_confirmed", 0),
                counts.get("review_notices", 0),
                counts.get("notifications", 0),
            )
            for category, counts in categories.items()
        ),
    )


def _echo_genes(summary: dict[str, Any]) -> None:
    genes = summary.get("genes") or {}
    if not genes:
        return
    click.echo("Genes:")
    echo_table(
        ("GENE", "CHANGED", "CARRIERS"),
        ((gene, counts.get("changed_variants"), counts.get("carriers")) for gene, counts in genes.items()),
    )


def _echo_rules(run: ReevaluationRun, summary: dict[str, Any]) -> None:
    for change in run.rule_change or []:
        old = change.get("old_version")
        versions = f"v{old} → v{change['new_version']}" if old is not None else f"v{change['new_version']} (new)"
        click.echo(f"Rule change: {change['code']} {versions}")
    rules = summary.get("rules") or {}
    if rules:
        echo_table(
            ("RULE", "CREATED", "SUPERSEDED", "CONFIRMED", "NOTICES", "NOTIFIED"),
            (
                (
                    code,
                    counts.get("alerts_created", 0) + counts.get("notes_created", 0),
                    counts.get("alerts_superseded", 0),
                    counts.get("alerts_confirmed", 0),
                    counts.get("review_notices", 0),
                    counts.get("notifications", 0),
                )
                for code, counts in rules.items()
            ),
        )


def _echo_changes(changes: list[VariantChange]) -> None:
    click.echo(f"Changed variants ({len(changes):,}):")
    echo_table(
        ("VARIANT", "GENE", "OLD", "NEW", "MECHANISM", "CATEGORY", "CARRIERS"),
        (
            (
                change.variant_key.label,
                change.gene,
                change.old_classification,
                change.new_classification,
                (
                    f"{change.old_mechanism} → {change.new_mechanism}"
                    if change.old_mechanism != change.new_mechanism
                    else change.new_mechanism
                ),
                change.category,
                change.carrier_count,
            )
            for change in changes
        ),
    )


@reeval_cli.command("show")
@click.argument("run_id", type=int)
def show_command(run_id: int) -> None:
    """Show one re-evaluation run in detail."""
    statement = (
        select(ReevaluationRun)
        .where(ReevaluationRun.id == run_id)
        .options(
            joinedload(ReevaluationRun.old_release),
            joinedload(ReevaluationRun.new_release),
            joinedload(ReevaluationRun.initiated_by),
            selectinload(ReevaluationRun.changes).joinedload(VariantChange.variant_key),
        )
    )
    run = db.session.scalar(statement)
    if run is None:
        raise click.ClickException(f"No re-evaluation run with id {run_id}; list runs with: flask reeval runs")
    finished = run.finished_at.strftime(TIME_FORMAT) if run.finished_at is not None else "-"
    click.echo(
        f"Re-evaluation run #{run.id}: {C.TRIGGER_LABELS.get(run.trigger, run.trigger)}, "
        f"{C.RUN_STRATEGY_LABELS.get(run.strategy, run.strategy)}, {run.status}"
    )
    click.echo(
        f"  Started:  {run.started_at.strftime(TIME_FORMAT)} UTC; finished {finished} UTC ({_duration(run) or '-'})"
    )
    click.echo(f"  By:       {run.initiated_by.username if run.initiated_by is not None else 'system'}")
    click.echo(f"  Releases: {_release_label(run.old_release)} → {_release_label(run.new_release)}")
    if run.error:
        click.echo(f"  Error:    {run.error}")
    _echo_counters(run)
    summary = run.summary or {}
    if summary.get("initial"):
        click.echo("  First activation: initial classification, not a reclassification.")
    _echo_rules(run, summary)
    _echo_categories(summary)
    _echo_genes(summary)
    if run.changes:
        _echo_changes(run.changes)


def register(app: Flask) -> None:
    """Register the ``reeval`` command group."""
    app.cli.add_command(reeval_cli)
