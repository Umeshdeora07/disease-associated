"""Tests of ``flask reeval full|runs|show`` (commands/reeval_cmds.py)."""

from __future__ import annotations

from typing import Any

import pytest
from flask.testing import FlaskCliRunner
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import func, select

from ge_ehr.models import KbRelease, ReevaluationRun
from ge_ehr.services import alerting, reevaluation
from ge_ehr.services.alerting import import_rule_versions
from ge_ehr.services.rules import RULES_DATA_DIR, load_rule_set_file
from tests.factories import (
    add_known_variant,
    add_medication,
    add_patient_variant,
    make_patient,
    make_release,
    rule_definition,
)

V01 = ("12", 120978900, "G", "A")  # HNF1A
V06 = ("12", 120994300, "C", "T")  # HNF1A
V07 = ("7", 44150500, "G", "A")  # GCK


@pytest.fixture
def activated(db: SQLAlchemy) -> dict[str, Any]:
    """Rules v1, R1 (first activation) and R2 (change-driven) for three patients, committed."""
    import_rule_versions(load_rule_set_file(RULES_DATA_DIR / "ruleset_v1.json"))
    r1, r2 = make_release("R1"), make_release("R2")
    for key, cls in ((V01, "P"), (V06, "VUS"), (V07, "LP")):
        add_known_variant(r1, *key, cls)
    for key, cls in ((V01, "P"), (V06, "LP"), (V07, "VUS")):
        add_known_variant(r2, *key, cls)
    for key in (V01, V06, V07):
        patient = make_patient(diagnosis={"diabetes_type": "type2", "age_at_diagnosis_months": 400})
        add_medication(patient, "Metformin", "metformin")
        add_patient_variant(patient, *key)
    db.session.commit()
    first = reevaluation.activate_release(r1)
    second = reevaluation.activate_release(r2)
    db.session.commit()
    return {"r1": r1, "r2": r2, "first": first, "second": second}


def run_total(db: SQLAlchemy) -> int:
    db.session.rollback()
    return db.session.scalar(select(func.count(ReevaluationRun.id))) or 0


# ---------------------------------------------------------------------------
# full
# ---------------------------------------------------------------------------


def test_full_asks_for_confirmation(runner: FlaskCliRunner, db: SQLAlchemy, activated: dict[str, Any]) -> None:
    result = runner.invoke(args=["reeval", "full"], input="n\n")
    assert result.exit_code == 1
    assert "Re-evaluate all 3 patient(s) (3 variant record(s)) against R2 and the current rules? [y/N]" in result.output
    assert "Aborted!" in result.output
    assert run_total(db) == 2


def test_full_runs_and_commits(runner: FlaskCliRunner, db: SQLAlchemy, activated: dict[str, Any]) -> None:
    result = runner.invoke(args=["reeval", "full"], input="y\n")
    assert result.exit_code == 0, result.output
    run = db.session.scalars(select(ReevaluationRun).order_by(ReevaluationRun.id.desc())).first()
    assert (run.trigger, run.strategy, run.status) == ("full_recompute", "full", "success")
    assert f"Full re-evaluation run #{run.id} (success) finished in " in result.output
    work = "  Work:     0 stored keys compared, 0 changed, 0 carriers affected, 3 patients re-evaluated, "
    assert work in result.output
    assert "3 records examined" in result.output
    assert "  Outcome:  0 alerts and 0 notes created, 0 superseded, 2 confirmed; 0 review notices, 0 notifications" in (
        result.output
    )
    assert run_total(db) == 3, "the command committed the run"


def test_full_without_a_release_and_without_prompt(runner: FlaskCliRunner, db: SQLAlchemy) -> None:
    result = runner.invoke(args=["reeval", "full", "--yes"])
    assert result.exit_code == 0, result.output
    assert "[y/N]" not in result.output
    assert "0 patients re-evaluated" in result.output
    assert run_total(db) == 1


def test_full_failure_is_reported_cleanly(
    runner: FlaskCliRunner, db: SQLAlchemy, activated: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    def explode(*_args: Any, **_kwargs: Any) -> None:
        raise RuntimeError("disk full")

    monkeypatch.setattr(alerting, "reconcile_patient", explode)
    result = runner.invoke(args=["reeval", "full", "--yes"])
    assert result.exit_code == 1
    assert result.output == (
        "Error: Full re-evaluation failed: disk full. No alert was changed; "
        "the failed run is listed by 'flask reeval runs'.\n"
    )
    failed = db.session.scalars(select(ReevaluationRun).where(ReevaluationRun.status == "failed")).one()
    assert failed.error == "RuntimeError: disk full"


# ---------------------------------------------------------------------------
# runs
# ---------------------------------------------------------------------------


def test_runs_without_runs(runner: FlaskCliRunner, db: SQLAlchemy) -> None:
    result = runner.invoke(args=["reeval", "runs"])
    assert result.exit_code == 0
    assert result.output == "No re-evaluation runs yet.\n"


def test_runs_lists_the_most_recent_first(runner: FlaskCliRunner, db: SQLAlchemy, activated: dict[str, Any]) -> None:
    result = runner.invoke(args=["reeval", "runs"])
    assert result.exit_code == 0, result.output
    lines = result.output.splitlines()
    assert lines[0].split() == [
        "ID",
        "STARTED",
        "(UTC)",
        "TRIGGER",
        "STRATEGY",
        "STATUS",
        "RELEASE",
        "DURATION",
        "CHANGED",
        "PATIENTS",
        "RECORDS",
        "CREATED",
        "SUPERSEDED",
        "NOTICES",
        "NOTIFIED",
    ]
    second, first = activated["second"], activated["first"]
    assert lines[2].split()[0] == str(second.id) and lines[3].split()[0] == str(first.id)
    # Columns: id, start date and time, trigger, strategy, status, release, duration ("12 ms"), counters.
    assert lines[2].split()[3:7] == ["clinvar_release", "change_driven", "success", "R2"]
    # R2: V06 upgrade (D6 superseded; D2 high + D5 low created, notifying) and V07 downgrade
    # (D1 + D5 superseded with one review notice; D6 created silently).
    assert lines[2].split()[-7:] == ["2", "2", "2", "3", "3", "1", "3"]

    limited = runner.invoke(args=["reeval", "runs", "--limit", "1"])
    assert limited.exit_code == 0 and len(limited.output.splitlines()) == 3


def test_runs_rejects_a_non_positive_limit(runner: FlaskCliRunner, db: SQLAlchemy) -> None:
    result = runner.invoke(args=["reeval", "runs", "--limit", "0"])
    assert result.exit_code == 2
    assert "Invalid value for '--limit'" in result.output


# ---------------------------------------------------------------------------
# show
# ---------------------------------------------------------------------------


def test_show_activation_run(runner: FlaskCliRunner, db: SQLAlchemy, activated: dict[str, Any]) -> None:
    run = activated["second"]
    result = runner.invoke(args=["reeval", "show", str(run.id)])
    assert result.exit_code == 0, result.output
    output = result.output
    assert f"Re-evaluation run #{run.id}: ClinVar release, Change-driven, success" in output
    assert "  By:       system" in output
    assert "  Releases: R1 → R2" in output
    assert "First activation" not in output
    assert "Change categories:" in output
    category_rows = {
        line.split("  ")[0]: line.split()[-7:]
        for line in output.splitlines()
        if line.startswith(("Upgrade", "Downgrade"))
    }
    assert category_rows["Upgrade into actionable band"] == ["1", "1", "2", "1", "0", "0", "2"]
    assert category_rows["Downgrade out of actionable band"] == ["1", "1", "1", "2", "0", "1", "1"]
    assert "Genes:" in output and any(line.split() == ["GCK", "1", "1"] for line in output.splitlines())
    assert "Changed variants (2):" in output
    upgrade_row = ["12-120994300-C-T", "HNF1A", "VUS", "LP", "unknown", "upgrade", "1"]
    assert any(line.split() == upgrade_row for line in output.splitlines())


def test_show_first_activation(runner: FlaskCliRunner, db: SQLAlchemy, activated: dict[str, Any]) -> None:
    result = runner.invoke(args=["reeval", "show", str(activated["first"].id)])
    assert result.exit_code == 0, result.output
    assert "  Releases: - → R1" in result.output
    assert "  First activation: initial classification, not a reclassification." in result.output


def test_show_rule_revision_run(
    runner: FlaskCliRunner, db: SQLAlchemy, activated: dict[str, Any], admin_user: Any
) -> None:
    run = reevaluation.apply_rule_set(load_rule_set_file(RULES_DATA_DIR / "ruleset_v2.json"), user=admin_user)
    db.session.commit()
    result = runner.invoke(args=["reeval", "show", str(run.id)])
    assert result.exit_code == 0, result.output
    assert "Rule revision, Change-driven, success" in result.output
    assert f"  By:       {admin_user.username}" in result.output
    assert "Rule change: D4 v1 → v2" in result.output
    assert any(line.split() == ["D4", "0", "0", "0", "0", "0"] for line in result.output.splitlines())
    assert any(line.split() == ["KCNJ11", "-", "0"] for line in result.output.splitlines())
    assert "Change categories:" not in result.output


def test_show_new_rule_and_full_recompute(runner: FlaskCliRunner, db: SQLAlchemy, activated: dict[str, Any]) -> None:
    run = reevaluation.apply_rule_set({"rule_set": "t", "label": "t", "rules": [rule_definition("D7", genes=["GCK"])]})
    db.session.commit()
    result = runner.invoke(args=["reeval", "show", str(run.id)])
    assert "Rule change: D7 v1 (new)" in result.output

    full = reevaluation.full_recompute()
    db.session.commit()
    result = runner.invoke(args=["reeval", "show", str(full.id)])
    assert result.exit_code == 0, result.output
    assert "Full recomputation, Full recomputation, success" in result.output
    assert "  Releases: R2 → R2" in result.output


def test_show_failed_run(
    runner: FlaskCliRunner, db: SQLAlchemy, activated: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    def explode(*_args: Any, **_kwargs: Any) -> None:
        raise RuntimeError("out of memory")

    monkeypatch.setattr(alerting, "reconcile_patient", explode)
    with pytest.raises(RuntimeError):
        reevaluation.activate_release(activated["r1"])
    failed = db.session.scalars(select(ReevaluationRun).where(ReevaluationRun.status == "failed")).one()
    result = runner.invoke(args=["reeval", "show", str(failed.id)])
    assert result.exit_code == 0, result.output
    assert "ClinVar release, Change-driven, failed" in result.output
    assert "  Error:    RuntimeError: out of memory" in result.output
    assert db.session.get(KbRelease, activated["r1"].id).is_current is False


def test_show_unknown_run(runner: FlaskCliRunner, db: SQLAlchemy) -> None:
    result = runner.invoke(args=["reeval", "show", "999"])
    assert result.exit_code == 1
    assert result.output == "Error: No re-evaluation run with id 999; list runs with: flask reeval runs\n"
