"""Tests for the admin blueprint: users, activation, password reset, audit log."""

from __future__ import annotations

import re
from datetime import timedelta

import pytest
from flask import Flask
from flask.testing import FlaskClient
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import func, select

from ge_ehr.models import AuditLog, User, utcnow
from ge_ehr.security import audit
from tests.conftest import LoginHelper
from tests.factories import TEST_PASSWORD, make_user

ADMIN_GET_URLS = ["/admin/users", "/admin/users/new", "/admin/audit"]


def new_user_form(**overrides: str) -> dict[str, str]:
    data = {
        "username": "new.clinician",
        "full_name": "Dr. N. Clinician",
        "email": "n.clinician@example.org",
        "role": "clinician",
        "password": "Initial-Pass-77",
        "confirm_password": "Initial-Pass-77",
        "must_change_password": "y",
    }
    data.update(overrides)
    return data


# ---------------------------------------------------------------------------
# Access control
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("url", ADMIN_GET_URLS)
def test_admin_pages_forbidden_for_other_roles(
    clinician_client: FlaskClient, lab_client: FlaskClient, db: SQLAlchemy, url: str
) -> None:
    for client in (clinician_client, lab_client):
        response = client.get(url)
        assert response.status_code == 403
        assert "Access denied" in response.get_data(as_text=True)
    denied = db.session.scalars(select(AuditLog).filter_by(action="access_denied")).all()
    assert {entry.username for entry in denied} == {"dr.test", "lab.test"}
    assert all(entry.detail["path"] == url for entry in denied)


def test_admin_post_endpoints_forbidden_for_clinician(clinician_client: FlaskClient, admin_user: User, db: SQLAlchemy) -> None:
    for url in (f"/admin/users/{admin_user.id}/toggle", f"/admin/users/{admin_user.id}/reset-password", "/admin/users/new"):
        assert clinician_client.post(url).status_code == 403
    db.session.refresh(admin_user)
    assert admin_user.active and admin_user.check_password(TEST_PASSWORD)


@pytest.mark.parametrize("url", ADMIN_GET_URLS)
def test_admin_pages_require_login(client: FlaskClient, url: str) -> None:
    response = client.get(url)
    assert response.status_code == 302 and response.headers["Location"].startswith("/login")


# ---------------------------------------------------------------------------
# Users list and creation
# ---------------------------------------------------------------------------


def test_users_list(admin_client: FlaskClient, db: SQLAlchemy, clinician_user: User, lab_user: User) -> None:
    locked = make_user("locked.user", role="clinician")
    locked.locked_until = utcnow() + timedelta(minutes=5)
    make_user("idle.user", active=False, must_change_password=True)
    db.session.commit()
    html = admin_client.get("/admin/users").get_data(as_text=True)
    for username in ("admin.test", "dr.test", "lab.test", "locked.user", "idle.user"):
        assert username in html
    assert "Lab technician" in html and "Administrator" in html
    assert "Locked" in html and "Inactive" in html and "Password change due" in html
    assert 'data-confirm="Deactivate dr.test?' in html


def test_users_list_paginates(admin_client: FlaskClient, db: SQLAlchemy) -> None:
    for index in range(55):
        make_user(f"bulk{index:02d}")
    db.session.commit()
    first = admin_client.get("/admin/users").get_data(as_text=True)
    assert "Showing 1–50 of 56" in first
    second = admin_client.get("/admin/users?page=2").get_data(as_text=True)
    assert "bulk54" in second and "Showing 51–56 of 56" in second


def test_new_user_form_renders(admin_client: FlaskClient) -> None:
    html = admin_client.get("/admin/users/new").get_data(as_text=True)
    assert 'name="username"' in html and 'name="role"' in html
    assert "Lab technician" in html
    assert re.search(r'<input checked class="form-check-input" id="must_change_password"', html), "checked by default"


def test_create_user(app: Flask, admin_client: FlaskClient, db: SQLAlchemy, admin_user: User, login: LoginHelper) -> None:
    response = admin_client.post("/admin/users/new", data=new_user_form(username="  New.Clinician "))
    assert response.status_code == 302 and response.headers["Location"] == "/admin/users"
    user = db.session.execute(select(User).filter_by(username="new.clinician")).scalar_one()
    assert (user.full_name, user.email, user.role, user.must_change_password) == (
        "Dr. N. Clinician", "n.clinician@example.org", "clinician", True,
    )
    assert user.check_password("Initial-Pass-77")
    entry = db.session.execute(select(AuditLog).filter_by(action="user_created")).scalar_one()
    assert entry.entity_id == user.id and entry.user_id == admin_user.id
    assert entry.detail == {"username": "new.clinician", "role": "clinician"}
    assert "User new.clinician (Clinician) created." in admin_client.get("/admin/users").get_data(as_text=True)
    assert login(app.test_client(), "new.clinician", "Initial-Pass-77").headers["Location"] == "/account/password"


def test_create_user_without_forced_change_and_email(admin_client: FlaskClient, db: SQLAlchemy) -> None:
    data = new_user_form(username="lab.two", role="lab_technician", email="")
    del data["must_change_password"]
    assert admin_client.post("/admin/users/new", data=data).status_code == 302
    user = db.session.execute(select(User).filter_by(username="lab.two")).scalar_one()
    assert user.email is None and user.must_change_password is False and user.role == "lab_technician"


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"username": "admin.test"}, "A user with this username already exists."),
        ({"username": "x"}, "2–64 characters"),
        ({"username": "bad name!"}, "2–64 characters"),
        ({"full_name": ""}, "Enter the user&#39;s full name."),
        ({"email": "not-an-email"}, "Enter a valid email address"),
        ({"role": "superuser"}, "Not a valid choice"),
        ({"password": "short1", "confirm_password": "short1"}, "at least 10 characters"),
        ({"password": "nodigitsatall", "confirm_password": "nodigitsatall"}, "at least one digit"),
        ({"confirm_password": "Something-Else-1"}, "The two passwords do not match."),
    ],
)
def test_create_user_validation(admin_client: FlaskClient, db: SQLAlchemy, overrides: dict[str, str], message: str) -> None:
    before = db.session.scalar(select(func.count(User.id)))
    response = admin_client.post("/admin/users/new", data=new_user_form(**overrides))
    assert response.status_code == 200
    assert message in response.get_data(as_text=True)
    assert db.session.scalar(select(func.count(User.id))) == before


# ---------------------------------------------------------------------------
# Activation toggle
# ---------------------------------------------------------------------------


def test_toggle_user(app: Flask, admin_client: FlaskClient, db: SQLAlchemy, clinician_user: User, login: LoginHelper) -> None:
    victim_client = app.test_client()
    login(victim_client, clinician_user)
    response = admin_client.post(f"/admin/users/{clinician_user.id}/toggle")
    assert response.status_code == 302
    db.session.refresh(clinician_user)
    assert clinician_user.active is False
    assert "User dr.test deactivated." in admin_client.get("/admin/users").get_data(as_text=True)
    assert victim_client.get("/account/password").status_code == 302, "deactivation ends existing sessions"

    admin_client.post(f"/admin/users/{clinician_user.id}/toggle")
    db.session.refresh(clinician_user)
    assert clinician_user.active is True
    actions = db.session.scalars(select(AuditLog.action).where(AuditLog.entity_id == clinician_user.id, AuditLog.entity_type == "user")).all()
    assert "user_deactivated" in actions and "user_activated" in actions


def test_cannot_deactivate_yourself(admin_client: FlaskClient, db: SQLAlchemy, admin_user: User) -> None:
    response = admin_client.post(f"/admin/users/{admin_user.id}/toggle", follow_redirects=True)
    assert "You cannot deactivate your own account." in response.get_data(as_text=True)
    db.session.refresh(admin_user)
    assert admin_user.active is True


def test_toggle_unknown_user_is_404(admin_client: FlaskClient) -> None:
    assert admin_client.post("/admin/users/9999/toggle").status_code == 404
    assert admin_client.post("/admin/users/9999/reset-password").status_code == 404


# ---------------------------------------------------------------------------
# Password reset
# ---------------------------------------------------------------------------


def _temporary_password(html: str) -> str:
    match = re.search(r'<code class="user-select-all">([^<]+)</code>', html)
    assert match, "temporary password is shown in the flash message"
    return match.group(1)


def test_reset_password(app: Flask, admin_client: FlaskClient, db: SQLAlchemy, clinician_user: User, login: LoginHelper) -> None:
    clinician_user.failed_logins = 4
    clinician_user.locked_until = utcnow() + timedelta(minutes=10)
    db.session.commit()
    victim_client = app.test_client()
    login(victim_client, clinician_user)  # locked: fails, no session

    response = admin_client.post(f"/admin/users/{clinician_user.id}/reset-password", follow_redirects=True)
    html = response.get_data(as_text=True)
    temporary = _temporary_password(html)
    assert "It is shown only once" in html
    assert "alert-credential" in html and 'data-autodismiss' not in html.split("alert-credential")[1].split(">")[0]
    db.session.refresh(clinician_user)
    assert clinician_user.must_change_password is True
    assert clinician_user.failed_logins == 0 and clinician_user.locked_until is None
    assert not clinician_user.check_password(TEST_PASSWORD)
    entry = db.session.execute(select(AuditLog).filter_by(action="password_reset")).scalar_one()
    assert entry.detail == {"username": "dr.test"}
    assert temporary not in str(entry.detail)

    assert temporary not in admin_client.get("/admin/users").get_data(as_text=True), "shown once"
    response = login(app.test_client(), clinician_user, temporary)
    assert response.headers["Location"] == "/account/password"


def test_reset_password_signs_out_the_user(app: Flask, admin_client: FlaskClient, clinician_user: User, login: LoginHelper) -> None:
    victim_client = app.test_client()
    login(victim_client, clinician_user)
    admin_client.post(f"/admin/users/{clinician_user.id}/reset-password")
    assert victim_client.get("/account/password").status_code == 302


def test_reset_own_password_keeps_session(admin_client: FlaskClient, db: SQLAlchemy, admin_user: User) -> None:
    admin_client.post(f"/admin/users/{admin_user.id}/reset-password")
    response = admin_client.get("/admin/users")
    assert response.status_code == 302 and response.headers["Location"].startswith("/account/password")
    assert admin_client.get("/account/password").status_code == 200


# ---------------------------------------------------------------------------
# Audit log
# ---------------------------------------------------------------------------


def test_audit_log_lists_entries_newest_first(admin_client: FlaskClient, db: SQLAlchemy, admin_user: User, clinician_user: User) -> None:
    audit("release_imported", "kb_release", 7, detail={"label": "R2 (2026-01, synthetic)"}, user=admin_user)
    db.session.commit()
    html = admin_client.get("/admin/audit").get_data(as_text=True)
    badge = '<span class="badge audit-action">{}</span>'
    assert html.index(badge.format("release_imported")) < html.index(badge.format("login"))
    assert "kb_release #7" in html
    assert "R2 (2026-01, synthetic)" in html
    assert '<option value="release_imported">' in html


def test_audit_log_filters(admin_client: FlaskClient, db: SQLAlchemy, admin_user: User, clinician_user: User) -> None:
    audit("rules_imported", detail={"rule_set": "v2"}, user=admin_user)
    audit("fhir_export", "patient", 3, user=clinician_user)
    db.session.commit()
    by_action = admin_client.get("/admin/audit?action=fhir_export").get_data(as_text=True)
    assert "patient #3" in by_action and "rule_set" not in by_action
    by_user = admin_client.get("/admin/audit?user=DR.TEST").get_data(as_text=True)
    assert "patient #3" in by_user and "rule_set" not in by_user
    assert '<option value="dr.test" selected>' in by_user
    empty = admin_client.get("/admin/audit?action=no_such_action").get_data(as_text=True)
    assert "No entries match the selected filters." in empty


def test_audit_log_pagination_keeps_filters(admin_client: FlaskClient, db: SQLAlchemy, admin_user: User) -> None:
    for index in range(60):
        audit("fhir_export", "patient", index, user=admin_user)
    db.session.commit()
    html = admin_client.get("/admin/audit?action=fhir_export").get_data(as_text=True)
    assert "Showing 1–50 of 60" in html
    assert "/admin/audit?page=2&amp;action=fhir_export" in html or "/admin/audit?action=fhir_export&amp;page=2" in html
    page_two = admin_client.get("/admin/audit?action=fhir_export&page=2").get_data(as_text=True)
    assert "Showing 51–60 of 60" in page_two
    assert "patient #0" in page_two
