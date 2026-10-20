"""Route tests for the API blueprint: ``GET /patients/<id>/fhir`` and ``GET /health`` (SPEC §8.2, §9)."""

from __future__ import annotations

import json
import logging
from typing import Any

import pytest
from fhir.resources.R4B.operationoutcome import OperationOutcome
from flask.testing import FlaskClient
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import select
from sqlalchemy.exc import OperationalError
from werkzeug.test import TestResponse

from ge_ehr import SECURITY_HEADERS
from ge_ehr.api import routes
from ge_ehr.api.routes import download_filename
from ge_ehr.models import AuditLog, Patient, User
from ge_ehr.services.fhir_export import validate_bundle
from tests.factories import add_known_variant, add_patient_variant, make_patient, make_release

FHIR_JSON = "application/fhir+json"


@pytest.fixture
def patient(db: SQLAlchemy) -> Patient:
    release = make_release("R1 (2025-01, synthetic)", current=True)
    add_known_variant(release, "12", 120978900, "G", "A", "P", hgvs="NC_000012.12:g.120978900G>A",
                      condition="Maturity-onset diabetes of the young type 3",
                      review_status="reviewed_by_expert_panel", review_stars=3, is_expert_panel=True)
    record = make_patient(mrn="DEMO-0001", given_name="Ana", family_name="Silva")
    add_patient_variant(record, "12", 120978900, "G", "A", classification="P", release=release)
    db.session.commit()
    return record


def fhir_audit_rows(db: SQLAlchemy) -> list[AuditLog]:
    return list(db.session.execute(select(AuditLog).filter_by(action="fhir_export")).scalars())


def assert_operation_outcome(response: TestResponse, status: int, code: str, diagnostics: str) -> None:
    assert response.status_code == status
    assert response.mimetype == FHIR_JSON
    body = json.loads(response.get_data(as_text=True))
    OperationOutcome.model_validate(body)
    assert body == {
        "resourceType": "OperationOutcome",
        "issue": [{"severity": "error", "code": code, "diagnostics": diagnostics}],
    }


# ---------------------------------------------------------------------------
# Access control and errors (all as OperationOutcome)
# ---------------------------------------------------------------------------


def test_anonymous_request_gets_401_outcome(client: FlaskClient, db: SQLAlchemy, patient: Patient) -> None:
    response = client.get(f"/patients/{patient.id}/fhir")
    assert_operation_outcome(response, 401, "login", routes.UNAUTHENTICATED_MESSAGE)
    assert fhir_audit_rows(db) == []


def test_lab_technician_gets_403_outcome_and_is_audited(
    lab_client: FlaskClient, db: SQLAlchemy, lab_user: User, patient: Patient
) -> None:
    response = lab_client.get(f"/patients/{patient.id}/fhir")
    assert_operation_outcome(response, 403, "forbidden", routes.FORBIDDEN_MESSAGE)
    denied = db.session.execute(select(AuditLog).filter_by(action="access_denied")).scalar_one()
    assert denied.user_id == lab_user.id
    assert denied.detail["endpoint"] == "api.patient_fhir"
    assert denied.detail["required_roles"] == ["admin", "clinician"]
    assert fhir_audit_rows(db) == []


def test_unknown_patient_gets_404_outcome(clinician_client: FlaskClient, db: SQLAlchemy) -> None:
    response = clinician_client.get("/patients/999/fhir")
    assert_operation_outcome(response, 404, "not-found", "Patient 999 does not exist.")
    assert fhir_audit_rows(db) == []


def test_role_is_checked_before_patient_existence(lab_client: FlaskClient) -> None:
    assert lab_client.get("/patients/999/fhir").status_code == 403


def test_invalid_bundle_gets_500_outcome_and_is_not_audited(
    clinician_client: FlaskClient, db: SQLAlchemy, patient: Patient, monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    def reject(bundle: dict[str, Any]) -> None:
        raise ValueError("Invalid FHIR R4B Bundle (1 error): entry[1].resource.status: required")

    monkeypatch.setattr(routes, "validate_bundle", reject)
    with caplog.at_level(logging.ERROR, logger="ge_ehr.api.routes"):
        response = clinician_client.get(f"/patients/{patient.id}/fhir")
    assert_operation_outcome(response, 500, "exception", routes.EXPORT_FAILED_MESSAGE)
    assert "DEMO-0001 produced an invalid Bundle" in caplog.text
    assert fhir_audit_rows(db) == []


@pytest.mark.parametrize("method", ["POST", "PUT", "DELETE"])
def test_fhir_endpoint_is_get_only(clinician_client: FlaskClient, patient: Patient, method: str) -> None:
    assert clinician_client.open(f"/patients/{patient.id}/fhir", method=method).status_code == 405


# ---------------------------------------------------------------------------
# Successful export
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("client_fixture", ["clinician_client", "admin_client"])
def test_clinical_roles_export_a_valid_bundle(
    request: pytest.FixtureRequest, db: SQLAlchemy, patient: Patient, client_fixture: str
) -> None:
    client: FlaskClient = request.getfixturevalue(client_fixture)
    response = client.get(f"/patients/{patient.id}/fhir")
    assert response.status_code == 200
    assert response.mimetype == FHIR_JSON
    assert "Content-Disposition" not in response.headers
    bundle = json.loads(response.get_data(as_text=True))
    validate_bundle(bundle)
    assert bundle["resourceType"] == "Bundle" and bundle["type"] == "collection"
    assert bundle["entry"][0]["resource"]["identifier"] == [{"system": "urn:ge-ehr:mrn", "value": "DEMO-0001"}]
    assert [e["resource"]["resourceType"] for e in bundle["entry"]] == ["Patient", "Observation", "Observation"]


def test_export_is_audited(
    clinician_client: FlaskClient, db: SQLAlchemy, clinician_user: User, patient: Patient
) -> None:
    clinician_client.get(f"/patients/{patient.id}/fhir")
    (entry,) = fhir_audit_rows(db)
    assert entry.user_id == clinician_user.id and entry.username == "dr.test"
    assert (entry.entity_type, entry.entity_id) == ("patient", patient.id)
    assert entry.detail == {"mrn": "DEMO-0001", "resources": 3, "download": False}
    assert entry.ip_address == "127.0.0.1"


def test_bundle_self_link_is_the_request_url(clinician_client: FlaskClient, patient: Patient) -> None:
    bundle = clinician_client.get(f"/patients/{patient.id}/fhir").get_json()
    assert bundle["link"] == [{"relation": "self", "url": f"http://localhost/patients/{patient.id}/fhir"}]


def test_patient_without_variants_exports_patient_only(clinician_client: FlaskClient, db: SQLAlchemy) -> None:
    record = make_patient()
    db.session.commit()
    bundle = clinician_client.get(f"/patients/{record.id}/fhir").get_json()
    assert [e["resource"]["resourceType"] for e in bundle["entry"]] == ["Patient"]


@pytest.mark.parametrize("value", ["1", "true", "YES"])
def test_download_is_served_as_attachment(
    clinician_client: FlaskClient, db: SQLAlchemy, patient: Patient, value: str
) -> None:
    response = clinician_client.get(f"/patients/{patient.id}/fhir?download={value}")
    assert response.status_code == 200
    assert response.mimetype == FHIR_JSON
    assert response.headers["Content-Disposition"] == "attachment; filename=GE-EHR_DEMO-0001_fhir.json"
    validate_bundle(json.loads(response.get_data(as_text=True)))
    assert fhir_audit_rows(db)[0].detail["download"] is True


@pytest.mark.parametrize("value", ["0", "no", ""])
def test_download_flag_off(clinician_client: FlaskClient, patient: Patient, value: str) -> None:
    response = clinician_client.get(f"/patients/{patient.id}/fhir?download={value}")
    assert response.status_code == 200 and "Content-Disposition" not in response.headers


def test_download_filename_is_sanitised(clinician_client: FlaskClient, db: SQLAlchemy) -> None:
    record = make_patient(mrn='AB/12 "x"')
    db.session.commit()
    response = clinician_client.get(f"/patients/{record.id}/fhir?download=1")
    assert response.headers["Content-Disposition"] == "attachment; filename=GE-EHR_AB_12__x__fhir.json"


@pytest.mark.parametrize(
    ("mrn", "filename"),
    [("DEMO-0001", "GE-EHR_DEMO-0001_fhir.json"), ("mrn.7_x", "GE-EHR_mrn.7_x_fhir.json"),
     ("../etc", "GE-EHR_.._etc_fhir.json"), ("Ünï 1", "GE-EHR__n__1_fhir.json")],
)
def test_download_filename(mrn: str, filename: str) -> None:
    assert download_filename(mrn) == filename


def test_security_headers_do_not_break_fhir_json(clinician_client: FlaskClient, patient: Patient) -> None:
    response = clinician_client.get(f"/patients/{patient.id}/fhir")
    for header, value in SECURITY_HEADERS.items():
        assert response.headers[header] == value
    assert response.headers["Cache-Control"] == "no-store"
    assert response.is_json and response.get_json()["resourceType"] == "Bundle"


def test_error_outcomes_carry_security_headers(client: FlaskClient) -> None:
    response = client.get("/patients/1/fhir")
    assert response.headers["X-Content-Type-Options"] == "nosniff"
    assert response.headers["Cache-Control"] == "no-store"


# ---------------------------------------------------------------------------
# Health check
# ---------------------------------------------------------------------------


def test_health_needs_no_authentication(client: FlaskClient) -> None:
    response = client.get("/health")
    assert response.status_code == 200
    assert response.mimetype == "application/json"
    assert response.get_json() == {"status": "ok", "db": "ok", "current_release": None}


def test_health_reports_the_current_release(client: FlaskClient, patient: Patient) -> None:
    assert client.get("/health").get_json()["current_release"] == "R1 (2025-01, synthetic)"


def test_health_reports_database_failure(
    client: FlaskClient, db: SQLAlchemy, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fail(*args: Any, **kwargs: Any) -> None:
        raise OperationalError("SELECT 1", {}, Exception("connection refused"))

    monkeypatch.setattr(db.session, "execute", fail)
    response = client.get("/health")
    assert response.status_code == 503
    assert response.get_json() == {"status": "error", "db": "error", "current_release": None}
