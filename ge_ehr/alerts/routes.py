"""Routes of the alerts blueprint: alert inbox, alert detail and review, review notices.

All pages are for clinicians and administrators only (SPEC §8.1; lab technicians get 403 and an
``access_denied`` audit row). State changes go through :mod:`ge_ehr.alerts.review`; routes
commit, flash and redirect back to the page the action was taken on (POST-redirect-GET, the
``next`` field is accepted only as a same-host path).

Owned by the UI-A work package.
"""

from __future__ import annotations

import logging

from flask import Blueprint, current_app, flash, redirect, render_template, request, url_for
from flask_login import current_user
from werkzeug.wrappers import Response

from ..constants import CLINICAL_ROLES
from ..extensions import db
from ..models import ReviewNotice
from ..security import roles_required, safe_next_url
from . import queries, review
from .forms import ReviewAlertForm, acknowledge_form

logger = logging.getLogger(__name__)

bp = Blueprint("alerts", __name__)


def _per_page() -> int:
    return int(current_app.config["ITEMS_PER_PAGE"])


def _form_errors(form: ReviewAlertForm) -> str:
    return " ".join(error for errors in form.errors.values() for error in errors)


@bp.route("/alerts")
@roles_required(*CLINICAL_ROLES)
def inbox() -> str:
    """Alerts of all patients: high priority first, then severity, newest first; filterable."""
    codes = queries.rule_codes()
    filters = queries.InboxFilters.from_args(request.args, codes)
    pagination = db.paginate(
        queries.inbox_statement(filters),
        page=request.args.get("page", 1, type=int),
        per_page=_per_page(),
        error_out=False,
    )
    return render_template(
        "alerts/inbox.html",
        pagination=pagination,
        filters=filters,
        rule_codes=codes,
        summary=queries.inbox_summary(),
        statuses=queries.INBOX_STATUS_LABELS,
        kinds=queries.INBOX_KIND_LABELS,
    )


@bp.route("/alerts/<int:alert_id>")
@roles_required(*CLINICAL_ROLES)
def view_alert(alert_id: int) -> str:
    """One alert or note with its recommendation, rule criteria, provenance timeline and review."""
    alert = queries.load_alert(alert_id)
    return render_template(
        "alerts/detail.html",
        alert=alert,
        criteria=queries.rule_criteria(alert),
        form=ReviewAlertForm(formdata=None, next=request.args.get("next", "")),
        back_url=safe_next_url(request.args.get("next"), url_for("alerts.inbox")),
    )


@bp.route("/alerts/<int:alert_id>/review", methods=["POST"])
@roles_required(*CLINICAL_ROLES)
def review_alert(alert_id: int) -> Response:
    """Mark an active alert or note as reviewed (optional comment), then return to ``next``."""
    alert = queries.load_alert(alert_id)
    form = ReviewAlertForm()
    target = safe_next_url(form.next.data, url_for("alerts.view_alert", alert_id=alert.id))
    if not form.validate_on_submit():
        flash(f"The review was not saved. {_form_errors(form)}", "danger")
        return redirect(target)
    try:
        review.review_alert(alert, comment=form.comment.data, user=current_user)
    except ValueError as exc:
        db.session.rollback()
        flash(str(exc), "warning")
        return redirect(target)
    db.session.commit()
    flash(f"{alert.rule_code} {alert.kind} for {alert.patient.display_name} ({alert.patient.mrn}) marked as reviewed.", "success")
    return redirect(target)


@bp.route("/notices")
@roles_required(*CLINICAL_ROLES)
def notices() -> str:
    """Review notices raised when re-evaluation superseded alerts (open first)."""
    status = request.args.get("status", "open")
    if status not in queries.NOTICE_FILTERS:
        status = "open"
    pagination = db.paginate(
        queries.notices_statement(status),
        page=request.args.get("page", 1, type=int),
        per_page=_per_page(),
        error_out=False,
    )
    next_url = request.full_path.rstrip("?")
    return render_template(
        "alerts/notices.html",
        pagination=pagination,
        status=status,
        counts=queries.notice_counts(),
        filter_labels=queries.NOTICE_FILTER_LABELS,
        forms={
            notice.id: acknowledge_form(notice.id, formdata=None, next=next_url)
            for notice in pagination.items
            if notice.status == "open"
        },
    )


@bp.route("/notices/<int:notice_id>/acknowledge", methods=["POST"])
@roles_required(*CLINICAL_ROLES)
def acknowledge_notice(notice_id: int) -> Response:
    """Acknowledge an open review notice (optional comment), then return to ``next``."""
    notice = db.get_or_404(ReviewNotice, notice_id, description=f"Review notice {notice_id} does not exist.")
    form = acknowledge_form(notice.id)
    target = safe_next_url(form.next.data, url_for("alerts.notices"))
    if not form.validate_on_submit():
        flash(f"The acknowledgement was not saved. {_form_errors(form)}", "danger")
        return redirect(target)
    try:
        review.acknowledge_notice(notice, comment=form.comment.data, user=current_user)
    except ValueError as exc:
        db.session.rollback()
        flash(str(exc), "warning")
        return redirect(target)
    db.session.commit()
    flash(f"Review notice for {notice.patient.display_name} ({notice.patient.mrn}) acknowledged.", "success")
    return redirect(target)
