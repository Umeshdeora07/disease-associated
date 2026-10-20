"""F5 — reclassification-aware re-evaluation (SPEC §7) — the paper's main contribution.

Entry points (each logs one :class:`~ge_ehr.models.ReevaluationRun`, flushes, never commits):

* :func:`activate_release` — Algorithm 2. ``strategy="change_driven"`` compares the new release
  with the current one **only for variant keys stored for some patient** (``Δ``) and reconciles
  only the carriers of changed keys, restricted to those keys; ``strategy="full"`` (evaluation
  strategy 2) reconciles every patient with full scope. Both apply the same change-category
  semantics (paper Table IV) and reach the same alert state:

  ========================  ==========================================================
  upgrade                   created alerts ``is_reclassified``, priority ``high``,
                            ``notify`` unless severity ``info``; superseded findings
                            (e.g. the D6 VUS monitoring alert) reason ``reclassified``
  downgrade                 superseded reason ``downgrade``; one ReviewNotice per
                            (patient, variant, run) when a non-info alert was superseded;
                            created findings (e.g. D6) are silent
  within_actionable /       still-firing alerts confirmed under the new release (no
  within_non_actionable     notification); findings that stop applying superseded
                            silently (reason ``reclassified``); findings that newly apply
                            (mechanism-only change) notify as usual (§6.3)
  ========================  ==========================================================

  A key absent from the new release although it was listed before ("removed from ClinVar")
  is not classified (NC) and its superseded findings get reason ``variant_removed``.

  The **first activation** (no current release) is an initial classification, not a
  reclassification: alerts are not flagged, no notices, ``summary.initial = true``.

* :func:`apply_rule_set` — rule revision (§7.3): imports the rule set, then re-runs every revised
  rule for the carriers of its old and new genes (``strategy="full"``: every patient).
* :func:`full_recompute` — reconciles every patient against the current release and rules.
* :func:`alerts_as_of` / :func:`provenance_as_of` — point-in-time view (§7.5).
* :func:`release_history` — activated releases in activation order.

**PatientVariant bookkeeping.** A variant first classified against a release
(``classification_release_id IS NULL``) gets ``reported_classification =
current_classification`` = its class and no reclassification count. Afterwards, every class change
caused by an activation increments ``reclassified_count`` and sets ``last_reclassified_at`` /
``last_reclassified_run_id`` (this drives FHIR ``amended``). After every activation all
PatientVariants point to the new release (``classification_release_id``): unchanged keys were
compared and found identical, so the new release backs their classification too (one bulk
UPDATE; no record is re-evaluated for it).

**Re-activation.** Re-activating the current release raises :class:`ValueError`. Activating an
older, previously activated release is a *new* activation: it is compared with the current
release like any other and receives a new ``sequence`` (the release keeps only its latest
sequence; earlier activations remain visible as runs).

**Failure handling (§7.6).** Invalid input (unknown strategy, a release that is already current or
not stored, an invalid rule set) raises before any run is recorded. Any other exception rolls the
session back, records a ``failed`` run with the error text in a fresh transaction (committed) and
is re-raised; the release stays non-current and no alert changes.

Owned by the Reeval work package.
"""

from __future__ import annotations

import logging
import time
from collections import Counter, defaultdict
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from itertools import batched
from typing import Any

from sqlalchemy import func, or_, select, update
from sqlalchemy.orm import joinedload, selectinload

from .. import constants as C
from ..extensions import db
from ..models import (
    RUN_COUNTERS,
    Alert,
    AlertEvent,
    KbRelease,
    Patient,
    PatientVariant,
    ReevaluationRun,
    ReviewNotice,
    RuleVersion,
    User,
    VariantChange,
    VariantKey,
    utcnow,
)
from ..security import audit
from . import alerting, classification, clinvar
from .alerting import ReconcileResult
from .classification import IN_CLAUSE_CHUNK, KnownClass
from .clinvar import KeyChange
from .rules import ClassifiedVariant, RuleDefinitionError, rule_code_sort_key

logger = logging.getLogger(__name__)

#: Strategies of :func:`activate_release` and :func:`apply_rule_set`.
STRATEGIES: tuple[str, ...] = ("change_driven", "full")
#: Patients loaded (with diagnosis, medications and variants) per batch.
PATIENT_BATCH = 500
#: Changes listed in ``ReevaluationRun.summary["changes"]`` (all are stored as VariantChange rows).
MAX_SUMMARY_CHANGES = 1_000

# Superseded reason of the findings of a changed key, by change category.
_CATEGORY_REASONS: dict[str, str] = {
    "upgrade": "reclassified",
    "downgrade": "downgrade",
    "within_actionable": "reclassified",
    "within_non_actionable": "reclassified",
}
_REMOVED_REASON = "variant_removed"
# Findings of unchanged keys only change when the stored state had drifted from the knowledge base.
_DEFAULT_REASON = "reclassified"
_RULE_REASON = "rule_revised"
_REVIEW_ADVICE = (
    "Review any treatment change made on the basis of this alert; GE-EHR never reverses therapy automatically."
)

# Applies the notification / notice semantics of a run to one patient's reconciliation results.
_Semantics = Callable[[Patient, Sequence[ReconcileResult], Sequence[ClassifiedVariant]], None]


# ---------------------------------------------------------------------------
# Run bookkeeping
# ---------------------------------------------------------------------------


@dataclass
class _RunInfo:
    """What is needed to record a run — also after a rollback (ids only, no ORM objects)."""

    trigger: str
    strategy: str
    old_release_id: int | None
    new_release_id: int | None
    user_id: int | None
    started_at: datetime
    clock: float
    rule_change: list[dict[str, Any]] | None = None

    def elapsed_ms(self) -> float:
        return (time.perf_counter() - self.clock) * 1000


def _run_info(trigger: str, strategy: str, old: KbRelease | None, new: KbRelease | None, user: User | None) -> _RunInfo:
    return _RunInfo(
        trigger=trigger,
        strategy=strategy,
        old_release_id=old.id if old is not None else None,
        new_release_id=new.id if new is not None else None,
        user_id=user.id if user is not None else None,
        started_at=utcnow(),
        clock=time.perf_counter(),
    )


def _start_run(info: _RunInfo, user: User | None) -> ReevaluationRun:
    run = ReevaluationRun(
        trigger=info.trigger,
        strategy=info.strategy,
        old_release_id=info.old_release_id,
        new_release_id=info.new_release_id,
        rule_change=info.rule_change,
        started_at=info.started_at,
        status="running",
        initiated_by=user,
    )
    db.session.add(run)
    db.session.flush()
    return run


def _run_detail(run: ReevaluationRun) -> dict[str, Any]:
    """Audit detail of a run: identity, status and every counter."""
    detail: dict[str, Any] = {
        "trigger": run.trigger,
        "strategy": run.strategy,
        "status": run.status,
        "old_release_id": run.old_release_id,
        "new_release_id": run.new_release_id,
        "duration_ms": round(run.duration_ms or 0.0, 1),
    }
    if run.rule_change:
        detail["rule_change"] = run.rule_change
    if run.error:
        detail["error"] = run.error
    detail.update({name: getattr(run, name) for name in RUN_COUNTERS})
    return detail


def _finish_run(run: ReevaluationRun, engine: _Engine, info: _RunInfo, summary: dict[str, Any]) -> None:
    for name in RUN_COUNTERS:
        setattr(run, name, engine.counts[name])
    run.summary = summary
    run.status = "success"
    run.finished_at = utcnow()
    run.duration_ms = info.elapsed_ms()
    db.session.flush()
    audit("reevaluation_run", "reevaluation_run", run.id, detail=_run_detail(run), user=engine.user)
    logger.info(
        "Re-evaluation run %d (%s, %s) finished in %.0f ms: %d changed key(s), %d patient(s) re-evaluated, "
        "%d record(s) examined, %d alert(s) and %d note(s) created, %d superseded, %d confirmed, "
        "%d review notice(s), %d notification(s)",
        run.id,
        run.trigger,
        run.strategy,
        run.duration_ms,
        run.changed_variants,
        run.patients_reevaluated,
        run.records_examined,
        run.alerts_created,
        run.notes_created,
        run.alerts_superseded,
        run.alerts_confirmed,
        run.review_notices,
        run.notifications,
    )


def _existing_id(model: type[KbRelease | User], object_id: int | None) -> int | None:
    """``object_id`` if the row still exists (it may have been rolled back), else None."""
    return object_id if object_id is not None and db.session.get(model, object_id) is not None else None


def _record_failed_run(info: _RunInfo, exc: Exception) -> None:
    """Roll back, then record a failed run (and its audit row) in a fresh, committed transaction."""
    error = f"{type(exc).__name__}: {exc}" if str(exc) else type(exc).__name__
    logger.error("Re-evaluation (%s, %s) failed and was rolled back: %s", info.trigger, info.strategy, error)
    db.session.rollback()
    try:
        user_id = _existing_id(User, info.user_id)
        run = ReevaluationRun(
            trigger=info.trigger,
            strategy=info.strategy,
            old_release_id=_existing_id(KbRelease, info.old_release_id),
            new_release_id=_existing_id(KbRelease, info.new_release_id),
            rule_change=info.rule_change,
            initiated_by_id=user_id,
            started_at=info.started_at,
            finished_at=utcnow(),
            duration_ms=info.elapsed_ms(),
            status="failed",
            error=error,
        )
        db.session.add(run)
        db.session.flush()
        audit(
            "reevaluation_run",
            "reevaluation_run",
            run.id,
            detail=_run_detail(run),
            user=db.session.get(User, user_id) if user_id is not None else None,
        )
        db.session.commit()
    except Exception:
        # Never mask the original error: the caller re-raises it whether or not it was recorded.
        db.session.rollback()
        logger.exception("Could not record the failed re-evaluation run")


@contextmanager
def _failure_recorded(info: _RunInfo, *, passthrough: tuple[type[Exception], ...] = ()) -> Iterator[None]:
    """Record a failed run for any exception except ``passthrough`` ones (input errors), then re-raise."""
    try:
        yield
    except passthrough:
        raise
    except Exception as exc:
        _record_failed_run(info, exc)
        raise


def _check_strategy(strategy: str) -> None:
    if strategy not in STRATEGIES:
        raise ValueError(f"Unknown re-evaluation strategy {strategy!r}; expected one of {', '.join(STRATEGIES)}.")


# ---------------------------------------------------------------------------
# Engine: reconciliation, counters, notices
# ---------------------------------------------------------------------------


class _Engine:
    """Reconciles patients for one run and keeps its counters and per-group outcome breakdown."""

    def __init__(
        self,
        run: ReevaluationRun,
        release: KbRelease | None,
        *,
        user: User | None,
        group_of: Callable[[Alert], str | None] = lambda _alert: None,
    ) -> None:
        self.run = run
        self.release = release
        self.user = user
        self.now = run.started_at
        self.rules = alerting.load_current_rules()
        # Keep the current rule versions in the identity map: confirming an alert under a new rule
        # version looks the version up by id, which must not cost a query per alert.
        self._rule_versions = RuleVersion.current_versions()
        self.group_of = group_of
        self.counts: Counter[str] = Counter()
        self.groups: defaultdict[str, Counter[str]] = defaultdict(Counter)
        self.reasons: Counter[str] = Counter()
        self.carrier_patients: set[int] = set()
        self.carriers_by_key: Counter[int] = Counter()

    def reconcile(
        self,
        patient: Patient,
        classified: Sequence[ClassifiedVariant],
        *,
        trigger: str,
        reason: str,
        keys: Iterable[int] | None = None,
        rule_codes: Iterable[str] | None = None,
    ) -> ReconcileResult:
        return alerting.reconcile_patient(
            patient,
            self.release,
            self.rules,
            trigger=trigger,
            variant_key_ids=keys,
            rule_codes=rule_codes,
            run=self.run,
            superseded_reason=reason,
            user=self.user,
            now=self.now,
            classified=classified,
        )

    def _count(self, alert: Alert, counter: str) -> None:
        self.counts[counter] += 1
        group = self.group_of(alert)
        if group is not None:
            self.groups[group][counter] += 1

    def record(self, results: Iterable[ReconcileResult]) -> None:
        """Add one patient's results (after the run's semantics were applied) to the counters."""
        self.counts["patients_reevaluated"] += 1
        for result in results:
            self.counts["records_examined"] += result.records_examined
            for alert in result.created:
                self._count(alert, "alerts_created" if alert.kind == "alert" else "notes_created")
                if alert.notify:
                    self._count(alert, "notifications")
            for alert in result.superseded:
                self._count(alert, "alerts_superseded")
                self.reasons[alert.superseded_reason] += 1
            for alert in result.confirmed:
                self._count(alert, "alerts_confirmed")

    def notice(
        self, patient: Patient, variant_key_id: int, alerts: Sequence[Alert], *, old: str, new: str, message: str
    ) -> ReviewNotice:
        """Open one review notice linking the superseded ``alerts`` (counts as a notification)."""
        notice = ReviewNotice(
            patient=patient,
            variant_key_id=variant_key_id,
            run=self.run,
            old_classification=old,
            new_classification=new,
            message=message,
            status="open",
            created_at=self.now,
        )
        db.session.add(notice)
        for alert in alerts:
            alert.review_notice = notice
        self._count(alerts[0], "review_notices")
        self._count(alerts[0], "notifications")
        return notice

    def carrier(self, patient_id: int, variant_key_id: int | None = None) -> None:
        self.carrier_patients.add(patient_id)
        if variant_key_id is not None:
            self.carriers_by_key[variant_key_id] += 1

    def outcome_counts(self, group: str) -> dict[str, int]:
        counts = self.groups.get(group, Counter())
        return {
            name: counts[name]
            for name in (
                "alerts_created",
                "notes_created",
                "alerts_superseded",
                "alerts_confirmed",
                "review_notices",
                "notifications",
            )
        }


def _classified(variant: PatientVariant, known: KnownClass) -> ClassifiedVariant:
    """A stored variant classified as ``known`` (its key must be loaded)."""
    return ClassifiedVariant(
        patient_variant_id=variant.id,
        variant_key_id=variant.variant_key_id,
        gene=variant.gene or variant.variant_key.gene or "",
        classification=known.classification,
        mechanism=known.mechanism,
        zygosity=variant.zygosity,
        label=variant.variant_key.label,
        is_expert_panel=known.is_expert_panel,
    )


def _patient_batches(patient_ids: Iterable[int]) -> Iterator[list[Patient]]:
    """Patients with diagnosis and medications, :data:`PATIENT_BATCH` per query, ordered by id."""
    for chunk in batched(patient_ids, PATIENT_BATCH):
        statement = (
            select(Patient)
            .where(Patient.id.in_(chunk))
            .options(selectinload(Patient.diagnosis), selectinload(Patient.medications))
            .order_by(Patient.id)
        )
        yield list(db.session.scalars(statement))


def _group_by_patient(statement: Any) -> dict[int, list[PatientVariant]]:
    grouped: dict[int, list[PatientVariant]] = defaultdict(list)
    for variant in db.session.scalars(statement):
        grouped[variant.patient_id].append(variant)
    return grouped


def _variants_query() -> Any:
    return select(PatientVariant).options(joinedload(PatientVariant.variant_key)).order_by(PatientVariant.id)


def _variants_of_patients(patient_ids: Sequence[int]) -> dict[int, list[PatientVariant]]:
    return _group_by_patient(_variants_query().where(PatientVariant.patient_id.in_(patient_ids)))


def _variants_with_keys(key_ids: Iterable[int]) -> dict[int, list[PatientVariant]]:
    """Carriers of the given keys, one query per :data:`IN_CLAUSE_CHUNK` keys."""
    grouped: dict[int, list[PatientVariant]] = defaultdict(list)
    for chunk in batched(sorted(key_ids), IN_CLAUSE_CHUNK):
        for patient_id, variants in _group_by_patient(
            _variants_query().where(PatientVariant.variant_key_id.in_(chunk))
        ).items():
            grouped[patient_id].extend(variants)
    return grouped


def _reconcile_grouped(
    engine: _Engine,
    patient: Patient,
    classified: Sequence[ClassifiedVariant],
    groups: Mapping[str, set[int]],
    *,
    trigger: str,
    full_scope: bool,
    default_reason: str = _DEFAULT_REASON,
) -> list[ReconcileResult]:
    """Reconcile ``patient`` once per superseded reason (reasons partition the variant keys).

    Findings are per (rule, variant), so reconciling disjoint key sets separately gives the same
    result as one call over their union. With ``full_scope`` and at most one reason the patient
    is reconciled once without a key restriction (``default_reason`` when it has no variant).
    """
    if full_scope and len(groups) <= 1:
        reason = next(iter(groups), default_reason)
        return [engine.reconcile(patient, classified, trigger=trigger, reason=reason)]
    return [
        engine.reconcile(patient, classified, trigger=trigger, reason=reason, keys=keys)
        for reason, keys in sorted(groups.items())
    ]


# ---------------------------------------------------------------------------
# Release activation (Algorithm 2)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _Change:
    """One element of Δ with its classification under the new release."""

    key: KeyChange
    known: KnownClass
    label: str
    gene: str

    @property
    def category(self) -> str:
        return self.key.category

    @property
    def removed(self) -> bool:
        """The key is not listed in the new release (e.g. removed from ClinVar) — not classified."""
        return self.known.known_variant_id is None

    @property
    def reason(self) -> str:
        return _REMOVED_REASON if self.removed else _CATEGORY_REASONS[self.category]


def _stored_key_ids() -> set[int]:
    """``V_obs``: distinct variant keys of all PatientVariants."""
    return set(db.session.scalars(select(PatientVariant.variant_key_id).distinct()))


def _release_changes(old: KbRelease | None, new: KbRelease, stored: set[int]) -> dict[int, _Change]:
    """Δ restricted to stored keys, with each key's new classification, label and gene (bulk queries)."""
    delta = clinvar.diff_releases(old, new, stored)
    key_ids = [change.variant_key_id for change in delta]
    known = classification.classify_keys(new, key_ids)
    keys: dict[int, tuple[str, str | None]] = {}
    for chunk in batched(key_ids, IN_CLAUSE_CHUNK):
        statement = select(VariantKey).where(VariantKey.id.in_(chunk))
        keys.update({key.id: (key.label, key.gene) for key in db.session.scalars(statement)})
    return {
        change.variant_key_id: _Change(
            key=change,
            known=known[change.variant_key_id],
            label=keys[change.variant_key_id][0],
            gene=change.gene or keys[change.variant_key_id][1] or "",
        )
        for change in delta
    }


def _reclassify(engine: _Engine, variant: PatientVariant, change: _Change) -> None:
    """PatientVariant bookkeeping for a changed key (SPEC §7.1)."""
    new_class = change.key.new_class
    if variant.classification_release_id is None:
        variant.reported_classification = variant.current_classification = new_class
    elif variant.current_classification != new_class:
        variant.current_classification = new_class
        variant.reclassified_count = (variant.reclassified_count or 0) + 1
        variant.last_reclassified_at = engine.now
        variant.last_reclassified_run = engine.run
    variant.classification_release = engine.release


def _release_semantics(engine: _Engine, changes: Mapping[int, _Change], *, initial: bool) -> _Semantics:
    """Flags, notifications and review notices of a release activation (category semantics)."""

    def apply(patient: Patient, results: Sequence[ReconcileResult], _classified: Sequence[ClassifiedVariant]) -> None:
        if initial:
            return  # initial classification: §6.3 defaults, nothing is a reclassification
        downgraded: defaultdict[int, list[Alert]] = defaultdict(list)
        for result in results:
            for alert in result.created:
                change = changes.get(alert.variant_key_id)
                if change is None:
                    continue
                if change.category == "upgrade":
                    alert.is_reclassified = True
                    if alert.kind == "alert":
                        alert.priority = "high"
                        alert.notify = alert.severity != "info"
                elif change.category == "downgrade":
                    alert.notify = False
            for alert in result.superseded:
                change = changes.get(alert.variant_key_id)
                if change is not None and change.category == "downgrade" and alert.kind == "alert":
                    downgraded[alert.variant_key_id].append(alert)
        for key_id in sorted(downgraded):
            alerts = downgraded[key_id]
            if any(alert.severity != "info" for alert in alerts):
                change = changes[key_id]
                engine.notice(
                    patient,
                    key_id,
                    alerts,
                    old=change.key.old_class,
                    new=change.key.new_class,
                    message=_downgrade_message(change, alerts, engine.release),
                )

    return apply


def _rule_codes_text(alerts: Iterable[Alert]) -> str:
    return ", ".join(sorted({alert.rule_code for alert in alerts}, key=rule_code_sort_key))


def _downgrade_message(change: _Change, alerts: Sequence[Alert], release: KbRelease) -> str:
    if change.removed:
        what = (
            f"{change.gene} variant {change.label} ({change.key.old_class}) is no longer listed in {release.label} "
            "and is now not classified (NC)"
        )
    else:
        what = (
            f"{change.gene} variant {change.label} was downgraded from {change.key.old_class} to "
            f"{change.key.new_class} in {release.label}"
        )
    return f"{what}; alert(s) {_rule_codes_text(alerts)} superseded. {_REVIEW_ADVICE}"


def _reevaluate_changed_carriers(engine: _Engine, changes: Mapping[int, _Change], apply: _Semantics) -> None:
    """Change-driven: only carriers of changed keys, only those keys (bulk-loaded carriers)."""
    carriers = _variants_with_keys(changes)
    for patients in _patient_batches(sorted(carriers)):
        for patient in patients:
            classified: list[ClassifiedVariant] = []
            groups: defaultdict[str, set[int]] = defaultdict(set)
            for variant in carriers[patient.id]:
                change = changes[variant.variant_key_id]
                _reclassify(engine, variant, change)
                engine.carrier(patient.id, variant.variant_key_id)
                classified.append(_classified(variant, change.known))
                groups[change.reason].add(variant.variant_key_id)
            results = _reconcile_grouped(
                engine, patient, classified, groups, trigger="clinvar_release", full_scope=False
            )
            apply(patient, results, classified)
            engine.record(results)


def _reevaluate_all_patients(
    engine: _Engine,
    *,
    trigger: str,
    apply: _Semantics,
    changes: Mapping[int, _Change] | None = None,
    default_reason: str = _DEFAULT_REASON,
    carrier_genes: frozenset[str] | None = None,
) -> None:
    """Full strategy: every patient, every variant classified under ``engine.release``.

    ``changes`` (release activation) drive PatientVariant bookkeeping and the superseded reason
    of their keys; other keys use ``default_reason``. ``carrier_genes`` (rule revision) marks
    patients carrying a variant in those genes as carriers.
    """
    changes = changes or {}
    patient_ids = db.session.scalars(select(Patient.id).order_by(Patient.id)).all()
    for patients in _patient_batches(patient_ids):
        variants_by_patient = _variants_of_patients([patient.id for patient in patients])
        known = classification.classify_keys(
            engine.release,
            {variant.variant_key_id for variants in variants_by_patient.values() for variant in variants},
        )
        for patient in patients:
            variants = variants_by_patient.get(patient.id, [])
            classified: list[ClassifiedVariant] = []
            groups: defaultdict[str, set[int]] = defaultdict(set)
            for variant in variants:
                change = changes.get(variant.variant_key_id)
                if change is not None:
                    _reclassify(engine, variant, change)
                    engine.carrier(patient.id, variant.variant_key_id)
                    groups[change.reason].add(variant.variant_key_id)
                else:
                    groups[default_reason].add(variant.variant_key_id)
                classified.append(_classified(variant, known[variant.variant_key_id]))
            if carrier_genes is not None and any(variant.gene in carrier_genes for variant in classified):
                engine.carrier(patient.id)
            results = _reconcile_grouped(
                engine, patient, classified, groups, trigger=trigger, full_scope=True, default_reason=default_reason
            )
            apply(patient, results, classified)
            engine.record(results)


def _complete_classification_bookkeeping(release: KbRelease) -> None:
    """After the changed keys: first classification of never-classified variants, then point all
    variants to the new release (one bulk UPDATE)."""
    pending = list(db.session.scalars(select(PatientVariant).where(PatientVariant.classification_release_id.is_(None))))
    if pending:
        known = classification.classify_keys(release, {variant.variant_key_id for variant in pending})
        for variant in pending:
            variant.reported_classification = variant.current_classification = known[
                variant.variant_key_id
            ].classification
            variant.classification_release = release
    db.session.execute(
        update(PatientVariant)
        .where(
            or_(
                PatientVariant.classification_release_id.is_(None),
                PatientVariant.classification_release_id != release.id,
            )
        )
        .values(classification_release_id=release.id)
    )


def _record_variant_changes(run: ReevaluationRun, changes: Mapping[int, _Change], engine: _Engine) -> None:
    db.session.add_all(
        VariantChange(
            run_id=run.id,
            variant_key_id=key_id,
            gene=change.gene or None,
            old_classification=change.key.old_class,
            new_classification=change.key.new_class,
            old_mechanism=change.key.old_mechanism,
            new_mechanism=change.key.new_mechanism,
            category=change.category,
            carrier_count=engine.carriers_by_key[key_id],
        )
        for key_id, change in changes.items()
    )
    db.session.flush()


def _make_current(release: KbRelease, now: datetime) -> None:
    """Switch the current release; the activation gets the next sequence number."""
    db.session.execute(
        update(KbRelease).where(KbRelease.is_current.is_(True), KbRelease.id != release.id).values(is_current=False)
    )
    release.sequence = (db.session.scalar(select(func.max(KbRelease.sequence))) or 0) + 1
    release.is_current = True
    release.activated_at = now
    db.session.flush()


def _gene_order(gene: str) -> tuple[int, str]:
    return (C.GENES.index(gene), gene) if gene in C.GENES else (len(C.GENES), gene)


def _release_summary(
    engine: _Engine, changes: Mapping[int, _Change], old: KbRelease | None, new: KbRelease, *, initial: bool
) -> dict[str, Any]:
    """Per-category and per-gene counts, superseded reasons and the list of changes."""
    categories: dict[str, dict[str, int]] = {
        category: {"changed_variants": 0, "carriers": 0} for category in C.CHANGE_CATEGORIES
    }
    genes: dict[str, dict[str, Any]] = {}
    for key_id, change in changes.items():
        carriers = engine.carriers_by_key[key_id]
        categories[change.category]["changed_variants"] += 1
        categories[change.category]["carriers"] += carriers
        gene = genes.setdefault(
            change.gene or "unknown",
            {"changed_variants": 0, "carriers": 0, "categories": dict.fromkeys(C.CHANGE_CATEGORIES, 0)},
        )
        gene["changed_variants"] += 1
        gene["carriers"] += carriers
        gene["categories"][change.category] += 1
    for category, counts in categories.items():
        counts.update(engine.outcome_counts(category))
    listed = list(changes.items())[:MAX_SUMMARY_CHANGES]
    return {
        "initial": initial,
        "old_release": old.label if old is not None else None,
        "new_release": new.label,
        "categories": categories,
        "genes": {name: genes[name] for name in sorted(genes, key=_gene_order)},
        "superseded_by_reason": dict(sorted(engine.reasons.items())),
        "changes_total": len(changes),
        "changes_truncated": len(changes) > len(listed),
        "changes": [
            {
                "variant_key_id": key_id,
                "label": change.label,
                "gene": change.gene,
                "old_classification": change.key.old_class,
                "new_classification": change.key.new_class,
                "old_mechanism": change.key.old_mechanism,
                "new_mechanism": change.key.new_mechanism,
                "category": change.category,
                "removed": change.removed,
                "carriers": engine.carriers_by_key[key_id],
            }
            for key_id, change in listed
        ],
    }


def activate_release(
    new_release: KbRelease, *, strategy: str = "change_driven", user: User | None = None
) -> ReevaluationRun:
    """Make ``new_release`` current and re-evaluate affected carriers (Algorithm 2, SPEC §7.1/§7.2).

    ``strategy="change_driven"`` examines only stored keys and only the carriers of changed keys
    (``records_examined`` = carrier × changed-key records); ``"full"`` reconciles every patient
    (``records_examined`` = all patient-variant records). Both record one VariantChange per
    changed stored key and apply the category semantics described in the module docstring.

    Raises :class:`ValueError` (before any run is recorded) for an unknown strategy, an unstored
    release or the release that is already current. Other failures are recorded as a failed run
    and re-raised (§7.6).
    """
    _check_strategy(strategy)
    if new_release.id is None:
        raise ValueError(f"Release {new_release.label!r} is not stored yet; import it before activating it.")
    old_release = KbRelease.current()
    if new_release.is_current or (old_release is not None and old_release.id == new_release.id):
        raise ValueError(f"Release {new_release.label!r} is already the current release; activate a different release.")
    info = _run_info("clinvar_release", strategy, old_release, new_release, user)
    initial = old_release is None
    with _failure_recorded(info):
        run = _start_run(info, user)
        stored = _stored_key_ids()
        changes = _release_changes(old_release, new_release, stored)

        def category_of(alert: Alert) -> str | None:
            change = changes.get(alert.variant_key_id)
            return change.category if change is not None else None

        engine = _Engine(run, new_release, user=user, group_of=category_of)
        engine.counts["stored_keys_compared"] = len(stored)
        engine.counts["changed_variants"] = len(changes)
        semantics = _release_semantics(engine, changes, initial=initial)
        if strategy == "change_driven":
            _reevaluate_changed_carriers(engine, changes, semantics)
        else:
            _reevaluate_all_patients(engine, trigger="clinvar_release", apply=semantics, changes=changes)
        engine.counts["carriers_affected"] = len(engine.carrier_patients)
        _complete_classification_bookkeeping(new_release)
        _record_variant_changes(run, changes, engine)
        _make_current(new_release, engine.now)
        _finish_run(run, engine, info, _release_summary(engine, changes, old_release, new_release, initial=initial))
        audit(
            "release_activated",
            "kb_release",
            new_release.id,
            detail={
                "label": new_release.label,
                "sequence": new_release.sequence,
                "previous_release_id": info.old_release_id,
                "previous_release": old_release.label if old_release is not None else None,
                "strategy": strategy,
                "run_id": run.id,
                "initial": initial,
            },
            user=user,
        )
    return run


# ---------------------------------------------------------------------------
# Rule revision (SPEC §7.3) and full recomputation
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _Revision:
    """A rule code whose current version changed, with the genes it affects (old ∪ new)."""

    code: str
    old_version: int | None
    new_version: int
    genes: frozenset[str]

    @property
    def label(self) -> str:
        if self.old_version is None:
            return f"{self.code} v{self.new_version} (new rule)"
        return f"{self.code} v{self.old_version} → v{self.new_version}"


def _revisions(changes: Sequence[Mapping[str, Any]]) -> dict[str, _Revision]:
    revisions: dict[str, _Revision] = {}
    for change in changes:
        new = db.session.get(RuleVersion, change["new_rule_version_id"])
        old_id = change["old_rule_version_id"]
        old = db.session.get(RuleVersion, old_id) if old_id is not None else None
        genes = frozenset(new.genes) | (frozenset(old.genes) if old is not None else frozenset())
        revisions[change["code"]] = _Revision(change["code"], change["old_version"], change["new_version"], genes)
    return revisions


def _withdrawal_semantics(engine: _Engine, cause: Callable[[Sequence[Alert]], str]) -> _Semantics:
    """One review notice per (patient, variant) whose non-info alert(s) were superseded."""

    def apply(patient: Patient, results: Sequence[ReconcileResult], classified: Sequence[ClassifiedVariant]) -> None:
        variants = {variant.variant_key_id: variant for variant in classified}
        superseded: defaultdict[int, list[Alert]] = defaultdict(list)
        created: defaultdict[int, list[Alert]] = defaultdict(list)
        for result in results:
            for alert in result.superseded:
                if alert.kind == "alert":
                    superseded[alert.variant_key_id].append(alert)
            for alert in result.created:
                created[alert.variant_key_id].append(alert)
        for key_id in sorted(superseded):
            alerts = superseded[key_id]
            if all(alert.severity == "info" for alert in alerts):
                continue
            variant = variants.get(key_id)
            new_class = variant.classification if variant is not None else alerts[0].classification
            engine.notice(
                patient,
                key_id,
                alerts,
                old=alerts[0].classification,
                new=new_class,
                message=_withdrawal_message(cause(alerts), alerts, created.get(key_id, []), variant),
            )

    return apply


def _finding_text(alert: Alert) -> str:
    return f"{alert.rule_code} ({alert.severity})" if alert.kind == "alert" else f"{alert.rule_code} note"


def _withdrawal_message(
    cause: str, alerts: Sequence[Alert], replacements: Sequence[Alert], variant: ClassifiedVariant | None
) -> str:
    withdrawn = ", ".join(_finding_text(alert) for alert in alerts)
    if variant is not None:
        subject = f"{variant.gene} variant {variant.label} ({variant.classification})"
    else:
        subject = f"variant {alerts[0].variant_key.label}"
    text = f"{cause}: alert(s) {withdrawn} for {subject} superseded"
    if replacements:
        text += "; now " + ", ".join(_finding_text(alert) for alert in replacements)
    return f"{text}. {_REVIEW_ADVICE}"


def _reevaluate_rule_carriers(engine: _Engine, revisions: Mapping[str, _Revision], apply: _Semantics) -> Counter[str]:
    """Change-driven rule revision: carriers of the revised rules' genes, only those rules.

    Returns the number of carriers per gene.
    """
    genes = frozenset().union(*(revision.genes for revision in revisions.values()))
    carriers = _group_by_patient(_variants_query().where(PatientVariant.gene.in_(sorted(genes))))
    known = classification.classify_keys(
        engine.release, {variant.variant_key_id for variants in carriers.values() for variant in variants}
    )
    carriers_by_gene: Counter[str] = Counter()
    for patients in _patient_batches(sorted(carriers)):
        for patient in patients:
            classified = [_classified(variant, known[variant.variant_key_id]) for variant in carriers[patient.id]]
            carried = {variant.gene for variant in classified}
            carriers_by_gene.update(carried)
            codes = {code for code, revision in revisions.items() if revision.genes & carried}
            engine.carrier(patient.id)
            result = engine.reconcile(
                patient, classified, trigger="rule_revision", reason=_RULE_REASON, rule_codes=codes
            )
            apply(patient, [result], classified)
            engine.record([result])
    return carriers_by_gene


def _carriers_by_gene(genes: frozenset[str]) -> Counter[str]:
    statement = (
        select(PatientVariant.gene, func.count(func.distinct(PatientVariant.patient_id)))
        .where(PatientVariant.gene.in_(sorted(genes)))
        .group_by(PatientVariant.gene)
    )
    return Counter(dict(db.session.execute(statement).tuples().all()))


def apply_rule_set(
    doc: dict[str, Any], *, user: User | None = None, strategy: str = "change_driven"
) -> ReevaluationRun:
    """Import rule versions and re-evaluate the carriers of every revised rule (SPEC §7.3).

    :func:`~ge_ehr.services.alerting.import_rule_versions` validates and stores the document
    (an existing (code, version) must be identical); then, for every code whose current version
    changed, the carriers of its old and new genes are reconciled for that rule under the current
    release (``strategy="full"``: every patient, every rule). Still firing with the same severity →
    provenance update (``confirmed_rule_version_id``) without notification; newly firing → new
    alert that notifies (RS5); stopping or changing severity → superseded (reason
    ``rule_revised``) with a review notice when a non-info alert was withdrawn, plus the new
    finding if any. A rule set identical to the current one gives a no-op run.

    Raises :class:`~ge_ehr.services.rules.RuleDefinitionError` (no run recorded) for an invalid
    rule set and :class:`ValueError` for an unknown strategy; other failures are recorded as a
    failed run and re-raised.
    """
    _check_strategy(strategy)
    release = KbRelease.current()
    info = _run_info("rule_revision", strategy, release, release, user)
    with _failure_recorded(info, passthrough=(RuleDefinitionError,)):
        changes = alerting.import_rule_versions(doc, user=user)
        info.rule_change = [
            {"code": change["code"], "old_version": change["old_version"], "new_version": change["new_version"]}
            for change in changes
        ]
        run = _start_run(info, user)
        engine = _Engine(run, release, user=user, group_of=lambda alert: alert.rule_code)
        revisions = _revisions(changes)
        genes = frozenset().union(*(revision.genes for revision in revisions.values()))
        carriers_by_gene: Counter[str] = Counter()
        if revisions:

            def cause(alerts: Sequence[Alert]) -> str:
                codes = sorted({alert.rule_code for alert in alerts} & revisions.keys(), key=rule_code_sort_key)
                labels = [revisions[code].label for code in codes]
                return f"Rule revision {', '.join(labels)}" if labels else "Rule revision"

            semantics = _withdrawal_semantics(engine, cause)
            if strategy == "change_driven":
                carriers_by_gene = _reevaluate_rule_carriers(engine, revisions, semantics)
            else:
                _reevaluate_all_patients(
                    engine, trigger="rule_revision", apply=semantics, default_reason=_RULE_REASON, carrier_genes=genes
                )
                carriers_by_gene = _carriers_by_gene(genes)
        engine.counts["carriers_affected"] = len(engine.carrier_patients)
        summary = {
            "rule_set": doc.get("rule_set"),
            "label": doc.get("label"),
            "release": release.label if release is not None else None,
            "rule_changes": [
                {
                    "code": revision.code,
                    "old_version": revision.old_version,
                    "new_version": revision.new_version,
                    "genes": sorted(revision.genes, key=_gene_order),
                }
                for revision in revisions.values()
            ],
            "rules": {
                code: engine.outcome_counts(code)
                for code in sorted(revisions.keys() | engine.groups.keys(), key=rule_code_sort_key)
            },
            "genes": {gene: {"carriers": carriers_by_gene[gene]} for gene in sorted(genes, key=_gene_order)},
            "superseded_by_reason": dict(sorted(engine.reasons.items())),
        }
        _finish_run(run, engine, info, summary)
    return run


def full_recompute(*, user: User | None = None, trigger: str = "full_recompute") -> ReevaluationRun:
    """Reconcile every patient (full scope) against the current release and rules.

    In a consistent database this changes nothing (every event is handled when it happens); it
    repairs drift, e.g. after rule versions were bootstrapped without re-evaluation. Superseded
    findings get reason ``reclassified`` and a withdrawn non-info alert opens a review notice
    (actionable advice is never withdrawn silently). Raises :class:`ValueError` for an unknown
    trigger; other failures are recorded as a failed run and re-raised.
    """
    if trigger not in C.TRIGGERS:
        raise ValueError(f"Unknown trigger {trigger!r}; expected one of {', '.join(C.TRIGGERS)}.")
    release = KbRelease.current()
    info = _run_info(trigger, "full", release, release, user)
    with _failure_recorded(info):
        run = _start_run(info, user)
        engine = _Engine(run, release, user=user)
        basis = release.label if release is not None else "no knowledge-base release"
        semantics = _withdrawal_semantics(
            engine, lambda _alerts: f"Full re-evaluation under {basis} and the current rules"
        )
        _reevaluate_all_patients(engine, trigger=trigger, apply=semantics)
        summary = {
            "release": release.label if release is not None else None,
            "superseded_by_reason": dict(sorted(engine.reasons.items())),
        }
        _finish_run(run, engine, info, summary)
    return run


# ---------------------------------------------------------------------------
# Point-in-time view (SPEC §7.5) and release history
# ---------------------------------------------------------------------------


def _naive_utc(at: datetime) -> datetime:
    return at.astimezone(UTC).replace(tzinfo=None) if at.tzinfo is not None else at


def _display_order(alert: Alert) -> tuple[Any, ...]:
    """Alerts before notes, highest severity first, then rule code and creation order."""
    return (
        alert.kind != "alert",
        -C.SEVERITY_ORDER.get(alert.severity or "", -1),
        rule_code_sort_key(alert.rule_code),
        alert.id,
    )


def alerts_as_of(patient: Patient, at: datetime) -> list[Alert]:
    """Alerts and notes that were current for ``patient`` at time ``at`` (naive UTC or aware).

    Current at ``at`` means ``created_at ≤ at`` and (``superseded_at`` is null or ``> at``).
    Release, rule-version and variant provenance and the events are loaded with the alerts (no
    per-alert queries); :func:`provenance_as_of` gives the evidence an alert rested on at ``at``.
    Ordered alerts first, by severity, rule code and creation.
    """
    moment = _naive_utc(at)
    statement = (
        select(Alert)
        .where(
            Alert.patient_id == patient.id,
            Alert.created_at <= moment,
            or_(Alert.superseded_at.is_(None), Alert.superseded_at > moment),
        )
        .options(
            joinedload(Alert.variant_key),
            joinedload(Alert.created_release),
            joinedload(Alert.confirmed_release),
            joinedload(Alert.created_rule_version),
            joinedload(Alert.confirmed_rule_version),
            selectinload(Alert.events),
        )
    )
    return sorted(db.session.scalars(statement).unique(), key=_display_order)


def provenance_as_of(alert: Alert, at: datetime) -> AlertEvent | None:
    """The latest ``created``/``confirmed`` event of ``alert`` at or before ``at``.

    Its ``release``, ``rule_version`` and ``classification`` are the evidence the alert rested on
    at that time (later confirmations under newer releases or rule versions are ignored). None
    when the alert did not exist yet.
    """
    moment = _naive_utc(at)
    evidence = [event for event in alert.events if event.event in ("created", "confirmed") and event.at <= moment]
    return max(evidence, key=lambda event: (event.at, event.id), default=None)


def release_history() -> list[KbRelease]:
    """Activated releases ordered by activation ``sequence`` (a re-activated release appears once,
    at its latest activation)."""
    statement = select(KbRelease).where(KbRelease.sequence.is_not(None)).order_by(KbRelease.sequence)
    return list(db.session.scalars(statement))
