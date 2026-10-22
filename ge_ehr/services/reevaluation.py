"""F5 — reclassification-aware re-evaluation (SPEC §7) — the paper's main contribution.

Entry points (each logs one :class:`~ge_ehr.models.ReevaluationRun`, flushes, never commits):

* :func:`activate_release` — Algorithm 2. ``strategy="change_driven"`` compares the new release
  with the current one **only for variant keys stored for some patient** (Δ) and reconciles only
  the carriers of changed keys, restricted to those keys; ``strategy="full"`` (evaluation
  strategy 2) reconciles every patient with full scope. Both apply the same change-category
  semantics (paper Table IV) and reach the same set of current alerts:

  ========================  ==========================================================
  upgrade                   created findings ``is_reclassified``; created alerts get
                            priority ``high`` and ``notify`` unless severity ``info``;
                            superseded findings (e.g. the D6 VUS monitoring alert)
                            reason ``reclassified``
  downgrade                 superseded reason ``downgrade``; created findings (e.g. D6)
                            silent
  within_actionable /       still-firing findings confirmed under the new release (no
  within_non_actionable     notification); findings that stop applying superseded (reason
                            ``reclassified``); findings that newly apply (e.g. a
                            mechanism-only change) notify as usual (§6.3)
  ========================  ==========================================================

  Whatever the category, actionable advice is never withdrawn silently: one ReviewNotice per
  (patient, variant, run) links the superseded alerts whenever one of them has severity ≠ info
  (superseded info alerts and notes alone raise no notice). A mechanism change (e.g. the
  condition ClinVar lists for a KCNJ11 variant changed from neonatal diabetes to
  hyperinsulinism) is a change of interpretation even when the class stays the same: it counts
  in the PatientVariant reclassification bookkeeping, so the FHIR export reports ``amended``.

  A key that is absent from the new release although the old one listed it ("removed from
  ClinVar") is not classified (NC); its category follows from the classes (LP → NC is a
  downgrade) and its superseded findings get the more specific reason ``variant_removed``.

  The **first activation** (no current release) is an initial classification, not a
  reclassification: §6.3 defaults for created alerts, no notices, ``summary["initial"] = True``.

* :func:`apply_rule_set` — rule revision (§7.3): imports the rule set, then re-runs every revised
  rule for the carriers of its old and new genes (``strategy="full"``: every patient).
* :func:`full_recompute` — reconciles every patient against the current release and rules.
* :func:`alerts_as_of` / :func:`provenance_as_of` / :func:`alert_snapshot_as_of` — point-in-time
  view (§7.5).
* :func:`release_history` — activated releases in activation order.

**PatientVariant bookkeeping (§7.1).** A variant first classified against a release
(``classification_release_id IS NULL``) gets ``reported_classification = current_classification``
= its class and no reclassification count. Afterwards every class change — and every mechanism
change of a changed key — increments ``reclassified_count`` and sets ``last_reclassified_at`` /
``last_reclassified_run_id`` (FHIR ``amended``). After an activation every PatientVariant points to the new release: unchanged keys
were compared and found identical, so the new release backs their class too (one bulk UPDATE; no
record is re-evaluated for it). :func:`full_recompute` applies the same bookkeeping, so it also
repairs PatientVariants whose stored class had drifted from the current release.

**Re-activation.** Re-activating the current release raises :class:`ValueError`. Activating an
older, previously activated release is a *new* activation: it is compared with the current
release like any other and receives the next ``sequence`` (a release keeps only its latest
sequence; every activation remains visible as a run).

**Failure handling (§7.6).** Invalid input (unknown strategy or trigger, a release that is already
current or not stored, an invalid rule set) raises before any run is recorded. Any other exception
rolls the session back, records a ``failed`` run with the error text in a fresh, committed
transaction and is re-raised; the release stays non-current and no alert changes.

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

from sqlalchemy import Select, func, or_, select, update
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
#: Patients loaded (with diagnosis and medications) per query.
PATIENT_BATCH = 500
#: Changes listed in ``ReevaluationRun.summary["changes"]`` (all are stored as VariantChange rows).
MAX_SUMMARY_CHANGES = 1_000
#: Outcome counters broken down per change category (activation) or per rule code (rule runs).
OUTCOME_COUNTERS: tuple[str, ...] = (
    "alerts_created",
    "notes_created",
    "alerts_superseded",
    "alerts_confirmed",
    "review_notices",
    "notifications",
)

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

# Applies the flag / notification / notice semantics of a run to one patient's reconciliation.
_Semantics = Callable[[Patient, Sequence[ReconcileResult], Sequence[ClassifiedVariant]], None]


# ---------------------------------------------------------------------------
# Run bookkeeping (SPEC §7.4, §7.6)
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

    @classmethod
    def begin(
        cls, trigger: str, strategy: str, old: KbRelease | None, new: KbRelease | None, user: User | None
    ) -> _RunInfo:
        return cls(
            trigger=trigger,
            strategy=strategy,
            old_release_id=old.id if old is not None else None,
            new_release_id=new.id if new is not None else None,
            user_id=user.id if user is not None else None,
            started_at=utcnow(),
            clock=time.perf_counter(),
        )

    def elapsed_ms(self) -> float:
        return (time.perf_counter() - self.clock) * 1000


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
    """Store the counters and summary, mark the run successful and audit ``reevaluation_run``."""
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


def _existing_id(model: type[KbRelease] | type[User], object_id: int | None) -> int | None:
    """``object_id`` if the row still exists (the rollback may have discarded it), else None."""
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
            # A release imported in the rolled-back transaction no longer exists.
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
# Engine: reconciliation, bookkeeping, counters, notices
# ---------------------------------------------------------------------------


class _Engine:
    """Reconciles patients for one run and keeps its counters and per-group outcome breakdown.

    ``group_of`` maps an alert to the group its outcome is counted under in the summary (the
    change category of its variant, or its rule code); None counts it in the totals only.
    """

    def __init__(
        self,
        run: ReevaluationRun,
        release: KbRelease | None,
        *,
        user: User | None,
        group_of: Callable[[Alert], str | None],
    ) -> None:
        self.run = run
        self.release = release
        self.user = user
        self.now = run.started_at
        self.group_of = group_of
        self.rules = alerting.load_current_rules()
        # Keep the current rule versions in the identity map: confirming an alert under a new rule
        # version looks the version up by id, which must not cost a query per alert.
        self._rule_versions = RuleVersion.current_versions()
        self.counts: Counter[str] = Counter()
        self.groups: defaultdict[str, Counter[str]] = defaultdict(Counter)
        self.reasons: Counter[str] = Counter()
        self.carriers_by_key: defaultdict[int, set[int]] = defaultdict(set)
        self.carriers_by_gene: defaultdict[str, set[int]] = defaultdict(set)

    def carrier(self, patient_id: int, variant_key_id: int, gene: str) -> None:
        """Note that ``patient_id`` carries an affected key (changed key or revised rule's gene)."""
        self.carriers_by_key[variant_key_id].add(patient_id)
        self.carriers_by_gene[gene].add(patient_id)

    @property
    def carriers_affected(self) -> int:
        return len(set().union(*self.carriers_by_key.values()))

    def classify(self, variant: PatientVariant, new_class: str, *, reinterpreted: bool = False) -> None:
        """PatientVariant classification bookkeeping under the run's (non-None) release (SPEC §7.1).

        ``reinterpreted``: the variant's mechanism changed, which counts as a reclassification
        even when the class stays the same. A variant whose class and mechanism stay the same is
        not touched (no per-row UPDATE); :func:`_point_variants_to_release` re-points those to
        the release in bulk.
        """
        if variant.classification_release_id is None:
            variant.reported_classification = new_class  # first classification: not a reclassification
        elif variant.current_classification == new_class and not reinterpreted:
            return
        else:
            variant.reclassified_count = (variant.reclassified_count or 0) + 1
            variant.last_reclassified_at = self.now
            variant.last_reclassified_run = self.run
        variant.current_classification = new_class
        variant.classification_release = self.release

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

    def outcome_counts(self, group: str) -> dict[str, int]:
        counts = self.groups.get(group, Counter())
        return {name: counts[name] for name in OUTCOME_COUNTERS}

    def superseded_by_reason(self) -> dict[str, int]:
        return dict(sorted(self.reasons.items()))


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
    """Patients with diagnosis and medications, :data:`PATIENT_BATCH` per query, in id order."""
    for chunk in batched(patient_ids, PATIENT_BATCH):
        statement = (
            select(Patient)
            .where(Patient.id.in_(chunk))
            .options(selectinload(Patient.diagnosis), selectinload(Patient.medications))
            .order_by(Patient.id)
        )
        yield list(db.session.scalars(statement))


def _variants_query() -> Select[tuple[PatientVariant]]:
    return select(PatientVariant).options(joinedload(PatientVariant.variant_key)).order_by(PatientVariant.id)


def _group_by_patient(statements: Iterable[Select[tuple[PatientVariant]]]) -> dict[int, list[PatientVariant]]:
    grouped: defaultdict[int, list[PatientVariant]] = defaultdict(list)
    for statement in statements:
        for variant in db.session.scalars(statement):
            grouped[variant.patient_id].append(variant)
    return grouped


def _variants_of_patients(patient_ids: Sequence[int]) -> dict[int, list[PatientVariant]]:
    return _group_by_patient([_variants_query().where(PatientVariant.patient_id.in_(patient_ids))])


def _variants_with_keys(key_ids: Iterable[int]) -> dict[int, list[PatientVariant]]:
    """All carriers of the given keys, bulk-loaded (one query per :data:`IN_CLAUSE_CHUNK` keys)."""
    return _group_by_patient(
        _variants_query().where(PatientVariant.variant_key_id.in_(chunk))
        for chunk in batched(sorted(key_ids), IN_CLAUSE_CHUNK)
    )


def _reconcile_grouped(
    engine: _Engine,
    patient: Patient,
    classified: Sequence[ClassifiedVariant],
    groups: Mapping[str, set[int]],
    *,
    trigger: str,
    full_scope: bool,
) -> list[ReconcileResult]:
    """Reconcile ``patient`` once per superseded reason (``groups`` partitions its variant keys).

    Findings are per (rule, variant), so reconciling disjoint key sets separately gives the same
    result as one call over their union. With ``full_scope`` and a single reason the patient is
    reconciled once without a key restriction (also when it has no variant at all).
    """
    if full_scope and len(groups) <= 1:
        reason = next(iter(groups), _DEFAULT_REASON)
        return [engine.reconcile(patient, classified, trigger=trigger, reason=reason)]
    return [
        engine.reconcile(patient, classified, trigger=trigger, reason=reason, keys=keys)
        for reason, keys in sorted(groups.items())
    ]


def _reevaluate_all_patients(
    engine: _Engine,
    *,
    trigger: str,
    apply: _Semantics,
    reason_of: Callable[[int], str],
    carrier_gene: Callable[[PatientVariant], str | None],
    sync_classifications: bool,
    reinterpreted: Callable[[int], bool] = lambda _key_id: False,
) -> None:
    """Full strategy: every patient, every variant classified under ``engine.release``.

    ``reason_of`` gives the superseded reason per variant key; ``carrier_gene`` returns the gene
    under which a variant makes its patient an affected carrier (None: not affected);
    ``sync_classifications`` applies the PatientVariant bookkeeping to every variant, counting
    the keys for which ``reinterpreted`` is true as reclassified even when their class is unchanged.
    """
    patient_ids = db.session.scalars(select(Patient.id).order_by(Patient.id)).all()
    for patients in _patient_batches(patient_ids):
        variants_by_patient = _variants_of_patients([patient.id for patient in patients])
        known = classification.classify_keys(
            engine.release,
            {variant.variant_key_id for variants in variants_by_patient.values() for variant in variants},
        )
        for patient in patients:
            classified: list[ClassifiedVariant] = []
            groups: defaultdict[str, set[int]] = defaultdict(set)
            for variant in variants_by_patient.get(patient.id, []):
                key_class = known[variant.variant_key_id]
                if sync_classifications:
                    engine.classify(
                        variant, key_class.classification, reinterpreted=reinterpreted(variant.variant_key_id)
                    )
                gene = carrier_gene(variant)
                if gene is not None:
                    engine.carrier(patient.id, variant.variant_key_id, gene)
                groups[reason_of(variant.variant_key_id)].add(variant.variant_key_id)
                classified.append(_classified(variant, key_class))
            results = _reconcile_grouped(engine, patient, classified, groups, trigger=trigger, full_scope=True)
            apply(patient, results, classified)
            engine.record(results)


# ---------------------------------------------------------------------------
# Release activation (Algorithm 2, SPEC §7.1 / §7.2)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _Change:
    """One element of Δ with its classification under the new release and its display data."""

    key: KeyChange
    known: KnownClass
    label: str
    gene: str

    @property
    def category(self) -> str:
        return self.key.category

    @property
    def removed(self) -> bool:
        """The key is not listed in the new release (e.g. removed from ClinVar), so it is NC."""
        return self.known.known_variant_id is None

    @property
    def mechanism_changed(self) -> bool:
        """The interpretation changed: the mechanism inferred from the listed condition differs."""
        return self.key.old_mechanism != self.key.new_mechanism

    @property
    def reason(self) -> str:
        return _REMOVED_REASON if self.removed else _CATEGORY_REASONS[self.category]


def _stored_key_ids() -> set[int]:
    """``V_obs``: the distinct variant keys of all PatientVariants."""
    return set(db.session.scalars(select(PatientVariant.variant_key_id).distinct()))


def _release_changes(old: KbRelease | None, new: KbRelease, stored: set[int]) -> dict[int, _Change]:
    """Δ restricted to stored keys, with each key's new classification, label and gene (bulk queries)."""
    delta = clinvar.diff_releases(old, new, stored)
    key_ids = [change.variant_key_id for change in delta]
    known = classification.classify_keys(new, key_ids)
    keys: dict[int, VariantKey] = {}
    for chunk in batched(key_ids, IN_CLAUSE_CHUNK):
        keys.update((key.id, key) for key in db.session.scalars(select(VariantKey).where(VariantKey.id.in_(chunk))))
    return {
        change.variant_key_id: _Change(
            key=change,
            known=known[change.variant_key_id],
            label=keys[change.variant_key_id].label,
            gene=change.gene or keys[change.variant_key_id].gene or "",
        )
        for change in delta
    }


def _rule_codes_text(alerts: Iterable[Alert]) -> str:
    return ", ".join(sorted({alert.rule_code for alert in alerts}, key=rule_code_sort_key))


def _release_notice_message(change: _Change, alerts: Sequence[Alert], release: KbRelease) -> str:
    """What changed for the variant in ``release`` and which alerts were superseded."""
    old, new = change.key.old_class, change.key.new_class
    subject = f"{change.gene} variant {change.label}"
    if change.removed:
        what = f"{subject} ({old}) is no longer listed in {release.label} and is now not classified (NC)"
    elif old != new:
        verb = "downgraded" if change.category == "downgrade" else "reclassified"
        what = f"{subject} was {verb} from {old} to {new} in {release.label}"
    else:
        what = f"{subject} ({new}) was reinterpreted in {release.label}"
    if change.mechanism_changed and not change.removed:
        what += (
            f" (mechanism {C.MECHANISM_LABELS[change.key.old_mechanism].lower()} → "
            f"{C.MECHANISM_LABELS[change.key.new_mechanism].lower()}: the condition listed for it changed)"
        )
    return f"{what}; alert(s) {_rule_codes_text(alerts)} superseded. {_REVIEW_ADVICE}"


def _withdrawn_alerts(results: Sequence[ReconcileResult]) -> defaultdict[int, list[Alert]]:
    """Superseded alerts (not notes) per variant key, except those replaced by a revised recommendation."""
    withdrawn: defaultdict[int, list[Alert]] = defaultdict(list)
    for result in results:
        revised = {id(old) for old, _new in result.revised}
        for alert in result.superseded:
            if alert.kind == "alert" and id(alert) not in revised:
                withdrawn[alert.variant_key_id].append(alert)
    return withdrawn


def _created_by_key(results: Sequence[ReconcileResult]) -> defaultdict[int, list[Alert]]:
    created: defaultdict[int, list[Alert]] = defaultdict(list)
    for result in results:
        for alert in result.created:
            created[alert.variant_key_id].append(alert)
    return created


def _release_semantics(engine: _Engine, changes: Mapping[int, _Change], *, initial: bool) -> _Semantics:
    """Flags, notifications and review notices of a release activation (change-category semantics).

    One review notice per (patient, variant) whose superseded alerts include a non-info one,
    whatever the change category: a release never withdraws actionable advice silently.
    """

    def apply(patient: Patient, results: Sequence[ReconcileResult], classified: Sequence[ClassifiedVariant]) -> None:
        if initial:
            return  # initial classification: §6.3 defaults, nothing is a reclassification
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
        variants = {variant.variant_key_id: variant for variant in classified}
        created = _created_by_key(results)
        for key_id, alerts in sorted(_withdrawn_alerts(results).items()):
            if all(alert.severity == "info" for alert in alerts):
                continue
            change = changes.get(key_id)
            if change is not None:
                engine.notice(
                    patient,
                    key_id,
                    alerts,
                    old=change.key.old_class,
                    new=change.key.new_class,
                    message=_release_notice_message(change, alerts, engine.release),
                )
            else:
                # Full strategy only: an unchanged key whose stored state had drifted from the
                # knowledge base (e.g. a therapy change recorded without re-evaluation).
                variant = variants.get(key_id)
                engine.notice(
                    patient,
                    key_id,
                    alerts,
                    old=alerts[0].classification,
                    new=variant.classification if variant is not None else alerts[0].classification,
                    message=_withdrawal_message(
                        f"Re-evaluation under {engine.release.label}", alerts, created.get(key_id, []), variant
                    ),
                )

    return apply


def _reevaluate_changed_carriers(engine: _Engine, changes: Mapping[int, _Change], apply: _Semantics) -> None:
    """Change-driven: only the carriers of changed keys, restricted to those keys."""
    carriers = _variants_with_keys(changes)
    for patients in _patient_batches(sorted(carriers)):
        for patient in patients:
            classified: list[ClassifiedVariant] = []
            groups: defaultdict[str, set[int]] = defaultdict(set)
            for variant in carriers[patient.id]:
                change = changes[variant.variant_key_id]
                engine.classify(variant, change.key.new_class, reinterpreted=change.mechanism_changed)
                engine.carrier(patient.id, variant.variant_key_id, change.gene)
                classified.append(_classified(variant, change.known))
                groups[change.reason].add(variant.variant_key_id)
            results = _reconcile_grouped(
                engine, patient, classified, groups, trigger="clinvar_release", full_scope=False
            )
            apply(patient, results, classified)
            engine.record(results)


def _point_variants_to_release(engine: _Engine, release: KbRelease) -> None:
    """Complete the PatientVariant bookkeeping after reconciliation.

    Variants never classified before get their first classification under ``release``; then every
    variant points to ``release`` (one bulk UPDATE): the class of an unchanged key is backed by
    that release as well.
    """
    pending = list(db.session.scalars(select(PatientVariant).where(PatientVariant.classification_release_id.is_(None))))
    known = classification.classify_keys(release, {variant.variant_key_id for variant in pending})
    for variant in pending:
        engine.classify(variant, known[variant.variant_key_id].classification)
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
            carrier_count=len(engine.carriers_by_key[key_id]),
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


def _carriers(engine: _Engine, key_ids: Iterable[int]) -> int:
    """Distinct patients carrying any of ``key_ids``."""
    return len(set().union(*(engine.carriers_by_key[key_id] for key_id in key_ids)))


def _release_summary(
    engine: _Engine, changes: Mapping[int, _Change], old: KbRelease | None, new: KbRelease, *, initial: bool
) -> dict[str, Any]:
    """Per-category and per-gene counts, superseded reasons and the list of changes."""
    keys_by_category: defaultdict[str, list[int]] = defaultdict(list)
    keys_by_gene: defaultdict[str, list[int]] = defaultdict(list)
    for key_id, change in changes.items():
        keys_by_category[change.category].append(key_id)
        keys_by_gene[change.gene or "unknown"].append(key_id)
    categories = {
        category: {
            "changed_variants": len(keys_by_category[category]),
            "carriers": _carriers(engine, keys_by_category[category]),
            **engine.outcome_counts(category),
        }
        for category in C.CHANGE_CATEGORIES
    }
    genes = {}
    for gene in sorted(keys_by_gene, key=_gene_order):
        per_category = Counter(changes[key_id].category for key_id in keys_by_gene[gene])
        genes[gene] = {
            "changed_variants": len(keys_by_gene[gene]),
            "carriers": _carriers(engine, keys_by_gene[gene]),
            "categories": {category: per_category[category] for category in C.CHANGE_CATEGORIES},
        }
    listed = list(changes.items())[:MAX_SUMMARY_CHANGES]
    return {
        "initial": initial,
        "old_release": old.label if old is not None else None,
        "new_release": new.label,
        "categories": categories,
        "genes": genes,
        "superseded_by_reason": engine.superseded_by_reason(),
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
                "carriers": len(engine.carriers_by_key[key_id]),
            }
            for key_id, change in listed
        ],
    }


def activate_release(
    new_release: KbRelease, *, strategy: str = "change_driven", user: User | None = None
) -> ReevaluationRun:
    """Make ``new_release`` current and re-evaluate the affected carriers (Algorithm 2, SPEC §7.1/§7.2).

    ``strategy="change_driven"`` examines only stored keys and only the carriers of changed keys
    (``records_examined`` = carrier × changed-key records); ``"full"`` reconciles every patient
    (``records_examined`` = all patient-variant records). Both record one VariantChange per
    changed stored key and apply the category semantics described in the module docstring.

    ``summary``: ``initial``, ``old_release`` / ``new_release`` labels, ``categories`` (per change
    category: ``changed_variants``, distinct ``carriers`` and the outcome counters of
    :data:`OUTCOME_COUNTERS`), ``genes`` (per gene: ``changed_variants``, ``carriers`` and the
    number of changes per category), ``superseded_by_reason``, ``changes_total``,
    ``changes_truncated`` and ``changes`` (the first :data:`MAX_SUMMARY_CHANGES` changes).

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
    info = _RunInfo.begin("clinvar_release", strategy, old_release, new_release, user)
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

            def reason_of(key_id: int) -> str:
                change = changes.get(key_id)
                return change.reason if change is not None else _DEFAULT_REASON

            def carrier_gene(variant: PatientVariant) -> str | None:
                change = changes.get(variant.variant_key_id)
                return change.gene if change is not None else None

            def reinterpreted(key_id: int) -> bool:
                change = changes.get(key_id)
                return change is not None and change.mechanism_changed

            _reevaluate_all_patients(
                engine,
                trigger="clinvar_release",
                apply=semantics,
                reason_of=reason_of,
                carrier_gene=carrier_gene,
                sync_classifications=True,
                reinterpreted=reinterpreted,
            )
        _point_variants_to_release(engine, new_release)
        engine.counts["carriers_affected"] = engine.carriers_affected
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
        """``"D4 v1 → v2"`` — used for withdrawn alerts, which only a code with an earlier version has."""
        return f"{self.code} v{self.old_version} → v{self.new_version}"


def _revisions(changes: Sequence[Mapping[str, Any]]) -> dict[str, _Revision]:
    revisions: dict[str, _Revision] = {}
    for change in changes:
        genes = set(db.session.get(RuleVersion, change["new_rule_version_id"]).genes)
        if change["old_rule_version_id"] is not None:
            genes.update(db.session.get(RuleVersion, change["old_rule_version_id"]).genes)
        revisions[change["code"]] = _Revision(
            change["code"], change["old_version"], change["new_version"], frozenset(genes)
        )
    return revisions


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


def _withdrawal_semantics(engine: _Engine, cause: Callable[[Sequence[Alert]], str]) -> _Semantics:
    """One review notice per (patient, variant) whose superseded alerts include a non-info one.

    Used by rule revisions and full recomputations: actionable advice is never withdrawn silently.
    Created alerts keep the §6.3 defaults (a newly firing alert notifies). An alert replaced
    because its rule revised the recommendation (same severity) raises no notice: its
    replacement notifies.
    """

    def apply(patient: Patient, results: Sequence[ReconcileResult], classified: Sequence[ClassifiedVariant]) -> None:
        variants = {variant.variant_key_id: variant for variant in classified}
        created = _created_by_key(results)
        for key_id, alerts in sorted(_withdrawn_alerts(results).items()):
            if all(alert.severity == "info" for alert in alerts):
                continue
            variant = variants.get(key_id)
            engine.notice(
                patient,
                key_id,
                alerts,
                old=alerts[0].classification,
                new=variant.classification if variant is not None else alerts[0].classification,
                message=_withdrawal_message(cause(alerts), alerts, created.get(key_id, []), variant),
            )

    return apply


def _reevaluate_rule_carriers(engine: _Engine, revisions: Mapping[str, _Revision], apply: _Semantics) -> None:
    """Change-driven rule revision: carriers of the revised rules' genes, only those rules."""
    genes = frozenset().union(*(revision.genes for revision in revisions.values()))
    carriers = _group_by_patient([_variants_query().where(PatientVariant.gene.in_(sorted(genes)))])
    known = classification.classify_keys(
        engine.release, {variant.variant_key_id for variants in carriers.values() for variant in variants}
    )
    for patients in _patient_batches(sorted(carriers)):
        for patient in patients:
            classified: list[ClassifiedVariant] = []
            for variant in carriers[patient.id]:
                engine.carrier(patient.id, variant.variant_key_id, variant.gene)
                classified.append(_classified(variant, known[variant.variant_key_id]))
            carried = {variant.gene for variant in classified}
            codes = {code for code, revision in revisions.items() if revision.genes & carried}
            result = engine.reconcile(
                patient, classified, trigger="rule_revision", reason=_RULE_REASON, rule_codes=codes
            )
            apply(patient, [result], classified)
            engine.record([result])


def apply_rule_set(
    doc: dict[str, Any], *, user: User | None = None, strategy: str = "change_driven"
) -> ReevaluationRun:
    """Import rule versions and re-evaluate the carriers of every revised rule (SPEC §7.3).

    :func:`~ge_ehr.services.alerting.import_rule_versions` validates and stores the document (an
    existing (code, version) must be identical); then, for every code whose current version
    changed, the carriers of its old and new genes are reconciled for that rule under the current
    release (``strategy="full"``: every patient, every rule). Still firing with the same severity →
    provenance update (``confirmed_rule_version_id``) without notification; newly firing → new
    alert that notifies (RS5); stopping or changing severity → superseded (reason
    ``rule_revised``) with a review notice when a non-info alert was withdrawn, plus the new
    finding if any. A rule set identical to the current one gives a no-op run.

    ``summary``: ``rule_set``, ``label``, ``release``, ``rule_changes`` (code, versions, genes),
    ``rules`` (outcome counters per rule code), ``genes`` (distinct ``carriers`` per affected
    gene) and ``superseded_by_reason``.

    Raises :class:`~ge_ehr.services.rules.RuleDefinitionError` (no run recorded) for an invalid
    rule set and :class:`ValueError` for an unknown strategy; other failures are recorded as a
    failed run and re-raised.
    """
    _check_strategy(strategy)
    release = KbRelease.current()
    info = _RunInfo.begin("rule_revision", strategy, release, release, user)
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
        if revisions:

            def cause(alerts: Sequence[Alert]) -> str:
                codes = sorted({alert.rule_code for alert in alerts} & revisions.keys(), key=rule_code_sort_key)
                labels = ", ".join(revisions[code].label for code in codes)
                return f"Rule revision {labels}" if labels else "Rule revision"

            semantics = _withdrawal_semantics(engine, cause)
            if strategy == "change_driven":
                _reevaluate_rule_carriers(engine, revisions, semantics)
            else:
                _reevaluate_all_patients(
                    engine,
                    trigger="rule_revision",
                    apply=semantics,
                    reason_of=lambda _key_id: _RULE_REASON,
                    carrier_gene=lambda variant: variant.gene if variant.gene in genes else None,
                    sync_classifications=False,
                )
        engine.counts["carriers_affected"] = engine.carriers_affected
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
            "genes": {
                gene: {"carriers": len(engine.carriers_by_gene[gene])} for gene in sorted(genes, key=_gene_order)
            },
            "superseded_by_reason": engine.superseded_by_reason(),
        }
        _finish_run(run, engine, info, summary)
    return run


def full_recompute(*, user: User | None = None, trigger: str = "full_recompute") -> ReevaluationRun:
    """Reconcile every patient (full scope) against the current release and rules.

    In a consistent database this changes no alert (every event is handled when it happens) and
    only refreshes provenance; it repairs drift, e.g. after rule versions were loaded without
    re-evaluation. PatientVariant classifications are brought in line with the current release
    (module docstring). Superseded findings get reason ``reclassified`` and a withdrawn non-info
    alert opens a review notice. ``summary``: ``release``, ``rules`` (outcome counters per rule
    code) and ``superseded_by_reason``.

    Raises :class:`ValueError` for an unknown trigger; other failures are recorded as a failed run
    and re-raised.
    """
    if trigger not in C.TRIGGERS:
        raise ValueError(f"Unknown trigger {trigger!r}; expected one of {', '.join(C.TRIGGERS)}.")
    release = KbRelease.current()
    info = _RunInfo.begin(trigger, "full", release, release, user)
    with _failure_recorded(info):
        run = _start_run(info, user)
        engine = _Engine(run, release, user=user, group_of=lambda alert: alert.rule_code)
        cause = f"Full re-evaluation under {release.label if release is not None else 'no knowledge-base release'}"
        _reevaluate_all_patients(
            engine,
            trigger=trigger,
            apply=_withdrawal_semantics(engine, lambda _alerts: f"{cause} and the current rules"),
            reason_of=lambda _key_id: _DEFAULT_REASON,
            carrier_gene=lambda _variant: None,
            sync_classifications=release is not None,
        )
        if release is not None:
            _point_variants_to_release(engine, release)
        summary = {
            "release": release.label if release is not None else None,
            "rules": {code: engine.outcome_counts(code) for code in sorted(engine.groups, key=rule_code_sort_key)},
            "superseded_by_reason": engine.superseded_by_reason(),
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


@dataclass(frozen=True)
class AlertSnapshot:
    """An alert or note as it stood at a point in time: its text and the evidence behind it."""

    alert: Alert
    event: AlertEvent  # the created/confirmed event in force at that time
    title: str
    message: str
    source: str | None
    severity: str | None
    release: KbRelease | None
    rule_version: RuleVersion | None
    classification: str


def alert_snapshot_as_of(alert: Alert, at: datetime) -> AlertSnapshot | None:
    """``alert`` as shown at time ``at`` (naive UTC or aware): text, rule version, release, class.

    Built from :func:`provenance_as_of`: the title, message (recommendation or note text), source
    and severity snapshotted on that event, with its release, rule version and classification —
    so a later rule revision or therapy change that rewrote a note's text does not alter what the
    point-in-time view shows. Events recorded without a text snapshot fall back to the alert's
    stored text. None when the alert did not exist yet at ``at``.
    """
    event = provenance_as_of(alert, at)
    if event is None:
        return None
    snapshotted = event.message is not None
    return AlertSnapshot(
        alert=alert,
        event=event,
        title=event.title if snapshotted and event.title is not None else alert.title,
        message=event.message if snapshotted else alert.message,
        source=event.source if snapshotted else alert.source,
        severity=event.severity if snapshotted else alert.severity,
        release=event.release,
        rule_version=event.rule_version,
        classification=event.classification or alert.classification,
    )


def release_history() -> list[KbRelease]:
    """Activated releases ordered by activation ``sequence``.

    A re-activated release appears once, at its latest activation; the runs record every activation.
    """
    statement = select(KbRelease).where(KbRelease.sequence.is_not(None)).order_by(KbRelease.sequence)
    return list(db.session.scalars(statement))
