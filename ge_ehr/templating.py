"""Jinja filters and context processors shared by every template.

Owned by the Foundation work package (registered by :func:`ge_ehr.create_app`).
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable, Mapping
from datetime import date, datetime, timezone
from typing import Any

from flask import Flask
from flask_login import current_user
from sqlalchemy import func, select
from sqlalchemy.exc import SQLAlchemyError

from . import constants as C
from .extensions import db
from .models import Alert, KbRelease, ReviewNotice

logger = logging.getLogger(__name__)

EMPTY = "—"
# Severities counted in the navbar alert badge (SPEC §8.3: "current high/medium alerts").
BADGE_SEVERITIES: tuple[str, ...] = ("high", "medium")


# ---------------------------------------------------------------------------
# Filters
# ---------------------------------------------------------------------------


def format_datetime(value: datetime | None) -> str:
    """``2026-01-15 09:30 UTC``; naive datetimes are taken to be UTC."""
    if value is None:
        return EMPTY
    if value.tzinfo is not None:
        value = value.astimezone(timezone.utc)
    return value.strftime("%Y-%m-%d %H:%M UTC")


def format_date(value: date | datetime | None) -> str:
    """``2026-01-15``."""
    if value is None:
        return EMPTY
    if isinstance(value, datetime):
        value = (value.astimezone(timezone.utc) if value.tzinfo else value).date()
    return value.strftime("%Y-%m-%d")


def months_to_age(months: int | None) -> str:
    """Age from a month count: ``'2 months'`` below two years, otherwise ``'15 years'``."""
    if months is None:
        return EMPTY
    if months < 24:
        return f"{months} month" if months == 1 else f"{months} months"
    return f"{months // 12} years"


def format_filesize(size: int | None) -> str:
    """Human-readable byte count, e.g. ``'1.4 MB'``."""
    if size is None:
        return EMPTY
    if size < 1024:
        return f"{size} bytes"
    value = size / 1024
    for unit in ("KB", "MB"):
        if value < 1024:
            return f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} GB"


def format_number(value: int | float | None) -> str:
    """Thousands separators: ``12,345``."""
    if value is None:
        return EMPTY
    return f"{value:,}"


def format_duration_ms(value: float | None) -> str:
    """``'850 ms'``, ``'2.4 s'`` or ``'3 min 05 s'``."""
    if value is None:
        return EMPTY
    if value < 1000:
        return f"{value:.0f} ms"
    seconds = value / 1000
    if seconds < 60:
        return f"{seconds:.1f} s"
    minutes, rest = divmod(int(round(seconds)), 60)
    return f"{minutes} min {rest:02d} s"


def pretty_json(value: Any) -> str:
    """Indented, key-sorted JSON for audit details and rule definitions."""
    return json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False, default=str)


def humanize(value: str | None) -> str:
    """``'change_driven'`` → ``'Change driven'``."""
    if not value:
        return EMPTY
    text = str(value).replace("_", " ")
    return text[:1].upper() + text[1:]


def _label_filter(labels: Mapping[str, str]) -> Callable[[str | None], str]:
    def label(code: str | None) -> str:
        if code is None or code == "":
            return EMPTY
        return labels.get(code, humanize(code))

    return label


LABEL_FILTERS: dict[str, Mapping[str, str]] = {
    "class_label": C.CLASS_LABELS,
    "severity_label": C.SEVERITY_LABELS,
    "drug_class_label": C.DRUG_CLASS_LABELS,
    "role_label": C.ROLE_LABELS,
    "diabetes_type_label": C.DIABETES_TYPE_LABELS,
    "trigger_label": C.TRIGGER_LABELS,
    "category_label": C.CHANGE_CATEGORY_LABELS,
    "mechanism_label": C.MECHANISM_LABELS,
    "sex_label": C.SEX_LABELS,
    "alert_status_label": C.ALERT_STATUS_LABELS,
    "strategy_label": C.RUN_STRATEGY_LABELS,
    "superseded_reason_label": C.SUPERSEDED_REASON_LABELS,
}


# ---------------------------------------------------------------------------
# Context processors
# ---------------------------------------------------------------------------


def _user_has_role(*roles: str) -> bool:
    return bool(current_user.is_authenticated and current_user.has_role(*roles))


def navigation_context() -> dict[str, Any]:
    """Badge counts and the current release for the signed-in user's navbar.

    Lab technicians get no alert/notice counts (they may not see alerts). Database errors are
    logged and degrade to empty values so that error pages still render.
    """
    context: dict[str, Any] = {"nav_counts": None, "current_release": None}
    if not current_user.is_authenticated:
        return context
    try:
        context["current_release"] = KbRelease.current()
        if current_user.has_role(*C.CLINICAL_ROLES):
            alerts = db.session.scalar(
                select(func.count(Alert.id)).where(
                    Alert.status == "active", Alert.kind == "alert", Alert.severity.in_(BADGE_SEVERITIES)
                )
            )
            notices = db.session.scalar(select(func.count(ReviewNotice.id)).where(ReviewNotice.status == "open"))
            context["nav_counts"] = {"alerts": alerts or 0, "notices": notices or 0}
    except SQLAlchemyError:
        logger.exception("Could not load navigation counts")
        db.session.rollback()
    return context


def register_template_helpers(app: Flask) -> None:
    """Register filters, globals and context processors on ``app``."""
    filters: dict[str, Callable[..., str]] = {
        "dt": format_datetime,
        "date": format_date,
        "months_to_age": months_to_age,
        "filesize": format_filesize,
        "number": format_number,
        "duration_ms": format_duration_ms,
        "pretty_json": pretty_json,
        "humanize": humanize,
    }
    filters.update({name: _label_filter(labels) for name, labels in LABEL_FILTERS.items()})
    for name, function in filters.items():
        app.add_template_filter(function, name)

    app.jinja_env.globals.update(
        C=C,
        app_name=app.config["APP_NAME"],
        app_version=app.config["APP_VERSION"],
        synthetic_notice=C.SYNTHETIC_DATA_NOTICE,
        advisory_disclaimer=C.ADVISORY_DISCLAIMER,
        has_role=_user_has_role,
    )

    @app.context_processor
    def inject_user_context() -> dict[str, Any]:
        return {
            "is_admin": _user_has_role("admin"),
            "is_clinical": _user_has_role(*C.CLINICAL_ROLES),
            "current_role": current_user.role if current_user.is_authenticated else None,
            **navigation_context(),
        }
