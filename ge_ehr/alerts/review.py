"""Clinician review of alerts and acknowledgement of review notices.

The re-evaluation engine never reverses therapy; it asks a clinician to look at what changed.
These two actions record that the clinician did:

* :func:`review_alert` — an *active* alert or note becomes ``reviewed`` (it stays current; the
  engine keeps reviewed alerts reviewed), with an AlertEvent ``reviewed`` in its provenance;
* :func:`acknowledge_notice` — an *open* review notice becomes ``acknowledged``.

Both audit the action (``alert_reviewed`` / ``notice_acknowledged``; the free-text comment is
stored on the record, the audit row only notes that one was given), flush and never commit —
the caller commits. Errors are :class:`ValueError` with a user-facing message.

Owned by the UI-A work package (no other work package provides these actions).
"""

from __future__ import annotations

import logging

from ..extensions import db
from ..models import Alert, AlertEvent, ReviewNotice, User, utcnow
from ..security import audit
from ..templating import format_datetime

logger = logging.getLogger(__name__)

#: Maximum length of a review or acknowledgement comment.
COMMENT_MAX_LENGTH = 2_000


def _comment(comment: str | None) -> str | None:
    text = (comment or "").strip()
    if len(text) > COMMENT_MAX_LENGTH:
        raise ValueError(f"Comment must be at most {COMMENT_MAX_LENGTH:,} characters (got {len(text):,}).")
    return text or None


def _by(user: User | None) -> str:
    return user.full_name if user is not None else "an unknown user"


def review_alert(alert: Alert, *, comment: str | None = None, user: User | None = None) -> AlertEvent:
    """Mark an active alert or note as reviewed; returns the ``reviewed`` provenance event.

    The event carries the evidence the finding currently rests on (confirmed release and rule
    version, the variant's current class). Raises :class:`ValueError` when the finding is already
    reviewed or superseded, or the comment is too long.
    """
    if alert.status == "reviewed":
        raise ValueError(
            f"This {alert.kind} was already reviewed by {_by(alert.reviewed_by)} on {format_datetime(alert.reviewed_at)}."
        )
    if alert.status != "active":
        raise ValueError(f"Superseded {alert.kind}s cannot be reviewed; they are kept unchanged for the record.")
    text = _comment(comment)
    now = utcnow()
    alert.status = "reviewed"
    alert.reviewed_at = now
    alert.reviewed_by = user
    alert.review_comment = text
    variant = alert.patient_variant
    event = AlertEvent(
        alert=alert,
        event="reviewed",
        release_id=alert.confirmed_release_id,
        rule_version_id=alert.confirmed_rule_version_id,
        classification=variant.current_classification if variant is not None else alert.classification,
        user=user,
        at=now,
        detail=f"Reviewed by {_by(user)}." + (f" Comment: {text}" if text else ""),
    )
    db.session.add(event)
    db.session.flush()
    audit(
        "alert_reviewed",
        "alert",
        alert.id,
        detail={
            "patient_id": alert.patient_id,
            "rule_code": alert.rule_code,
            "kind": alert.kind,
            "severity": alert.severity,
            "has_comment": text is not None,
        },
        user=user,
    )
    logger.info("Alert %d (%s) reviewed by %s", alert.id, alert.rule_code, user.username if user else None)
    return event


def acknowledge_notice(notice: ReviewNotice, *, comment: str | None = None, user: User | None = None) -> None:
    """Acknowledge an open review notice.

    Raises :class:`ValueError` when it was already acknowledged or the comment is too long.
    """
    if notice.status != "open":
        raise ValueError(
            f"This review notice was already acknowledged by {_by(notice.acknowledged_by)} "
            f"on {format_datetime(notice.acknowledged_at)}."
        )
    text = _comment(comment)
    notice.status = "acknowledged"
    notice.acknowledged_at = utcnow()
    notice.acknowledged_by = user
    notice.acknowledgement_comment = text
    db.session.flush()
    audit(
        "notice_acknowledged",
        "review_notice",
        notice.id,
        detail={
            "patient_id": notice.patient_id,
            "variant_key_id": notice.variant_key_id,
            "old_classification": notice.old_classification,
            "new_classification": notice.new_classification,
            "has_comment": text is not None,
        },
        user=user,
    )
    logger.info("Review notice %d acknowledged by %s", notice.id, user.username if user else None)
