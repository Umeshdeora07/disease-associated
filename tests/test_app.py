"""Tests for the application factory, configuration, templating helpers, error pages, the
normative endpoint/role contract (SPEC §8.1–8.2), the base layout and vendored assets."""

from __future__ import annotations

import logging
import re
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest
from flask import Flask, abort, request
from flask.testing import FlaskClient
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import select
from sqlalchemy.exc import OperationalError

import ge_ehr
from ge_ehr import create_app
from ge_ehr.admin.forms import NewUserForm
from ge_ehr.config import (
    DEFAULT_CLINVAR_SYNC_URL,
    DEVELOPMENT_SECRET_KEY,
    Config,
    DevelopmentConfig,
    TestingConfig,
    database_url,
    get_config_class,
)
from ge_ehr.extensions import db as _db
from ge_ehr.models import KbRelease, Patient, User
from ge_ehr.templating import (
    format_date,
    format_datetime,
    format_duration_ms,
    format_filesize,
    format_number,
    humanize,
    months_to_age,
    pretty_json,
)
from tests.conftest import build_app
from tests.factories import (
    add_patient_variant,
    make_alert,
    make_patient,
    make_release,
    make_review_notice,
    make_rule_version,
    rule_definition,
)

STATIC_ROOT = Path(ge_ehr.__file__).parent / "static"


# ---------------------------------------------------------------------------
# Factory and configuration
# ---------------------------------------------------------------------------


@pytest.fixture
def isolated_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> pytest.MonkeyPatch:
    """No .env loading, no inherited settings, database in a temporary directory."""
    monkeypatch.setattr(ge_ehr, "_load_environment_file", lambda: None)
    for name in ("FLASK_CONFIG", "SECRET_KEY", "DEMO_PASSWORD", "CLINVAR_SYNC_URL", "LOG_LEVEL", "TRUSTED_PROXIES"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'app.db'}")
    return monkeypatch


def test_testing_config(app: Flask) -> None:
    config = app.config
    assert config["CONFIG_NAME"] == "testing" and config["TESTING"] is True
    assert config["SQLALCHEMY_DATABASE_URI"] == "sqlite://"
    assert config["SQLALCHEMY_ENGINE_OPTIONS"]["connect_args"] == {"check_same_thread": False}
    assert config["WTF_CSRF_ENABLED"] is False
    assert config["APP_VERSION"] == ge_ehr.__version__


def test_shared_settings(app: Flask) -> None:
    config = app.config
    assert config["SESSION_COOKIE_HTTPONLY"] is True
    assert config["SESSION_COOKIE_SAMESITE"] == "Lax"
    assert config["PERMANENT_SESSION_LIFETIME"] == timedelta(hours=8)
    assert config["MAX_CONTENT_LENGTH"] == 256 * 1024 * 1024
    assert config["PATIENT_VCF_MAX_BYTES"] == 20 * 1024 * 1024
    assert config["LOGIN_MAX_ATTEMPTS"] == 5 and config["LOGIN_LOCK_MINUTES"] == 15
    assert config["CLINVAR_SYNC_URL"] == DEFAULT_CLINVAR_SYNC_URL


def test_default_configuration_is_development(isolated_env: pytest.MonkeyPatch) -> None:
    application = create_app()
    assert application.config["CONFIG_NAME"] == "development"
    assert application.config["SECRET_KEY"] == DEVELOPMENT_SECRET_KEY
    assert application.config["DEMO_PASSWORD"] == "Demo-Pass-2026!"
    assert application.config["SQLALCHEMY_DATABASE_URI"].endswith("app.db")
    assert application.config["SESSION_COOKIE_SECURE"] is False


def test_environment_overrides(isolated_env: pytest.MonkeyPatch) -> None:
    isolated_env.setenv("FLASK_CONFIG", "development")
    isolated_env.setenv("SECRET_KEY", "from-env")
    isolated_env.setenv("DEMO_PASSWORD", "Other-Demo-1")
    isolated_env.setenv("CLINVAR_SYNC_URL", "https://example.org/clinvar.vcf.gz")
    isolated_env.setenv("LOG_LEVEL", "debug")
    application = create_app()
    assert application.config["SECRET_KEY"] == "from-env"
    assert application.config["DEMO_PASSWORD"] == "Other-Demo-1"
    assert application.config["CLINVAR_SYNC_URL"] == "https://example.org/clinvar.vcf.gz"
    assert logging.getLogger("ge_ehr").level == logging.DEBUG
    create_app({"LOG_LEVEL": "INFO"})


def test_dict_config_overrides_the_named_base(isolated_env: pytest.MonkeyPatch) -> None:
    isolated_env.setenv("FLASK_CONFIG", "testing")
    application = create_app({"ITEMS_PER_PAGE": 7, "LOGIN_MAX_ATTEMPTS": 3})
    assert application.config["CONFIG_NAME"] == "testing"
    assert application.config["ITEMS_PER_PAGE"] == 7 and application.config["LOGIN_MAX_ATTEMPTS"] == 3


def test_testing_config_ignores_database_url(isolated_env: pytest.MonkeyPatch) -> None:
    isolated_env.setenv("DATABASE_URL", "postgresql://nobody@nowhere/db")
    assert create_app("testing").config["SQLALCHEMY_DATABASE_URI"] == "sqlite://"


def test_unknown_configuration_name(isolated_env: pytest.MonkeyPatch) -> None:
    with pytest.raises(ValueError, match="Unknown configuration 'staging'; expected one of development, production, testing"):
        create_app("staging")
    with pytest.raises(TypeError, match="config must be"):
        create_app(42)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("variable", "value", "message"),
    [("TRUSTED_PROXIES", "many", "TRUSTED_PROXIES must be a non-negative integer"), ("LOG_LEVEL", "LOUD", "not a valid logging level")],
)
def test_invalid_environment_values(isolated_env: pytest.MonkeyPatch, variable: str, value: str, message: str) -> None:
    isolated_env.setenv(variable, value)
    with pytest.raises(ValueError, match=message):
        create_app("development")


def test_trusted_proxies_apply_proxy_fix(isolated_env: pytest.MonkeyPatch) -> None:
    isolated_env.setenv("TRUSTED_PROXIES", "1")
    application = create_app("development")

    @application.route("/_ip")
    def ip() -> str:
        return f"{request.remote_addr} {request.scheme}"

    response = application.test_client().get("/_ip", headers={"X-Forwarded-For": "203.0.113.9", "X-Forwarded-Proto": "https"})
    assert response.get_data(as_text=True) == "203.0.113.9 https"


def test_missing_secret_key_is_rejected() -> None:
    with pytest.raises(RuntimeError, match="SECRET_KEY is not set for the development configuration"):
        DevelopmentConfig.validate({"SECRET_KEY": None, "LOG_LEVEL": "INFO"})


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (None, "sqlite:////srv/instance/ge_ehr.db"),
        ("", "sqlite:////srv/instance/ge_ehr.db"),
        ("postgres://u:p@host:5432/db", "postgresql://u:p@host:5432/db"),
        ("postgresql://u:p@host/db", "postgresql://u:p@host/db"),
        ("sqlite:////data/x.db", "sqlite:////data/x.db"),
    ],
)
def test_database_url(raw: str | None, expected: str) -> None:
    assert database_url(raw, "/srv/instance") == expected


def test_config_classes() -> None:
    assert get_config_class("testing") is TestingConfig
    assert issubclass(TestingConfig, Config)
    settings = TestingConfig.settings("/tmp/instance", {"EXTRA": 1})
    assert settings["EXTRA"] == 1 and settings["PASSWORD_HASH_METHOD"].startswith("scrypt")


# ---------------------------------------------------------------------------
# Filters
# ---------------------------------------------------------------------------


def test_datetime_filters() -> None:
    assert format_datetime(datetime(2026, 1, 15, 9, 30, 59)) == "2026-01-15 09:30 UTC"
    aware = datetime(2026, 1, 15, 10, 30, tzinfo=timezone(timedelta(hours=1)))
    assert format_datetime(aware) == "2026-01-15 09:30 UTC"
    assert format_datetime(None) == "—"
    assert format_date(date(2025, 1, 2)) == "2025-01-02"
    assert format_date(datetime(2025, 1, 2, 23, 59)) == "2025-01-02"
    assert format_date(datetime(2025, 1, 2, 0, 30, tzinfo=timezone(timedelta(hours=2)))) == "2025-01-01"
    assert format_date(None) == "—"


@pytest.mark.parametrize(
    ("months", "text"),
    [(None, "—"), (0, "0 months"), (1, "1 month"), (2, "2 months"), (23, "23 months"), (24, "2 years"), (180, "15 years"), (300, "25 years"), (151, "12 years")],
)
def test_months_to_age(months: int | None, text: str) -> None:
    assert months_to_age(months) == text


def test_number_and_size_filters() -> None:
    assert format_filesize(None) == "—"
    assert format_filesize(512) == "512 bytes"
    assert format_filesize(2048) == "2.0 KB"
    assert format_filesize(20 * 1024 * 1024) == "20.0 MB"
    assert format_filesize(3 * 1024**3) == "3.0 GB"
    assert format_number(12345) == "12,345" and format_number(None) == "—"
    assert format_duration_ms(None) == "—"
    assert format_duration_ms(12.4) == "12 ms"
    assert format_duration_ms(2400) == "2.4 s"
    assert format_duration_ms(185_000) == "3 min 05 s"
    assert humanize("change_driven") == "Change driven" and humanize(None) == "—"
    assert pretty_json({"b": 1, "a": [1]}) == '{\n  "a": [\n    1\n  ],\n  "b": 1\n}'


def test_label_filters_are_registered(app: Flask) -> None:
    filters = app.jinja_env.filters
    assert filters["class_label"]("LP") == "Likely pathogenic"
    assert filters["class_label"]("weird_code") == "Weird code"
    assert filters["class_label"](None) == "—"
    assert filters["severity_label"]("medium") == "Medium"
    assert filters["severity_label"](None) == "—"
    assert filters["drug_class_label"]("sulfonylurea") == "Sulfonylurea"
    assert filters["role_label"]("lab_technician") == "Lab technician"
    assert filters["diabetes_type_label"]("none") == "No diabetes"
    assert filters["trigger_label"]("clinvar_release") == "ClinVar release"
    assert filters["category_label"]("upgrade") == "Upgrade into actionable band"
    assert filters["strategy_label"]("full") == "Full recomputation"
    assert filters["superseded_reason_label"]("downgrade") == "Variant downgraded"
    for name in ("dt", "date", "months_to_age", "filesize", "number", "duration_ms", "pretty_json", "humanize", "mechanism_label", "sex_label", "alert_status_label"):
        assert name in filters


# ---------------------------------------------------------------------------
# Layout and context processors
# ---------------------------------------------------------------------------


def test_layout_for_clinician(clinician_client: FlaskClient) -> None:
    html = clinician_client.get("/account/password").get_data(as_text=True)
    assert "GE-EHR" in html and "Monogenic Diabetes" in html
    assert "Synthetic data only · Research prototype · Not for clinical use" in html
    assert "Advisory decision support — for specialist review" in html
    assert 'href="/alerts"' in html and 'href="/notices"' in html and 'href="/knowledge/releases"' in html
    assert 'href="/admin/users"' not in html and 'href="/knowledge/releases/import"' not in html
    assert "No active release" in html
    assert re.search(r'<form method="post" action="/logout"[^>]*>\s*<input type="hidden" name="csrf_token"', html)
    assert "Dr. C. Test" in html and "badge-role-clinician" in html


def test_layout_for_lab_technician_hides_clinical_navigation(lab_client: FlaskClient) -> None:
    html = lab_client.get("/account/password").get_data(as_text=True)
    assert 'href="/patients"' in html
    assert 'href="/alerts"' not in html and 'href="/notices"' not in html
    assert 'href="/knowledge/releases"' not in html and "release-pill" not in html


def test_layout_for_admin(admin_client: FlaskClient) -> None:
    html = admin_client.get("/account/password").get_data(as_text=True)
    for url in ("/admin/users", "/admin/audit", "/knowledge/releases/import", "/knowledge/rules/import"):
        assert f'href="{url}"' in html


def test_layout_for_anonymous_users(client: FlaskClient) -> None:
    html = client.get("/login").get_data(as_text=True)
    assert 'href="/about"' in html
    assert 'action="/logout"' not in html and 'href="/patients"' not in html


def test_navigation_badges_count_active_high_and_medium_alerts(clinician_client: FlaskClient, db: SQLAlchemy) -> None:
    release = make_release("R1 (2025-01, synthetic)", current=True)
    rules = {severity: make_rule_version(rule_definition(f"X{index}", severity=severity)) for index, severity in enumerate(("high", "medium", "low", "info"))}
    variants = [add_patient_variant(make_patient(), "12", 120_978_900, "G", "A", classification="P", release=release) for _ in range(3)]
    make_alert(variants[0], rules["high"], release=release)
    make_alert(variants[0], rules["medium"], release=release)
    make_alert(variants[0], rules["low"], release=release)
    make_alert(variants[0], rules["info"], release=release)
    make_alert(variants[1], rules["high"], release=release, status="reviewed")
    make_alert(variants[2], rules["high"], release=release, status="superseded")
    make_alert(variants[1], rules["medium"], kind="note", release=release)
    make_review_notice(variants[0])
    make_review_notice(variants[1])
    make_review_notice(variants[2], status="acknowledged")
    db.session.commit()
    html = clinician_client.get("/account/password").get_data(as_text=True)
    assert re.search(r'nav-badge-alerts">2<span class="visually-hidden"> active high or medium alerts', html)
    assert re.search(r'nav-badge-notices">2<span class="visually-hidden"> open review notices', html)
    assert "R1 (2025-01, synthetic)" in html
    assert f'href="/knowledge/releases/{release.id}"' in html


def test_navigation_without_data_shows_no_badges(clinician_client: FlaskClient) -> None:
    html = clinician_client.get("/account/password").get_data(as_text=True)
    assert "nav-badge-alerts" not in html and "nav-badge-notices" not in html


def test_layout_survives_database_errors(clinician_client: FlaskClient, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> None:
    def broken() -> KbRelease:
        raise OperationalError("SELECT", {}, Exception("database is locked"))

    monkeypatch.setattr(KbRelease, "current", staticmethod(broken))
    response = clinician_client.get("/account/password")
    assert response.status_code == 200
    assert "Could not load navigation counts" in caplog.text


# ---------------------------------------------------------------------------
# Error pages
# ---------------------------------------------------------------------------


def test_404_page_uses_layout(client: FlaskClient) -> None:
    response = client.get("/no/such/page")
    assert response.status_code == 404
    html = response.get_data(as_text=True)
    assert "Page not found" in html and "Error 404" in html
    assert "Synthetic data only · Research prototype · Not for clinical use" in html


def test_404_json_for_json_clients(client: FlaskClient) -> None:
    response = client.get("/no/such/page", headers={"Accept": "application/json"})
    assert response.status_code == 404
    assert response.get_json()["error"] == "Page not found"


def test_405_page(clinician_client: FlaskClient) -> None:
    response = clinician_client.get("/logout")
    assert response.status_code == 405
    assert "Method Not Allowed" in response.get_data(as_text=True)


def test_413_page() -> None:
    application = build_app({"MAX_CONTENT_LENGTH": 1024 * 1024})
    with application.app_context():
        _db.create_all()
        response = application.test_client().post("/login", data={"username": "x" * (2 * 1024 * 1024), "password": "y"})
        assert response.status_code == 413
        html = response.get_data(as_text=True)
        assert "Upload too large" in html and "maximum upload size of 1 MB" in html
        _db.engine.dispose()


def test_500_page_rolls_back_and_hides_details(caplog: pytest.LogCaptureFixture) -> None:
    application = build_app({"PROPAGATE_EXCEPTIONS": False})

    @application.route("/_boom")
    def boom() -> str:
        _db.session.add(User(username="half.done", full_name="H", role="admin"))
        raise RuntimeError("secret internal detail")

    with application.app_context():
        _db.create_all()
        client = application.test_client()
        response = client.get("/_boom")
        assert response.status_code == 500
        html = response.get_data(as_text=True)
        assert "Something went wrong" in html and "secret internal detail" not in html
        assert not _db.session.new, "the failed request's changes were rolled back"
        assert client.get("/_boom", headers={"Accept": "application/json"}).get_json()["status"] == 500
        _db.engine.dispose()


def test_abort_with_specific_reason_is_shown() -> None:
    application = build_app()

    @application.route("/_bad")
    def bad() -> str:
        abort(400, description="The VCF header has fewer than 10 columns.")

    @application.route("/_plain")
    def plain() -> str:
        abort(400)

    with application.app_context():
        _db.create_all()
        client = application.test_client()
        assert "The VCF header has fewer than 10 columns." in client.get("/_bad").get_data(as_text=True)
        assert "Check the submitted information" in client.get("/_plain").get_data(as_text=True)
        _db.engine.dispose()


# ---------------------------------------------------------------------------
# Health check
# ---------------------------------------------------------------------------


def test_health_without_release(client: FlaskClient) -> None:
    response = client.get("/health")
    assert response.status_code == 200
    assert response.get_json() == {"status": "ok", "db": "ok", "current_release": None}


def test_health_reports_current_release(client: FlaskClient, db: SQLAlchemy) -> None:
    make_release("R2 (2026-01, synthetic)", current=True)
    db.session.commit()
    assert client.get("/health").get_json()["current_release"] == "R2 (2026-01, synthetic)"


def test_health_reports_database_errors(client: FlaskClient, db: SQLAlchemy, monkeypatch: pytest.MonkeyPatch) -> None:
    def fail(*args, **kwargs):
        raise OperationalError("SELECT 1", {}, Exception("connection refused"))

    monkeypatch.setattr(db.session, "execute", fail)
    response = client.get("/health")
    assert response.status_code == 503
    assert response.get_json() == {"status": "error", "db": "error", "current_release": None}


# ---------------------------------------------------------------------------
# Normative endpoints and role restrictions (SPEC §8.1, §8.2)
# ---------------------------------------------------------------------------

ENDPOINTS: list[tuple[str, str, set[str]]] = [
    ("auth.login", "/login", {"GET", "POST"}),
    ("auth.logout", "/logout", {"POST"}),
    ("auth.change_password", "/account/password", {"GET", "POST"}),
    ("main.dashboard", "/", {"GET"}),
    ("main.about", "/about", {"GET"}),
    ("patients.list_patients", "/patients", {"GET"}),
    ("patients.new_patient", "/patients/new", {"GET", "POST"}),
    ("patients.view_patient", "/patients/<>", {"GET"}),
    ("patients.edit_patient", "/patients/<>/edit", {"GET", "POST"}),
    ("patients.update_diagnosis", "/patients/<>/diagnosis", {"POST"}),
    ("patients.add_medication", "/patients/<>/medications", {"POST"}),
    ("patients.stop_medication", "/patients/<>/medications/<>/stop", {"POST"}),
    ("patients.upload_vcf", "/patients/<>/vcf", {"GET", "POST"}),
    ("patients.recommendations", "/patients/<>/recommendations", {"GET"}),
    ("alerts.inbox", "/alerts", {"GET"}),
    ("alerts.view_alert", "/alerts/<>", {"GET"}),
    ("alerts.review_alert", "/alerts/<>/review", {"POST"}),
    ("alerts.notices", "/notices", {"GET"}),
    ("alerts.acknowledge_notice", "/notices/<>/acknowledge", {"POST"}),
    ("knowledge.releases", "/knowledge/releases", {"GET"}),
    ("knowledge.import_release", "/knowledge/releases/import", {"GET", "POST"}),
    ("knowledge.view_release", "/knowledge/releases/<>", {"GET"}),
    ("knowledge.activate_release", "/knowledge/releases/<>/activate", {"POST"}),
    ("knowledge.compare_releases", "/knowledge/releases/compare", {"GET"}),
    ("knowledge.rules", "/knowledge/rules", {"GET"}),
    ("knowledge.import_rules", "/knowledge/rules/import", {"GET", "POST"}),
    ("knowledge.runs", "/knowledge/runs", {"GET"}),
    ("knowledge.view_run", "/knowledge/runs/<>", {"GET"}),
    ("knowledge.recompute", "/knowledge/recompute", {"POST"}),
    ("admin.users", "/admin/users", {"GET"}),
    ("admin.new_user", "/admin/users/new", {"GET", "POST"}),
    ("admin.toggle_user", "/admin/users/<>/toggle", {"POST"}),
    ("admin.reset_password", "/admin/users/<>/reset-password", {"POST"}),
    ("admin.audit", "/admin/audit", {"GET"}),
    ("api.patient_fhir", "/patients/<>/fhir", {"GET"}),
    ("api.health", "/health", {"GET"}),
]


@pytest.mark.parametrize(("endpoint", "rule", "methods"), ENDPOINTS, ids=[e[0] for e in ENDPOINTS])
def test_normative_endpoint(app: Flask, endpoint: str, rule: str, methods: set[str]) -> None:
    rules = [r for r in app.url_map.iter_rules() if r.endpoint == endpoint]
    assert rules, f"endpoint {endpoint} is not registered"
    assert {re.sub(r"<[^>]+>", "<>", r.rule) for r in rules} == {rule}
    assert methods <= set().union(*(r.methods for r in rules))


# (method, url) pairs a lab technician must not reach (SPEC §8.1).
CLINICIAN_ONLY = [
    ("GET", "/patients/new"), ("POST", "/patients/new"), ("GET", "/patients/1/edit"), ("POST", "/patients/1/diagnosis"),
    ("POST", "/patients/1/medications"), ("POST", "/patients/1/medications/1/stop"), ("GET", "/patients/1/recommendations"),
    ("GET", "/alerts"), ("GET", "/alerts/1"), ("POST", "/alerts/1/review"), ("GET", "/notices"), ("POST", "/notices/1/acknowledge"),
    ("GET", "/knowledge/releases"), ("GET", "/knowledge/releases/1"), ("GET", "/knowledge/releases/compare"),
    ("GET", "/knowledge/rules"), ("GET", "/knowledge/runs"), ("GET", "/knowledge/runs/1"), ("GET", "/patients/1/fhir"),
]
ADMIN_ONLY = [
    ("GET", "/knowledge/releases/import"), ("POST", "/knowledge/releases/import"), ("POST", "/knowledge/releases/1/activate"),
    ("GET", "/knowledge/rules/import"), ("POST", "/knowledge/rules/import"), ("POST", "/knowledge/recompute"),
    ("GET", "/admin/users"), ("GET", "/admin/users/new"), ("POST", "/admin/users/1/toggle"), ("POST", "/admin/users/1/reset-password"),
    ("GET", "/admin/audit"),
]


@pytest.mark.parametrize(("method", "url"), CLINICIAN_ONLY + ADMIN_ONLY)
def test_lab_technician_is_denied(lab_client: FlaskClient, method: str, url: str) -> None:
    assert lab_client.open(url, method=method).status_code == 403


@pytest.mark.parametrize(("method", "url"), ADMIN_ONLY)
def test_clinician_is_denied_admin_actions(clinician_client: FlaskClient, method: str, url: str) -> None:
    assert clinician_client.open(url, method=method).status_code == 403


@pytest.mark.parametrize(("method", "url"), [("GET", "/"), ("GET", "/patients"), ("GET", "/patients/1"), ("GET", "/patients/1/vcf")] + CLINICIAN_ONLY + ADMIN_ONLY)
def test_anonymous_users_are_sent_to_login(client: FlaskClient, method: str, url: str) -> None:
    response = client.open(url, method=method)
    if url.endswith("/fhir"):
        assert response.status_code == 401
    else:
        assert response.status_code == 302 and response.headers["Location"].startswith("/login")


# ---------------------------------------------------------------------------
# Vendored assets, macros, CSP hygiene
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("path", "mimetype"),
    [
        ("vendor/bootstrap/css/bootstrap.min.css", "text/css"),
        ("vendor/bootstrap/js/bootstrap.bundle.min.js", "text/javascript"),
        ("vendor/bootstrap-icons/bootstrap-icons.min.css", "text/css"),
        ("vendor/bootstrap-icons/fonts/bootstrap-icons.woff2", "font/woff2"),
        ("vendor/bootstrap-icons/fonts/bootstrap-icons.woff", "font/woff"),
        ("css/app.css", "text/css"),
        ("js/app.js", "text/javascript"),
        ("img/logo.svg", "image/svg+xml"),
    ],
)
def test_static_assets_are_served(client: FlaskClient, path: str, mimetype: str) -> None:
    response = client.get(f"/static/{path}")
    assert response.status_code == 200
    assert response.mimetype == mimetype
    response.close()


def test_vendored_versions_and_licenses() -> None:
    vendor = STATIC_ROOT / "vendor"
    assert "Bootstrap  v5.3.3" in (vendor / "bootstrap/css/bootstrap.min.css").read_text(encoding="utf-8")[:200]
    assert "Bootstrap v5.3.3" in (vendor / "bootstrap/js/bootstrap.bundle.min.js").read_text(encoding="utf-8")[:200]
    assert "Bootstrap Icons v1.11.3" in (vendor / "bootstrap-icons/bootstrap-icons.min.css").read_text(encoding="utf-8")[:200]
    assert 'url("fonts/bootstrap-icons.woff2' in (vendor / "bootstrap-icons/bootstrap-icons.min.css").read_text(encoding="utf-8")
    assert (vendor / "bootstrap/LICENSE").is_file() and (vendor / "bootstrap-icons/LICENSE").is_file()


CSS_STRING = re.compile(r""""(?:[^"\\\n]|\\.)*"|'(?:[^'\\\n]|\\.)*'""")
CSS_COMMENT = re.compile(r"/\*.*?\*/", re.DOTALL)
CSS_TOKEN = re.compile(f"{CSS_STRING.pattern}|{CSS_COMMENT.pattern}", re.DOTALL)


def css_code(css: str) -> str:
    """``css`` without comments and with string literals emptied (either may hold ``/*`` or braces).

    Scanning strings and comments in one pass keeps ``content: "/*"`` from opening a comment and
    a quote inside a comment from opening a string. An unterminated comment is left in place.
    """
    return CSS_TOKEN.sub(lambda match: '""' if match.group(0)[0] in "\"'" else " ", css)


@pytest.mark.parametrize(
    ("css", "code"),
    [
        ("a { b: c; } /* note */ d { }", "a { b: c; }   d { }"),
        ('a::after { content: "/*"; } /* it\'s "quoted" */ b { }', 'a::after { content: ""; }   b { }'),
        ("a { } /* one */ /* two\n lines */ b { }", "a { }     b { }"),
        ("a { } /* unterminated", "a { } /* unterminated"),
    ],
)
def test_css_code_strips_comments_and_strings(css: str, code: str) -> None:
    assert css_code(css) == code


def test_app_css_classification_colours() -> None:
    code = css_code((STATIC_ROOT / "css/app.css").read_text(encoding="utf-8"))
    for cls in ("p", "lp", "vus", "lb", "b", "nc"):
        assert f".badge-class-{cls} " in code
    for severity in ("high", "medium", "low", "info"):
        assert f".badge-severity-{severity} " in code
    assert "/*" not in code and "*/" not in code, "every comment is closed and none is closed twice"
    assert code.count("{") == code.count("}"), "balanced braces"


INLINE_CODE = re.compile(r"<script(?![^>]*\bsrc=)[^>]*>|\sstyle=|\son[a-z]+=", re.IGNORECASE)


def test_foundation_pages_have_no_inline_scripts_or_styles(admin_client: FlaskClient, client: FlaskClient) -> None:
    pages = [client.get("/login"), client.get("/missing"), admin_client.get("/account/password")]
    pages += [admin_client.get(url) for url in ("/admin/users", "/admin/users/new", "/admin/audit")]
    for response in pages:
        html = response.get_data(as_text=True)
        assert not INLINE_CODE.search(html), INLINE_CODE.search(html)


def _render(app: Flask, source: str, **context) -> str:
    with app.test_request_context("/"):
        return app.jinja_env.from_string('{% import "_macros.html" as ui %}' + source).render(**context)


def test_badge_macros(app: Flask) -> None:
    html = _render(app, '{{ ui.class_badge("VUS") }}|{{ ui.class_badge("LP", full=true) }}|{{ ui.class_badge(none) }}')
    assert 'badge-class-vus' in html and ">VUS<span class=\"visually-hidden\"> (Uncertain significance)" in html
    assert ">Likely pathogenic</span>" in html and "badge-class-nc" in html
    html = _render(app, '{{ ui.severity_badge("high") }}|{{ ui.severity_badge(none) }}')
    assert "badge-severity-high" in html and "High" in html and "Notes have no severity" in html
    html = _render(app, '{{ ui.status_badge("superseded") }}{{ ui.status_badge("acknowledged") }}{{ ui.status_badge("odd_state") }}')
    assert "Superseded" in html and "Acknowledged" in html and "Odd state" in html
    html = _render(app, '{{ ui.kind_badge("note") }}{{ ui.kind_badge("alert") }}{{ ui.reclassified_badge() }}{{ ui.expert_panel_badge() }}{{ ui.amended_badge() }}')
    for text in ("Note", "Alert", "Reclassified", "Expert panel", "Amended", "bi-shield-fill-check"):
        assert text in html
    assert "High priority" in _render(app, '{{ ui.priority_badge("high") }}')
    assert _render(app, '{{ ui.priority_badge("normal") }}') == ""
    assert "Lab technician" in _render(app, '{{ ui.role_badge("lab_technician") }}')
    assert '<span class="gene-symbol">HNF1A</span>' in _render(app, '{{ ui.gene("HNF1A") }}')
    assert "Advisory — for specialist review" in _render(app, "{{ ui.advisory_disclaimer() }}")


def test_layout_macros(app: Flask) -> None:
    html = _render(app, '{% call ui.page_header("Patients", "All patients", icon="people") %}<a class="btn">New</a>{% endcall %}')
    assert '<h1 class="page-title"><i class="bi bi-people" aria-hidden="true"></i><span>Patients</span></h1>' in html
    assert '<div class="page-header-actions"><a class="btn">New</a></div>' in html
    assert "page-header-actions" not in _render(app, '{{ ui.page_header("Plain") }}')
    html = _render(app, '{{ ui.empty_state("inbox", "No alerts", "Nothing to review.") }}')
    assert "No alerts" in html and "Nothing to review." in html and "empty-state-actions" not in html
    html = _render(app, '{% call ui.empty_state("inbox", "No alerts") %}<a>Import</a>{% endcall %}')
    assert '<div class="empty-state-actions"><a>Import</a></div>' in html
    html = _render(app, '{{ ui.confirm_button("/x", "Activate", "Activate R2?", variant="primary", icon="check") }}')
    assert 'method="post" action="/x"' in html and 'data-confirm="Activate R2?"' in html and 'name="csrf_token"' in html
    assert 'data-confirm-variant="primary"' in html
    assert "data-confirm=" not in _render(app, '{{ ui.confirm_button("/x", "Go") }}')
    html = _render(app, '{{ ui.stat_card("Active alerts", 4, "bell", href="/alerts", tone="danger") }}')
    assert "stat-card-danger" in html and 'href="/alerts"' in html


def test_pagination_macro(app: Flask, db: SQLAlchemy) -> None:
    for index in range(30):
        make_patient(mrn=f"PG-{index:03d}")
    pagination = db.paginate(select(Patient).order_by(Patient.mrn), page=2, per_page=10)
    html = _render(app, '{{ ui.pagination(p, "patients.list_patients", q="pg", family=none) }}', p=pagination)
    assert "Showing 11–20 of 30" in html
    assert 'href="/patients?page=1&amp;q=pg"' in html and 'href="/patients?page=3&amp;q=pg"' in html
    assert "family" not in html
    single = db.paginate(select(Patient), page=1, per_page=100)
    assert "30 records" in _render(app, '{{ ui.pagination(p, "patients.list_patients") }}', p=single)


def test_render_field_macro(app: Flask) -> None:
    with app.test_request_context("/", method="POST", data={"username": "x", "role": "clinician"}):
        form = NewUserForm()
        form.validate()
        template = app.jinja_env.from_string(
            '{% import "_macros.html" as ui %}{{ ui.render_field(form.username) }}{{ ui.render_field(form.role) }}'
            "{{ ui.render_field(form.must_change_password) }}{{ ui.render_form_errors(form) }}"
        )
        html = template.render(form=form)
    assert 'class="form-control is-invalid"' in html and 'aria-invalid="true"' in html
    assert 'aria-describedby="username-help username-error"' in html
    assert '<div class="invalid-feedback" id="username-error">' in html
    assert 'class="form-select"' in html
    assert 'class="form-check-input"' in html and 'class="form-check-label"' in html
    assert '<span class="required-mark" aria-hidden="true">*</span>' in html
