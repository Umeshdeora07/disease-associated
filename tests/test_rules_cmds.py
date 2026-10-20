"""Tests for the ``flask rules`` command group: validate, bootstrap, import and list."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from flask.testing import FlaskCliRunner
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import func, select

from ge_ehr.models import ReevaluationRun, RuleVersion
from ge_ehr.services import reevaluation
from ge_ehr.services.alerting import import_rule_versions
from ge_ehr.services.rules import RULES_DATA_DIR, RuleDefinitionError, load_rule_set_file
from tests.factories import add_patient_variant, make_patient, make_run, rule_definition

V1_PATH = str(RULES_DATA_DIR / "ruleset_v1.json")
V2_PATH = str(RULES_DATA_DIR / "ruleset_v2.json")


def write_json(tmp_path: Path, doc: Any, name: str = "rules.json") -> str:
    path = tmp_path / name
    path.write_text(json.dumps(doc), encoding="utf-8")
    return str(path)


def stored_versions(db: SQLAlchemy) -> list[tuple[str, int, bool]]:
    db.session.expire_all()
    versions = db.session.scalars(select(RuleVersion).order_by(RuleVersion.code, RuleVersion.version))
    return [(version.code, version.version, version.is_current) for version in versions]


# ---------------------------------------------------------------------------
# validate
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(("path", "label", "d4"), [(V1_PATH, "v1 (2025)", "D4 v1"), (V2_PATH, "v2 (2026)", "D4 v2")])
def test_validate_bundled_rule_sets(runner: FlaskCliRunner, db: SQLAlchemy, path: str, label: str, d4: str) -> None:
    result = runner.invoke(args=["rules", "validate", path])
    assert result.exit_code == 0, result.output
    assert result.output == (
        f"{path}: valid rule set {label!r} with 6 rule definition(s): D1 v1, D2 v1, D3 v1, {d4}, D5 v1, D6 v1.\n"
    )
    assert stored_versions(db) == [], "validate does not touch the database"


def test_validate_rule_set_without_label(runner: FlaskCliRunner, tmp_path: Path) -> None:
    path = write_json(tmp_path, {"rules": [rule_definition("D2")]})
    result = runner.invoke(args=["rules", "validate", path])
    assert result.exit_code == 0
    assert "valid rule set (no label) with 1 rule definition(s): D2 v1." in result.output


@pytest.mark.parametrize(
    ("content", "message"),
    [
        (
            {"rules": [rule_definition("D2", genes=["HNF1C"])]},
            "Rule D2 v1: 'genetic.genes' contains unknown gene 'HNF1C'; allowed: GCK, HNF1A, HNF4A, HNF1B, KCNJ11, ABCC8, INS.",
        ),
        ({"rules": []}, "Rule set: 'rules' must be a non-empty list of rule definitions, got list."),
        ('{"rules": [', "Rule set is not valid JSON: Expecting value (line 1, column 12)."),
    ],
)
def test_validate_reports_invalid_files(runner: FlaskCliRunner, tmp_path: Path, content: Any, message: str) -> None:
    path = tmp_path / "bad.json"
    path.write_text(content if isinstance(content, str) else json.dumps(content), encoding="utf-8")
    result = runner.invoke(args=["rules", "validate", str(path)])
    assert result.exit_code == 1
    assert result.output == f"Error: {path}: {message}\n"


def test_validate_missing_file(runner: FlaskCliRunner, tmp_path: Path) -> None:
    result = runner.invoke(args=["rules", "validate", str(tmp_path / "missing.json")])
    assert result.exit_code == 2
    assert "does not exist" in result.output


# ---------------------------------------------------------------------------
# bootstrap
# ---------------------------------------------------------------------------


def test_bootstrap_fresh_database(runner: FlaskCliRunner, db: SQLAlchemy) -> None:
    result = runner.invoke(args=["rules", "bootstrap", V1_PATH])
    assert result.exit_code == 0, result.output
    assert result.output == (
        "Bootstrapped rule set 'v1 (2025)': 6 new rule version(s) stored, 6 current version change(s). "
        "Current rules: D1 v1, D2 v1, D3 v1, D4 v1, D5 v1, D6 v1.\n"
    )
    db.session.rollback()
    assert stored_versions(db) == [(f"D{n}", 1, True) for n in range(1, 7)], "the import was committed"

    again = runner.invoke(args=["rules", "bootstrap", V1_PATH])
    assert again.exit_code == 0
    assert again.output == "The rule set 'v1 (2025)' is already imported; nothing changed.\n"


def test_bootstrap_next_rule_set_without_patients(runner: FlaskCliRunner, db: SQLAlchemy) -> None:
    assert runner.invoke(args=["rules", "bootstrap", V1_PATH]).exit_code == 0
    result = runner.invoke(args=["rules", "bootstrap", V2_PATH])
    assert result.exit_code == 0, result.output
    assert "1 new rule version(s) stored, 1 current version change(s)" in result.output
    assert "Current rules: D1 v1, D2 v1, D3 v1, D4 v2, D5 v1, D6 v1." in result.output
    assert ("D4", 1, False) in stored_versions(db) and ("D4", 2, True) in stored_versions(db)


def test_bootstrap_refuses_to_change_rules_once_variants_are_stored(runner: FlaskCliRunner, db: SQLAlchemy) -> None:
    import_rule_versions(load_rule_set_file(V1_PATH))
    add_patient_variant(make_patient(), "11", 17410300, "G", "A", classification="P")
    db.session.commit()

    result = runner.invoke(args=["rules", "bootstrap", V2_PATH])

    assert result.exit_code == 1
    assert result.output == (
        "Error: Refusing to bootstrap: rule set 'v2 (2026)' would change the current rules (D4 v1 → v2) while "
        "1 patient variant(s) are stored, so their alerts would no longer match. "
        f"Use 'flask rules import {V2_PATH}' to import and re-evaluate the affected patients.\n"
    )
    assert ("D4", 2, True) not in stored_versions(db)
    assert runner.invoke(args=["rules", "bootstrap", V1_PATH]).exit_code == 0, "a no-op bootstrap is allowed"


def test_bootstrap_rejects_conflicting_definitions(runner: FlaskCliRunner, db: SQLAlchemy, tmp_path: Path) -> None:
    assert runner.invoke(args=["rules", "bootstrap", V1_PATH]).exit_code == 0
    doc = load_rule_set_file(V1_PATH)
    doc["rules"][0]["severity"] = "high"
    path = write_json(tmp_path, doc)

    result = runner.invoke(args=["rules", "bootstrap", path])

    assert result.exit_code == 1
    assert f"Error: {path}: Rule D1 v1 is already stored with a different definition." in result.output
    assert len(stored_versions(db)) == 6


def test_bootstrap_rejects_invalid_files(runner: FlaskCliRunner, db: SQLAlchemy, tmp_path: Path) -> None:
    path = write_json(tmp_path, {"rules": [rule_definition("D2", severity="urgent")]})
    result = runner.invoke(args=["rules", "bootstrap", path])
    assert result.exit_code == 1
    assert "Rule D2 v1: 'severity' must be one of high, medium, low, info; got 'urgent'." in result.output
    assert stored_versions(db) == []


# ---------------------------------------------------------------------------
# import (re-evaluation is delegated to reevaluation.apply_rule_set)
# ---------------------------------------------------------------------------


@pytest.fixture
def apply_calls(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """Replace ``reevaluation.apply_rule_set`` by a fake that imports the versions and logs a run."""
    calls: list[dict[str, Any]] = []

    def fake_apply_rule_set(doc: dict[str, Any], *, user: Any = None, strategy: str = "change_driven") -> ReevaluationRun:
        calls.append({"doc": doc, "user": user, "strategy": strategy})
        changes = import_rule_versions(doc, user=user)
        return make_run(
            "rule_revision",
            strategy=strategy,
            status="success",
            rule_change=[{key: change[key] for key in ("code", "old_version", "new_version")} for change in changes],
            patients_reevaluated=3,
            records_examined=4,
            alerts_created=1,
            notifications=1,
            duration_ms=12.34,
        )

    monkeypatch.setattr(reevaluation, "apply_rule_set", fake_apply_rule_set)
    return calls


def test_import_applies_the_rule_set_and_commits(runner: FlaskCliRunner, db: SQLAlchemy, apply_calls: list[dict[str, Any]]) -> None:
    import_rule_versions(load_rule_set_file(V1_PATH))
    db.session.commit()

    result = runner.invoke(args=["rules", "import", V2_PATH])

    assert result.exit_code == 0, result.output
    assert len(apply_calls) == 1
    assert apply_calls[0]["doc"] == load_rule_set_file(V2_PATH)
    assert (apply_calls[0]["strategy"], apply_calls[0]["user"]) == ("change_driven", None)
    run_id = db.session.scalar(select(func.max(ReevaluationRun.id)))
    assert result.output.splitlines() == [
        f"Imported rule set 'v2 (2026)' from {V2_PATH}.",
        "Revised rules: D4 v1 → v2.",
        f"Re-evaluation run #{run_id} (rule_revision, change_driven, success): 3 patient(s) re-evaluated, "
        "4 record(s) examined; 1 alert(s) and 0 note(s) created, 0 superseded, 0 confirmed; "
        "0 review notice(s), 1 notification(s); 12.3 ms.",
    ]
    db.session.rollback()
    assert ("D4", 2, True) in stored_versions(db), "the command committed the import"


def test_import_full_strategy_and_new_rules(
    runner: FlaskCliRunner, db: SQLAlchemy, apply_calls: list[dict[str, Any]], tmp_path: Path
) -> None:
    path = write_json(tmp_path, {"label": "extra", "rules": [rule_definition("D7", genes=["INS"])]})
    result = runner.invoke(args=["rules", "import", path, "--strategy", "full"])
    assert result.exit_code == 0, result.output
    assert apply_calls[0]["strategy"] == "full"
    assert "Revised rules: D7 (new) v1." in result.output
    assert "(rule_revision, full, success)" in result.output


def test_import_identical_rule_set(runner: FlaskCliRunner, db: SQLAlchemy, apply_calls: list[dict[str, Any]]) -> None:
    import_rule_versions(load_rule_set_file(V1_PATH))
    db.session.commit()
    result = runner.invoke(args=["rules", "import", V1_PATH])
    assert result.exit_code == 0, result.output
    assert "No current rule version changed." in result.output


def test_import_reports_definition_errors(runner: FlaskCliRunner, db: SQLAlchemy, monkeypatch: pytest.MonkeyPatch) -> None:
    def failing_apply_rule_set(doc: dict[str, Any], **kwargs: Any) -> ReevaluationRun:
        raise RuleDefinitionError("Rule D2 v1 is already stored with a different definition.")

    monkeypatch.setattr(reevaluation, "apply_rule_set", failing_apply_rule_set)
    result = runner.invoke(args=["rules", "import", V1_PATH])
    assert result.exit_code == 1
    assert result.output == f"Error: {V1_PATH}: Rule D2 v1 is already stored with a different definition.\n"


def test_import_validates_the_file_before_re_evaluating(
    runner: FlaskCliRunner, apply_calls: list[dict[str, Any]], tmp_path: Path
) -> None:
    path = write_json(tmp_path, {"rules": [{"code": "D2"}]})
    result = runner.invoke(args=["rules", "import", path])
    assert result.exit_code == 1
    assert "Rule D2: missing required field(s)" in result.output
    assert apply_calls == []


def test_import_rejects_unknown_strategy(runner: FlaskCliRunner, apply_calls: list[dict[str, Any]]) -> None:
    result = runner.invoke(args=["rules", "import", V2_PATH, "--strategy", "patient"])
    assert result.exit_code == 2
    assert "'patient' is not one of 'change_driven', 'full'" in result.output
    assert apply_calls == []


# ---------------------------------------------------------------------------
# list
# ---------------------------------------------------------------------------


def test_list_without_rules(runner: FlaskCliRunner, db: SQLAlchemy) -> None:
    result = runner.invoke(args=["rules", "list"])
    assert result.exit_code == 0
    assert result.output == (
        "No rule versions stored. Load the initial rules with: flask rules bootstrap data/rules/ruleset_v1.json\n"
    )


def test_list_rule_versions(runner: FlaskCliRunner, db: SQLAlchemy) -> None:
    import_rule_versions(load_rule_set_file(V1_PATH))
    import_rule_versions(load_rule_set_file(V2_PATH))
    import_rule_versions({"rules": [rule_definition("D10", genes=["INS"], severity="low", kind="cascade")]})
    db.session.commit()

    lines = runner.invoke(args=["rules", "list"]).output.splitlines()

    assert lines[0].split() == ["CODE", "VERSION", "CURRENT", "KIND", "SEVERITY", "GENETIC", "CRITERION", "CLINICAL", "CONDITION", "RULE", "SET"]
    assert [line.split()[:3] for line in lines[2:]] == [
        ["D1", "1", "yes"], ["D2", "1", "yes"], ["D3", "1", "yes"], ["D4", "1", "no"], ["D4", "2", "yes"],
        ["D5", "1", "yes"], ["D6", "1", "yes"], ["D10", "1", "yes"],
    ]
    d2 = lines[3]
    assert "treatment" in d2 and "high" in d2 and "HNF1A · P/LP" in d2
    assert "On insulin or metformin · not on a sulfonylurea" in d2 and d2.endswith("v1 (2025)")
    assert "KCNJ11 or ABCC8 · P/LP · activating" in lines[6] and lines[6].endswith("v2 (2026)")
    assert lines[9].endswith("-"), "a version without rule-set label"

    current = runner.invoke(args=["rules", "list", "--current"]).output.splitlines()
    assert [line.split()[:2] for line in current[2:]] == [
        ["D1", "1"], ["D2", "1"], ["D3", "1"], ["D4", "2"], ["D5", "1"], ["D6", "1"], ["D10", "1"],
    ]
