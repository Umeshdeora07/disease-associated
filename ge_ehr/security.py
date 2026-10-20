"""Access control, audit logging, password policy and redirect safety.

Owned by the Foundation work package.
"""

from __future__ import annotations

import json
import logging
import re
import secrets
import string
from collections.abc import Callable, Mapping
from functools import wraps
from typing import Any, TypeVar
from urllib.parse import urlsplit

from flask import abort, has_request_context, request
from flask_login import current_user

from . import constants as C
from .extensions import db, login_manager
from .models import AuditLog, User

logger = logging.getLogger(__name__)

F = TypeVar("F", bound=Callable[..., Any])

PASSWORD_MIN_LENGTH = 10
PASSWORD_MAX_LENGTH = 128
USERNAME_PATTERN = re.compile(r"^[a-z0-9][a-z0-9._-]{1,63}$")
# Deliberately simple shape check (no deliverability check): something@domain.tld
EMAIL_PATTERN = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
USERNAME_RULES = (
    "2–64 characters: lower-case letters, digits, dots, hyphens or underscores, "
    "starting with a letter or digit."
)
_TEMPORARY_PASSWORD_ALPHABET = "".join(
    ch for ch in string.ascii_letters + string.digits if ch not in "0O1lI"
)


# ---------------------------------------------------------------------------
# Role-based access control
# ---------------------------------------------------------------------------


def roles_required(*roles: str) -> Callable[[F], F]:
    """Require a signed-in user whose role is one of ``roles`` (any role when none given).

    Anonymous users are sent to the login page (or get 401 JSON for API requests). A signed-in
    user with another role gets 403 and an ``access_denied`` audit row is committed.
    """
    unknown = sorted(set(roles) - set(C.ROLES))
    if unknown:
        raise ValueError(f"Unknown role(s) {', '.join(unknown)}; expected one of {', '.join(C.ROLES)}.")

    def decorator(view: F) -> F:
        @wraps(view)
        def wrapped(*args: Any, **kwargs: Any) -> Any:
            if not current_user.is_authenticated:
                return login_manager.unauthorized()
            if roles and not current_user.has_role(*roles):
                audit(
                    "access_denied",
                    detail={
                        "endpoint": request.endpoint,
                        "path": request.path,
                        "method": request.method,
                        "required_roles": list(roles),
                        "role": current_user.role,
                    },
                )
                db.session.commit()
                logger.warning(
                    "Access denied: user %s (%s) requested %s", current_user.username, current_user.role, request.path
                )
                abort(403)
            return view(*args, **kwargs)

        return wrapped  # type: ignore[return-value]

    return decorator


# ---------------------------------------------------------------------------
# Audit log
# ---------------------------------------------------------------------------


def _json_default(value: Any) -> Any:
    """Serialise sets as sorted lists and anything else (dates, decimals, ...) as text."""
    if isinstance(value, (set, frozenset)):
        return sorted(value, key=str)
    return str(value)


def _json_safe(detail: Mapping[str, Any]) -> dict[str, Any]:
    """Round-trip through JSON so that only plain JSON values are stored."""
    return json.loads(json.dumps(dict(detail), default=_json_default))


def audit(
    action: str,
    entity_type: str | None = None,
    entity_id: int | None = None,
    detail: Mapping[str, Any] | None = None,
    user: User | None = None,
) -> AuditLog:
    """Add an :class:`AuditLog` row and flush (the caller commits).

    ``user`` defaults to the signed-in user of the current request; the username is stored as a
    snapshot. The client IP is recorded when called within a request.
    """
    if user is None and has_request_context() and current_user.is_authenticated:
        user = current_user._get_current_object()
    entry = AuditLog(
        action=action,
        entity_type=entity_type,
        entity_id=entity_id,
        detail=_json_safe(detail) if detail else None,
        user=user,
        username=user.username if user is not None else None,
        ip_address=request.remote_addr if has_request_context() else None,
    )
    db.session.add(entry)
    db.session.flush()
    return entry


# ---------------------------------------------------------------------------
# Password policy and usernames
# ---------------------------------------------------------------------------


class PasswordPolicyError(ValueError):
    """A password does not meet the policy; the message lists every problem."""


def password_policy_problems(password: str) -> list[str]:
    """Return human-readable reasons why ``password`` violates the policy (empty if it is fine).

    Policy: at least 10 characters (at most 128) containing both letters and digits.
    """
    problems: list[str] = []
    if len(password) < PASSWORD_MIN_LENGTH:
        problems.append(f"Password must be at least {PASSWORD_MIN_LENGTH} characters long.")
    if len(password) > PASSWORD_MAX_LENGTH:
        problems.append(f"Password must be at most {PASSWORD_MAX_LENGTH} characters long.")
    if not any(ch.isalpha() for ch in password):
        problems.append("Password must contain at least one letter.")
    if not any(ch.isdigit() for ch in password):
        problems.append("Password must contain at least one digit.")
    return problems


def validate_password(password: str) -> None:
    """Raise :class:`PasswordPolicyError` when ``password`` violates the policy."""
    problems = password_policy_problems(password)
    if problems:
        raise PasswordPolicyError(" ".join(problems))


def generate_temporary_password(length: int = 14) -> str:
    """Random password satisfying the policy (unambiguous letters and digits only)."""
    while True:
        candidate = "".join(secrets.choice(_TEMPORARY_PASSWORD_ALPHABET) for _ in range(length))
        if not password_policy_problems(candidate):
            return candidate


def normalize_username(username: str) -> str:
    """Usernames are stored stripped and lower-case."""
    return username.strip().lower()


def is_valid_username(username: str) -> bool:
    """True when the (normalised) username matches :data:`USERNAME_PATTERN`."""
    return bool(USERNAME_PATTERN.fullmatch(normalize_username(username)))


# ---------------------------------------------------------------------------
# Redirect safety
# ---------------------------------------------------------------------------


def is_safe_redirect_url(target: str | None) -> bool:
    """True only for same-host relative paths such as ``/patients?q=x``.

    Rejects absolute URLs, scheme-relative ``//host`` URLs, backslash tricks (``/\\host``) and
    control characters.
    """
    if not target or not target.startswith("/") or target.startswith("//"):
        return False
    if "\\" in target or any(ord(ch) < 32 or ord(ch) == 127 for ch in target):
        return False
    parts = urlsplit(target)
    return not parts.scheme and not parts.netloc


def safe_next_url(target: str | None, default: str) -> str:
    """Return ``target`` when it is a safe redirect, otherwise ``default``."""
    return target if is_safe_redirect_url(target) else default
