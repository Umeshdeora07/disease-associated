"""Ground truth and the metrics of the evaluation (SPEC §10, paper Table VI).

**Ground truth** (:func:`ground_truth`) is computed without the database or the re-evaluation
engine: every planned patient's panel variants are classified from the in-memory release records
(:func:`evaluation.knowledge.classify_records`) and evaluated with the pure Algorithm 1
(:func:`ge_ehr.services.rules.evaluate`) against the patient's planned clinical context. The result
is the set of findings (:class:`~evaluation.strategies.FindingId`) a correct system shows.

**Metrics** of one knowledge change (:func:`change_metrics`), given the ground truth before (GT₀)
and after (GT₁) the change:

* *re-alert sensitivity* — carrier pairs (patient, variant) that must be re-alerted (release:
  every carrier of an upgraded variant; rule revision: every pair with a newly indicated alert)
  that have a *new* current alert from that variant / all such pairs;
* *stale alerts* — current alerts not in GT₁; *missed alerts* — GT₁ alerts not current;
  *affected alerts* — GT₀ alerts that GT₁ no longer contains (what a stale-free system removes);
* *notification precision / recall* — a notification is correct when it is a new alert expected
  by GT₁ (in GT₁ ∖ GT₀, severity ≠ info) or a review notice for a (patient, variant) whose
  actionable (severity ≠ info) alert GT₁ removed; precision = correct / notifications, recall =
  correct / expected notifications;
* *records examined*, *patients re-evaluated* and the wall-clock *time* of the change.

:func:`scenario_checks` verifies the correctness cohort against paper Table V: S1–S9 through the
point-in-time view at T0 (what the system recommended before any knowledge change), S10 (a
malformed VCF is rejected), S11 (the FHIR Bundle of the S1 patient validates), S12 (a lab
technician gets 403 on ``patients.recommendations`` through the test client), and RS1–RS7 on the
state after the knowledge changes.

Owned by the Eval work package.
"""

from __future__ import annotations

import logging
import secrets
from collections.abc import Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from flask import Flask, url_for
from sqlalchemy import func, select

from ge_ehr import constants as C
from ge_ehr.extensions import db
from ge_ehr.models import (
    Alert,
    GenomicUpload,
    KbRelease,
    Patient,
    PatientVariant,
    ReviewNotice,
    User,
    VariantChange,
    VariantKey,
)
from ge_ehr.services import fhir_export, patient_records, reevaluation
from ge_ehr.services.rules import ClassifiedVariant, ClinicalContext, RuleSpec, evaluate, validate_rule_set
from ge_ehr.services.vcf_parser import VcfError

from .knowledge import InjectedChange, Key, KnownRecord, key_label
from .strategies import ChangeOutcome, CohortPlan, FindingId, Notification, StrategyOutcome

logger = logging.getLogger(__name__)

NO_CLASS_CHANGE = "no_class_change"
UNCHANGED = "unchanged"
BUCKETS: tuple[str, ...] = (*C.CHANGE_CATEGORIES, NO_CLASS_CHANGE, UNCHANGED)
BUCKET_LABELS: dict[str, str] = {
    **C.CHANGE_CATEGORY_LABELS,
    NO_CLASS_CHANGE: "Raw significance change only (no class change)",
    UNCHANGED: "Unchanged variants",
}
EXAMPLES = 5  # stale / missed findings listed per change for diagnosis


# ---------------------------------------------------------------------------
# Ground truth
# ---------------------------------------------------------------------------


def rule_specs(doc: Mapping[str, Any]) -> list[RuleSpec]:
    """Executable specs of every rule in a rule-set document (validated)."""
    return [RuleSpec.from_definition(definition) for definition in validate_rule_set(doc)]


def ground_truth(
    plan: CohortPlan, release: Mapping[Key, KnownRecord], rules: Sequence[RuleSpec]
) -> frozenset[FindingId]:
    """The findings every patient of ``plan`` should have under ``release`` and ``rules`` (pure).

    Keys absent from ``release`` are not classified (NC); off-panel variants are ignored as the
    VCF parser ignores them; the clinical context is the patient's planned diagnosis and current
    medications.
    """
    key_ids: dict[Key, int] = {}
    keys: dict[int, Key] = {}
    findings: set[FindingId] = set()
    for planned in plan.patients:
        variants = []
        for chrom, pos, ref, alt, zygosity in planned.panel_variants:
            key = (chrom, pos, ref, alt)
            key_id = key_ids.setdefault(key, len(key_ids) + 1)
            keys[key_id] = key
            known = release.get(key)
            variants.append(
                ClassifiedVariant(
                    patient_variant_id=None,
                    variant_key_id=key_id,
                    gene=C.gene_for_position(chrom, pos) or "",
                    classification=known.classification if known else "NC",
                    mechanism=known.mechanism if known else "unknown",
                    zygosity=zygosity,
                    label=key_label(key),
                )
            )
        patient = planned.patient
        context = ClinicalContext(
            drug_classes=frozenset(medication.drug_class for medication in patient.medications),
            age_at_diagnosis_months=patient.age_at_diagnosis_months,
            diabetes_type=patient.diabetes_type,
        )
        for finding in evaluate(variants, context, rules):
            findings.add(
                FindingId(planned.mrn, finding.rule_code, keys[finding.variant_key_id], finding.kind, finding.severity)
            )
    return frozenset(findings)


# ---------------------------------------------------------------------------
# Metrics of one knowledge change
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Ratio:
    """``numerator / denominator``; ``value`` is None when the denominator is 0 (n/a)."""

    numerator: int
    denominator: int

    @property
    def value(self) -> float | None:
        return self.numerator / self.denominator if self.denominator else None

    def as_dict(self) -> dict[str, Any]:
        return {"numerator": self.numerator, "denominator": self.denominator, "value": self.value}


def _alerts(findings: Iterable[FindingId]) -> set[FindingId]:
    return {finding for finding in findings if finding.kind == "alert"}


def _actionable(finding: FindingId) -> bool:
    return finding.kind == "alert" and finding.severity != "info"


def expected_notifications(
    before: frozenset[FindingId], after: frozenset[FindingId]
) -> tuple[set[FindingId], set[tuple[str, Key]]]:
    """(new actionable alerts GT₁ ∖ GT₀, (patient, variant) pairs whose actionable alert GT₁ removed)."""
    new_alerts = {finding for finding in after - before if _actionable(finding)}
    removed = {(finding.mrn, finding.key) for finding in before - after if _actionable(finding)}
    return new_alerts, removed


def _notification_identity(notification: Notification) -> FindingId | tuple[str, Key]:
    if notification.channel == "alert":
        return FindingId(
            notification.mrn, notification.rule_code or "", notification.key, "alert", notification.severity
        )
    return (notification.mrn, notification.key)


def _correct_notifications(
    notifications: Iterable[Notification], expected: Collection[FindingId | tuple[str, Key]]
) -> int:
    """Notifications matching an expected one; a repeated notification counts once."""
    seen: set[FindingId | tuple[str, Key]] = set()
    for notification in notifications:
        identity = _notification_identity(notification)
        if identity in expected and identity not in seen:
            seen.add(identity)
    return len(seen)


def release_realert_pairs(plan: CohortPlan, changes: Iterable[InjectedChange]) -> set[tuple[str, Key]]:
    """(patient, variant) pairs of every carrier of a variant upgraded into the actionable band."""
    upgraded = {change.key for change in changes if change.category == "upgrade"}
    return {(mrn, key) for key in upgraded for mrn in plan.carriers.get(key, ())}


def revision_realert_pairs(before: frozenset[FindingId], after: frozenset[FindingId]) -> set[tuple[str, Key]]:
    """(patient, variant) pairs with an alert that the rule revision newly indicates."""
    return {(finding.mrn, finding.key) for finding in _alerts(after - before)}


def _examples(findings: Iterable[FindingId]) -> list[str]:
    return [
        f"{finding.mrn} {finding.rule_code} {finding.kind}{f' {finding.severity}' if finding.severity else ''} "
        f"{key_label(finding.key)}"
        for finding in sorted(findings)[:EXAMPLES]
    ]


def change_metrics(
    outcome: ChangeOutcome,
    *,
    before: frozenset[FindingId],
    after: frozenset[FindingId],
    realert_pairs: Collection[tuple[str, Key]],
) -> dict[str, Any]:
    """Table VI metrics of one knowledge change in one strategy (JSON-ready), see module docstring."""
    state_alerts, after_alerts, before_alerts = _alerts(outcome.state), _alerts(after), _alerts(before)
    realerted = {(finding.mrn, finding.key) for finding in _alerts(outcome.new_findings)}
    new_alerts, removed = expected_notifications(before, after)
    expected: set[FindingId | tuple[str, Key]] = {*new_alerts, *removed}
    correct = _correct_notifications(outcome.notifications, expected)
    run = outcome.run or {}
    engine_ms = run.get("engine_duration_ms")
    stale, missed = state_alerts - after_alerts, after_alerts - state_alerts
    return {
        "re_alert_sensitivity": Ratio(sum(pair in realerted for pair in realert_pairs), len(realert_pairs)).as_dict(),
        "stale_alerts": len(stale),
        "affected_alerts": len(before_alerts - after_alerts),
        "missed_alerts": len(missed),
        "stale_findings": len(outcome.state - after),
        "missed_findings": len(after - outcome.state),
        "final_state_matches_ground_truth": outcome.state == after,
        "stale_examples": _examples(stale),
        "missed_examples": _examples(missed),
        "notifications": len(outcome.notifications),
        "alert_notifications": sum(item.channel == "alert" for item in outcome.notifications),
        "review_notices": sum(item.channel == "review_notice" for item in outcome.notifications),
        "correct_notifications": correct,
        "expected_notifications": len(expected),
        "expected_alert_notifications": len(new_alerts),
        "expected_review_notices": len(removed),
        "notification_precision": Ratio(correct, len(outcome.notifications)).as_dict(),
        "notification_recall": Ratio(correct, len(expected)).as_dict(),
        "alerts_created": len(_alerts(outcome.new_findings)),
        "notes_created": len(outcome.new_findings) - len(_alerts(outcome.new_findings)),
        "findings_superseded": len(outcome.superseded),
        "records_examined": outcome.records_examined,
        "patients_reevaluated": int(run.get("patients_reevaluated", 0)),
        "stored_keys_compared": run.get("stored_keys_compared"),
        "changed_variants": run.get("changed_variants"),
        "duration_ms": round(outcome.duration_ms, 3),
        "commit_ms": round(outcome.commit_ms, 3),
        "engine_duration_ms": round(engine_ms, 3) if engine_ms is not None else None,
    }


def delta_check(outcome: ChangeOutcome, plan: CohortPlan, changes: Iterable[InjectedChange]) -> dict[str, Any] | None:
    """Compare Δ recorded by the engine (VariantChange rows) with the injected changes carried by the cohort.

    Expected Δ = carried keys whose class or mechanism changed (a raw CLNSIG change that keeps the
    class, e.g. VUS → Conflicting, is not a change). ``carrier_records`` = the patient-variant
    records of the expected Δ, i.e. what change-driven re-evaluation must examine ("changed
    only"). None for a strategy without a run.
    """
    if outcome.changed_keys is None:
        return None
    carried = plan.carried_keys
    expected = {change.key for change in changes if change.key in carried and change.category is not None}
    recorded = set(outcome.changed_keys)
    return {
        "expected": len(expected),
        "recorded": len(recorded),
        "carrier_records": sum(len(plan.carriers[key]) for key in expected),
        "matches": expected == recorded,
        "missing": [key_label(key) for key in sorted(expected - recorded)[:EXAMPLES]],
        "unexpected": [key_label(key) for key in sorted(recorded - expected)[:EXAMPLES]],
    }


def bucket_of_keys(plan: CohortPlan, changes: Iterable[InjectedChange]) -> dict[Key, str]:
    """Category bucket of every changed key the cohort carries (keys not listed are unchanged)."""
    carried = plan.carried_keys
    return {change.key: change.category or NO_CLASS_CHANGE for change in changes if change.key in carried}


def category_breakdown(
    outcomes: Mapping[str, ChangeOutcome],
    *,
    plan: CohortPlan,
    changes: Sequence[InjectedChange],
    before: frozenset[FindingId],
    after: frozenset[FindingId],
) -> dict[str, Any]:
    """Expected and observed outcomes of the release change per change category (and per strategy)."""
    buckets = bucket_of_keys(plan, changes)

    def bucket(finding: FindingId) -> str:
        return buckets.get(finding.key, UNCHANGED)

    new_alerts, removed = expected_notifications(before, after)
    result: dict[str, Any] = {}
    for name in BUCKETS:
        keys = (
            [key for key, value in buckets.items() if value == name]
            if name != UNCHANGED
            else [key for key in plan.carried_keys if key not in buckets]
        )
        expected = {f for f in new_alerts if bucket(f) == name} | {
            pair for pair in removed if buckets.get(pair[1], UNCHANGED) == name
        }
        row: dict[str, Any] = {
            "label": BUCKET_LABELS[name],
            "changed_keys": len(keys) if name != UNCHANGED else 0,
            "carried_keys": len(keys),
            "carrier_pairs": sum(len(plan.carriers[key]) for key in keys),
            "expected_new_alerts": len({f for f in _alerts(after - before) if bucket(f) == name}),
            "expected_removed_alerts": len({f for f in _alerts(before - after) if bucket(f) == name}),
            "expected_notifications": len(expected),
            "strategies": {},
        }
        for strategy, outcome in outcomes.items():
            in_bucket = [item for item in outcome.notifications if buckets.get(item.key, UNCHANGED) == name]
            row["strategies"][strategy] = {
                "alerts_created": len({f for f in _alerts(outcome.new_findings) if bucket(f) == name}),
                "alerts_superseded": len({f for f in _alerts(outcome.superseded) if bucket(f) == name}),
                "stale_alerts": len({f for f in _alerts(outcome.state) - _alerts(after) if bucket(f) == name}),
                "missed_alerts": len({f for f in _alerts(after) - _alerts(outcome.state) if bucket(f) == name}),
                "notifications": len(in_bucket),
                "correct_notifications": _correct_notifications(in_bucket, expected),
            }
        result[name] = row
    return result


def kind_breakdown(plan: CohortPlan, changes: Iterable[InjectedChange]) -> dict[str, dict[str, int]]:
    """Per injected change kind: changed keys the cohort carries and their carrier pairs."""
    rows: dict[str, dict[str, int]] = {}
    for change in changes:
        if change.key in plan.carried_keys:
            row = rows.setdefault(change.kind, {"carried_keys": 0, "carrier_pairs": 0})
            row["carried_keys"] += 1
            row["carrier_pairs"] += len(plan.carriers[change.key])
    return rows


def state_check(observed: frozenset[FindingId], expected: frozenset[FindingId]) -> dict[str, Any]:
    """Compare an observed set of findings with the ground truth (e.g. the initial state at T0)."""
    return {
        "findings": len(observed),
        "expected": len(expected),
        "matches": observed == expected,
        "unexpected_examples": _examples(observed - expected),
        "missing_examples": _examples(expected - observed),
    }


# ---------------------------------------------------------------------------
# Scenario checks (paper Table V)
# ---------------------------------------------------------------------------

Expectation = frozenset[tuple[str, str, str | None]]

# Findings of the S1–S9 patients right after their upload (SPEC §11, "Expected under R1 + rules v1").
SCENARIO_EXPECTATIONS: dict[str, Expectation] = {
    "S1": frozenset({("D2", "alert", "high"), ("D5", "alert", "low")}),
    "S2": frozenset({("D2", "note", None), ("D5", "alert", "low")}),
    "S3": frozenset({("D1", "alert", "medium"), ("D5", "alert", "low")}),
    "S4": frozenset({("D1", "note", None), ("D5", "alert", "low")}),
    "S5": frozenset({("D3", "alert", "medium"), ("D5", "alert", "low")}),
    "S6": frozenset({("D4", "alert", "high"), ("D5", "alert", "low")}),
    "S7": frozenset({("D5", "alert", "low")}),
    "S8": frozenset({("D6", "alert", "info")}),
    "S9": frozenset(),
}
TABLE_V: dict[str, tuple[str, str]] = {
    "S1": ("HNF1A pathogenic, on insulin", "High alert, D2"),
    "S2": ("HNF1A pathogenic, already on sulfonylurea", "Note only (no D2 alert)"),
    "S3": ("GCK pathogenic, on metformin", "Medium alert, D1"),
    "S4": ("GCK pathogenic, no medication", "Note only (no D1 alert)"),
    "S5": ("HNF4A likely pathogenic, on metformin", "Medium alert, D3"),
    "S6": ("KCNJ11 pathogenic, diagnosed in infancy, on insulin", "High alert, D4"),
    "S7": ("Any actionable variant", "Low alert, D5"),
    "S8": ("HNF1A VUS", "Info, D6"),
    "S9": ("Only benign variants", "No alert"),
    "S10": ("Malformed VCF", "Rejected"),
    "S11": ("FHIR export of S1 patient", "Valid FHIR R4 Bundle"),
    "S12": ("Lab technician opens recommendations", "Access denied"),
    "RS1": ("HNF1A VUS (S8) upgraded to LP; patient on insulin", "New high alert D2, reclassified"),
    "RS2": ("GCK LP downgraded to VUS", "Alert superseded; review notice"),
    "RS3": ("HNF4A LP changed to P", "Provenance updated; no notice"),
    "RS4": ("HNF1A VUS changed to LB", "Provenance updated; no notice"),
    "RS5": ("D4 v1 (KCNJ11) replaced by v2 (adds ABCC8)", "ABCC8 carriers on insulin newly alerted"),
    "RS6": ("Upgraded variant shared by a family of patients", "Every carrier alerted"),
    "RS7": ("Changed variant carried by no patient", "No work performed"),
}
POINT_IN_TIME_SCENARIOS: tuple[str, ...] = ("S1", "S2", "S3", "S4", "S5", "S6", "S7", "S8", "S9", "S10", "S11", "S12")
MALFORMED_VCF = Path(__file__).resolve().parents[1] / "data" / "demo" / "vcfs" / "malformed.vcf"


@dataclass(frozen=True)
class ScenarioCheck:
    """Outcome of one Table V scenario in one strategy's database."""

    code: str
    passed: bool
    expected: str
    observed: str

    def as_dict(self) -> dict[str, Any]:
        return {"code": self.code, "passed": self.passed, "expected": self.expected, "observed": self.observed}


def _describe(findings: Iterable[tuple[str, str, str | None]]) -> str:
    parts = [
        f"{rule} {severity} alert" if kind == "alert" else f"{rule} note"
        for rule, kind, severity in sorted(findings, key=lambda item: (item[0], item[1]))
    ]
    return ", ".join(parts) or "none"


def _patient(plan: CohortPlan, code: str) -> Patient:
    planned = plan.scenario_patients(code)
    if not planned:
        raise ValueError(f"The correctness cohort has no patient for scenario {code}.")
    return db.session.execute(select(Patient).where(Patient.mrn == planned[0].mrn)).scalar_one()


def _key_id(plan: CohortPlan, name: str) -> int:
    key = VariantKey.find(*plan.scenario_keys[name])
    if key is None:
        raise ValueError(f"Scenario variant {name} ({key_label(plan.scenario_keys[name])}) is not stored.")
    return key.id


def _variant_alerts(patient: Patient, key_id: int) -> list[Alert]:
    statement = select(Alert).where(Alert.patient_id == patient.id, Alert.variant_key_id == key_id).order_by(Alert.id)
    return list(db.session.scalars(statement))


def _findings(alerts: Iterable[Alert]) -> Expectation:
    return frozenset((alert.rule_code, alert.kind, alert.severity) for alert in alerts)


def _patient_notifications(outcome: ChangeOutcome, mrn: str) -> list[Notification]:
    return [item for item in outcome.notifications if item.mrn == mrn]


def _check_point_in_time(plan: CohortPlan, code: str, t0: datetime) -> ScenarioCheck:
    shown = _findings(reevaluation.alerts_as_of(_patient(plan, code), t0))
    expected = SCENARIO_EXPECTATIONS[code]
    return ScenarioCheck(code, shown == expected, _describe(expected), f"as of T0: {_describe(shown)}")


def _check_malformed_upload(plan: CohortPlan, malformed: Path) -> ScenarioCheck:
    patient = _patient(plan, "S9")
    count = select(func.count(PatientVariant.id)).where(PatientVariant.patient_id == patient.id)
    variants_before = db.session.scalar(count)
    try:
        patient_records.ingest_vcf(patient, malformed.read_bytes(), malformed.name)
        error = None
    except VcfError as exc:
        error = str(exc)
    db.session.commit()
    upload = db.session.scalars(
        select(GenomicUpload).where(GenomicUpload.patient_id == patient.id).order_by(GenomicUpload.id.desc())
    ).first()
    stored_rejected = upload is not None and upload.status == "rejected"
    unchanged = db.session.scalar(count) == variants_before
    observed = f"rejected: {error}" if error else "accepted"
    if error and not (stored_rejected and unchanged):
        observed += " (but the rejected upload was not recorded or variants changed)"
    return ScenarioCheck(
        "S10", bool(error) and stored_rejected and unchanged, "VcfError, rejected upload stored, no variants", observed
    )


def _check_fhir(plan: CohortPlan) -> ScenarioCheck:
    bundle = fhir_export.build_patient_bundle(_patient(plan, "S1"))
    try:
        fhir_export.validate_bundle(bundle)
    except ValueError as exc:
        return ScenarioCheck("S11", False, "valid FHIR R4 Bundle", f"invalid: {exc}")
    return ScenarioCheck("S11", True, "valid FHIR R4 Bundle", f"valid Bundle with {len(bundle['entry'])} entries")


def _check_access_denied(app: Flask, plan: CohortPlan) -> ScenarioCheck:
    """S12 through the real login form and ``patients.recommendations`` of the S1 patient.

    Passes when the lab technician gets 403 while a clinician (the control) is not denied —
    neither 403 nor sent back to the login page — so the 403 is a role decision. Runs outside
    any app context so each request gets a fresh one.
    """
    password = secrets.token_urlsafe(18) + "aA1!"
    users = {"lab_technician": "eval.lab", "clinician": "eval.clinician"}
    with app.app_context():
        for role, username in users.items():
            user = User(username=username, full_name=f"Evaluation {C.ROLE_LABELS[role].lower()}", role=role)
            user.set_password(password)
            db.session.add(user)
        db.session.commit()
        patient_id = _patient(plan, "S1").id
    with app.test_request_context():
        url = url_for("patients.recommendations", patient_id=patient_id)
    statuses: dict[str, int] = {}
    for role, username in users.items():
        client = app.test_client()
        login = client.post("/login", data={"username": username, "password": password})
        if login.status_code != 302:
            return ScenarioCheck(
                "S12", False, "lab technician 403", f"sign-in of {username} failed ({login.status_code})"
            )
        statuses[role] = client.get(url).status_code
    lab, clinician = statuses["lab_technician"], statuses["clinician"]
    passed = lab == 403 and clinician not in (302, 401, 403)
    observed = f"lab technician {lab}, clinician {clinician}"
    if clinician >= 500:
        observed += " (page error after the access check)"
    return ScenarioCheck("S12", passed, "lab technician 403 (clinician not denied)", observed)


def _check_upgrade(plan: CohortPlan, outcome: StrategyOutcome) -> ScenarioCheck:
    """RS1: S8's HNF1A VUS → LP; on insulin → new reclassified high-priority D2 high + D5; D6 superseded."""
    patient, started = _patient(plan, "S8"), outcome.release.started_at
    alerts = _variant_alerts(patient, _key_id(plan, "V06"))
    current = [alert for alert in alerts if alert.is_current]
    flagged = all(
        alert.created_at >= started and alert.is_reclassified and alert.priority == "high" and alert.notify
        for alert in current
    )
    d6_superseded = any(
        alert.rule_code == "D6" and alert.status == "superseded" and alert.superseded_reason == "reclassified"
        for alert in alerts
    )
    expected = frozenset({("D2", "alert", "high"), ("D5", "alert", "low")})
    observed = (
        f"current: {_describe(_findings(current))}; reclassified/high priority/notify: {flagged}; "
        f"D6 superseded: {d6_superseded}"
    )
    return ScenarioCheck(
        "RS1",
        _findings(current) == expected and flagged and d6_superseded,
        f"{_describe(expected)} (reclassified, high priority, notify); D6 superseded",
        observed,
    )


def _check_downgrade(plan: CohortPlan, outcome: StrategyOutcome) -> ScenarioCheck:
    """RS2: GCK LP → VUS: D1 and D5 superseded (downgrade), one review notice, silent D6, FHIR amended."""
    patient, started = _patient(plan, "RS2"), outcome.release.started_at
    key_id = _key_id(plan, "V07")
    alerts = _variant_alerts(patient, key_id)
    superseded = [alert for alert in alerts if alert.status == "superseded" and alert.superseded_at >= started]
    superseded_ok = {alert.rule_code for alert in superseded} == {"D1", "D5"} and all(
        alert.superseded_reason == "downgrade" for alert in superseded
    )
    notices = list(
        db.session.scalars(
            select(ReviewNotice).where(
                ReviewNotice.patient_id == patient.id,
                ReviewNotice.variant_key_id == key_id,
                ReviewNotice.created_at >= started,
            )
        )
    )
    notice_ok = len(notices) == 1 and {alert.id for alert in notices[0].alerts} == {alert.id for alert in superseded}
    current = [alert for alert in alerts if alert.is_current]
    silent_d6 = _findings(current) == {("D6", "alert", "info")} and not any(alert.notify for alert in current)
    variant = db.session.execute(
        select(PatientVariant).where(PatientVariant.patient_id == patient.id, PatientVariant.variant_key_id == key_id)
    ).scalar_one()
    bundle = fhir_export.build_patient_bundle(patient)
    statuses = {
        entry["resource"]["status"] for entry in bundle["entry"] if entry["resource"]["resourceType"] == "Observation"
    }
    amended = variant.is_amended and statuses == {"amended"}
    reason = "downgrade" if superseded_ok else "unexpected"
    observed = (
        f"superseded {sorted(alert.rule_code for alert in superseded)} ({reason}); review notices: {len(notices)}; "
        f"current: {_describe(_findings(current))}; FHIR status: {sorted(statuses)}"
    )
    return ScenarioCheck(
        "RS2",
        superseded_ok and notice_ok and silent_d6 and amended,
        "D1, D5 superseded; 1 review notice; silent D6 info; FHIR amended",
        observed,
    )


def _check_within_actionable(plan: CohortPlan, outcome: StrategyOutcome, t0: datetime) -> ScenarioCheck:
    """RS3: HNF4A LP → P: the same D3/D5 alerts stay current with provenance moved to K_new; no notification."""
    patient = _patient(plan, "S5")
    key_id = _key_id(plan, "V03")
    current = [alert for alert in _variant_alerts(patient, key_id) if alert.is_current]
    before = {alert.id for alert in reevaluation.alerts_as_of(patient, t0) if alert.variant_key_id == key_id}
    release = KbRelease.current()
    provenance = release is not None and all(alert.confirmed_release_id == release.id for alert in current)
    silent = not _patient_notifications(outcome.release, patient.mrn)
    same = {alert.id for alert in current} == before
    expected = frozenset({("D3", "alert", "medium"), ("D5", "alert", "low")})
    observed = (
        f"current: {_describe(_findings(current))}; same alerts: {same}; confirmed under "
        f"{release.label if release else '-'}: {provenance}; notifications: {not silent}"
    )
    return ScenarioCheck(
        "RS3",
        _findings(current) == expected and same and provenance and silent,
        "D3 medium + D5 low kept, provenance updated to K_new, no notification",
        observed,
    )


def _check_within_non_actionable(plan: CohortPlan, outcome: StrategyOutcome) -> ScenarioCheck:
    """RS4: HNF1A VUS → LB: the D6 monitoring alert is superseded silently; no notice, no notification."""
    patient, started = _patient(plan, "RS4"), outcome.release.started_at
    alerts = _variant_alerts(patient, _key_id(plan, "V08"))
    current = [alert for alert in alerts if alert.is_current]
    d6_superseded = any(
        alert.rule_code == "D6" and alert.status == "superseded" and alert.superseded_reason == "reclassified"
        for alert in alerts
    )
    notices = db.session.scalar(
        select(func.count(ReviewNotice.id)).where(
            ReviewNotice.patient_id == patient.id, ReviewNotice.created_at >= started
        )
    )
    silent = notices == 0 and not _patient_notifications(outcome.release, patient.mrn)
    observed = f"current: {_describe(_findings(current))}; D6 superseded: {d6_superseded}; review notices: {notices}"
    return ScenarioCheck(
        "RS4", not current and d6_superseded and silent, "D6 superseded silently; no notice, no notification", observed
    )


def _check_rule_revision(plan: CohortPlan, outcome: StrategyOutcome) -> ScenarioCheck:
    """RS5: D4 v2 adds ABCC8: the neonatal ABCC8 carrier on insulin gets a new D4 high alert that notifies."""
    patient, started = _patient(plan, "RS5"), outcome.rule_revision.started_at
    alerts = _variant_alerts(patient, _key_id(plan, "V09"))
    d4 = [alert for alert in alerts if alert.rule_code == "D4" and alert.is_current]
    before = [alert for alert in reevaluation.alerts_as_of(patient, started) if alert.rule_code == "D4"]
    passed = (
        not before
        and len(d4) == 1
        and d4[0].kind == "alert"
        and d4[0].severity == "high"
        and d4[0].notify
        and d4[0].created_at >= started
        and d4[0].confirmed_rule_version.version == 2
    )
    observed = f"before revision: {_describe(_findings(before))}; after: {_describe(_findings(d4))}" + (
        f" (rule version {d4[0].confirmed_rule_version.version}, notify {d4[0].notify})" if d4 else ""
    )
    return ScenarioCheck("RS5", passed, "new D4 high alert under D4 v2 (notify)", observed)


def _check_family(plan: CohortPlan, outcome: StrategyOutcome) -> ScenarioCheck:
    """RS6: every family member carrying the upgraded HNF4A VUS gets new D3 (alert or note) and D5 findings."""
    started = outcome.release.started_at
    key_id = _key_id(plan, "V10")
    results = []
    for planned in plan.scenario_patients("RS6"):
        patient = db.session.execute(select(Patient).where(Patient.mrn == planned.mrn)).scalar_one()
        drugs = {medication.drug_class for medication in planned.patient.medications}
        d3 = (
            ("D3", "alert", "medium")
            if drugs & {"insulin", "metformin"} and "sulfonylurea" not in drugs
            else ("D3", "note", None)
        )
        current = [alert for alert in _variant_alerts(patient, key_id) if alert.is_current]
        new = all(alert.created_at >= started for alert in current)
        results.append(_findings(current) == {d3, ("D5", "alert", "low")} and new)
    observed = f"{sum(results)} of {len(results)} carriers alerted as expected"
    return ScenarioCheck(
        "RS6", bool(results) and all(results), "every carrier: new D5 low + D3 (alert or note)", observed
    )


def _check_uncarried(plan: CohortPlan) -> ScenarioCheck:
    """RS7: the changed V11 is carried by nobody: no patient variant and no VariantChange row."""
    key = VariantKey.find(*plan.scenario_keys["V11"])
    carriers = changes = 0
    if key is not None:
        carriers = db.session.scalar(
            select(func.count(PatientVariant.id)).where(PatientVariant.variant_key_id == key.id)
        )
        changes = db.session.scalar(select(func.count(VariantChange.id)).where(VariantChange.variant_key_id == key.id))
    return ScenarioCheck(
        "RS7",
        carriers == 0 and changes == 0,
        "not examined (no carrier, not in Δ)",
        f"carriers: {carriers}; VariantChange rows: {changes}",
    )


def scenario_checks(
    app: Flask, plan: CohortPlan, outcome: StrategyOutcome, *, t0: datetime, malformed_vcf: Path = MALFORMED_VCF
) -> list[ScenarioCheck]:
    """Check S1–S12 and RS1–RS7 (paper Table V) in a strategy's database after both knowledge changes.

    S1–S9 use ``alerts_as_of(patient, t0)`` — the recommendations shown before any knowledge
    change; RS1–RS7 use the final state. Call with no app context pushed (S12 signs in through the
    test client). S10 stores a rejected upload, so it runs after the other database checks.
    """
    with app.app_context():
        point_in_time = [_check_point_in_time(plan, code, t0) for code in SCENARIO_EXPECTATIONS]
        fhir = _check_fhir(plan)
        reclassification = [
            _check_upgrade(plan, outcome),
            _check_downgrade(plan, outcome),
            _check_within_actionable(plan, outcome, t0),
            _check_within_non_actionable(plan, outcome),
            _check_rule_revision(plan, outcome),
            _check_family(plan, outcome),
            _check_uncarried(plan),
        ]
        malformed = _check_malformed_upload(plan, malformed_vcf)
        db.session.remove()
    access = _check_access_denied(app, plan)
    return [*point_in_time, malformed, fhir, access, *reclassification]


def point_in_time_correctness(checks: Sequence[ScenarioCheck]) -> Ratio:
    """Passed S1–S12 checks / 12."""
    relevant = [check for check in checks if check.code in POINT_IN_TIME_SCENARIOS]
    return Ratio(sum(check.passed for check in relevant), len(relevant))
