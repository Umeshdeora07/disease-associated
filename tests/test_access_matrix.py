"""Access control of every web endpoint on the full demo dataset (SPEC §8.1, §8.4).

* **Role matrix** — every rule of ``app.url_map`` (static files aside) must be listed in
  :data:`ACCESS`. For each one, representative URLs built from the seeded demo database are
  requested by an anonymous visitor, a lab technician, a clinician and an administrator, and the
  outcome is checked against the SPEC §8.1 table: allowed, sent to the login page (401 for the
  FHIR API), or 403 with an ``access_denied`` audit row. Every test works on its own copy of the
  demo database, so the POST requests may change data.
* **No clinical content for lab technicians** — no page a lab technician can open shows alerts,
  notes, recommendations or review notices, nor links to them (S12).
* **CSRF** — with CSRF protection on, every POST endpoint rejects a request without a token.
* **Security headers** — on HTML, JSON, redirect, error and static responses.
"""

from __future__ import annotations

import re
import sqlite3
from collections.abc import Iterator

import pytest
from flask import Flask, url_for
from flask.testing import FlaskClient
from sqlalchemy import func, select
from werkzeug.routing import Rule
from werkzeug.test import TestResponse

from ge_ehr.auth.routes import PASSWORD_CHANGE_EXEMPT_ENDPOINTS
from ge_ehr.demo import seed_demo
from ge_ehr.extensions import db as _db
from ge_ehr.models import Alert, AuditLog, KbRelease, Medication, Patient, ReevaluationRun, ReviewNotice, User
from tests.conftest import build_app
from tests.demo_fixtures import DEMO_PASSWORD, restore, sign_in, text
from tests.test_security import EXPECTED_CSP, assert_security_headers

PUBLIC, SIGNED_IN, CLINICAL, ADMIN = "public", "signed in", "clinical", "admin"

#: Who may use each endpoint (SPEC §8.1). ``SIGNED_IN``: every role; ``CLINICAL``: clinicians and
#: administrators; ``ADMIN``: administrators only.
ACCESS: dict[str, str] = {
    # Sign-in, sign-out and the user's own password.
    "auth.login": PUBLIC,
    "auth.logout": SIGNED_IN,
    "auth.change_password": SIGNED_IN,
    # Dashboard (lab view without alerts) and the public about page.
    "main.dashboard": SIGNED_IN,
    "main.about": PUBLIC,
    # Patient list / demographics & genomic profile; VCF upload: every role.
    "patients.list_patients": SIGNED_IN,
    "patients.view_patient": SIGNED_IN,
    "patients.upload_vcf": SIGNED_IN,
    # Create/edit patient, diagnosis, medications: not lab technicians.
    "patients.new_patient": CLINICAL,
    "patients.edit_patient": CLINICAL,
    "patients.update_diagnosis": CLINICAL,
    "patients.add_medication": CLINICAL,
    "patients.stop_medication": CLINICAL,
    # Recommendations / alerts / notes / review notices; review and acknowledge (S12).
    "patients.recommendations": CLINICAL,
    "alerts.inbox": CLINICAL,
    "alerts.view_alert": CLINICAL,
    "alerts.review_alert": CLINICAL,
    "alerts.notices": CLINICAL,
    "alerts.acknowledge_notice": CLINICAL,
    # FHIR export.
    "api.patient_fhir": CLINICAL,
    # Knowledge base, rules and runs: view.
    "knowledge.releases": CLINICAL,
    "knowledge.view_release": CLINICAL,
    "knowledge.compare_releases": CLINICAL,
    "knowledge.rules": CLINICAL,
    "knowledge.runs": CLINICAL,
    "knowledge.view_run": CLINICAL,
    # Import/activate release, import rules, full recompute.
    "knowledge.import_release": ADMIN,
    "knowledge.activate_release": ADMIN,
    "knowledge.import_rules": ADMIN,
    "knowledge.recompute": ADMIN,
    # Users and audit log.
    "admin.users": ADMIN,
    "admin.new_user": ADMIN,
    "admin.toggle_user": ADMIN,
    "admin.reset_password": ADMIN,
    "admin.audit": ADMIN,
    # Health probe (no authentication).
    "api.health": PUBLIC,
}

#: Extra query strings requested besides the plain URL: filters must not widen access.
QUERY_VARIANTS: dict[str, list[dict[str, str]]] = {
    "patients.list_patients": [{"alerts": "1"}, {"q": "DEMO-0010"}],
    "patients.recommendations": [{"at": "2025-06-01T00:00"}],
    "alerts.inbox": [{"reclassified": "1", "status": "all"}],
    "alerts.notices": [{"status": "all"}],
    "knowledge.compare_releases": [{"all": "1"}],
    "api.patient_fhir": [{"download": "1"}],
}

#: Roles in request order (None = anonymous) with the demo account used for each.
ROLES: dict[str, str | None] = {"anonymous": None, "lab_technician": "lab.kim", "clinician": "dr.rao", "admin": "admin"}
ALLOWED_ROLES: dict[str, frozenset[str]] = {
    PUBLIC: frozenset(ROLES),
    SIGNED_IN: frozenset({"lab_technician", "clinician", "admin"}),
    CLINICAL: frozenset({"clinician", "admin"}),
    ADMIN: frozenset({"admin"}),
}

#: Content a lab technician must never see: links to alerts, notices and recommendations, alert
#: and notice cards, rule-version chips and severity badges.
LAB_FORBIDDEN_MARKUP = re.compile(
    r'href="/(?:alerts|notices)\b|/recommendations\b|class="[^"]*\b(?:finding-card|notice-card|rule-chip|badge-severity|sev-chip)\b'
    r"|Review notices?\b|Current alerts|Information notes"
)
#: Pages exempt from that scan, with the reason.
LAB_SCAN_EXEMPT: dict[str, str] = {
    "main.about": "public description of the design paper's six rules (Table III), shown to anonymous visitors too",
}


@pytest.fixture(scope="module")
def full_demo() -> Iterator[sqlite3.Connection]:
    """The full demo (rules v1 → v2, releases R1 → R2, a review notice), seeded once per module."""
    application = build_app()
    snapshot = sqlite3.connect(":memory:")
    with application.app_context():
        _db.create_all()
        seed_demo(full=True)
        _db.session.commit()
        raw = _db.engine.raw_connection()
        try:
            raw.driver_connection.backup(snapshot)
        finally:
            raw.close()
        _db.session.remove()
        _db.engine.dispose()
    yield snapshot
    snapshot.close()


@pytest.fixture
def demo(app: Flask, full_demo: sqlite3.Connection) -> Flask:
    """This test's own copy of the full demo database."""
    restore(full_demo)
    return app


def url_values() -> dict[str, int]:
    """A value for every URL argument: DEMO-0010 (superseded alerts, a review notice) and friends."""
    patient = _db.session.scalar(select(Patient).filter_by(mrn="DEMO-0010"))
    values = {
        "patient_id": patient.id,
        "medication_id": _db.session.scalar(select(Medication.id).filter_by(patient_id=patient.id)),
        "alert_id": _db.session.scalar(
            select(Alert.id).filter_by(patient_id=patient.id, status="active", kind="alert").order_by(Alert.id)
        ),
        "notice_id": _db.session.scalar(select(ReviewNotice.id).filter_by(patient_id=patient.id, status="open")),
        # A release that is not current, so that activating it does something.
        "release_id": _db.session.scalar(select(KbRelease.id).filter_by(is_current=False).order_by(KbRelease.id)),
        "run_id": _db.session.scalar(select(func.max(ReevaluationRun.id))),
        "user_id": _db.session.scalar(select(User.id).filter_by(username="dr.rao")),
    }
    assert all(value is not None for value in values.values()), values
    return values


def endpoint_rules(application: Flask) -> dict[str, list[Rule]]:
    """The URL rules of every endpoint except ``static``."""
    rules: dict[str, list[Rule]] = {}
    for rule in application.url_map.iter_rules():
        if rule.endpoint != "static":
            rules.setdefault(rule.endpoint, []).append(rule)
    return rules


def representative_requests(application: Flask, endpoint: str) -> list[tuple[str, str]]:
    """``(method, url)`` pairs exercising every method of ``endpoint`` on demo data."""
    values = url_values()
    requests: list[tuple[str, str]] = []
    with application.test_request_context():
        for rule in endpoint_rules(application)[endpoint]:
            arguments = {name: values[name] for name in rule.arguments}
            for method in sorted(rule.methods - {"HEAD", "OPTIONS"}):
                requests.append((method, url_for(endpoint, **arguments)))
                if method == "GET":
                    for query in QUERY_VARIANTS.get(endpoint, []):
                        requests.append(("GET", url_for(endpoint, **arguments, **query)))
    return requests


def client_for(application: Flask, role: str) -> FlaskClient:
    """A test client signed in as the demo account of ``role`` (anonymous: not signed in)."""
    username = ROLES[role]
    return sign_in(application, username) if username else application.test_client()


def denied_rows() -> int:
    return _db.session.scalar(select(func.count(AuditLog.id)).where(AuditLog.action == "access_denied")) or 0


def check_response(endpoint: str, role: str, method: str, url: str, response: TestResponse, denied_before: int) -> None:
    where = f"{role} {method} {url}"
    allowed = role in ALLOWED_ROLES[ACCESS[endpoint]]
    if allowed:
        assert response.status_code not in (401, 403, 404, 405) and response.status_code < 500, (where, response.status_code)
        assert denied_rows() == denied_before, f"{where}: unexpected access_denied audit row"
        if method == "GET":
            expected = 302 if endpoint == "auth.login" and role != "anonymous" else 200
            assert response.status_code == expected, (where, response.status_code)
        else:
            assert response.status_code in (200, 302, 400), (where, response.status_code)
    elif role == "anonymous":
        if endpoint.startswith("api."):
            assert response.status_code == 401 and response.is_json, (where, response.status_code)
        else:
            assert response.status_code == 302, (where, response.status_code)
            assert response.headers["Location"].startswith("/login?next="), (where, response.headers["Location"])
        assert denied_rows() == denied_before
    else:
        assert response.status_code == 403, (where, response.status_code)
        row = _db.session.scalars(
            select(AuditLog).where(AuditLog.action == "access_denied").order_by(AuditLog.id.desc())
        ).first()
        assert denied_rows() == denied_before + 1, f"{where}: no access_denied audit row"
        assert (row.detail["endpoint"], row.detail["method"], row.detail["role"]) == (endpoint, method, role), where


def test_the_matrix_lists_every_endpoint(app: Flask) -> None:
    """A new route must be added to ACCESS (and so to this matrix) before it can ship."""
    assert set(endpoint_rules(app)) == set(ACCESS)
    assert set(ACCESS.values()) <= set(ALLOWED_ROLES)


@pytest.mark.parametrize("endpoint", sorted(ACCESS))
def test_access_matrix(demo: Flask, endpoint: str) -> None:
    requests = representative_requests(demo, endpoint)
    assert requests
    for role in ROLES:
        client = client_for(demo, role)
        for method, url in requests:
            denied_before = denied_rows()
            response = client.open(url, method=method)
            check_response(endpoint, role, method, url, response, denied_before)
            scanned = endpoint not in LAB_SCAN_EXEMPT and response.mimetype == "text/html"
            if role == "lab_technician" and response.status_code == 200 and scanned:
                leak = LAB_FORBIDDEN_MARKUP.search(text(response))
                assert leak is None, f"lab technician sees {leak.group(0)!r} on {url}"


def test_lab_technician_pages_show_no_finding_text(demo: Flask) -> None:
    """No page open to lab technicians contains the text of an alert, note or review notice."""
    texts = {alert.title for alert in _db.session.scalars(select(Alert))}
    texts |= {alert.message for alert in _db.session.scalars(select(Alert))}
    texts |= {notice.message for notice in _db.session.scalars(select(ReviewNotice))}
    lab = sign_in(demo, "lab.kim")
    patient_ids = list(_db.session.scalars(select(Patient.id)))
    urls = ["/", "/patients", "/patients?alerts=1", "/account/password"]
    urls += [f"/patients/{patient_id}" for patient_id in patient_ids]
    urls += [f"/patients/{patient_id}/vcf" for patient_id in patient_ids]
    for url in urls:
        response = lab.get(url)
        assert response.status_code == 200, url
        body = text(response)
        shown = sorted(item for item in texts if item and item in body)
        assert not shown, f"{url} shows {shown}"
        assert not LAB_FORBIDDEN_MARKUP.search(body), url


def test_a_pending_password_change_blocks_every_other_endpoint(demo: Flask) -> None:
    """A user who must change the password (e.g. after an admin reset) reaches nothing else."""
    _db.session.scalar(select(User).filter_by(username="admin")).must_change_password = True
    _db.session.commit()
    client = sign_in(demo, "admin")
    audit_rows = _db.session.scalar(select(func.count(AuditLog.id)))
    for endpoint in sorted(set(ACCESS) - PASSWORD_CHANGE_EXEMPT_ENDPOINTS):
        for method, url in representative_requests(demo, endpoint):
            response = client.open(url, method=method)
            assert response.status_code == 302, (method, url, response.status_code)
            assert response.headers["Location"].startswith("/account/password"), (method, url)
    assert _db.session.scalar(select(func.count(AuditLog.id))) == audit_rows, "nothing was done"
    assert client.get("/account/password").status_code == 200


# ---------------------------------------------------------------------------
# CSRF on every POST endpoint
# ---------------------------------------------------------------------------


@pytest.fixture
def csrf_demo(full_demo: sqlite3.Connection) -> Iterator[Flask]:
    """The full demo served with CSRF protection on (as in development and production)."""
    application = build_app({"WTF_CSRF_ENABLED": True})
    with application.app_context():
        restore(full_demo)
        try:
            yield application
        finally:
            _db.session.remove()
            _db.engine.dispose()


def csrf_sign_in(application: Flask, username: str) -> FlaskClient:
    client = application.test_client()
    token = re.search(r'name="csrf_token" type="hidden" value="([^"]+)"', text(client.get("/login"))).group(1)
    response = client.post("/login", data={"username": username, "password": DEMO_PASSWORD, "csrf_token": token})
    assert response.status_code == 302 and "/login" not in response.headers["Location"]
    return client


def test_every_post_endpoint_rejects_a_missing_csrf_token(csrf_demo: Flask) -> None:
    post_endpoints = sorted(
        endpoint
        for endpoint, rules in endpoint_rules(csrf_demo).items()
        if any("POST" in rule.methods for rule in rules)
    )
    assert "auth.login" in post_endpoints and "knowledge.recompute" in post_endpoints
    admin = csrf_sign_in(csrf_demo, "admin")
    anonymous = csrf_demo.test_client()
    audit_rows = _db.session.scalar(select(func.count(AuditLog.id)))
    for endpoint in post_endpoints:
        for method, url in representative_requests(csrf_demo, endpoint):
            if method != "POST":
                continue
            for who, client in (("admin", admin), ("anonymous", anonymous)):
                response = client.post(url, data={"comment": "x", "username": "admin", "password": DEMO_PASSWORD})
                assert response.status_code == 400, (who, url, response.status_code)
                assert "security token is missing or has expired" in text(response), (who, url)
    # Nothing was done: no audit row (no login, no change, no denial) was written.
    assert _db.session.scalar(select(func.count(AuditLog.id))) == audit_rows
    assert admin.get("/").status_code == 200, "the admin is still signed in"


# ---------------------------------------------------------------------------
# Security headers on every kind of response
# ---------------------------------------------------------------------------


def test_security_headers_on_html_json_redirect_error_and_static_responses(demo: Flask) -> None:
    anonymous = demo.test_client()
    clinician = sign_in(demo, "dr.rao")
    lab = sign_in(demo, "lab.kim")
    patient_id = url_values()["patient_id"]
    responses = {
        "html": clinician.get("/"),
        "html (public)": anonymous.get("/about"),
        "json": anonymous.get("/health"),
        "fhir json": clinician.get(f"/patients/{patient_id}/fhir"),
        "redirect": anonymous.get("/patients"),
        "401 json": anonymous.get(f"/patients/{patient_id}/fhir"),
        "403 html": lab.get(f"/patients/{patient_id}/recommendations"),
        "403 fhir json": lab.get(f"/patients/{patient_id}/fhir"),
        "404 html": clinician.get("/patients/999999"),
        "404 json": anonymous.get("/no-such-page", headers={"Accept": "application/json"}),
        "405": anonymous.get("/logout"),
        "static css": anonymous.get("/static/css/app.css"),
        "static js": anonymous.get("/static/js/app.js"),
        "static font": anonymous.get("/static/vendor/bootstrap-icons/fonts/bootstrap-icons.woff2"),
        "static 404": anonymous.get("/static/no-such-file.css"),
    }
    expected_status = {"redirect": 302, "401 json": 401, "403 html": 403, "403 fhir json": 403, "404 html": 404,
                       "404 json": 404, "405": 405, "static 404": 404}
    for kind, response in responses.items():
        assert response.status_code == expected_status.get(kind, 200), (kind, response.status_code)
        assert_security_headers(response.headers)
        if not kind.startswith("static"):
            assert response.headers["Cache-Control"] == "no-store", kind
        response.close()
    assert responses["json"].is_json and responses["404 json"].is_json
    assert responses["fhir json"].mimetype == "application/fhir+json"


def test_security_headers_on_server_errors() -> None:
    application = build_app({"PROPAGATE_EXCEPTIONS": False})

    @application.route("/_test/boom")
    def boom() -> str:
        raise RuntimeError("test failure")

    with application.app_context():
        _db.create_all()
        try:
            for accept in ("text/html", "application/json"):
                response = application.test_client().get("/_test/boom", headers={"Accept": accept})
                assert response.status_code == 500
                assert response.headers["Content-Security-Policy"] == EXPECTED_CSP
                assert_security_headers(response.headers)
        finally:
            _db.session.remove()
            _db.engine.dispose()
