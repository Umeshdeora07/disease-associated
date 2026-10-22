"""Tests of ``flask patients …`` (commands/patient_cmds.py) and the shared helpers in commands/_format.py.

The commands run with the click runner against the demo dataset: ``seeded`` is the plain seed
(rules v1, release R1), ``full`` the full demo (rules v1 → v2, R1 → R2 with reclassifications
RS1–RS7). Both are restored per test from a snapshot built once per module, so tests that change
data stay isolated. The commands run in the test's app context and share its session; after a
change the tests roll the session back before checking the database, which proves the command
committed (or did not).
"""

from __future__ import annotations

import json
import logging
import sqlite3
from collections.abc import Iterator
from datetime import date, timedelta
from pathlib import Path

import click
import pytest
from click.testing import Result
from flask import Flask
from flask.testing import FlaskCliRunner
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import func, select

from ge_ehr import constants as C
from ge_ehr.commands import _format as F
from ge_ehr.commands import patient_cmds
from ge_ehr.demo import seed_demo
from ge_ehr.extensions import db as _db
from ge_ehr.models import Alert, AuditLog, GenomicUpload, Medication, Patient, User, utc_today, utcnow
from ge_ehr.services import patient_records, reevaluation
from ge_ehr.services.rules import RULES_DATA_DIR, load_rule_set_file
from ge_ehr.services.fhir_export import validate_bundle
from tests.conftest import build_app
from tests.demo_fixtures import restore, seed_snapshot, seeded  # noqa: F401 - fixtures used by name
from tests.factories import make_user

ROOT = Path(__file__).resolve().parents[1]
VCF_DIR = ROOT / "data" / "demo" / "vcfs"
ADVISORY = "Advisory — for specialist review"
SUBCOMMANDS = (
    "list",
    "show",
    "create",
    "set-diagnosis",
    "add-medication",
    "stop-medication",
    "upload-vcf",
    "history",
    "fhir",
)


# ---------------------------------------------------------------------------
# Fixtures and helpers
# ---------------------------------------------------------------------------


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
    """Invoke ``flask patients ARGS``."""
    return runner.invoke(args=["patients", *args], catch_exceptions=False, **kwargs)


def patient(mrn: str) -> Patient:
    return _db.session.scalar(select(Patient).where(Patient.mrn == mrn))


def audits(action: str) -> list[AuditLog]:
    return list(_db.session.scalars(select(AuditLog).where(AuditLog.action == action).order_by(AuditLog.id)))


def current_findings(mrn: str) -> set[tuple[str, str, str | None]]:
    """``{(rule_code, kind, severity)}`` of the patient's current alerts and notes."""
    statement = select(Alert).where(
        Alert.patient_id == patient(mrn).id, Alert.status.in_(C.CURRENT_ALERT_STATUSES)
    )
    return {(alert.rule_code, alert.kind, alert.severity) for alert in _db.session.scalars(statement)}


def lines(result: Result) -> list[str]:
    return click.unstyle(result.output).splitlines()


def assert_fits(result: Result, width: int = F.WIDTH) -> None:
    too_long = [line for line in lines(result) if len(line) > width]
    assert not too_long, f"lines wider than {width} columns: {too_long}"


# ---------------------------------------------------------------------------
# Help and registration
# ---------------------------------------------------------------------------


def test_group_lists_every_command_with_examples(runner: FlaskCliRunner) -> None:
    result = run(runner, "--help")
    assert result.exit_code == 0
    for name in SUBCOMMANDS:
        assert name in result.output
    assert "Examples:" in result.output
    assert "--as USERNAME" in result.output


@pytest.mark.parametrize("name", SUBCOMMANDS)
def test_every_command_has_help_with_examples(runner: FlaskCliRunner, name: str) -> None:
    result = run(runner, name, "--help")
    assert result.exit_code == 0, result.output
    assert "Example" in result.output
    assert f"flask patients {name}" in result.output


def test_flask_cli_registers_the_group(app: Flask) -> None:
    assert "patients" in app.cli.commands
    assert "alerts" in app.cli.commands


# ---------------------------------------------------------------------------
# list
# ---------------------------------------------------------------------------


def test_list_shows_every_demo_patient_with_alert_counts(runner: FlaskCliRunner, seeded: Flask) -> None:
    result = run(runner, "list")
    assert result.exit_code == 0, result.output
    text = click.unstyle(result.output)
    header = lines(result)[0]
    for column in ("MRN", "NAME", "AGE/SEX", "DIABETES", "FAMILY", "P/LP", "LAST VCF", "CURRENT ALERTS"):
        assert column in header
    for number in range(1, 16):
        assert f"DEMO-{number:04d}" in text
    row = next(line for line in lines(result) if line.startswith("DEMO-0001"))
    assert "Maya Okonkwo" in row and ", F" in row and "Type 1" in row and "accepted" in row
    assert row.endswith("1 high, 1 low")
    assert next(line for line in lines(result) if line.startswith("DEMO-0009")).endswith("none")
    assert "15 patients. Details: flask patients show MRN" in text
    assert_fits(result)


def test_list_search_family_alert_filter_and_limit(runner: FlaskCliRunner, seeded: Flask) -> None:
    by_name = run(runner, "list", "--search", "okonkwo")
    assert [line.split()[0] for line in lines(by_name)[1:] if line.startswith("DEMO-")] == ["DEMO-0001"]
    by_mrn = run(runner, "list", "-s", "demo-001")
    assert {line.split()[0] for line in lines(by_mrn) if line.startswith("DEMO-")} == {
        f"DEMO-{n:04d}" for n in range(10, 16)
    }
    family = run(runner, "list", "--family", "FAM-RS6")
    assert {line.split()[0] for line in lines(family) if line.startswith("DEMO-")} == {
        "DEMO-0013",
        "DEMO-0014",
        "DEMO-0015",
    }
    with_alerts = click.unstyle(run(runner, "list", "--with-alerts").output)
    assert "DEMO-0001" in with_alerts and "DEMO-0009" not in with_alerts
    limited = run(runner, "list", "--limit", "3")
    assert len([line for line in lines(limited) if line.startswith("DEMO-")]) == 3
    assert "Showing 3 of 15 patients" in limited.output
    typed = run(runner, "list", "--diabetes-type", "NEONATAL")
    assert {line.split()[0] for line in lines(typed) if line.startswith("DEMO-")} == {"DEMO-0006", "DEMO-0012"}


def test_list_without_matches_or_patients(runner: FlaskCliRunner, seeded: Flask) -> None:
    result = run(runner, "list", "--search", "nobody-like-this")
    assert result.exit_code == 0
    assert "No patients match" in result.output


def test_list_on_an_empty_database_points_to_create_and_seed(runner: FlaskCliRunner, db: SQLAlchemy) -> None:
    result = run(runner, "list")
    assert result.exit_code == 0
    assert "No patients yet" in result.output and "flask seed-demo" in result.output


def test_list_as_lab_technician_hides_alerts(runner: FlaskCliRunner, seeded: Flask) -> None:
    result = run(runner, "list", "--as", "lab.kim")
    assert result.exit_code == 0, result.output
    assert "CURRENT ALERTS" not in result.output
    assert "high" not in click.unstyle(result.output)
    assert "DEMO-0001" in result.output


def test_lab_technician_may_not_filter_by_alerts(runner: FlaskCliRunner, seeded: Flask, db: SQLAlchemy) -> None:
    result = run(runner, "list", "--with-alerts", "--as", "lab.kim")
    assert result.exit_code == 1
    assert "lab.kim (Lab technician) may not filter patients by alerts" in result.output
    assert "recorded in the audit log" in result.output
    db.session.rollback()
    denied = audits("access_denied")
    assert len(denied) == 1 and denied[0].username == "lab.kim"
    assert denied[0].detail["via"] == "cli" and denied[0].detail["command"] == "flask patients list"


# ---------------------------------------------------------------------------
# show
# ---------------------------------------------------------------------------


def test_show_prints_record_profile_uploads_and_alerts(runner: FlaskCliRunner, seeded: Flask) -> None:
    result = run(runner, "show", "DEMO-0001")
    assert result.exit_code == 0, result.output
    text = click.unstyle(result.output)
    assert text.startswith("DEMO-0001  Maya Okonkwo  (synthetic record)")
    assert "Born:             1994-05-12" in text
    assert "Diagnosis:        Type 1, diagnosed at 180 months (15 years), on 2009-05-26" in text
    assert "Current diabetes medications (1)" in text
    assert "Insulin glargine 100 units/mL (Lantus)  Insulin" in text
    # Genomic profile: class, review stars and the expert-panel flag.
    assert "Genomic profile (1 actionable of 2 panel variants)" in text
    assert "Classified against R1 (2025-01, synthetic)." in text
    profile_row = next(line for line in text.splitlines() if "12-120978900-G-A" in line and "heterozygous" in line)
    assert " P " in profile_row and "3/4 stars" in profile_row and "Expert panel" in profile_row
    assert "VCF uploads (1)" in text and "DEMO-0001.vcf" in text and "lab.kim" in text
    # Alerts grouped by severity with rule code + version, recommendation, source and release.
    assert "Current alerts (2) and notes (0)" in text
    assert text.index("HIGH (1)") < text.index("LOW (1)")
    assert "D2 v1  active  HNF1A diabetes on insulin or metformin" in text
    assert "Recommendation: Consider low-dose sulfonylurea as first-line treatment." in text
    assert "Source:         Kang E" in text
    assert "Evidence:       R1 (2025-01, synthetic); rule D2 v1" in text
    assert "flask alerts review" in text
    assert "Open review notices (0)" in text
    assert text.rstrip().endswith(ADVISORY)
    assert_fits(result)


def test_show_finds_mrn_case_insensitively(runner: FlaskCliRunner, seeded: Flask) -> None:
    result = run(runner, "show", "  demo-0002 ")
    assert result.exit_code == 0, result.output
    text = click.unstyle(result.output)
    assert "DEMO-0002  Tomás Herrera" in text
    assert "NOTES (1) - genetic criterion met, clinical condition not met: no alert raised" in text
    assert "Note:           Genetic criterion of D2 met (HNF1A pathogenic) but therapy condition not" in text


def test_show_unknown_patient(runner: FlaskCliRunner, seeded: Flask) -> None:
    result = run(runner, "show", "DEMO-9999")
    assert result.exit_code == 1
    assert "No patient with MRN 'DEMO-9999'" in result.output
    assert "flask patients list --search" in result.output


def test_show_as_lab_technician_hides_recommendations(runner: FlaskCliRunner, seeded: Flask) -> None:
    result = run(runner, "show", "DEMO-0001", "--as", "lab.kim")
    assert result.exit_code == 0, result.output
    text = click.unstyle(result.output)
    assert "Genomic profile" in text and "3/4 stars" in text and "VCF uploads" in text
    for hidden in ("Current alerts", "D2 v1", "sulfonylurea as first-line", "Open review notices", "flask alerts"):
        assert hidden not in text
    assert "shown to clinicians and administrators only" in text
    assert text.rstrip().endswith(ADVISORY)


def test_show_reclassified_amended_and_review_notice(runner: FlaskCliRunner, full: Flask) -> None:
    upgraded = click.unstyle(run(runner, "show", "DEMO-0008").output)
    assert "D2 v1  active  HNF1A diabetes on insulin or metformin  Reclassified, High priority" in upgraded
    assert "Evidence:       R2 (2026-01, synthetic); rule D2 v1" in upgraded
    assert "1 superseded finding: flask patients history DEMO-0008" in upgraded

    downgraded = run(runner, "show", "DEMO-0010")
    text = click.unstyle(downgraded.output)
    row = next(line for line in text.splitlines() if "7-44150500-G-A" in line and "heterozygous" in line)
    assert "VUS" in row and "LP > VUS" in row and "Amended" in row and "Reclassified" in row
    assert "7-44150500-G-A was first reported as likely pathogenic." in text
    assert "Open review notices (1)" in text
    assert "GCK 7-44150500-G-A: LP -> VUS in R2 (2026-01, synthetic)" in text
    assert "Superseded: #" in text and "D1 MEDIUM" in text and "D5 LOW" in text
    assert "flask alerts acknowledge" in text
    assert_fits(downgraded)


def test_show_rule_revision_alert(runner: FlaskCliRunner, full: Flask) -> None:
    text = click.unstyle(run(runner, "show", "DEMO-0012").output)
    assert "D4 v2  active  " in text


def test_show_patient_without_genomic_data(runner: FlaskCliRunner, seeded: Flask) -> None:
    patient_records.create_patient(
        mrn="NEW-1", given_name="Ana", family_name="Silva", sex="female", birth_date=date(1990, 4, 1)
    )
    _db.session.commit()
    text = click.unstyle(run(runner, "show", "NEW-1").output)
    assert "Diagnosis:        Unknown" in text
    assert "None recorded." in text
    assert "No genomic data yet. Upload a GRCh38 VCF with:\n    flask patients upload-vcf NEW-1 PATH" in text
    assert "VCF uploads (0)" in text and "No alerts or notes." in text


# ---------------------------------------------------------------------------
# create
# ---------------------------------------------------------------------------

CREATE = (
    "create",
    "--mrn",
    "DEMO-0016",
    "--given",
    "Ana",
    "--family",
    "Silva",
    "--sex",
    "Female",
    "--birth-date",
    "1990-04-01",
)


def test_create_commits_patient_and_audits_the_user(runner: FlaskCliRunner, seeded: Flask, db: SQLAlchemy) -> None:
    result = run(
        runner, *CREATE, "--diabetes-type", "type2", "--age-at-diagnosis-months", "300", "--family-id", "FAM-9",
        "--as", "dr.rao",
    )
    assert result.exit_code == 0, result.output
    assert "Created patient DEMO-0016 Ana Silva (Female, born 1990-04-01; diagnosis: Type 2" in result.output
    assert "flask patients upload-vcf DEMO-0016 PATH --as dr.rao" in result.output
    db.session.rollback()
    created = patient("DEMO-0016")
    assert created is not None and created.is_synthetic
    assert (created.sex, created.birth_date, created.family_id) == ("female", date(1990, 4, 1), "FAM-9")
    assert (created.diagnosis.diabetes_type, created.diagnosis.age_at_diagnosis_months) == ("type2", 300)
    entry = audits("patient_created")[-1]
    assert entry.username == "dr.rao" and entry.entity_id == created.id


def test_create_requires_as(runner: FlaskCliRunner, seeded: Flask) -> None:
    result = run(runner, *CREATE)
    assert result.exit_code == 2
    assert "Missing option '--as'" in result.output


def test_create_refused_for_lab_technician(runner: FlaskCliRunner, seeded: Flask, db: SQLAlchemy) -> None:
    result = run(runner, *CREATE, "--as", "lab.kim")
    assert result.exit_code == 1
    assert "may not create patients" in result.output
    db.session.rollback()
    assert patient("DEMO-0016") is None
    assert audits("access_denied")[-1].detail["required_roles"] == list(C.CLINICAL_ROLES)


def test_create_unknown_or_inactive_account(runner: FlaskCliRunner, seeded: Flask, db: SQLAlchemy) -> None:
    unknown = run(runner, *CREATE, "--as", "nobody")
    assert unknown.exit_code == 1
    assert "No user account named 'nobody'" in unknown.output and "flask list-users" in unknown.output
    make_user("dr.gone", role="clinician", active=False)
    db.session.commit()
    inactive = run(runner, *CREATE, "--as", "DR.GONE")
    assert inactive.exit_code == 1
    assert "The account 'dr.gone' is deactivated" in inactive.output
    db.session.rollback()
    assert patient("DEMO-0016") is None


def test_create_validation_errors_roll_back(runner: FlaskCliRunner, seeded: Flask, db: SQLAlchemy) -> None:
    duplicate = run(runner, "create", "--mrn", "DEMO-0001", *CREATE[3:], "--as", "dr.rao")
    assert duplicate.exit_code == 1
    assert "Could not create the patient: A patient with MRN DEMO-0001 already exists. Nothing was changed." in (
        duplicate.output
    )
    future = (utc_today() + timedelta(days=2)).isoformat()
    in_future = run(runner, *CREATE[:-1], future, "--as", "dr.rao")
    assert in_future.exit_code == 1 and "cannot be in the future" in in_future.output
    none_with_age = run(
        runner, *CREATE, "--diabetes-type", "none", "--age-at-diagnosis-months", "5", "--as", "dr.rao"
    )
    assert none_with_age.exit_code == 1 and "must be empty when the patient has no diabetes" in none_with_age.output
    db.session.rollback()
    assert patient("DEMO-0016") is None
    assert db.session.scalar(select(func.count(Patient.id))) == 15


def test_create_rejects_badly_written_dates_and_choices(runner: FlaskCliRunner, seeded: Flask) -> None:
    bad_date = run(runner, *CREATE[:-1], "1990-13-01", "--as", "dr.rao")
    assert bad_date.exit_code == 2
    assert "'1990-13-01' is not a date; write it as YYYY-MM-DD" in bad_date.output
    bad_sex = run(runner, *CREATE[:8], "--sex", "x", *CREATE[10:], "--as", "dr.rao")
    assert bad_sex.exit_code == 2 and "Invalid value for '--sex'" in bad_sex.output


# ---------------------------------------------------------------------------
# set-diagnosis
# ---------------------------------------------------------------------------


def test_set_diagnosis_reevaluates_and_reports_the_change(
    runner: FlaskCliRunner, seeded: Flask, db: SQLAlchemy
) -> None:
    assert ("D4", "alert", "high") in current_findings("DEMO-0006")
    result = run(runner, "set-diagnosis", "DEMO-0006", "--type", "neonatal", "--age-at-diagnosis-months", "24",
                 "--as", "dr.rao")
    assert result.exit_code == 0, result.output
    text = click.unstyle(result.output)
    assert "Diagnosis of DEMO-0006 Leo Novak: Neonatal, diagnosed at 24 months (2 years)" in text
    assert "Changed: age at diagnosis (months) 2 -> 24." in text
    assert "Decision support re-evaluated: 1 new note, 1 superseded." in text
    assert "+ new note" in text and "- superseded alert" in text and "D4 v1" in text
    db.session.rollback()
    assert ("D4", "note", None) in current_findings("DEMO-0006")
    assert ("D4", "alert", "high") not in current_findings("DEMO-0006")
    superseded = db.session.scalar(select(Alert).where(Alert.rule_code == "D4", Alert.status == "superseded"))
    assert superseded.superseded_reason == "diagnosis_changed"
    entry = audits("diagnosis_updated")[-1]
    assert entry.username == "dr.rao"
    assert entry.detail["changes"]["age_at_diagnosis_months"] == {"old": 2, "new": 24}


def test_set_diagnosis_keeps_values_not_given(runner: FlaskCliRunner, seeded: Flask, db: SQLAlchemy) -> None:
    result = run(runner, "set-diagnosis", "demo-0001", "--type", "MONOGENIC", "--as", "admin")
    assert result.exit_code == 0, result.output
    assert "Changed: type Type 1 -> Monogenic." in result.output
    assert "Decision support re-evaluated: no change to alerts or notes." in result.output
    db.session.rollback()
    diagnosis = patient("DEMO-0001").diagnosis
    assert (diagnosis.diabetes_type, diagnosis.age_at_diagnosis_months, diagnosis.diagnosed_on) == (
        "monogenic",
        180,
        date(2009, 5, 26),
    )
    unchanged = run(runner, "set-diagnosis", "DEMO-0001", "--type", "monogenic", "--as", "admin")
    assert "No recorded value changed." in unchanged.output


def test_set_diagnosis_unknown_age_and_no_diabetes(runner: FlaskCliRunner, seeded: Flask, db: SQLAlchemy) -> None:
    cleared = run(runner, "set-diagnosis", "DEMO-0003", "--type", "type2", "--unknown-age", "--as", "dr.rao")
    assert cleared.exit_code == 0, cleared.output
    assert "age at diagnosis (months) 360 -> -" in cleared.output
    none = run(runner, "set-diagnosis", "DEMO-0004", "--type", "none", "--as", "dr.rao")
    assert none.exit_code == 0, none.output
    db.session.rollback()
    assert patient("DEMO-0003").diagnosis.age_at_diagnosis_months is None
    diagnosis = patient("DEMO-0004").diagnosis
    assert (diagnosis.diabetes_type, diagnosis.age_at_diagnosis_months, diagnosis.diagnosed_on) == ("none", None, None)


def test_set_diagnosis_errors(runner: FlaskCliRunner, seeded: Flask, db: SQLAlchemy) -> None:
    both = run(runner, "set-diagnosis", "DEMO-0006", "--type", "neonatal", "--age-at-diagnosis-months", "3",
               "--unknown-age", "--as", "dr.rao")
    assert both.exit_code == 2 and "not both" in both.output
    too_old = run(runner, "set-diagnosis", "DEMO-0006", "--type", "neonatal", "--age-at-diagnosis-months", "900",
                  "--as", "dr.rao")
    assert too_old.exit_code == 1
    assert "Could not update the diagnosis: Age at diagnosis (900 months) cannot exceed" in too_old.output
    lab = run(runner, "set-diagnosis", "DEMO-0006", "--type", "type1", "--as", "lab.kim")
    assert lab.exit_code == 1 and "may not change diagnoses" in lab.output
    db.session.rollback()
    assert patient("DEMO-0006").diagnosis.age_at_diagnosis_months == 2
    assert patient("DEMO-0006").diagnosis.diabetes_type == "neonatal"
    assert ("D4", "alert", "high") in current_findings("DEMO-0006")


# ---------------------------------------------------------------------------
# add-medication / stop-medication
# ---------------------------------------------------------------------------


def test_add_medication_suggests_the_class_and_reevaluates(
    runner: FlaskCliRunner, seeded: Flask, db: SQLAlchemy
) -> None:
    assert ("D1", "note", None) in current_findings("DEMO-0004")
    result = run(runner, "add-medication", "DEMO-0004", "--drug", "Metformin 500 mg tablet", "--dose", "500 mg bd",
                 "--start", "2026-01-10", "--as", "dr.rao")
    assert result.exit_code == 0, result.output
    text = click.unstyle(result.output)
    assert "Added Metformin 500 mg tablet to DEMO-0004 Samuel Adeyemi as Metformin (suggested from the drug" in text
    assert "Decision support re-evaluated: 1 new alert, 1 superseded." in text
    assert "+ new alert" in text and "MEDIUM" in text and "- superseded note" in text
    assert_fits(result)
    db.session.rollback()
    medication = db.session.scalar(select(Medication).where(Medication.patient_id == patient("DEMO-0004").id))
    assert (medication.drug_class, medication.dose, medication.start_date) == ("metformin", "500 mg bd", date(2026, 1, 10))
    assert medication.created_by.username == "dr.rao"
    assert ("D1", "alert", "medium") in current_findings("DEMO-0004")
    assert audits("medication_added")[-1].username == "dr.rao"


def test_add_medication_with_explicit_class(runner: FlaskCliRunner, seeded: Flask, db: SQLAlchemy) -> None:
    result = run(runner, "add-medication", "DEMO-0004", "--drug", "Sitagliptin", "--class", "OTHER", "--as", "dr.rao")
    assert result.exit_code == 0, result.output
    assert "as Other glucose-lowering" in result.output or "as Other" in result.output
    assert "suggested" not in result.output
    db.session.rollback()
    medication = db.session.scalar(select(Medication).where(Medication.drug_name == "Sitagliptin"))
    assert medication.drug_class == "other"


def test_add_medication_errors_change_nothing(runner: FlaskCliRunner, seeded: Flask, db: SQLAlchemy) -> None:
    future = (utc_today() + timedelta(days=3)).isoformat()
    result = run(runner, "add-medication", "DEMO-0004", "--drug", "Metformin", "--start", future, "--as", "dr.rao")
    assert result.exit_code == 1
    assert "Could not add the medication: Start date" in result.output and "Nothing was changed." in result.output
    lab = run(runner, "add-medication", "DEMO-0004", "--drug", "Metformin", "--as", "lab.kim")
    assert lab.exit_code == 1 and "may not change medications" in lab.output
    db.session.rollback()
    assert patient("DEMO-0004").medications == []
    assert ("D1", "note", None) in current_findings("DEMO-0004")


def test_unexpected_service_error_rolls_back(
    runner: FlaskCliRunner, seeded: Flask, db: SQLAlchemy, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = patient_records.add_medication

    def failing(*args, **kwargs):  # noqa: ANN002, ANN003, ANN202
        original(*args, **kwargs)  # the medication is flushed, then something breaks
        raise RuntimeError("disk full")

    monkeypatch.setattr(patient_records, "add_medication", failing)
    audited = len(audits("medication_added"))
    result = run(runner, "add-medication", "DEMO-0004", "--drug", "Metformin", "--as", "dr.rao")
    assert result.exit_code == 1
    assert "Could not add the medication: unexpected error (RuntimeError: disk full). Nothing was changed." in (
        result.output
    )
    db.session.rollback()
    assert patient("DEMO-0004").medications == []
    assert len(audits("medication_added")) == audited


def test_stop_medication_by_partial_name(runner: FlaskCliRunner, seeded: Flask, db: SQLAlchemy) -> None:
    assert ("D1", "alert", "medium") in current_findings("DEMO-0003")
    result = run(runner, "stop-medication", "DEMO-0003", "--drug", "METFORMIN", "--as", "dr.rao")
    assert result.exit_code == 0, result.output
    text = click.unstyle(result.output)
    today = utc_today().isoformat()
    assert f"Stopped Metformin 500 mg tablet for DEMO-0003 Hannah Lindqvist (last day {today})." in text
    assert "Decision support re-evaluated: 1 new note, 1 superseded." in text
    assert "- superseded alert" in text and "MEDIUM" in text
    db.session.rollback()
    medication = patient("DEMO-0003").medications[0]
    assert medication.end_date == utc_today()
    assert ("D1", "note", None) in current_findings("DEMO-0003")
    assert ("D1", "alert", "medium") not in current_findings("DEMO-0003")
    entry = audits("medication_stopped")[-1]
    assert entry.username == "dr.rao" and entry.entity_id == medication.id


def test_stop_medication_with_end_date(runner: FlaskCliRunner, seeded: Flask, db: SQLAlchemy) -> None:
    result = run(runner, "stop-medication", "DEMO-0002", "--drug", "Gliclazide 80 mg tablet", "--end", "2026-01-31",
                 "--as", "admin")
    assert result.exit_code == 0, result.output
    db.session.rollback()
    assert patient("DEMO-0002").medications[0].end_date == date(2026, 1, 31)
    before_start = run(runner, "stop-medication", "DEMO-0003", "--drug", "metformin", "--end", "1990-01-01",
                       "--as", "admin")
    assert before_start.exit_code == 1 and "cannot be before the start date" in before_start.output
    db.session.rollback()
    assert patient("DEMO-0003").medications[0].end_date is None


def test_stop_medication_unknown_or_ambiguous(runner: FlaskCliRunner, seeded: Flask, db: SQLAlchemy) -> None:
    unknown = run(runner, "stop-medication", "DEMO-0002", "--drug", "insulin", "--as", "dr.rao")
    assert unknown.exit_code == 1
    assert "DEMO-0002 has no current medication named 'insulin'. Current medications: Gliclazide 80 mg tablet." in (
        unknown.output
    )
    patient_records.add_medication(patient("DEMO-0001"), drug_name="Insulin aspart", drug_class="insulin")
    db.session.commit()
    ambiguous = run(runner, "stop-medication", "DEMO-0001", "--drug", "insulin", "--as", "dr.rao")
    assert ambiguous.exit_code == 1 and "matches several current medications" in ambiguous.output
    db.session.rollback()
    assert all(medication.is_active for medication in patient("DEMO-0001").medications)


# ---------------------------------------------------------------------------
# upload-vcf
# ---------------------------------------------------------------------------


def test_upload_vcf_accepted_for_a_clinician(runner: FlaskCliRunner, seeded: Flask, db: SQLAlchemy) -> None:
    patient_records.create_patient(
        mrn="NEW-1", given_name="Ana", family_name="Silva", sex="female", birth_date=date(1990, 4, 1),
        diabetes_type="type2", age_at_diagnosis_months=300,
    )
    patient_records.add_medication(patient("NEW-1"), drug_name="Metformin", drug_class="metformin")
    db.session.commit()
    result = run(runner, "upload-vcf", "NEW-1", str(VCF_DIR / "DEMO-0001.vcf"), "--as", "dr.rao")
    assert result.exit_code == 0, result.output
    text = click.unstyle(result.output)
    assert "Accepted DEMO-0001.vcf for NEW-1 Ana Silva (upload #" in text
    assert "2 records in the file; 2 panel variants (2 new, 0 already recorded)" in text
    assert "0 off-panel, 0 non-PASS, 0 reference calls" in text
    assert "Decision support re-evaluated: 2 new alerts." in text
    assert "D2 v1" in text and "HIGH" in text and "D5 v1" in text
    assert "Genomic profile: flask patients show NEW-1" in text
    db.session.rollback()
    assert current_findings("NEW-1") == {("D2", "alert", "high"), ("D5", "alert", "low")}
    upload = patient("NEW-1").uploads[0]
    assert upload.status == "accepted" and upload.uploaded_by.username == "dr.rao"


def test_upload_vcf_by_lab_technician_hides_recommendations(
    runner: FlaskCliRunner, seeded: Flask, db: SQLAlchemy
) -> None:
    result = run(runner, "upload-vcf", "DEMO-0009", str(VCF_DIR / "DEMO-0009.vcf"), "--as", "lab.kim")
    assert result.exit_code == 0, result.output
    text = click.unstyle(result.output)
    assert "Accepted DEMO-0009.vcf" in text and "1 off-panel" in text
    assert "Decision support has been updated for the care team." in text
    assert "alert" not in text.replace("care team", "")
    db.session.rollback()
    assert audits("vcf_upload_accepted")[-1].username == "lab.kim"


def test_upload_vcf_rejected_keeps_the_record_and_exits_1(
    runner: FlaskCliRunner, seeded: Flask, db: SQLAlchemy
) -> None:
    variants_before = len(patient("DEMO-0001").variants)
    result = run(runner, "upload-vcf", "DEMO-0001", str(VCF_DIR / "malformed.vcf"), "--as", "lab.kim")
    assert result.exit_code == 1
    assert "Rejected malformed.vcf for DEMO-0001: Line 12: POS must be a positive whole number" in result.output
    assert "Nothing was imported; the rejection is recorded in the audit trail (rejected upload #" in result.output
    db.session.rollback()
    rejected = db.session.scalars(
        select(GenomicUpload).where(GenomicUpload.status == "rejected", GenomicUpload.patient_id == patient("DEMO-0001").id)
    ).all()
    assert len(rejected) == 1 and "Line 12" in rejected[0].error
    assert len(patient("DEMO-0001").variants) == variants_before
    entry = audits("vcf_upload_rejected")[-1]
    assert entry.username == "lab.kim" and entry.detail["line"] == 12


def test_upload_vcf_missing_file_or_patient(runner: FlaskCliRunner, seeded: Flask, tmp_path: Path) -> None:
    missing = run(runner, "upload-vcf", "DEMO-0001", str(tmp_path / "nope.vcf"), "--as", "lab.kim")
    assert missing.exit_code == 2 and "does not exist" in missing.output
    no_patient = run(runner, "upload-vcf", "DEMO-9999", str(VCF_DIR / "DEMO-0001.vcf"), "--as", "lab.kim")
    assert no_patient.exit_code == 1 and "No patient with MRN 'DEMO-9999'" in no_patient.output
    no_as = run(runner, "upload-vcf", "DEMO-0001", str(VCF_DIR / "DEMO-0001.vcf"))
    assert no_as.exit_code == 2


def test_upload_vcf_refuses_files_beyond_the_upload_limit(
    runner: FlaskCliRunner, seeded: Flask, db: SQLAlchemy, tmp_path: Path
) -> None:
    seeded.config["MAX_CONTENT_LENGTH"] = 100
    big = tmp_path / "big.vcf"
    big.write_bytes((VCF_DIR / "DEMO-0001.vcf").read_bytes())
    result = run(runner, "upload-vcf", "DEMO-0001", str(big), "--as", "lab.kim")
    assert result.exit_code == 1 and "patient VCFs may be at most 20 MB" in result.output
    db.session.rollback()
    assert audits("vcf_upload_rejected") == []


# ---------------------------------------------------------------------------
# history (point-in-time view)
# ---------------------------------------------------------------------------


def test_history_timeline_lists_every_change(runner: FlaskCliRunner, full: Flask) -> None:
    result = run(runner, "history", "DEMO-0010")
    assert result.exit_code == 0, result.output
    text = click.unstyle(result.output)
    assert text.startswith("Recommendation history of DEMO-0010 Marcus Bell")
    assert "1 current finding, 2 superseded findings." in text
    assert "ClinVar release R2 (2026-01, synthetic) (run #" in text
    assert "VCF upload; evidence: R1 (2025-01, synthetic)" in text
    assert text.count("- superseded alert") == 2 and "downgrade" in text
    assert "+ new alert" in text and "D6 v1" in text
    assert text.index("ClinVar release R2") < text.index("VCF upload")  # newest first
    assert 'flask patients history DEMO-0010 --at "' in text
    assert text.rstrip().endswith(ADVISORY)
    assert_fits(result)


def test_history_shows_confirmations_and_reviews(runner: FlaskCliRunner, full: Flask, db: SQLAlchemy) -> None:
    text = click.unstyle(run(runner, "history", "DEMO-0005").output)
    assert text.count("= confirmed alert") == 2 and "provenance updated" in text
    alert = db.session.scalar(select(Alert).where(Alert.patient_id == patient("DEMO-0005").id, Alert.rule_code == "D3"))
    assert runner.invoke(args=["alerts", "review", str(alert.id), "--as", "dr.rao"]).exit_code == 0
    reviewed = click.unstyle(run(runner, "history", "DEMO-0005").output)
    assert "* reviewed alert" in reviewed and "by dr.rao" in reviewed and "Clinician review" in reviewed


def test_history_at_reproduces_the_state_before_the_release(
    runner: FlaskCliRunner, full: Flask, db: SQLAlchemy
) -> None:
    d6 = db.session.scalar(select(Alert).where(Alert.patient_id == patient("DEMO-0010").id, Alert.rule_code == "D6"))
    before = (d6.created_at - timedelta(milliseconds=5)).isoformat(sep=" ", timespec="milliseconds")
    result = run(runner, "history", "DEMO-0010", "--at", before)
    assert result.exit_code == 0, result.output
    text = click.unstyle(result.output)
    assert text.startswith(f"Recommendations of DEMO-0010 Marcus Bell as of {before[:19]}")
    assert "Alerts (2) and notes (0)" in text
    assert "D1 v1" in text and "D5 v1" in text and "D6 v1" not in text
    assert "Evidence:       R1 (2025-01, synthetic)" in text
    assert "Now:            superseded" in text and "(Variant downgraded)" in text
    assert "flask alerts review" not in text  # no review hint for a past state
    after = run(runner, "history", "DEMO-0010", "--at", d6.created_at.isoformat(sep=" "))
    after_text = click.unstyle(after.output)
    assert "D6 v1" in after_text and "D1 v1" not in after_text
    assert "Now:            still current (active)" in after_text


def test_history_at_shows_the_text_of_the_time(runner: FlaskCliRunner, full: Flask, db: SQLAlchemy) -> None:
    """Finding F2: neither a therapy change nor a rule revision rewrites what --at shows."""
    note = db.session.scalar(
        select(Alert).where(Alert.patient_id == patient("DEMO-0002").id, Alert.rule_code == "D2", Alert.kind == "note")
    )
    alert = db.session.scalar(
        select(Alert).where(Alert.patient_id == patient("DEMO-0001").id, Alert.rule_code == "D2", Alert.kind == "alert")
    )
    note_then, alert_then = note.message, alert.message
    assert "already receiving a sulfonylurea" in note_then
    t0 = utcnow()
    for medication in patient("DEMO-0002").active_medications:
        patient_records.stop_medication(medication)
    rules = load_rule_set_file(RULES_DATA_DIR / "ruleset_v2.json")
    d2 = next(rule for rule in rules["rules"] if rule["code"] == "D2")
    d2.update(version=2, recommendation="REVISED TEXT: refer to diabetes genetics clinic.")
    reevaluation.apply_rule_set(rules)
    db.session.commit()
    assert note.is_current and note.message != note_then

    at = t0.isoformat(sep=" ", timespec="milliseconds")
    then_note = " ".join(click.unstyle(run(runner, "history", "DEMO-0002", "--at", at).output).split())
    then_alert = " ".join(click.unstyle(run(runner, "history", "DEMO-0001", "--at", at).output).split())
    now_alert = " ".join(click.unstyle(run(runner, "history", "DEMO-0001", "--at", utcnow().isoformat(sep=" ")).output).split())

    assert f"Note: {note_then}" in then_note and "no current glucose-lowering medication" not in then_note
    assert f"Recommendation: {alert_then}" in then_alert and "REVISED TEXT" not in then_alert
    assert "D2 v1" in then_alert and "Now: superseded" in then_alert and "(Rule revised)" in then_alert
    assert "Recommendation: REVISED TEXT: refer to diabetes genetics clinic." in now_alert and "D2 v2" in now_alert


def test_history_at_before_any_finding_and_date_only(runner: FlaskCliRunner, full: Flask) -> None:
    result = run(runner, "history", "DEMO-0010", "--at", "2020-01-01")
    assert result.exit_code == 0, result.output
    assert "as of 2020-01-01 00:00 UTC" in result.output
    assert "No alerts or notes were current at that time." in result.output


def test_history_without_findings(runner: FlaskCliRunner, seeded: Flask) -> None:
    result = run(runner, "history", "DEMO-0009")
    assert result.exit_code == 0
    assert "No alert or note has been raised for this patient." in result.output


def test_history_errors(runner: FlaskCliRunner, seeded: Flask, db: SQLAlchemy) -> None:
    bad = run(runner, "history", "DEMO-0010", "--at", "yesterday")
    assert bad.exit_code == 2
    assert "'yesterday' is not a date and time; use \"YYYY-MM-DD HH:MM\" (UTC)" in bad.output
    lab = run(runner, "history", "DEMO-0010", "--as", "lab.kim")
    assert lab.exit_code == 1 and "may not view recommendations" in lab.output
    db.session.rollback()
    assert audits("access_denied")[-1].username == "lab.kim"


# ---------------------------------------------------------------------------
# fhir
# ---------------------------------------------------------------------------


def test_fhir_to_stdout_is_a_valid_bundle(runner: FlaskCliRunner, seeded: Flask, db: SQLAlchemy) -> None:
    result = run(runner, "fhir", "DEMO-0001", "--as", "dr.rao")
    assert result.exit_code == 0, result.output
    bundle = json.loads(result.stdout)
    assert bundle["resourceType"] == "Bundle" and bundle["type"] == "collection"
    validate_bundle(bundle)
    patients = [entry["resource"] for entry in bundle["entry"] if entry["resource"]["resourceType"] == "Patient"]
    assert patients[0]["identifier"][0]["value"] == "DEMO-0001"
    db.session.rollback()
    entry = audits("fhir_export")[-1]
    assert entry.username == "dr.rao" and entry.detail["output"] == "stdout" and entry.detail["via"] == "cli"


def test_fhir_reports_amended_variants(runner: FlaskCliRunner, full: Flask) -> None:
    bundle = json.loads(run(runner, "fhir", "DEMO-0010").stdout)
    statuses = {entry["resource"].get("status") for entry in bundle["entry"] if entry["resource"]["resourceType"] == "Observation"}
    assert statuses == {"amended"}


def test_fhir_to_file(runner: FlaskCliRunner, seeded: Flask, db: SQLAlchemy, tmp_path: Path) -> None:
    target = tmp_path / "DEMO-0001 fhir.json"
    result = run(runner, "fhir", "DEMO-0001", "--output", str(target), "--as", "admin")
    assert result.exit_code == 0, result.output
    assert "Wrote the validated FHIR R4 Bundle of DEMO-0001 (" in result.output
    bundle = json.loads(target.read_text(encoding="utf-8"))
    validate_bundle(bundle)
    again = run(runner, "fhir", "DEMO-0001", "-o", str(target), "--as", "admin")
    assert again.exit_code == 1 and "already exists; add --force" in again.output
    forced = run(runner, "fhir", "DEMO-0001", "-o", str(target), "--force", "--as", "admin")
    assert forced.exit_code == 0, forced.output
    db.session.rollback()
    assert [entry.detail["output"] for entry in audits("fhir_export")] == ["file", "file"]


def test_fhir_errors(runner: FlaskCliRunner, seeded: Flask, db: SQLAlchemy, tmp_path: Path) -> None:
    lab = run(runner, "fhir", "DEMO-0001", "--as", "lab.kim")
    assert lab.exit_code == 1 and "may not export FHIR bundles" in lab.output
    unwritable = run(runner, "fhir", "DEMO-0001", "-o", str(tmp_path / "missing-dir" / "x.json"))
    assert unwritable.exit_code == 1 and "Could not write" in unwritable.output
    db.session.rollback()
    assert audits("fhir_export") == []


# ---------------------------------------------------------------------------
# Output formatting, colour and logging
# ---------------------------------------------------------------------------


def test_colour_labels_on_a_terminal_and_none_with_no_color(
    runner: FlaskCliRunner, full: Flask, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.delenv("TERM", raising=False)
    coloured = run(runner, "show", "DEMO-0008", color=True)
    assert "\x1b[" in coloured.output
    assert click.style("HIGH", fg="red", bold=True) in coloured.output
    assert click.style("Reclassified", fg="magenta", bold=True) in coloured.output
    plain = run(runner, "show", "DEMO-0008")  # not a terminal: click strips the styles
    assert "\x1b[" not in plain.output
    monkeypatch.setenv("NO_COLOR", "1")
    no_color = run(runner, "show", "DEMO-0008", color=True)
    assert "\x1b[" not in no_color.output
    assert click.unstyle(coloured.output) == no_color.output


def test_wide_views_fit_100_columns(runner: FlaskCliRunner, full: Flask) -> None:
    for args in (("list",), ("show", "DEMO-0008"), ("show", "DEMO-0013"), ("history", "DEMO-0008")):
        assert_fits(run(runner, *args))


def test_service_info_logs_are_hidden_unless_log_level_is_set(
    runner: FlaskCliRunner, seeded: Flask, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("LOG_LEVEL", raising=False)
    package = logging.getLogger("ge_ehr")
    caplog.set_level(logging.INFO, logger="ge_ehr")
    result = run(runner, "upload-vcf", "DEMO-0001", str(VCF_DIR / "DEMO-0001.vcf"), "--as", "lab.kim")
    assert result.exit_code == 0, result.output
    assert not [record for record in caplog.records if record.name.startswith("ge_ehr.services")]
    assert package.level == logging.INFO, "the level is restored after the command"
    monkeypatch.setenv("LOG_LEVEL", "INFO")
    caplog.clear()
    run(runner, "upload-vcf", "DEMO-0001", str(VCF_DIR / "DEMO-0001.vcf"), "--as", "lab.kim")
    assert [record for record in caplog.records if record.name == "ge_ehr.services.patient_records"]


def test_style_helpers_degrade_without_colour(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.setenv("TERM", "xterm")
    assert F.severity_label("high") == click.style("HIGH", fg="red", bold=True)
    assert click.unstyle(F.class_label("LP", long=True)) == "Likely pathogenic (LP)"
    assert F.severity_label(None) == "note"
    assert F.class_label(None) == "-" and F.status_label(None) == "-"
    monkeypatch.setenv("TERM", "dumb")
    assert F.severity_label("high") == "HIGH"
    monkeypatch.setenv("TERM", "xterm")
    monkeypatch.setenv("NO_COLOR", "1")
    assert F.class_label("P") == "P" and F.flag("Reclassified") == "Reclassified"
    assert F.style("x", fg="red") == "x"


def test_table_aligns_styled_cells_and_shortens_the_flex_column(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.setenv("TERM", "xterm")
    rows = [[F.severity_label("high"), "x" * 80, None], [F.severity_label("info"), "short", 7]]
    result = F.table_lines(["SEV", "TEXT", "N"], rows, flex=1, width=40)
    plain = [click.unstyle(line) for line in result]
    assert plain[0] == "  SEV   TEXT" + " " * 27 + "N"
    assert plain[1].startswith("  HIGH  xxxx") and "..." in plain[1] and plain[1].endswith("  -")
    assert all(len(line) <= 40 for line in plain)
    assert plain[2] == "  INFO  short" + " " * 26 + "7"
    assert F.table_lines(None, [["a", "bb"], ["ccc", "d"]], indent=0) == ["a    bb", "ccc  d"]


def test_text_helpers() -> None:
    assert F.shorten("abcdefghij", 6) == "abc..."
    assert F.shorten("abc", 6) == "abc"
    assert F.plural(1, "alert") == "1 alert" and F.plural(2, "alert") == "2 alerts"
    assert F.plural(2, "family", "families") == "2 families"
    assert F.text(None) == "-" and F.text("") == "-" and F.text(date(2026, 1, 15)) == "2026-01-15"
    assert F.class_change("LP", "VUS") in ("LP -> VUS", click.style("LP", fg="bright_red") + " -> " + click.style("VUS", fg="yellow"))


def test_field_wraps_long_values(capsys: pytest.CaptureFixture[str]) -> None:
    F.field("Source", "word " * 40, width=60)
    output = capsys.readouterr().out.splitlines()
    assert output[0].startswith("  Source:" + " " * 11 + "word")
    assert all(len(line) <= 60 for line in output) and len(output) > 2
    assert all(line.startswith(" " * 20 + "word") for line in output[1:])


def test_echo_replaces_characters_the_console_cannot_show(monkeypatch: pytest.MonkeyPatch) -> None:
    written: list[str] = []

    def fake_echo(message: str = "", err: bool = False) -> None:  # noqa: FBT001, FBT002
        if any(ord(char) > 127 for char in message):
            raise UnicodeEncodeError("ascii", message, 0, 1, "unsupported")
        written.append(message)

    class Stream:
        encoding = "ascii"

    monkeypatch.setattr(click, "echo", fake_echo)
    monkeypatch.setattr(F.sys, "stdout", Stream())
    F.echo(ADVISORY)
    assert written == ["Advisory ? for specialist review"]


def test_acting_user_role_check_audits_denial(seeded: Flask, db: SQLAlchemy) -> None:
    assert F.acting_user(None) is None
    assert F.acting_user("Dr.Rao", roles=C.CLINICAL_ROLES).username == "dr.rao"
    with pytest.raises(click.ClickException, match="needs an administrator or clinician account"):
        with click.Context(patient_cmds.patients_cli):
            F.acting_user("lab.kim", roles=C.CLINICAL_ROLES, action="see alerts")
    db.session.rollback()
    assert audits("access_denied")[-1].detail["role"] == "lab_technician"
    assert F.may_see_recommendations(None)
    assert not F.may_see_recommendations(db.session.scalar(select(User).where(User.username == "lab.kim")))


def test_saving_commits_or_rolls_back(seeded: Flask, db: SQLAlchemy) -> None:
    with F.saving("Could not rename"):
        patient("DEMO-0001").notes = "changed"
    db.session.rollback()
    assert patient("DEMO-0001").notes == "changed"
    with pytest.raises(click.ClickException, match="Could not rename: bad value. Nothing was changed."):
        with F.saving("Could not rename"):
            patient("DEMO-0001").notes = "again"
            raise ValueError("bad value")
    assert patient("DEMO-0001").notes == "changed"
