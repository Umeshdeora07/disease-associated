"""Forms of the alerts blueprint: alert review and review-notice acknowledgement.

Both forms carry a ``next`` field so a POST made from the inbox, the alert page, the notices
list or a patient's recommendations returns there (validated as a same-host path by the route).

Owned by the UI-A work package.
"""

from __future__ import annotations

from typing import Any

from flask_wtf import FlaskForm
from wtforms import HiddenField, TextAreaField
from wtforms.validators import Length, Optional

from .review import COMMENT_MAX_LENGTH


def _strip(value: Any) -> Any:
    return value.strip() if isinstance(value, str) else value


class ReviewAlertForm(FlaskForm):
    """Mark an alert or note as reviewed, with an optional comment."""

    comment = TextAreaField(
        "Review comment",
        filters=[_strip],
        validators=[Optional(), Length(max=COMMENT_MAX_LENGTH)],
        description="Optional: what was decided, e.g. referred to the diabetes genetics clinic.",
        render_kw={"rows": 3},
    )
    next = HiddenField()


class AcknowledgeNoticeForm(FlaskForm):
    """Acknowledge a review notice, with an optional comment.

    Several notices can be listed on one page, so each form gets the prefix ``ack<notice id>``
    (see :func:`acknowledge_form`), which keeps element ids unique.
    """

    comment = TextAreaField(
        "Comment",
        filters=[_strip],
        validators=[Optional(), Length(max=COMMENT_MAX_LENGTH)],
        description="Optional: e.g. whether treatment changed on the basis of the superseded alert.",
        render_kw={"rows": 2},
    )
    next = HiddenField()


def acknowledge_form(notice_id: int, **kwargs: Any) -> AcknowledgeNoticeForm:
    """The acknowledgement form of one notice (prefix ``ack<notice id>``)."""
    return AcknowledgeNoticeForm(prefix=f"ack{notice_id}", **kwargs)
