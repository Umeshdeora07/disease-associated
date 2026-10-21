"""Rendering tests of the admin audit-log page as polished by UI-B (``templates/admin/audit.html``):
readable action labels, linked entities, compact details with the full record on demand,
security highlighting, filters and pagination. The route itself is Foundation's.
"""

from __future__ import annotations

import re

import pytest
from flask import Flask
from flask.testing import FlaskClient
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import func, select

from ge_ehr.models import AuditLog
from ge_ehr.security import audit
from tests.demo_fixtures import (  # noqa: F401  (fixtures)
    demo_admin,
    demo_app,
    demo_clinician,
    demo_lab,
    text,
)


def rows(page: str) -> list[str]:
    """The body rows of the audit table."""
    body = page.split('<table class="table table-app align-middle audit-table', 1)[1].split("</tbody>", 1)[0]
    return re.findall(r"<tr[^>]*>(.*?)</tr>", body.split("<tbody>", 1)[1], re.S)


def test_only_administrators_see_the_audit_log(demo_clinician: FlaskClient, demo_lab: FlaskClient) -> None:
    assert demo_clinician.get("/admin/audit").status_code == 403
    assert demo_lab.get("/admin/audit").status_code == 403


def test_entities_link_to_their_pages(demo_admin: FlaskClient) -> None:
    page = text(demo_admin.get("/admin/audit?action=release_activated"))
    entries = rows(page)
    assert len(entries) == 2
    assert all('<span class="badge audit-action">release_activated</span>' in entry for entry in entries)
    assert 'href="/knowledge/releases/2">kb_release #2</a>' in entries[0]
    assert 'href="/knowledge/releases/1">kb_release #1</a>' in entries[1]
    run_page = text(demo_admin.get("/admin/audit?action=reevaluation_run"))
    assert 'href="/knowledge/runs/3">reevaluation_run #3</a>' in run_page
    patient_page = text(demo_admin.get("/admin/audit?action=patient_created"))
    assert re.search(r'href="/patients/\d+">patient #\d+</a>', patient_page)


def test_details_show_key_facts_and_the_full_record(demo_admin: FlaskClient) -> None:
    entry = rows(text(demo_admin.get("/admin/audit?action=vcf_upload_accepted")))[0]
    assert '<span class="audit-k">MRN</span> DEMO-0015' in entry
    assert '<span class="audit-k">Filename</span> DEMO-0015.vcf' in entry
    assert re.search(r"<summary>Full record \(\d+ fields\)</summary>", entry)
    assert "&#34;sha256&#34;" in entry or '"sha256"' in entry  # the whole detail as JSON
    assert 'href="/patients/' in entry and "Patient DEMO-0015" in entry  # upload → its patient


def test_nested_values_are_summarised(demo_admin: FlaskClient) -> None:
    entry = rows(text(demo_admin.get("/admin/audit?action=rules_imported")))[-1]
    assert '<span class="audit-k">Label</span> v1 (2025)' in entry
    assert "Full record" in entry  # created_versions and changes are only in the full record


def test_long_values_are_shortened_with_the_full_value_in_a_tooltip(demo_app: Flask, demo_admin: FlaskClient) -> None:
    from ge_ehr.extensions import db

    long_reason = "A deliberately long reason " * 4
    audit("login_failed", detail={"username": "someone", "reason": long_reason})
    db.session.commit()
    entry = rows(text(demo_admin.get("/admin/audit?action=login_failed")))[0]
    assert f'title="{long_reason}"' in entry and "…" in entry
    assert "row-security" in text(demo_admin.get("/admin/audit?action=login_failed"))
    assert "audit-action-security" in entry


def test_denied_access_is_highlighted_as_security_relevant(demo_lab: FlaskClient, demo_admin: FlaskClient) -> None:
    demo_lab.get("/knowledge/rules")
    page = text(demo_admin.get("/admin/audit?action=access_denied"))
    assert '<tr class="row-security">' in page
    assert '<span class="audit-k">Path</span> /knowledge/rules' in page
    assert "access_denied (security)" in page  # the action filter marks security actions


def test_filters_and_pagination(demo_app: Flask, demo_admin: FlaskClient) -> None:
    from ge_ehr.extensions import db

    page = text(demo_admin.get("/admin/audit?user=lab.kim"))
    assert rows(page) and all("lab.kim" in entry for entry in rows(page))
    assert "Clear filters" in page
    total = db.session.scalar(select(func.count(AuditLog.id)))
    assert total > 50
    first = text(demo_admin.get("/admin/audit"))
    assert len(rows(first)) == 50 and 'href="/admin/audit?page=2"' in first
    second = text(demo_admin.get("/admin/audit?page=2"))
    assert len(rows(second)) == total - 50


@pytest.mark.parametrize("query", ["action=nothing_like_this", "user=nobody"])
def test_filters_without_matches_show_an_empty_state(demo_admin: FlaskClient, query: str) -> None:
    page = text(demo_admin.get(f"/admin/audit?{query}"))
    assert "No entries match the selected filters." in page


def test_audit_page_on_a_fresh_database(admin_client: FlaskClient, db: SQLAlchemy) -> None:
    page = text(admin_client.get("/admin/audit"))
    entries = rows(page)
    assert len(entries) == 1 and ">login</span>" in entries[0] and 'href="/admin/users">user #' in entries[0]
