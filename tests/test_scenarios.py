"""Table V scenarios S1–S9, S11 and RS1–RS7 on the demo dataset (SPEC §11, §12), plus seed_demo itself.

Two databases are seeded once per module and only read by the scenario tests:

* ``r1`` — ``seed_demo()``: rules v1 and demo release R1, then the 15 demo patients (S rows);
* ``final`` — ``seed_demo(full=True)``: additionally rule set v2 through the re-evaluation engine
  (RS5) and release R2 activated change-driven (RS1–RS4, RS6, RS7).

Findings are compared as exact sets of ``(rule code, kind, severity, demo variant id)``. S10
(malformed VCF rejected) and S12 (lab technician denied) run through the routes in
``tests/test_scenarios_ui.py`` (UI-A work package). The seed_demo / ``flask seed-demo`` tests at the
end use the suite's per-test ``app`` database.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterator
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
from flask import Flask
from flask.testing import FlaskCliRunner
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import func, select

from ge_ehr import demo
from ge_ehr.config import DEFAULT_DEMO_PASSWORD
from ge_ehr.demo import DemoSeedError, demo_password, load_demo_patients, seed_demo
from ge_ehr.extensions import db as _db
from ge_ehr.models import (
    Alert,
    AlertEvent,
    AuditLog,
    GenomicUpload,
    KbRelease,
    KnownVariant,
    Medication,
    Model,
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
from ge_ehr.services.alerting import import_rule_versions
from ge_ehr.services.fhir_export import build_patient_bundle, validate_bundle
from ge_ehr.services.reevaluation import alerts_as_of, provenance_as_of
from ge_ehr.services.rules import RuleDefinitionError, load_rule_set_file
from ge_ehr.services.vcf_parser import VcfError
from tests.conftest import build_app
from tests.factories import TEST_PASSWORD, make_patient, make_user

R1_LABEL = "R1 (2025-01, synthetic)"
R2_LABEL = "R2 (2026-01, synthetic)"

#: SPEC §11 demo variants: id → key label (chrom-pos-ref-alt).
VARIANTS: dict[str, str] = {
    "V01": "12-120978900-G-A",
    "V02": "7-44145200-C-T",
    "V03": "20-44401300-C-T",
    "V04": "11-17387100-C-T",
    "V05": "11-2160500-G-A",
    "V06": "12-120994300-C-T",
    "V07": "7-44150500-G-A",
    "V08": "12-120999100-A-G",
    "V09": "11-17410300-G-A",
    "V10": "20-44410200-G-T",
    "V11": "7-44160100-T-C",
    "B01": "12-121001000-C-T",
    "B02": "7-44180000-A-G",
    "B03": "17-37700000-G-A",
    "B04": "11-17450000-C-T",
}
_VARIANT_IDS = {label: variant_id for variant_id, label in VARIANTS.items()}

Finding = tuple[str, str, str | None, str]  # (rule code, kind, severity, variant id)

#: SPEC §11 "Expected under R1 + rules v1" column.
EXPECTED_R1: dict[str, set[Finding]] = {
    "DEMO-0001": {("D2", "alert", "high", "V01"), ("D5", "alert", "low", "V01")},
    "DEMO-0002": {("D2", "note", None, "V01"), ("D5", "alert", "low", "V01")},
    "DEMO-0003": {("D1", "alert", "medium", "V02"), ("D5", "alert", "low", "V02")},
    "DEMO-0004": {("D1", "note", None, "V02"), ("D5", "alert", "low", "V02")},
    "DEMO-0005": {("D3", "alert", "medium", "V03"), ("D5", "alert", "low", "V03")},
    "DEMO-0006": {("D4", "alert", "high", "V04"), ("D5", "alert", "low", "V04")},
    "DEMO-0007": {("D5", "alert", "low", "V05")},
    "DEMO-0008": {("D6", "alert", "info", "V06")},
    "DEMO-0009": set(),
    "DEMO-0010": {("D1", "alert", "medium", "V07"), ("D5", "alert", "low", "V07")},
    "DEMO-0011": {("D6", "alert", "info", "V08")},
    "DEMO-0012": {("D5", "alert", "low", "V09")},
    "DEMO-0013": {("D6", "alert", "info", "V10")},
    "DEMO-0014": {("D6", "alert", "info", "V10")},
    "DEMO-0015": {("D6", "alert", "info", "V10")},
}
#: After importing rule set v2 (RS5): DEMO-0012 gains D4 high.
EXPECTED_RULES_V2: dict[str, set[Finding]] = {
    **EXPECTED_R1,
    "DEMO-0012": {("D4", "alert", "high", "V09"), ("D5", "alert", "low", "V09")},
}
#: After activating R2 change-driven (RS1–RS4, RS6).
EXPECTED_FINAL: dict[str, set[Finding]] = {
    **EXPECTED_RULES_V2,
    "DEMO-0008": {("D2", "alert", "high", "V06"), ("D5", "alert", "low", "V06")},
    "DEMO-0010": {("D6", "alert", "info", "V07")},
    "DEMO-0011": set(),
    "DEMO-0013": {("D3", "alert", "medium", "V10"), ("D5", "alert", "low", "V10")},
    "DEMO-0014": {("D3", "alert", "medium", "V10"), ("D5", "alert", "low", "V10")},
    "DEMO-0015": {("D3", "note", None, "V10"), ("D5", "alert", "low", "V10")},
}
#: Patients carrying a key that changes between R1 and R2.
R2_CARRIERS = ("DEMO-0005", "DEMO-0008", "DEMO-0010", "DEMO-0011", "DEMO-0013", "DEMO-0014", "DEMO-0015")


# ---------------------------------------------------------------------------
# Seeded databases and lookup helpers
# ---------------------------------------------------------------------------


def _seeded(**options: Any) -> Iterator[tuple[Flask, dict[str, Any]]]:
    application = build_app()
    with application.app_context():
        _db.create_all()
        summary = seed_demo(**options)
        _db.session.commit()
    try:
        yield application, summary
    finally:
        with application.app_context():
            _db.drop_all()
            _db.engine.dispose()


@pytest.fixture(scope="module")
def r1_seed() -> Iterator[tuple[Flask, dict[str, Any]]]:
    yield from _seeded()


@pytest.fixture(scope="module")
def final_seed() -> Iterator[tuple[Flask, dict[str, Any]]]:
    yield from _seeded(full=True)


@pytest.fixture
def r1(r1_seed: tuple[Flask, dict[str, Any]]) -> Iterator[dict[str, Any]]:
    """The R1 database (application context pushed); yields the seed summary. Read-only."""
    application, summary = r1_seed
    with application.app_context():
        yield summary
        _db.session.rollback()


@pytest.fixture
def final(final_seed: tuple[Flask, dict[str, Any]]) -> Iterator[dict[str, Any]]:
    """The fully re-evaluated database (application context pushed); yields the seed summary. Read-only."""
    application, summary = final_seed
    with application.app_context():
        yield summary
        _db.session.rollback()


def patient(mrn: str) -> Patient:
    return _db.session.execute(select(Patient).filter_by(mrn=mrn)).scalar_one()


def release(label: str) -> KbRelease:
    return _db.session.execute(select(KbRelease).filter_by(label=label)).scalar_one()


def rule_version(code: str, version: int) -> RuleVersion:
    return _db.session.execute(select(RuleVersion).filter_by(code=code, version=version)).scalar_one()


def variant_key(variant_id: str) -> VariantKey:
    chrom, pos, ref, alt = VARIANTS[variant_id].split("-")
    key = VariantKey.find(chrom, int(pos), ref, alt)
    assert key is not None, f"{variant_id} has no variant key"
    return key


def patient_variant(mrn: str, variant_id: str) -> PatientVariant:
    statement = select(PatientVariant).where(
        PatientVariant.patient_id == patient(mrn).id, PatientVariant.variant_key_id == variant_key(variant_id).id
    )
    return _db.session.execute(statement).scalar_one()


def alerts_of(mrn: str, *, status: str | None = None) -> list[Alert]:
    statement = select(Alert).where(Alert.patient_id == patient(mrn).id).order_by(Alert.id)
    if status is not None:
        statement = statement.where(Alert.status == status)
    return list(_db.session.scalars(statement))


def current_alerts(mrn: str) -> list[Alert]:
    return [alert for alert in alerts_of(mrn) if alert.is_current]


def finding(alert: Alert) -> Finding:
    return (alert.rule_code, alert.kind, alert.severity, _VARIANT_IDS[alert.variant_key.label])


def findings(alerts: list[Alert]) -> set[Finding]:
    return {finding(alert) for alert in alerts}


def the_alert(mrn: str, code: str, *, kind: str = "alert", status: str | None = None) -> Alert:
    matches = [
        alert
        for alert in alerts_of(mrn, status=status)
        if alert.rule_code == code and alert.kind == kind and (status is not None or alert.is_current)
    ]
    assert len(matches) == 1, f"{mrn}: expected one {code} {kind}, found {matches}"
    return matches[0]


def runs() -> list[ReevaluationRun]:
    return list(_db.session.scalars(select(ReevaluationRun).order_by(ReevaluationRun.id)))


def rule_run() -> ReevaluationRun:
    return _db.session.execute(select(ReevaluationRun).filter_by(trigger="rule_revision")).scalar_one()


def r2_run() -> ReevaluationRun:
    statement = select(ReevaluationRun).filter_by(trigger="clinvar_release", new_release_id=release(R2_LABEL).id)
    return _db.session.execute(statement).scalar_one()


def notices_of(mrn: str) -> list[ReviewNotice]:
    return list(_db.session.scalars(select(ReviewNotice).filter_by(patient_id=patient(mrn).id)))


def events_of(alert: Alert, event: str) -> list[AlertEvent]:
    return [item for item in alert.events if item.event == event]


def bundle_resources(mrn: str) -> list[dict[str, Any]]:
    bundle = build_patient_bundle(patient(mrn))
    validate_bundle(bundle)
    return [entry["resource"] for entry in bundle["entry"]]


def _profile_suffix(resource: dict[str, Any]) -> str:
    return resource["meta"]["profile"][0].rsplit("/", 1)[-1]


def _component_value(resource: dict[str, Any], loinc_code: str) -> dict[str, Any]:
    for component in resource["component"]:
        if component["code"]["coding"][0]["code"] == loinc_code:
            return component
    raise AssertionError(f"no component {loinc_code} in {resource['id']}")


# ---------------------------------------------------------------------------
# S1–S9: initial evaluation under R1 + rules v1
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("mrn", sorted(EXPECTED_R1))
def test_R1_state_matches_the_spec_table(r1: dict[str, Any], mrn: str) -> None:
    """SPEC §11 demo patients, column "Expected under R1 + rules v1" — exact current alerts and notes."""
    assert findings(current_alerts(mrn)) == EXPECTED_R1[mrn]
    assert alerts_of(mrn, status="superseded") == []


def test_R1_alerts_carry_initial_upload_provenance(r1: dict[str, Any]) -> None:
    """Every finding of the seeded database comes from the VCF upload under R1 and rules v1."""
    r1_release = release(R1_LABEL)
    alerts = list(_db.session.scalars(select(Alert)))
    assert len(alerts) == sum(len(expected) for expected in EXPECTED_R1.values())
    for alert in alerts:
        assert alert.trigger == "vcf_upload"
        assert alert.created_release_id == alert.confirmed_release_id == r1_release.id
        assert alert.created_rule_version_id == alert.confirmed_rule_version_id
        assert alert.created_rule_version.version == 1
        assert alert.created_run_id is None
        assert alert.is_reclassified is False and alert.priority == "normal"
        assert alert.notify is (alert.kind == "alert" and alert.severity != "info")
        assert [event.event for event in alert.events] == ["created"]
    assert _db.session.scalar(select(func.count(ReviewNotice.id))) == 0


def test_S1_hnf1a_pathogenic_on_insulin_gives_high_D2_alert(r1: dict[str, Any]) -> None:
    """Table V S1: "HNF1A pathogenic, on insulin" → "High alert, D2" (SPEC §11 DEMO-0001: D2 high, D5 low)."""
    assert findings(current_alerts("DEMO-0001")) == {("D2", "alert", "high", "V01"), ("D5", "alert", "low", "V01")}
    d2 = the_alert("DEMO-0001", "D2")
    assert d2.classification == "P"
    assert d2.message == "Consider low-dose sulfonylurea as first-line treatment."
    assert d2.source == rule_version("D2", 1).source
    assert d2.notify is True
    assert d2.patient_variant.current_classification == "P"
    # The benign background variant B01 produces nothing.
    assert patient_variant("DEMO-0001", "B01").current_classification == "B"


def test_S2_hnf1a_pathogenic_on_sulfonylurea_gives_note_only(r1: dict[str, Any]) -> None:
    """Table V S2: "HNF1A pathogenic, already on sulfonylurea" → "Info note only".

    SPEC §11 DEMO-0002: D2 note, D5 low — the treatment rule yields a note; the cascade-testing
    alert D5 does not depend on therapy.
    """
    assert findings(current_alerts("DEMO-0002")) == {("D2", "note", None, "V01"), ("D5", "alert", "low", "V01")}
    note = the_alert("DEMO-0002", "D2", kind="note")
    assert note.notify is False
    assert note.message.startswith("Genetic criterion of D2 met (HNF1A pathogenic) but therapy condition not met")
    assert "sulfonylurea" in note.message and note.message.endswith("No alert raised.")
    assert [medication.drug_class for medication in patient("DEMO-0002").active_medications] == ["sulfonylurea"]


def test_S3_gck_pathogenic_on_metformin_gives_medium_D1_alert(r1: dict[str, Any]) -> None:
    """Table V S3: "GCK pathogenic, on metformin" → "Medium alert, D1" (SPEC §11 DEMO-0003: D1 medium, D5 low)."""
    assert findings(current_alerts("DEMO-0003")) == {("D1", "alert", "medium", "V02"), ("D5", "alert", "low", "V02")}
    assert the_alert("DEMO-0003", "D1").message.startswith("Medication usually not required outside pregnancy")


def test_S4_gck_pathogenic_without_medication_gives_note_only(r1: dict[str, Any]) -> None:
    """Table V S4: "GCK pathogenic, no medication" → "Info note only" (SPEC §11 DEMO-0004: D1 note, D5 low)."""
    assert findings(current_alerts("DEMO-0004")) == {("D1", "note", None, "V02"), ("D5", "alert", "low", "V02")}
    assert patient("DEMO-0004").active_medications == []
    note = the_alert("DEMO-0004", "D1", kind="note")
    assert note.message.startswith("Genetic criterion of D1 met (GCK pathogenic) but therapy condition not met")
    # The likely benign background variant B02 produces nothing.
    assert patient_variant("DEMO-0004", "B02").current_classification == "LB"


def test_S5_hnf4a_likely_pathogenic_on_metformin_gives_medium_D3_alert(r1: dict[str, Any]) -> None:
    """Table V S5: "HNF4A likely pathogenic, on metformin" → "Medium alert, D3" (DEMO-0005: D3 medium, D5 low)."""
    assert findings(current_alerts("DEMO-0005")) == {("D3", "alert", "medium", "V03"), ("D5", "alert", "low", "V03")}
    assert the_alert("DEMO-0005", "D3").classification == "LP"


def test_S6_kcnj11_pathogenic_infancy_on_insulin_gives_high_D4_alert(r1: dict[str, Any]) -> None:
    """Table V S6: "KCNJ11 pathogenic, diagnosed in infancy, on insulin" → "High alert, D4" (DEMO-0006)."""
    assert findings(current_alerts("DEMO-0006")) == {("D4", "alert", "high", "V04"), ("D5", "alert", "low", "V04")}
    known = _db.session.execute(
        select(KnownVariant).filter_by(release_id=release(R1_LABEL).id, variant_key_id=variant_key("V04").id)
    ).scalar_one()
    assert known.mechanism == "activating"
    assert patient("DEMO-0006").diagnosis.age_at_diagnosis_months == 2
    assert the_alert("DEMO-0006", "D4").created_rule_version.label == "D4 v1"


def test_S7_any_actionable_variant_gives_low_D5_alert(r1: dict[str, Any]) -> None:
    """Table V S7: "Any actionable variant" → "Low alert, D5" (DEMO-0007: INS LP, D5 low only)."""
    assert findings(current_alerts("DEMO-0007")) == {("D5", "alert", "low", "V05")}
    d5 = the_alert("DEMO-0007", "D5")
    assert d5.classification == "LP"
    assert "cascade testing" in d5.message


def test_S8_hnf1a_vus_gives_info_D6(r1: dict[str, Any]) -> None:
    """Table V S8: "HNF1A VUS" → "Info, D6" (DEMO-0008: D6 info)."""
    assert findings(current_alerts("DEMO-0008")) == {("D6", "alert", "info", "V06")}
    d6 = the_alert("DEMO-0008", "D6")
    assert d6.classification == "VUS"
    assert d6.notify is False  # monitoring only, not a clinician notification


def test_S9_only_benign_variants_give_no_alert(r1: dict[str, Any]) -> None:
    """Table V S9: "Only benign variants" → "No alert" (DEMO-0009: B01, B02, B04 and the off-panel X01)."""
    assert alerts_of("DEMO-0009") == []
    variants = {
        _VARIANT_IDS[variant.variant_key.label]: variant.current_classification
        for variant in patient("DEMO-0009").variants
    }
    assert variants == {"B01": "B", "B02": "LB", "B04": "LB"}
    (upload,) = patient("DEMO-0009").uploads
    assert (upload.status, upload.total_records, upload.panel_variants, upload.off_panel_skipped) == (
        "accepted",
        4,
        3,
        1,
    )


def test_S11_fhir_export_of_S1_patient_is_a_valid_bundle(r1: dict[str, Any]) -> None:
    """Table V S11: "FHIR export of S1 patient" → "Valid FHIR R4 Bundle" (DEMO-0001)."""
    bundle = build_patient_bundle(patient("DEMO-0001"))
    validate_bundle(bundle)
    assert bundle["resourceType"] == "Bundle" and bundle["type"] == "collection"
    resources = [entry["resource"] for entry in bundle["entry"]]
    assert [resource["resourceType"] for resource in resources] == ["Patient"] + ["Observation"] * 4
    assert resources[0]["identifier"][0] == {"system": "urn:ge-ehr:mrn", "value": "DEMO-0001"}
    assert all(resource["meta"]["tag"][0]["code"] == "HTEST" for resource in resources)

    observations = resources[1:]
    assert [_profile_suffix(resource) for resource in observations] == [
        "variant",
        "diagnostic-implication",
        "variant",
        "diagnostic-implication",
    ]
    assert {resource["status"] for resource in observations} == {"final"}
    v01_variant, v01_implication = observations[0], observations[1]
    gene = _component_value(v01_variant, "48018-6")["valueCodeableConcept"]["coding"][0]
    assert (gene["code"], gene["display"]) == ("HGNC:11621", "HNF1A")
    assert _component_value(v01_variant, "81254-5")["valueRange"]["low"]["value"] == 120978900
    significance = _component_value(v01_implication, "53037-8")["valueCodeableConcept"]["coding"][0]
    assert (significance["code"], significance["display"]) == ("LA6668-3", "Pathogenic")
    assert v01_implication["derivedFrom"] == [{"reference": bundle["entry"][1]["fullUrl"]}]
    note = v01_implication["note"][0]["text"]
    assert note.startswith(f"Classified against {R1_LABEL}") and "expert panel yes" in note


def test_seed_summary_describes_the_R1_database(r1: dict[str, Any]) -> None:
    """seed_demo() returns users, rules, releases, patients, alerts by severity and runs."""
    assert r1["users"] == [
        {"username": "admin", "full_name": "Demo Administrator", "role": "admin", "created": True},
        {"username": "dr.rao", "full_name": "Dr. A. Rao", "role": "clinician", "created": True},
        {"username": "lab.kim", "full_name": "J. Kim", "role": "lab_technician", "created": True},
    ]
    assert r1["rules"] == ["D1 v1", "D2 v1", "D3 v1", "D4 v1", "D5 v1", "D6 v1"]
    assert [(item["label"], item["is_current"], item["sequence"]) for item in r1["releases"]] == [(R1_LABEL, True, 1)]
    assert r1["releases"][0]["record_count"] > 1_000  # demo variants plus deterministic filler records
    assert r1["current_release"] == R1_LABEL
    assert (r1["patients"], r1["uploads"], r1["patient_variants"]) == (15, {"accepted": 15, "rejected": 0}, 20)
    assert r1["alerts"] == {"high": 2, "medium": 3, "low": 9, "info": 5}
    assert (r1["notes"], r1["superseded"], r1["open_review_notices"]) == (2, 0, 0)
    (run,) = r1["runs"]
    assert (run["trigger"], run["strategy"], run["status"], run["release"], run["initial"]) == (
        "clinvar_release",
        "change_driven",
        "success",
        R1_LABEL,
        True,
    )
    assert run["stored_keys_compared"] == run["changed_variants"] == run["records_examined"] == 0


def test_seed_records_who_did_what(r1: dict[str, Any]) -> None:
    """dr.rao records patients and medications, lab.kim uploads the VCFs, admin loads rules and releases."""
    clinician = _db.session.execute(select(User).filter_by(username="dr.rao")).scalar_one()
    lab = _db.session.execute(select(User).filter_by(username="lab.kim")).scalar_one()
    admin = _db.session.execute(select(User).filter_by(username="admin")).scalar_one()
    assert {upload.uploaded_by_id for upload in _db.session.scalars(select(GenomicUpload))} == {lab.id}
    assert {medication.created_by_id for medication in _db.session.scalars(select(Medication))} == {clinician.id}
    assert release(R1_LABEL).imported_by_id == admin.id
    assert {version.imported_by_id for version in _db.session.scalars(select(RuleVersion))} == {admin.id}
    actions = {
        (entry.action, entry.username)
        for entry in _db.session.scalars(
            select(AuditLog).where(
                AuditLog.action.in_(["patient_created", "vcf_upload_accepted", "demo_seeded", "user_created"])
            )
        )
    }
    assert actions == {
        ("patient_created", "dr.rao"),
        ("vcf_upload_accepted", "lab.kim"),
        ("demo_seeded", "admin"),
        ("user_created", None),
    }
    for user in (admin, clinician, lab):
        assert user.check_password(DEFAULT_DEMO_PASSWORD) and not user.must_change_password


# ---------------------------------------------------------------------------
# RS1–RS7: rule set v2 and release R2 (seed_demo(full=True))
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("mrn", sorted(EXPECTED_FINAL))
def test_final_state_matches_the_spec_table(final: dict[str, Any], mrn: str) -> None:
    """SPEC §11 "Expected after importing rules v2 / after activating R2" — exact current findings."""
    assert findings(current_alerts(mrn)) == EXPECTED_FINAL[mrn]


def test_RS1_upgraded_vus_on_insulin_gives_new_reclassified_high_D2_alert(final: dict[str, Any]) -> None:
    """Table V RS1: "HNF1A VUS (S8) upgraded to LP; patient on insulin" → "New high alert D2, reclassified".

    SPEC §11: DEMO-0008 → D2 high + D5 low, both is_reclassified, high priority; D6 superseded.
    """
    run, r2 = r2_run(), release(R2_LABEL)
    assert findings(current_alerts("DEMO-0008")) == {("D2", "alert", "high", "V06"), ("D5", "alert", "low", "V06")}
    for alert in current_alerts("DEMO-0008"):
        assert alert.is_reclassified is True
        assert alert.priority == "high" and alert.notify is True
        assert (alert.trigger, alert.classification) == ("clinvar_release", "LP")
        assert alert.created_release_id == alert.confirmed_release_id == r2.id
        assert alert.created_run_id == run.id

    d6 = the_alert("DEMO-0008", "D6", status="superseded")
    assert (d6.superseded_reason, d6.superseded_run_id, d6.review_notice_id) == ("reclassified", run.id, None)
    assert [event.event for event in d6.events] == ["created", "superseded"]
    assert notices_of("DEMO-0008") == []

    variant = patient_variant("DEMO-0008", "V06")
    assert (variant.reported_classification, variant.current_classification) == ("VUS", "LP")
    assert (variant.reclassified_count, variant.last_reclassified_run_id) == (1, run.id)
    assert variant.classification_release_id == r2.id and variant.is_amended


def test_RS2_downgraded_gck_variant_supersedes_alerts_with_one_review_notice(final: dict[str, Any]) -> None:
    """Table V RS2: "GCK LP downgraded to VUS" → "Alert superseded; review notice".

    SPEC §11: DEMO-0010 → D1 and D5 superseded, one ReviewNotice, D6 info created, FHIR ``amended``.
    """
    run = r2_run()
    superseded = alerts_of("DEMO-0010", status="superseded")
    assert findings(superseded) == {("D1", "alert", "medium", "V07"), ("D5", "alert", "low", "V07")}
    (notice,) = notices_of("DEMO-0010")
    for alert in superseded:
        assert (alert.superseded_reason, alert.superseded_run_id) == ("downgrade", run.id)
        assert alert.review_notice_id == notice.id
    assert (notice.old_classification, notice.new_classification, notice.status) == ("LP", "VUS", "open")
    assert (notice.run_id, notice.variant_key_id) == (run.id, variant_key("V07").id)
    assert notice.message == (
        f"GCK variant {VARIANTS['V07']} was downgraded from LP to VUS in {R2_LABEL}; alert(s) D1, D5 superseded. "
        "Review any treatment change made on the basis of this alert; GE-EHR never reverses therapy automatically."
    )
    assert sorted(alert.rule_code for alert in notice.alerts) == ["D1", "D5"]

    d6 = the_alert("DEMO-0010", "D6")
    assert (d6.severity, d6.classification, d6.created_run_id) == ("info", "VUS", run.id)
    assert d6.notify is False and d6.is_reclassified is False and d6.priority == "normal"
    # Therapy is never reversed automatically: metformin is still active.
    assert [medication.drug_class for medication in patient("DEMO-0010").active_medications] == ["metformin"]

    variant = patient_variant("DEMO-0010", "V07")
    assert (variant.reported_classification, variant.current_classification) == ("LP", "VUS")
    assert (variant.reclassified_count, variant.last_reclassified_run_id, variant.is_amended) == (1, run.id, True)

    observations = bundle_resources("DEMO-0010")[1:]
    assert [(_profile_suffix(resource), resource["status"]) for resource in observations] == [
        ("variant", "amended"),
        ("diagnostic-implication", "amended"),
    ]
    significance = _component_value(observations[1], "53037-8")["valueCodeableConcept"]["coding"][0]
    assert significance["code"] == "LA26333-7"  # Uncertain significance
    assert "reclassified 1 time(s); first reported as Likely pathogenic" in observations[1]["note"][0]["text"]


def test_RS3_within_actionable_change_updates_provenance_without_notice(final: dict[str, Any]) -> None:
    """Table V RS3: "HNF4A LP changed to P" → "Provenance updated; no notice".

    SPEC §11: DEMO-0005 → D3/D5 provenance updated to R2 (confirmed_release_id), no notification.
    """
    run, r1_release, r2 = r2_run(), release(R1_LABEL), release(R2_LABEL)
    assert findings(current_alerts("DEMO-0005")) == EXPECTED_R1["DEMO-0005"]
    for alert in current_alerts("DEMO-0005"):
        # The same alert rows as at upload: created under R1, confirmed under R2.
        assert (alert.trigger, alert.created_release_id, alert.confirmed_release_id) == (
            "vcf_upload",
            r1_release.id,
            r2.id,
        )
        assert alert.is_reclassified is False and alert.priority == "normal"
        (confirmed,) = events_of(alert, "confirmed")
        assert (confirmed.release_id, confirmed.run_id, confirmed.classification) == (r2.id, run.id, "P")
    assert alerts_of("DEMO-0005", status="superseded") == [] and notices_of("DEMO-0005") == []
    assert run.summary["categories"]["within_actionable"] == {
        "changed_variants": 1,
        "carriers": 1,
        "alerts_created": 0,
        "notes_created": 0,
        "alerts_superseded": 0,
        "alerts_confirmed": 2,
        "review_notices": 0,
        "notifications": 0,
    }
    variant = patient_variant("DEMO-0005", "V03")
    assert (variant.reported_classification, variant.current_classification, variant.reclassified_count) == (
        "LP",
        "P",
        1,
    )


def test_RS4_within_non_actionable_change_supersedes_D6_silently(final: dict[str, Any]) -> None:
    """Table V RS4: "HNF1A VUS changed to LB" → "Provenance updated; no notice".

    SPEC §11: DEMO-0011 → D6 superseded silently, no notice, no notification.
    """
    run = r2_run()
    assert current_alerts("DEMO-0011") == []
    d6 = the_alert("DEMO-0011", "D6", status="superseded")
    assert (d6.superseded_reason, d6.superseded_run_id, d6.review_notice_id) == ("reclassified", run.id, None)
    assert notices_of("DEMO-0011") == []
    assert run.summary["categories"]["within_non_actionable"] == {
        "changed_variants": 1,
        "carriers": 1,
        "alerts_created": 0,
        "notes_created": 0,
        "alerts_superseded": 1,
        "alerts_confirmed": 0,
        "review_notices": 0,
        "notifications": 0,
    }
    variant = patient_variant("DEMO-0011", "V08")
    assert (variant.current_classification, variant.reclassified_count) == ("LB", 1)


def test_RS5_rule_revision_d4_v2_newly_alerts_abcc8_carrier_on_insulin(final: dict[str, Any]) -> None:
    """Table V RS5: "D4 v1 (KCNJ11) replaced by v2 (adds ABCC8)" → "ABCC8 carriers on insulin newly alerted".

    SPEC §11: after importing rules v2 DEMO-0012 gains D4 high (notify); only the outcome change notifies.
    """
    run, d4_v1, d4_v2 = rule_run(), rule_version("D4", 1), rule_version("D4", 2)
    assert run.rule_change == [{"code": "D4", "old_version": 1, "new_version": 2}]
    assert (run.status, run.strategy, run.new_release_id) == ("success", "change_driven", release(R1_LABEL).id)
    # Carriers of KCNJ11 or ABCC8 variants: DEMO-0006 (V04), DEMO-0009 (B04, likely benign), DEMO-0012 (V09).
    assert (run.patients_reevaluated, run.carriers_affected, run.records_examined) == (3, 3, 3)
    assert (run.alerts_created, run.alerts_confirmed, run.alerts_superseded) == (1, 1, 0)
    assert (run.notes_created, run.review_notices, run.notifications) == (0, 0, 1)

    d4 = the_alert("DEMO-0012", "D4")
    assert (d4.severity, d4.trigger, d4.created_run_id) == ("high", "rule_revision", run.id)
    assert d4.created_rule_version_id == d4.confirmed_rule_version_id == d4_v2.id
    assert d4.notify is True and d4.is_reclassified is False
    assert findings(current_alerts("DEMO-0012")) == EXPECTED_RULES_V2["DEMO-0012"]

    # DEMO-0006 still fires with the same severity: provenance moves to D4 v2, no new notification.
    kcnj11 = the_alert("DEMO-0006", "D4")
    assert (kcnj11.created_rule_version_id, kcnj11.confirmed_rule_version_id) == (d4_v1.id, d4_v2.id)
    (confirmed,) = events_of(kcnj11, "confirmed")
    assert (confirmed.rule_version_id, confirmed.run_id) == (d4_v2.id, run.id)
    assert alerts_of("DEMO-0009") == []
    assert d4_v2.is_current and not d4_v1.is_current


def test_RS6_upgraded_variant_shared_by_a_family_alerts_every_carrier(final: dict[str, Any]) -> None:
    """Table V RS6: "Upgraded variant shared by a family of patients" → "Every carrier alerted".

    SPEC §11: DEMO-0013/14/15 (family FAM-RS6) → D3 medium + D5 low for 0013 and 0014; D3 note + D5
    low for 0015.
    """
    run = r2_run()
    family = ("DEMO-0013", "DEMO-0014", "DEMO-0015")
    assert {patient(mrn).family_id for mrn in family} == {"FAM-RS6"}
    for mrn in family:
        assert findings(current_alerts(mrn)) == EXPECTED_FINAL[mrn]
        for alert in current_alerts(mrn):
            assert alert.is_reclassified is True and alert.created_run_id == run.id
            if alert.kind == "alert":
                assert alert.priority == "high" and alert.notify is True
            else:
                assert alert.priority == "normal" and alert.notify is False
        d6 = the_alert(mrn, "D6", status="superseded")
        assert (d6.superseded_reason, d6.superseded_run_id) == ("reclassified", run.id)
        assert notices_of(mrn) == []
    note = the_alert("DEMO-0015", "D3", kind="note")
    assert "therapy condition not met" in note.message

    change = _db.session.execute(
        select(VariantChange).filter_by(run_id=run.id, variant_key_id=variant_key("V10").id)
    ).scalar_one()
    assert (change.old_classification, change.new_classification, change.category, change.carrier_count) == (
        "VUS",
        "LP",
        "upgrade",
        3,
    )


def test_RS7_changed_variant_carried_by_nobody_is_not_examined(final: dict[str, Any]) -> None:
    """Table V RS7: "Changed variant carried by no patient" → "No work performed".

    SPEC §11: V11 (GCK VUS → P) is in both releases but carried by nobody, so it is never examined:
    only stored keys are compared and changed_variants excludes V11.
    """
    run, v11 = r2_run(), variant_key("V11")
    classes = {
        label: classification
        for label, classification in _db.session.execute(
            select(KbRelease.label, KnownVariant.classification)
            .join(KbRelease, KnownVariant.release_id == KbRelease.id)
            .where(KnownVariant.variant_key_id == v11.id)
        )
    }
    assert classes == {R1_LABEL: "VUS", R2_LABEL: "P"}  # a real upgrade between the releases ...
    assert _db.session.scalar(select(func.count(PatientVariant.id)).filter_by(variant_key_id=v11.id)) == 0
    # ... that is never compared.
    stored_keys = _db.session.scalar(select(func.count(func.distinct(PatientVariant.variant_key_id))))
    assert run.stored_keys_compared == stored_keys == 14
    changes = {
        _VARIANT_IDS[change.variant_key.label]: (change.old_classification, change.new_classification, change.category)
        for change in run.changes
    }
    assert changes == {
        "V03": ("LP", "P", "within_actionable"),
        "V06": ("VUS", "LP", "upgrade"),
        "V07": ("LP", "VUS", "downgrade"),
        "V08": ("VUS", "LB", "within_non_actionable"),
        "V10": ("VUS", "LP", "upgrade"),
    }
    assert run.changed_variants == 5
    assert all(item["label"] != VARIANTS["V11"] for item in run.summary["changes"])
    # Work is limited to the carriers of changed keys: one record each.
    assert (run.carriers_affected, run.patients_reevaluated, run.records_examined) == (7, 7, 7)
    assert (run.alerts_created, run.notes_created, run.alerts_superseded, run.alerts_confirmed) == (8, 1, 7, 2)
    assert (run.review_notices, run.notifications) == (1, 8)


def test_R2_leaves_patients_without_changed_variants_untouched(final: dict[str, Any]) -> None:
    """Change-driven: no alert of a non-carrier is created, superseded or confirmed by the R2 run."""
    run, r1_release = r2_run(), release(R1_LABEL)
    for mrn in sorted(set(EXPECTED_FINAL) - set(R2_CARRIERS)):
        for alert in alerts_of(mrn):
            assert run.id not in (alert.created_run_id, alert.superseded_run_id)
            assert alert.confirmed_release_id == r1_release.id
            assert all(event.run_id != run.id for event in alert.events)
        # Their unchanged variants are now backed by R2 without counting as reclassified.
        for variant in patient(mrn).variants:
            assert variant.classification_release_id == release(R2_LABEL).id
            assert variant.reclassified_count == 0 and not variant.is_amended
    assert {resource["status"] for resource in bundle_resources("DEMO-0001")[1:]} == {"final"}


def test_point_in_time_view_reproduces_each_stage(final: dict[str, Any]) -> None:
    """alerts_as_of() reproduces the R1, rules-v2 and R2 states, with the evidence of each moment."""
    before_rules = rule_run().started_at - timedelta(microseconds=1)
    before_r2 = r2_run().started_at - timedelta(microseconds=1)
    for mrn, expected_now in EXPECTED_FINAL.items():
        assert findings(alerts_as_of(patient(mrn), before_rules)) == EXPECTED_R1[mrn], mrn
        assert findings(alerts_as_of(patient(mrn), before_r2)) == EXPECTED_RULES_V2[mrn], mrn
        assert findings(alerts_as_of(patient(mrn), utcnow())) == expected_now, mrn
    d3 = the_alert("DEMO-0005", "D3")
    assert provenance_as_of(d3, before_r2).release_id == release(R1_LABEL).id
    assert provenance_as_of(d3, r2_run().finished_at).release_id == release(R2_LABEL).id


def test_full_seed_summary(final: dict[str, Any]) -> None:
    """seed_demo(full=True): three runs (R1, rules v2, R2) and the final alert counts."""
    assert final["rules"] == ["D1 v1", "D2 v1", "D3 v1", "D4 v2", "D5 v1", "D6 v1"]
    assert [(item["label"], item["is_current"], item["sequence"]) for item in final["releases"]] == [
        (R1_LABEL, False, 1),
        (R2_LABEL, True, 2),
    ]
    assert final["current_release"] == R2_LABEL
    assert final["alerts"] == {"high": 4, "medium": 4, "low": 12, "info": 1}
    assert (final["notes"], final["superseded"], final["open_review_notices"]) == (3, 7, 1)
    assert [(run["trigger"], run["release"], run["initial"]) for run in final["runs"]] == [
        ("clinvar_release", R1_LABEL, True),
        ("rule_revision", R1_LABEL, False),
        ("clinvar_release", R2_LABEL, False),
    ]
    assert [run["id"] for run in final["runs"]] == [run.id for run in runs()]
    assert final["runs"][1]["rule_change"] == [{"code": "D4", "old_version": 1, "new_version": 2}]
    assert final["runs"][2]["changed_variants"] == 5 and final["runs"][2]["notifications"] == 8


# ---------------------------------------------------------------------------
# seed_demo options and guards (per-test database)
# ---------------------------------------------------------------------------


def _count(model: type[Model]) -> int:
    return _db.session.scalar(select(func.count()).select_from(model)) or 0


def test_seed_demo_refuses_a_non_empty_database(r1: dict[str, Any]) -> None:
    """Idempotency guard: a second seed is refused before anything changes."""
    with pytest.raises(DemoSeedError) as excinfo:
        seed_demo()
    message = str(excinfo.value)
    assert message.startswith(
        "The database is not empty (15 patient(s), 1 knowledge-base release(s), 6 rule version(s))"
    )
    assert "flask seed-demo --reset" in message and "DELETES ALL DATA" in message
    _db.session.rollback()
    assert (_count(Patient), _count(User), _count(KbRelease)) == (15, 3, 1)


@pytest.mark.parametrize("model", ["release", "rules"])
def test_seed_demo_refuses_existing_knowledge_base(app: Flask, model: str) -> None:
    """Releases or rule versions without patients also block seeding (the scenarios need a clean KB)."""
    if model == "release":
        _db.session.add(KbRelease(label="Imported earlier", source="synthetic"))
    else:
        import_rule_versions(load_rule_set_file(demo.DEMO_RULE_SETS["v2"]))
    _db.session.commit()
    with pytest.raises(DemoSeedError, match="The database is not empty"):
        seed_demo()


def test_seed_demo_reset_drops_and_reseeds(app: Flask) -> None:
    """reset=True drops every table (including unrelated data) and seeds again."""
    seed_demo()
    make_patient(mrn="EXTRA-1")
    make_user("someone.else")
    _db.session.commit()
    assert (_count(Patient), _count(User)) == (16, 4)

    summary = seed_demo(reset=True)
    _db.session.commit()
    assert summary["patients"] == 15 and all(user["created"] for user in summary["users"])
    assert _db.session.execute(select(Patient).filter_by(mrn="EXTRA-1")).scalar_one_or_none() is None
    assert _db.session.execute(select(User).filter_by(username="someone.else")).scalar_one_or_none() is None
    assert [item["sequence"] for item in summary["releases"]] == [1]
    assert findings(current_alerts("DEMO-0001")) == EXPECTED_R1["DEMO-0001"]


def test_seed_demo_keeps_an_existing_demo_account(app: Flask) -> None:
    """An account created before seeding (e.g. with flask create-user) is kept with its password."""
    make_user("admin", role="admin", full_name="Site Admin")
    _db.session.commit()
    summary = seed_demo(password="Custom-Demo-2026")
    _db.session.commit()
    assert summary["users"][0] == {"username": "admin", "full_name": "Site Admin", "role": "admin", "created": False}
    assert [user["created"] for user in summary["users"][1:]] == [True, True]
    admin = _db.session.execute(select(User).filter_by(username="admin")).scalar_one()
    assert admin.check_password(TEST_PASSWORD) and not admin.check_password("Custom-Demo-2026")
    clinician = _db.session.execute(select(User).filter_by(username="dr.rao")).scalar_one()
    assert clinician.check_password("Custom-Demo-2026")


@pytest.mark.parametrize(
    ("options", "message"),
    [
        ({"password": "short"}, "The demo password does not meet the password policy: Password must be at least 10"),
        ({"rules": "v3"}, "Unknown demo rule set 'v3'; expected one of: v1, v2."),
    ],
)
def test_seed_demo_rejects_invalid_options_before_changing_anything(
    app: Flask, options: dict[str, Any], message: str
) -> None:
    """A weak password or an unknown rule set is refused before reset could drop any data."""
    make_patient(mrn="KEEP-1")
    _db.session.commit()
    with pytest.raises(DemoSeedError) as excinfo:
        seed_demo(reset=True, **options)
    assert str(excinfo.value).startswith(message)
    _db.session.rollback()
    assert [item.mrn for item in _db.session.scalars(select(Patient))] == ["KEEP-1"]
    assert _count(User) == 0


def test_seed_demo_checks_demo_files_before_reset(app: Flask, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """A missing demo file is reported before reset could drop any data."""
    make_patient(mrn="KEEP-1")
    _db.session.commit()
    monkeypatch.setattr(demo, "DEMO_VCF_DIR", tmp_path)
    with pytest.raises(DemoSeedError) as excinfo:
        seed_demo(reset=True)
    assert str(excinfo.value).startswith(f"Demo data file(s) not found: {tmp_path / 'DEMO-0001.vcf'}, ")
    assert str(tmp_path / "DEMO-0015.vcf") in str(excinfo.value)
    _db.session.rollback()
    assert [item.mrn for item in _db.session.scalars(select(Patient))] == ["KEEP-1"]


def test_seed_demo_with_rules_v2_bootstraps_v2_directly(app: Flask) -> None:
    """rules="v2": D4 v2 is current from the start; full then needs no rule-revision run."""
    summary = seed_demo(rules="v2", full=True)
    assert summary["rules"] == ["D1 v1", "D2 v1", "D3 v1", "D4 v2", "D5 v1", "D6 v1"]
    assert [run["trigger"] for run in summary["runs"]] == ["clinvar_release", "clinvar_release"]
    d4 = the_alert("DEMO-0012", "D4")
    assert (d4.trigger, d4.created_release.label) == ("vcf_upload", R1_LABEL)
    assert {patient: findings(current_alerts(patient)) for patient in EXPECTED_FINAL} == EXPECTED_FINAL


def test_seed_demo_activate_r2_without_rule_revision(app: Flask) -> None:
    """activate_r2=True (SPEC §11 signature) activates R2 but keeps rules v1: no D4 for the ABCC8 carrier."""
    summary = seed_demo(activate_r2=True)
    assert summary["current_release"] == R2_LABEL and "D4 v1" in summary["rules"]
    assert [run["trigger"] for run in summary["runs"]] == ["clinvar_release", "clinvar_release"]
    assert findings(current_alerts("DEMO-0012")) == EXPECTED_R1["DEMO-0012"]
    assert findings(current_alerts("DEMO-0008")) == EXPECTED_FINAL["DEMO-0008"]


def test_seed_demo_joins_the_current_session_without_committing(app: Flask) -> None:
    """With the current app (or None) the caller commits: a rollback discards the whole seed."""
    seed_demo(app)
    assert _count(Patient) == 15
    _db.session.rollback()
    assert (_count(Patient), _count(User), _count(KbRelease)) == (0, 0, 0)


def test_seed_demo_with_another_app_creates_tables_and_commits() -> None:
    """Given an app without an active context, seed_demo pushes one, creates the tables and commits."""
    application = build_app()
    try:
        summary = seed_demo(application)
        assert summary["patients"] == 15
        with application.app_context():
            assert _count(Patient) == 15
            assert findings(current_alerts("DEMO-0006")) == EXPECTED_R1["DEMO-0006"]
    finally:
        with application.app_context():
            _db.drop_all()
            _db.engine.dispose()


def test_demo_password_defaults_to_the_app_config(app: Flask) -> None:
    """The --password value wins over DEMO_PASSWORD; both must meet the password policy."""
    assert demo_password() == DEFAULT_DEMO_PASSWORD
    app.config["DEMO_PASSWORD"] = "Configured-Pass-1"
    assert demo_password() == "Configured-Pass-1"
    assert demo_password("Explicit-Pass-2") == "Explicit-Pass-2"
    with pytest.raises(DemoSeedError, match="Password must contain at least one digit"):
        demo_password("no digits in here")


def _demo_patients_file(tmp_path: Path, **changes: Any) -> Path:
    """A patients.json holding only the first demo patient (DEMO-0001) with ``changes`` applied."""
    path = tmp_path / "patients.json"
    path.write_text(json.dumps([{**load_demo_patients()[0], **changes}]), encoding="utf-8")
    return path


def test_load_demo_patients_reads_the_fifteen_patients() -> None:
    """The shipped patients.json holds DEMO-0001 … DEMO-0015 in order."""
    records = load_demo_patients()
    assert [record["mrn"] for record in records] == sorted(EXPECTED_R1)


@pytest.mark.parametrize(
    ("content", "message"),
    [
        (None, "Demo data file not found: "),
        ("{not json", " is not valid JSON: "),
        ('{"mrn": "DEMO-0001"}', " must contain a JSON list of patient objects."),
        ('["DEMO-0001"]', " must contain a JSON list of patient objects."),
    ],
)
def test_load_demo_patients_reports_a_bad_file(tmp_path: Path, content: str | None, message: str) -> None:
    """A missing file, invalid JSON or anything but a list of objects is refused with a clear message."""
    path = tmp_path / "patients.json"
    if content is not None:
        path.write_text(content, encoding="utf-8")
    with pytest.raises(DemoSeedError, match=re.escape(message)):
        load_demo_patients(path)


@pytest.mark.parametrize(
    ("changes", "problem"),
    [
        ({"sex": None, "vcf": "  "}, "(DEMO-0001): missing or empty field(s) sex, vcf."),
        ({"mrn": ""}, "(no MRN): missing or empty field(s) mrn."),
        ({"vcf": "../patients.json"}, "(DEMO-0001): 'vcf' must be a file name in "),
        ({"diagnosis": "type1"}, "(DEMO-0001): 'diagnosis' must be an object with a 'diabetes_type'."),
        ({"diagnosis": {"age_at_diagnosis_months": 180}}, "'diagnosis' must be an object with a 'diabetes_type'."),
        (
            {"medications": [{"drug_name": "Metformin"}]},
            "(DEMO-0001): 'medications' must be a list of objects with a 'drug_name' and a 'drug_class'.",
        ),
        ({"medications": "insulin"}, "'medications' must be a list of objects"),
    ],
)
def test_load_demo_patients_checks_the_structure_of_each_record(
    tmp_path: Path, changes: dict[str, Any], problem: str
) -> None:
    """A record seed_demo could not read is reported with its position and MRN before anything changes."""
    path = _demo_patients_file(tmp_path, **changes)
    with pytest.raises(DemoSeedError) as excinfo:
        load_demo_patients(path)
    assert str(excinfo.value).startswith(f"{path}: patient #1 (")
    assert problem in str(excinfo.value)


def test_load_demo_patients_reports_every_problem_of_a_record(tmp_path: Path) -> None:
    """All structural problems of the first bad record are listed together."""
    path = _demo_patients_file(tmp_path, given_name=3, diagnosis=None, medications=[None])
    with pytest.raises(DemoSeedError) as excinfo:
        load_demo_patients(path)
    assert str(excinfo.value) == (
        f"{path}: patient #1 (DEMO-0001): missing or empty field(s) given_name; 'diagnosis' must be an object "
        "with a 'diabetes_type'; 'medications' must be a list of objects with a 'drug_name' and a 'drug_class'."
    )


@pytest.mark.parametrize(
    ("changes", "cause", "message"),
    [
        ({"sex": "robot"}, ValueError, "Sex must be one of: "),
        ({"birth_date": "12/05/1994"}, ValueError, "Birth date must be a date in YYYY-MM-DD format, got '12/05/1994'."),
        (
            {"medications": [{"drug_name": "Metformin", "drug_class": "biguanide"}]},
            ValueError,
            "Drug class must be one of: ",
        ),
        ({"vcf": "malformed.vcf"}, VcfError, "POS must be a positive whole number"),
    ],
)
def test_seed_demo_names_the_demo_patient_a_service_rejects(
    app: Flask,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    changes: dict[str, Any],
    cause: type[Exception],
    message: str,
) -> None:
    """patient_records validates the values and the VCF; the error names the patient and a rollback keeps nothing."""
    monkeypatch.setattr(demo, "DEMO_PATIENTS_FILE", _demo_patients_file(tmp_path, **changes))
    with pytest.raises(DemoSeedError) as excinfo:
        seed_demo()
    assert str(excinfo.value).startswith("Demo patient DEMO-0001 could not be loaded: ")
    assert message in str(excinfo.value)
    assert isinstance(excinfo.value.__cause__, cause)
    _db.session.rollback()
    assert (_count(Patient), _count(GenomicUpload), _count(User), _count(RuleVersion)) == (0, 0, 0, 0)


def test_seed_demo_reports_an_invalid_rule_set_before_reset(
    app: Flask, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A demo rule set that does not validate is reported before reset could drop any data."""
    make_patient(mrn="KEEP-1")
    _db.session.commit()
    path = tmp_path / "ruleset_v1.json"
    path.write_text('{"rule_set": "Broken", "label": "v1", "rules": [{"code": "D1"}]}', encoding="utf-8")
    monkeypatch.setitem(demo.DEMO_RULE_SETS, "v1", path)
    with pytest.raises(DemoSeedError) as excinfo:
        seed_demo(reset=True)
    assert str(excinfo.value).startswith(f"Demo rule set {path} cannot be loaded: ")
    assert isinstance(excinfo.value.__cause__, RuleDefinitionError)
    _db.session.rollback()
    assert [item.mrn for item in _db.session.scalars(select(Patient))] == ["KEEP-1"]


def test_seed_demo_reports_a_demo_release_the_importer_refuses(
    app: Flask, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A release file import_release refuses is named in the error; a rollback keeps nothing."""
    path = tmp_path / "not_clinvar.vcf"
    path.write_text("hello\n", encoding="utf-8")
    monkeypatch.setattr(demo, "DEMO_RELEASE_1_FILE", path)
    with pytest.raises(DemoSeedError) as excinfo:
        seed_demo()
    assert str(excinfo.value) == (
        f"Demo release {path} could not be imported: not_clinvar.vcf is not a VCF file: its first line must be "
        "'##fileformat=VCFv4.x'."
    )
    _db.session.rollback()
    assert (_count(KbRelease), _count(RuleVersion), _count(User)) == (0, 0, 0)


# ---------------------------------------------------------------------------
# flask seed-demo
# ---------------------------------------------------------------------------


def test_cli_seed_demo_prints_logins_and_next_steps(runner: FlaskCliRunner, db: SQLAlchemy) -> None:
    """``flask seed-demo`` commits the R1 state, prints the summary, the logins and the live-demo steps."""
    result = runner.invoke(args=["seed-demo"])
    assert result.exit_code == 0, result.output
    output = result.output
    assert "Loaded the GE-EHR demo dataset (synthetic data only)" in output
    assert "Rules:     D1 v1, D2 v1, D3 v1, D4 v1, D5 v1, D6 v1" in output
    assert f"{R1_LABEL} [current]" in output
    assert "Patients:  15 (15 VCF upload(s) accepted, 0 rejected; 20 patient variant(s))" in output
    assert "Alerts:    2 high, 3 medium, 9 low, 5 info (current); 2 note(s)" in output
    assert f"Demo logins (password: {DEFAULT_DEMO_PASSWORD}):" in output
    for line in ("admin     Administrator", "dr.rao    Clinician", "lab.kim   Lab technician"):
        assert line in output
    assert "Knowledge base → Rules → import data/rules/ruleset_v2.json" in output
    assert (
        "Knowledge base → Releases → import data/demo/clinvar/demo_release_2.vcf.gz with 'activate and re-evaluate'"
        in output
    )
    assert f"Label it '{R2_LABEL}'." in output
    db.session.rollback()  # the command committed: the data survives a rollback
    assert _count(Patient) == 15

    again = runner.invoke(args=["seed-demo"])
    assert again.exit_code == 1
    assert "Error: The database is not empty (15 patient(s)" in again.output and "--reset" in again.output


def test_cli_reset_asks_for_confirmation(runner: FlaskCliRunner, db: SQLAlchemy) -> None:
    """``--reset`` drops nothing unless confirmed (or ``--yes``); ``--full`` then reaches the R2 state."""
    make_patient(mrn="KEEP-1")
    db.session.commit()
    declined = runner.invoke(args=["seed-demo", "--reset"], input="n\n")
    assert declined.exit_code == 1 and "Drop ALL tables and data in" in declined.output
    assert "Aborted" in declined.output
    assert [item.mrn for item in db.session.scalars(select(Patient))] == ["KEEP-1"]

    result = runner.invoke(args=["seed-demo", "--reset", "--yes", "--full", "--password", "Other-Pass-2026"])
    assert result.exit_code == 0, result.output
    assert result.output.startswith("Dropped and recreated all tables, then loaded the GE-EHR demo dataset")
    assert "Drop ALL tables" not in result.output
    assert "Demo logins (password: the password given with --password):" in result.output
    assert "Other-Pass-2026" not in result.output
    assert "#2 Rule revision (change_driven, success): D4 v1 → v2" in result.output
    assert f"Rule set v2 and release {R2_LABEL} are applied" in result.output
    assert "Next steps" not in result.output
    db.session.rollback()
    assert db.session.execute(select(Patient).filter_by(mrn="KEEP-1")).scalar_one_or_none() is None
    assert KbRelease.current().label == R2_LABEL
    admin = db.session.execute(select(User).filter_by(username="admin")).scalar_one()
    assert admin.check_password("Other-Pass-2026")


def test_cli_hides_a_configured_demo_password(app: Flask, runner: FlaskCliRunner) -> None:
    """Only the public default password is printed; a configured DEMO_PASSWORD is not."""
    app.config["DEMO_PASSWORD"] = "Configured-Secret-77"
    result = runner.invoke(args=["seed-demo"])
    assert result.exit_code == 0, result.output
    assert "Demo logins (password: the value of DEMO_PASSWORD):" in result.output
    assert "Configured-Secret-77" not in result.output


def test_cli_rejects_a_weak_password_before_touching_the_database(runner: FlaskCliRunner, db: SQLAlchemy) -> None:
    """A weak ``--password`` is a usage error (exit 2), reported before the reset confirmation."""
    result = runner.invoke(args=["seed-demo", "--reset", "--password", "weak"])
    assert result.exit_code == 2
    assert "Invalid value for '--password': The demo password does not meet the password policy" in result.output
    assert "Drop ALL tables" not in result.output
    assert _count(User) == 0


def test_cli_reports_a_demo_data_error_and_commits_nothing(
    runner: FlaskCliRunner, db: SQLAlchemy, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A demo record the services reject ends the command with a clear error (exit 1), not a traceback."""
    monkeypatch.setattr(demo, "DEMO_PATIENTS_FILE", _demo_patients_file(tmp_path, sex="robot"))
    result = runner.invoke(args=["seed-demo"])
    assert result.exit_code == 1
    assert result.output.startswith("Error: Demo patient DEMO-0001 could not be loaded: Sex must be one of: ")
    assert "Traceback" not in result.output and "Demo logins" not in result.output
    assert (_count(Patient), _count(User), _count(RuleVersion), _count(KbRelease)) == (0, 0, 0, 0)
