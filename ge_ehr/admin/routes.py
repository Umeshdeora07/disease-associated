"""Routes under ``/admin``: user management and the audit log (admin role only).

Owned by the Foundation work package (UI-B may polish the audit page).
"""

from __future__ import annotations

import logging

from flask import Blueprint, flash, redirect, render_template, request, url_for
from flask_login import current_user, login_user
from markupsafe import Markup
from sqlalchemy import select
from werkzeug.wrappers import Response

from ..extensions import db
from ..models import AuditLog, User
from ..security import audit, generate_temporary_password, roles_required
from .forms import NewUserForm

logger = logging.getLogger(__name__)

bp = Blueprint("admin", __name__, url_prefix="/admin")

USERS_PER_PAGE = 50
AUDIT_PER_PAGE = 50


@bp.route("/users")
@roles_required("admin")
def users() -> str:
    """All user accounts."""
    pagination = db.paginate(
        select(User).order_by(User.username),
        page=request.args.get("page", 1, type=int),
        per_page=USERS_PER_PAGE,
        error_out=False,
    )
    return render_template("admin/users.html", pagination=pagination)


@bp.route("/users/new", methods=["GET", "POST"])
@roles_required("admin")
def new_user() -> Response | str:
    """Create a user account."""
    form = NewUserForm()
    if form.validate_on_submit():
        user = User(
            username=form.username.data,
            full_name=form.full_name.data,
            email=form.email.data or None,
            role=form.role.data,
            must_change_password=form.must_change_password.data,
        )
        user.set_password(form.password.data)
        db.session.add(user)
        db.session.flush()
        audit("user_created", "user", user.id, detail={"username": user.username, "role": user.role})
        db.session.commit()
        flash(f"User {user.username} ({user.role_label}) created.", "success")
        return redirect(url_for("admin.users"))
    return render_template("admin/new_user.html", form=form)


@bp.route("/users/<int:user_id>/toggle", methods=["POST"])
@roles_required("admin")
def toggle_user(user_id: int) -> Response:
    """Activate or deactivate an account (never your own)."""
    user = db.get_or_404(User, user_id)
    if user.id == current_user.id:
        flash("You cannot deactivate your own account.", "danger")
        return redirect(url_for("admin.users"))
    user.active = not user.active
    audit("user_activated" if user.active else "user_deactivated", "user", user.id, detail={"username": user.username})
    db.session.commit()
    flash(f"User {user.username} {'activated' if user.active else 'deactivated'}.", "success")
    return redirect(url_for("admin.users"))


@bp.route("/users/<int:user_id>/reset-password", methods=["POST"])
@roles_required("admin")
def reset_password(user_id: int) -> Response:
    """Set a temporary password (shown once), unlock the account and force a password change."""
    user = db.get_or_404(User, user_id)
    temporary = generate_temporary_password()
    user.set_password(temporary)
    user.must_change_password = True
    user.failed_logins = 0
    user.locked_until = None
    audit("password_reset", "user", user.id, detail={"username": user.username})
    db.session.commit()
    if user.id == current_user.id:
        login_user(user)  # keep the admin's own session valid after the fingerprint change
    flash(
        Markup(
            "Temporary password for <strong>{}</strong>: <code class=\"user-select-all\">{}</code>. "
            "It is shown only once — pass it on securely. The user must choose a new password at "
            "the next sign-in."
        ).format(user.username, temporary),
        "credential",
    )
    logger.info("Password of %s reset by %s", user.username, current_user.username)
    return redirect(url_for("admin.users"))


@bp.route("/audit", endpoint="audit")
@roles_required("admin")
def audit_log() -> str:
    """Audit log, newest first, filterable by action and username."""
    action = request.args.get("action", "").strip()
    username = request.args.get("user", "").strip().lower()
    statement = select(AuditLog).order_by(AuditLog.at.desc(), AuditLog.id.desc())
    if action:
        statement = statement.where(AuditLog.action == action)
    if username:
        statement = statement.where(AuditLog.username == username)
    pagination = db.paginate(
        statement, page=request.args.get("page", 1, type=int), per_page=AUDIT_PER_PAGE, error_out=False
    )
    actions = db.session.scalars(select(AuditLog.action).distinct().order_by(AuditLog.action)).all()
    usernames = db.session.scalars(
        select(AuditLog.username).where(AuditLog.username.is_not(None)).distinct().order_by(AuditLog.username)
    ).all()
    return render_template(
        "admin/audit.html",
        pagination=pagination,
        actions=actions,
        usernames=usernames,
        filters={"action": action, "user": username},
    )
