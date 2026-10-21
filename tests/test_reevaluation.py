"""Tests of the F5 re-evaluation engine (SPEC §7): Algorithm 2 change categories, first activation,
PatientVariant bookkeeping, rule revisions (§7.3), full recomputation, run bookkeeping (§7.4),
the point-in-time view (§7.5), failure handling (§7.6) and a randomised equivalence property:
change-driven and full re-evaluation always reach the ground-truth state of pure Algorithm 1.

Scenarios are built with the shared factories (not the demo seeder) on the demo variant keys of
SPEC §11.
"""

from __future__ import annotations

import copy
import logging
import random
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, date, timedelta
from typing import Any

import pytest
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import func, select
from sqlalchemy.orm import joinedload, selectinload

from ge_ehr import constants as C
from ge_ehr.extensions import db as database
from ge_ehr.models import (
    Alert,
    AuditLog,
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
from ge_ehr.services import alerting, fhir_export, reevaluation
from ge_ehr.services import patient_records as pr
from ge_ehr.services.alerting import import_rule_versions, load_current_rules
from ge_ehr.services.rules import (
    RULES_DATA_DIR,
    ClassifiedVariant,
    ClinicalContext,
    RuleDefinitionError,
    evaluate,
    load_rule_set_file,
)
from tests.factories import (
    add_known_variant,
    add_medication,
    add_patient_variant,
    make_alert,
    make_patient,
    make_release,
    rule_definition,
)

RULES_V1 = load_rule_set_file(RULES_DATA_DIR / "ruleset_v1.json")
RULES_V2 = load_rule_set_file(RULES_DATA_DIR / "ruleset_v2.json")

# Demo variant keys (SPEC §11).
V01 = ("12", 120978900, "G", "A")  # HNF1A
V02 = ("7", 44145200, "C", "T")  # GCK
V03 = ("20", 44401300, "C", "T")  # HNF4A
V04 = ("11", 17387100, "C", "T")  # KCNJ11
V05 = ("11", 2160500, "G", "A")  # INS
V06 = ("12", 120994300, "C", "T")  # HNF1A
V07 = ("7", 44150500, "G", "A")  # GCK
V08 = ("12", 120999100, "A", "G")  # HNF1A
V09 = ("11", 17410300, "G", "A")  # ABCC8
V10 = ("20", 44410200, "G", "T")  # HNF4A
V11 = ("7", 44160100, "T", "C")  # GCK
B01 = ("12", 121001000, "C", "T")  # HNF1A
B03 = ("17", 37700000, "G", "A")  # HNF1B

Key = tuple[str, int, str, str]
NEONATAL_DIABETES = "Permanent neonatal diabetes mellitus"


def label(key: Key) -> str:
    return "-".join(str(part) for part in key)


def release(name: str, classes: Mapping[Key, str | tuple[str, str]]) -> KbRelease:
    """A non-current release; values are a class or ``(class, mechanism)``."""
    kb_release = make_release(name)
    for key, spec in classes.items():
        cls, mechanism = (spec, "unknown") if isinstance(spec, str) else spec
        condition = NEONATAL_DIABETES if mechanism == "activating" else None
        add_known_variant(kb_release, *key, cls, mechanism=mechanism, condition=condition)
    return kb_release


def carrier(
    *keys: Key,
    diabetes_type: str = "type1",
    age_at_diagnosis_months: int | None = 180,
    drugs: Iterable[str] = ("insulin",),
    family_id: str | None = None,
) -> Patient:
    """A patient carrying ``keys`` (stored unclassified, as if ingested before any release)."""
    patient = make_patient(
        diagnosis={"diabetes_type": diabetes_type, "age_at_diagnosis_months": age_at_diagnosis_months},
        family_id=family_id,
    )
    for drug_class in drugs:
        add_medication(patient, f"Test {drug_class}", drug_class)
    for key in keys:
        add_patient_variant(patient, *key)
    return patient


def alerts_of(patient: Patient) -> list[Alert]:
    return list(database.session.scalars(select(Alert).where(Alert.patient_id == patient.id).order_by(Alert.id)))


def current(patient: Patient) -> dict[tuple[str, str, str | None], Alert]:
    """The patient's current alerts and notes keyed by (rule code, kind, severity)."""
    return {(alert.rule_code, alert.kind, alert.severity): alert for alert in alerts_of(patient) if alert.is_current}


def findings(patient: Patient) -> set[tuple[str, str, str | None]]:
    return set(current(patient))


def variant_of(patient: Patient, key: Key) -> PatientVariant:
    statement = (
        select(PatientVariant)
        .join(VariantKey)
        .where(PatientVariant.patient_id == patient.id, VariantKey.chrom == key[0], VariantKey.pos == key[1])
    )
    return database.session.scalars(statement).one()


def notices_of(patient: Patient) -> list[ReviewNotice]:
    statement = select(ReviewNotice).where(ReviewNotice.patient_id == patient.id).order_by(ReviewNotice.id)
    return list(database.session.scalars(statement))


def run_count() -> int:
    return database.session.scalar(select(func.count(ReevaluationRun.id))) or 0


@pytest.fixture
def rules_v1(db: SQLAlchemy) -> None:
    import_rule_versions(RULES_V1)
    db.session.flush()


# ---------------------------------------------------------------------------
# First activation (SPEC §7.1 step 5)
# ---------------------------------------------------------------------------


def test_first_activation_is_an_initial_classification(db: SQLAlchemy, rules_v1: None, admin_user: User) -> None:
    r1 = release("R1", {V01: "P", B01: "B", V11: "VUS"})
    on_insulin = carrier(V01, B01)
    on_sulfonylurea = carrier(V01, diabetes_type="type2", age_at_diagnosis_months=300, drugs=["sulfonylurea"])
    unlisted = carrier(B03)
    db.session.commit()

    run = reevaluation.activate_release(r1, user=admin_user)

    assert (run.trigger, run.strategy, run.status, run.error) == ("clinvar_release", "change_driven", "success", None)
    assert (run.old_release_id, run.new_release_id, run.initiated_by) == (None, r1.id, admin_user)
    assert (run.stored_keys_compared, run.changed_variants, run.carriers_affected) == (3, 2, 2)
    assert (run.patients_reevaluated, run.records_examined) == (2, 3)
    assert (run.alerts_created, run.notes_created, run.alerts_superseded, run.alerts_confirmed) == (3, 1, 0, 0)
    assert (run.review_notices, run.notifications) == (0, 3)
    assert run.summary["initial"] is True and run.summary["old_release"] is None

    assert findings(on_insulin) == {("D2", "alert", "high"), ("D5", "alert", "low")}
    assert findings(on_sulfonylurea) == {("D2", "note", None), ("D5", "alert", "low")}
    assert findings(unlisted) == set()
    for alert in alerts_of(on_insulin) + alerts_of(on_sulfonylurea):
        assert (alert.is_reclassified, alert.priority, alert.trigger) == (False, "normal", "clinvar_release")
        assert alert.notify is (alert.kind == "alert")
        assert (alert.created_release_id, alert.confirmed_release_id, alert.created_run_id) == (r1.id, r1.id, run.id)

    for patient, key, cls in ((on_insulin, V01, "P"), (on_insulin, B01, "B"), (unlisted, B03, "NC")):
        variant = variant_of(patient, key)
        assert (variant.reported_classification, variant.current_classification) == (cls, cls)
        assert variant.classification_release_id == r1.id
        assert (variant.reclassified_count, variant.last_reclassified_run_id, variant.is_amended) == (0, None, False)

    changes = {change.variant_key.label: change for change in run.changes}
    assert set(changes) == {label(V01), label(B01)}, "unchanged (NC) and unstored keys are not changes"
    assert (changes[label(V01)].old_classification, changes[label(V01)].new_classification) == ("NC", "P")
    assert (changes[label(V01)].category, changes[label(V01)].carrier_count) == ("upgrade", 2)
    assert (changes[label(B01)].category, changes[label(B01)].carrier_count) == ("within_non_actionable", 1)
    assert db.session.scalar(select(func.count(ReviewNotice.id))) == 0

    assert (r1.is_current, r1.sequence, r1.activated_at) == (True, 1, run.started_at)
    assert KbRelease.current() is r1


def test_first_activation_with_full_strategy(db: SQLAlchemy, rules_v1: None) -> None:
    r1 = release("R1", {V01: "P"})
    patient = carrier(V01, B03)
    without_variants = make_patient()
    db.session.commit()

    run = reevaluation.activate_release(r1, strategy="full")

    assert (run.strategy, run.patients_reevaluated, run.records_examined, run.carriers_affected) == ("full", 2, 2, 1)
    assert run.summary["initial"] is True
    assert findings(patient) == {("D2", "alert", "high"), ("D5", "alert", "low")}
    assert not any(alert.is_reclassified for alert in alerts_of(patient))
    assert findings(without_variants) == set()
    for key, cls in ((V01, "P"), (B03, "NC")):
        variant = variant_of(patient, key)
        assert (variant.reported_classification, variant.current_classification, variant.reclassified_count) == (
            cls,
            cls,
            0,
        )
        assert variant.classification_release_id == r1.id


# ---------------------------------------------------------------------------
# Change categories (Algorithm 2, paper Table IV)
# ---------------------------------------------------------------------------


def test_upgrade_vus_to_lp_raises_reclassified_high_priority_alerts(db: SQLAlchemy, rules_v1: None) -> None:
    """RS1: HNF1A VUS → LP for a patient on insulin."""
    r1, r2 = release("R1", {V06: "VUS"}), release("R2", {V06: "LP"})
    patient = carrier(V06, diabetes_type="type1", age_at_diagnosis_months=192)
    db.session.commit()
    reevaluation.activate_release(r1)
    (d6,) = current(patient).values()
    assert (d6.rule_code, d6.severity, d6.notify) == ("D6", "info", False)

    run = reevaluation.activate_release(r2)

    assert findings(patient) == {("D2", "alert", "high"), ("D5", "alert", "low")}
    for alert in current(patient).values():
        assert (alert.is_reclassified, alert.priority, alert.notify) == (True, "high", True)
        assert (alert.classification, alert.created_release_id, alert.created_run_id) == ("LP", r2.id, run.id)
    assert (d6.status, d6.superseded_reason, d6.superseded_run_id, d6.review_notice_id) == (
        "superseded",
        "reclassified",
        run.id,
        None,
    )
    assert notices_of(patient) == []
    assert (run.changed_variants, run.carriers_affected, run.records_examined) == (1, 1, 1)
    assert (run.alerts_created, run.alerts_superseded, run.review_notices, run.notifications) == (2, 1, 0, 2)
    assert run.summary["categories"]["upgrade"] == {
        "changed_variants": 1,
        "carriers": 1,
        "alerts_created": 2,
        "notes_created": 0,
        "alerts_superseded": 1,
        "alerts_confirmed": 0,
        "review_notices": 0,
        "notifications": 2,
    }
    assert run.summary["superseded_by_reason"] == {"reclassified": 1}

    variant = variant_of(patient, V06)
    assert (variant.reported_classification, variant.current_classification) == ("VUS", "LP")
    assert (variant.reclassified_count, variant.last_reclassified_run_id) == (1, run.id)
    assert variant.last_reclassified_at == run.started_at and variant.classification_release_id == r2.id
    assert variant.is_amended


def test_upgrade_of_a_variant_newly_added_to_clinvar(db: SQLAlchemy, rules_v1: None) -> None:
    """Absent from R1 (not classified, NC) → listed LP in R2: an upgrade."""
    r1, r2 = release("R1", {V01: "P"}), release("R2", {V01: "P", V05: "LP"})
    patient = carrier(V05, diabetes_type="type1", age_at_diagnosis_months=96)
    db.session.commit()
    first = reevaluation.activate_release(r1)
    assert first.records_examined == 0 and findings(patient) == set()
    assert variant_of(patient, V05).classification_release_id == r1.id

    run = reevaluation.activate_release(r2)

    (change,) = run.changes
    assert (change.old_classification, change.new_classification, change.category) == ("NC", "LP", "upgrade")
    (d5,) = current(patient).values()
    assert (d5.rule_code, d5.is_reclassified, d5.priority, d5.notify) == ("D5", True, "high", True)
    variant = variant_of(patient, V05)
    assert (variant.reported_classification, variant.current_classification, variant.reclassified_count) == (
        "NC",
        "LP",
        1,
    )


def test_upgrade_creates_reclassified_note_without_notification(db: SQLAlchemy, rules_v1: None) -> None:
    r1, r2 = release("R1", {V06: "VUS"}), release("R2", {V06: "LP"})
    patient = carrier(V06, diabetes_type="type2", age_at_diagnosis_months=300, drugs=["sulfonylurea"])
    db.session.commit()
    reevaluation.activate_release(r1)

    run = reevaluation.activate_release(r2)

    note, d5 = current(patient)[("D2", "note", None)], current(patient)[("D5", "alert", "low")]
    assert (note.is_reclassified, note.priority, note.notify) == (True, "normal", False)
    assert "already receiving a sulfonylurea" in note.message
    assert (d5.priority, d5.notify) == ("high", True)
    assert (run.alerts_created, run.notes_created, run.notifications) == (1, 1, 1)


def test_downgrade_supersedes_alerts_with_one_review_notice(db: SQLAlchemy, rules_v1: None) -> None:
    """RS2: GCK LP → VUS for a patient on metformin; FHIR reports the variant as amended."""
    r1, r2 = release("R1", {V07: "LP"}), release("R2", {V07: "VUS"})
    patient = carrier(V07, diabetes_type="type2", age_at_diagnosis_months=420, drugs=["metformin"])
    db.session.commit()
    reevaluation.activate_release(r1)
    old = current(patient)
    assert set(old) == {("D1", "alert", "medium"), ("D5", "alert", "low")}

    run = reevaluation.activate_release(r2)

    (notice,) = notices_of(patient)
    assert notice.message == (
        "GCK variant 7-44150500-G-A was downgraded from LP to VUS in R2; alert(s) D1, D5 superseded. "
        "Review any treatment change made on the basis of this alert; GE-EHR never reverses therapy automatically."
    )
    assert (notice.old_classification, notice.new_classification, notice.status, notice.run_id) == (
        "LP",
        "VUS",
        "open",
        run.id,
    )
    assert notice.variant_key.label == label(V07)
    assert sorted(alert.rule_code for alert in notice.alerts) == ["D1", "D5"]
    for alert in old.values():
        assert (alert.status, alert.superseded_reason, alert.review_notice_id) == ("superseded", "downgrade", notice.id)
        assert alert.superseded_at == run.started_at
        assert [event.event for event in alert.events] == ["created", "superseded"]
    (d6,) = current(patient).values()
    assert (d6.rule_code, d6.notify, d6.is_reclassified, d6.priority) == ("D6", False, False, "normal")
    assert (run.alerts_created, run.alerts_superseded, run.review_notices, run.notifications) == (1, 2, 1, 1)
    assert run.summary["superseded_by_reason"] == {"downgrade": 2}

    variant = variant_of(patient, V07)
    assert (variant.reported_classification, variant.current_classification, variant.is_amended) == (
        "LP",
        "VUS",
        True,
    )
    bundle = fhir_export.build_patient_bundle(patient)
    resources = [entry["resource"] for entry in bundle["entry"]]
    statuses = {resource["status"] for resource in resources if resource["resourceType"] == "Observation"}
    assert statuses == {"amended"}


def test_downgrade_of_a_variant_removed_from_clinvar(db: SQLAlchemy, rules_v1: None) -> None:
    """LP → absent from the new release: not classified (NC), a downgrade with reason variant_removed."""
    r1, r2 = release("R1", {V07: "LP", V02: "P"}), release("R2", {V02: "P"})
    patient = carrier(V07, diabetes_type="type2", age_at_diagnosis_months=420, drugs=["metformin"])
    db.session.commit()
    reevaluation.activate_release(r1)
    old = list(current(patient).values())

    run = reevaluation.activate_release(r2)

    assert findings(patient) == set(), "a not-classified variant triggers no rule (NC is not VUS)"
    assert {(alert.superseded_reason, alert.status) for alert in old} == {("variant_removed", "superseded")}
    (notice,) = notices_of(patient)
    assert notice.message.startswith(
        "GCK variant 7-44150500-G-A (LP) is no longer listed in R2 and is now not classified (NC); "
        "alert(s) D1, D5 superseded."
    )
    assert (notice.old_classification, notice.new_classification) == ("LP", "NC")
    (change,) = run.changes
    assert (change.old_classification, change.new_classification, change.category) == ("LP", "NC", "downgrade")
    assert run.summary["changes"][0]["removed"] is True
    assert run.summary["superseded_by_reason"] == {"variant_removed": 2}
    variant = variant_of(patient, V07)
    assert (variant.current_classification, variant.reported_classification, variant.reclassified_count) == (
        "NC",
        "LP",
        1,
    )


def test_downgrade_withdrawing_only_info_alerts_raises_no_notice(db: SQLAlchemy) -> None:
    import_rule_versions(
        {
            "rule_set": "Test rules",
            "label": "info only",
            "rules": [rule_definition("D7", genes=["INS"], severity="info", kind="monitoring")],
        }
    )
    r1, r2 = release("R1", {V05: "LP"}), release("R2", {V05: "VUS"})
    patient = carrier(V05)
    db.session.commit()
    reevaluation.activate_release(r1)
    (d7,) = current(patient).values()

    run = reevaluation.activate_release(r2)

    assert (d7.status, d7.superseded_reason) == ("superseded", "downgrade")
    assert notices_of(patient) == [] and (run.review_notices, run.notifications) == (0, 0)


def test_within_actionable_change_updates_provenance_only(db: SQLAlchemy, rules_v1: None) -> None:
    """RS3: HNF4A LP → P; a reviewed alert stays reviewed."""
    r1, r2 = release("R1", {V03: "LP"}), release("R2", {V03: "P"})
    patient = carrier(V03, diabetes_type="type2", age_at_diagnosis_months=264, drugs=["metformin"])
    db.session.commit()
    reevaluation.activate_release(r1)
    alerts = current(patient)
    assert set(alerts) == {("D3", "alert", "medium"), ("D5", "alert", "low")}
    alerts[("D3", "alert", "medium")].status = "reviewed"
    db.session.commit()

    run = reevaluation.activate_release(r2)

    assert current(patient) == alerts
    for alert in alerts.values():
        assert (alert.created_release_id, alert.confirmed_release_id, alert.classification) == (r1.id, r2.id, "LP")
        confirmed = alert.events[-1]
        assert (confirmed.event, confirmed.release_id, confirmed.run_id, confirmed.classification) == (
            "confirmed",
            r2.id,
            run.id,
            "P",
        )
    assert alerts[("D3", "alert", "medium")].status == "reviewed"
    assert (run.alerts_created, run.alerts_superseded, run.alerts_confirmed, run.notifications) == (0, 0, 2, 0)
    assert run.summary["categories"]["within_actionable"]["alerts_confirmed"] == 2
    variant = variant_of(patient, V03)
    assert (variant.current_classification, variant.reclassified_count) == ("P", 1)


def test_within_non_actionable_supersedes_monitoring_alert_silently(db: SQLAlchemy, rules_v1: None) -> None:
    """RS4: HNF1A VUS → LB; D6 superseded, no notice and no notification."""
    r1, r2 = release("R1", {V08: "VUS"}), release("R2", {V08: "LB"})
    patient = carrier(V08, diabetes_type="type1", age_at_diagnosis_months=150)
    db.session.commit()
    reevaluation.activate_release(r1)
    (d6,) = current(patient).values()

    run = reevaluation.activate_release(r2)

    assert (d6.status, d6.superseded_reason, d6.review_notice_id) == ("superseded", "reclassified", None)
    assert findings(patient) == set() and notices_of(patient) == []
    assert (run.alerts_superseded, run.review_notices, run.notifications) == (1, 0, 0)
    assert run.changes[0].category == "within_non_actionable"


def test_within_non_actionable_change_without_findings(db: SQLAlchemy, rules_v1: None) -> None:
    r1, r2 = release("R1", {B01: "LB"}), release("R2", {B01: "B"})
    patient = carrier(B01)
    db.session.commit()
    reevaluation.activate_release(r1)

    run = reevaluation.activate_release(r2)

    assert (run.changed_variants, run.patients_reevaluated, run.records_examined) == (1, 1, 1)
    assert (run.alerts_created, run.alerts_superseded, run.alerts_confirmed) == (0, 0, 0)
    assert findings(patient) == set()
    assert variant_of(patient, B01).reclassified_count == 1


def test_mechanism_only_change_creates_an_alert_that_notifies(db: SQLAlchemy, rules_v1: None) -> None:
    r1 = release("R1", {V04: ("P", "unknown")})
    r2 = release("R2", {V04: ("P", "activating")})
    patient = carrier(V04, diabetes_type="neonatal", age_at_diagnosis_months=2)
    db.session.commit()
    reevaluation.activate_release(r1)
    assert findings(patient) == {("D5", "alert", "low")}, "D4 requires an activating variant"

    run = reevaluation.activate_release(r2)

    d4 = current(patient)[("D4", "alert", "high")]
    assert (d4.notify, d4.is_reclassified, d4.priority) == (True, False, "normal")
    assert current(patient)[("D5", "alert", "low")].confirmed_release_id == r2.id
    (change,) = run.changes
    assert (change.old_classification, change.new_classification) == ("P", "P")
    assert (change.old_mechanism, change.new_mechanism) == ("unknown", "activating")
    assert change.category == "within_actionable"
    assert (run.alerts_created, run.alerts_confirmed, run.notifications) == (1, 1, 1)
    variant = variant_of(patient, V04)
    assert (variant.reclassified_count, variant.is_amended, variant.classification_release_id) == (0, False, r2.id)


def test_mechanism_loss_supersedes_silently(db: SQLAlchemy, rules_v1: None) -> None:
    r1 = release("R1", {V04: ("P", "activating")})
    r2 = release("R2", {V04: ("P", "unknown")})
    patient = carrier(V04, diabetes_type="neonatal", age_at_diagnosis_months=2)
    db.session.commit()
    reevaluation.activate_release(r1)
    d4 = current(patient)[("D4", "alert", "high")]

    run = reevaluation.activate_release(r2)

    assert (d4.status, d4.superseded_reason) == ("superseded", "reclassified")
    assert notices_of(patient) == [] and run.notifications == 0
    assert findings(patient) == {("D5", "alert", "low")}


def test_every_carrier_of_a_shared_family_variant_is_reevaluated(db: SQLAlchemy, rules_v1: None) -> None:
    """RS6: HNF4A VUS → LP shared by a mother and two children."""
    r1, r2 = release("R1", {V10: "VUS"}), release("R2", {V10: "LP"})
    mother = carrier(V10, diabetes_type="type2", age_at_diagnosis_months=324, drugs=["metformin"], family_id="FAM-RS6")
    child = carrier(V10, diabetes_type="type1", age_at_diagnosis_months=168, family_id="FAM-RS6")
    healthy = carrier(V10, diabetes_type="none", age_at_diagnosis_months=None, drugs=(), family_id="FAM-RS6")
    db.session.commit()
    reevaluation.activate_release(r1)
    assert all(findings(patient) == {("D6", "alert", "info")} for patient in (mother, child, healthy))

    run = reevaluation.activate_release(r2)

    assert findings(mother) == findings(child) == {("D3", "alert", "medium"), ("D5", "alert", "low")}
    assert findings(healthy) == {("D3", "note", None), ("D5", "alert", "low")}
    assert (run.carriers_affected, run.patients_reevaluated, run.records_examined) == (3, 3, 3)
    assert (run.alerts_created, run.notes_created, run.alerts_superseded, run.notifications) == (5, 1, 3, 5)
    assert run.changes[0].carrier_count == 3
    assert run.summary["genes"] == {
        "HNF4A": {
            "changed_variants": 1,
            "carriers": 3,
            "categories": {"upgrade": 1, "downgrade": 0, "within_actionable": 0, "within_non_actionable": 0},
        }
    }


def test_keys_carried_by_nobody_are_never_examined(db: SQLAlchemy, rules_v1: None) -> None:
    """RS7: V11 changes VUS → P but no patient carries it."""
    r1 = release("R1", {V01: "P", V06: "VUS", V11: "VUS"})
    r2 = release("R2", {V01: "P", V06: "LP", V11: "P"})
    unchanged = carrier(V01)
    upgraded = carrier(V06)
    db.session.commit()
    reevaluation.activate_release(r1)
    before = {alert.id: alert.confirmed_release_id for alert in alerts_of(unchanged)}

    run = reevaluation.activate_release(r2)

    assert (run.stored_keys_compared, run.changed_variants) == (2, 1)
    assert (run.carriers_affected, run.patients_reevaluated, run.records_examined) == (1, 1, 1)
    assert [change.variant_key.label for change in run.changes] == [label(V06)]
    assert [change["label"] for change in run.summary["changes"]] == [label(V06)]
    assert {alert.id: alert.confirmed_release_id for alert in alerts_of(unchanged)} == before
    assert findings(upgraded) == {("D2", "alert", "high"), ("D5", "alert", "low")}
    assert variant_of(unchanged, V01).classification_release_id == r2.id, "unchanged class is backed by R2 too"


def test_identical_release_is_a_no_op_activation(db: SQLAlchemy, rules_v1: None) -> None:
    r1, r2 = release("R1", {V01: "P"}), release("R2", {V01: "P"})
    patient = carrier(V01)
    db.session.commit()
    reevaluation.activate_release(r1)

    run = reevaluation.activate_release(r2)

    assert (run.changed_variants, run.patients_reevaluated, run.records_examined, run.notifications) == (0, 0, 0, 0)
    assert run.changes == [] and run.summary["changes"] == [] and run.summary["initial"] is False
    assert all(counts["changed_variants"] == 0 for counts in run.summary["categories"].values())
    variant = variant_of(patient, V01)
    assert (variant.classification_release_id, variant.reclassified_count) == (r2.id, 0)
    assert (r1.is_current, r2.is_current, r2.sequence) == (False, True, 2)


def test_full_strategy_examines_every_record_with_the_same_semantics(db: SQLAlchemy, rules_v1: None) -> None:
    r1 = release("R1", {V01: "P", V06: "VUS", V07: "LP"})
    r2 = release("R2", {V01: "P", V06: "LP", V07: "VUS"})
    unchanged = carrier(V01, B01)
    upgraded = carrier(V06)
    downgraded = carrier(V07, diabetes_type="type2", age_at_diagnosis_months=420, drugs=["metformin"])
    make_patient()
    db.session.commit()
    reevaluation.activate_release(r1)

    run = reevaluation.activate_release(r2, strategy="full")

    assert (run.strategy, run.stored_keys_compared, run.changed_variants, run.carriers_affected) == ("full", 4, 2, 2)
    assert (run.patients_reevaluated, run.records_examined) == (4, 4)
    assert (run.alerts_created, run.alerts_superseded, run.review_notices, run.notifications) == (3, 3, 1, 3)
    assert run.alerts_confirmed == 2, "the unchanged patient's alerts are re-confirmed under R2"
    assert {alert.confirmed_release_id for alert in current(unchanged).values()} == {r2.id}
    assert all(alert.is_reclassified and alert.priority == "high" for alert in current(upgraded).values())
    assert findings(downgraded) == {("D6", "alert", "info")} and len(notices_of(downgraded)) == 1
    assert {change.variant_key.label: change.category for change in run.changes} == {
        label(V06): "upgrade",
        label(V07): "downgrade",
    }
    assert variant_of(upgraded, V06).reclassified_count == 1
    assert variant_of(unchanged, V01).reclassified_count == 0


# ---------------------------------------------------------------------------
# Activation rules, run bookkeeping, failure handling
# ---------------------------------------------------------------------------


def test_reactivating_the_current_release_is_rejected(db: SQLAlchemy, rules_v1: None) -> None:
    r1 = release("R1", {V01: "P"})
    reevaluation.activate_release(r1)
    runs = run_count()
    with pytest.raises(ValueError, match="Release 'R1' is already the current release; activate a different release."):
        reevaluation.activate_release(r1)
    assert run_count() == runs and r1.sequence == 1


def test_activating_an_older_release_is_a_new_activation(db: SQLAlchemy, rules_v1: None) -> None:
    r1, r2 = release("R1", {V06: "VUS"}), release("R2", {V06: "LP"})
    make_release("R3, imported only")
    patient = carrier(V06)
    db.session.commit()
    reevaluation.activate_release(r1)
    reevaluation.activate_release(r2)
    assert reevaluation.release_history() == [r1, r2]

    run = reevaluation.activate_release(r1)

    assert (run.old_release_id, run.new_release_id) == (r2.id, r1.id)
    assert (r1.is_current, r1.sequence, r2.is_current, r2.sequence) == (True, 3, False, 2)
    assert reevaluation.release_history() == [r2, r1]
    assert run.changes[0].category == "downgrade"
    assert findings(patient) == {("D6", "alert", "info")}
    variant = variant_of(patient, V06)
    assert (variant.reported_classification, variant.current_classification, variant.reclassified_count) == (
        "VUS",
        "VUS",
        2,
    )


def test_activate_release_rejects_invalid_input(db: SQLAlchemy, rules_v1: None) -> None:
    r1 = release("R1", {V01: "P"})
    with pytest.raises(ValueError, match="Unknown re-evaluation strategy 'fast'; expected one of change_driven, full."):
        reevaluation.activate_release(r1, strategy="fast")
    with pytest.raises(ValueError, match="Release 'Draft' is not stored yet; import it before activating it."):
        reevaluation.activate_release(KbRelease(label="Draft"))
    assert run_count() == 0 and not r1.is_current


def test_run_bookkeeping_and_audit(db: SQLAlchemy, rules_v1: None, admin_user: User) -> None:
    r1, r2 = release("R1", {V06: "VUS", V07: "LP"}), release("R2", {V06: "LP", V07: "VUS"})
    carrier(V06)
    carrier(V07, drugs=["metformin"])
    db.session.commit()
    reevaluation.activate_release(r1, user=admin_user)

    run = reevaluation.activate_release(r2, user=admin_user)

    assert run.status == "success" and run.finished_at is not None and run.finished_at >= run.started_at
    assert run.duration_ms is not None and run.duration_ms > 0
    summary = run.summary
    assert (summary["old_release"], summary["new_release"], summary["changes_total"]) == ("R1", "R2", 2)
    assert summary["changes_truncated"] is False
    assert summary["categories"]["downgrade"]["review_notices"] == 1
    assert summary["categories"]["downgrade"]["notifications"] == 1
    assert summary["categories"]["upgrade"]["notifications"] == 2
    assert run.notifications == 3 and run.review_notices == 1
    assert list(summary["genes"]) == ["GCK", "HNF1A"], "genes in panel order"
    assert summary["changes"][0] == {
        "variant_key_id": run.changes[0].variant_key_id,
        "label": run.changes[0].variant_key.label,
        "gene": run.changes[0].gene,
        "old_classification": run.changes[0].old_classification,
        "new_classification": run.changes[0].new_classification,
        "old_mechanism": "unknown",
        "new_mechanism": "unknown",
        "category": run.changes[0].category,
        "removed": False,
        "carriers": 1,
    }

    (entry,) = db.session.scalars(
        select(AuditLog).where(AuditLog.entity_id == run.id, AuditLog.action == "reevaluation_run")
    ).all()
    assert entry.username == admin_user.username and entry.entity_type == "reevaluation_run"
    assert entry.detail["status"] == "success" and entry.detail["changed_variants"] == 2
    assert entry.detail["notifications"] == 3 and entry.detail["new_release_id"] == r2.id
    (activated,) = db.session.scalars(
        select(AuditLog).where(AuditLog.action == "release_activated", AuditLog.entity_id == r2.id)
    ).all()
    assert activated.detail == {
        "label": "R2",
        "sequence": 2,
        "previous_release_id": r1.id,
        "previous_release": "R1",
        "strategy": "change_driven",
        "run_id": run.id,
        "initial": False,
    }


def test_summary_lists_a_bounded_number_of_changes(
    db: SQLAlchemy, rules_v1: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(reevaluation, "MAX_SUMMARY_CHANGES", 1)
    r1 = release("R1", {V01: "P", V06: "VUS"})
    carrier(V01, V06)
    run = reevaluation.activate_release(r1)
    assert (run.summary["changes_total"], run.summary["changes_truncated"], len(run.summary["changes"])) == (2, True, 1)
    assert len(run.changes) == 2, "every change is stored as a VariantChange row"


def _fail_reconciliation(monkeypatch: pytest.MonkeyPatch, message: str = "database exploded") -> None:
    def explode(*_args: Any, **_kwargs: Any) -> None:
        raise RuntimeError(message)

    monkeypatch.setattr(alerting, "reconcile_patient", explode)


def test_activation_failure_rolls_back_and_records_a_failed_run(
    db: SQLAlchemy, rules_v1: None, admin_user: User, monkeypatch: pytest.MonkeyPatch
) -> None:
    r1, r2 = release("R1", {V06: "VUS"}), release("R2", {V06: "LP"})
    patient = carrier(V06)
    db.session.commit()
    reevaluation.activate_release(r1)
    db.session.commit()
    alerts_before = [(alert.id, alert.status) for alert in alerts_of(patient)]
    _fail_reconciliation(monkeypatch)

    with pytest.raises(RuntimeError, match="database exploded"):
        reevaluation.activate_release(r2, user=admin_user)

    db.session.rollback()  # whatever the caller does next, the failed run is already committed
    failed = db.session.scalars(select(ReevaluationRun).where(ReevaluationRun.status == "failed")).one()
    assert failed.error == "RuntimeError: database exploded"
    assert (failed.trigger, failed.strategy, failed.old_release_id, failed.new_release_id) == (
        "clinvar_release",
        "change_driven",
        r1.id,
        r2.id,
    )
    assert failed.initiated_by_id == admin_user.id and failed.finished_at is not None and failed.duration_ms >= 0
    assert db.session.get(KbRelease, r2.id).is_current is False and db.session.get(KbRelease, r2.id).sequence is None
    assert KbRelease.current().id == r1.id
    assert db.session.scalar(select(func.count(VariantChange.id)).where(VariantChange.run_id == failed.id)) == 0
    assert [(alert.id, alert.status) for alert in alerts_of(patient)] == alerts_before
    assert variant_of(patient, V06).current_classification == "VUS"
    (entry,) = db.session.scalars(
        select(AuditLog).where(AuditLog.entity_id == failed.id, AuditLog.action == "reevaluation_run")
    )
    assert entry.detail["status"] == "failed" and entry.detail["error"] == "RuntimeError: database exploded"
    assert entry.username == admin_user.username


def test_failure_of_a_release_imported_in_the_same_transaction(
    db: SQLAlchemy, rules_v1: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    carrier(V01)
    db.session.commit()
    pending = release("Uncommitted", {V01: "P"})
    _fail_reconciliation(monkeypatch, "")

    with pytest.raises(RuntimeError):
        reevaluation.activate_release(pending)

    failed = db.session.scalars(select(ReevaluationRun)).one()
    assert (failed.status, failed.error, failed.new_release_id) == ("failed", "RuntimeError", None)
    assert db.session.scalar(select(func.count(KbRelease.id))) == 0, "the import was rolled back with the run"


def test_failure_recording_problems_never_mask_the_original_error(
    db: SQLAlchemy, rules_v1: None, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    r1 = release("R1", {V01: "P"})
    carrier(V01)
    db.session.commit()
    _fail_reconciliation(monkeypatch)

    def broken_audit(*_args: Any, **_kwargs: Any) -> None:
        raise OSError("audit store unavailable")

    monkeypatch.setattr(reevaluation, "audit", broken_audit)
    with caplog.at_level(logging.ERROR, logger="ge_ehr.services.reevaluation"), pytest.raises(RuntimeError):
        reevaluation.activate_release(r1)
    assert "Could not record the failed re-evaluation run" in caplog.text
    assert run_count() == 0


# ---------------------------------------------------------------------------
# Rule revision (SPEC §7.3)
# ---------------------------------------------------------------------------


@pytest.fixture
def katp_patients(db: SQLAlchemy, rules_v1: None) -> dict[str, Patient]:
    """RS5 setup under R1 + rules v1: a KCNJ11 and an ABCC8 neonatal-diabetes carrier on insulin."""
    r1 = release("R1", {V04: ("P", "activating"), V09: ("P", "activating"), V01: "P"})
    patients = {
        "kcnj11": carrier(V04, diabetes_type="neonatal", age_at_diagnosis_months=2),
        "abcc8": carrier(V09, diabetes_type="neonatal", age_at_diagnosis_months=3),
        "hnf1a": carrier(V01),
    }
    db.session.commit()
    reevaluation.activate_release(r1)
    db.session.commit()
    assert findings(patients["kcnj11"]) == {("D4", "alert", "high"), ("D5", "alert", "low")}
    assert findings(patients["abcc8"]) == {("D5", "alert", "low")}, "D4 v1 covers KCNJ11 only"
    return patients


def _version(code: str, number: int) -> RuleVersion:
    return database.session.scalars(select(RuleVersion).filter_by(code=code, version=number)).one()


def test_rule_revision_d4_v2_alerts_abcc8_carriers(
    db: SQLAlchemy, katp_patients: dict[str, Patient], admin_user: User
) -> None:
    """RS5: D4 v1 → v2 adds ABCC8; KCNJ11 alerts keep firing with updated provenance."""
    kcnj11_alert = current(katp_patients["kcnj11"])[("D4", "alert", "high")]

    def hnf1a_state() -> dict[int, tuple[str, int]]:
        alerts = alerts_of(katp_patients["hnf1a"])
        return {alert.id: (alert.status, alert.confirmed_rule_version_id) for alert in alerts}

    hnf1a_before = hnf1a_state()

    run = reevaluation.apply_rule_set(RULES_V2, user=admin_user)

    v1, v2 = _version("D4", 1), _version("D4", 2)
    assert (run.trigger, run.status, run.initiated_by) == ("rule_revision", "success", admin_user)
    assert run.rule_change == [{"code": "D4", "old_version": 1, "new_version": 2}]
    assert run.old_release_id == run.new_release_id == KbRelease.current().id
    new_alert = current(katp_patients["abcc8"])[("D4", "alert", "high")]
    assert (new_alert.notify, new_alert.trigger, new_alert.created_rule_version_id) == (True, "rule_revision", v2.id)
    assert (new_alert.is_reclassified, new_alert.created_run_id) == (False, run.id)
    assert kcnj11_alert.is_current and kcnj11_alert.notify is True
    assert (kcnj11_alert.created_rule_version_id, kcnj11_alert.confirmed_rule_version_id) == (v1.id, v2.id)
    confirmed = kcnj11_alert.events[-1]
    assert (confirmed.event, confirmed.rule_version_id, confirmed.run_id) == ("confirmed", v2.id, run.id)
    assert hnf1a_state() == hnf1a_before
    assert (run.carriers_affected, run.patients_reevaluated, run.records_examined) == (2, 2, 2)
    assert (run.alerts_created, run.alerts_confirmed, run.alerts_superseded) == (1, 1, 0)
    assert (run.review_notices, run.notifications, run.changed_variants) == (0, 1, 0)
    assert run.summary["rule_changes"] == [
        {"code": "D4", "old_version": 1, "new_version": 2, "genes": ["KCNJ11", "ABCC8"]}
    ]
    assert run.summary["rules"]["D4"]["alerts_created"] == 1 and run.summary["rules"]["D4"]["alerts_confirmed"] == 1
    assert run.summary["genes"] == {"KCNJ11": {"carriers": 1}, "ABCC8": {"carriers": 1}}
    assert v2.is_current and not v1.is_current


def test_rule_revision_full_strategy_reaches_the_same_state(db: SQLAlchemy, katp_patients: dict[str, Patient]) -> None:
    run = reevaluation.apply_rule_set(RULES_V2, strategy="full")
    assert (run.strategy, run.patients_reevaluated, run.records_examined, run.carriers_affected) == ("full", 3, 3, 2)
    assert (run.alerts_created, run.alerts_confirmed, run.alerts_superseded, run.notifications) == (1, 1, 0, 1)
    assert ("D4", "alert", "high") in findings(katp_patients["abcc8"])


def test_identical_rule_set_is_a_no_op_run(db: SQLAlchemy, katp_patients: dict[str, Patient]) -> None:
    run = reevaluation.apply_rule_set(copy.deepcopy(RULES_V1))
    assert (run.status, run.rule_change, run.changed_variants, run.patients_reevaluated) == ("success", [], 0, 0)
    assert (run.records_examined, run.alerts_created, run.notifications) == (0, 0, 0)
    assert run.summary["rule_changes"] == [] and run.summary["rules"] == {}


def _revised(code: str, **changes: Any) -> dict[str, Any]:
    """A one-rule document holding version 2 of a v1 rule with ``changes`` applied."""
    definition = copy.deepcopy(next(rule for rule in RULES_V1["rules"] if rule["code"] == code))
    definition.update(version=2, change_note="Test revision", **changes)
    return {"rule_set": RULES_V1["rule_set"], "label": f"{code} v2 (test)", "rules": [definition]}


def test_rule_severity_change_supersedes_with_a_review_notice(
    db: SQLAlchemy, katp_patients: dict[str, Patient]
) -> None:
    patient = katp_patients["hnf1a"]
    old = current(patient)[("D2", "alert", "high")]
    d5 = current(patient)[("D5", "alert", "low")]

    run = reevaluation.apply_rule_set(_revised("D2", severity="medium"))

    assert (old.status, old.superseded_reason) == ("superseded", "rule_revised")
    new = current(patient)[("D2", "alert", "medium")]
    assert new.notify is True and new.created_rule_version_id == _version("D2", 2).id
    (notice,) = notices_of(patient)
    assert notice.message == (
        "Rule revision D2 v1 → v2: alert(s) D2 (high) for HNF1A variant 12-120978900-G-A (P) superseded; "
        "now D2 (medium). Review any treatment change made on the basis of this alert; "
        "GE-EHR never reverses therapy automatically."
    )
    assert (notice.old_classification, notice.new_classification, notice.alerts) == ("P", "P", [old])
    assert d5.is_current and d5.events[-1].event == "created", "only the revised rule is re-run"
    assert (run.review_notices, run.notifications, run.alerts_created, run.alerts_superseded) == (1, 2, 1, 1)
    assert run.summary["superseded_by_reason"] == {"rule_revised": 1}


def test_rule_that_stops_firing_turns_the_alert_into_a_note(db: SQLAlchemy, rules_v1: None) -> None:
    r1 = release("R1", {V02: "P"})
    patient = carrier(V02, diabetes_type="type2", age_at_diagnosis_months=360, drugs=["metformin"])
    db.session.commit()
    reevaluation.activate_release(r1)
    clinical = {"any_of_drug_classes": ["insulin"], "none_of_drug_classes": None, "max_age_at_diagnosis_months": None}

    run = reevaluation.apply_rule_set(_revised("D1", clinical=clinical))

    assert findings(patient) == {("D1", "note", None), ("D5", "alert", "low")}
    (notice,) = notices_of(patient)
    assert "alert(s) D1 (medium)" in notice.message and "now D1 note" in notice.message
    assert (run.alerts_superseded, run.notes_created, run.review_notices, run.notifications) == (1, 1, 1, 1)


def test_withdrawn_info_rule_raises_no_notice(db: SQLAlchemy, rules_v1: None) -> None:
    r1 = release("R1", {V06: "VUS", V11: "VUS"})
    hnf1a = carrier(V06)
    gck = carrier(V11)
    db.session.commit()
    reevaluation.activate_release(r1)
    gck_d6 = current(gck)[("D6", "alert", "info")]

    genetic = {"genes": ["GCK", "HNF4A"], "classes": ["VUS"], "mechanism": None}
    run = reevaluation.apply_rule_set(_revised("D6", genetic=genetic))

    assert findings(hnf1a) == set() and notices_of(hnf1a) == []
    assert gck_d6.is_current and gck_d6.confirmed_rule_version_id == _version("D6", 2).id
    assert (run.alerts_superseded, run.alerts_confirmed, run.review_notices, run.notifications) == (1, 1, 0, 0)
    assert run.summary["genes"] == {"GCK": {"carriers": 1}, "HNF1A": {"carriers": 1}, "HNF4A": {"carriers": 0}}


def test_brand_new_rule_alerts_its_carriers(db: SQLAlchemy, katp_patients: dict[str, Patient]) -> None:
    doc = {
        "rule_set": "Test rules",
        "label": "D7 (test)",
        "rules": [rule_definition("D7", genes=["ABCC8"], severity="medium", kind="monitoring")],
    }
    run = reevaluation.apply_rule_set(doc)
    assert run.rule_change == [{"code": "D7", "old_version": None, "new_version": 1}]
    assert run.summary["rule_changes"][0]["old_version"] is None
    assert current(katp_patients["abcc8"])[("D7", "alert", "medium")].notify is True
    assert (run.patients_reevaluated, run.notifications) == (1, 1)


def test_invalid_rule_sets_are_rejected_without_a_run(db: SQLAlchemy, katp_patients: dict[str, Patient]) -> None:
    runs = run_count()
    with pytest.raises(RuleDefinitionError):
        reevaluation.apply_rule_set({"rule_set": "x", "label": "x", "rules": []})
    conflicting = copy.deepcopy(RULES_V1)
    conflicting["rules"][1]["severity"] = "low"
    with pytest.raises(RuleDefinitionError, match="already stored with a different definition"):
        reevaluation.apply_rule_set(conflicting)
    with pytest.raises(ValueError, match="Unknown re-evaluation strategy"):
        reevaluation.apply_rule_set(RULES_V2, strategy="patient")
    assert run_count() == runs


def test_rule_revision_failure_rolls_back_the_import(
    db: SQLAlchemy, katp_patients: dict[str, Patient], monkeypatch: pytest.MonkeyPatch
) -> None:
    _fail_reconciliation(monkeypatch)
    with pytest.raises(RuntimeError):
        reevaluation.apply_rule_set(RULES_V2)
    failed = db.session.scalars(select(ReevaluationRun).where(ReevaluationRun.status == "failed")).one()
    assert failed.trigger == "rule_revision"
    assert failed.rule_change == [{"code": "D4", "old_version": 1, "new_version": 2}]
    assert db.session.scalars(select(RuleVersion).filter_by(code="D4", version=2)).first() is None
    assert _version("D4", 1).is_current


# ---------------------------------------------------------------------------
# Full recomputation
# ---------------------------------------------------------------------------


def test_full_recompute_of_a_consistent_database_changes_nothing(
    db: SQLAlchemy, rules_v1: None, admin_user: User
) -> None:
    r1 = release("R1", {V01: "P", V06: "VUS"})
    patients = [carrier(V01), carrier(V06), make_patient()]
    db.session.commit()
    reevaluation.activate_release(r1)
    def state() -> dict[int, tuple[str, int | None]]:
        return {alert.id: (alert.status, alert.confirmed_release_id) for p in patients for alert in alerts_of(p)}

    before = state()

    run = reevaluation.full_recompute(user=admin_user)

    assert (run.trigger, run.strategy, run.status) == ("full_recompute", "full", "success")
    assert run.initiated_by == admin_user
    assert (run.old_release_id, run.new_release_id) == (r1.id, r1.id)
    assert (run.patients_reevaluated, run.records_examined, run.carriers_affected) == (3, 2, 0)
    assert (run.alerts_created, run.alerts_superseded, run.alerts_confirmed, run.notifications) == (0, 0, 0, 0)
    assert state() == before
    assert run.summary == {"release": "R1", "rules": {}, "superseded_by_reason": {}}


def test_full_recompute_refreshes_provenance_after_a_change_driven_activation(db: SQLAlchemy, rules_v1: None) -> None:
    r1, r2 = release("R1", {V01: "P", V06: "VUS"}), release("R2", {V01: "P", V06: "LP"})
    unchanged = carrier(V01)
    carrier(V06)
    db.session.commit()
    reevaluation.activate_release(r1)
    reevaluation.activate_release(r2)
    assert {alert.confirmed_release_id for alert in current(unchanged).values()} == {r1.id}

    run = reevaluation.full_recompute(trigger="manual")

    assert run.trigger == "manual"
    assert {alert.confirmed_release_id for alert in current(unchanged).values()} == {r2.id}
    assert (run.alerts_confirmed, run.alerts_created, run.alerts_superseded) == (2, 0, 0)
    assert run.summary["rules"]["D2"]["alerts_confirmed"] == 1


def test_full_recompute_repairs_drift_and_never_withdraws_advice_silently(db: SQLAlchemy, rules_v1: None) -> None:
    r1 = release("R1", {V01: "P", V03: "LP"})
    patient = carrier(V01)
    db.session.commit()
    reevaluation.activate_release(r1)
    d2 = current(patient)[("D2", "alert", "high")]
    add_medication(patient, "Gliclazide", "sulfonylurea")  # recorded without re-evaluation (drift)
    unclassified = add_patient_variant(patient, *V03)  # stored without a classification (drift)
    drifted = variant_of(patient, V01)
    drifted.current_classification = "VUS"
    db.session.commit()

    run = reevaluation.full_recompute()

    assert (d2.status, d2.superseded_reason) == ("superseded", "reclassified")
    (notice,) = notices_of(patient)
    assert notice.message.startswith(
        "Full re-evaluation under R1 and the current rules: alert(s) D2 (high) for HNF1A variant "
        "12-120978900-G-A (P) superseded; now D2 note."
    )
    assert findings(patient) == {
        ("D2", "note", None),
        ("D5", "alert", "low"),
        ("D3", "note", None),
    }
    assert (unclassified.reported_classification, unclassified.current_classification) == ("LP", "LP")
    assert (unclassified.classification_release_id, unclassified.reclassified_count) == (r1.id, 0)
    assert (drifted.current_classification, drifted.reported_classification, drifted.reclassified_count) == (
        "P",
        "P",
        1,
    )
    assert drifted.last_reclassified_run_id == run.id
    assert run.summary["superseded_by_reason"] == {"reclassified": 1}


def test_full_recompute_withdraws_orphan_alerts_and_stale_notes(db: SQLAlchemy, rules_v1: None) -> None:
    r1 = release("R1", {V01: "P", V02: "P"})
    patient = carrier(V01, drugs=["insulin", "sulfonylurea"])
    other = carrier(V02, drugs=["metformin"])
    db.session.commit()
    reevaluation.activate_release(r1)
    note = current(patient)[("D2", "note", None)]
    for medication in patient.medications:
        if medication.drug_class == "sulfonylurea":
            medication.end_date = date(2025, 1, 1)  # stopped without re-evaluation (drift)
    orphan = make_alert(
        variant_of(other, V02), _version("D1", 1), patient_id=patient.id, release=r1, classification="P"
    )
    db.session.commit()

    run = reevaluation.full_recompute()

    assert (note.status, note.superseded_reason, note.review_notice_id) == ("superseded", "reclassified", None)
    assert (orphan.status, orphan.superseded_reason) == ("superseded", "reclassified")
    (notice,) = notices_of(patient)
    assert notice.alerts == [orphan]
    assert notice.message == (
        "Full re-evaluation under R1 and the current rules: alert(s) D1 (medium) for variant 7-44145200-C-T "
        "superseded. Review any treatment change made on the basis of this alert; "
        "GE-EHR never reverses therapy automatically."
    )
    assert findings(patient) == {("D2", "alert", "high"), ("D5", "alert", "low")}
    assert (run.alerts_superseded, run.alerts_created, run.review_notices) == (2, 1, 1)


def test_full_strategy_activation_repairs_drift_without_reclassification_flags(db: SQLAlchemy, rules_v1: None) -> None:
    r1, r2 = release("R1", {V01: "P", V06: "VUS"}), release("R2", {V01: "P", V06: "LP"})
    drifted = carrier(V01)
    upgraded = carrier(V06)
    db.session.commit()
    reevaluation.activate_release(r1)
    add_medication(drifted, "Gliclazide", "sulfonylurea")  # recorded without re-evaluation
    db.session.commit()

    run = reevaluation.activate_release(r2, strategy="full")

    note = current(drifted)[("D2", "note", None)]
    assert (note.is_reclassified, note.priority, note.notify) == (False, "normal", False)
    assert all(alert.is_reclassified for alert in current(upgraded).values())
    assert run.summary["superseded_by_reason"] == {"reclassified": 2}
    assert run.summary["categories"]["upgrade"]["notes_created"] == 0


def test_full_recompute_without_a_current_release(db: SQLAlchemy, rules_v1: None) -> None:
    patient = carrier(V01)
    run = reevaluation.full_recompute()
    assert (run.old_release_id, run.summary["release"], run.records_examined) == (None, None, 1)
    variant = variant_of(patient, V01)
    assert (variant.current_classification, variant.classification_release_id) == ("NC", None)
    assert findings(patient) == set()


def test_full_recompute_input_and_failure(db: SQLAlchemy, rules_v1: None, monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(ValueError, match="Unknown trigger 'nightly'; expected one of vcf_upload"):
        reevaluation.full_recompute(trigger="nightly")
    carrier(V01)
    db.session.commit()
    _fail_reconciliation(monkeypatch)
    with pytest.raises(RuntimeError):
        reevaluation.full_recompute()
    failed = db.session.scalars(select(ReevaluationRun)).one()
    assert (failed.status, failed.trigger, failed.strategy) == ("failed", "full_recompute", "full")


# ---------------------------------------------------------------------------
# Point-in-time view (SPEC §7.5) and release history
# ---------------------------------------------------------------------------


def test_alerts_as_of_reproduces_the_state_before_and_after_a_release(db: SQLAlchemy, rules_v1: None) -> None:
    r1, r2 = release("R1", {V06: "VUS", V03: "LP"}), release("R2", {V06: "LP", V03: "P"})
    patient = carrier(V06, V03, drugs=["metformin"])
    db.session.commit()
    before_any = utcnow()
    reevaluation.activate_release(r1)
    between = utcnow()
    run = reevaluation.activate_release(r2)
    after = utcnow()

    assert reevaluation.alerts_as_of(patient, before_any - timedelta(seconds=1)) == []
    old_state = reevaluation.alerts_as_of(patient, between)
    assert [(alert.rule_code, alert.severity) for alert in old_state] == [
        ("D3", "medium"),
        ("D5", "low"),
        ("D6", "info"),
    ]
    assert reevaluation.alerts_as_of(patient, between.replace(tzinfo=UTC)) == old_state
    assert [alert.created_release.label for alert in old_state] == ["R1", "R1", "R1"]

    new_state = reevaluation.alerts_as_of(patient, after)
    assert [(alert.rule_code, alert.severity) for alert in new_state] == [
        ("D2", "high"),
        ("D3", "medium"),
        ("D5", "low"),
        ("D5", "low"),
    ]
    assert reevaluation.alerts_as_of(patient, run.started_at) == new_state, "a supersession takes effect at once"

    d3 = next(alert for alert in new_state if alert.rule_code == "D3")
    assert d3.confirmed_release.label == "R2"
    evidence_then = reevaluation.provenance_as_of(d3, between)
    assert (evidence_then.event, evidence_then.release.label, evidence_then.classification) == ("created", "R1", "LP")
    evidence_now = reevaluation.provenance_as_of(d3, after)
    assert (evidence_now.event, evidence_now.release.label, evidence_now.classification) == ("confirmed", "R2", "P")
    assert reevaluation.provenance_as_of(d3, before_any) is None


def test_alerts_as_of_orders_alerts_before_notes(db: SQLAlchemy, rules_v1: None) -> None:
    r1 = release("R1", {V01: "P", V02: "P"})
    patient = carrier(V02, V01, drugs=["sulfonylurea"])
    db.session.commit()
    reevaluation.activate_release(r1)
    state = reevaluation.alerts_as_of(patient, utcnow())
    assert [(alert.rule_code, alert.kind) for alert in state] == [
        ("D1", "alert"),
        ("D5", "alert"),
        ("D5", "alert"),
        ("D2", "note"),
    ]


def test_release_history_lists_activated_releases_in_order(db: SQLAlchemy, rules_v1: None) -> None:
    assert reevaluation.release_history() == []
    r1, r2 = release("R1", {}), release("R2", {})
    release("R3", {})
    reevaluation.activate_release(r2)
    reevaluation.activate_release(r1)
    assert [kb.label for kb in reevaluation.release_history()] == ["R2", "R1"]


# ---------------------------------------------------------------------------
# Randomised equivalence: change-driven == full == ground truth (pure Algorithm 1)
# ---------------------------------------------------------------------------

KATP_GENES = frozenset({"KCNJ11", "ABCC8"})
# Three keys per panel gene inside its padded region (synthetic alleles).
KEY_POOL: tuple[tuple[Key, str], ...] = tuple(
    ((info.chrom, info.start + 1_000 * (index + 1), "C", "T"), gene)
    for gene, info in C.GENE_INFO.items()
    for index in range(3)
)
CLASS_WEIGHTS = (("P", 3), ("LP", 3), ("VUS", 4), ("LB", 2), ("B", 2), ("NC", 1))

ReleaseSpec = dict[Key, tuple[str, str]]


def _random_class(rng: random.Random, gene: str) -> tuple[str, str]:
    cls = rng.choices([cls for cls, _ in CLASS_WEIGHTS], [weight for _, weight in CLASS_WEIGHTS])[0]
    mechanism = rng.choice(C.MECHANISMS) if gene in KATP_GENES else "unknown"
    return cls, mechanism


def _random_release_spec(rng: random.Random, previous: ReleaseSpec | None) -> ReleaseSpec:
    """A release over the key pool; later releases reclassify, add and remove keys at random."""
    spec: ReleaseSpec = dict(previous or {})
    for key, gene in KEY_POOL:
        roll = rng.random()
        if previous is None:
            if roll < 0.8:
                spec[key] = _random_class(rng, gene)
        elif roll < 0.1:
            spec.pop(key, None)
        elif roll < 0.4:
            spec[key] = _random_class(rng, gene)
    return spec


def _vcf(keys: Iterable[Key]) -> bytes:
    lines = [
        "##fileformat=VCFv4.2",
        "##reference=GRCh38",
        '##FORMAT=<ID=GT,Number=1,Type=String,Description="Genotype">',
        "#CHROM\tPOS\tID\tREF\tALT\tQUAL\tFILTER\tINFO\tFORMAT\tSAMPLE",
    ]
    lines += [f"{chrom}\t{pos}\t.\t{ref}\t{alt}\t50\tPASS\t.\tGT\t0/1" for chrom, pos, ref, alt in keys]
    return ("\n".join(lines) + "\n").encode()


@dataclass(frozen=True)
class Snapshot:
    """Strategy-independent state after one step of a scenario."""

    step: str
    alerts: frozenset[tuple[Any, ...]]
    notices: tuple[tuple[Any, ...], ...]
    variants: frozenset[tuple[Any, ...]]
    counters: tuple[int, ...]
    records_examined: int


def _snapshot(step: str, run: ReevaluationRun) -> Snapshot:
    session = database.session
    all_alerts = session.scalars(select(Alert).options(joinedload(Alert.patient), joinedload(Alert.variant_key)))
    all_notices = session.scalars(
        select(ReviewNotice).options(
            joinedload(ReviewNotice.patient), joinedload(ReviewNotice.variant_key), selectinload(ReviewNotice.alerts)
        )
    )
    all_variants = session.scalars(
        select(PatientVariant).options(
            joinedload(PatientVariant.patient),
            joinedload(PatientVariant.variant_key),
            joinedload(PatientVariant.classification_release),
        )
    )
    alerts = frozenset(
        (
            alert.patient.mrn,
            alert.rule_code,
            alert.variant_key.label,
            alert.kind,
            alert.severity,
            alert.status,
            alert.superseded_reason,
            alert.is_reclassified,
            alert.priority,
            alert.notify,
        )
        for alert in all_alerts
    )
    notices = tuple(
        sorted(
            (
                notice.patient.mrn,
                notice.variant_key.label,
                notice.old_classification,
                notice.new_classification,
                tuple(sorted(alert.rule_code for alert in notice.alerts)),
            )
            for notice in all_notices.unique()
        )
    )
    variants = frozenset(
        (
            variant.patient.mrn,
            variant.variant_key.label,
            variant.current_classification,
            variant.reported_classification,
            variant.reclassified_count,
            variant.classification_release.label if variant.classification_release else None,
        )
        for variant in all_variants
    )
    counters = (
        run.stored_keys_compared,
        run.changed_variants,
        run.carriers_affected,
        run.alerts_created,
        run.notes_created,
        run.alerts_superseded,
        run.review_notices,
        run.notifications,
    )
    return Snapshot(step, alerts, notices, variants, counters, run.records_examined)


def _assert_ground_truth(spec: ReleaseSpec, step: str) -> None:
    """Current alerts equal pure Algorithm 1 on the current release (computed from ``spec``)."""
    rules = load_current_rules()
    statement = select(Patient).options(
        selectinload(Patient.variants).joinedload(PatientVariant.variant_key),
        selectinload(Patient.medications),
        selectinload(Patient.diagnosis),
        selectinload(Patient.alerts),
    )
    for patient in database.session.scalars(statement):
        classified = []
        for variant in patient.variants:
            cls, mechanism = spec.get((variant.variant_key.chrom, variant.variant_key.pos, "C", "T"), ("NC", "unknown"))
            classified.append(
                ClassifiedVariant(variant.id, variant.variant_key_id, variant.gene, cls, mechanism, variant.zygosity)
            )
        diagnosis = patient.diagnosis
        context = ClinicalContext(
            drug_classes=frozenset(m.drug_class for m in patient.medications if m.end_date is None),
            age_at_diagnosis_months=diagnosis.age_at_diagnosis_months,
            diabetes_type=diagnosis.diabetes_type,
        )
        expected = {finding.key for finding in evaluate(classified, context, rules)}
        actual = {
            (alert.rule_code, alert.variant_key_id, alert.kind, alert.severity)
            for alert in patient.alerts
            if alert.is_current
        }
        assert actual == expected, f"{step}: {patient.mrn} has {sorted(actual)}, ground truth {sorted(expected)}"


def _random_diagnosis(rng: random.Random) -> tuple[str, int | None]:
    diabetes_type = rng.choice(("type1", "type2", "neonatal", "monogenic", "none", "unknown"))
    if diabetes_type == "none":
        age = None
    elif diabetes_type == "neonatal":
        age = rng.randrange(0, 12)
    else:
        age = rng.choice((None, rng.randrange(0, 24), rng.randrange(24, 600)))
    return diabetes_type, age


def _new_patient(rng: random.Random, number: int, keys: list[Key]) -> Patient:
    """A patient registered through the patient-record service (its VCF classified on ingest)."""
    diabetes_type, age = _random_diagnosis(rng)
    patient = pr.create_patient(
        mrn=f"EQ-{number:04d}",
        given_name="Eq",
        family_name=f"Patient{number}",
        sex=rng.choice(C.SEXES),
        birth_date=date(1950 + rng.randrange(0, 20), rng.randrange(1, 13), rng.randrange(1, 29)),
        diabetes_type=diabetes_type,
        age_at_diagnosis_months=age,
        family_id=f"FAM-{number // 4}",
    )
    for drug_class in rng.sample(C.DRUG_CLASSES, rng.randrange(0, 3)):
        pr.add_medication(patient, drug_name=f"Test {drug_class}", drug_class=drug_class)
    pr.ingest_vcf(patient, _vcf(keys), f"EQ-{number:04d}.vcf")
    return patient


def _run_scenario(seed: int, strategy: str, n_patients: int = 40) -> list[Snapshot]:
    """Patients, then four random releases with therapy changes, new patients and a rule revision."""
    rng = random.Random(seed)
    session = database.session
    import_rule_versions(RULES_V1)
    pool = [key for key, _ in KEY_POOL]
    family_keys: dict[int, Key] = {}
    for number in range(n_patients):
        # Families of four share one variant (autosomal dominant inheritance); stored unclassified,
        # as if their VCFs were ingested before the first release.
        diabetes_type, age = _random_diagnosis(rng)
        patient = make_patient(
            mrn=f"EQ-{number:04d}",
            family_id=f"FAM-{number // 4}",
            diagnosis={"diabetes_type": diabetes_type, "age_at_diagnosis_months": age},
        )
        for drug_class in rng.sample(C.DRUG_CLASSES, rng.randrange(0, 3)):
            add_medication(patient, f"Test {drug_class}", drug_class)
        shared = family_keys.setdefault(number // 4, rng.choice(pool))
        for key in sorted({shared, *rng.sample(pool, rng.randrange(0, 4))}):
            add_patient_variant(patient, *key, zygosity=rng.choice(C.ZYGOSITIES))
    session.commit()

    snapshots: list[Snapshot] = []
    spec: ReleaseSpec | None = None
    releases: list[tuple[KbRelease, ReleaseSpec]] = []
    for step in range(4):
        spec = _random_release_spec(rng, spec)
        kb_release = make_release(f"EQ R{step + 1}")
        for key, (cls, mechanism) in spec.items():
            condition = NEONATAL_DIABETES if mechanism == "activating" else None
            add_known_variant(kb_release, *key, cls, mechanism=mechanism, condition=condition)
        releases.append((kb_release, spec))
        run = reevaluation.activate_release(kb_release, strategy=strategy)
        session.commit()
        _assert_ground_truth(spec, f"seed {seed} {strategy} release {step + 1}")
        snapshots.append(_snapshot(f"release {step + 1}", run))

        # Between releases: therapy changes and a newly registered patient (consistent updates).
        for patient in rng.sample(list(session.scalars(select(Patient))), 5):
            active = [medication for medication in patient.medications if medication.is_active]
            if active and rng.random() < 0.5:
                pr.stop_medication(rng.choice(active))
            else:
                drug_class = rng.choice(C.DRUG_CLASSES)
                pr.add_medication(patient, drug_name=f"Test {drug_class}", drug_class=drug_class)
        _new_patient(rng, n_patients + step, rng.sample(pool, 2))
        session.commit()
        _assert_ground_truth(spec, f"seed {seed} {strategy} after changes {step + 1}")

        if step == 1:
            run = reevaluation.apply_rule_set(RULES_V2, strategy=strategy)
            session.commit()
            _assert_ground_truth(spec, f"seed {seed} {strategy} rules v2")
            snapshots.append(_snapshot("rules v2", run))

    # Re-activate the first release (a new activation of an older release).
    first_release, first_spec = releases[0]
    run = reevaluation.activate_release(first_release, strategy=strategy)
    session.commit()
    _assert_ground_truth(first_spec, f"seed {seed} {strategy} re-activation")
    snapshots.append(_snapshot("re-activation", run))
    return snapshots


def _fresh_database() -> None:
    database.session.remove()
    database.drop_all()
    database.create_all()


def _check_equivalence(seed: int, n_patients: int) -> None:
    change_driven = _run_scenario(seed, "change_driven", n_patients)
    _fresh_database()
    full = _run_scenario(seed, "full", n_patients)

    assert [snapshot.step for snapshot in change_driven] == [snapshot.step for snapshot in full]
    for cd, fr in zip(change_driven, full, strict=True):
        assert cd.alerts == fr.alerts, f"seed {seed}, {cd.step}: alert state differs"
        assert cd.notices == fr.notices, f"seed {seed}, {cd.step}: review notices differ"
        assert cd.variants == fr.variants, f"seed {seed}, {cd.step}: PatientVariant bookkeeping differs"
        assert cd.counters == fr.counters, f"seed {seed}, {cd.step}: run counters differ"
        assert cd.records_examined <= fr.records_examined, f"seed {seed}, {cd.step}: change-driven examined more"
    assert sum(snapshot.counters[1] for snapshot in change_driven) > 0, "the scenario must reclassify something"


@pytest.mark.parametrize("seed", [11, 23, 37])
def test_change_driven_and_full_reach_the_ground_truth_state(db: SQLAlchemy, seed: int) -> None:
    _check_equivalence(seed, n_patients=40)


@pytest.mark.slow
@pytest.mark.parametrize("seed", [41, 59, 73, 89, 97])
def test_equivalence_on_more_seeds_and_a_larger_cohort(db: SQLAlchemy, seed: int) -> None:
    _check_equivalence(seed, n_patients=80)


@pytest.mark.slow
def test_change_driven_work_scales_with_the_carriers_of_changed_keys(db: SQLAlchemy, rules_v1: None) -> None:
    """2,000 patients × 3 variants over 600 stored keys; R2 reclassifies 5 % of the keys."""
    rng = random.Random(5)
    keys = []
    for gene, info in C.GENE_INFO.items():
        keys += [((info.chrom, info.start + 7 * index, "C", "T"), gene) for index in range(1, 100)]
    keys = keys[:600]
    r1_spec = {key: _random_class(rng, gene) for key, gene in keys}
    r2_spec = {key: (_random_class(rng, gene) if rng.random() < 0.05 else r1_spec[key]) for key, gene in keys}
    r1, r2 = make_release("Scale R1"), make_release("Scale R2")
    for kb_release, spec in ((r1, r1_spec), (r2, r2_spec)):
        for key, (cls, mechanism) in spec.items():
            add_known_variant(kb_release, *key, cls, mechanism=mechanism)
    for number in range(2_000):
        patient = make_patient(
            mrn=f"SC-{number:05d}", diagnosis={"diabetes_type": "type2", "age_at_diagnosis_months": 400}
        )
        add_medication(patient, "Metformin", "metformin")
        for key, _gene in rng.sample(keys, 3):
            add_patient_variant(patient, *key)
    db.session.commit()
    reevaluation.activate_release(r1)
    db.session.commit()

    run = reevaluation.activate_release(r2)

    changed = {(change.variant_key.chrom, change.variant_key.pos, "C", "T") for change in run.changes}
    carrier_records = db.session.scalar(
        select(func.count(PatientVariant.id)).where(
            PatientVariant.variant_key_id.in_([change.variant_key_id for change in run.changes])
        )
    )
    assert changed and changed <= {key for key, _ in keys}
    assert run.stored_keys_compared <= 600 and run.records_examined == carrier_records
    assert run.records_examined < 6_000 / 5, "change-driven examines only carriers of changed keys"
    assert run.duration_ms < 120_000
