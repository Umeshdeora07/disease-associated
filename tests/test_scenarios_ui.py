"""Table V scenarios S10 and S12 through the web routes, on the demo dataset (SPEC §11, §12).

* **S10** — uploading ``data/demo/vcfs/malformed.vcf`` is rejected with HTTP 400 and the
  parser's message and line number on the upload page; only the rejected-upload record (and
  its audit row) is stored — no variants, no alerts.
* **S12** — a lab technician requesting the recommendations of the S1 patient (DEMO-0001) gets
  403, and an ``access_denied`` audit row is committed; clinicians see the recommendations.

The other scenarios run on the services in ``tests/test_scenarios.py`` (Scenario WP). Each test
seeds the suite's per-test database with :func:`ge_ehr.demo.seed_demo` (rules v1 + release R1).
"""

from __future__ import annotations

from io import BytesIO
from typing import Any

import pytest
from flask import Flask
from flask.testing import FlaskClient
from flask_sqlalchemy import SQLAlchemy
from markupsafe import escape
from sqlalchemy import func, select

from ge_ehr.demo import DEMO_ADMIN, DEMO_CLINICIAN, DEMO_LAB_TECHNICIAN, DEMO_VCF_DIR, demo_password, seed_demo
from ge_ehr.extensions import db as _db
from ge_ehr.models import Alert, AuditLog, GenomicUpload, Patient, PatientVariant
from ge_ehr.services.vcf_parser import VcfError, parse_patient_vcf
from tests.conftest import login_client

MALFORMED_VCF = DEMO_VCF_DIR / "malformed.vcf"


@pytest.fixture
def demo(db: SQLAlchemy) -> dict[str, Any]:
    """The R1 demo database (15 patients, rules v1, release R1 current); returns the seed summary."""
    summary = seed_demo()
    db.session.commit()
    return summary


def sign_in(app: Flask, username: str) -> FlaskClient:
    """A test client signed in as the demo account ``username``."""
    client = app.test_client()
    response = login_client(client, username, demo_password())
    assert response.status_code == 302 and "/login" not in response.headers["Location"], f"{username} signs in"
    return client


def demo_patient(mrn: str) -> Patient:
    return _db.session.scalars(select(Patient).filter_by(mrn=mrn)).one()


def upload_malformed(client: FlaskClient, patient: Patient):  # noqa: ANN201 - werkzeug TestResponse
    return client.post(
        f"/patients/{patient.id}/vcf",
        data={"vcf_file": (BytesIO(MALFORMED_VCF.read_bytes()), MALFORMED_VCF.name)},
        content_type="multipart/form-data",
    )


def count(db: SQLAlchemy, column: Any, *criteria: Any) -> int:
    return db.session.scalar(select(func.count(column)).where(*criteria)) or 0


# ---------------------------------------------------------------------------
# S10 — malformed VCF rejected
# ---------------------------------------------------------------------------


def test_S10_malformed_vcf_is_rejected_and_only_the_rejection_is_stored(
    app: Flask, db: SQLAlchemy, demo: dict[str, Any]
) -> None:
    clinician = sign_in(app, DEMO_CLINICIAN.username)
    response = clinician.post(
        "/patients/new",
        data={
            "mrn": "DEMO-S10",
            "given_name": "Samira",
            "family_name": "Haddad",
            "sex": "female",
            "birth_date": "1991-04-09",
            "diabetes_type": "type2",
            "age_at_diagnosis_months": "300",
        },
    )
    assert response.status_code == 302
    patient = demo_patient("DEMO-S10")
    alerts_before = count(db, Alert.id)

    lab = sign_in(app, DEMO_LAB_TECHNICIAN.username)
    response = upload_malformed(lab, patient)

    assert response.status_code == 400
    with pytest.raises(VcfError) as rejected:
        parse_patient_vcf(MALFORMED_VCF.read_bytes(), MALFORMED_VCF.name)
    body = response.get_data(as_text=True)
    assert "The file was rejected." in body
    assert f'<span class="line-tag">Line {rejected.value.line}</span>' in body and rejected.value.line == 12
    assert str(escape(rejected.value.message)) in body
    assert "No variants were stored." in body

    (upload,) = db.session.scalars(select(GenomicUpload).where(GenomicUpload.patient_id == patient.id))
    assert upload.status == "rejected" and upload.filename == "malformed.vcf"
    assert upload.error == str(rejected.value)
    assert upload.panel_variants == 0 and upload.uploaded_by.username == DEMO_LAB_TECHNICIAN.username
    assert count(db, PatientVariant.id, PatientVariant.patient_id == patient.id) == 0
    assert count(db, Alert.id) == alerts_before
    (row,) = db.session.scalars(select(AuditLog).where(AuditLog.action == "vcf_upload_rejected"))
    assert row.username == DEMO_LAB_TECHNICIAN.username
    # The rejection stays visible in the patient's upload history.
    page = lab.get(f"/patients/{patient.id}").get_data(as_text=True)
    assert "malformed.vcf" in page and "Rejected" in page and str(escape(rejected.value.message)) in page


def test_S10_rejected_upload_leaves_an_existing_genomic_profile_unchanged(
    app: Flask, db: SQLAlchemy, demo: dict[str, Any]
) -> None:
    patient = demo_patient("DEMO-0001")
    variants = sorted(variant.variant_key_id for variant in patient.variants)
    current = sorted((alert.rule_code, alert.kind) for alert in patient.alerts if alert.is_current)
    alerts_total = count(db, Alert.id)

    response = upload_malformed(sign_in(app, DEMO_LAB_TECHNICIAN.username), patient)

    assert response.status_code == 400
    db.session.expire_all()
    assert sorted(variant.variant_key_id for variant in patient.variants) == variants
    assert sorted((alert.rule_code, alert.kind) for alert in patient.alerts if alert.is_current) == current
    assert count(db, Alert.id) == alerts_total
    assert [upload.status for upload in sorted(patient.uploads, key=lambda upload: upload.id)] == ["accepted", "rejected"]


# ---------------------------------------------------------------------------
# S12 — lab technician denied the recommendations
# ---------------------------------------------------------------------------


def test_S12_lab_technician_gets_403_on_recommendations_and_the_denial_is_audited(
    app: Flask, db: SQLAlchemy, demo: dict[str, Any]
) -> None:
    patient = demo_patient("DEMO-0001")
    path = f"/patients/{patient.id}/recommendations"

    response = sign_in(app, DEMO_LAB_TECHNICIAN.username).get(path)

    assert response.status_code == 403
    body = response.get_data(as_text=True)
    assert "Consider low-dose sulfonylurea" not in body and "HNF1A diabetes on insulin or metformin" not in body
    db.session.rollback()  # the audit row was committed by the access check, not left pending
    (row,) = db.session.scalars(select(AuditLog).where(AuditLog.action == "access_denied"))
    assert row.username == DEMO_LAB_TECHNICIAN.username
    assert row.detail["path"] == path and row.detail["endpoint"] == "patients.recommendations"
    assert row.detail["role"] == "lab_technician" and row.detail["required_roles"] == ["admin", "clinician"]


def test_S12_lab_technician_patient_page_offers_no_recommendations(app: Flask, demo: dict[str, Any]) -> None:
    patient = demo_patient("DEMO-0001")
    page = sign_in(app, DEMO_LAB_TECHNICIAN.username).get(f"/patients/{patient.id}")
    assert page.status_code == 200
    body = page.get_data(as_text=True)
    assert "12-120978900-G-A" in body  # the genomic profile is visible
    assert f"/patients/{patient.id}/recommendations" not in body and "Decision support" not in body


@pytest.mark.parametrize("username", [DEMO_ADMIN.username, DEMO_CLINICIAN.username])
def test_S12_clinical_roles_see_the_S1_recommendations(app: Flask, demo: dict[str, Any], username: str) -> None:
    patient = demo_patient("DEMO-0001")
    response = sign_in(app, username).get(f"/patients/{patient.id}/recommendations")
    assert response.status_code == 200
    body = response.get_data(as_text=True)
    assert "D2 v1" in body and "Consider low-dose sulfonylurea as first-line treatment." in body
    assert "R1 (2025-01, synthetic)" in body and "Advisory — for specialist review" in body
