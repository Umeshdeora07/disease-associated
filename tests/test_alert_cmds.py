"""Tests of ``flask alerts …`` (commands/alert_cmds.py): inbox, alert detail, review, notices.

The commands run with the click runner against the full demo (rules v1 → v2, R1 → R2), restored
per test from a snapshot built once per module: 21 current alerts, 7 of them reclassified (RS1,
RS6), superseded alerts from the downgrade (RS2) and one open review notice for DEMO-0010.
After a change the tests roll the session back before checking the database, which proves the
command committed (or did not).
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator

import click
import pytest
from click.testing import Result
from flask import Flask
from flask.testing import FlaskCliRunner
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import func, select

from ge_ehr import constants as C
from ge_ehr.commands import _format as F
from ge_ehr.demo import seed_demo
from ge_ehr.extensions import db as _db
from ge_ehr.models import Alert, AlertEvent, AuditLog, Patient, ReviewNotice
from tests.conftest import build_app
from tests.demo_fixtures import restore

ADVISORY = "Advisory — for specialist review"
SUBCOMMANDS = ("list", "show", "review", "notices", "acknowledge")


@pytest.fixture(scope="module")
def full_snapshot() -> Iterator[sqlite3.Connection]:
    """The full demo (rules v1 → v2, R1 → R2), built once per module."""
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
def full(app: Flask, db: SQLAlchemy, full_snapshot: sqlite3.Connection) -> Flask:
    """A per-test copy of the full demo in the test's own in-memory database."""
    restore(full_snapshot)
    return app


def run(runner: FlaskCliRunner, *args: str, **kwargs) -> Result:  # noqa: ANN003
    """Invoke ``flask alerts ARGS``."""
    return runner.invoke(args=["alerts", *args], catch_exceptions=False, **kwargs)


def text(result: Result) -> str:
    return click.unstyle(result.output)


def flat(result: Result) -> str:
    """The output with styles removed and all whitespace (line wrapping) collapsed to single spaces."""
    return " ".join(text(result).split())


def rows(result: Result) -> list[str]:
    """The table rows of ``alerts list`` (lines starting with an alert id)."""
    return [line for line in text(result).splitlines() if line.startswith("#")]


def alert_of(mrn: str, rule_code: str, *, status: str | None = None) -> Alert:
    statement = select(Alert).join(Patient).where(Patient.mrn == mrn, Alert.rule_code == rule_code)
    if status is not None:
        statement = statement.where(Alert.status == status)
    return _db.session.scalars(statement.order_by(Alert.id.desc())).first()


def audits(action: str) -> list[AuditLog]:
    return list(_db.session.scalars(select(AuditLog).where(AuditLog.action == action).order_by(AuditLog.id)))


def the_notice() -> ReviewNotice:
    return _db.session.scalars(select(ReviewNotice)).one()


def assert_fits(result: Result) -> None:
    too_long = [line for line in text(result).splitlines() if len(line) > F.WIDTH]
    assert not too_long, too_long


# ---------------------------------------------------------------------------
# Help
# ---------------------------------------------------------------------------


def test_group_help_lists_commands_and_examples(runner: FlaskCliRunner) -> None:
    result = run(runner, "--help")
    assert result.exit_code == 0
    for name in SUBCOMMANDS:
        assert name in result.output
    assert "flask alerts review 12 --as dr.rao" in result.output


@pytest.mark.parametrize("name", SUBCOMMANDS)
def test_every_command_has_help_with_examples(runner: FlaskCliRunner, name: str) -> None:
    result = run(runner, name, "--help")
    assert result.exit_code == 0, result.output
    assert "Examples:" in result.output and f"flask alerts {name}" in result.output


# ---------------------------------------------------------------------------
# list
# ---------------------------------------------------------------------------


def test_list_current_alerts_high_priority_first(runner: FlaskCliRunner, full: Flask) -> None:
    result = run(runner, "list")
    assert result.exit_code == 0, result.output
    output = text(result)
    assert output.startswith("Current alerts: 21 (4 high, 4 medium, 12 low, 1 info)\n")
    assert "21 awaiting review, 7 reclassified, 1 open review notice (flask alerts notices)." in output
    header = next(line for line in output.splitlines() if line.startswith("ID"))
    for column in ("SEV", "RULE", "PATIENT", "VARIANT", "CLASS", "STATUS", "RAISED", "FLAGS"):
        assert column in header
    table = rows(result)
    assert len(table) == 21
    # Reclassified (high priority) alerts come first: the RS1 D2 alert of DEMO-0008 heads the list.
    assert "HIGH" in table[0] and "D2 v1" in table[0] and "DEMO-0008" in table[0] and table[0].endswith("Reclassified")
    assert all(line.endswith("Reclassified") for line in table[:7])
    assert not any("Reclassified" in line for line in table[7:])
    assert "21 rows. Details: flask alerts show ID" in output
    assert output.rstrip().endswith(ADVISORY)
    assert_fits(result)


def test_list_filters(runner: FlaskCliRunner, full: Flask) -> None:
    high = rows(run(runner, "list", "--severity", "HIGH"))
    assert len(high) == 4 and all(" HIGH " in line for line in high)
    assert {line.split()[2] for line in high} == {"D2", "D4"}
    reclassified = rows(run(runner, "list", "--reclassified"))
    assert len(reclassified) == 7
    d2 = rows(run(runner, "list", "--rule", "d2"))
    assert {line.split()[4] for line in d2} == {"DEMO-0001", "DEMO-0008"}
    superseded = rows(run(runner, "list", "--status", "superseded"))
    assert superseded and all("superseded" in line for line in superseded)
    assert {"DEMO-0010"} <= {line.split()[4] for line in superseded}
    notes = rows(run(runner, "list", "--kind", "note"))
    assert notes and all(" note " in line for line in notes)
    patient_all = rows(run(runner, "list", "--status", "all", "--kind", "all", "--patient", "demo-0010"))
    assert len(patient_all) == 3 and all("DEMO-0010" in line for line in patient_all)
    limited = run(runner, "list", "--limit", "2")
    assert len(rows(limited)) == 2 and "Showing 2 of 21 alerts" in limited.output


def test_list_empty_results(runner: FlaskCliRunner, full: Flask, db: SQLAlchemy) -> None:
    result = run(runner, "list", "--severity", "info", "--reclassified")
    assert result.exit_code == 0
    assert "No alerts match these filters. Try --status all or drop a filter." in result.output
    db.session.execute(Alert.__table__.update().values(status="superseded"))
    db.session.commit()
    assert "No current alerts." in run(runner, "list").output


def test_list_rejects_unknown_rule_and_patient(runner: FlaskCliRunner, full: Flask) -> None:
    unknown_rule = run(runner, "list", "--rule", "D9")
    assert unknown_rule.exit_code == 2
    assert "unknown rule code 'D9'; known codes: D1, D2, D3, D4, D5, D6." in unknown_rule.output
    unknown_patient = run(runner, "list", "--patient", "NOPE-1")
    assert unknown_patient.exit_code == 1 and "No patient with MRN 'NOPE-1'" in unknown_patient.output
    bad_status = run(runner, "list", "--status", "open")
    assert bad_status.exit_code == 2


def test_lab_technician_may_not_list_alerts(runner: FlaskCliRunner, full: Flask, db: SQLAlchemy) -> None:
    result = run(runner, "list", "--as", "lab.kim")
    assert result.exit_code == 1
    assert "lab.kim (Lab technician) may not view alerts" in result.output
    assert "#" not in result.output
    db.session.rollback()
    entry = audits("access_denied")[-1]
    assert entry.username == "lab.kim" and entry.detail["command"] == "flask alerts list"
    assert run(runner, "list", "--as", "dr.rao").exit_code == 0


# ---------------------------------------------------------------------------
# show
# ---------------------------------------------------------------------------


def test_show_reclassified_alert_with_provenance(runner: FlaskCliRunner, full: Flask) -> None:
    alert = alert_of("DEMO-0008", "D2")
    result = run(runner, "show", str(alert.id))
    assert result.exit_code == 0, result.output
    output = text(result)
    assert output.startswith(f"Alert #{alert.id}  D2 v1  HIGH  active  Reclassified, High priority")
    assert "HNF1A diabetes on insulin or metformin" in output
    assert "Patient:          DEMO-0008 Arjun Mehta  (flask patients show DEMO-0008)" in output
    assert "Variant:          HNF1A 12-120994300-C-T - Likely pathogenic (LP) at creation" in output
    assert "Amended:          class changed since first report (1 time)" in output
    assert "Consider low-dose sulfonylurea as first-line treatment." in output
    assert "Genetic:" in output and "Clinical:" in output and "Source:" in output
    assert "Raised:           " in output and "(ClinVar release, run #" in output
    assert "Created with:     rule D2 v1; R2 (2026-01, synthetic)" in output
    assert "Notification:     clinician notified" in output
    assert f"Review:           flask alerts review {alert.id} --as USERNAME" in output
    assert "Provenance (1 event; alerts are never deleted)" in output
    event_row = next(line for line in output.splitlines() if line.strip().startswith("20") and "created" in line)
    assert "R2 (2026-01, synthetic)" in event_row and "D2 v1" in event_row and "LP" in event_row
    assert output.rstrip().endswith(ADVISORY)
    assert_fits(result)


def test_show_superseded_alert_and_its_notice(runner: FlaskCliRunner, full: Flask) -> None:
    alert = alert_of("DEMO-0010", "D1")
    output = text(run(runner, "show", str(alert.id)))
    assert f"Alert #{alert.id}  D1 v1  MEDIUM  superseded" in output
    assert "now Uncertain significance (VUS)" in output
    assert "Superseded:       " in output and "(Variant downgraded, run #" in output
    assert "kept unchanged for the record" in output
    assert f"Review notice:    #{the_notice().id} (open)" in output
    assert "flask alerts review" not in output
    assert "Provenance (2 events" in output and "superseded" in output


def test_show_note(runner: FlaskCliRunner, full: Flask) -> None:
    note = _db.session.scalars(select(Alert).where(Alert.kind == "note", Alert.status == "active")).first()
    output = text(run(runner, "show", str(note.id)))
    assert output.startswith(f"Note #{note.id}  {note.rule_code} v")
    assert "  note  active" in output.splitlines()[0]
    assert "\nNote\n" in output and "Notification:     silent (no notification)" in output


def test_show_errors(runner: FlaskCliRunner, full: Flask) -> None:
    missing = run(runner, "show", "99999")
    assert missing.exit_code == 1 and "No alert #99999" in missing.output
    not_a_number = run(runner, "show", "abc")
    assert not_a_number.exit_code == 2
    lab = run(runner, "show", str(alert_of("DEMO-0001", "D2").id), "--as", "lab.kim")
    assert lab.exit_code == 1 and "may not view alerts" in lab.output


# ---------------------------------------------------------------------------
# review
# ---------------------------------------------------------------------------


def test_review_marks_the_alert_and_commits(runner: FlaskCliRunner, full: Flask, db: SQLAlchemy) -> None:
    alert_id = alert_of("DEMO-0008", "D2").id
    result = run(runner, "review", str(alert_id), "--as", "dr.rao", "--comment", "Referred to genetics clinic")
    assert result.exit_code == 0, result.output
    output = text(result)
    assert f"Marked alert #{alert_id} (D2 v1 HIGH, DEMO-0008 Arjun Mehta) as reviewed by dr.rao at" in flat(result)
    assert "Comment:          Referred to genetics clinic" in output
    db.session.rollback()
    alert = db.session.get(Alert, alert_id)
    assert alert.status == "reviewed" and alert.reviewed_by.username == "dr.rao"
    assert alert.review_comment == "Referred to genetics clinic"
    assert [event.event for event in alert.events] == ["created", "reviewed"]
    entry = audits("alert_reviewed")[-1]
    assert entry.username == "dr.rao" and entry.entity_id == alert_id and entry.detail["has_comment"] is True
    shown = text(run(runner, "show", str(alert_id)))
    assert "Reviewed:         " in shown and "by dr.rao" in shown
    assert "Comment:          Referred to genetics clinic" in shown
    assert "Reviewed by Dr. A. Rao. Comment: Referred to genetics clinic" in shown


def test_review_errors_change_nothing(runner: FlaskCliRunner, full: Flask, db: SQLAlchemy) -> None:
    alert_id = alert_of("DEMO-0001", "D2").id
    assert run(runner, "review", str(alert_id), "--as", "admin").exit_code == 0
    again = run(runner, "review", str(alert_id), "--as", "dr.rao")
    assert again.exit_code == 1
    assert f"Could not review alert #{alert_id}: This alert was already reviewed by Demo Administrator" in again.output
    superseded = alert_of("DEMO-0010", "D1", status="superseded")
    refused = run(runner, "review", str(superseded.id), "--as", "dr.rao")
    assert refused.exit_code == 1 and "Superseded alerts cannot be reviewed" in refused.output
    too_long = run(runner, "review", str(alert_of("DEMO-0003", "D1").id), "--as", "dr.rao", "--comment", "x" * 2001)
    assert too_long.exit_code == 1 and "at most 2,000 characters" in too_long.output
    missing_as = run(runner, "review", str(alert_id))
    assert missing_as.exit_code == 2 and "Missing option '--as'" in missing_as.output
    db.session.rollback()
    assert db.session.get(Alert, alert_id).reviewed_by.username == "admin"
    assert db.session.get(Alert, superseded.id).status == "superseded"
    assert alert_of("DEMO-0003", "D1").status == "active"
    assert len(audits("alert_reviewed")) == 1


def test_lab_technician_may_not_review(runner: FlaskCliRunner, full: Flask, db: SQLAlchemy) -> None:
    alert_id = alert_of("DEMO-0001", "D2").id
    result = run(runner, "review", str(alert_id), "--as", "lab.kim")
    assert result.exit_code == 1
    assert "lab.kim (Lab technician) may not review alerts; that needs an administrator or clinician account." in (
        result.output
    )
    db.session.rollback()
    assert db.session.get(Alert, alert_id).status == "active"
    assert audits("alert_reviewed") == []
    entry = audits("access_denied")[-1]
    assert entry.detail == {
        "command": "flask alerts review",
        "required_roles": list(C.CLINICAL_ROLES),
        "role": "lab_technician",
        "via": "cli",
    }


# ---------------------------------------------------------------------------
# notices / acknowledge
# ---------------------------------------------------------------------------


def test_notices_lists_the_open_notice(runner: FlaskCliRunner, full: Flask) -> None:
    notice = the_notice()
    result = run(runner, "notices")
    assert result.exit_code == 0, result.output
    output = text(result)
    assert output.startswith("Review notices: 1 open, 0 acknowledged.")
    assert f"#{notice.id}  open  " in output and "DEMO-0010 Marcus Bell" in output
    assert "GCK 7-44150500-G-A: LP -> VUS in R2 (2026-01, synthetic) (run #" in output
    assert "GE-EHR never" in output and "reverses therapy automatically." in output
    assert "Superseded: #" in output and "D1 MEDIUM" in output and "D5 LOW" in output
    assert f"Acknowledge: flask alerts acknowledge {notice.id} --as USERNAME" in output
    assert output.rstrip().endswith(ADVISORY)
    assert_fits(result)


def test_acknowledge_commits_and_moves_the_notice(runner: FlaskCliRunner, full: Flask, db: SQLAlchemy) -> None:
    notice_id = the_notice().id
    result = run(runner, "acknowledge", str(notice_id), "--as", "dr.rao", "--comment", "Therapy unchanged")
    assert result.exit_code == 0, result.output
    output = text(result)
    assert f"Acknowledged review notice #{notice_id} (GCK 7-44150500-G-A: LP -> VUS, DEMO-0010 Marcus Bell)" in (
        flat(result)
    )
    assert "Comment:          Therapy unchanged" in output
    assert "0 open review notices left." in output
    db.session.rollback()
    notice = db.session.get(ReviewNotice, notice_id)
    assert notice.status == "acknowledged" and notice.acknowledged_by.username == "dr.rao"
    assert notice.acknowledgement_comment == "Therapy unchanged"
    assert audits("notice_acknowledged")[-1].username == "dr.rao"

    open_only = text(run(runner, "notices"))
    assert "Review notices: 0 open, 1 acknowledged." in open_only
    assert "No open review notices." in open_only and "flask alerts notices --all" in open_only
    everything = text(run(runner, "notices", "--all"))
    assert f"#{notice_id}  acknowledged" in everything
    assert "by dr.rao: Therapy unchanged" in everything


def test_acknowledge_errors(runner: FlaskCliRunner, full: Flask, db: SQLAlchemy) -> None:
    notice_id = the_notice().id
    lab = run(runner, "acknowledge", str(notice_id), "--as", "lab.kim")
    assert lab.exit_code == 1 and "may not acknowledge review notices" in lab.output
    missing = run(runner, "acknowledge", "999", "--as", "dr.rao")
    assert missing.exit_code == 1 and "No review notice #999" in missing.output
    assert run(runner, "acknowledge", str(notice_id), "--as", "admin").exit_code == 0
    again = run(runner, "acknowledge", str(notice_id), "--as", "dr.rao")
    assert again.exit_code == 1
    assert "This review notice was already acknowledged by Demo Administrator" in again.output
    db.session.rollback()
    assert db.session.get(ReviewNotice, notice_id).acknowledged_by.username == "admin"
    assert len(audits("notice_acknowledged")) == 1


def test_notices_when_there_are_none(runner: FlaskCliRunner, db: SQLAlchemy) -> None:
    result = run(runner, "notices", "--all")
    assert result.exit_code == 0
    assert "Review notices: 0 open, 0 acknowledged." in result.output and "No review notices yet." in result.output


def test_lab_technician_may_not_see_notices(runner: FlaskCliRunner, full: Flask) -> None:
    result = run(runner, "notices", "--as", "lab.kim")
    assert result.exit_code == 1 and "may not view review notices" in result.output
    assert "LP -> VUS" not in result.output


def test_review_through_the_terminal_matches_the_web_event(runner: FlaskCliRunner, full: Flask, db: SQLAlchemy) -> None:
    """The terminal records the same provenance event as the web review (shared service)."""
    alert_id = alert_of("DEMO-0005", "D3").id
    assert run(runner, "review", str(alert_id), "--as", "dr.rao").exit_code == 0
    db.session.rollback()
    event = db.session.scalars(select(AlertEvent).where(AlertEvent.alert_id == alert_id, AlertEvent.event == "reviewed")).one()
    alert = db.session.get(Alert, alert_id)
    assert event.release_id == alert.confirmed_release_id and event.rule_version_id == alert.confirmed_rule_version_id
    assert event.user.username == "dr.rao"
    assert db.session.scalar(select(func.count(Alert.id)).where(Alert.status == "reviewed")) == 1
