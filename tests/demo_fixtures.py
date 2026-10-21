"""Fixtures and helpers shared by the UI-B route tests (main and knowledge blueprints, audit page).

* ``demo_app`` — one app per test module holding the full demo (``seed_demo(full=True)``: rules
  v1 → v2, releases R1 → R2), for read-only page tests; ``demo_admin``, ``demo_clinician`` and
  ``demo_lab`` are the demo accounts signed in to it.
* ``seed_snapshot`` / ``seeded`` — the plain demo seed (rules v1, release R1, 15 patients) built
  once per module and restored into each test's own in-memory database with SQLite's backup
  API, so tests that change data stay isolated without re-running the seed; ``admin`` and
  ``clinician`` are signed in to it.

Import the fixtures a module uses by name (pytest registers fixtures found in a test module).
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager

import pytest
from flask import Flask
from flask.testing import FlaskClient
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import event
from werkzeug.test import TestResponse

from ge_ehr.demo import seed_demo
from ge_ehr.extensions import db as _db
from tests.conftest import build_app, login_client

DEMO_PASSWORD = "Demo-Pass-2026!"


def text(response: TestResponse) -> str:
    """The response body as text."""
    return response.get_data(as_text=True)


def flashes(client: FlaskClient) -> list[tuple[str, str]]:
    """``(category, message)`` of the flash messages waiting in the client's session."""
    with client.session_transaction() as session:
        return list(session.get("_flashes", []))


@contextmanager
def count_queries(db: SQLAlchemy) -> Iterator[list[str]]:
    """Collect the SQL statements executed on the current app's engine inside the block."""
    statements: list[str] = []

    def record(conn, cursor, statement, parameters, context, executemany) -> None:  # noqa: ANN001
        statements.append(statement)

    event.listen(db.engine, "before_cursor_execute", record)
    try:
        yield statements
    finally:
        event.remove(db.engine, "before_cursor_execute", record)


def sign_in(application: Flask, username: str) -> FlaskClient:
    """A test client of ``application`` signed in as the demo account ``username``."""
    client = application.test_client()
    response = login_client(client, username, DEMO_PASSWORD)
    assert response.status_code == 302 and "/login" not in response.headers["Location"], f"login of {username}"
    return client


def restore(snapshot: sqlite3.Connection) -> None:
    """Copy ``snapshot`` into the current app's in-memory database (replacing its content)."""
    raw = _db.engine.raw_connection()
    try:
        snapshot.backup(raw.driver_connection)
    finally:
        raw.close()


@pytest.fixture(scope="module")
def demo_app() -> Iterator[Flask]:
    """One app with the full demo (rules v1 → v2, R1 → R2) shared by a module's read-only tests."""
    application = build_app()
    with application.app_context():
        _db.create_all()
        seed_demo(full=True)
        _db.session.commit()
        try:
            yield application
        finally:
            _db.session.remove()
            _db.drop_all()
            _db.engine.dispose()


@pytest.fixture(scope="module")
def demo_admin(demo_app: Flask) -> FlaskClient:
    return sign_in(demo_app, "admin")


@pytest.fixture(scope="module")
def demo_clinician(demo_app: Flask) -> FlaskClient:
    return sign_in(demo_app, "dr.rao")


@pytest.fixture(scope="module")
def demo_lab(demo_app: Flask) -> FlaskClient:
    return sign_in(demo_app, "lab.kim")


@pytest.fixture(scope="module")
def seed_snapshot() -> Iterator[sqlite3.Connection]:
    """The plain demo seed (rules v1, release R1 current, 15 patients), built once per module."""
    application = build_app()
    snapshot = sqlite3.connect(":memory:")
    with application.app_context():
        _db.create_all()
        seed_demo()
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
def seeded(app: Flask, db: SQLAlchemy, seed_snapshot: sqlite3.Connection) -> Flask:
    """A per-test copy of the plain demo seed in the test's own in-memory database."""
    restore(seed_snapshot)
    return app


@pytest.fixture
def admin(seeded: Flask) -> FlaskClient:
    return sign_in(seeded, "admin")


@pytest.fixture
def clinician(seeded: Flask) -> FlaskClient:
    return sign_in(seeded, "dr.rao")


@pytest.fixture
def lab(seeded: Flask) -> FlaskClient:
    return sign_in(seeded, "lab.kim")
