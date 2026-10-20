"""Tests for the CLI: init-db, create-user, list-users and command registration."""

from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest
from flask.testing import FlaskCliRunner
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import inspect, select

from ge_ehr.cli import echo_table
from ge_ehr.models import AuditLog, User
from tests.factories import make_user

REPO_ROOT = Path(__file__).resolve().parent.parent


def test_init_db_creates_tables(runner: FlaskCliRunner, db: SQLAlchemy) -> None:
    db.drop_all()
    result = runner.invoke(args=["init-db"])
    assert result.exit_code == 0, result.output
    assert "Database initialised: 17 tables" in result.output
    assert "users" in inspect(db.engine).get_table_names()


def test_init_db_is_idempotent_and_keeps_data(runner: FlaskCliRunner, db: SQLAlchemy) -> None:
    make_user("keep.me")
    db.session.commit()
    assert runner.invoke(args=["init-db"]).exit_code == 0
    assert db.session.execute(select(User).filter_by(username="keep.me")).scalar_one()


def test_init_db_drop_with_yes(runner: FlaskCliRunner, db: SQLAlchemy) -> None:
    make_user("drop.me")
    db.session.commit()
    result = runner.invoke(args=["init-db", "--drop", "--yes"])
    assert result.exit_code == 0, result.output
    assert "Dropped all tables." in result.output
    db.session.expire_all()
    assert db.session.scalars(select(User)).all() == []


def test_init_db_drop_asks_for_confirmation(runner: FlaskCliRunner, db: SQLAlchemy) -> None:
    make_user("safe.user")
    db.session.commit()
    result = runner.invoke(args=["init-db", "--drop"], input="n\n")
    assert result.exit_code == 1
    assert "Drop ALL tables and data" in result.output and "Aborted" in result.output
    assert db.session.execute(select(User).filter_by(username="safe.user")).scalar_one()


def test_create_user_with_password_option(runner: FlaskCliRunner, db: SQLAlchemy) -> None:
    result = runner.invoke(
        args=["create-user", "Dr.Rao", "--role", "clinician", "--full-name", "Dr. A. Rao", "--email", "rao@example.org", "--password", "Clinic-Pass-2026"]
    )
    assert result.exit_code == 0, result.output
    assert "Created clinician account 'dr.rao'." in result.output
    user = db.session.execute(select(User).filter_by(username="dr.rao")).scalar_one()
    assert (user.full_name, user.email, user.role, user.must_change_password) == ("Dr. A. Rao", "rao@example.org", "clinician", False)
    assert user.check_password("Clinic-Pass-2026")
    entry = db.session.execute(select(AuditLog).filter_by(action="user_created")).scalar_one()
    assert entry.detail == {"username": "dr.rao", "role": "clinician", "via": "cli"}


def test_create_user_prompts_for_password(runner: FlaskCliRunner, db: SQLAlchemy) -> None:
    result = runner.invoke(
        args=["create-user", "lab.kim", "--role", "lab_technician", "--full-name", "J. Kim", "--must-change-password"],
        input="weak\nLab-Pass-2026x\nLab-Pass-2026x\n",
    )
    assert result.exit_code == 0, result.output
    assert "at least 10 characters" in result.output, "a weak password is rejected and re-prompted"
    assert "Lab-Pass-2026x" not in result.output, "input is hidden"
    user = db.session.execute(select(User).filter_by(username="lab.kim")).scalar_one()
    assert user.check_password("Lab-Pass-2026x") and user.must_change_password is True


def test_create_user_prompt_requires_matching_confirmation(runner: FlaskCliRunner, db: SQLAlchemy) -> None:
    result = runner.invoke(
        args=["create-user", "admin2", "--role", "admin", "--full-name", "A. Two"],
        input="Admin-Pass-2026\nAdmin-Pass-2027\nAdmin-Pass-2026\nAdmin-Pass-2026\n",
    )
    assert result.exit_code == 0, result.output
    assert "do not match" in result.output


@pytest.mark.parametrize(
    ("args", "message"),
    [
        (["x", "--role", "admin", "--full-name", "X", "--password", "Admin-Pass-2026"], "2–64 characters"),
        (["bad name", "--role", "admin", "--full-name", "X", "--password", "Admin-Pass-2026"], "2–64 characters"),
        (["ok.user", "--role", "nurse", "--full-name", "X", "--password", "Admin-Pass-2026"], "'nurse' is not one of"),
        (["ok.user", "--role", "admin", "--full-name", "X", "--password", "short"], "at least 10 characters"),
        (["ok.user", "--role", "admin", "--full-name", "X", "--password", "Admin-Pass-2026", "--email", "nope"], "not a valid email address"),
        (["ok.user", "--role", "admin", "--password", "Admin-Pass-2026"], "Missing option '--full-name'"),
    ],
)
def test_create_user_rejects_invalid_input(runner: FlaskCliRunner, db: SQLAlchemy, args: list[str], message: str) -> None:
    result = runner.invoke(args=["create-user", *args])
    assert result.exit_code == 2
    assert message in result.output
    assert db.session.scalars(select(User)).all() == []


def test_create_user_rejects_duplicates(runner: FlaskCliRunner, db: SQLAlchemy) -> None:
    make_user("taken")
    db.session.commit()
    result = runner.invoke(args=["create-user", "TAKEN", "--role", "admin", "--full-name", "T", "--password", "Admin-Pass-2026"])
    assert result.exit_code == 1
    assert "A user named 'taken' already exists." in result.output


def test_list_users(runner: FlaskCliRunner, db: SQLAlchemy) -> None:
    assert "No users." in runner.invoke(args=["list-users"]).output
    make_user("b.user", role="lab_technician", full_name="B User", active=False)
    make_user("a.user", role="admin", full_name="A User", must_change_password=True)
    db.session.commit()
    lines = runner.invoke(args=["list-users"]).output.splitlines()
    assert lines[0].split() == ["USERNAME", "FULL", "NAME", "ROLE", "ACTIVE", "MUST", "CHANGE", "PW", "LAST", "LOGIN", "(UTC)"]
    assert lines[2].split()[:6] == ["a.user", "A", "User", "admin", "yes", "yes"]
    assert lines[3].split() == ["b.user", "B", "User", "lab_technician", "no", "no", "-"]


def test_echo_table(capsys: pytest.CaptureFixture[str]) -> None:
    echo_table(("A", "LONGER"), [("x", None), ("wide value", 3)])
    assert capsys.readouterr().out.splitlines() == [
        "A           LONGER",
        "----------  ------",
        "x           -",
        "wide value  3",
    ]


@pytest.mark.parametrize("group", ["clinvar", "rules", "reeval"])
def test_command_groups_are_registered(runner: FlaskCliRunner, group: str) -> None:
    result = runner.invoke(args=[group, "--help"])
    assert result.exit_code == 0, result.output
    assert "Usage: " in result.output


def test_seed_demo_command_is_registered(runner: FlaskCliRunner) -> None:
    result = runner.invoke(args=["seed-demo", "--help"])
    assert result.exit_code == 0
    assert "--full" in result.output and "--reset" in result.output


def test_wsgi_entry_point_end_to_end(tmp_path: Path) -> None:
    """`flask --app wsgi init-db` and `create-user` against a real SQLite file."""
    database = tmp_path / "e2e.db"
    env = {
        **os.environ,
        "FLASK_CONFIG": "development",
        "DATABASE_URL": f"sqlite:///{database}",
        "PYTHONPATH": str(REPO_ROOT),
    }
    flask = [sys.executable, "-m", "flask", "--app", "wsgi"]

    def run(*args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run([*flask, *args], cwd=REPO_ROOT, env=env, capture_output=True, text=True, timeout=60)

    init = run("init-db")
    assert init.returncode == 0, init.stderr
    assert "17 tables" in init.stdout
    created = run("create-user", "e2e.admin", "--role", "admin", "--full-name", "E2E Admin", "--password", "E2e-Admin-Pass-1")
    assert created.returncode == 0, created.stderr
    listed = run("list-users")
    assert "e2e.admin" in listed.stdout
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT username, role FROM users").fetchall() == [("e2e.admin", "admin")]
