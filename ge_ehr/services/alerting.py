"""F3 — reconciliation of findings into alerts/notes, and rule-version import (SPEC §6.3).

:func:`reconcile_patient` is the only code that creates, confirms or supersedes
:class:`~ge_ehr.models.Alert` rows. It evaluates the patient with the pure Algorithm 1
(:func:`ge_ehr.services.rules.evaluate`) and makes the patient's *current* alerts and notes match
the desired findings, matching on ``Finding.key`` = (rule code, variant key, kind, severity):

* desired but not current → new alert/note with full provenance (AlertEvent ``created``);
* current but no longer desired → ``superseded`` (AlertEvent ``superseded``) — never deleted;
* in both → provenance updated (AlertEvent ``confirmed``) when the release, the rule version or
  the finding's text changed, otherwise unchanged. A reviewed alert stays reviewed. Exception: an
  **alert** whose recommendation text changed (a rule revision rewrote it) is an outcome change —
  it is superseded (reason ``rule_revised``) and replaced by a new alert that notifies, so a
  clinician's review never silently carries over to advice it did not see. Notes follow the
  patient's therapy/diagnosis, so their text is updated in place.

``created`` and ``confirmed`` events snapshot the finding's title, message, source and severity,
so the text shown at any earlier time stays reproducible (SPEC §7.5).

The invariant "at most one current alert per (patient, rule code, variant key, kind)" holds
because evaluation yields one finding per (rule code, variant) and replaced rows are superseded
(and flushed) before their replacements are inserted.

Services flush but never commit; the caller commits. Owned by the Rules work package.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from operator import attrgetter
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import selectinload

from .. import constants as C
from ..extensions import db
from ..models import Alert, AlertEvent, KbRelease, Patient, ReevaluationRun, Rule, RuleVersion, User, utcnow
from ..security import audit
from . import classification
from .rules import (
    ClassifiedVariant,
    ClinicalContext,
    Finding,
    RuleDefinitionError,
    RuleSpec,
    evaluate,
    rule_code_sort_key,
    validate_rule_set,
)

logger = logging.getLogger(__name__)


@dataclass
class ReconcileResult:
    """Outcome of :func:`reconcile_patient`."""

    created: list[Alert] = field(default_factory=list)
    superseded: list[Alert] = field(default_factory=list)
    confirmed: list[Alert] = field(default_factory=list)
    unchanged: list[Alert] = field(default_factory=list)
    records_examined: int = 0
    #: (old, new) pairs: alerts superseded because their rule's recommendation was revised and
    #: their replacements. Each old alert is also in ``superseded``, each new one in ``created``.
    revised: list[tuple[Alert, Alert]] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Inputs of the evaluation
# ---------------------------------------------------------------------------


def load_current_rules() -> list[RuleSpec]:
    """The current rule versions (``RuleVersion.is_current``) as executable specs, in code order."""
    versions = sorted(RuleVersion.current_versions(), key=lambda version: rule_code_sort_key(version.code))
    return [RuleSpec.from_definition(version.definition, rule_version_id=version.id) for version in versions]


def clinical_context(patient: Patient) -> ClinicalContext:
    """Current drug classes (active medications) and diagnosis data of ``patient``."""
    diagnosis = patient.diagnosis
    return ClinicalContext(
        drug_classes=patient.current_drug_classes,
        age_at_diagnosis_months=diagnosis.age_at_diagnosis_months if diagnosis is not None else None,
        diabetes_type=diagnosis.diabetes_type if diagnosis is not None else None,
    )


# ---------------------------------------------------------------------------
# Reconciliation
# ---------------------------------------------------------------------------


def _alert_key(alert: Alert) -> tuple[str, int, str, str | None]:
    """The :attr:`Finding.key` an existing alert corresponds to."""
    return (alert.rule_code, alert.variant_key_id, alert.kind, alert.severity)


def _release_text(release: KbRelease | None) -> str:
    return f"release {release.label}" if release is not None else "no knowledge-base release"


def _current_alerts(
    patient: Patient, variant_key_ids: frozenset[int] | None, rule_codes: frozenset[str] | None
) -> list[Alert]:
    """The patient's current (active/reviewed) alerts and notes within the scope — one query."""
    statement = select(Alert).where(Alert.patient_id == patient.id, Alert.status.in_(C.CURRENT_ALERT_STATUSES))
    if variant_key_ids is not None:
        statement = statement.where(Alert.variant_key_id.in_(variant_key_ids))
    if rule_codes is not None:
        statement = statement.where(Alert.rule_code.in_(rule_codes))
    return list(db.session.scalars(statement.order_by(Alert.id)))


def current_alerts_by_patient(patient_ids: Iterable[int]) -> dict[int, list[Alert]]:
    """The current alerts and notes of several patients in one query, per patient in id order.

    For batch callers of :func:`reconcile_patient` (``current_alerts=``); at most a few hundred
    patient ids per call.
    """
    grouped: dict[int, list[Alert]] = {patient_id: [] for patient_id in patient_ids}
    if grouped:
        statement = (
            select(Alert)
            .where(Alert.patient_id.in_(list(grouped)), Alert.status.in_(C.CURRENT_ALERT_STATUSES))
            .order_by(Alert.id)
        )
        for alert in db.session.scalars(statement):
            grouped[alert.patient_id].append(alert)
    return grouped


def _in_scope(
    alerts: Iterable[Alert], variant_key_ids: frozenset[int] | None, rule_codes: frozenset[str] | None
) -> list[Alert]:
    """The still-current ``alerts`` within the scope (a preloaded list may hold alerts superseded since)."""
    return [
        alert
        for alert in alerts
        if alert.is_current
        and (variant_key_ids is None or alert.variant_key_id in variant_key_ids)
        and (rule_codes is None or alert.rule_code in rule_codes)
    ]


def _index_key(alert_or_finding: Alert | Finding) -> tuple[str, int, str]:
    """The columns of the partial unique index on current alerts (rule code, variant key, kind)."""
    return (alert_or_finding.rule_code, alert_or_finding.variant_key_id, alert_or_finding.kind)


class _Reconciler:
    """State shared by the create / confirm / supersede steps of one :func:`reconcile_patient` call."""

    def __init__(
        self,
        patient: Patient,
        release: KbRelease | None,
        rules: Mapping[str, RuleSpec],
        *,
        trigger: str,
        run: ReevaluationRun | None,
        user: User | None,
        now: datetime,
    ) -> None:
        self.patient = patient
        self.release = release
        self.release_id = release.id if release is not None else None
        self.rules = rules
        self.trigger = trigger
        self.run = run
        self.user = user
        self.now = now

    def _event(
        self,
        alert: Alert,
        event: str,
        *,
        rule_version_id: int | None,
        variant_class: str | None,
        detail: str,
        finding: Finding | None = None,
    ) -> None:
        """Add a provenance event; ``finding`` (created/confirmed events) is snapshotted with it."""
        db.session.add(
            AlertEvent(
                alert=alert,
                event=event,
                release=self.release,
                rule_version_id=rule_version_id,
                classification=variant_class,
                run=self.run,
                user=self.user,
                at=self.now,
                detail=detail,
                title=finding.title if finding is not None else None,
                message=finding.message if finding is not None else None,
                source=finding.source if finding is not None else None,
                severity=finding.severity if finding is not None else None,
            )
        )

    def create(self, finding: Finding, *, replaces: Alert | None = None) -> Alert:
        """Insert the alert or note for a newly desired finding (``replaces``: the alert it revises)."""
        rule = self.rules[finding.rule_code]
        alert = Alert(
            patient=self.patient,
            patient_variant_id=finding.patient_variant_id,
            variant_key_id=finding.variant_key_id,
            rule_code=finding.rule_code,
            created_rule_version_id=finding.rule_version_id,
            confirmed_rule_version_id=finding.rule_version_id,
            kind=finding.kind,
            severity=finding.severity,
            title=finding.title,
            message=finding.message,
            source=finding.source,
            classification=finding.classification,
            status="active",
            trigger=self.trigger,
            is_reclassified=False,
            priority="normal",
            notify=finding.kind == "alert" and finding.severity != "info",
            created_release=self.release,
            confirmed_release=self.release,
            created_run=self.run,
            created_at=self.now,
        )
        db.session.add(alert)
        detail = (
            f"{'Alert' if finding.kind == 'alert' else 'Note'} created by {rule.label} under "
            f"{_release_text(self.release)} (trigger: {C.TRIGGER_LABELS[self.trigger]})."
        )
        if replaces is not None:
            detail += f" Replaces alert #{replaces.id}, whose recommendation {rule.label} revised."
        self._event(
            alert,
            "created",
            rule_version_id=finding.rule_version_id,
            variant_class=finding.classification,
            detail=detail,
            finding=finding,
        )
        return alert

    def needs_replacement(self, alert: Alert, finding: Finding) -> bool:
        """A still-firing alert whose recommendation text changed (a rule revision rewrote it)."""
        return alert.kind == "alert" and alert.message != finding.message

    def confirm(self, alert: Alert, finding: Finding) -> bool:
        """Bring a still-desired alert's provenance (and text) up to date; False when nothing changed."""
        provenance_changed = (
            alert.confirmed_release_id != self.release_id or alert.confirmed_rule_version_id != finding.rule_version_id
        )
        message_changed = alert.message != finding.message
        text_changed = message_changed or (alert.title, alert.source) != (finding.title, finding.source)
        if not (provenance_changed or text_changed):
            return False
        detail = f"Confirmed by {self.rules[finding.rule_code].label} under {_release_text(self.release)}."
        if message_changed:
            # Notes explain the patient's current therapy/diagnosis, so their text follows it; the
            # previous text is kept in the event for the audit trail.
            detail += f" Previous text: {alert.message}"
        alert.confirmed_release = self.release
        if alert.confirmed_rule_version_id != finding.rule_version_id:
            # Assign the object (not just the id) so an already-loaded relationship stays in sync.
            alert.confirmed_rule_version = db.session.get(RuleVersion, finding.rule_version_id)
        alert.title, alert.message, alert.source = finding.title, finding.message, finding.source
        self._event(
            alert,
            "confirmed",
            rule_version_id=finding.rule_version_id,
            variant_class=finding.classification,
            detail=detail,
            finding=finding,
        )
        return True

    def supersede(self, alert: Alert, reason: str, variant_class: str | None, *, detail: str | None = None) -> None:
        """Mark an alert that is no longer desired as superseded (it is never deleted)."""
        rule = self.rules.get(alert.rule_code)
        alert.status = "superseded"
        alert.superseded_at = self.now
        alert.superseded_reason = reason
        alert.superseded_run = self.run
        self._event(
            alert,
            "superseded",
            rule_version_id=rule.rule_version_id if rule is not None else alert.confirmed_rule_version_id,
            variant_class=variant_class,
            detail=detail
            or (
                f"{C.SUPERSEDED_REASON_LABELS[reason]}: no longer produced under {_release_text(self.release)} "
                f"(trigger: {C.TRIGGER_LABELS[self.trigger]})."
            ),
        )


def reconcile_patient(
    patient: Patient,
    release: KbRelease | None,
    rules: Iterable[RuleSpec],
    *,
    trigger: str,
    variant_key_ids: Iterable[int] | None = None,
    rule_codes: Iterable[str] | None = None,
    run: ReevaluationRun | None = None,
    superseded_reason: str = "therapy_changed",
    user: User | None = None,
    now: datetime | None = None,
    classified: Iterable[ClassifiedVariant] | None = None,
    current_alerts: list[Alert] | None = None,
    flush: bool = True,
) -> ReconcileResult:
    """Create, supersede or confirm the patient's alerts/notes so they match the desired findings.

    ``variant_key_ids`` / ``rule_codes`` restrict the scope: only those variants and rules are
    evaluated, and only current alerts in the same scope are compared (others are left alone).
    ``classified`` supplies the patient's classified variants; when None they are looked up
    under ``release`` with :func:`~ge_ehr.services.classification.classified_variants_for_patient`.
    ``current_alerts`` supplies the patient's current alerts preloaded by a batch caller (e.g.
    with :func:`current_alerts_by_patient`; any scope — they are filtered here) instead of one
    query; created alerts are appended to it, so the list stays valid for further calls.
    ``flush=False`` leaves the final flush to the caller. Either way a superseded row is flushed
    before a replacement sharing its unique-index key is inserted.
    Created alerts get ``notify = (kind == "alert" and severity != "info")``, priority
    ``normal`` and ``is_reclassified=False`` (the re-evaluation engine adjusts these per change
    category). A current alert whose recommendation text differs from its finding's is
    superseded with reason ``rule_revised`` and replaced by a new alert (listed in
    ``result.revised`` as well as in ``superseded`` / ``created``). ``records_examined`` is the
    number of classified variants evaluated.

    Every rule in scope must be stored (``rule_version_id`` set). Raises :class:`ValueError` for
    an unknown ``trigger`` / ``superseded_reason`` or an unstored rule.
    """
    if trigger not in C.TRIGGERS:
        raise ValueError(f"Unknown trigger {trigger!r}; expected one of {', '.join(C.TRIGGERS)}.")
    if superseded_reason not in C.SUPERSEDED_REASONS:
        raise ValueError(
            f"Unknown superseded reason {superseded_reason!r}; expected one of {', '.join(C.SUPERSEDED_REASONS)}."
        )
    key_scope = frozenset(variant_key_ids) if variant_key_ids is not None else None
    code_scope = frozenset(rule_codes) if rule_codes is not None else None
    scoped_rules = [rule for rule in rules if code_scope is None or rule.code in code_scope]
    unstored = [rule.label for rule in scoped_rules if rule.rule_version_id is None]
    if unstored:
        raise ValueError(
            f"Rule(s) {', '.join(unstored)} have no stored rule version; import the rule set before reconciling alerts."
        )

    if classified is None:
        variants = classification.classified_variants_for_patient(patient, release, key_scope)
    else:
        variants = list(classified)
    if key_scope is not None:
        variants = [variant for variant in variants if variant.variant_key_id in key_scope]
    findings = evaluate(variants, clinical_context(patient), scoped_rules)

    reconciler = _Reconciler(
        patient,
        release,
        {rule.code: rule for rule in scoped_rules},
        trigger=trigger,
        run=run,
        user=user,
        now=now or utcnow(),
    )
    result = ReconcileResult(records_examined=len(variants))
    if current_alerts is None:
        in_scope = _current_alerts(patient, key_scope, code_scope)
    else:
        in_scope = _in_scope(current_alerts, key_scope, code_scope)
    current = {_alert_key(alert): alert for alert in in_scope}
    to_create: list[Finding] = []
    to_replace: list[tuple[Alert, Finding]] = []
    for finding in findings:
        alert = current.pop(finding.key, None)
        if alert is None:
            to_create.append(finding)
        elif reconciler.needs_replacement(alert, finding):
            to_replace.append((alert, finding))
        elif reconciler.confirm(alert, finding):
            result.confirmed.append(alert)
        else:
            result.unchanged.append(alert)

    classification_by_key = {variant.variant_key_id: variant.classification for variant in variants}
    for alert in current.values():
        reconciler.supersede(alert, superseded_reason, classification_by_key.get(alert.variant_key_id))
        result.superseded.append(alert)
    for alert, finding in to_replace:
        rule = reconciler.rules[finding.rule_code]
        reconciler.supersede(
            alert,
            "rule_revised",
            finding.classification,
            detail=f"{C.SUPERSEDED_REASON_LABELS['rule_revised']}: {rule.label} revised the recommendation; "
            "replaced by a new alert.",
        )
        result.superseded.append(alert)
    superseded_keys = {_index_key(alert) for alert in result.superseded}
    if to_replace or any(_index_key(finding) in superseded_keys for finding in to_create):
        # A replacement (same rule, variant and kind; new severity or text) shares the partial
        # unique index key of a row superseded above, so that row must reach the database first.
        db.session.flush()
    result.created.extend(reconciler.create(finding) for finding in to_create)
    for alert, finding in to_replace:
        replacement = reconciler.create(finding, replaces=alert)
        result.created.append(replacement)
        result.revised.append((alert, replacement))
    if current_alerts is not None:
        current_alerts.extend(result.created)
    if flush:
        db.session.flush()

    logger.debug(
        "Reconciled patient %s (%s): %d created, %d superseded (%d revised), %d confirmed, %d unchanged, "
        "%d records examined",
        patient.id,
        trigger,
        len(result.created),
        len(result.superseded),
        len(result.revised),
        len(result.confirmed),
        len(result.unchanged),
        result.records_examined,
    )
    return result


def evaluate_patient_now(
    patient: Patient, *, trigger: str, user: User | None = None, superseded_reason: str = "therapy_changed"
) -> ReconcileResult:
    """Reconcile one patient against the current release and rules (full scope).

    Used after a VCF upload, a medication change or a diagnosis change (SPEC §6.4).
    """
    return reconcile_patient(
        patient,
        KbRelease.current(),
        load_current_rules(),
        trigger=trigger,
        user=user,
        superseded_reason=superseded_reason,
    )


# ---------------------------------------------------------------------------
# Rule-version import
# ---------------------------------------------------------------------------


def _canonical_json(value: Any) -> str:
    """Key-order independent JSON text, used to compare rule definitions."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def import_rule_versions(doc: Mapping[str, Any], *, user: User | None = None) -> list[dict[str, Any]]:
    """Store unseen rule versions and make the highest version per code current.

    Validates ``doc`` (:func:`~ge_ehr.services.rules.validate_rule_set`), creates a
    :class:`~ge_ehr.models.Rule` for every new code (title from the definition; it follows the
    current version's title afterwards) and a :class:`~ge_ehr.models.RuleVersion` for every
    unseen (code, version), storing the definition verbatim. Versions already stored with an
    identical definition are skipped, so re-importing a rule set is a no-op. Audits
    ``rules_imported``. No re-evaluation happens here
    (see :func:`ge_ehr.services.reevaluation.apply_rule_set`).

    Returns ``{"code", "old_version", "new_version", "old_rule_version_id",
    "new_rule_version_id"}`` for every code whose current version changed (``old_*`` None for
    a brand-new rule), in code order. Raises
    :class:`~ge_ehr.services.rules.RuleDefinitionError` for an invalid document or an existing
    (code, version) whose definition differs; nothing is added to the session in that case.
    """
    definitions = validate_rule_set(doc)
    codes = sorted({definition["code"] for definition in definitions}, key=rule_code_sort_key)
    statement = select(Rule).where(Rule.code.in_(codes)).options(selectinload(Rule.versions))
    rules_by_code = {rule.code: rule for rule in db.session.scalars(statement)}

    new_definitions: list[dict[str, Any]] = []
    for definition in definitions:
        code, number = definition["code"], definition["version"]
        rule = rules_by_code.get(code)
        stored = next((version for version in rule.versions if version.version == number), None) if rule else None
        if stored is None:
            new_definitions.append(definition)
        elif _canonical_json(stored.definition) != _canonical_json(definition):
            next_version = max(version.version for version in rule.versions) + 1
            raise RuleDefinitionError(
                f"Rule {code} v{number} is already stored with a different definition. Rule versions are "
                f"immutable: publish the change as a new version (e.g. {code} v{next_version})."
            )

    label = doc.get("label")
    created: list[RuleVersion] = []
    for definition in new_definitions:
        code = definition["code"]
        rule = rules_by_code.get(code)
        if rule is None:
            rule = Rule(code=code, title=definition["title"])
            db.session.add(rule)
            rules_by_code[code] = rule
        version = RuleVersion(
            rule=rule,
            code=code,
            version=definition["version"],
            definition=definition,
            genes=list(definition["genetic"]["genes"]),
            severity=definition["severity"],
            title=definition["title"],
            recommendation=definition["recommendation"],
            source=definition["source"],
            kind=definition["kind"],
            is_current=False,
            rule_set_label=label,
            change_note=definition.get("change_note"),
            imported_by=user,
        )
        db.session.add(version)
        created.append(version)
    db.session.flush()

    changes: list[dict[str, Any]] = []
    for code in codes:
        rule = rules_by_code[code]
        previous = max(
            (version for version in rule.versions if version.is_current), key=attrgetter("version"), default=None
        )
        newest = max(rule.versions, key=attrgetter("version"))
        for version in rule.versions:
            version.is_current = version is newest
        rule.title = newest.title
        if previous is not newest:
            changes.append(
                {
                    "code": code,
                    "old_version": previous.version if previous is not None else None,
                    "new_version": newest.version,
                    "old_rule_version_id": previous.id if previous is not None else None,
                    "new_rule_version_id": newest.id,
                }
            )
    db.session.flush()

    audit(
        "rules_imported",
        "rule_set",
        detail={
            "rule_set": doc.get("rule_set"),
            "label": label,
            "created_versions": [version.label for version in created],
            "changes": changes,
        },
        user=user,
    )
    logger.info(
        "Imported rule set %r: %d new version(s), %d current version change(s)", label, len(created), len(changes)
    )
    return changes
