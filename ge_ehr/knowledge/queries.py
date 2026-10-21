"""Read models of the knowledge pages: releases, release comparison, rules and re-evaluation runs.

Each function issues a fixed number of queries whatever the number of rows (related objects are
eager-loaded or fetched in one bulk query), so list pages stay free of N+1 query patterns. All
changes to the knowledge base go through the services; nothing here writes.

Owned by the UI-B work package.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from itertools import batched
from typing import Any

from flask import abort
from flask_sqlalchemy.pagination import Pagination
from sqlalchemy import Select, case, distinct, func, select
from sqlalchemy.orm import joinedload, selectinload

from .. import constants as C
from ..extensions import db
from ..models import (
    Alert,
    AlertEvent,
    KbRelease,
    KnownVariant,
    Patient,
    PatientVariant,
    ReevaluationRun,
    ReviewNotice,
    Rule,
    RuleVersion,
    VariantChange,
    VariantKey,
)
from ..services.classification import IN_CLAUSE_CHUNK
from ..services.clinvar import KeyChange, diff_releases
from ..services.rules import RuleSpec, describe_clinical_condition, describe_genetic_criterion, rule_code_sort_key

#: Display order of change categories (most consequential first).
CATEGORY_ORDER: dict[str, int] = {category: index for index, category in enumerate(C.CHANGE_CATEGORIES)}
#: Outcome counters shown per change category / per rule (keys of ``ReevaluationRun.summary``).
OUTCOME_FIELDS: tuple[tuple[str, str], ...] = (
    ("alerts_created", "Alerts created"),
    ("notes_created", "Notes created"),
    ("alerts_superseded", "Superseded"),
    ("alerts_confirmed", "Confirmed"),
    ("review_notices", "Review notices"),
    ("notifications", "Notifications"),
)
#: Carriers listed per variant change on a run page (the count is always shown in full).
CARRIERS_SHOWN = 6
#: Patients listed in a run's "affected patients" section.
AFFECTED_PATIENTS_SHOWN = 100


def gene_sort_key(gene: str | None) -> tuple[int, str]:
    """Panel order (GCK, HNF1A, …, INS); other or unknown genes last."""
    if gene in C.GENES:
        return C.GENES.index(gene), gene
    return len(C.GENES), gene or ""


# ---------------------------------------------------------------------------
# Proportional bars
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Segment:
    """One part of a proportional bar: ``offset`` and ``width`` are percentages of the bar."""

    key: str
    count: int
    offset: float
    width: float


def proportion_segments(counts: Mapping[str, int], order: Sequence[str]) -> list[Segment]:
    """Segments of a stacked 0–100 % bar for the non-zero ``counts`` in ``order``."""
    total = sum(counts.get(key, 0) for key in order)
    segments: list[Segment] = []
    offset = 0.0
    for key in order:
        count = counts.get(key, 0)
        if count <= 0 or total <= 0:
            continue
        width = round(100 * count / total, 3)
        segments.append(Segment(key, count, round(offset, 3), width))
        offset += width
    return segments


class SequencePagination(Pagination):
    """Flask-SQLAlchemy pagination over an in-memory sequence (``items=``), for computed lists."""

    def _query_items(self) -> list[Any]:
        items: Sequence[Any] = self._query_args["items"]
        start = (self.page - 1) * self.per_page
        return list(items[start : start + self.per_page])

    def _query_count(self) -> int:
        return len(self._query_args["items"])


def paginate_sequence(items: Sequence[Any], *, page: int, per_page: int) -> SequencePagination:
    """Page ``page`` (1-based; out-of-range pages are empty) of ``items``."""
    return SequencePagination(page=max(page, 1), per_page=per_page, error_out=False, items=items)


# ---------------------------------------------------------------------------
# Releases
# ---------------------------------------------------------------------------


def releases_statement() -> Select[tuple[KbRelease]]:
    """Every release, the most recently imported first, with the importing user."""
    return (
        select(KbRelease)
        .options(joinedload(KbRelease.imported_by))
        .order_by(KbRelease.imported_at.desc(), KbRelease.id.desc())
    )


def all_releases() -> list[KbRelease]:
    """Every release for selection lists: current first, then by activation sequence and import."""
    statement = select(KbRelease).order_by(
        KbRelease.is_current.desc(), KbRelease.sequence.desc().nulls_first(), KbRelease.id.desc()
    )
    return list(db.session.scalars(statement))


def latest_activations(release_ids: Iterable[int]) -> dict[int, ReevaluationRun]:
    """The latest successful activation run of each release (with its initiator), by release id."""
    ids = sorted(set(release_ids))
    if not ids:
        return {}
    statement = (
        select(ReevaluationRun)
        .options(joinedload(ReevaluationRun.initiated_by))
        .where(
            ReevaluationRun.trigger == "clinvar_release",
            ReevaluationRun.status == "success",
            ReevaluationRun.new_release_id.in_(ids),
        )
        .order_by(ReevaluationRun.id)
    )
    return {run.new_release_id: run for run in db.session.scalars(statement) if run.new_release_id is not None}


def stored_key_count() -> int:
    """Distinct variant keys stored for some patient (``V_obs`` of Algorithm 2)."""
    return db.session.scalar(select(func.count(distinct(PatientVariant.variant_key_id)))) or 0


@dataclass(frozen=True)
class GeneClassRow:
    """Record counts of one gene by classification."""

    gene: str
    counts: dict[str, int]
    total: int


@dataclass(frozen=True)
class ReleaseDetail:
    """Everything the release page shows besides the release itself."""

    release: KbRelease
    rows: list[GeneClassRow]
    class_totals: dict[str, int]
    expert_panel: int
    overlay_overrides: int
    stored_keys: int
    stored_keys_listed: int
    activations: list[ReevaluationRun]
    compare_old: KbRelease | None
    compare_new: KbRelease | None


def _compare_pair(release: KbRelease, activations: Sequence[ReevaluationRun]) -> tuple[KbRelease | None, KbRelease | None]:
    """The natural comparison for a release: current → it, or (if it is current) its predecessor → it."""
    if release.is_current:
        previous = next(
            (run.old_release for run in activations if run.status == "success" and run.old_release is not None), None
        )
        return (previous, release) if previous is not None else (None, None)
    current = KbRelease.current()
    return (current, release) if current is not None else (None, None)


def release_detail(release: KbRelease) -> ReleaseDetail:
    """Gene × class counts, expert-panel counts, stored-key coverage and activation runs of a release."""
    counts: defaultdict[str, dict[str, int]] = defaultdict(dict)
    grouped = select(KnownVariant.gene, KnownVariant.classification, func.count(KnownVariant.id)).where(
        KnownVariant.release_id == release.id
    ).group_by(KnownVariant.gene, KnownVariant.classification)
    for gene, classification, count in db.session.execute(grouped):
        counts[gene or "Unknown"][classification] = count
    rows = [
        GeneClassRow(gene, {cls: by_class.get(cls, 0) for cls in C.CLASSES}, sum(by_class.values()))
        for gene, by_class in sorted(counts.items(), key=lambda item: gene_sort_key(item[0]))
    ]
    class_totals = {cls: sum(row.counts[cls] for row in rows) for cls in C.CLASSES}

    expert_panel, overlay = db.session.execute(
        select(
            func.coalesce(func.sum(case((KnownVariant.is_expert_panel.is_(True), 1), else_=0)), 0),
            func.coalesce(
                func.sum(case((KnownVariant.classification_source == "expert_panel_overlay", 1), else_=0)), 0
            ),
        ).where(KnownVariant.release_id == release.id)
    ).one()
    listed = db.session.scalar(
        select(func.count(distinct(PatientVariant.variant_key_id))).join(
            KnownVariant,
            (KnownVariant.variant_key_id == PatientVariant.variant_key_id) & (KnownVariant.release_id == release.id),
        )
    )
    activations = list(
        db.session.scalars(
            select(ReevaluationRun)
            .options(joinedload(ReevaluationRun.initiated_by), joinedload(ReevaluationRun.old_release))
            .where(ReevaluationRun.trigger == "clinvar_release", ReevaluationRun.new_release_id == release.id)
            .order_by(ReevaluationRun.id.desc())
        )
    )
    compare_old, compare_new = _compare_pair(release, activations)
    return ReleaseDetail(
        release=release,
        rows=rows,
        class_totals=class_totals,
        expert_panel=int(expert_panel),
        overlay_overrides=int(overlay),
        stored_keys=stored_key_count(),
        stored_keys_listed=listed or 0,
        activations=activations,
        compare_old=compare_old,
        compare_new=compare_new,
    )


# ---------------------------------------------------------------------------
# Release comparison
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CategorySummary:
    """Changed keys and their distinct carriers in one change category."""

    category: str
    changes: int
    carriers: int


@dataclass(frozen=True)
class GeneSummary:
    """Changed keys of one gene, by category, with their distinct carriers."""

    gene: str
    counts: dict[str, int]
    total: int
    carriers: int


@dataclass(frozen=True)
class Comparison:
    """The difference between two releases, restricted to stored keys unless ``all_keys``."""

    old: KbRelease
    new: KbRelease
    all_keys: bool
    category: str | None
    stored_keys: int
    total: int
    changes: list[KeyChange]
    categories: list[CategorySummary]
    genes: list[GeneSummary]
    carriers_affected: int
    carriers_by_key: dict[int, int]

    @property
    def activation_preview(self) -> bool:
        """True when the comparison is exactly what activating ``new`` would apply (current → new)."""
        return self.old.is_current and not self.new.is_current


def _carriers_of_keys(key_ids: Iterable[int]) -> dict[int, set[int]]:
    """``variant_key_id → patient ids`` for keys stored for some patient (chunked IN queries)."""
    carriers: defaultdict[int, set[int]] = defaultdict(set)
    statement = select(PatientVariant.variant_key_id, PatientVariant.patient_id)
    for chunk in batched(sorted(set(key_ids)), IN_CLAUSE_CHUNK):
        for key_id, patient_id in db.session.execute(statement.where(PatientVariant.variant_key_id.in_(chunk))):
            carriers[key_id].add(patient_id)
    return dict(carriers)


def _union(sets: Iterable[set[int]]) -> int:
    return len(set().union(*sets))


def compare_releases(old: KbRelease, new: KbRelease, *, all_keys: bool, category: str | None) -> Comparison:
    """Diff ``old`` → ``new`` with :func:`~ge_ehr.services.clinvar.diff_releases`.

    By default only variant keys stored for some patient are compared (what an activation would
    examine); ``all_keys`` compares every key of either release. Changes carried by a patient come
    first, then by category, gene and key. ``category`` filters the listed changes; the summaries
    always cover every change.
    """
    stored = set(db.session.scalars(select(PatientVariant.variant_key_id).distinct()))
    changes = diff_releases(old, new, None if all_keys else stored)
    carriers = _carriers_of_keys({change.variant_key_id for change in changes} & stored)
    by_category: defaultdict[str, list[KeyChange]] = defaultdict(list)
    by_gene: defaultdict[str, list[KeyChange]] = defaultdict(list)
    for change in changes:
        by_category[change.category].append(change)
        by_gene[change.gene or "Unknown"].append(change)

    def carriers_of(group: Iterable[KeyChange]) -> int:
        return _union(carriers.get(change.variant_key_id, set()) for change in group)

    genes = []
    for gene in sorted(by_gene, key=gene_sort_key):
        group = by_gene[gene]
        per_category = {name: sum(change.category == name for change in group) for name in C.CHANGE_CATEGORIES}
        genes.append(GeneSummary(gene, per_category, len(group), carriers_of(group)))
    listed = [change for change in changes if category is None or change.category == category]
    listed.sort(
        key=lambda change: (
            change.variant_key_id not in carriers,
            CATEGORY_ORDER[change.category],
            gene_sort_key(change.gene),
            change.variant_key_id,
        )
    )
    return Comparison(
        old=old,
        new=new,
        all_keys=all_keys,
        category=category,
        stored_keys=len(stored),
        total=len(changes),
        changes=listed,
        categories=[
            CategorySummary(name, len(by_category[name]), carriers_of(by_category[name])) for name in C.CHANGE_CATEGORIES
        ],
        genes=genes,
        carriers_affected=carriers_of(changes),
        carriers_by_key={key_id: len(patients) for key_id, patients in carriers.items()},
    )


@dataclass(frozen=True)
class ChangeRow:
    """A listed change with its variant key, carrier count and the new release's record."""

    change: KeyChange
    variant_key: VariantKey
    carriers: int
    known: KnownVariant | None


def change_rows(changes: Sequence[KeyChange], comparison: Comparison) -> list[ChangeRow]:
    """Rows for one page of changes (two bulk queries: variant keys and new-release records)."""
    key_ids = [change.variant_key_id for change in changes]
    if not key_ids:
        return []
    keys = {key.id: key for key in db.session.scalars(select(VariantKey).where(VariantKey.id.in_(key_ids)))}
    known = {
        record.variant_key_id: record
        for record in db.session.scalars(
            select(KnownVariant).where(
                KnownVariant.release_id == comparison.new.id, KnownVariant.variant_key_id.in_(key_ids)
            )
        )
    }
    return [
        ChangeRow(
            change,
            keys[change.variant_key_id],
            comparison.carriers_by_key.get(change.variant_key_id, 0),
            known.get(change.variant_key_id),
        )
        for change in changes
    ]


# ---------------------------------------------------------------------------
# Rules
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class VersionView:
    """One rule version with its criteria in words."""

    version: RuleVersion
    spec: RuleSpec
    genetic: str
    clinical: str


@dataclass(frozen=True)
class RuleCard:
    """A rule of the current rule set: current version, history (newest first) and current findings."""

    rule: Rule
    current: VersionView
    history: list[VersionView]
    current_alerts: int
    current_notes: int


def _version_view(version: RuleVersion) -> VersionView:
    spec = RuleSpec.from_definition(version.definition, version.id)
    return VersionView(
        version, spec, describe_genetic_criterion(spec), describe_clinical_condition(version.definition.get("clinical"))
    )


def rule_cards() -> list[RuleCard]:
    """Every rule that has a current version, in natural code order (D1, D2, …)."""
    rules = db.session.scalars(
        select(Rule).options(selectinload(Rule.versions).joinedload(RuleVersion.imported_by))
    ).all()
    findings: defaultdict[str, dict[str, int]] = defaultdict(dict)
    grouped = (
        select(Alert.rule_code, Alert.kind, func.count(Alert.id))
        .where(Alert.status.in_(C.CURRENT_ALERT_STATUSES))
        .group_by(Alert.rule_code, Alert.kind)
    )
    for code, kind, count in db.session.execute(grouped):
        findings[code][kind] = count
    cards = []
    for rule in sorted(rules, key=lambda item: rule_code_sort_key(item.code)):
        current = rule.current_version
        if current is None:
            continue
        cards.append(
            RuleCard(
                rule=rule,
                current=_version_view(current),
                history=[_version_view(version) for version in sorted(rule.versions, key=lambda v: -v.version)],
                current_alerts=findings[rule.code].get("alert", 0),
                current_notes=findings[rule.code].get("note", 0),
            )
        )
    return cards


#: What importing a rule definition would do, by :attr:`PreviewRow.status`.
PREVIEW_STATUS_LABELS: dict[str, str] = {
    "new_rule": "New rule",
    "becomes_current": "Becomes current",
    "unchanged": "Already current",
    "history": "Kept as history",
}


@dataclass(frozen=True)
class PreviewRow:
    """One definition of a validated rule set compared with the stored current version of its code."""

    spec: RuleSpec
    genetic: str
    clinical: str
    current_version: int | None
    status: str


@dataclass(frozen=True)
class RuleSetPreview:
    """A validated rule set as it would be imported (nothing is stored)."""

    rule_set: str | None
    label: str | None
    rows: list[PreviewRow]

    @property
    def revisions(self) -> int:
        """Definitions that would become the current version of their code."""
        return sum(row.status in ("new_rule", "becomes_current") for row in self.rows)


def rule_set_preview(doc: Mapping[str, Any]) -> RuleSetPreview:
    """Compare a validated rule-set document with the current rule versions (read-only).

    The highest version per code in the document becomes current when it is newer than the
    stored current version; every other unseen version is kept as history. Whether an already
    stored (code, version) is identical is checked by the import itself.
    """
    current = dict(
        db.session.execute(select(RuleVersion.code, RuleVersion.version).where(RuleVersion.is_current.is_(True)))
    )
    specs = [RuleSpec.from_definition(definition) for definition in doc["rules"]]
    newest: dict[str, int] = {}
    for spec in specs:
        newest[spec.code] = max(spec.version, newest.get(spec.code, 0))
    rows = []
    for spec, definition in sorted(
        zip(specs, doc["rules"], strict=True), key=lambda item: (rule_code_sort_key(item[0].code), item[0].version)
    ):
        stored = current.get(spec.code)
        if spec.version == stored:
            status = "unchanged"
        elif spec.version == newest[spec.code] and (stored is None or spec.version > stored):
            status = "new_rule" if stored is None else "becomes_current"
        else:
            status = "history"
        rows.append(
            PreviewRow(
                spec,
                describe_genetic_criterion(spec),
                describe_clinical_condition(definition.get("clinical")),
                stored,
                status,
            )
        )
    return RuleSetPreview(doc.get("rule_set"), doc.get("label"), rows)


# ---------------------------------------------------------------------------
# Re-evaluation runs
# ---------------------------------------------------------------------------


def runs_statement(*, trigger: str | None = None, status: str | None = None) -> Select[tuple[ReevaluationRun]]:
    """Runs, newest first, with releases and initiator eager-loaded; optional filters."""
    statement = (
        select(ReevaluationRun)
        .options(
            joinedload(ReevaluationRun.old_release),
            joinedload(ReevaluationRun.new_release),
            joinedload(ReevaluationRun.initiated_by),
        )
        .order_by(ReevaluationRun.started_at.desc(), ReevaluationRun.id.desc())
    )
    if trigger:
        statement = statement.where(ReevaluationRun.trigger == trigger)
    if status:
        statement = statement.where(ReevaluationRun.status == status)
    return statement


def load_run(run_id: int) -> ReevaluationRun:
    """The run with its releases and initiator, or 404."""
    run = db.session.scalars(
        select(ReevaluationRun)
        .options(
            joinedload(ReevaluationRun.old_release),
            joinedload(ReevaluationRun.new_release),
            joinedload(ReevaluationRun.initiated_by),
        )
        .where(ReevaluationRun.id == run_id)
    ).one_or_none()
    if run is None:
        abort(404, description=f"Re-evaluation run {run_id} does not exist.")
    return run


def run_changes_statement(run: ReevaluationRun) -> Select[tuple[VariantChange]]:
    """The run's variant changes by category (upgrades first), gene and key."""
    category_rank = case(CATEGORY_ORDER, value=VariantChange.category, else_=len(CATEGORY_ORDER))
    return (
        select(VariantChange)
        .options(joinedload(VariantChange.variant_key))
        .where(VariantChange.run_id == run.id)
        .order_by(category_rank, VariantChange.gene, VariantChange.variant_key_id)
    )


def carriers_by_key(key_ids: Iterable[int]) -> dict[int, list[Patient]]:
    """Patients currently carrying each key, by MRN (one query)."""
    ids = sorted(set(key_ids))
    if not ids:
        return {}
    carriers: defaultdict[int, list[Patient]] = defaultdict(list)
    statement = (
        select(PatientVariant.variant_key_id, Patient)
        .join(Patient, Patient.id == PatientVariant.patient_id)
        .where(PatientVariant.variant_key_id.in_(ids))
        .order_by(Patient.mrn)
    )
    for key_id, patient in db.session.execute(statement):
        carriers[key_id].append(patient)
    return dict(carriers)


@dataclass
class AffectedPatient:
    """What one run did to one patient's findings."""

    patient: Patient
    alerts_created: int = 0
    notes_created: int = 0
    superseded: int = 0
    confirmed: int = 0
    notices: int = 0
    notifications: int = 0


def affected_patients(run: ReevaluationRun, limit: int = AFFECTED_PATIENTS_SHOWN) -> tuple[list[AffectedPatient], int]:
    """Patients whose alerts, notes or review notices the run changed (by MRN, at most ``limit``) and their total."""
    outcomes: defaultdict[int, Counter[str]] = defaultdict(Counter)
    events = (
        select(Alert.patient_id, AlertEvent.event, Alert.kind, Alert.notify, func.count(AlertEvent.id))
        .join(Alert, Alert.id == AlertEvent.alert_id)
        .where(AlertEvent.run_id == run.id, AlertEvent.event.in_(("created", "superseded", "confirmed")))
        .group_by(Alert.patient_id, AlertEvent.event, Alert.kind, Alert.notify)
    )
    for patient_id, event, kind, notify, count in db.session.execute(events):
        if event == "created":
            outcomes[patient_id]["alerts_created" if kind == "alert" else "notes_created"] += count
            if kind == "alert" and notify:
                outcomes[patient_id]["notifications"] += count
        else:
            outcomes[patient_id][event] += count
    notices = (
        select(ReviewNotice.patient_id, func.count(ReviewNotice.id))
        .where(ReviewNotice.run_id == run.id)
        .group_by(ReviewNotice.patient_id)
    )
    for patient_id, count in db.session.execute(notices):
        outcomes[patient_id]["notices"] += count
        outcomes[patient_id]["notifications"] += count
    if not outcomes:
        return [], 0
    patients = db.session.scalars(
        select(Patient).where(Patient.id.in_(list(outcomes))).order_by(Patient.mrn).limit(limit)
    ).all()
    return [AffectedPatient(patient, **outcomes[patient.id]) for patient in patients], len(outcomes)


@dataclass(frozen=True)
class RunSummary:
    """The parts of ``ReevaluationRun.summary`` the run page shows, normalised for every run type."""

    initial: bool
    categories: list[tuple[str, dict[str, int]]]
    genes: list[tuple[str, dict[str, Any]]]
    rules: list[tuple[str, dict[str, int]]]
    rule_changes: list[dict[str, Any]]
    superseded_by_reason: list[tuple[str, int]]


def run_summary(run: ReevaluationRun) -> RunSummary:
    """Normalise the run's JSON summary (absent for failed runs)."""
    summary: Mapping[str, Any] = run.summary or {}
    categories = summary.get("categories") or {}
    genes = summary.get("genes") or {}
    rules = summary.get("rules") or {}
    reasons = summary.get("superseded_by_reason") or {}
    return RunSummary(
        initial=bool(summary.get("initial")),
        categories=[(name, categories[name]) for name in C.CHANGE_CATEGORIES if name in categories],
        genes=sorted(genes.items(), key=lambda item: gene_sort_key(item[0])),
        rules=sorted(rules.items(), key=lambda item: rule_code_sort_key(item[0])),
        rule_changes=list(summary.get("rule_changes") or run.rule_change or []),
        superseded_by_reason=sorted(reasons.items(), key=lambda item: (-item[1], item[0])),
    )
