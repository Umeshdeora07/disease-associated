"""GE-EHR for monogenic diabetes — Genomic-Enhanced EHR with reclassification-aware re-evaluation.

Research prototype that stores synthetic data only; recommendations are advisory. This module
holds the application factory (:func:`create_app`), which wires configuration, extensions,
blueprints, CLI commands, error pages and security headers. Owned by the Foundation work package.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Mapping
from typing import Any

from dotenv import load_dotenv
from flask import Flask, Response, flash, jsonify, redirect, render_template, request
from flask_login import login_url
from flask_wtf.csrf import CSRFError
from werkzeug.exceptions import HTTPException
from werkzeug.middleware.proxy_fix import ProxyFix

from .config import get_config_class

__version__ = "1.0.0"

logger = logging.getLogger(__name__)

# SPEC §8.4 — Bootstrap and Bootstrap Icons are vendored, so everything is served from 'self'.
SECURITY_HEADERS: dict[str, str] = {
    "Content-Security-Policy": (
        "default-src 'self'; img-src 'self' data:; style-src 'self'; script-src 'self'; frame-ancestors 'none'"
    ),
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "same-origin",
    "X-Frame-Options": "DENY",
}

# Friendly (title, message, Bootstrap icon) per HTTP status; Werkzeug's text is used otherwise.
ERROR_PAGES: dict[int, tuple[str, str, str]] = {
    400: (
        "Bad request",
        "The request could not be processed. Check the submitted information and try again.",
        "exclamation-octagon",
    ),
    403: (
        "Access denied",
        "Your role does not permit access to this page. Access attempts are recorded in the audit log.",
        "shield-lock",
    ),
    404: (
        "Page not found",
        "The page or record you asked for does not exist. It may have been mistyped or removed.",
        "signpost-split",
    ),
    413: (
        "Upload too large",
        "The request exceeds the maximum upload size of {max_mb} MB.",
        "file-earmark-x",
    ),
    500: (
        "Something went wrong",
        "An unexpected error occurred and has been logged. Please try again; if the problem persists, "
        "contact the system administrator.",
        "bug",
    ),
}
CSRF_ERROR_MESSAGE = (
    "The form's security token is missing or has expired. Reload the page and submit the form again."
)


def create_app(config: str | Mapping[str, Any] | None = None) -> Flask:
    """Create and configure the Flask application.

    ``config`` is a configuration name (``"development"``, ``"testing"``, ``"production"``), a
    mapping of settings applied on top of the configuration named by ``FLASK_CONFIG`` (default
    ``development``), or None to use ``FLASK_CONFIG`` alone. A ``.env`` file is loaded first
    (without overriding real environment variables) except for the testing configuration.
    """
    if config != "testing":
        _load_environment_file()
    name, overrides = _resolve_config(config)

    app = Flask(__name__, instance_relative_config=True)
    app.config.from_mapping(get_config_class(name).settings(app.instance_path, overrides))
    app.config["CONFIG_NAME"] = name
    app.config["APP_VERSION"] = __version__

    _ensure_instance_folder(app)
    _configure_logging(app)
    _configure_proxy(app)
    _init_extensions(app)
    _register_blueprints(app)
    _register_error_handlers(app)
    _register_security_headers(app)

    from .cli import register_cli
    from .templating import register_template_helpers

    register_template_helpers(app)
    register_cli(app)
    return app


def _load_environment_file() -> None:
    """Load ``.env`` from the working directory or a parent, if present."""
    load_dotenv(override=False)


def _resolve_config(config: str | Mapping[str, Any] | None) -> tuple[str, dict[str, Any]]:
    """Return (configuration name, overrides) for :func:`create_app`'s ``config`` argument."""
    default_name = os.environ.get("FLASK_CONFIG") or "development"
    if config is None:
        return default_name, {}
    if isinstance(config, str):
        return config, {}
    if isinstance(config, Mapping):
        return default_name, dict(config)
    raise TypeError(f"config must be a configuration name, a mapping or None, not {type(config).__name__}.")


def _ensure_instance_folder(app: Flask) -> None:
    """Create ``instance/`` when the default SQLite database lives there."""
    if app.config["SQLALCHEMY_DATABASE_URI"].startswith("sqlite:///" + app.instance_path):
        os.makedirs(app.instance_path, exist_ok=True)


def _configure_logging(app: Flask) -> None:
    """Set the package log level; add a stderr handler when nothing else configured logging."""
    package_logger = logging.getLogger("ge_ehr")
    package_logger.setLevel(app.config["LOG_LEVEL"])
    if not app.testing and not package_logger.handlers and not logging.getLogger().handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s [%(name)s] %(message)s"))
        package_logger.addHandler(handler)


def _configure_proxy(app: Flask) -> None:
    """Trust X-Forwarded-For/-Proto/-Host from ``TRUSTED_PROXIES`` reverse proxies."""
    hops = app.config["TRUSTED_PROXIES"]
    if hops:
        app.wsgi_app = ProxyFix(app.wsgi_app, x_for=hops, x_proto=hops, x_host=hops)  # type: ignore[method-assign]


def _init_extensions(app: Flask) -> None:
    from .extensions import csrf, db, login_manager, migrate
    from .models import User

    db.init_app(app)
    migrate.init_app(app, db, render_as_batch=True)
    csrf.init_app(app)
    login_manager.init_app(app)

    @login_manager.user_loader
    def load_user(session_id: str) -> User | None:
        """Resolve the session id written by :meth:`User.get_id` (inactive users are rejected)."""
        return User.from_session_id(session_id)

    @login_manager.unauthorized_handler
    def unauthorized() -> Response | tuple[Response, int]:
        if wants_json_response():
            return jsonify(error="unauthorized", message="Authentication is required."), 401
        flash(login_manager.login_message, login_manager.login_message_category)
        return redirect(login_url(login_manager.login_view, next_url=request.url))


def _register_blueprints(app: Flask) -> None:
    from .admin import bp as admin_bp
    from .alerts import bp as alerts_bp
    from .api import bp as api_bp
    from .auth import bp as auth_bp
    from .knowledge import bp as knowledge_bp
    from .main import bp as main_bp
    from .patients import bp as patients_bp

    for blueprint in (auth_bp, admin_bp, main_bp, patients_bp, alerts_bp, knowledge_bp, api_bp):
        app.register_blueprint(blueprint)


def wants_json_response() -> bool:
    """True for API endpoints and for clients that prefer JSON over HTML."""
    if request.blueprint == "api":
        return True
    accept = request.accept_mimetypes
    best = accept.best_match(["text/html", "application/json", "application/fhir+json"])
    return best is not None and best != "text/html" and accept[best] > accept["text/html"]


def _register_error_handlers(app: Flask) -> None:
    from .extensions import db

    @app.errorhandler(HTTPException)
    def handle_http_error(error: HTTPException) -> tuple[Response | str, int]:
        code = error.code or 500
        if code >= 500:
            # The failed request may have left the session unusable; templates query it.
            db.session.rollback()
        title, message, icon = ERROR_PAGES.get(code, (error.name, error.description or "", "exclamation-circle"))
        if code == 413:
            message = message.format(max_mb=app.config["MAX_CONTENT_LENGTH"] // (1024 * 1024))
        if isinstance(error, CSRFError):
            message = CSRF_ERROR_MESSAGE
        elif code in ERROR_PAGES and code < 500 and error.description and error.description != type(error).description:
            message = error.description  # a specific reason given with abort(code, description=...)
        if wants_json_response():
            return jsonify(error=title, message=message, status=code), code
        return render_template("errors/error.html", code=code, title=title, message=message, icon=icon), code


def _register_security_headers(app: Flask) -> None:
    hsts = app.config["SESSION_COOKIE_SECURE"]

    @app.after_request
    def set_security_headers(response: Response) -> Response:
        for header, value in SECURITY_HEADERS.items():
            response.headers.setdefault(header, value)
        if request.endpoint != "static":
            # Pages show (synthetic) patient data; never store them in shared caches.
            response.headers.setdefault("Cache-Control", "no-store")
        if hsts:
            response.headers.setdefault("Strict-Transport-Security", "max-age=31536000; includeSubDomains")
        return response
