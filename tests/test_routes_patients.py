"""Route tests of the patients blueprint (UI-A): list, create/edit, patient page, inline diagnosis
and medication forms, VCF upload and recommendations — status codes per role, validation
messages, state changes through the services, filters, pagination and query counts.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import date, datetime, timedelta

import pytest
from flask import Flask
from flask.testing import FlaskClient
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import event, func, select

from ge_ehr.models import Alert, AuditLog, GenomicUpload, Medication, Patient, PatientVariant, ReviewNotice, utcnow
from ge_ehr.patients import queries
from ge_ehr.patients.forms import drug_suggestions
from ge_ehr.patients.routes import describe_outcome
from ge_ehr.services.alerting import ReconcileResult
from tests.factories import make_patient, make_review_notice
from tests.ui_data import GCK_LP, HNF1A_P, Clinic, build_clinic, vcf_bytes


@pytest.fixture
def clinic(db: SQLAlchemy) -> Clinic:
    return build_clinic()


@contextmanager
def count_queries(db: SQLAlchemy) -> Iterator[list[str]]:
    """Collect the SQL statements executed inside the block."""
    statements: list[str] = []

    def record(conn, cursor, statement, parameters, context, executemany) -> None:  # noqa: ANN001
        statements.append(statement)

    event.listen(db.engine, "before_cursor_execute", record)
    try:
        yield statements
    finally:
        event.remove(db.engine, "before_cursor_execute", record)


def text(response) -> str:  # noqa: ANN001
    return response.get_data(as_text=True)


def audit_actions(db: SQLAlchemy, action: str) -> list[AuditLog]:
    return list(db.session.scalars(select(AuditLog).where(AuditLog.action == action)))


# ---------------------------------------------------------------------------
# Authentication and roles
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("path", ["/patients", "/patients/new", "/patients/1", "/patients/1/vcf", "/patients/1/recommendations"])
def test_anonymous_users_are_sent_to_login(client: FlaskClient, path: str) -> None:
    response = client.get(path)
    assert response.status_code == 302
    assert "/login" in response.headers["Location"]


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("get", "/patients/new"),
        ("post", "/patients/new"),
        ("get", "/patients/{id}/edit"),
        ("post", "/patients/{id}/edit"),
        ("post", "/patients/{id}/diagnosis"),
        ("post", "/patients/{id}/medications"),
        ("post", "/patients/{id}/medications/{med}/stop"),
        ("get", "/patients/{id}/recommendations"),
    ],
)
def test_lab_technician_is_denied_clinical_routes(
    lab_client: FlaskClient, clinic: Clinic, db: SQLAlchemy, method: str, path: str
) -> None:
    medication = clinic.carrier.medications[0]
    url = path.format(id=clinic.carrier.id, med=medication.id)
    response = getattr(lab_client, method)(url)
    assert response.status_code == 403
    denied = audit_actions(db, "access_denied")
    assert denied and denied[-1].detail["path"] == url
    assert medication.end_date is None


@pytest.mark.parametrize("client_name", ["admin_client", "clinician_client", "lab_client"])
def test_every_role_can_list_view_and_open_the_upload_form(
    request: pytest.FixtureRequest, clinic: Clinic, client_name: str
) -> None:
    client: FlaskClient = request.getfixturevalue(client_name)
    for path in ("/patients", f"/patients/{clinic.carrier.id}", f"/patients/{clinic.carrier.id}/vcf"):
        assert client.get(path).status_code == 200, path


def test_missing_patient_gives_404(clinician_client: FlaskClient, db: SQLAlchemy) -> None:
    for path in ("/patients/999", "/patients/999/edit", "/patients/999/vcf", "/patients/999/recommendations"):
        response = clinician_client.get(path)
        assert response.status_code == 404, path
        assert "Patient 999 does not exist." in text(response)
    assert clinician_client.post("/patients/999/diagnosis", data={}).status_code == 404
    assert clinician_client.post("/patients/999/medications", data={}).status_code == 404


# ---------------------------------------------------------------------------
# Patient list
# ---------------------------------------------------------------------------


def test_list_shows_patients_with_aggregates_for_clinicians(clinician_client: FlaskClient, clinic: Clinic) -> None:
    body = text(clinician_client.get("/patients"))
    for mrn in ("UI-0001", "UI-0002", "UI-0003", "UI-0004"):
        assert mrn in body
    assert "Current alerts" in body
    assert "New patient" in body
    assert 'class="sev-chip sev-chip-high"' in body
    assert "Maya Okonkwo" in body and "FAM-1" in body


def test_list_hides_alerts_and_creation_from_lab_technicians(lab_client: FlaskClient, clinic: Clinic) -> None:
    body = text(lab_client.get("/patients?alerts=1"))
    assert "Current alerts" not in body
    assert "sev-chip" not in body
    assert "New patient" not in body
    assert "Has current alerts" not in body
    # The alert filter is ignored for lab technicians, so it cannot reveal who has alerts.
    assert "UI-0004" in body


def test_list_search_matches_name_and_mrn(clinician_client: FlaskClient, clinic: Clinic) -> None:
    body = text(clinician_client.get("/patients?q=lindq"))
    assert "UI-0002" in body and "UI-0001" not in body
    body = text(clinician_client.get("/patients?q=ui-0003"))
    assert "UI-0003" in body and "UI-0002" not in body
    body = text(clinician_client.get("/patients?q=Maya Okonkwo"))
    assert "UI-0001" in body and "UI-0002" not in body


def test_list_search_treats_wildcards_literally(clinician_client: FlaskClient, clinic: Clinic) -> None:
    body = text(clinician_client.get("/patients?q=%25"))
    assert "No patients match these filters" in body


def test_list_filters_by_family_diabetes_type_and_current_alerts(clinician_client: FlaskClient, clinic: Clinic) -> None:
    body = text(clinician_client.get("/patients?family=FAM-1"))
    assert "UI-0001" in body and "UI-0002" in body and "UI-0003" not in body
    assert "2 members" in body
    body = text(clinician_client.get("/patients?diabetes_type=type1"))
    assert "UI-0001" in body and "UI-0003" in body and "UI-0002" not in body
    body = text(clinician_client.get("/patients?alerts=1"))
    assert "UI-0001" in body and "UI-0003" in body and "UI-0004" not in body
    # Unknown filter values are ignored.
    body = text(clinician_client.get("/patients?diabetes_type=bogus"))
    assert "UI-0004" in body


def test_list_paginates_and_keeps_filters(app: Flask, clinician_client: FlaskClient, db: SQLAlchemy) -> None:
    app.config["ITEMS_PER_PAGE"] = 2
    for number in range(5):
        make_patient(mrn=f"PG-{number:03d}", diagnosis="type2")
    db.session.commit()
    first = text(clinician_client.get("/patients?diabetes_type=type2"))
    assert "PG-000" in first and "PG-002" not in first
    assert "Showing 1–2 of 5" in first
    assert "diabetes_type=type2" in first and "page=2" in first
    third = text(clinician_client.get("/patients?diabetes_type=type2&page=3"))
    assert "PG-004" in third and "PG-000" not in third
    beyond = clinician_client.get("/patients?page=99")
    assert beyond.status_code == 200


def test_list_on_an_empty_database(clinician_client: FlaskClient, lab_client: FlaskClient) -> None:
    body = text(clinician_client.get("/patients"))
    assert "No patients yet" in body and "New patient" in body
    assert "No patients yet" in text(lab_client.get("/patients"))


def test_list_query_count_does_not_grow_with_the_page(
    app: Flask, clinician_client: FlaskClient, db: SQLAlchemy, clinic: Clinic
) -> None:
    with count_queries(db) as few:
        clinician_client.get("/patients")
    for number in range(12):
        make_patient(mrn=f"NQ-{number:03d}", family_id=f"F{number}")
    db.session.commit()
    with count_queries(db) as many:
        clinician_client.get("/patients")
    assert len(many) == len(few)


# ---------------------------------------------------------------------------
# Create and edit
# ---------------------------------------------------------------------------

VALID_PATIENT = {
    "mrn": "NEW-0001",
    "given_name": "Grace",
    "family_name": "Mwangi",
    "sex": "female",
    "birth_date": "2010-02-03",
    "family_id": "",
    "external_id": "",
    "notes": "",
    "diabetes_type": "type1",
    "age_at_diagnosis_months": "96",
    "diagnosed_on": "",
}


def test_new_patient_form_renders(admin_client: FlaskClient) -> None:
    body = text(admin_client.get("/patients/new"))
    assert "New patient" in body and 'name="mrn"' in body and 'name="age_at_diagnosis_months"' in body


def test_create_patient(clinician_client: FlaskClient, db: SQLAlchemy) -> None:
    response = clinician_client.post("/patients/new", data=VALID_PATIENT)
    assert response.status_code == 302
    patient = db.session.scalar(select(Patient).filter_by(mrn="NEW-0001"))
    assert patient is not None and patient.is_synthetic
    assert patient.diagnosis.diabetes_type == "type1" and patient.diagnosis.age_at_diagnosis_months == 96
    assert response.headers["Location"].endswith(f"/patients/{patient.id}")
    assert audit_actions(db, "patient_created")
    page = clinician_client.get(response.headers["Location"])
    assert "Patient Grace Mwangi (NEW-0001) created." in text(page)


def test_create_patient_reports_missing_fields(clinician_client: FlaskClient, db: SQLAlchemy) -> None:
    data = dict(VALID_PATIENT, given_name="", birth_date="", age_at_diagnosis_months="ten")
    response = clinician_client.post("/patients/new", data=data)
    assert response.status_code == 400
    body = text(response)
    assert "Enter the given name." in body
    assert "Enter the birth date." in body
    assert "Enter a whole number of months" in body
    assert db.session.scalar(select(func.count(Patient.id))) == 0


def test_create_patient_shows_service_errors_next_to_the_field(
    clinician_client: FlaskClient, db: SQLAlchemy, clinic: Clinic
) -> None:
    response = clinician_client.post("/patients/new", data=dict(VALID_PATIENT, mrn="UI-0001"))
    assert response.status_code == 400
    assert re.search(r'id="mrn-error">A patient with MRN UI-0001 already exists\.', text(response))
    future = (date.today() + timedelta(days=30)).isoformat()
    response = clinician_client.post("/patients/new", data=dict(VALID_PATIENT, birth_date=future))
    assert response.status_code == 400
    assert "cannot be in the future" in text(response)
    response = clinician_client.post("/patients/new", data=dict(VALID_PATIENT, age_at_diagnosis_months="999"))
    assert response.status_code == 400
    assert "cannot exceed the patient&#39;s current age" in text(response)


def test_edit_patient_form_is_prefilled(clinician_client: FlaskClient, clinic: Clinic) -> None:
    body = text(clinician_client.get(f"/patients/{clinic.carrier.id}/edit"))
    assert 'value="UI-0001"' in body and 'value="Maya"' in body and 'value="180"' in body
    assert "Edit patient" in body


def test_edit_demographics_only(clinician_client: FlaskClient, db: SQLAlchemy, clinic: Clinic) -> None:
    patient = clinic.carrier
    data = dict(
        VALID_PATIENT,
        mrn="UI-0001",
        given_name="Maia",
        family_name="Okonkwo",
        birth_date="1980-05-17",
        family_id="FAM-1",
        age_at_diagnosis_months="180",
    )
    response = clinician_client.post(f"/patients/{patient.id}/edit", data=data, follow_redirects=True)
    assert response.status_code == 200
    assert "Patient UI-0001 updated." in text(response)
    assert "Decision support re-evaluated" not in text(response)
    db.session.refresh(patient)
    assert patient.given_name == "Maia"
    assert audit_actions(db, "patient_updated") and not audit_actions(db, "diagnosis_updated")


def test_edit_diagnosis_re_evaluates(clinician_client: FlaskClient, db: SQLAlchemy, clinic: Clinic) -> None:
    patient = clinic.carrier
    data = dict(
        VALID_PATIENT,
        mrn="UI-0001",
        given_name="Maya",
        family_name="Okonkwo",
        birth_date="1980-05-17",
        family_id="FAM-1",
        diabetes_type="monogenic",
        age_at_diagnosis_months="180",
    )
    response = clinician_client.post(f"/patients/{patient.id}/edit", data=data, follow_redirects=True)
    assert "Decision support re-evaluated" in text(response)
    db.session.refresh(patient)
    assert patient.diagnosis.diabetes_type == "monogenic"
    assert audit_actions(db, "diagnosis_updated")


def test_edit_patient_rejects_duplicate_mrn(clinician_client: FlaskClient, db: SQLAlchemy, clinic: Clinic) -> None:
    data = dict(VALID_PATIENT, mrn="UI-0002", birth_date="1980-05-17", age_at_diagnosis_months="180")
    response = clinician_client.post(f"/patients/{clinic.carrier.id}/edit", data=data)
    assert response.status_code == 400
    assert "A patient with MRN UI-0002 already exists." in text(response)
    db.session.refresh(clinic.carrier)
    assert clinic.carrier.mrn == "UI-0001"


# ---------------------------------------------------------------------------
# Patient page
# ---------------------------------------------------------------------------


def test_patient_page_for_clinicians(clinician_client: FlaskClient, clinic: Clinic) -> None:
    patient = clinic.carrier
    body = text(clinician_client.get(f"/patients/{patient.id}"))
    assert "Maya Okonkwo" in body and "UI-0001" in body and "Synthetic" in body
    # Header actions
    assert f"/patients/{patient.id}/recommendations" in body
    assert f"/patients/{patient.id}/fhir?download=1" in body
    assert f"/patients/{patient.id}/edit" in body
    # Family link and relative
    assert "/patients?family=FAM-1" in body and "Hannah Lindqvist" in body
    # Decision-support summary
    assert "Decision support" in body and "D2 v1" in body and "Advisory — for specialist review" in body
    # Diagnosis and medications
    assert "Type 1" in body and "15 years" in body and "Insulin glargine" in body
    assert f"/patients/{patient.id}/medications/{patient.medications[0].id}/stop" in body
    # Genomic profile: class, expert panel, stars, HGVS, condition and release history
    assert "12-120978900-G-A" in body and "NC_000012.12:g.120978900G&gt;A" in body
    assert "Pathogenic" in body and "Expert panel" in body and "3 of 4 review stars" in body
    assert "Maturity-onset diabetes of the young type 3" in body
    assert "UI R1 (2025-01, synthetic)" in body
    # Upload counters
    assert "UI-0001.vcf" in body and "1 off-panel" in body


def test_patient_page_hides_clinical_content_from_lab_technicians(lab_client: FlaskClient, clinic: Clinic) -> None:
    patient = clinic.carrier
    body = text(lab_client.get(f"/patients/{patient.id}"))
    assert "Upload VCF" in body and "12-120978900-G-A" in body
    for hidden in ("Decision support", "/recommendations", "/fhir", "/edit", "/stop", "Add medication", "D2 v1"):
        assert hidden not in body, hidden


def test_patient_page_without_genomic_data(clinician_client: FlaskClient, clinic: Clinic) -> None:
    body = text(clinician_client.get(f"/patients/{clinic.plain.id}"))
    assert "No genomic data yet" in body
    assert "No VCF has been uploaded" in body
    assert "No alerts — upload a VCF to run decision support." in body


def test_patient_page_shows_open_notices_and_rejected_uploads(
    clinician_client: FlaskClient, db: SQLAlchemy, clinic: Clinic
) -> None:
    patient = clinic.gck_patient
    make_review_notice(patient.variants[0])
    db.session.add(
        GenomicUpload(patient=patient, filename="broken.vcf", sha256="0" * 64, status="rejected", error="Line 3: bad")
    )
    db.session.commit()
    body = text(clinician_client.get(f"/patients/{patient.id}"))
    assert "1 open review notice." in body
    assert "Line 3: bad" in body and "Rejected" in body


# ---------------------------------------------------------------------------
# Diagnosis (inline form)
# ---------------------------------------------------------------------------


def test_update_diagnosis(clinician_client: FlaskClient, db: SQLAlchemy, clinic: Clinic) -> None:
    patient = clinic.carrier
    data = {"dx-diabetes_type": "monogenic", "dx-age_at_diagnosis_months": "170", "dx-diagnosed_on": "", "dx-notes": "C-peptide preserved"}
    response = clinician_client.post(f"/patients/{patient.id}/diagnosis", data=data)
    assert response.status_code == 302
    assert response.headers["Location"].endswith(f"/patients/{patient.id}#diagnosis")
    db.session.refresh(patient)
    assert patient.diagnosis.diabetes_type == "monogenic"
    assert patient.diagnosis.age_at_diagnosis_months == 170
    assert patient.diagnosis.notes == "C-peptide preserved"
    assert "Diagnosis updated." in text(clinician_client.get(f"/patients/{patient.id}"))


def test_update_diagnosis_validation_reopens_the_editor(clinician_client: FlaskClient, db: SQLAlchemy, clinic: Clinic) -> None:
    patient = clinic.carrier
    response = clinician_client.post(
        f"/patients/{patient.id}/diagnosis",
        data={"dx-diabetes_type": "type1", "dx-age_at_diagnosis_months": "-3"},
    )
    assert response.status_code == 400
    body = text(response)
    assert "Enter a number of months between 0 and 1,500." in body
    assert 'class="collapse inline-editor show" id="diagnosis-editor"' in body
    response = clinician_client.post(
        f"/patients/{patient.id}/diagnosis",
        data={"dx-diabetes_type": "none", "dx-age_at_diagnosis_months": "12"},
    )
    assert response.status_code == 400
    assert "must be empty when the patient has no diabetes" in text(response)
    db.session.refresh(patient)
    assert patient.diagnosis.diabetes_type == "type1"


# ---------------------------------------------------------------------------
# Medications
# ---------------------------------------------------------------------------


def test_add_medication_suggests_the_class_and_supersedes_alerts(
    clinician_client: FlaskClient, db: SQLAlchemy, clinic: Clinic
) -> None:
    patient = clinic.carrier
    data = {"med-drug_name": "Gliclazide 80 mg", "med-drug_class": "", "med-dose": "40 mg daily", "med-start_date": ""}
    response = clinician_client.post(f"/patients/{patient.id}/medications", data=data)
    assert response.status_code == 302
    assert response.headers["Location"].endswith("#medications")
    medication = db.session.scalar(select(Medication).filter_by(drug_name="Gliclazide 80 mg"))
    assert medication.drug_class == "sulfonylurea" and medication.dose == "40 mg daily"
    page = text(clinician_client.get(f"/patients/{patient.id}"))
    assert "Gliclazide 80 mg added as Sulfonylurea (suggested from the drug name)." in page
    assert "1 new note, 1 superseded." in page
    d2 = db.session.scalar(select(Alert).where(Alert.patient_id == patient.id, Alert.rule_code == "D2", Alert.kind == "alert"))
    assert d2.status == "superseded" and d2.superseded_reason == "therapy_changed"


def test_add_medication_with_an_explicit_class(clinician_client: FlaskClient, db: SQLAlchemy, clinic: Clinic) -> None:
    patient = clinic.plain
    data = {"med-drug_name": "Novel agent", "med-drug_class": "insulin", "med-start_date": "2024-01-02"}
    response = clinician_client.post(f"/patients/{patient.id}/medications", data=data, follow_redirects=True)
    assert "Novel agent added as Insulin." in text(response)
    medication = db.session.scalar(select(Medication).filter_by(drug_name="Novel agent"))
    assert medication.drug_class == "insulin" and medication.start_date == date(2024, 1, 2)


def test_add_medication_validation(clinician_client: FlaskClient, db: SQLAlchemy, clinic: Clinic) -> None:
    patient = clinic.carrier
    response = clinician_client.post(f"/patients/{patient.id}/medications", data={"med-drug_name": "  "})
    assert response.status_code == 400
    body = text(response)
    assert "Enter the drug name." in body
    assert 'class="collapse inline-editor show" id="medication-editor"' in body
    future = (date.today() + timedelta(days=5)).isoformat()
    response = clinician_client.post(
        f"/patients/{patient.id}/medications", data={"med-drug_name": "Metformin", "med-start_date": future}
    )
    assert response.status_code == 400
    assert re.search(r'id="med-start_date-error">Start date \(\S+\) cannot be in the future\.', text(response))
    response = clinician_client.post(
        f"/patients/{patient.id}/medications", data={"med-drug_name": "Metformin", "med-drug_class": "aspirin"}
    )
    assert response.status_code == 400
    assert "Not a valid choice" in text(response)
    assert db.session.scalar(select(func.count(Medication.id)).where(Medication.patient_id == patient.id)) == 1


def test_stop_medication(clinician_client: FlaskClient, db: SQLAlchemy, clinic: Clinic) -> None:
    patient = clinic.carrier
    medication = patient.medications[0]
    response = clinician_client.post(f"/patients/{patient.id}/medications/{medication.id}/stop")
    assert response.status_code == 302
    db.session.refresh(medication)
    assert medication.end_date == date.today()
    page = text(clinician_client.get(f"/patients/{patient.id}"))
    assert "Insulin glargine stopped." in page and "Stopped medications (1)" in page
    # Stopping it again is refused with a precise message.
    clinician_client.post(f"/patients/{patient.id}/medications/{medication.id}/stop")
    assert "Insulin glargine was already stopped on" in text(clinician_client.get(f"/patients/{patient.id}"))


def test_stop_medication_of_another_patient_is_404(clinician_client: FlaskClient, clinic: Clinic) -> None:
    medication = clinic.carrier.medications[0]
    response = clinician_client.post(f"/patients/{clinic.gck_patient.id}/medications/{medication.id}/stop")
    assert response.status_code == 404
    assert clinician_client.post(f"/patients/{clinic.carrier.id}/medications/999/stop").status_code == 404


def test_describe_outcome() -> None:
    assert describe_outcome(None) == "No panel variant to evaluate."
    empty = ReconcileResult(created=[], superseded=[], confirmed=[], unchanged=[], records_examined=0)
    assert describe_outcome(empty) == "Decision support re-evaluated: no change to alerts or notes."
    alert, note = Alert(kind="alert"), Alert(kind="note")
    result = ReconcileResult(created=[alert, alert, note], superseded=[alert], confirmed=[alert], unchanged=[], records_examined=3)
    assert describe_outcome(result) == (
        "Decision support re-evaluated: 2 new alerts, 1 new note, 1 superseded, 1 provenance update."
    )


def test_drug_suggestions_name_the_suggested_class() -> None:
    suggestions = dict(drug_suggestions())
    assert suggestions["Gliclazide"] == "Sulfonylurea"
    assert suggestions["Insulin glargine"] == "Insulin"
    assert suggestions["Sitagliptin"] == "Other glucose-lowering drug"


# ---------------------------------------------------------------------------
# VCF upload
# ---------------------------------------------------------------------------


def upload(client: FlaskClient, patient: Patient, data: bytes, filename: str = "sample.vcf"):  # noqa: ANN201
    from io import BytesIO

    return client.post(
        f"/patients/{patient.id}/vcf",
        data={"vcf_file": (BytesIO(data), filename)},
        content_type="multipart/form-data",
    )


def test_upload_form_lists_requirements_and_recent_uploads(lab_client: FlaskClient, clinic: Clinic) -> None:
    body = text(lab_client.get(f"/patients/{clinic.carrier.id}/vcf"))
    assert "GRCh38" in body and "VCF 4.2" in body and "NM_000545.6" in body
    assert 'accept=".vcf,.gz"' in body and 'data-max-bytes="20971520"' in body
    assert "UI-0001.vcf" in body


def test_upload_accepted_by_clinician_summarises_alerts(clinician_client: FlaskClient, db: SQLAlchemy, clinic: Clinic) -> None:
    patient = clinic.plain
    response = upload(clinician_client, patient, vcf_bytes(GCK_LP, ("1", 5000, "C", "T")), "plain.vcf")
    assert response.status_code == 302
    assert response.headers["Location"].endswith(f"/patients/{patient.id}#genomic-profile")
    page = text(clinician_client.get(response.headers["Location"]))
    assert "VCF plain.vcf accepted: 1 panel variant, 1 off-panel record skipped." in page
    assert "1 alert and 1 note created." in page
    assert db.session.scalar(select(func.count(PatientVariant.id)).where(PatientVariant.patient_id == patient.id)) == 1


def test_upload_by_lab_technician_does_not_reveal_alerts(lab_client: FlaskClient, db: SQLAlchemy, clinic: Clinic) -> None:
    response = upload(lab_client, clinic.plain, vcf_bytes(HNF1A_P), "lab.vcf")
    page = text(lab_client.get(response.headers["Location"]))
    assert "VCF lab.vcf accepted: 1 panel variant, 0 off-panel records skipped." in page
    assert "Decision support has been updated for the care team." in page
    assert "alert" not in page.split("Decision support has been updated")[0].split("accepted:")[1]


def test_upload_without_panel_variants(clinician_client: FlaskClient, clinic: Clinic) -> None:
    response = upload(clinician_client, clinic.plain, vcf_bytes(("1", 5000, "C", "T"), ("1", 6000, "G", "A")))
    page = text(clinician_client.get(response.headers["Location"]))
    assert "0 panel variants, 2 off-panel records skipped." in page
    assert "No panel variant was found, so there was nothing to evaluate." in page


def test_upload_requires_a_vcf_file(lab_client: FlaskClient, db: SQLAlchemy, clinic: Clinic) -> None:
    response = lab_client.post(f"/patients/{clinic.plain.id}/vcf", data={}, content_type="multipart/form-data")
    assert response.status_code == 400
    assert "Choose a VCF file to upload." in text(response)
    response = upload(lab_client, clinic.plain, b"hello", "notes.txt")
    assert response.status_code == 400
    assert "Choose a VCF file whose name ends in .vcf or .vcf.gz." in text(response)
    assert db.session.scalar(select(func.count(GenomicUpload.id)).where(GenomicUpload.patient_id == clinic.plain.id)) == 0


def test_upload_rejected_file_is_recorded(lab_client: FlaskClient, db: SQLAlchemy, clinic: Clinic) -> None:
    bad = vcf_bytes(HNF1A_P).replace(b"120978900", b"12O978900")
    response = upload(lab_client, clinic.plain, bad, "bad.vcf")
    assert response.status_code == 400
    body = text(response)
    assert "The file was rejected." in body and "Line 14" in body and "POS must be a positive whole number" in body
    rejected = db.session.scalar(select(GenomicUpload).where(GenomicUpload.patient_id == clinic.plain.id))
    assert rejected.status == "rejected" and "POS" in rejected.error
    assert not clinic.plain.variants
    assert audit_actions(db, "vcf_upload_rejected")


def test_upload_larger_than_the_patient_vcf_limit(app: Flask, lab_client: FlaskClient, db: SQLAlchemy, clinic: Clinic) -> None:
    app.config["PATIENT_VCF_MAX_BYTES"] = 100
    response = upload(lab_client, clinic.plain, vcf_bytes(HNF1A_P), "big.vcf")
    assert response.status_code == 400
    assert "patient VCFs may be at most" in text(response)
    assert db.session.scalar(select(GenomicUpload.status).where(GenomicUpload.patient_id == clinic.plain.id)) == "rejected"


# ---------------------------------------------------------------------------
# Recommendations
# ---------------------------------------------------------------------------


def test_recommendations_show_current_alerts_with_provenance(clinician_client: FlaskClient, clinic: Clinic) -> None:
    body = text(clinician_client.get(f"/patients/{clinic.carrier.id}/recommendations"))
    assert "Current alerts" in body
    assert "High severity" in body and "Low severity" in body
    assert body.index("D2 v1") < body.index("D5 v1")
    assert "Consider low-dose sulfonylurea as first-line treatment." in body
    assert "Kang 2021; Naylor 2024" in body
    assert "UI R1 (2025-01, synthetic)" in body
    assert "Advisory — for specialist review" in body
    assert "Provenance · 1 event" in body
    assert "Mark reviewed" in body
    assert "Nothing has been superseded for this patient yet." in body


def test_recommendations_show_notes_history_and_notices(clinician_client: FlaskClient, db: SQLAlchemy, clinic: Clinic) -> None:
    patient = clinic.carrier
    clinician_client.post(f"/patients/{patient.id}/medications", data={"med-drug_name": "Gliclazide"})
    superseded = db.session.scalar(select(Alert).where(Alert.patient_id == patient.id, Alert.status == "superseded"))
    notice = make_review_notice(patient.variants[0], alerts=[superseded])
    db.session.commit()
    body = text(clinician_client.get(f"/patients/{patient.id}/recommendations"))
    assert "Information notes" in body and "therapy condition not met" in body
    assert "Therapy changed" in body  # superseded reason in the history
    assert "Open review notices" in body
    assert f'name="ack{notice.id}-comment"' in body
    assert f"/notices/{notice.id}/acknowledge" in body


def test_recommendations_point_in_time(clinician_client: FlaskClient, db: SQLAlchemy, clinic: Clinic) -> None:
    patient = clinic.carrier
    before = utcnow() - timedelta(days=1)
    body = text(clinician_client.get(f"/patients/{patient.id}/recommendations?at={before.isoformat(timespec='minutes')}"))
    assert "Viewing the state as of" in body
    assert "No alerts at that time" in body
    assert "Mark reviewed" not in body and "History" not in body
    later = text(clinician_client.get(f"/patients/{patient.id}/recommendations?at={(utcnow() + timedelta(minutes=1)).isoformat()}Z"))
    assert "Alerts at that time" in later and "D2 v1" in later


def test_recommendations_point_in_time_shows_superseded_alerts_as_they_were(
    clinician_client: FlaskClient, db: SQLAlchemy, clinic: Clinic
) -> None:
    patient = clinic.carrier
    moment = utcnow()
    clinician_client.post(f"/patients/{patient.id}/medications", data={"med-drug_name": "Gliclazide"})
    body = text(clinician_client.get(f"/patients/{patient.id}/recommendations"))
    change_points = re.findall(r'class="pit-point[^"]*" href="([^"]+)"', body)
    assert len(change_points) == 2
    past = text(clinician_client.get(f"/patients/{patient.id}/recommendations?at={moment.isoformat()}"))
    assert "HNF1A diabetes on insulin or metformin" in past and "Active" in past


def test_recommendations_invalid_point_in_time(clinician_client: FlaskClient, clinic: Clinic) -> None:
    response = clinician_client.get(f"/patients/{clinic.carrier.id}/recommendations?at=yesterday")
    assert response.status_code == 400
    body = text(response)
    assert "&#39;yesterday&#39; is not a valid point in time." in body
    assert "Current alerts" in body


def test_recommendations_for_a_patient_without_findings(clinician_client: FlaskClient, clinic: Clinic) -> None:
    body = text(clinician_client.get(f"/patients/{clinic.plain.id}/recommendations"))
    assert "No current alerts" in body and "Upload a VCF" in body


# ---------------------------------------------------------------------------
# View-model helpers
# ---------------------------------------------------------------------------


def test_parse_point_in_time() -> None:
    assert queries.parse_point_in_time("2026-01-15T09:30") == datetime(2026, 1, 15, 9, 30)
    assert queries.parse_point_in_time(" 2026-01-15T10:30+01:00 ") == datetime(2026, 1, 15, 9, 30)
    assert queries.parse_point_in_time("2026-01-15T09:30:00Z") == datetime(2026, 1, 15, 9, 30)
    with pytest.raises(ValueError, match="is not a valid point in time"):
        queries.parse_point_in_time("15/01/2026")


@pytest.mark.parametrize(
    ("moment", "value", "step", "display"),
    [
        (datetime(2026, 1, 15, 9, 30), "2026-01-15T09:30", "60", "2026-01-15 09:30 UTC"),
        (datetime(2026, 1, 15, 9, 30, 5), "2026-01-15T09:30:05", "1", "2026-01-15 09:30:05 UTC"),
        (datetime(2026, 1, 15, 9, 30, 5, 250000), "2026-01-15T09:30:05.250", "0.001", "2026-01-15 09:30:05.250 UTC"),
    ],
)
def test_point_in_time_formatting(moment: datetime, value: str, step: str, display: str) -> None:
    assert queries.point_in_time_value(moment) == value
    assert queries.point_in_time_step(moment) == step
    assert queries.format_moment(moment) == display


def test_change_point_selects_the_state_right_after_the_change() -> None:
    point = queries.ChangePoint(at=datetime(2026, 1, 15, 9, 30, 5, 250400), label="VCF upload", run_id=None, created=2, superseded=0)
    assert point.moment == datetime(2026, 1, 15, 9, 30, 5, 251000)
    assert point.value == "2026-01-15T09:30:05.251"
    assert queries.parse_point_in_time(point.value) >= point.at


def test_age_text() -> None:
    assert queries.age_text(date(2026, 1, 20), on=date(2026, 3, 1)) == "1 month"
    assert queries.age_text(date(2025, 1, 1), on=date(2026, 3, 1)) == "14 months"
    assert queries.age_text(date(1990, 6, 1), on=date(2026, 3, 1)) == "35 years"
    assert queries.age_text(date(2026, 3, 1), on=date(2026, 2, 1)) == "0 months"


def test_patient_list_filters_from_args() -> None:
    filters = queries.PatientListFilters.from_args(
        {"q": "  Maya ", "family": "FAM-1", "diabetes_type": "type1", "alerts": "1"}, allow_alert_filter=True
    )
    assert filters == queries.PatientListFilters(q="Maya", family="FAM-1", diabetes_type="type1", with_alerts=True)
    assert filters.active
    assert filters.query_args() == {"q": "Maya", "family": "FAM-1", "diabetes_type": "type1", "alerts": "1"}
    assert not queries.PatientListFilters.from_args({"alerts": "1"}, allow_alert_filter=False).with_alerts
    assert not queries.PatientListFilters.from_args({"diabetes_type": "x"}, allow_alert_filter=True).active


def test_genomic_profile_history_and_hgvs(app: Flask, db: SQLAlchemy, clinic: Clinic) -> None:
    profile = queries.genomic_profile(clinic.carrier)
    assert profile.release.id == clinic.release.id
    assert profile.actionable_count == 1
    (row,) = profile.variants
    assert row.hgvs == "NC_000012.12:g.120978900G>A"
    assert row.known.is_expert_panel and row.known.review_stars == 3
    assert [step.classification for step in row.history] == ["P"]
    assert not row.changed_across_releases


def test_alert_summary_counts(app: Flask, db: SQLAlchemy, clinic: Clinic) -> None:
    summary = queries.alert_summary(clinic.carrier)
    assert summary.counts == {"high": 1, "low": 1}
    assert summary.total == 2 and summary.notes == 0 and summary.superseded == 0 and summary.open_notices == 0
    assert [alert.rule_code for alert in summary.top_alerts] == ["D2", "D5"]
    db.session.add(ReviewNotice(patient_id=clinic.carrier.id, variant_key_id=clinic.carrier.variants[0].variant_key_id,
                                old_classification="P", new_classification="VUS", message="m"))
    db.session.flush()
    assert queries.alert_summary(clinic.carrier).open_notices == 1
