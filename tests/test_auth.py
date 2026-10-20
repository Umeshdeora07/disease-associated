"""Tests for the auth blueprint: login, lockout, next validation, logout, password change."""

from __future__ import annotations

from datetime import timedelta
from urllib.parse import parse_qs, urlsplit

import pytest
from flask import Flask
from flask.testing import FlaskClient
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import select

from ge_ehr.auth.routes import LOGIN_FAILED_MESSAGE
from ge_ehr.models import AuditLog, User, utcnow
from tests.conftest import LoginHelper
from tests.factories import TEST_PASSWORD, make_user

NEW_PASSWORD = "Fresh-Pass-2026"


def redirect_target(location: str) -> tuple[str, str | None]:
    """(path, decoded ``next`` parameter) of a redirect Location."""
    parts = urlsplit(location)
    return parts.path, parse_qs(parts.query).get("next", [None])[0]


def audit_actions(db: SQLAlchemy, action: str) -> list[AuditLog]:
    return db.session.scalars(select(AuditLog).filter_by(action=action).order_by(AuditLog.id)).all()


# ---------------------------------------------------------------------------
# Login
# ---------------------------------------------------------------------------


def test_login_page_renders(client: FlaskClient) -> None:
    response = client.get("/login")
    assert response.status_code == 200
    html = response.get_data(as_text=True)
    assert 'name="username"' in html and 'name="password"' in html
    assert 'autocomplete="current-password"' in html
    assert "Synthetic data only · Research prototype · Not for clinical use" in html


def test_successful_login(client: FlaskClient, db: SQLAlchemy, clinician_user: User, login: LoginHelper) -> None:
    clinician_user.failed_logins = 3
    db.session.commit()
    response = login(client, clinician_user)
    assert response.status_code == 302
    assert response.headers["Location"] == "/"
    db.session.refresh(clinician_user)
    assert clinician_user.failed_logins == 0
    assert clinician_user.last_login_at is not None
    [entry] = audit_actions(db, "login")
    assert entry.user_id == clinician_user.id and entry.username == "dr.test"
    assert client.get("/account/password").status_code == 200


def test_login_session_cookie_is_httponly_and_lax(client: FlaskClient, clinician_user: User, login: LoginHelper) -> None:
    response = login(client, clinician_user)
    cookie = response.headers["Set-Cookie"]
    assert "HttpOnly" in cookie and "SameSite=Lax" in cookie
    assert "Expires=" in cookie, "permanent session limited to PERMANENT_SESSION_LIFETIME"


def test_username_is_case_insensitive(client: FlaskClient, clinician_user: User, login: LoginHelper) -> None:
    assert login(client, "  DR.Test ").status_code == 302


def test_wrong_password_shows_generic_error(
    client: FlaskClient, db: SQLAlchemy, clinician_user: User, login: LoginHelper
) -> None:
    response = login(client, clinician_user, "Wrong-Pass-999")
    assert response.status_code == 200
    assert LOGIN_FAILED_MESSAGE in response.get_data(as_text=True)
    db.session.refresh(clinician_user)
    assert clinician_user.failed_logins == 1
    [entry] = audit_actions(db, "login_failed")
    assert entry.user_id == clinician_user.id
    assert entry.detail == {"reason": "wrong_password"}


def test_unknown_user_gets_the_same_generic_error(client: FlaskClient, db: SQLAlchemy, login: LoginHelper) -> None:
    response = login(client, "Nobody", TEST_PASSWORD)
    assert response.status_code == 200
    assert LOGIN_FAILED_MESSAGE in response.get_data(as_text=True)
    [entry] = audit_actions(db, "login_failed")
    assert entry.user_id is None
    assert entry.detail == {"username": "nobody", "reason": "unknown_user"}


def test_login_form_validation_errors(client: FlaskClient) -> None:
    response = client.post("/login", data={"username": "", "password": ""})
    html = response.get_data(as_text=True)
    assert response.status_code == 200
    assert "Enter your username." in html and "Enter your password." in html
    assert "is-invalid" in html


def test_lockout_after_max_attempts(app: Flask, client: FlaskClient, db: SQLAlchemy, clinician_user: User, login: LoginHelper) -> None:
    attempts = app.config["LOGIN_MAX_ATTEMPTS"]
    for _ in range(attempts):
        assert login(client, clinician_user, "Wrong-Pass-999").status_code == 200
    db.session.refresh(clinician_user)
    assert clinician_user.is_locked()
    remaining = clinician_user.locked_until - utcnow()
    assert timedelta(minutes=app.config["LOGIN_LOCK_MINUTES"] - 1) < remaining <= timedelta(minutes=15)
    assert clinician_user.failed_logins == 0
    assert len(audit_actions(db, "account_locked")) == 1

    response = login(client, clinician_user)  # correct password, but locked
    assert response.status_code == 200
    assert LOGIN_FAILED_MESSAGE in response.get_data(as_text=True)
    assert audit_actions(db, "login_failed")[-1].detail == {"reason": "locked"}
    assert audit_actions(db, "login") == []


def test_login_works_again_after_lock_expires(client: FlaskClient, db: SQLAlchemy, clinician_user: User, login: LoginHelper) -> None:
    clinician_user.locked_until = utcnow() - timedelta(seconds=1)
    db.session.commit()
    assert login(client, clinician_user).status_code == 302
    db.session.refresh(clinician_user)
    assert clinician_user.locked_until is None


def test_failed_attempts_below_limit_do_not_lock(app: Flask, client: FlaskClient, db: SQLAlchemy, clinician_user: User, login: LoginHelper) -> None:
    for _ in range(app.config["LOGIN_MAX_ATTEMPTS"] - 1):
        login(client, clinician_user, "Wrong-Pass-999")
    assert login(client, clinician_user).status_code == 302


def test_inactive_user_cannot_sign_in(client: FlaskClient, db: SQLAlchemy, login: LoginHelper) -> None:
    make_user("gone.user", active=False)
    db.session.commit()
    response = login(client, "gone.user")
    assert response.status_code == 200
    assert LOGIN_FAILED_MESSAGE in response.get_data(as_text=True)
    assert audit_actions(db, "login_failed")[-1].detail == {"reason": "inactive"}


@pytest.mark.parametrize(
    ("next_url", "expected"),
    [
        ("/patients?q=okafor", "/patients?q=okafor"),
        ("/knowledge/runs", "/knowledge/runs"),
        ("https://evil.example/phish", "/"),
        ("//evil.example/phish", "/"),
        ("/\\evil.example", "/"),
        ("javascript:alert(1)", "/"),
        ("", "/"),
    ],
)
def test_login_next_parameter_is_validated(
    client: FlaskClient, clinician_user: User, next_url: str, expected: str
) -> None:
    response = client.post("/login", query_string={"next": next_url}, data={"username": "dr.test", "password": TEST_PASSWORD})
    assert response.status_code == 302
    assert response.headers["Location"] == expected


def test_authenticated_user_visiting_login_is_redirected(clinician_client: FlaskClient) -> None:
    response = clinician_client.get("/login", query_string={"next": "/patients"})
    assert response.status_code == 302 and response.headers["Location"] == "/patients"
    response = clinician_client.get("/login", query_string={"next": "https://evil.example"})
    assert response.headers["Location"] == "/"


def test_protected_page_redirects_anonymous_users_to_login(client: FlaskClient) -> None:
    response = client.get("/account/password")
    assert response.status_code == 302
    assert redirect_target(response.headers["Location"]) == ("/login", "/account/password")
    follow = client.get(response.headers["Location"])
    assert "Please sign in to continue." in follow.get_data(as_text=True)


def test_login_form_keeps_next_in_action(client: FlaskClient) -> None:
    html = client.get("/login?next=/patients").get_data(as_text=True)
    assert 'action="/login?next=/patients"' in html


# ---------------------------------------------------------------------------
# Logout
# ---------------------------------------------------------------------------


def test_logout_requires_post(clinician_client: FlaskClient) -> None:
    assert clinician_client.get("/logout").status_code == 405


def test_logout(clinician_client: FlaskClient, db: SQLAlchemy, clinician_user: User) -> None:
    response = clinician_client.post("/logout")
    assert response.status_code == 302 and response.headers["Location"] == "/login"
    [entry] = audit_actions(db, "logout")
    assert entry.user_id == clinician_user.id
    assert clinician_client.get("/account/password").status_code == 302
    assert "You have been signed out." in clinician_client.get("/login").get_data(as_text=True)


def test_logout_when_anonymous_redirects_to_login(client: FlaskClient) -> None:
    response = client.post("/logout")
    assert response.status_code == 302 and response.headers["Location"].startswith("/login")


# ---------------------------------------------------------------------------
# Change password
# ---------------------------------------------------------------------------


def change_password(client: FlaskClient, current: str, new: str, confirm: str | None = None, **params: str):
    return client.post(
        "/account/password",
        query_string=params,
        data={"current_password": current, "new_password": new, "confirm_password": new if confirm is None else confirm},
    )


def test_change_password_success(app: Flask, clinician_client: FlaskClient, db: SQLAlchemy, clinician_user: User, login: LoginHelper) -> None:
    response = change_password(clinician_client, TEST_PASSWORD, NEW_PASSWORD)
    assert response.status_code == 302 and response.headers["Location"] == "/"
    db.session.refresh(clinician_user)
    assert clinician_user.check_password(NEW_PASSWORD)
    assert len(audit_actions(db, "password_changed")) == 1
    assert clinician_client.get("/account/password").status_code == 200, "the session stays valid"
    fresh = app.test_client()
    assert login(fresh, clinician_user, TEST_PASSWORD).status_code == 200
    assert login(fresh, clinician_user, NEW_PASSWORD).status_code == 302


def test_change_password_redirects_to_safe_next(clinician_client: FlaskClient) -> None:
    response = change_password(clinician_client, TEST_PASSWORD, NEW_PASSWORD, next="/patients")
    assert response.headers["Location"] == "/patients"


def test_change_password_signs_out_other_sessions(app: Flask, clinician_user: User, login: LoginHelper) -> None:
    first, second = app.test_client(), app.test_client()
    login(first, clinician_user)
    login(second, clinician_user)
    assert change_password(first, TEST_PASSWORD, NEW_PASSWORD).status_code == 302
    assert first.get("/account/password").status_code == 200
    assert second.get("/account/password").status_code == 302


def test_change_password_rejects_wrong_current_password(clinician_client: FlaskClient, db: SQLAlchemy, clinician_user: User) -> None:
    response = change_password(clinician_client, "Not-My-Pass-1", NEW_PASSWORD)
    assert response.status_code == 200
    assert "Your current password is incorrect." in response.get_data(as_text=True)
    db.session.refresh(clinician_user)
    assert clinician_user.check_password(TEST_PASSWORD)
    assert audit_actions(db, "password_change_failed")[0].detail == {"reason": "wrong_current_password"}


@pytest.mark.parametrize(
    ("new", "confirm", "message"),
    [
        ("Short1", None, "at least 10 characters"),
        ("onlyletterslong", None, "at least one digit"),
        ("1234567890123", None, "at least one letter"),
        (NEW_PASSWORD, "Different-Pass-1", "The two new passwords do not match."),
        (TEST_PASSWORD, None, "must be different from the current password"),
    ],
)
def test_change_password_validation(clinician_client: FlaskClient, db: SQLAlchemy, clinician_user: User, new: str, confirm: str | None, message: str) -> None:
    response = change_password(clinician_client, TEST_PASSWORD, new, confirm)
    assert response.status_code == 200
    assert message in response.get_data(as_text=True)
    db.session.refresh(clinician_user)
    assert clinician_user.check_password(TEST_PASSWORD)


# ---------------------------------------------------------------------------
# Forced password change
# ---------------------------------------------------------------------------


@pytest.fixture
def temporary_user(db: SQLAlchemy) -> User:
    user = make_user("temp.user", role="clinician", must_change_password=True)
    db.session.commit()
    return user


def test_must_change_password_redirects_after_login(client: FlaskClient, temporary_user: User, login: LoginHelper) -> None:
    response = login(client, temporary_user)
    assert response.status_code == 302 and response.headers["Location"] == "/account/password"
    page = client.get("/account/password").get_data(as_text=True)
    assert "temporary password" in page
    assert "Please choose a new password before continuing." in page


def test_must_change_password_keeps_next(client: FlaskClient, temporary_user: User) -> None:
    response = client.post("/login?next=/patients", data={"username": "temp.user", "password": TEST_PASSWORD})
    assert redirect_target(response.headers["Location"]) == ("/account/password", "/patients")


def test_must_change_password_blocks_other_pages(client: FlaskClient, db: SQLAlchemy, temporary_user: User, login: LoginHelper) -> None:
    login(client, temporary_user)
    response = client.get("/patients?q=x")
    assert response.status_code == 302
    assert redirect_target(response.headers["Location"]) == ("/account/password", "/patients?q=x")
    assert client.get("/about").status_code == 200
    assert client.get("/health").status_code == 200

    response = change_password(client, TEST_PASSWORD, NEW_PASSWORD, next="/patients?q=x")
    assert response.headers["Location"] == "/patients?q=x"
    db.session.refresh(temporary_user)
    assert temporary_user.must_change_password is False
    assert client.get("/account/password").status_code == 200


def test_must_change_password_user_can_still_sign_out(client: FlaskClient, temporary_user: User, login: LoginHelper) -> None:
    login(client, temporary_user)
    assert client.post("/logout").headers["Location"] == "/login"
