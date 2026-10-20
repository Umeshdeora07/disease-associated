"""Placeholder routes of the main blueprint (``/``, ``/about``).

Owned by the UI-B work package, which replaces this file keeping the endpoint names.
Created by the Foundation work package so that ``url_for`` works for every normative endpoint.
"""

from __future__ import annotations

from flask import Blueprint, render_template

from ..security import roles_required

bp = Blueprint("main", __name__)


def _placeholder(title: str) -> str:
    return render_template("placeholder.html", title=title, owner="UI-B")


@bp.route("/")
@roles_required()
def dashboard() -> str:
    return _placeholder("Dashboard")


@bp.route("/about")
def about() -> str:
    return _placeholder("About GE-EHR")
