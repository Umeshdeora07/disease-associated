"""Tests for ``services/alerting.py``: reconciliation of findings into alerts/notes (SPEC §6.3),
the current-rule loader, the clinical context and the rule-version import.

Every reconciliation passes ``classified=`` explicitly (or patches the classification service),
so these tests do not depend on the knowledge-base lookup.
"""

from __future__ import annotations

import copy
from collections import Counter
from datetime import date, datetime
from typing import Any

import pytest
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import event, func, select

from ge_ehr.extensions import db as _db
from ge_ehr.models import Alert, AlertEvent, AuditLog, KbRelease, Patient, PatientVariant, Rule, RuleVersion
from ge_ehr.services import classification
from ge_ehr.services.alerting import (
    ReconcileResult,
    clinical_context,
    current_alerts_by_patient,
    evaluate_patient_now,
    import_rule_versions,
    load_current_rules,
    reconcile_patient,
)
from ge_ehr.services.rules import (
    RULES_DATA_DIR,
    ClassifiedVariant,
    ClinicalContext,
    RuleDefinitionError,
    RuleSpec,
    load_rule_set_file,
)
from tests.factories import (
    add_medication,
    add_patient_variant,
    make_alert,
    make_patient,
    make_release,
    make_rule_version,
    make_run,
    make_user,
    rule_definition,
)

V1 = load_rule_set_file(RULES_DATA_DIR / "ruleset_v1.json")
V2 = load_rule_set_file(RULES_DATA_DIR / "ruleset_v2.json")
NOW = datetime(2026, 3, 1, 9, 30)

# Demo variant keys (SPEC §11).
V01_HNF1A = ("12", 120978900, "G", "A")
V02_GCK = ("7", 44145200, "C", "T")
V04_KCNJ11 = ("11", 17387100, "C", "T")
V06_HNF1A = ("12", 120994300, "C", "T")
V09_ABCC8 = ("11", 17410300, "G", "A")


@pytest.fixture
def rules_v1(db: SQLAlchemy) -> list[RuleSpec]:
    import_rule_versions(V1)
    return load_current_rules()


@pytest.fixture
def release(db: SQLAlchemy) -> KbRelease:
    return make_release("R1 (2025-01, synthetic)", current=True)


def classified(pv: PatientVariant, classification_: str | None = None, *, mechanism: str = "unknown") -> ClassifiedVariant:
    return ClassifiedVariant(
        patient_variant_id=pv.id,
        variant_key_id=pv.variant_key_id,
        gene=pv.gene,
        classification=classification_ or pv.current_classification,
        mechanism=mechanism,
        zygosity=pv.zygosity,
        label=pv.variant_key.label,
    )


def patient_with(
    key: tuple[str, int, str, str],
    classification_: str,
    *drugs: str,
    age: int | None = 180,
    diabetes_type: str = "type1",
    release: KbRelease | None = None,
) -> tuple[Patient, PatientVariant]:
    patient = make_patient(diagnosis={"diabetes_type": diabetes_type, "age_at_diagnosis_months": age})
    for drug in drugs:
        add_medication(patient, drug)
    variant = add_patient_variant(patient, *key, classification=classification_, release=release)
    return patient, variant


def current_alerts(patient: Patient) -> list[Alert]:
    statement = (
        select(Alert)
        .where(Alert.patient_id == patient.id, Alert.status.in_(("active", "reviewed")))
        .order_by(Alert.rule_code, Alert.variant_key_id, Alert.kind)
    )
    return list(_db.session.scalars(statement))


def summary(alerts: list[Alert]) -> list[tuple[str, str, str | None]]:
    return [(alert.rule_code, alert.kind, alert.severity) for alert in alerts]


def assert_one_current_alert_per_key(patient: Patient) -> None:
    keys = Counter((alert.rule_code, alert.variant_key_id, alert.kind) for alert in current_alerts(patient))
    assert all(count == 1 for count in keys.values()), keys


# ---------------------------------------------------------------------------
# Creating alerts and notes
# ---------------------------------------------------------------------------


def test_creates_alerts_with_full_provenance(db: SQLAlchemy, release: KbRelease, rules_v1: list[RuleSpec]) -> None:
    patient, pv = patient_with(V01_HNF1A, "P", "Insulin glargine", release=release)
    run, user = make_run("vcf_upload", strategy="patient"), make_user("lab.kim", role="lab_technician")
    d2 = db.session.execute(select(RuleVersion).filter_by(code="D2")).scalar_one()

    result = reconcile_patient(
        patient, release, rules_v1, trigger="vcf_upload", run=run, user=user, now=NOW, classified=[classified(pv)]
    )

    assert isinstance(result, ReconcileResult)
    assert (result.superseded, result.confirmed, result.unchanged, result.records_examined) == ([], [], [], 1)
    assert summary(result.created) == [("D2", "alert", "high"), ("D5", "alert", "low")]
    alert = result.created[0]
    assert alert.id is not None, "the service flushes"
    assert (alert.patient_id, alert.patient_variant_id, alert.variant_key_id) == (patient.id, pv.id, pv.variant_key_id)
    assert (alert.created_rule_version_id, alert.confirmed_rule_version_id) == (d2.id, d2.id)
    assert (alert.title, alert.message, alert.source) == (d2.title, d2.recommendation, d2.source)
    assert (alert.classification, alert.status, alert.trigger) == ("P", "active", "vcf_upload")
    assert (alert.is_reclassified, alert.priority, alert.notify) == (False, "normal", True)
    assert (alert.created_release_id, alert.confirmed_release_id) == (release.id, release.id)
    assert (alert.created_run_id, alert.superseded_run_id, alert.created_at) == (run.id, None, NOW)
    assert [e.event for e in alert.events] == ["created"]
    created = alert.events[0]
    assert (created.release_id, created.rule_version_id, created.classification) == (release.id, d2.id, "P")
    assert (created.run_id, created.user_id, created.at) == (run.id, user.id, NOW)
    assert created.detail == "Alert created by D2 v1 under release R1 (2025-01, synthetic) (trigger: VCF upload)."
    assert patient.alerts == result.created, "the patient's relationship sees the new alerts"


def test_notes_and_info_alerts_do_not_notify(db: SQLAlchemy, release: KbRelease, rules_v1: list[RuleSpec]) -> None:
    patient, pv = patient_with(V01_HNF1A, "P", "Gliclazide 80 mg", age=300, diabetes_type="type2")
    vus = add_patient_variant(patient, *V06_HNF1A, classification="VUS")
    result = reconcile_patient(
        patient, release, rules_v1, trigger="vcf_upload", classified=[classified(pv), classified(vus)]
    )
    by_rule = {alert.rule_code: alert for alert in result.created}
    assert summary(result.created) == [("D2", "note", None), ("D5", "alert", "low"), ("D6", "alert", "info")]
    assert by_rule["D2"].message == (
        "Genetic criterion of D2 met (HNF1A pathogenic) but therapy condition not met: "
        "already receiving a sulfonylurea. No alert raised."
    )
    assert by_rule["D2"].events[0].detail.startswith("Note created by D2 v1")
    assert [by_rule[code].notify for code in ("D2", "D5", "D6")] == [False, True, False]
    assert result.records_examined == 2


def test_reconcile_is_idempotent(db: SQLAlchemy, release: KbRelease, rules_v1: list[RuleSpec]) -> None:
    patient, pv = patient_with(V01_HNF1A, "P", "Insulin glargine")
    first = reconcile_patient(patient, release, rules_v1, trigger="vcf_upload", classified=[classified(pv)])
    events_before = db.session.scalar(select(func.count(AlertEvent.id)))

    again = reconcile_patient(patient, release, rules_v1, trigger="manual", classified=[classified(pv)])

    assert (again.created, again.superseded, again.confirmed) == ([], [], [])
    assert again.unchanged == first.created
    assert db.session.scalar(select(func.count(AlertEvent.id))) == events_before
    assert all(alert.trigger == "vcf_upload" for alert in again.unchanged)


def test_without_release_or_matching_variants(db: SQLAlchemy, rules_v1: list[RuleSpec]) -> None:
    patient, pv = patient_with(V01_HNF1A, "NC", "Insulin glargine")
    result = reconcile_patient(patient, None, rules_v1, trigger="vcf_upload", classified=[classified(pv)])
    assert (result.created, result.records_examined) == ([], 1)

    result = reconcile_patient(patient, None, rules_v1, trigger="manual", classified=[classified(pv, "LP")])
    assert summary(result.created) == [("D2", "alert", "high"), ("D5", "alert", "low")]
    assert result.created[0].created_release_id is None
    assert "under no knowledge-base release" in result.created[0].events[0].detail


# ---------------------------------------------------------------------------
# Confirming provenance
# ---------------------------------------------------------------------------


def test_new_release_confirms_provenance_without_changing_the_alert(
    db: SQLAlchemy, release: KbRelease, rules_v1: list[RuleSpec]
) -> None:
    patient, pv = patient_with(V01_HNF1A, "LP", "Insulin glargine", release=release)
    created = reconcile_patient(patient, release, rules_v1, trigger="vcf_upload", classified=[classified(pv)]).created
    d2 = created[0]
    d2.status = "reviewed"
    d2.reviewed_at = NOW
    assert d2.confirmed_release is release
    release_2 = make_release("R2 (2026-01, synthetic)", current=True)
    run = make_run("clinvar_release", new_release_id=release_2.id)

    result = reconcile_patient(
        patient, release_2, rules_v1, trigger="clinvar_release", variant_key_ids={pv.variant_key_id}, run=run,
        superseded_reason="reclassified", now=NOW, classified=[classified(pv, "P")],
    )

    assert result.confirmed == created and (result.created, result.superseded, result.unchanged) == ([], [], [])
    for alert in created:
        assert (alert.created_release_id, alert.confirmed_release_id) == (release.id, release_2.id)
        assert alert.confirmed_release is release_2, "a loaded relationship follows the new provenance"
        assert alert.classification == "LP", "classification at creation is kept"
        assert [e.event for e in alert.events] == ["created", "confirmed"]
        confirmed = alert.events[-1]
        assert (confirmed.release_id, confirmed.classification, confirmed.run_id, confirmed.at) == (
            release_2.id, "P", run.id, NOW,
        )
        assert confirmed.detail == f"Confirmed by {alert.rule_code} v1 under release R2 (2026-01, synthetic)."
    assert d2.status == "reviewed", "a reviewed alert stays reviewed while it still fires"
    assert d2.notify is True and d2.message == "Consider low-dose sulfonylurea as first-line treatment."


def test_note_text_follows_the_current_therapy(db: SQLAlchemy, release: KbRelease, rules_v1: list[RuleSpec]) -> None:
    patient, pv = patient_with(V01_HNF1A, "P", "Gliclazide 80 mg", age=300, diabetes_type="type2")
    note = reconcile_patient(patient, release, rules_v1, trigger="vcf_upload", classified=[classified(pv)]).created[0]
    old_message = note.message
    patient.medications[0].end_date = date(2026, 2, 1)
    add_medication(patient, "Sitagliptin")  # "other" drug class

    result = reconcile_patient(
        patient, release, rules_v1, trigger="medication_change", rule_codes={"D2"}, classified=[classified(pv)]
    )

    assert result.confirmed == [note] and result.created == [] and result.superseded == []
    assert note.message == (
        "Genetic criterion of D2 met (HNF1A pathogenic) but therapy condition not met: "
        "not receiving insulin or metformin. No alert raised."
    )
    assert note.events[-1].event == "confirmed"
    assert note.events[-1].detail == f"Confirmed by D2 v1 under release R1 (2025-01, synthetic). Previous text: {old_message}"


def test_rule_revision_with_same_severity_confirms_the_rule_version(
    db: SQLAlchemy, release: KbRelease, rules_v1: list[RuleSpec]
) -> None:
    patient, pv = patient_with(V04_KCNJ11, "P", "Insulin", age=2, diabetes_type="neonatal")
    variants = [classified(pv, mechanism="activating")]
    d4 = reconcile_patient(patient, release, rules_v1, trigger="vcf_upload", classified=variants).created[0]
    assert (d4.rule_code, d4.kind, d4.confirmed_rule_version.version) == ("D4", "alert", 1)
    changes = import_rule_versions(V2)

    result = reconcile_patient(
        patient, release, load_current_rules(), trigger="rule_revision", rule_codes={"D4"},
        superseded_reason="rule_revised", classified=variants,
    )

    assert result.confirmed == [d4] and (result.created, result.superseded) == ([], [])
    assert result.records_examined == 1
    assert d4.created_rule_version.version == 1
    assert d4.confirmed_rule_version.version == 2, "the loaded relationship is kept in sync"
    assert d4.confirmed_rule_version_id == changes[0]["new_rule_version_id"]
    assert d4.title == "Neonatal diabetes (activating KCNJ11 or ABCC8 variant) on insulin"
    assert d4.events[-1].rule_version_id == changes[0]["new_rule_version_id"]
    assert "Previous text" not in d4.events[-1].detail, "the recommendation text did not change"


def test_rule_revision_newly_alerts_abcc8_carriers(db: SQLAlchemy, release: KbRelease, rules_v1: list[RuleSpec]) -> None:
    patient, pv = patient_with(V09_ABCC8, "P", "Insulin", age=3, diabetes_type="neonatal")
    variants = [classified(pv, mechanism="activating")]
    assert summary(reconcile_patient(patient, release, rules_v1, trigger="vcf_upload", classified=variants).created) == [
        ("D5", "alert", "low")
    ]
    import_rule_versions(V2)
    result = reconcile_patient(
        patient, release, load_current_rules(), trigger="rule_revision", rule_codes={"D4"},
        superseded_reason="rule_revised", classified=variants,
    )
    assert summary(result.created) == [("D4", "alert", "high")]
    assert result.created[0].notify is True and result.created[0].trigger == "rule_revision"
    assert summary(current_alerts(patient)) == [("D4", "alert", "high"), ("D5", "alert", "low")]


# ---------------------------------------------------------------------------
# Superseding
# ---------------------------------------------------------------------------


def test_therapy_change_supersedes_the_alert_and_creates_a_note(
    db: SQLAlchemy, release: KbRelease, rules_v1: list[RuleSpec]
) -> None:
    patient, pv = patient_with(V01_HNF1A, "P", "Insulin glargine")
    first = reconcile_patient(patient, release, rules_v1, trigger="vcf_upload", classified=[classified(pv)])
    d2_alert, d5_alert = first.created
    add_medication(patient, "Gliclazide 80 mg")
    run, user = make_run("medication_change", strategy="patient"), make_user("dr.rao")
    d2 = db.session.execute(select(RuleVersion).filter_by(code="D2")).scalar_one()

    result = reconcile_patient(
        patient, release, rules_v1, trigger="medication_change", run=run, user=user, now=NOW,
        classified=[classified(pv)],
    )

    assert result.superseded == [d2_alert] and result.unchanged == [d5_alert] and result.confirmed == []
    assert summary(result.created) == [("D2", "note", None)]
    assert "already receiving a sulfonylurea" in result.created[0].message
    assert (d2_alert.status, d2_alert.superseded_at, d2_alert.superseded_reason) == ("superseded", NOW, "therapy_changed")
    assert d2_alert.superseded_run_id == run.id
    superseded = d2_alert.events[-1]
    assert (superseded.event, superseded.release_id, superseded.rule_version_id) == ("superseded", release.id, d2.id)
    assert (superseded.classification, superseded.run_id, superseded.user_id, superseded.at) == ("P", run.id, user.id, NOW)
    assert superseded.detail == (
        "Therapy changed: no longer produced under release R1 (2025-01, synthetic) (trigger: Medication change)."
    )
    assert summary(current_alerts(patient)) == [("D2", "note", None), ("D5", "alert", "low")]
    assert db.session.scalar(select(func.count(Alert.id))) == 3, "superseded alerts are kept"


def test_stopping_the_excluded_drug_turns_the_note_into_an_alert(
    db: SQLAlchemy, release: KbRelease, rules_v1: list[RuleSpec]
) -> None:
    patient, pv = patient_with(V02_GCK, "P", age=240, diabetes_type="other")
    note = reconcile_patient(patient, release, rules_v1, trigger="vcf_upload", classified=[classified(pv)]).created[0]
    assert (note.rule_code, note.kind) == ("D1", "note")
    add_medication(patient, "Metformin 500 mg")
    result = reconcile_patient(patient, release, rules_v1, trigger="medication_change", classified=[classified(pv)])
    assert result.superseded == [note] and summary(result.created) == [("D1", "alert", "medium")]
    assert note.superseded_reason == "therapy_changed"


def test_severity_change_replaces_the_alert(db: SQLAlchemy, release: KbRelease) -> None:
    high = make_rule_version(rule_definition("D7", 1, genes=["INS"], severity="high"))
    patient, pv = patient_with(("11", 2160500, "G", "A"), "LP")
    old = reconcile_patient(
        patient, release, [RuleSpec.from_definition(high.definition, high.id)], trigger="vcf_upload",
        classified=[classified(pv)],
    ).created[0]
    medium = make_rule_version(rule_definition("D7", 2, genes=["INS"], severity="medium"))

    result = reconcile_patient(
        patient, release, [RuleSpec.from_definition(medium.definition, medium.id)], trigger="rule_revision",
        rule_codes={"D7"}, superseded_reason="rule_revised", classified=[classified(pv)],
    )

    assert result.superseded == [old] and summary(result.created) == [("D7", "alert", "medium")]
    assert (old.status, old.superseded_reason) == ("superseded", "rule_revised")
    assert old.events[-1].rule_version_id == medium.id
    new = result.created[0]
    assert (new.created_rule_version_id, new.confirmed_rule_version_id) == (medium.id, medium.id)
    assert_one_current_alert_per_key(patient)


def test_removed_variant_supersedes_its_alerts(db: SQLAlchemy, release: KbRelease, rules_v1: list[RuleSpec]) -> None:
    patient, pv = patient_with(V01_HNF1A, "P", "Insulin glargine")
    created = reconcile_patient(patient, release, rules_v1, trigger="vcf_upload", classified=[classified(pv)]).created
    result = reconcile_patient(
        patient, release, rules_v1, trigger="manual", superseded_reason="variant_removed", classified=[]
    )
    assert result.superseded == created and result.records_examined == 0
    assert {alert.superseded_reason for alert in created} == {"variant_removed"}
    assert {alert.events[-1].classification for alert in created} == {None}


def test_alerts_are_never_deleted_and_stay_unique(db: SQLAlchemy, release: KbRelease, rules_v1: list[RuleSpec]) -> None:
    patient, pv = patient_with(V01_HNF1A, "P")
    variants = [classified(pv)]
    medications = ["Insulin glargine", "Gliclazide 80 mg", None, "Metformin 500 mg", None]
    for name in medications:
        for medication in patient.active_medications:
            medication.end_date = date(2026, 1, 1)
        if name:
            add_medication(patient, name)
        reconcile_patient(patient, release, rules_v1, trigger="medication_change", classified=variants)
        assert_one_current_alert_per_key(patient)
    # D2: alert → note (on SU) → note text (no meds) → alert (metformin) → note (no meds); D5 stays.
    assert db.session.scalar(select(func.count(Alert.id))) == 5
    assert summary(current_alerts(patient)) == [("D2", "note", None), ("D5", "alert", "low")]
    assert "no current glucose-lowering medication" in current_alerts(patient)[0].message


# ---------------------------------------------------------------------------
# Scope restriction
# ---------------------------------------------------------------------------


def test_variant_scope_restricts_desired_and_existing_alerts(
    db: SQLAlchemy, release: KbRelease, rules_v1: list[RuleSpec]
) -> None:
    patient, hnf1a = patient_with(V01_HNF1A, "P", "Metformin 500 mg")
    gck = add_patient_variant(patient, *V02_GCK, classification="P")
    initial = reconcile_patient(
        patient, release, rules_v1, trigger="vcf_upload", classified=[classified(hnf1a), classified(gck)]
    )
    assert summary(initial.created) == [("D1", "alert", "medium"), ("D2", "alert", "high"), ("D5", "alert", "low"), ("D5", "alert", "low")]

    # GCK downgraded to VUS, HNF1A unchanged: only the HNF1A key is in scope, so GCK alerts are untouched.
    variants = [classified(hnf1a), classified(gck, "VUS")]
    scoped = reconcile_patient(
        patient, release, rules_v1, trigger="manual", variant_key_ids={hnf1a.variant_key_id}, classified=variants
    )
    assert scoped.records_examined == 1
    assert (scoped.created, scoped.superseded, scoped.confirmed) == ([], [], [])
    assert {alert.variant_key_id for alert in scoped.unchanged} == {hnf1a.variant_key_id}

    full = reconcile_patient(
        patient, release, rules_v1, trigger="clinvar_release", superseded_reason="downgrade", classified=variants
    )
    assert full.records_examined == 2
    assert summary(full.superseded) == [("D1", "alert", "medium"), ("D5", "alert", "low")]
    assert {alert.variant_key_id for alert in full.superseded} == {gck.variant_key_id}
    assert summary(full.created) == [("D6", "alert", "info")]


def test_rule_code_scope_restricts_desired_and_existing_alerts(
    db: SQLAlchemy, release: KbRelease, rules_v1: list[RuleSpec]
) -> None:
    patient, pv = patient_with(V01_HNF1A, "P", "Insulin glargine")
    d2_alert, d5_alert = reconcile_patient(patient, release, rules_v1, trigger="vcf_upload", classified=[classified(pv)]).created

    result = reconcile_patient(
        patient, release, rules_v1, trigger="manual", rule_codes={"D2"}, classified=[classified(pv, "VUS")]
    )

    assert result.superseded == [d2_alert] and result.created == [], "D6 is out of scope"
    assert d5_alert.status == "active", "D5 is out of scope although it no longer applies"
    assert result.records_examined == 1


def test_other_patients_alerts_are_never_touched(db: SQLAlchemy, release: KbRelease, rules_v1: list[RuleSpec]) -> None:
    patient, pv = patient_with(V01_HNF1A, "P", "Insulin glargine")
    _, other_pv = patient_with(V01_HNF1A, "P", "Insulin glargine")
    d2 = db.session.execute(select(RuleVersion).filter_by(code="D2")).scalar_one()
    other_alert = make_alert(other_pv, d2, release=release)

    result = reconcile_patient(patient, release, rules_v1, trigger="manual", classified=[])

    assert result == ReconcileResult()
    assert other_alert.status == "active" and [e.event for e in other_alert.events] == ["created"]


def test_empty_scope_examines_nothing(db: SQLAlchemy, release: KbRelease, rules_v1: list[RuleSpec]) -> None:
    patient, pv = patient_with(V01_HNF1A, "P", "Insulin glargine")
    reconcile_patient(patient, release, rules_v1, trigger="vcf_upload", classified=[classified(pv)])
    result = reconcile_patient(patient, release, rules_v1, trigger="manual", variant_key_ids=[], classified=[classified(pv, "B")])
    assert result == ReconcileResult()
    assert len(current_alerts(patient)) == 2


# ---------------------------------------------------------------------------
# Classification lookup, evaluate_patient_now, argument checks, query count
# ---------------------------------------------------------------------------


@pytest.fixture
def lookup(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Patch the classification service; set ``lookup["variants"]`` to what it should return."""
    state: dict[str, Any] = {"calls": [], "variants": []}

    def fake(patient: Patient, release: KbRelease | None, variant_key_ids: Any = None) -> list[ClassifiedVariant]:
        state["calls"].append((patient, release, variant_key_ids))
        return list(state["variants"])

    monkeypatch.setattr(classification, "classified_variants_for_patient", fake)
    return state


def test_classified_variants_are_looked_up_when_not_given(
    db: SQLAlchemy, release: KbRelease, rules_v1: list[RuleSpec], lookup: dict[str, Any]
) -> None:
    patient, pv = patient_with(V01_HNF1A, "P", "Insulin glargine")
    other = add_patient_variant(patient, *V02_GCK, classification="P")
    lookup["variants"] = [classified(pv), classified(other)]

    result = reconcile_patient(patient, release, rules_v1, trigger="manual", variant_key_ids=[pv.variant_key_id])

    assert lookup["calls"] == [(patient, release, frozenset({pv.variant_key_id}))]
    assert result.records_examined == 1, "variants outside the scope are dropped"
    assert {alert.variant_key_id for alert in result.created} == {pv.variant_key_id}


def test_evaluate_patient_now_uses_current_release_and_rules(
    db: SQLAlchemy, release: KbRelease, rules_v1: list[RuleSpec], lookup: dict[str, Any]
) -> None:
    make_release("Older release")
    patient, pv = patient_with(V04_KCNJ11, "P", "Insulin", age=2, diabetes_type="neonatal")
    lookup["variants"] = [classified(pv, mechanism="activating")]
    user = make_user("dr.rao")

    result = evaluate_patient_now(patient, trigger="vcf_upload", user=user)

    assert lookup["calls"] == [(patient, release, None)]
    assert summary(result.created) == [("D4", "alert", "high"), ("D5", "alert", "low")]
    assert {(alert.trigger, alert.created_release_id) for alert in result.created} == {("vcf_upload", release.id)}
    assert {alert.events[0].user_id for alert in result.created} == {user.id}

    patient.diagnosis.age_at_diagnosis_months = 360
    result = evaluate_patient_now(patient, trigger="diagnosis_change", superseded_reason="diagnosis_changed")
    assert summary(result.superseded) == [("D4", "alert", "high")]
    assert result.superseded[0].superseded_reason == "diagnosis_changed"
    assert summary(result.created) == [("D4", "note", None)]
    assert "diagnosed at 30 years (rule requires diagnosis before 12 months)" in result.created[0].message


def test_evaluate_patient_now_without_current_release(db: SQLAlchemy, rules_v1: list[RuleSpec], lookup: dict[str, Any]) -> None:
    patient, pv = patient_with(V01_HNF1A, "NC", "Insulin glargine")
    lookup["variants"] = [classified(pv)]
    result = evaluate_patient_now(patient, trigger="vcf_upload")
    assert lookup["calls"] == [(patient, None, None)]
    assert result == ReconcileResult(records_examined=1)


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"trigger": "upload"}, r"^Unknown trigger 'upload'; expected one of vcf_upload, medication_change, "),
        ({"trigger": "manual", "superseded_reason": "obsolete"}, r"^Unknown superseded reason 'obsolete'; expected one of downgrade, "),
    ],
)
def test_reconcile_rejects_unknown_arguments(
    db: SQLAlchemy, release: KbRelease, rules_v1: list[RuleSpec], kwargs: dict[str, Any], message: str
) -> None:
    patient, pv = patient_with(V01_HNF1A, "P")
    with pytest.raises(ValueError, match=message):
        reconcile_patient(patient, release, rules_v1, classified=[classified(pv)], **kwargs)


def test_reconcile_requires_stored_rules_in_scope(db: SQLAlchemy, release: KbRelease, rules_v1: list[RuleSpec]) -> None:
    patient, pv = patient_with(V01_HNF1A, "P", "Insulin glargine")
    unstored = RuleSpec.from_definition(rule_definition("D9", genes=["INS"]))
    with pytest.raises(ValueError, match=r"^Rule\(s\) D9 v1 have no stored rule version; import the rule set"):
        reconcile_patient(patient, release, [*rules_v1, unstored], trigger="manual", classified=[classified(pv)])
    result = reconcile_patient(
        patient, release, [*rules_v1, unstored], trigger="manual", rule_codes={"D2"}, classified=[classified(pv)]
    )
    assert summary(result.created) == [("D2", "alert", "high")], "an unstored rule outside the scope is ignored"


def test_current_alerts_are_loaded_with_a_single_query(
    db: SQLAlchemy, release: KbRelease, rules_v1: list[RuleSpec]
) -> None:
    patient, pv = patient_with(V01_HNF1A, "P", "Insulin glargine")
    gck = add_patient_variant(patient, *V02_GCK, classification="VUS")
    variants = [classified(pv), classified(gck)]
    reconcile_patient(patient, release, rules_v1, trigger="vcf_upload", classified=variants)
    statements: list[str] = []

    def record(conn: Any, cursor: Any, statement: str, *args: Any) -> None:
        statements.append(statement)

    event.listen(db.engine, "before_cursor_execute", record)
    try:
        result = reconcile_patient(patient, release, rules_v1, trigger="manual", classified=variants)
    finally:
        event.remove(db.engine, "before_cursor_execute", record)

    assert len(result.unchanged) == 3
    assert len(statements) == 1 and "FROM alerts" in statements[0], statements


def test_preloaded_current_alerts_need_no_query_and_flush_is_deferred(
    db: SQLAlchemy, release: KbRelease, rules_v1: list[RuleSpec]
) -> None:
    patient, pv = patient_with(V01_HNF1A, "P", "Insulin glargine")
    other, other_pv = patient_with(V02_GCK, "P", "Metformin")
    reconcile_patient(patient, release, rules_v1, trigger="vcf_upload", classified=[classified(pv)])
    reconcile_patient(other, release, rules_v1, trigger="vcf_upload", classified=[classified(other_pv)])
    preloaded = current_alerts_by_patient([patient.id, other.id])
    assert summary(sorted(preloaded[patient.id], key=lambda alert: alert.rule_code)) == [
        ("D2", "alert", "high"),
        ("D5", "alert", "low"),
    ]
    patient.medications[0].end_date = date(2026, 2, 1)
    add_medication(patient, "Gliclazide")
    db.session.flush()
    statements: list[str] = []

    def record(conn: Any, cursor: Any, statement: str, *args: Any) -> None:
        statements.append(statement)

    event.listen(db.engine, "before_cursor_execute", record)
    try:
        result = reconcile_patient(
            patient,
            release,
            rules_v1,
            trigger="medication_change",
            classified=[classified(pv)],
            current_alerts=preloaded[patient.id],
            flush=False,
        )
    finally:
        event.remove(db.engine, "before_cursor_execute", record)

    assert statements == [], "no query and no flush: the caller flushes the batch"
    assert summary(result.superseded) == [("D2", "alert", "high")]
    assert summary(result.created) == [("D2", "note", None)] and result.created[0].id is None
    db.session.flush()
    assert_one_current_alert_per_key(patient)
    # The preloaded list stays valid: it gained the new note and its superseded alert is skipped.
    assert result.created[0] in preloaded[patient.id]
    again = reconcile_patient(
        patient, release, rules_v1, trigger="manual", classified=[classified(pv)], current_alerts=preloaded[patient.id]
    )
    assert (again.created, again.superseded, again.confirmed) == ([], [], [])
    assert summary(sorted(again.unchanged, key=lambda alert: alert.rule_code)) == [
        ("D2", "note", None),
        ("D5", "alert", "low"),
    ]


def test_revised_recommendation_replaces_the_alert(
    db: SQLAlchemy, release: KbRelease, rules_v1: list[RuleSpec]
) -> None:
    patient, pv = patient_with(V01_HNF1A, "P", "Insulin glargine")
    d2 = reconcile_patient(patient, release, rules_v1, trigger="vcf_upload", classified=[classified(pv)]).created[0]
    d2.status = "reviewed"
    old_text = V1["rules"][1]["recommendation"]
    revised = rule_definition(
        "D2", 2, recommendation="Refer to the diabetes genetics clinic.", clinical=V1["rules"][1]["clinical"]
    )
    import_rule_versions({"rules": [revised]})

    result = reconcile_patient(
        patient, release, load_current_rules(), trigger="rule_revision", rule_codes={"D2"},
        superseded_reason="rule_revised", classified=[classified(pv)],
    )

    (replacement,) = result.created
    assert result.superseded == [d2] and result.revised == [(d2, replacement)] and result.confirmed == []
    assert (d2.status, d2.superseded_reason, d2.message) == ("superseded", "rule_revised", old_text)
    assert (replacement.status, replacement.notify, replacement.message) == (
        "active",
        True,
        "Refer to the diabetes genetics clinic.",
    )
    created = replacement.events[0]
    assert (created.event, created.message, created.severity, created.title) == (
        "created",
        replacement.message,
        "high",
        replacement.title,
    )
    assert_one_current_alert_per_key(patient)


# ---------------------------------------------------------------------------
# load_current_rules and clinical_context
# ---------------------------------------------------------------------------


def test_load_current_rules(db: SQLAlchemy) -> None:
    assert load_current_rules() == []
    import_rule_versions(V1)
    import_rule_versions(V2)
    import_rule_versions({"rules": [rule_definition("D10", genes=["INS"], severity="low")]})
    rules = load_current_rules()
    assert [rule.label for rule in rules] == ["D1 v1", "D2 v1", "D3 v1", "D4 v2", "D5 v1", "D6 v1", "D10 v1"]
    current_ids = {version.code: version.id for version in RuleVersion.current_versions()}
    assert {rule.code: rule.rule_version_id for rule in rules} == current_ids
    assert next(rule for rule in rules if rule.code == "D4").genes == frozenset({"KCNJ11", "ABCC8"})


def test_clinical_context(db: SQLAlchemy) -> None:
    patient = make_patient(diagnosis={"diabetes_type": "neonatal", "age_at_diagnosis_months": 3})
    add_medication(patient, "Insulin glargine")
    add_medication(patient, "Glibenclamide", end_date=date(2025, 6, 1))
    add_medication(patient, "Metformin 500 mg")
    assert clinical_context(patient) == ClinicalContext(frozenset({"insulin", "metformin"}), 3, "neonatal")


def test_clinical_context_without_diagnosis(db: SQLAlchemy) -> None:
    patient = Patient(mrn="TEST-NODX", given_name="No", family_name="Diagnosis", birth_date=date(2000, 1, 1))
    db.session.add(patient)
    db.session.flush()
    assert clinical_context(patient) == ClinicalContext(frozenset(), None, None)


# ---------------------------------------------------------------------------
# import_rule_versions
# ---------------------------------------------------------------------------


def test_import_into_a_fresh_database(db: SQLAlchemy) -> None:
    user = make_user("admin", role="admin")
    changes = import_rule_versions(V1, user=user)

    versions = {version.code: version for version in db.session.scalars(select(RuleVersion))}
    assert changes == [
        {"code": code, "old_version": None, "new_version": 1, "old_rule_version_id": None, "new_rule_version_id": versions[code].id}
        for code in ("D1", "D2", "D3", "D4", "D5", "D6")
    ]
    definitions = {defn["code"]: defn for defn in V1["rules"]}
    for code, version in versions.items():
        defn = definitions[code]
        assert version.definition == defn, "the definition is stored verbatim"
        assert (version.code, version.version, version.genes) == (code, 1, defn["genetic"]["genes"])
        assert (version.title, version.recommendation, version.source) == (defn["title"], defn["recommendation"], defn["source"])
        assert (version.severity, version.kind, version.change_note) == (defn["severity"], defn["kind"], defn["change_note"])
        assert (version.is_current, version.rule_set_label, version.imported_by) == (True, "v1 (2025)", user)
        assert (version.rule.code, version.rule.title) == (code, defn["title"])
    entry = db.session.execute(select(AuditLog).filter_by(action="rules_imported")).scalar_one()
    assert (entry.user_id, entry.entity_type) == (user.id, "rule_set")
    assert entry.detail["label"] == "v1 (2025)" and entry.detail["rule_set"] == "GE-EHR monogenic diabetes rules"
    assert entry.detail["created_versions"] == ["D1 v1", "D2 v1", "D3 v1", "D4 v1", "D5 v1", "D6 v1"]
    assert len(entry.detail["changes"]) == 6


def test_identical_reimport_is_a_no_op(db: SQLAlchemy) -> None:
    import_rule_versions(V1)
    reordered = copy.deepcopy(V1)
    reordered["rules"] = [dict(reversed(list(rule.items()))) for rule in reversed(reordered["rules"])]
    assert import_rule_versions(reordered) == []
    assert import_rule_versions(V1) == []
    assert db.session.scalar(select(func.count(RuleVersion.id))) == 6
    assert db.session.scalar(select(func.count(Rule.id))) == 6
    entries = db.session.scalars(select(AuditLog).filter_by(action="rules_imported").order_by(AuditLog.id)).all()
    assert [entry.detail["created_versions"] for entry in entries] == [
        ["D1 v1", "D2 v1", "D3 v1", "D4 v1", "D5 v1", "D6 v1"], [], []
    ]


def test_importing_v2_revises_d4_only(db: SQLAlchemy) -> None:
    import_rule_versions(V1)
    d4_v1 = db.session.execute(select(RuleVersion).filter_by(code="D4", version=1)).scalar_one()

    changes = import_rule_versions(V2)

    d4_v2 = db.session.execute(select(RuleVersion).filter_by(code="D4", version=2)).scalar_one()
    assert changes == [
        {"code": "D4", "old_version": 1, "new_version": 2, "old_rule_version_id": d4_v1.id, "new_rule_version_id": d4_v2.id}
    ]
    assert (d4_v1.is_current, d4_v2.is_current) == (False, True)
    assert d4_v2.genes == ["KCNJ11", "ABCC8"] and d4_v2.rule_set_label == "v2 (2026)"
    assert d4_v2.rule is d4_v1.rule and d4_v1.rule.title == d4_v2.title, "the rule's title follows the current version"
    assert [version.label for version in RuleVersion.current_versions()] == ["D1 v1", "D2 v1", "D3 v1", "D4 v2", "D5 v1", "D6 v1"]
    d1 = db.session.execute(select(RuleVersion).filter_by(code="D1")).scalar_one()
    assert d1.rule_set_label == "v1 (2025)", "unchanged versions keep their provenance"


def test_importing_an_older_version_keeps_the_highest_current(db: SQLAlchemy) -> None:
    import_rule_versions(V2)
    assert import_rule_versions(V1) == []
    d4_versions = db.session.scalars(select(RuleVersion).filter_by(code="D4").order_by(RuleVersion.version)).all()
    assert [(version.version, version.is_current) for version in d4_versions] == [(1, False), (2, True)]


def test_several_versions_of_a_new_rule_in_one_document(db: SQLAlchemy) -> None:
    changes = import_rule_versions(
        {"rules": [rule_definition("D7", 2, genes=["INS"], severity="medium"), rule_definition("D7", 1, genes=["INS"])]}
    )
    assert [(change["code"], change["old_version"], change["new_version"]) for change in changes] == [("D7", None, 2)]
    versions = db.session.scalars(select(RuleVersion).filter_by(code="D7").order_by(RuleVersion.version)).all()
    assert [(version.version, version.is_current, version.rule_set_label) for version in versions] == [(1, False, None), (2, True, None)]


def test_conflicting_redefinition_is_rejected(db: SQLAlchemy) -> None:
    import_rule_versions(V2)
    changed = copy.deepcopy(V1)
    next(rule for rule in changed["rules"] if rule["code"] == "D2")["recommendation"] = "Something else."
    changed["rules"].append(rule_definition("D8", genes=["INS"]))

    with pytest.raises(RuleDefinitionError) as excinfo:
        import_rule_versions(changed)

    assert str(excinfo.value) == (
        "Rule D2 v1 is already stored with a different definition. Rule versions are immutable: "
        "publish the change as a new version (e.g. D2 v2)."
    )
    assert not db.session.new and not db.session.dirty, "nothing is added to the session"
    assert db.session.execute(select(Rule).filter_by(code="D8")).scalar_one_or_none() is None
    with pytest.raises(RuleDefinitionError, match=r"^Rule D4 v2 is already stored with a different definition\. .*\(e\.g\. D4 v3\)\.$"):
        conflicting = copy.deepcopy(V2)
        conflicting["rules"][3]["change_note"] = "edited"
        import_rule_versions(conflicting)


def test_invalid_documents_are_rejected_before_any_write(db: SQLAlchemy) -> None:
    with pytest.raises(RuleDefinitionError, match=r"^Rule set: 'rules' must be a non-empty list"):
        import_rule_versions({"label": "empty", "rules": []})
    assert db.session.scalar(select(func.count(RuleVersion.id))) == 0
    assert db.session.scalar(select(func.count(AuditLog.id))) == 0


def test_stored_definition_is_independent_of_the_input(db: SQLAlchemy) -> None:
    doc = copy.deepcopy(V1)
    import_rule_versions(doc)
    doc["rules"][0]["genetic"]["genes"].append("INS")
    stored = db.session.execute(select(RuleVersion).filter_by(code="D1")).scalar_one()
    assert stored.definition["genetic"]["genes"] == ["GCK"]
