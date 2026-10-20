"""Shared pytest fixtures for the whole suite.

* ``app`` — a fresh ``create_app("testing")`` (private in-memory SQLite, CSRF off) with all
  tables created and an application context pushed for the duration of the test, so tests can
  use ``db.session`` directly. Requests made with the test client run in that same context
  (and therefore share ``db.session``); commit test data before making requests.
* ``client``, ``runner``, ``db``, ``session``.
* Users ``admin_user``, ``clinician_user``, ``lab_user`` (password ``Test-Pass-123!``), the
  ``login`` helper fixture (``login(client, user)``) and signed-in clients ``admin_client``,
  ``clinician_client``, ``lab_client``.
"""

from __future__ import annotations

import os
from collections.abc import Callable, Iterator

import pytest
from flask import Flask, g
from flask.testing import FlaskClient, FlaskCliRunner
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy.orm import scoped_session
from werkzeug.test import TestResponse

# Any create_app({...}) call made by a test builds on the testing configuration.
os.environ["FLASK_CONFIG"] = "testing"

from ge_ehr import create_app  # noqa: E402
from ge_ehr.extensions import db as _db  # noqa: E402
from ge_ehr.models import User  # noqa: E402
from tests.factories import TEST_PASSWORD, make_user  # noqa: E402

LoginHelper = Callable[..., TestResponse]


def _reset_request_globals() -> None:
    """Give each request an empty ``g``.

    The ``app`` fixture keeps one application context pushed, and Flask reuses it for test
    requests, so without this ``g`` (Flask-Login's cached user, Flask-WTF's CSRF token, ...)
    would leak from one request — or one client — into the next.
    """
    for name in list(g):
        g.pop(name)


def build_app(overrides: dict | None = None) -> Flask:
    """A testing app (optionally with config overrides) isolated like the ``app`` fixture."""
    application = create_app(overrides) if overrides else create_app("testing")
    application.before_request_funcs.setdefault(None, []).insert(0, _reset_request_globals)
    return application


@pytest.fixture
def app() -> Iterator[Flask]:
    application = build_app()
    with application.app_context():
        _db.create_all()
        try:
            yield application
        finally:
            _db.session.remove()
            _db.drop_all()
            _db.engine.dispose()


@pytest.fixture
def client(app: Flask) -> FlaskClient:
    return app.test_client()


@pytest.fixture
def runner(app: Flask) -> FlaskCliRunner:
    return app.test_cli_runner()


@pytest.fixture
def db(app: Flask) -> SQLAlchemy:
    return _db


@pytest.fixture
def session(db: SQLAlchemy) -> scoped_session:
    return db.session


@pytest.fixture
def admin_user(db: SQLAlchemy) -> User:
    user = make_user("admin.test", role="admin", full_name="Ada Admin")
    db.session.commit()
    return user


@pytest.fixture
def clinician_user(db: SQLAlchemy) -> User:
    user = make_user("dr.test", role="clinician", full_name="Dr. C. Test")
    db.session.commit()
    return user


@pytest.fixture
def lab_user(db: SQLAlchemy) -> User:
    user = make_user("lab.test", role="lab_technician", full_name="L. Technician")
    db.session.commit()
    return user


def login_client(client: FlaskClient, user: User | str, password: str = TEST_PASSWORD) -> TestResponse:
    """POST the login form; returns the (unfollowed) response."""
    username = user if isinstance(user, str) else user.username
    return client.post("/login", data={"username": username, "password": password})


@pytest.fixture
def login() -> LoginHelper:
    """``login(client, user, password=TEST_PASSWORD)`` → response of the login POST."""
    return login_client


def _signed_in_client(app: Flask, user: User) -> FlaskClient:
    client = app.test_client()
    response = login_client(client, user)
    assert response.status_code == 302 and "/login" not in response.headers["Location"], (
        f"login of {user.username} failed"
    )
    return client


@pytest.fixture
def admin_client(app: Flask, admin_user: User) -> FlaskClient:
    return _signed_in_client(app, admin_user)


@pytest.fixture
def clinician_client(app: Flask, clinician_user: User) -> FlaskClient:
    return _signed_in_client(app, clinician_user)


@pytest.fixture
def lab_client(app: Flask, lab_user: User) -> FlaskClient:
    return _signed_in_client(app, lab_user)
