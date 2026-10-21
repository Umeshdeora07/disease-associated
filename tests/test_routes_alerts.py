"""Route tests of the alerts blueprint (UI-A): inbox (filters, ordering, pagination, roles),
alert detail with provenance, clinician review, review notices and their acknowledgement —
plus the review/acknowledge actions of :mod:`ge_ehr.alerts.review`.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import timedelta

import pytest
from flask import Flask
from flask.testing import FlaskClient
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import event, select

from ge_ehr.alerts import queries, review
from ge_ehr.alerts.review import COMMENT_MAX_LENGTH
from ge_ehr.models import Alert, AlertEvent, AuditLog, ReviewNotice, User
from ge_ehr.services import patient_records
from tests.factories import make_alert, make_review_notice, make_run
from tests.ui_data import Clinic, build_clinic


@pytest.fixture
def clinic(db: SQLAlchemy) -> Clinic:
    return build_clinic()


def text(response) -> str:  # noqa: ANN001
    return response.get_data(as_text=True)


def the_alert(db: SQLAlchemy, mrn_patient, code: str, kind: str = "alert") -> Alert:  # noqa: ANN001
    return db.session.scalar(
        select(Alert).where(Alert.patient_id == mrn_patient.id, Alert.rule_code == code, Alert.kind == kind)
        .order_by(Alert.id.desc())
    )


def audit_rows(db: SQLAlchemy, action: str) -> list[AuditLog]:
    return list(db.session.scalars(select(AuditLog).where(AuditLog.action == action)))


@contextmanager
def count_queries(db: SQLAlchemy) -> Iterator[list[str]]:
    statements: list[str] = []

    def record(conn, cursor, statement, parameters, context, executemany) -> None:  # noqa: ANN001
        statements.append(statement)

    event.listen(db.engine, "before_cursor_execute", record)
    try:
        yield statements
    finally:
        event.remove(db.engine, "before_cursor_execute", record)


@pytest.fixture
def notice(db: SQLAlchemy, clinic: Clinic) -> ReviewNotice:
    """An open review notice for the GCK carrier whose D1 alert was superseded by a downgrade."""
    patient = clinic.gck_patient
    d1 = the_alert(db, patient, "D1")
    run = make_run("clinvar_release", new_release=clinic.release, status="success")
    d1.status, d1.superseded_at, d1.superseded_reason, d1.superseded_run = "superseded", d1.created_at, "downgrade", run
    created = make_review_notice(patient.variants[0], run=run, alerts=[d1], message="GCK variant downgraded from LP to VUS.")
    db.session.commit()
    return created


# ---------------------------------------------------------------------------
# Roles
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("path", ["/alerts", "/alerts/1", "/notices"])
def test_anonymous_users_are_sent_to_login(client: FlaskClient, path: str) -> None:
    response = client.get(path)
    assert response.status_code == 302 and "/login" in response.headers["Location"]


def test_lab_technicians_cannot_see_or_act_on_alerts(
    lab_client: FlaskClient, db: SQLAlchemy, clinic: Clinic, notice: ReviewNotice
) -> None:
    alert = the_alert(db, clinic.carrier, "D2")
    requests = [
        ("get", "/alerts"),
        ("get", f"/alerts/{alert.id}"),
        ("post", f"/alerts/{alert.id}/review"),
        ("get", "/notices"),
        ("post", f"/notices/{notice.id}/acknowledge"),
    ]
    for method, path in requests:
        response = getattr(lab_client, method)(path)
        assert response.status_code == 403, path
        assert audit_rows(db, "access_denied")[-1].detail["path"] == path
    db.session.refresh(alert)
    db.session.refresh(notice)
    assert alert.status == "active" and notice.status == "open"


@pytest.mark.parametrize("client_name", ["admin_client", "clinician_client"])
def test_clinical_roles_can_open_every_page(
    request: pytest.FixtureRequest, db: SQLAlchemy, clinic: Clinic, notice: ReviewNotice, client_name: str
) -> None:
    client: FlaskClient = request.getfixturevalue(client_name)
    alert = the_alert(db, clinic.carrier, "D2")
    for path in ("/alerts", f"/alerts/{alert.id}", "/notices", "/notices?status=all"):
        assert client.get(path).status_code == 200, path


def test_pages_render_on_an_empty_database(clinician_client: FlaskClient) -> None:
    assert "No current alerts" in text(clinician_client.get("/alerts"))
    assert "No open review notices" in text(clinician_client.get("/notices"))
    assert "No acknowledged notices yet" in text(clinician_client.get("/notices?status=acknowledged"))
    assert "No review notices" in text(clinician_client.get("/notices?status=all"))
    assert clinician_client.get("/alerts/1").status_code == 404
    assert clinician_client.post("/alerts/1/review").status_code == 404
    assert clinician_client.post("/notices/1/acknowledge").status_code == 404


# ---------------------------------------------------------------------------
# Inbox
# ---------------------------------------------------------------------------


def test_inbox_lists_current_alerts_with_summary(clinician_client: FlaskClient, clinic: Clinic) -> None:
    body = text(clinician_client.get("/alerts"))
    for name in ("Maya Okonkwo", "Hannah Lindqvist", "Arjun Mehta"):
        assert name in body
    assert "D2 v1" in body and "D1 v1" in body and "D6 v1" in body
    assert "Advisory — for specialist review" in body
    assert re.search(r'stat-card-value">5<', body)  # five active alerts await review
    assert body.count('aria-label="Open ') == 5


def test_inbox_orders_by_priority_then_severity_then_date(clinician_client: FlaskClient, db: SQLAlchemy, clinic: Clinic) -> None:
    low = the_alert(db, clinic.gck_patient, "D5")
    low.priority, low.is_reclassified = "high", True
    db.session.commit()
    body = text(clinician_client.get("/alerts"))
    titles = re.findall(r'<span class="rule-chip">(D\d v\d)</span>', body)
    assert titles == ["D5 v1", "D2 v1", "D1 v1", "D5 v1", "D6 v1"]
    assert "High priority" in body and "Reclassified" in body


def test_inbox_filters(clinician_client: FlaskClient, db: SQLAlchemy, clinic: Clinic) -> None:
    def codes(query: str) -> list[str]:
        return re.findall(r'<span class="rule-chip">(D\d) v\d</span>', text(clinician_client.get("/alerts" + query)))

    assert codes("?severity=high") == ["D2"]
    assert codes("?rule=D5") == ["D5", "D5"]
    assert codes("?reclassified=1") == []
    # Superseding D2 by a therapy change: it leaves the current view, a note replaces it.
    patient_records.add_medication(clinic.carrier, drug_name="Gliclazide", drug_class="sulfonylurea")
    db.session.commit()
    assert "D2" not in codes("")
    assert codes("?status=superseded") == ["D2"]
    assert codes("?kind=note") == ["D2"]
    assert sorted(codes("?kind=all&rule=D2&status=all")) == ["D2", "D2"]
    assert codes("?status=reviewed") == []
    # Unknown values fall back to the defaults.
    assert codes("?severity=urgent&status=x&rule=D99&kind=y") == codes("")


def test_inbox_filter_links_keep_other_filters(clinician_client: FlaskClient, clinic: Clinic) -> None:
    body = text(clinician_client.get("/alerts?rule=D5&reclassified=1"))
    assert 'href="/alerts?rule=D5&amp;reclassified=1&amp;severity=high"' in body
    assert "Reset" in body
    assert "No alerts match these filters" in body


def test_inbox_paginates(app: Flask, clinician_client: FlaskClient, clinic: Clinic) -> None:
    app.config["ITEMS_PER_PAGE"] = 2
    first = text(clinician_client.get("/alerts?rule=D5"))
    assert "Showing 1–2 of 2" not in first and "2 records" in first
    page = text(clinician_client.get("/alerts"))
    assert "Showing 1–2 of 5" in page and 'href="/alerts?page=2"' in page
    last = text(clinician_client.get("/alerts?page=3"))
    assert "Showing 5–5 of 5" in last


def test_inbox_query_count_does_not_grow_with_the_page(clinician_client: FlaskClient, db: SQLAlchemy, clinic: Clinic) -> None:
    with count_queries(db) as few:
        clinician_client.get("/alerts")
    variant = clinic.carrier.variants[0]
    for _ in range(6):
        make_alert(variant, clinic.rules["D6"], status="superseded")
    db.session.commit()
    with count_queries(db) as many:
        clinician_client.get("/alerts?status=all")
    assert len(many) == len(few)


# ---------------------------------------------------------------------------
# Alert detail and review
# ---------------------------------------------------------------------------


def test_alert_detail_shows_recommendation_criteria_and_provenance(clinician_client: FlaskClient, db: SQLAlchemy, clinic: Clinic) -> None:
    alert = the_alert(db, clinic.carrier, "D2")
    body = text(clinician_client.get(f"/alerts/{alert.id}?next=/patients"))
    assert "HNF1A diabetes on insulin or metformin" in body
    assert "Consider low-dose sulfonylurea as first-line treatment." in body
    assert "HNF1A · P/LP" in body
    assert "On insulin or metformin · not on a sulfonylurea" in body
    assert "Kang 2021; Naylor 2024" in body
    assert "UI R1 (2025-01, synthetic)" in body
    assert "Created" in body and "VCF upload" in body
    assert "Mark as reviewed" in body
    assert 'href="/patients"' in body  # the Back button honours a safe next
    assert 'name="next" type="hidden" value="/patients"' in body


def test_alert_detail_ignores_unsafe_next(clinician_client: FlaskClient, db: SQLAlchemy, clinic: Clinic) -> None:
    alert = the_alert(db, clinic.carrier, "D2")
    body = text(clinician_client.get(f"/alerts/{alert.id}?next=https://evil.example/"))
    assert 'class="btn btn-outline-secondary" href="/alerts"' in body


def test_review_alert(clinician_client: FlaskClient, db: SQLAlchemy, clinician_user: User, clinic: Clinic) -> None:
    alert = the_alert(db, clinic.carrier, "D2")
    response = clinician_client.post(
        f"/alerts/{alert.id}/review", data={"comment": "  Referred to diabetes genetics clinic. ", "next": "/alerts"}
    )
    assert response.status_code == 302 and response.headers["Location"] == "/alerts"
    db.session.refresh(alert)
    assert alert.status == "reviewed" and alert.reviewed_by_id == clinician_user.id
    assert alert.review_comment == "Referred to diabetes genetics clinic."
    event = alert.events[-1]
    assert event.event == "reviewed" and event.user_id == clinician_user.id
    assert event.release_id == clinic.release.id and event.rule_version_id == clinic.rules["D2"].id
    assert event.classification == "P"
    (row,) = audit_rows(db, "alert_reviewed")
    assert row.entity_id == alert.id and row.detail["has_comment"] is True
    assert "marked as reviewed" in text(clinician_client.get("/alerts"))
    detail = text(clinician_client.get(f"/alerts/{alert.id}"))
    assert "Reviewed by" in detail and "Dr. C. Test" in detail and "Mark as reviewed" not in detail


def test_review_alert_redirects_to_the_alert_without_a_safe_next(clinician_client: FlaskClient, db: SQLAlchemy, clinic: Clinic) -> None:
    alert = the_alert(db, clinic.carrier, "D5")
    response = clinician_client.post(f"/alerts/{alert.id}/review", data={"next": "//evil.example/x"})
    assert response.headers["Location"] == f"/alerts/{alert.id}"
    db.session.refresh(alert)
    assert alert.status == "reviewed" and alert.review_comment is None


def test_review_alert_rejects_a_long_comment(clinician_client: FlaskClient, db: SQLAlchemy, clinic: Clinic) -> None:
    alert = the_alert(db, clinic.carrier, "D2")
    response = clinician_client.post(f"/alerts/{alert.id}/review", data={"comment": "x" * (COMMENT_MAX_LENGTH + 1)})
    assert response.status_code == 302
    page = text(clinician_client.get(response.headers["Location"]))
    assert "The review was not saved." in page and "Field cannot be longer than 2000 characters." in page
    db.session.refresh(alert)
    assert alert.status == "active"


def test_review_alert_twice_or_superseded(clinician_client: FlaskClient, db: SQLAlchemy, clinic: Clinic) -> None:
    alert = the_alert(db, clinic.carrier, "D2")
    clinician_client.post(f"/alerts/{alert.id}/review")
    clinician_client.post(f"/alerts/{alert.id}/review")
    assert "was already reviewed by Dr. C. Test" in text(clinician_client.get(f"/alerts/{alert.id}"))
    patient_records.add_medication(clinic.gck_patient, drug_name="Gliclazide", drug_class="sulfonylurea")
    patient_records.stop_medication(clinic.gck_patient.medications[0])
    patient_records.stop_medication(clinic.gck_patient.medications[1])
    db.session.commit()
    superseded = the_alert(db, clinic.gck_patient, "D1")
    assert superseded.status == "superseded"
    clinician_client.post(f"/alerts/{superseded.id}/review")
    page = text(clinician_client.get(f"/alerts/{superseded.id}"))
    assert "Superseded alerts cannot be reviewed" in page
    assert "Superseded on" in page and "therapy changed" in page
    assert "Superseded before it was reviewed." in page


# ---------------------------------------------------------------------------
# Review notices
# ---------------------------------------------------------------------------


def test_notices_list(clinician_client: FlaskClient, notice: ReviewNotice) -> None:
    body = text(clinician_client.get("/notices"))
    assert "Hannah Lindqvist" in body and "UI-0002" in body
    assert "GCK variant downgraded from LP to VUS." in body
    assert "7-44150500-G-A" in body and "D1 alert" in body
    assert f"Run #{notice.run_id}" in body and "UI R1 (2025-01, synthetic)" in body
    assert f'name="ack{notice.id}-comment"' in body
    assert 'name="ack%d-next" type="hidden" value="/notices"' % notice.id in body
    assert "No acknowledged notices yet" in text(clinician_client.get("/notices?status=acknowledged"))
    # An unknown status filter shows the open notices.
    assert "GCK variant downgraded" in text(clinician_client.get("/notices?status=bogus"))


def test_acknowledge_notice(clinician_client: FlaskClient, db: SQLAlchemy, clinician_user: User, notice: ReviewNotice) -> None:
    data = {f"ack{notice.id}-comment": "Therapy was not changed.", f"ack{notice.id}-next": "/notices?status=all"}
    response = clinician_client.post(f"/notices/{notice.id}/acknowledge", data=data)
    assert response.status_code == 302 and response.headers["Location"] == "/notices?status=all"
    db.session.refresh(notice)
    assert notice.status == "acknowledged" and notice.acknowledged_by_id == clinician_user.id
    assert notice.acknowledgement_comment == "Therapy was not changed."
    (row,) = audit_rows(db, "notice_acknowledged")
    assert row.entity_id == notice.id and row.detail["old_classification"] == "LP"
    page = text(clinician_client.get("/notices?status=acknowledged"))
    assert "Acknowledged by" in page and "Therapy was not changed." in page
    assert "Review notice for Hannah Lindqvist (UI-0002) acknowledged." in page
    # Acknowledging again is refused.
    clinician_client.post(f"/notices/{notice.id}/acknowledge")
    assert "This review notice was already acknowledged" in text(clinician_client.get("/notices"))


def test_acknowledge_notice_rejects_a_long_comment(clinician_client: FlaskClient, db: SQLAlchemy, notice: ReviewNotice) -> None:
    response = clinician_client.post(
        f"/notices/{notice.id}/acknowledge", data={f"ack{notice.id}-comment": "x" * (COMMENT_MAX_LENGTH + 1)}
    )
    assert response.headers["Location"] == "/notices"
    assert "The acknowledgement was not saved." in text(clinician_client.get("/notices"))
    db.session.refresh(notice)
    assert notice.status == "open"


def test_acknowledge_from_the_recommendations_page_returns_there(
    clinician_client: FlaskClient, db: SQLAlchemy, clinic: Clinic, notice: ReviewNotice
) -> None:
    target = f"/patients/{clinic.gck_patient.id}/recommendations"
    page = text(clinician_client.get(target))
    assert f'name="ack{notice.id}-next" type="hidden" value="{target}"' in page
    response = clinician_client.post(f"/notices/{notice.id}/acknowledge", data={f"ack{notice.id}-next": target})
    assert response.headers["Location"] == target
    body = text(clinician_client.get(target))
    assert "Open review notices" not in body and "Acknowledged review notices" in body


def test_csrf_protected_forms_work_with_tokens(db: SQLAlchemy, clinic: Clinic, clinician_user: User) -> None:
    from tests.conftest import build_app, login_client

    application = build_app({"WTF_CSRF_ENABLED": True})
    with application.app_context():
        client = application.test_client()
        login_page = text(client.get("/login"))
        token = re.search(r'name="csrf_token" type="hidden" value="([^"]+)"', login_page).group(1)
        client.post("/login", data={"username": clinician_user.username, "password": "Test-Pass-123!", "csrf_token": token})
        alert = db.session.scalar(select(Alert).where(Alert.rule_code == "D2"))
        page = text(client.get(f"/alerts/{alert.id}"))
        token = re.search(r'name="csrf_token" type="hidden" value="([^"]+)"', page).group(1)
        assert client.post(f"/alerts/{alert.id}/review", data={}).status_code == 400
        response = client.post(f"/alerts/{alert.id}/review", data={"csrf_token": token})
        assert response.status_code == 302


# ---------------------------------------------------------------------------
# Review service and queries
# ---------------------------------------------------------------------------


def test_review_service_errors(db: SQLAlchemy, clinic: Clinic, clinician_user: User) -> None:
    alert = the_alert(db, clinic.carrier, "D5")
    with pytest.raises(ValueError, match="Comment must be at most 2,000 characters"):
        review.review_alert(alert, comment="y" * 2001, user=clinician_user)
    assert alert.status == "active"
    event = review.review_alert(alert, comment=" ", user=clinician_user)
    assert isinstance(event, AlertEvent) and alert.review_comment is None
    with pytest.raises(ValueError, match="already reviewed by Dr. C. Test on"):
        review.review_alert(alert, user=clinician_user)


def test_acknowledge_service(db: SQLAlchemy, notice: ReviewNotice) -> None:
    review.acknowledge_notice(notice, comment=None, user=None)
    assert notice.status == "acknowledged" and notice.acknowledged_at is not None
    with pytest.raises(ValueError, match="already acknowledged by an unknown user"):
        review.acknowledge_notice(notice)


def test_inbox_filters_from_args() -> None:
    filters = queries.InboxFilters.from_args(
        {"severity": "high", "status": "all", "rule": "D2", "kind": "note", "reclassified": "1"}, ["D1", "D2"]
    )
    assert filters == queries.InboxFilters("high", "all", "D2", "note", True)
    assert not filters.is_default
    assert filters.args_with(severity="") == {"status": "all", "rule": "D2", "kind": "note", "reclassified": "1"}
    assert queries.InboxFilters.from_args({"rule": "D3"}, ["D1"]).is_default


def test_inbox_summary_and_notice_counts(db: SQLAlchemy, clinic: Clinic, notice: ReviewNotice) -> None:
    summary = queries.inbox_summary()
    assert summary.by_severity == {"high": 1, "medium": 0, "low": 2, "info": 1}
    assert summary.awaiting_review == 4 and summary.open_notices == 1
    assert summary.high_priority == 0 and summary.reclassified == 0
    assert queries.notice_counts() == {"open": 1, "acknowledged": 0, "all": 1}
    assert queries.rule_codes() == ["D1", "D2", "D5", "D6"]


def test_rule_criteria_of_an_alert(db: SQLAlchemy, clinic: Clinic) -> None:
    criteria = queries.rule_criteria(the_alert(db, clinic.vus_patient, "D6"))
    assert criteria.genetic == "GCK, HNF1A or HNF4A · VUS"
    assert criteria.clinical == "No therapy condition"


def test_notices_are_ordered_open_first(db: SQLAlchemy, clinic: Clinic, notice: ReviewNotice) -> None:
    older = make_review_notice(clinic.carrier.variants[0], status="acknowledged")
    older.created_at = notice.created_at + timedelta(minutes=5)
    db.session.flush()
    ordered = list(db.session.scalars(queries.notices_statement("all")))
    assert [item.id for item in ordered] == [notice.id, older.id]
