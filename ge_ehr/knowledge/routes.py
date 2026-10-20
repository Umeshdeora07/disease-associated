"""Placeholder routes of the knowledge blueprint (``/knowledge/...``).

Owned by the UI-B work package, which replaces this file keeping the endpoint names, URL rules
and role restrictions (SPEC §8.1, §8.2). Created by the Foundation work package.
"""

from __future__ import annotations

from flask import Blueprint, render_template

from ..constants import CLINICAL_ROLES
from ..security import roles_required

bp = Blueprint("knowledge", __name__, url_prefix="/knowledge")


def _placeholder(title: str) -> str:
    return render_template("placeholder.html", title=title, owner="UI-B")


@bp.route("/releases")
@roles_required(*CLINICAL_ROLES)
def releases() -> str:
    return _placeholder("Knowledge-base releases")


@bp.route("/releases/import", methods=["GET", "POST"])
@roles_required("admin")
def import_release() -> str:
    return _placeholder("Import release")


@bp.route("/releases/<int:release_id>")
@roles_required(*CLINICAL_ROLES)
def view_release(release_id: int) -> str:
    return _placeholder("Release")


@bp.route("/releases/<int:release_id>/activate", methods=["POST"])
@roles_required("admin")
def activate_release(release_id: int) -> str:
    return _placeholder("Activate release")


@bp.route("/releases/compare")
@roles_required(*CLINICAL_ROLES)
def compare_releases() -> str:
    return _placeholder("Compare releases")


@bp.route("/rules")
@roles_required(*CLINICAL_ROLES)
def rules() -> str:
    return _placeholder("Rules")


@bp.route("/rules/import", methods=["GET", "POST"])
@roles_required("admin")
def import_rules() -> str:
    return _placeholder("Import rules")


@bp.route("/runs")
@roles_required(*CLINICAL_ROLES)
def runs() -> str:
    return _placeholder("Re-evaluation runs")


@bp.route("/runs/<int:run_id>")
@roles_required(*CLINICAL_ROLES)
def view_run(run_id: int) -> str:
    return _placeholder("Re-evaluation run")


@bp.route("/recompute", methods=["POST"])
@roles_required("admin")
def recompute() -> str:
    return _placeholder("Full recomputation")
