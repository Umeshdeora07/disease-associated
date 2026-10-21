"""Routes of the main blueprint: the role-aware dashboard (``/``) and the about page (``/about``).

The dashboard shows clinicians and administrators the cohort, alert and knowledge-base overview;
lab technicians get a laboratory view without any alert information (SPEC §8.1). The about page
is public, so the system description and disclaimer can be read before signing in.

Owned by the UI-B work package.
"""

from __future__ import annotations

from flask import Blueprint, render_template
from flask_login import current_user

from .. import constants as C
from ..models import utcnow
from ..security import roles_required
from . import queries

bp = Blueprint("main", __name__)


@bp.route("/")
@roles_required()
def dashboard() -> str:
    """Clinical overview for clinicians and administrators, laboratory overview for lab technicians."""
    if current_user.has_role(*C.CLINICAL_ROLES):
        return render_template("main/dashboard.html", data=queries.clinical_dashboard(), now=utcnow())
    return render_template("main/lab_dashboard.html", data=queries.lab_dashboard(), now=utcnow())


@bp.route("/about")
def about() -> str:
    """What GE-EHR does, its scope and limitations, and the references of the design paper."""
    return render_template("main/about.html", genes=list(C.GENE_INFO.values()))
