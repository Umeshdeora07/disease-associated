"""Tests for ge_ehr.security and the security behaviour of the app (headers, CSRF, RBAC)."""

from __future__ import annotations

import re
from collections.abc import Iterator
from datetime import date

import pytest
from flask import Flask
from flask.testing import FlaskClient
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import select

import ge_ehr
from ge_ehr import SECURITY_HEADERS, create_app
from ge_ehr.config import DEVELOPMENT_SECRET_KEY
from ge_ehr.extensions import db as _db
from ge_ehr.models import AuditLog, User
from ge_ehr.security import (
    PasswordPolicyError,
    audit,
    generate_temporary_password,
    is_safe_redirect_url,
    is_valid_username,
    normalize_username,
    password_policy_problems,
    roles_required,
    safe_next_url,
    validate_password,
)
from tests.conftest import build_app
from tests.factories import TEST_PASSWORD, make_user

EXPECTED_CSP = "default-src 'self'; img-src 'self' data:; style-src 'self'; script-src 'self'; frame-ancestors 'none'"


# ---------------------------------------------------------------------------
# Security headers
# ---------------------------------------------------------------------------


def assert_security_headers(headers) -> None:
    assert headers["Content-Security-Policy"] == EXPECTED_CSP
    assert headers["X-Content-Type-Options"] == "nosniff"
    assert headers["Referrer-Policy"] == "same-origin"
    assert headers["X-Frame-Options"] == "DENY"


def test_security_header_values_match_spec() -> None:
    assert SECURITY_HEADERS["Content-Security-Policy"] == EXPECTED_CSP


@pytest.mark.parametrize("url", ["/login", "/health", "/does-not-exist", "/static/css/app.css"])
def test_security_headers_on_every_response(client: FlaskClient, url: str) -> None:
    assert_security_headers(client.get(url).headers)


def test_pages_are_not_cached_but_static_files_are(client: FlaskClient) -> None:
    assert client.get("/login").headers["Cache-Control"] == "no-store"
    assert client.get("/static/css/app.css").headers.get("Cache-Control") != "no-store"


def test_no_hsts_outside_production(client: FlaskClient) -> None:
    assert "Strict-Transport-Security" not in client.get("/login").headers


# ---------------------------------------------------------------------------
# CSRF
# ---------------------------------------------------------------------------


@pytest.fixture
def csrf_app() -> Iterator[Flask]:
    application = build_app({"WTF_CSRF_ENABLED": True})
    with application.app_context():
        _db.create_all()
        make_user("csrf.user", role="clinician")
        _db.session.commit()
        yield application
        _db.session.remove()
        _db.drop_all()
        _db.engine.dispose()


def _csrf_token(html: str) -> str:
    match = re.search(r'name="csrf_token" type="hidden" value="([^"]+)"', html) or re.search(
        r'name="csrf_token" value="([^"]+)"', html
    )
    assert match, "page contains a CSRF token"
    return match.group(1)


def test_csrf_is_enforced_when_enabled(csrf_app: Flask) -> None:
    client = csrf_app.test_client()
    response = client.post("/login", data={"username": "csrf.user", "password": TEST_PASSWORD})
    assert response.status_code == 400
    html = response.get_data(as_text=True)
    assert "security token is missing or has expired" in html
    assert "Synthetic data only" in html, "error pages use the layout"


def test_csrf_token_allows_login_and_protects_logout(csrf_app: Flask) -> None:
    client = csrf_app.test_client()
    token = _csrf_token(client.get("/login").get_data(as_text=True))
    response = client.post("/login", data={"username": "csrf.user", "password": TEST_PASSWORD, "csrf_token": token})
    assert response.status_code == 302 and response.headers["Location"] == "/"

    assert client.post("/logout").status_code == 400, "logout without token is rejected"
    page = client.get("/account/password").get_data(as_text=True)
    logout_token = _csrf_token(page)
    assert client.post("/logout", data={"csrf_token": logout_token}).status_code == 302


def test_csrf_error_is_json_for_json_clients(csrf_app: Flask) -> None:
    response = csrf_app.test_client().post("/login", headers={"Accept": "application/json"})
    assert response.status_code == 400
    assert response.get_json()["status"] == 400


# ---------------------------------------------------------------------------
# Role-based access control
# ---------------------------------------------------------------------------


def test_lab_technician_cannot_open_recommendations(lab_client: FlaskClient, db: SQLAlchemy, lab_user: User) -> None:
    response = lab_client.get("/patients/1/recommendations")
    assert response.status_code == 403
    html = response.get_data(as_text=True)
    assert "Access denied" in html and "Synthetic data only" in html
    entry = db.session.execute(select(AuditLog).filter_by(action="access_denied")).scalar_one()
    assert entry.user_id == lab_user.id and entry.username == "lab.test"
    assert entry.detail == {
        "endpoint": "patients.recommendations",
        "path": "/patients/1/recommendations",
        "method": "GET",
        "required_roles": ["admin", "clinician"],
        "role": "lab_technician",
    }
    assert entry.ip_address == "127.0.0.1"


def test_forbidden_json_for_api_clients(lab_client: FlaskClient) -> None:
    response = lab_client.get("/patients/1/fhir")
    assert response.status_code == 403
    body = response.get_json()
    assert response.is_json and body["resourceType"] == "OperationOutcome"
    assert body["issue"][0]["code"] == "forbidden"


def test_anonymous_api_request_gets_401_json(client: FlaskClient) -> None:
    response = client.get("/patients/1/fhir")
    assert response.status_code == 401
    body = response.get_json()
    assert body["resourceType"] == "OperationOutcome"
    assert body["issue"][0]["code"] == "login"


def test_roles_required_rejects_unknown_roles() -> None:
    with pytest.raises(ValueError, match="Unknown role"):
        roles_required("admin", "doctor")


def test_roles_required_without_roles_only_requires_login(app: Flask, db: SQLAlchemy, lab_user: User) -> None:
    @roles_required()
    def view() -> str:
        return "ok"

    @roles_required("admin")
    def admin_view() -> str:
        return "admin"

    app.add_url_rule("/_any_role", view_func=view)
    app.add_url_rule("/_admin_only", view_func=admin_view)
    client = app.test_client()
    assert client.get("/_any_role").status_code == 302
    client.post("/login", data={"username": "lab.test", "password": TEST_PASSWORD})
    assert client.get("/_any_role").get_data(as_text=True) == "ok"
    assert client.get("/_admin_only").status_code == 403


# ---------------------------------------------------------------------------
# audit()
# ---------------------------------------------------------------------------


def test_audit_outside_request(db: SQLAlchemy) -> None:
    user = make_user("auditor")
    entry = audit(
        "release_imported", "kb_release", 4, detail={"date": date(2026, 1, 5), "genes": {"GCK", "INS"}}, user=user
    )
    assert entry.id is not None, "flushed"
    assert (entry.user_id, entry.username, entry.ip_address) == (user.id, "auditor", None)
    assert entry.detail == {"date": "2026-01-05", "genes": ["GCK", "INS"]}
    db.session.rollback()
    assert db.session.scalars(select(AuditLog)).all() == [], "audit() never commits"


def test_audit_without_user_or_detail(db: SQLAlchemy) -> None:
    entry = audit("demo_seeded")
    assert entry.user_id is None and entry.username is None and entry.detail is None


def test_audit_defaults_to_current_user(app: Flask, db: SQLAlchemy, clinician_user: User) -> None:
    @roles_required()
    def record() -> str:
        audit("fhir_export", "patient", 1)
        db.session.commit()
        return "done"

    app.add_url_rule("/_record", view_func=record)
    client = app.test_client()
    client.post("/login", data={"username": clinician_user.username, "password": TEST_PASSWORD})
    assert client.get("/_record").status_code == 200
    entry = db.session.execute(select(AuditLog).filter_by(action="fhir_export")).scalar_one()
    assert entry.user_id == clinician_user.id and entry.ip_address == "127.0.0.1"


# ---------------------------------------------------------------------------
# Password policy, usernames, redirects
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("password", "problems"),
    [
        ("Test-Pass-123!", []),
        ("abcdefghi1", []),
        ("short1", ["Password must be at least 10 characters long."]),
        ("abcdefghijk", ["Password must contain at least one digit."]),
        ("12345678901", ["Password must contain at least one letter."]),
        ("", ["Password must be at least 10 characters long.", "Password must contain at least one letter.", "Password must contain at least one digit."]),
        ("a1" * 65, ["Password must be at most 128 characters long."]),
    ],
)
def test_password_policy(password: str, problems: list[str]) -> None:
    assert password_policy_problems(password) == problems
    if problems:
        with pytest.raises(PasswordPolicyError) as error:
            validate_password(password)
        assert str(error.value) == " ".join(problems)
    else:
        validate_password(password)


def test_generate_temporary_password() -> None:
    passwords = {generate_temporary_password() for _ in range(20)}
    assert len(passwords) == 20
    for password in passwords:
        assert len(password) == 14
        assert password_policy_problems(password) == []
        assert not set(password) & set("0O1lI")


@pytest.mark.parametrize(
    ("username", "valid"),
    [("dr.rao", True), ("lab.kim", True), ("Admin", True), ("a_b-c.9", True), ("x", False), (".dot", False), ("has space", False), ("a" * 65, False), ("ümlaut", False)],
)
def test_username_rules(username: str, valid: bool) -> None:
    assert is_valid_username(username) is valid


def test_normalize_username() -> None:
    assert normalize_username("  Dr.Rao ") == "dr.rao"


@pytest.mark.parametrize(
    ("target", "safe"),
    [
        ("/", True),
        ("/patients?q=1&alerts=1", True),
        ("/patients/5/recommendations#history", True),
        (None, False),
        ("", False),
        ("patients", False),
        ("//evil.example", False),
        ("/\\evil.example", False),
        ("https://evil.example/", False),
        ("http:/evil.example", False),
        ("javascript:alert(1)", False),
        ("/\x00admin", False),
        ("/patients\n", False),
    ],
)
def test_is_safe_redirect_url(target: str | None, safe: bool) -> None:
    assert is_safe_redirect_url(target) is safe
    assert safe_next_url(target, "/fallback") == (target if safe else "/fallback")


# ---------------------------------------------------------------------------
# Production configuration
# ---------------------------------------------------------------------------


@pytest.fixture
def production_env(monkeypatch: pytest.MonkeyPatch, tmp_path) -> pytest.MonkeyPatch:
    """Environment for building a production app without reading a developer's .env file."""
    monkeypatch.setattr(ge_ehr, "_load_environment_file", lambda: None)
    monkeypatch.delenv("SECRET_KEY", raising=False)
    monkeypatch.delenv("TRUSTED_PROXIES", raising=False)
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'prod.db'}")
    return monkeypatch


def test_production_refuses_to_start_without_secret_key(production_env: pytest.MonkeyPatch) -> None:
    with pytest.raises(RuntimeError, match="SECRET_KEY must be set in the environment for the production configuration"):
        create_app("production")


def test_production_refuses_the_development_secret_key(production_env: pytest.MonkeyPatch) -> None:
    production_env.setenv("SECRET_KEY", DEVELOPMENT_SECRET_KEY)
    with pytest.raises(RuntimeError, match="SECRET_KEY"):
        create_app("production")


def test_production_security_settings(production_env: pytest.MonkeyPatch) -> None:
    production_env.setenv("SECRET_KEY", "a-real-secret-for-testing-only-1234567890")
    application = create_app("production")
    assert application.config["SESSION_COOKIE_SECURE"] is True
    assert application.config["SESSION_COOKIE_HTTPONLY"] is True
    assert application.config["SESSION_COOKIE_SAMESITE"] == "Lax"
    assert application.config["WTF_CSRF_ENABLED"] is True
    assert application.config["TRUSTED_PROXIES"] == 1
    with application.app_context():
        _db.create_all()
        response = application.test_client().get("/health", headers={"X-Forwarded-Proto": "https"})
        assert response.status_code == 200
        assert response.headers["Strict-Transport-Security"] == "max-age=31536000; includeSubDomains"
        assert_security_headers(response.headers)
        _db.engine.dispose()
