"""Tests of the F1 patient-record service (SPEC §6.4): validation, audit, re-evaluation triggers
and VCF ingest with the PatientVariant classification bookkeeping (SPEC §7.1)."""

from __future__ import annotations

import gzip
import hashlib
from collections.abc import Iterable
from datetime import date, datetime, timedelta
from typing import Any

import pytest
from flask import Flask
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import func, select

from ge_ehr import constants as C
from ge_ehr.extensions import db as database
from ge_ehr.models import (
    Alert,
    AuditLog,
    GenomicUpload,
    KbRelease,
    Medication,
    Patient,
    PatientVariant,
    ReviewNotice,
    User,
    VariantKey,
    utc_today,
)
from ge_ehr.services import patient_records as pr
from ge_ehr.services.alerting import import_rule_versions
from ge_ehr.services.rules import RULES_DATA_DIR, load_rule_set_file
from ge_ehr.services.vcf_parser import VcfError
from tests.factories import add_known_variant, make_patient, make_release

# Demo keys (SPEC §11).
HNF1A_P = ("12", 120978900, "G", "A")  # V01
GCK_P = ("7", 44145200, "C", "T")  # V02
HNF1A_VUS = ("12", 120994300, "C", "T")  # V06 under R1
KCNJ11_P = ("11", 17387100, "C", "T")  # V04, activating
HNF1A_B = ("12", 121001000, "C", "T")  # B01
OFF_PANEL = ("1", 1000000, "A", "G")  # X01


def vcf_bytes(records: Iterable[tuple[tuple[str, int, str, str], str] | str], *, sample: str = "S1") -> bytes:
    """A minimal GRCh38 VCF; ``records`` are ``(key, genotype)`` pairs or raw data lines."""
    lines = [
        "##fileformat=VCFv4.2",
        "##reference=GRCh38",
        '##FORMAT=<ID=GT,Number=1,Type=String,Description="Genotype">',
        f"#CHROM\tPOS\tID\tREF\tALT\tQUAL\tFILTER\tINFO\tFORMAT\t{sample}",
    ]
    for record in records:
        if isinstance(record, str):
            lines.append(record)
        else:
            (chrom, pos, ref, alt), genotype = record
            lines.append(f"{chrom}\t{pos}\t.\t{ref}\t{alt}\t50\tPASS\t.\tGT\t{genotype}")
    return ("\n".join(lines) + "\n").encode()


def audit_rows(db: SQLAlchemy, action: str) -> list[AuditLog]:
    return list(db.session.scalars(select(AuditLog).where(AuditLog.action == action).order_by(AuditLog.id)))


def patient_alerts(patient: Patient) -> list[Alert]:
    return list(database.session.scalars(select(Alert).where(Alert.patient_id == patient.id).order_by(Alert.id)))


def current_findings(patient: Patient) -> set[tuple[str, str, str | None]]:
    return {(alert.rule_code, alert.kind, alert.severity) for alert in patient_alerts(patient) if alert.is_current}


def new_patient(**overrides: Any) -> Patient:
    fields: dict[str, Any] = {
        "mrn": "MRN-0001",
        "given_name": "Ada",
        "family_name": "Lovelace",
        "sex": "female",
        "birth_date": date(1990, 5, 17),
    }
    fields.update(overrides)
    return pr.create_patient(**fields)


@pytest.fixture
def rules_v1(db: SQLAlchemy) -> None:
    import_rule_versions(load_rule_set_file(RULES_DATA_DIR / "ruleset_v1.json"))
    db.session.flush()


@pytest.fixture
def release(db: SQLAlchemy, rules_v1: None) -> KbRelease:
    """A current release classifying the demo keys like R1 (SPEC §11)."""
    current = make_release("R1 (test)", current=True)
    add_known_variant(current, *HNF1A_P, "P", gene="HNF1A")
    add_known_variant(current, *GCK_P, "P", gene="GCK")
    add_known_variant(current, *HNF1A_VUS, "VUS", gene="HNF1A")
    add_known_variant(current, *HNF1A_B, "B", gene="HNF1A")
    add_known_variant(
        current, *KCNJ11_P, "P", gene="KCNJ11", mechanism="activating", condition="Permanent neonatal diabetes mellitus"
    )
    return current


# ---------------------------------------------------------------------------
# create_patient / update_patient
# ---------------------------------------------------------------------------


def test_create_patient_with_diagnosis_and_audit(db: SQLAlchemy, clinician_user: User) -> None:
    patient = pr.create_patient(
        mrn=" DEMO-0042 ",
        given_name="Grace",
        family_name="Hopper",
        sex="FEMALE",
        birth_date="1980-02-29",
        family_id="FAM-1",
        notes="Synthetic test patient",
        diabetes_type="type2",
        age_at_diagnosis_months="300",
        diagnosed_on="2005-03-01",
        external_id="synthea-uuid",
        user=clinician_user,
    )
    assert patient.id is not None
    assert (patient.mrn, patient.sex, patient.birth_date, patient.family_id) == (
        "DEMO-0042",
        "female",
        date(1980, 2, 29),
        "FAM-1",
    )
    assert patient.is_synthetic is True
    assert (patient.external_id, patient.notes) == ("synthea-uuid", "Synthetic test patient")
    assert patient.diagnosis.diabetes_type == "type2"
    assert patient.diagnosis.age_at_diagnosis_months == 300
    assert patient.diagnosis.diagnosed_on == date(2005, 3, 1)
    (entry,) = audit_rows(db, "patient_created")
    assert (entry.entity_type, entry.entity_id, entry.username) == ("patient", patient.id, clinician_user.username)
    assert entry.detail == {
        "mrn": "DEMO-0042",
        "family_id": "FAM-1",
        "diabetes_type": "type2",
        "age_at_diagnosis_months": 300,
    }


def test_create_patient_defaults(db: SQLAlchemy) -> None:
    patient = new_patient(family_id="  ", notes="   ")
    assert patient.family_id is None and patient.notes is None and patient.external_id is None
    assert patient.diagnosis.diabetes_type == "unknown"
    assert patient.diagnosis.age_at_diagnosis_months is None
    assert patient.variants == [] and patient.alerts == []


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"mrn": ""}, "MRN is required."),
        ({"mrn": "AB 12"}, "MRN may contain only letters, digits"),
        ({"mrn": "-AB12"}, "MRN may contain only letters, digits"),
        ({"mrn": "X" * 33}, "MRN must be at most 32 characters (got 33)."),
        ({"given_name": "  "}, "Given name is required."),
        ({"given_name": None}, "Given name is required."),
        ({"given_name": 42}, "Given name must be text."),
        ({"family_name": "Smith\tJones"}, "Family name must not contain control characters"),
        ({"family_name": "x" * 101}, "Family name must be at most 100 characters (got 101)."),
        ({"sex": "f"}, "Sex must be one of: Female (female), Male (male), Other (other), Unknown (unknown); got 'f'."),
        ({"birth_date": None}, "Birth date is required."),
        ({"birth_date": "17/05/1990"}, "Birth date must be a date in YYYY-MM-DD format, got '17/05/1990'."),
        ({"birth_date": 1990}, "Birth date must be a date in YYYY-MM-DD format, got 1990."),
        ({"birth_date": date(1899, 12, 31)}, "Birth date must be on or after 1900-01-01."),
        ({"birth_date": utc_today() + timedelta(days=1)}, "cannot be in the future."),
        ({"notes": 5}, "Notes must be text."),
        ({"notes": "x" * 10_001}, "Notes must be at most 10,000 characters (got 10,001)."),
        ({"diabetes_type": "type3"}, "Diabetes type must be one of: Type 1 (type1)"),
        ({"age_at_diagnosis_months": -1}, "Age at diagnosis cannot be negative."),
        ({"age_at_diagnosis_months": "12.5"}, "Age at diagnosis must be a whole number of months, got '12.5'."),
        ({"age_at_diagnosis_months": True}, "Age at diagnosis must be a whole number of months."),
        ({"age_at_diagnosis_months": 1.5}, "Age at diagnosis must be a whole number of months, got 1.5."),
        ({"age_at_diagnosis_months": 1_501}, "Age at diagnosis must be at most 1,500 months."),
        ({"age_at_diagnosis_months": 600}, "cannot exceed the patient's current age"),
        ({"diagnosed_on": "1989-01-01"}, "Diagnosis date (1989-01-01) cannot be before the birth date (1990-05-17)."),
        ({"diagnosed_on": utc_today() + timedelta(days=1)}, "Diagnosis date"),
        (
            {"diabetes_type": "none", "age_at_diagnosis_months": 3},
            "Age at diagnosis and diagnosis date must be empty when the patient has no diabetes.",
        ),
    ],
)
def test_create_patient_validation(db: SQLAlchemy, overrides: dict[str, Any], message: str) -> None:
    with pytest.raises(ValueError, match=None) as excinfo:
        new_patient(**overrides)
    assert message in str(excinfo.value)
    assert db.session.scalar(select(func.count(Patient.id))) == 0
    assert audit_rows(db, "patient_created") == []


def test_create_patient_rejects_duplicate_mrn(db: SQLAlchemy) -> None:
    new_patient(mrn="DEMO-0001")
    with pytest.raises(ValueError, match="A patient with MRN DEMO-0001 already exists."):
        new_patient(mrn="DEMO-0001", given_name="Other")


def test_update_patient_audits_changed_fields(db: SQLAlchemy, clinician_user: User) -> None:
    patient = new_patient()
    result = pr.update_patient(
        patient, given_name="Augusta Ada", notes="Updated notes", family_id="FAM-9", sex="female", user=clinician_user
    )
    assert result is patient
    assert (patient.given_name, patient.notes, patient.family_id) == ("Augusta Ada", "Updated notes", "FAM-9")
    (entry,) = audit_rows(db, "patient_updated")
    assert entry.username == clinician_user.username
    assert entry.detail == {
        "mrn": "MRN-0001",
        "changes": {
            "given_name": {"old": "Ada", "new": "Augusta Ada"},
            "family_id": {"old": None, "new": "FAM-9"},
            # Free text is not copied into the audit log.
            "notes": {"changed": True},
        },
    }


def test_update_patient_without_changes_writes_no_audit_row(db: SQLAlchemy) -> None:
    patient = new_patient()
    pr.update_patient(patient, mrn="MRN-0001", given_name=" Ada ")
    assert audit_rows(db, "patient_updated") == []


def test_update_patient_birth_date_is_serialised_in_audit(db: SQLAlchemy) -> None:
    patient = new_patient()
    pr.update_patient(patient, birth_date="1991-01-02")
    assert patient.birth_date == date(1991, 1, 2)
    (entry,) = audit_rows(db, "patient_updated")
    assert entry.detail["changes"]["birth_date"] == {"old": "1990-05-17", "new": "1991-01-02"}


def test_update_patient_validation(db: SQLAlchemy) -> None:
    patient = new_patient(diabetes_type="neonatal", age_at_diagnosis_months=3, diagnosed_on="1990-08-20")
    new_patient(mrn="MRN-0002")
    with pytest.raises(ValueError, match=r"Unknown patient field\(s\): diabetes_type, is_synthetic; allowed: mrn,"):
        pr.update_patient(patient, diabetes_type="type1", is_synthetic=False)
    with pytest.raises(ValueError, match="A patient with MRN MRN-0002 already exists."):
        pr.update_patient(patient, mrn="MRN-0002")
    with pytest.raises(ValueError, match="Diagnosis date \\(1990-08-20\\) cannot be before the birth date"):
        pr.update_patient(patient, birth_date="1991-01-01")
    with pytest.raises(ValueError, match="Given name is required."):
        pr.update_patient(patient, given_name="")
    assert (patient.mrn, patient.birth_date, patient.given_name) == ("MRN-0001", date(1990, 5, 17), "Ada")
    pr.update_patient(patient, mrn="MRN-0001")  # its own MRN is not a duplicate


# ---------------------------------------------------------------------------
# update_diagnosis
# ---------------------------------------------------------------------------


def test_update_diagnosis_reevaluates_with_diagnosis_change(db: SQLAlchemy, release: KbRelease, lab_user: User) -> None:
    patient = new_patient(diabetes_type="neonatal", age_at_diagnosis_months=2)
    pr.add_medication(patient, drug_name="Insulin glargine", drug_class="insulin")
    pr.ingest_vcf(patient, vcf_bytes([(KCNJ11_P, "0/1")]), "kcnj11.vcf", user=lab_user)
    assert current_findings(patient) == {("D4", "alert", "high"), ("D5", "alert", "low")}

    result = pr.update_diagnosis(patient, diabetes_type="type1", age_at_diagnosis_months=24, notes="Revised")

    assert patient.diagnosis.diabetes_type == "type1" and patient.diagnosis.age_at_diagnosis_months == 24
    assert [(alert.rule_code, alert.kind) for alert in result.superseded] == [("D4", "alert")]
    assert [(alert.rule_code, alert.kind) for alert in result.created] == [("D4", "note")]
    (superseded,) = result.superseded
    assert (superseded.status, superseded.superseded_reason) == ("superseded", "diagnosis_changed")
    assert result.created[0].trigger == "diagnosis_change"
    assert "diagnosed at 2 years" in result.created[0].message, result.created[0].message
    assert current_findings(patient) == {("D4", "note", None), ("D5", "alert", "low")}
    assert db.session.scalar(select(func.count(ReviewNotice.id))) == 0, "diagnosis changes raise no notices"
    (entry,) = audit_rows(db, "diagnosis_updated")
    assert entry.detail["changes"]["diabetes_type"] == {"old": "neonatal", "new": "type1"}
    assert entry.detail["changes"]["age_at_diagnosis_months"] == {"old": 2, "new": 24}
    assert entry.detail["changes"]["notes"] == {"changed": True}
    assert entry.detail["evaluation"] == {"alerts_created": 0, "notes_created": 1, "superseded": 1, "confirmed": 0}


def test_update_diagnosis_without_change_still_evaluates_but_writes_no_audit(
    db: SQLAlchemy, release: KbRelease
) -> None:
    patient = new_patient(diabetes_type="type2", age_at_diagnosis_months=300)
    result = pr.update_diagnosis(patient, diabetes_type="type2", age_at_diagnosis_months=300)
    assert (result.created, result.superseded, result.records_examined) == ([], [], 0)
    assert audit_rows(db, "diagnosis_updated") == []


def test_update_diagnosis_creates_missing_record(db: SQLAlchemy) -> None:
    patient = make_patient()
    patient.diagnosis = None
    db.session.flush()
    pr.update_diagnosis(patient, diabetes_type="none", age_at_diagnosis_months=None)
    assert patient.diagnosis is not None and patient.diagnosis.diabetes_type == "none"


def test_update_diagnosis_validation_changes_nothing(db: SQLAlchemy) -> None:
    patient = new_patient(diabetes_type="type1", age_at_diagnosis_months=120)
    with pytest.raises(ValueError, match="Diabetes type must be one of"):
        pr.update_diagnosis(patient, diabetes_type="", age_at_diagnosis_months=None)
    with pytest.raises(ValueError, match="cannot exceed the patient's current age"):
        pr.update_diagnosis(patient, diabetes_type="type1", age_at_diagnosis_months=999)
    assert (patient.diagnosis.diabetes_type, patient.diagnosis.age_at_diagnosis_months) == ("type1", 120)
    assert audit_rows(db, "diagnosis_updated") == []


# ---------------------------------------------------------------------------
# Medications
# ---------------------------------------------------------------------------


def test_add_medication_supersedes_with_therapy_changed_and_no_notice(
    db: SQLAlchemy, release: KbRelease, clinician_user: User
) -> None:
    patient = new_patient(diabetes_type="type1", age_at_diagnosis_months=180)
    pr.add_medication(patient, drug_name="Insulin glargine", drug_class="insulin")
    pr.ingest_vcf(patient, vcf_bytes([(HNF1A_P, "0/1")]), "a.vcf")
    assert current_findings(patient) == {("D2", "alert", "high"), ("D5", "alert", "low")}

    medication, result = pr.add_medication(
        patient,
        drug_name=" Gliclazide 80 mg ",
        drug_class="Sulfonylurea",
        dose="80 mg",
        start_date="2024-01-05",
        user=clinician_user,
    )

    assert (medication.drug_name, medication.drug_class) == ("Gliclazide 80 mg", "sulfonylurea")
    assert (medication.dose, medication.start_date, medication.end_date) == ("80 mg", date(2024, 1, 5), None)
    assert medication.created_by == clinician_user
    (superseded,) = result.superseded
    assert (superseded.rule_code, superseded.kind, superseded.superseded_reason) == ("D2", "alert", "therapy_changed")
    (note,) = result.created
    assert (note.rule_code, note.kind, note.severity, note.notify, note.trigger) == (
        "D2",
        "note",
        None,
        False,
        "medication_change",
    )
    assert "sulfonylurea" in note.message
    assert current_findings(patient) == {("D2", "note", None), ("D5", "alert", "low")}
    assert db.session.scalar(select(func.count(ReviewNotice.id))) == 0, "therapy changes raise no review notices"
    entry = audit_rows(db, "medication_added")[-1]
    assert (entry.entity_type, entry.entity_id) == ("medication", medication.id)
    assert entry.username == clinician_user.username
    assert entry.detail == {
        "patient_id": patient.id,
        "mrn": patient.mrn,
        "drug_name": "Gliclazide 80 mg",
        "drug_class": "sulfonylurea",
        "dose": "80 mg",
        "start_date": "2024-01-05",
        "evaluation": {"alerts_created": 0, "notes_created": 1, "superseded": 1, "confirmed": 0},
    }


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"drug_name": " "}, "Drug name is required."),
        ({"drug_class": "statin"}, "Drug class must be one of: Insulin (insulin), Metformin (metformin)"),
        ({"dose": "x" * 65}, "Dose must be at most 64 characters (got 65)."),
        ({"start_date": "2024-02-30"}, "Start date must be a date in YYYY-MM-DD format, got '2024-02-30'."),
        ({"start_date": utc_today() + timedelta(days=1)}, "cannot be in the future."),
        ({"start_date": date(1980, 1, 1)}, "Start date (1980-01-01) cannot be before the birth date (1990-05-17)."),
    ],
)
def test_add_medication_validation(db: SQLAlchemy, kwargs: dict[str, Any], message: str) -> None:
    patient = new_patient()
    fields: dict[str, Any] = {"drug_name": "Metformin 500 mg", "drug_class": "metformin"}
    fields.update(kwargs)
    with pytest.raises(ValueError) as excinfo:
        pr.add_medication(patient, **fields)
    assert message in str(excinfo.value)
    assert db.session.scalar(select(func.count(Medication.id))) == 0


def test_add_medication_accepts_a_datetime_start(db: SQLAlchemy) -> None:
    patient = new_patient()
    medication, _ = pr.add_medication(
        patient, drug_name="Metformin", drug_class="metformin", start_date=datetime(2024, 3, 1, 8, 30)
    )
    assert medication.start_date == date(2024, 3, 1)


def test_stop_medication_reevaluates(db: SQLAlchemy, release: KbRelease, clinician_user: User) -> None:
    patient = new_patient(diabetes_type="type1", age_at_diagnosis_months=180)
    pr.add_medication(patient, drug_name="Insulin glargine", drug_class="insulin")
    sulfonylurea, _ = pr.add_medication(
        patient, drug_name="Gliclazide", drug_class="sulfonylurea", start_date="2024-01-01"
    )
    pr.ingest_vcf(patient, vcf_bytes([(HNF1A_P, "0/1")]), "a.vcf")
    assert current_findings(patient) == {("D2", "note", None), ("D5", "alert", "low")}

    result = pr.stop_medication(sulfonylurea, end_date="2024-06-30", user=clinician_user)

    assert sulfonylurea.end_date == date(2024, 6, 30) and not sulfonylurea.is_active
    assert [(alert.rule_code, alert.kind, alert.superseded_reason) for alert in result.superseded] == [
        ("D2", "note", "therapy_changed")
    ]
    assert [(alert.rule_code, alert.kind, alert.severity, alert.notify) for alert in result.created] == [
        ("D2", "alert", "high", True)
    ]
    assert current_findings(patient) == {("D2", "alert", "high"), ("D5", "alert", "low")}
    (entry,) = audit_rows(db, "medication_stopped")
    assert entry.entity_id == sulfonylurea.id and entry.username == clinician_user.username
    assert entry.detail["end_date"] == "2024-06-30"
    assert entry.detail["evaluation"] == {"alerts_created": 1, "notes_created": 0, "superseded": 1, "confirmed": 0}


def test_stop_medication_defaults_to_today_and_validates(db: SQLAlchemy) -> None:
    patient = new_patient()
    medication, _ = pr.add_medication(patient, drug_name="Metformin", drug_class="metformin", start_date="2020-01-01")
    with pytest.raises(ValueError, match=r"End date \(2019-12-31\) cannot be before the start date \(2020-01-01\)."):
        pr.stop_medication(medication, end_date=date(2019, 12, 31))
    with pytest.raises(ValueError, match="cannot be in the future"):
        pr.stop_medication(medication, end_date=utc_today() + timedelta(days=2))
    pr.stop_medication(medication)
    assert medication.end_date == utc_today()
    with pytest.raises(ValueError, match=f"Metformin was already stopped on {utc_today().isoformat()}."):
        pr.stop_medication(medication)


def test_suggest_drug_class_is_re_exported() -> None:
    assert pr.suggest_drug_class is C.suggest_drug_class
    assert pr.suggest_drug_class("Glimepiride 2 mg") == "sulfonylurea"


# ---------------------------------------------------------------------------
# ingest_vcf
# ---------------------------------------------------------------------------


def test_ingest_vcf_accepts_classifies_and_evaluates(db: SQLAlchemy, release: KbRelease, lab_user: User) -> None:
    patient = new_patient(diabetes_type="type1", age_at_diagnosis_months=180)
    pr.add_medication(patient, drug_name="Insulin glargine", drug_class="insulin")
    data = vcf_bytes(
        [
            (HNF1A_P, "0/1"),
            (HNF1A_B, "1/1"),
            (OFF_PANEL, "0/1"),
            (GCK_P, "0/0"),
            "7\t44150000\t.\tG\tA\t50\tLowQual\t.\tGT\t0/1",
            ((HNF1A_VUS[0], HNF1A_VUS[1], "c", "t"), "0|1"),
        ],
        sample="DEMO-S1",
    )

    upload, result = pr.ingest_vcf(patient, data, "C:\\Users\\lab\\DEMO-0001.vcf", user=lab_user)

    assert upload.status == "accepted" and upload.error is None
    assert (upload.filename, upload.sample_id, upload.size_bytes) == ("DEMO-0001.vcf", "DEMO-S1", len(data))
    assert upload.sha256 == hashlib.sha256(data).hexdigest()
    assert (upload.total_records, upload.panel_variants) == (6, 3)
    assert (upload.off_panel_skipped, upload.non_pass_skipped, upload.ref_calls_skipped) == (1, 1, 1)
    assert upload.uploaded_by == lab_user and upload.patient == patient

    variants = {variant.variant_key.label: variant for variant in patient.variants}
    assert set(variants) == {"12-120978900-G-A", "12-121001000-C-T", "12-120994300-C-T"}
    hnf1a = variants["12-120978900-G-A"]
    assert (hnf1a.gene, hnf1a.zygosity, hnf1a.upload_id) == ("HNF1A", "heterozygous", upload.id)
    assert (hnf1a.current_classification, hnf1a.reported_classification) == ("P", "P")
    assert hnf1a.classification_release_id == release.id and hnf1a.reclassified_count == 0
    assert variants["12-121001000-C-T"].zygosity == "homozygous"
    assert variants["12-121001000-C-T"].current_classification == "B"
    assert variants["12-120994300-C-T"].current_classification == "VUS"

    assert result is not None and result.records_examined == 3
    created = {(alert.rule_code, alert.kind, alert.severity) for alert in result.created}
    assert created == {("D2", "alert", "high"), ("D5", "alert", "low"), ("D6", "alert", "info")}
    assert {alert.trigger for alert in result.created} == {"vcf_upload"}
    assert {alert.created_release_id for alert in result.created} == {release.id}

    (entry,) = audit_rows(db, "vcf_upload_accepted")
    assert (entry.entity_type, entry.entity_id, entry.username) == ("genomic_upload", upload.id, lab_user.username)
    assert entry.detail["new_variants"] == 3 and entry.detail["updated_variants"] == 0
    assert entry.detail["panel_variants"] == 3 and entry.detail["sample_id"] == "DEMO-S1"
    assert entry.detail["sha256"] == upload.sha256 and isinstance(entry.detail["warnings"], list)


def test_ingest_vcf_classifies_mechanism_for_d4(db: SQLAlchemy, release: KbRelease) -> None:
    patient = new_patient(diabetes_type="neonatal", age_at_diagnosis_months=2)
    pr.add_medication(patient, drug_name="Insulin", drug_class="insulin")
    _, result = pr.ingest_vcf(patient, vcf_bytes([(KCNJ11_P, "0/1")]), "a.vcf")
    assert {(alert.rule_code, alert.kind) for alert in result.created} == {("D4", "alert"), ("D5", "alert")}


def test_ingest_vcf_without_current_release_stores_unclassified_variants(db: SQLAlchemy, rules_v1: None) -> None:
    make_release("Imported, not activated")
    patient = new_patient()
    upload, result = pr.ingest_vcf(patient, vcf_bytes([(HNF1A_P, "0/1")]), "a.vcf")
    (variant,) = patient.variants
    assert (variant.current_classification, variant.reported_classification) == ("NC", "NC")
    assert variant.classification_release_id is None and variant.reclassified_count == 0
    assert result is not None and result.created == [] and result.records_examined == 1
    assert upload.status == "accepted"


def test_second_upload_unions_variants_and_latest_upload_wins(db: SQLAlchemy, release: KbRelease) -> None:
    patient = new_patient()
    first, _ = pr.ingest_vcf(patient, vcf_bytes([(HNF1A_P, "0/1"), (HNF1A_B, "0/1")]), "first.vcf")
    original = {variant.variant_key_id: variant.id for variant in patient.variants}

    # Reclassification of a stored variant is the re-evaluation engine's job, never the upload's.
    hnf1a_p = next(variant for variant in patient.variants if variant.variant_key.label == "12-120978900-G-A")
    hnf1a_p.current_classification = "LP"
    db.session.flush()

    second, _ = pr.ingest_vcf(patient, vcf_bytes([(HNF1A_P, "1/1"), (GCK_P, "0/1")]), "second.vcf")

    db.session.expire_all()
    variants = {variant.variant_key.label: variant for variant in patient.variants}
    assert set(variants) == {"12-120978900-G-A", "12-121001000-C-T", "7-44145200-C-T"}
    assert variants["12-120978900-G-A"].id == original[variants["12-120978900-G-A"].variant_key_id]
    assert (variants["12-120978900-G-A"].zygosity, variants["12-120978900-G-A"].upload_id) == ("homozygous", second.id)
    assert variants["12-120978900-G-A"].current_classification == "LP", "existing variant is not reclassified"
    assert variants["12-120978900-G-A"].reported_classification == "P"
    assert variants["12-121001000-C-T"].upload_id == first.id, "variant missing from the later upload is kept"
    assert variants["7-44145200-C-T"].current_classification == "P"
    entry = audit_rows(db, "vcf_upload_accepted")[-1]
    assert (entry.detail["new_variants"], entry.detail["updated_variants"]) == (1, 1)
    assert len(patient.uploads) == 2


def test_ingest_vcf_counts_a_repeated_key_once(db: SQLAlchemy, release: KbRelease) -> None:
    patient = new_patient()
    upload, _ = pr.ingest_vcf(patient, vcf_bytes([(HNF1A_P, "0/1"), (HNF1A_P, "1/1")]), "dup.vcf")
    (variant,) = patient.variants
    assert variant.zygosity == "heterozygous", "the parser keeps the first record of a repeated key"
    entry = audit_rows(db, "vcf_upload_accepted")[-1]
    assert (entry.detail["new_variants"], entry.detail["updated_variants"]) == (1, 0)
    assert upload.panel_variants == 1
    assert any("listed more than once" in warning for warning in entry.detail["warnings"])


def test_ingest_vcf_reuses_existing_variant_keys(db: SQLAlchemy, release: KbRelease) -> None:
    keys_before = db.session.scalar(select(func.count(VariantKey.id)))
    VariantKey.get_or_create("17", 37700000, "G", "A")  # e.g. imported with an earlier release
    db.session.flush()
    patient = new_patient()
    pr.ingest_vcf(patient, vcf_bytes([(HNF1A_P, "0/1"), (("17", 37700000, "G", "A"), "0/1")]), "a.vcf")
    assert db.session.scalar(select(func.count(VariantKey.id))) == keys_before + 1
    assert {variant.gene for variant in patient.variants} == {"HNF1A", "HNF1B"}


def test_ingest_vcf_distinguishes_alleles_at_the_same_position(db: SQLAlchemy, release: KbRelease) -> None:
    patient = new_patient()
    pr.ingest_vcf(patient, vcf_bytes([((HNF1A_P[0], HNF1A_P[1], "G", "T"), "0/1")]), "a.vcf")
    (variant,) = patient.variants
    assert variant.variant_key.label == "12-120978900-G-T"
    assert variant.current_classification == "NC", "a different ALT allele is a different (unlisted) variant"


def test_ingest_vcf_fills_missing_gene_of_existing_key(db: SQLAlchemy, release: KbRelease) -> None:
    key = VariantKey(chrom="20", pos=44401300, ref="C", alt="T", gene=None)
    db.session.add(key)
    db.session.flush()
    patient = new_patient()
    pr.ingest_vcf(patient, vcf_bytes([(("20", 44401300, "C", "T"), "0/1")]), "a.vcf")
    assert key.gene == "HNF4A"
    assert patient.variants[0].variant_key is key


def test_ingest_gzip_vcf(db: SQLAlchemy, release: KbRelease) -> None:
    patient = new_patient()
    upload, result = pr.ingest_vcf(patient, gzip.compress(vcf_bytes([(GCK_P, "0/1")])), "a.vcf.gz")
    assert upload.status == "accepted" and upload.panel_variants == 1
    # GCK P without glucose-lowering therapy: D1 note, D5 alert.
    assert result is not None and {(alert.rule_code, alert.kind) for alert in result.created} == {
        ("D1", "note"),
        ("D5", "alert"),
    }


@pytest.mark.parametrize(
    "data",
    [
        vcf_bytes([]),
        vcf_bytes([(OFF_PANEL, "0/1")]),
    ],
    ids=["header-only", "off-panel-only"],
)
def test_ingest_vcf_without_panel_variants_skips_evaluation(db: SQLAlchemy, release: KbRelease, data: bytes) -> None:
    patient = new_patient()
    upload, result = pr.ingest_vcf(patient, data, "empty.vcf")
    assert upload.status == "accepted" and upload.panel_variants == 0
    assert result is None and patient.variants == []
    assert len(audit_rows(db, "vcf_upload_accepted")) == 1


def test_ingest_malformed_vcf_stores_rejected_upload_and_reraises(
    db: SQLAlchemy, release: KbRelease, lab_user: User
) -> None:
    patient = new_patient()
    pr.ingest_vcf(patient, vcf_bytes([(HNF1A_P, "0/1")]), "good.vcf")
    alerts_before = [(alert.id, alert.status) for alert in patient_alerts(patient)]
    data = vcf_bytes(["12\tnot-a-number\t.\tG\tA\t50\tPASS\t.\tGT\t0/1"])

    with pytest.raises(VcfError) as excinfo:
        pr.ingest_vcf(patient, data, "../../etc/bad.vcf", user=lab_user)

    assert excinfo.value.line == 5
    upload = db.session.scalars(select(GenomicUpload).where(GenomicUpload.status == "rejected")).one()
    assert (upload.filename, upload.patient_id, upload.uploaded_by) == ("bad.vcf", patient.id, lab_user)
    assert upload.error == str(excinfo.value) and upload.error.startswith("Line 5: ")
    assert upload.size_bytes == len(data) and len(upload.sha256) == 64
    assert upload.panel_variants == 0 and upload.variants == []
    assert len(patient.variants) == 1
    assert [(alert.id, alert.status) for alert in patient_alerts(patient)] == alerts_before
    (entry,) = audit_rows(db, "vcf_upload_rejected")
    assert (entry.entity_id, entry.username) == (upload.id, lab_user.username)
    assert entry.detail["error"] == excinfo.value.message and entry.detail["line"] == 5
    assert entry.detail["filename"] == "bad.vcf"


def test_ingest_oversized_vcf_is_rejected(app: Flask, db: SQLAlchemy, release: KbRelease) -> None:
    app.config["PATIENT_VCF_MAX_BYTES"] = 1024 * 1024
    patient = new_patient()
    data = vcf_bytes([(HNF1A_P, "0/1")]) + b"#" * (1024 * 1024)
    with pytest.raises(VcfError, match=r"The file is 1\.0 MB; patient VCFs may be at most 1\.0 MB\.") as excinfo:
        pr.ingest_vcf(patient, data, "")
    assert excinfo.value.line is None
    upload = db.session.scalars(select(GenomicUpload)).one()
    assert (upload.status, upload.filename) == ("rejected", "upload.vcf")
    assert patient.variants == [] and audit_rows(db, "vcf_upload_rejected")[0].detail["line"] is None


def test_ingest_vcf_default_size_limit_comes_from_config(app: Flask) -> None:
    assert app.config["PATIENT_VCF_MAX_BYTES"] == 20 * 1024 * 1024


def test_ingest_vcf_rejected_upload_survives_caller_commit(db: SQLAlchemy, release: KbRelease) -> None:
    patient = new_patient()
    db.session.commit()
    with pytest.raises(VcfError):
        pr.ingest_vcf(patient, b"not a vcf at all", "x.vcf")
    db.session.commit()
    db.session.expire_all()
    assert db.session.scalars(select(GenomicUpload.status)).all() == ["rejected"]


def test_patients_share_variant_keys(db: SQLAlchemy, release: KbRelease) -> None:
    first, second = new_patient(), new_patient(mrn="MRN-0002")
    pr.ingest_vcf(first, vcf_bytes([(HNF1A_P, "0/1")]), "a.vcf")
    pr.ingest_vcf(second, vcf_bytes([(HNF1A_P, "1/1")]), "b.vcf")
    rows = db.session.execute(select(PatientVariant.patient_id, PatientVariant.zygosity).order_by(PatientVariant.id))
    assert rows.all() == [(first.id, "heterozygous"), (second.id, "homozygous")]
    assert db.session.scalar(select(func.count(VariantKey.id)).where(VariantKey.pos == HNF1A_P[1])) == 1
