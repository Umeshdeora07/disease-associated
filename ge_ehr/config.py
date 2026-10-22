"""Configuration classes: Config / DevelopmentConfig / TestingConfig / ProductionConfig.

Class attributes hold static defaults; :meth:`Config.settings` adds the values read from the
environment *when the app is created* (not at import time), then explicit overrides, then
validates the result. :func:`instance_secret_key` gives local installs (``python run.py``) a
persistent random SECRET_KEY instead of the fixed development one. Owned by the Foundation work
package; the secret-key file and VCF backend check were added for local installs (portability).
"""

from __future__ import annotations

import logging
import os
import secrets
from collections.abc import Mapping
from datetime import timedelta
from pathlib import Path
from typing import Any, ClassVar

from sqlalchemy.pool import StaticPool

DEFAULT_DEMO_PASSWORD = "Demo-Pass-2026!"
DEFAULT_CLINVAR_SYNC_URL = "https://ftp.ncbi.nlm.nih.gov/pub/clinvar/vcf_GRCh38/clinvar.vcf.gz"
DEVELOPMENT_SECRET_KEY = "ge-ehr-development-only-secret-key"  # never accepted in production
SQLITE_FILE_NAME = "ge_ehr.db"
SECRET_KEY_FILE_NAME = "secret_key"
MIN_SECRET_KEY_LENGTH = 16


def database_url(raw: str | None, instance_path: str) -> str:
    """Return the SQLAlchemy URL: ``DATABASE_URL`` if given, else SQLite in the instance folder.

    ``postgres://`` URLs (issued by some hosting providers) are rewritten to ``postgresql://``,
    the only scheme SQLAlchemy accepts.
    """
    if not raw:
        return "sqlite:///" + os.path.join(instance_path, SQLITE_FILE_NAME)
    if raw.startswith("postgres://"):
        return "postgresql://" + raw.removeprefix("postgres://")
    return raw


def instance_secret_key(instance_path: str | os.PathLike[str]) -> str:
    """The persistent random SECRET_KEY of an instance folder, created on first use.

    The key lives in ``<instance>/secret_key`` (64 hex characters from :mod:`secrets`, written
    with owner-only permissions where the operating system supports them), so sessions survive
    restarts and no shared key is hard-coded. Two processes starting at once agree on one key
    (the file is created exclusively). An empty file is replaced; a key shorter than 16
    characters is refused with RuntimeError (delete the file to get a new one).
    """
    path = Path(instance_path) / SECRET_KEY_FILE_NAME
    path.parent.mkdir(parents=True, exist_ok=True)
    for _attempt in range(2):
        try:
            key = path.read_text(encoding="utf-8").strip()
        except FileNotFoundError:
            key = ""
        else:
            if len(key) >= MIN_SECRET_KEY_LENGTH:
                return key
            if key:
                raise RuntimeError(
                    f"{path} holds a SECRET_KEY shorter than {MIN_SECRET_KEY_LENGTH} characters; "
                    "delete the file to generate a new random key."
                )
            path.unlink(missing_ok=True)  # an empty file left by an interrupted first start
        try:
            # O_BINARY (Windows only): no CRT newline translation, so the file is the same everywhere.
            descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0), 0o600)
        except FileExistsError:
            continue  # another process created it first: read its key
        key = secrets.token_hex(32)
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(key + "\n")
        return key
    raise RuntimeError(f"Could not create {path}; check that the instance folder is writable.")


def _non_negative_int(name: str, raw: str) -> int:
    try:
        value = int(raw)
    except ValueError:
        value = -1
    if value < 0:
        raise ValueError(f"{name} must be a non-negative integer, got {raw!r}.")
    return value


class Config:
    """Settings shared by every environment."""

    NAME: ClassVar[str] = "base"

    APP_NAME = "GE-EHR · Monogenic Diabetes"
    SECRET_KEY: str | None = None

    SQLALCHEMY_DATABASE_URI: str | None = None
    SQLALCHEMY_ENGINE_OPTIONS: ClassVar[dict[str, Any]] = {"pool_pre_ping": True}
    SQLALCHEMY_TRACK_MODIFICATIONS = False

    SESSION_COOKIE_HTTPONLY = True
    SESSION_COOKIE_SAMESITE = "Lax"
    SESSION_COOKIE_SECURE = False
    REMEMBER_COOKIE_HTTPONLY = True
    REMEMBER_COOKIE_SAMESITE = "Lax"
    REMEMBER_COOKIE_SECURE = False
    PERMANENT_SESSION_LIFETIME = timedelta(hours=8)

    MAX_CONTENT_LENGTH = 256 * 1024 * 1024
    PATIENT_VCF_MAX_BYTES = 20 * 1024 * 1024

    WTF_CSRF_ENABLED = True
    # CSRF tokens live as long as the session (8 h) instead of Flask-WTF's default 1 h, so a
    # form left open during a clinic session can still be submitted.
    WTF_CSRF_TIME_LIMIT = None

    LOGIN_MAX_ATTEMPTS = 5
    LOGIN_LOCK_MINUTES = 15
    # Werkzeug hash method; plain "scrypt" uses Werkzeug's default work factor (N=2**15).
    PASSWORD_HASH_METHOD = "scrypt"

    ITEMS_PER_PAGE = 25
    TRUSTED_PROXIES = 0
    LOG_LEVEL = "INFO"
    DEMO_PASSWORD = DEFAULT_DEMO_PASSWORD
    CLINVAR_SYNC_URL = DEFAULT_CLINVAR_SYNC_URL

    @classmethod
    def settings(cls, instance_path: str, overrides: Mapping[str, Any] | None = None) -> dict[str, Any]:
        """Return the complete, validated settings for this configuration.

        Precedence (lowest first): class attributes, environment variables, ``overrides``.
        """
        values: dict[str, Any] = {key: getattr(cls, key) for key in dir(cls) if key.isupper()}
        values.update(cls.environment_settings(instance_path))
        if overrides:
            values.update(overrides)
        cls.validate(values)
        return values

    @classmethod
    def environment_settings(cls, instance_path: str) -> dict[str, Any]:
        """Read the settings that come from environment variables."""
        env = os.environ
        values: dict[str, Any] = {
            "SQLALCHEMY_DATABASE_URI": database_url(env.get("DATABASE_URL"), instance_path),
            "DEMO_PASSWORD": env.get("DEMO_PASSWORD") or DEFAULT_DEMO_PASSWORD,
            "CLINVAR_SYNC_URL": env.get("CLINVAR_SYNC_URL") or DEFAULT_CLINVAR_SYNC_URL,
            "LOG_LEVEL": (env.get("LOG_LEVEL") or cls.LOG_LEVEL).upper(),
        }
        if env.get("SECRET_KEY"):
            values["SECRET_KEY"] = env["SECRET_KEY"]
        if env.get("TRUSTED_PROXIES"):
            values["TRUSTED_PROXIES"] = _non_negative_int("TRUSTED_PROXIES", env["TRUSTED_PROXIES"])
        return values

    @classmethod
    def validate(cls, values: Mapping[str, Any]) -> None:
        """Reject inconsistent settings with a clear message."""
        if not isinstance(logging.getLevelName(str(values["LOG_LEVEL"]).upper()), int):
            raise ValueError(f"LOG_LEVEL {values['LOG_LEVEL']!r} is not a valid logging level.")
        if not values.get("SECRET_KEY"):
            raise RuntimeError(f"SECRET_KEY is not set for the {cls.NAME} configuration.")
        # GE_EHR_VCF_BACKEND is read by the VCF readers on every call; check it at start-up so a
        # typo or a missing cyvcf2 is reported now rather than at the first upload.
        from .services.vcf_backend import vcf_backend

        vcf_backend()


class DevelopmentConfig(Config):
    """Local development: SQLite in ``instance/`` and a fixed, non-secret key unless one is set."""

    NAME = "development"
    SECRET_KEY = DEVELOPMENT_SECRET_KEY


class TestingConfig(Config):
    """pytest: private in-memory SQLite database, CSRF off, cheap password hashing."""

    NAME = "testing"
    TESTING = True
    SECRET_KEY = "ge-ehr-testing-secret-key"
    SQLALCHEMY_ENGINE_OPTIONS: ClassVar[dict[str, Any]] = {
        "poolclass": StaticPool,
        "connect_args": {"check_same_thread": False},
    }
    WTF_CSRF_ENABLED = False
    # Still scrypt, but with a low work factor so the suite stays fast.
    PASSWORD_HASH_METHOD = "scrypt:1024:8:1"

    @classmethod
    def environment_settings(cls, instance_path: str) -> dict[str, Any]:
        """Like :class:`Config`, but tests never touch a database named in ``DATABASE_URL``."""
        values = super().environment_settings(instance_path)
        values["SQLALCHEMY_DATABASE_URI"] = "sqlite://"
        return values


class ProductionConfig(Config):
    """Deployment behind HTTPS: secure cookies, mandatory SECRET_KEY, one trusted proxy."""

    NAME = "production"
    SESSION_COOKIE_SECURE = True
    REMEMBER_COOKIE_SECURE = True
    PREFERRED_URL_SCHEME = "https"
    TRUSTED_PROXIES = 1

    @classmethod
    def validate(cls, values: Mapping[str, Any]) -> None:
        """Refuse to start without a real SECRET_KEY."""
        secret = values.get("SECRET_KEY")
        if not secret or secret == DEVELOPMENT_SECRET_KEY:
            raise RuntimeError(
                "SECRET_KEY must be set in the environment for the production configuration "
                "(generate one with: python -c \"import secrets; print(secrets.token_hex(32))\"); "
                "refusing to start."
            )
        super().validate(values)


CONFIGS: dict[str, type[Config]] = {
    "development": DevelopmentConfig,
    "testing": TestingConfig,
    "production": ProductionConfig,
}


def get_config_class(name: str) -> type[Config]:
    """Return the configuration class registered under ``name``."""
    try:
        return CONFIGS[name]
    except KeyError:
        raise ValueError(
            f"Unknown configuration {name!r}; expected one of {', '.join(sorted(CONFIGS))}."
        ) from None
