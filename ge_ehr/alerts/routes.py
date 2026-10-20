"""Placeholder routes of the alerts blueprint (``/alerts...``, ``/notices...``).

Owned by the UI-A work package, which replaces this file keeping the endpoint names, URL rules
and role restrictions (SPEC §8.1, §8.2). Created by the Foundation work package.
"""

from __future__ import annotations

from flask import Blueprint, render_template

from ..constants import CLINICAL_ROLES
from ..security import roles_required

bp = Blueprint("alerts", __name__)


def _placeholder(title: str) -> str:
    return render_template("placeholder.html", title=title, owner="UI-A")


@bp.route("/alerts")
@roles_required(*CLINICAL_ROLES)
def inbox() -> str:
    return _placeholder("Alerts")


@bp.route("/alerts/<int:alert_id>")
@roles_required(*CLINICAL_ROLES)
def view_alert(alert_id: int) -> str:
    return _placeholder("Alert")


@bp.route("/alerts/<int:alert_id>/review", methods=["POST"])
@roles_required(*CLINICAL_ROLES)
def review_alert(alert_id: int) -> str:
    return _placeholder("Review alert")


@bp.route("/notices")
@roles_required(*CLINICAL_ROLES)
def notices() -> str:
    return _placeholder("Review notices")


@bp.route("/notices/<int:notice_id>/acknowledge", methods=["POST"])
@roles_required(*CLINICAL_ROLES)
def acknowledge_notice(notice_id: int) -> str:
    return _placeholder("Acknowledge notice")
