"""Routes: ``/login``, ``/logout`` (POST), ``/account/password``.

Sign-in failures share one generic message. After ``LOGIN_MAX_ATTEMPTS`` consecutive wrong
passwords the account is locked for ``LOGIN_LOCK_MINUTES``. Users flagged
``must_change_password`` can only reach the password page until they choose a new password.
Owned by the Foundation work package.
"""

from __future__ import annotations

import logging
import secrets
from datetime import timedelta
from functools import lru_cache

from flask import Blueprint, current_app, flash, redirect, render_template, request, session, url_for
from flask_login import current_user, login_required, login_user, logout_user
from sqlalchemy import select
from werkzeug.security import check_password_hash, generate_password_hash
from werkzeug.wrappers import Response

from ..extensions import db
from ..models import User, utcnow
from ..security import audit, normalize_username, safe_next_url
from .forms import ChangePasswordForm, LoginForm

logger = logging.getLogger(__name__)

bp = Blueprint("auth", __name__)

LOGIN_FAILED_MESSAGE = (
    "Sign-in failed. Check your username and password. After repeated failures the account is "
    "locked for a short time."
)
# Endpoints a user who must change their password may still reach.
PASSWORD_CHANGE_EXEMPT_ENDPOINTS = frozenset(
    {"static", "auth.login", "auth.logout", "auth.change_password", "api.health", "main.about"}
)


@lru_cache(maxsize=4)
def _dummy_password_hash(method: str) -> str:
    """Hash checked for unknown usernames so failures take the same time either way."""
    return generate_password_hash(secrets.token_hex(16), method=method)


def _authenticate(username: str, password: str) -> User | None:
    """Return the user for valid credentials; otherwise record the failure and return None."""
    config = current_app.config
    now = utcnow()
    user = db.session.execute(select(User).filter_by(username=normalize_username(username))).scalar_one_or_none()
    if user is None:
        check_password_hash(_dummy_password_hash(config["PASSWORD_HASH_METHOD"]), password)
        audit("login_failed", detail={"username": normalize_username(username)[:64], "reason": "unknown_user"})
        db.session.commit()
        return None

    password_ok = user.check_password(password)
    locked_now = False
    if user.is_locked(now):
        reason = "locked"
    elif not user.active:
        reason = "inactive"
    elif not password_ok:
        reason = "wrong_password"
        user.failed_logins += 1
        if user.failed_logins >= config["LOGIN_MAX_ATTEMPTS"]:
            user.locked_until = now + timedelta(minutes=config["LOGIN_LOCK_MINUTES"])
            user.failed_logins = 0
            locked_now = True
    else:
        return user

    audit("login_failed", "user", user.id, detail={"reason": reason}, user=user)
    if locked_now:
        audit(
            "account_locked",
            "user",
            user.id,
            detail={"minutes": config["LOGIN_LOCK_MINUTES"], "until": user.locked_until},
            user=user,
        )
        logger.warning("Account %s locked after repeated failed sign-ins", user.username)
    db.session.commit()
    return None


def _complete_login(user: User, next_url: str | None) -> Response:
    """Start a fresh session for ``user`` and redirect to ``next`` (or the password page)."""
    user.failed_logins = 0
    user.locked_until = None
    user.last_login_at = utcnow()
    session.clear()  # no data from the anonymous session survives sign-in
    login_user(user)
    session.permanent = True  # expires after PERMANENT_SESSION_LIFETIME (8 h)
    audit("login", "user", user.id, user=user)
    db.session.commit()
    safe_next = safe_next_url(next_url, "")
    if user.must_change_password:
        flash("Please choose a new password before continuing.", "warning")
        return redirect(url_for("auth.change_password", next=safe_next or None))
    return redirect(safe_next or url_for("main.dashboard"))


@bp.route("/login", methods=["GET", "POST"])
def login() -> Response | str:
    """Sign-in form."""
    next_url = request.args.get("next")
    if current_user.is_authenticated:
        return redirect(safe_next_url(next_url, url_for("main.dashboard")))
    form = LoginForm()
    login_error = None
    if form.validate_on_submit():
        user = _authenticate(form.username.data, form.password.data)
        if user is not None:
            return _complete_login(user, next_url)
        login_error = LOGIN_FAILED_MESSAGE
    return render_template(
        "auth/login.html", form=form, login_error=login_error, next_url=safe_next_url(next_url, "") or None
    )


@bp.route("/logout", methods=["POST"])
@login_required
def logout() -> Response:
    """Sign out (POST only, CSRF-protected)."""
    audit("logout", "user", current_user.id)
    db.session.commit()
    logout_user()
    session.clear()
    flash("You have been signed out.", "info")
    return redirect(url_for("auth.login"))


@bp.route("/account/password", methods=["GET", "POST"])
@login_required
def change_password() -> Response | str:
    """Change the signed-in user's password (current password required)."""
    user: User = current_user._get_current_object()
    form = ChangePasswordForm()
    if form.validate_on_submit():
        if user.check_password(form.current_password.data):
            user.set_password(form.new_password.data)
            user.must_change_password = False
            audit("password_changed", "user", user.id)
            db.session.commit()
            login_user(user)  # the session id embeds the password fingerprint; renew it
            flash("Your password has been changed.", "success")
            return redirect(safe_next_url(request.args.get("next"), url_for("main.dashboard")))
        form.current_password.errors.append("Your current password is incorrect.")
        audit("password_change_failed", "user", user.id, detail={"reason": "wrong_current_password"})
        db.session.commit()
    return render_template("auth/change_password.html", form=form, forced=user.must_change_password)


@bp.before_app_request
def require_password_change() -> Response | None:
    """Send users who must change their password to the password page."""
    if request.endpoint is None or request.endpoint in PASSWORD_CHANGE_EXEMPT_ENDPOINTS:
        return None
    if not (current_user.is_authenticated and current_user.must_change_password):
        return None
    next_url = request.full_path.rstrip("?") if request.method == "GET" else None
    return redirect(url_for("auth.change_password", next=next_url))
