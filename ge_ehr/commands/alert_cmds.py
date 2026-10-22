"""``flask alerts …`` — the alert inbox, alert review and review notices in the terminal (F3/F5).

Commands (clinicians and administrators only, SPEC §8.1):

* ``list`` — the alert inbox of all patients (high priority first, then severity, newest first),
  filterable by severity, status, rule, kind, patient and the *Reclassified* flag;
* ``show ALERT_ID`` — one alert or note: recommendation, rule criteria, variant, record and the
  provenance events (the release and rule version behind every step);
* ``review ALERT_ID --as USER [--comment TEXT]`` — mark an active alert or note as reviewed;
* ``notices [--all]`` — the review notices raised when a downgrade or rule revision superseded
  alerts; ``acknowledge NOTICE_ID --as USER [--comment TEXT]`` — acknowledge one.

``--as USERNAME`` names the account an action is recorded under in the audit log; it is required
for ``review`` and ``acknowledge`` and optional for the read commands (a lab technician account is
refused, and the refusal is audited as ``access_denied``, as a 403 is in the web app). State
changes go through :mod:`ge_ehr.alerts.review` — the same functions the web pages use — and are
committed on success; on error they are rolled back and the command exits with status 1.

Owned by the CLI work package. Read-side queries come from :mod:`ge_ehr.alerts.queries`, shared
with the web pages, so the terminal and the browser list the same alerts in the same order.
"""

from __future__ import annotations

import logging
from typing import Any

import click
from flask import Flask
from sqlalchemy import select
from werkzeug.exceptions import NotFound

from .. import constants as C
from ..alerts import queries, review
from ..extensions import db
from ..models import Alert, AlertEvent, ReviewNotice
from . import _format as F

logger = logging.getLogger(__name__)

DEFAULT_LIST_LIMIT = 50
DEFAULT_NOTICE_LIMIT = 20
#: Rows of the inbox ordering: what each ``--status`` value selects.
STATUS_CHOICES: tuple[str, ...] = tuple(queries.INBOX_STATUSES)
KIND_CHOICES: tuple[str, ...] = tuple(queries.INBOX_KINDS)
EVENT_LABELS: dict[str, str] = {
    "created": "created",
    "confirmed": "confirmed",
    "reviewed": "reviewed",
    "superseded": "superseded",
}

alerts_cli = F.command_group(
    "alerts",
    """Review decision-support alerts and review notices (clinicians and administrators).

    \b
    Examples:
      flask alerts list
      flask alerts list --severity high --reclassified
      flask alerts list --status all --rule D2
      flask alerts show 12
      flask alerts review 12 --as dr.rao --comment "Discussed with the patient"
      flask alerts notices
      flask alerts acknowledge 1 --as dr.rao --comment "Therapy unchanged"

    Changes need --as USERNAME (the account recorded in the audit log). Recommendations are
    advisory, for specialist review; GE-EHR never changes therapy itself.
    """,
)


def _as_option(*, required: bool) -> Any:
    help_text = (
        F.AS_HELP + " Must be a clinician or administrator."
        if required
        else "View as this account (must be a clinician or administrator)."
    )
    return click.option("--as", "as_user", metavar="USERNAME", required=required, help=help_text)


def _rule_label(alert: Alert) -> str:
    version = alert.confirmed_rule_version
    return version.label if version is not None else alert.rule_code


def _flag_names(alert: Alert) -> list[str]:
    names = []
    if alert.is_reclassified:
        names.append("Reclassified")
    if alert.priority == "high" and not alert.is_reclassified:
        names.append("High priority")
    return names


def _load_alert(alert_id: int) -> Alert:
    """The alert with everything ``show`` prints; a clear error when it does not exist."""
    try:
        return queries.load_alert(alert_id)
    except NotFound:
        raise click.ClickException(f"No alert #{alert_id}. List alerts with: flask alerts list --status all") from None


def _load_notice(notice_id: int) -> ReviewNotice:
    notice = db.session.get(ReviewNotice, notice_id)
    if notice is None:
        raise click.ClickException(f"No review notice #{notice_id}. List them with: flask alerts notices --all")
    return notice


# ---------------------------------------------------------------------------
# list
# ---------------------------------------------------------------------------


def _rule_code(_ctx: click.Context, _param: click.Parameter, value: str | None) -> str:
    """``--rule``: a stored rule code, case-insensitive (``d2`` → ``D2``)."""
    if not value:
        return ""
    codes = queries.rule_codes()
    wanted = value.strip().upper()
    if wanted not in codes:
        known = ", ".join(codes) or "none imported yet"
        raise click.BadParameter(f"unknown rule code {value!r}; known codes: {known}.")
    return wanted


def _echo_summary() -> None:
    """Two lines above the inbox: current alerts by severity, then review workload and notices."""
    summary = queries.inbox_summary()
    counts = ", ".join(
        f"{count} {F.style(severity, **F.SEVERITY_STYLES[severity])}"
        for severity, count in summary.by_severity.items()
        if count
    )
    total = sum(summary.by_severity.values())
    F.echo(f"Current alerts: {total}" + (f" ({counts})" if counts else ""))
    notices = F.plural(summary.open_notices, "open review notice")
    F.echo(
        f"{summary.awaiting_review} awaiting review, {summary.reclassified} reclassified, {notices}"
        + (" (flask alerts notices)." if summary.open_notices else ".")
    )


@alerts_cli.command("list")
@click.option(
    "--severity", type=click.Choice(C.SEVERITIES, case_sensitive=False), help="Only alerts of this severity."
)
@click.option(
    "--status",
    type=click.Choice(STATUS_CHOICES, case_sensitive=False),
    default="current",
    show_default=True,
    help="current = active or reviewed; active = awaiting review; superseded = withdrawn by a re-evaluation.",
)
@click.option("--rule", callback=_rule_code, metavar="CODE", help="Only this rule, e.g. D2.")
@click.option("--reclassified", is_flag=True, help="Only alerts raised because a variant was reclassified.")
@click.option(
    "--kind",
    type=click.Choice(KIND_CHOICES, case_sensitive=False),
    default="alert",
    show_default=True,
    help="alert, note (genetic criterion met, clinical condition not met) or all.",
)
@click.option("--patient", "mrn", metavar="MRN", help="Only this patient.")
@click.option(
    "--limit", type=click.IntRange(min=1), default=DEFAULT_LIST_LIMIT, show_default=True, help="Rows to show."
)
@_as_option(required=False)
def list_command(
    severity: str | None,
    status: str,
    rule: str,
    reclassified: bool,
    kind: str,
    mrn: str | None,
    limit: int,
    as_user: str | None,
) -> None:
    """List alerts of all patients: high priority first, then severity, newest first.

    \b
    Examples:
      flask alerts list
      flask alerts list --severity high
      flask alerts list --reclassified
      flask alerts list --status superseded --rule D1
      flask alerts list --kind all --patient DEMO-0002
    """
    F.acting_user(as_user, roles=C.CLINICAL_ROLES, action="view alerts")
    patient = F.find_patient(mrn) if mrn else None
    filters = queries.InboxFilters(
        severity=(severity or "").lower(),
        status=status.lower(),
        rule=rule,
        kind=kind.lower(),
        reclassified=reclassified,
    )
    statement = queries.inbox_statement(filters)
    if patient is not None:
        statement = statement.where(Alert.patient_id == patient.id)
    page = db.paginate(statement, page=1, per_page=limit, max_per_page=None, error_out=False)
    _echo_summary()
    F.echo("")
    if not page.items:
        if filters.is_default and patient is None:
            F.echo("No current alerts.")
        else:
            F.echo("No alerts match these filters. Try --status all or drop a filter.")
        return
    F.table(
        ["ID", "SEV", "RULE", "PATIENT", "VARIANT", "CLASS", "STATUS", "RAISED", "FLAGS"],
        (
            (
                f"#{alert.id}",
                F.severity_label(alert.severity),
                _rule_label(alert),
                alert.patient.mrn,
                F.variant_text(alert.variant_key),
                F.class_label(alert.classification),
                F.status_label(alert.status),
                F.day(alert.created_at),
                F.flags(_flag_names(alert)) or None,
            )
            for alert in page.items
        ),
        flex=4,
        indent=0,
    )
    shown = len(page.items)
    F.echo("")
    if page.total and page.total > shown:
        F.echo(f"Showing {shown} of {page.total} {kind_word(filters.kind, page.total)}; narrow the filters or raise --limit.")
    else:
        F.echo(f"{F.plural(shown, 'row')}. Details: flask alerts show ID    Review: flask alerts review ID --as USERNAME")
    F.advisory_footer()


def kind_word(kind: str, count: int) -> str:
    """``alerts`` / ``notes`` / ``alerts and notes`` for the "showing N of M" line."""
    if kind == "note":
        return "note" if count == 1 else "notes"
    if kind == "all":
        return "alerts and notes"
    return "alert" if count == 1 else "alerts"


# ---------------------------------------------------------------------------
# show
# ---------------------------------------------------------------------------


def _event_rows(events: list[AlertEvent]) -> list[list[Any]]:
    ordered = sorted(events, key=lambda event: (event.at, event.id))
    return [
        [
            event.at.strftime("%Y-%m-%d %H:%M:%S"),
            EVENT_LABELS.get(event.event, event.event),
            event.release.label if event.release is not None else None,
            event.rule_version.label if event.rule_version is not None else None,
            F.class_label(event.classification) if event.classification else None,
            f"#{event.run_id}" if event.run_id is not None else None,
            event.user.username if event.user is not None else None,
        ]
        for event in ordered
    ]


def _echo_event_details(events: list[AlertEvent]) -> None:
    """The free-text detail of each event (e.g. a review comment), under the provenance table."""
    for event in sorted(events, key=lambda item: (item.at, item.id)):
        if event.detail:
            F.paragraph(f"{event.at:%Y-%m-%d %H:%M:%S} {EVENT_LABELS.get(event.event, event.event)}: {event.detail}")


def _created_text(alert: Alert) -> str:
    trigger = C.TRIGGER_LABELS.get(alert.trigger, alert.trigger)
    run = f", run #{alert.created_run_id}" if alert.created_run_id is not None else ""
    return f"{F.when(alert.created_at)} ({trigger}{run})"


def _evidence(rule_version: Any, release: Any) -> str:
    rule = rule_version.label if rule_version is not None else "-"
    return f"rule {rule}; {release.label if release is not None else 'no knowledge-base release'}"


@alerts_cli.command("show")
@click.argument("alert_id", type=int)
@_as_option(required=False)
def show_command(alert_id: int, as_user: str | None) -> None:
    """Show one alert or note with its recommendation, criteria and provenance events.

    \b
    Examples:
      flask alerts show 12
      flask alerts show 12 --as dr.rao
    """
    F.acting_user(as_user, roles=C.CLINICAL_ROLES, action="view alerts")
    alert = _load_alert(alert_id)
    is_alert = alert.kind == "alert"
    head = [
        F.style(f"{'Alert' if is_alert else 'Note'} #{alert.id}", bold=True),
        _rule_label(alert),
        F.severity_label(alert.severity),
        F.status_label(alert.status),
    ]
    flag_names = _flag_names(alert)
    if alert.is_reclassified and alert.priority == "high":
        flag_names.append("High priority")
    F.echo("  ".join(head) + (f"  {F.flags(flag_names)}" if flag_names else ""))
    F.echo(F.style(alert.title, bold=True))
    F.field("Patient", f"{F.patient_text(alert.patient)}  (flask patients show {alert.patient.mrn})")
    variant = alert.patient_variant
    variant_text = f"{F.variant_text(alert.variant_key)} - {F.class_label(alert.classification, long=True)} at creation"
    if variant is not None and variant.current_classification != alert.classification:
        variant_text += f"; now {F.class_label(variant.current_classification, long=True)}"
    F.field("Variant", variant_text)
    if variant is not None:
        F.field("Zygosity", variant.zygosity)
        if variant.is_amended:
            F.field("Amended", f"class changed since first report ({F.plural(variant.reclassified_count, 'time')})")

    F.heading("Recommendation" if is_alert else "Note")
    F.paragraph(alert.message)
    F.echo("")
    F.field("Source", alert.source)
    if alert.confirmed_rule_version is not None:
        criteria = queries.rule_criteria(alert)
        F.field("Genetic", criteria.genetic)
        F.field("Clinical", criteria.clinical)
        if alert.confirmed_rule_version.change_note:
            F.field("Rule change", alert.confirmed_rule_version.change_note)

    F.heading("Record")
    F.field("Status", F.status_label(alert.status))
    F.field("Raised", _created_text(alert))
    F.field("Created with", _evidence(alert.created_rule_version, alert.created_release))
    F.field("Confirmed with", _evidence(alert.confirmed_rule_version, alert.confirmed_release))
    F.field("Notification", "clinician notified" if alert.notify else "silent (no notification)")
    if alert.status == "superseded":
        reason = C.SUPERSEDED_REASON_LABELS.get(alert.superseded_reason or "", alert.superseded_reason)
        run = f", run #{alert.superseded_run_id}" if alert.superseded_run_id is not None else ""
        F.field("Superseded", f"{F.when(alert.superseded_at)} ({reason}{run}); kept unchanged for the record")
    if alert.review_notice is not None:
        notice = alert.review_notice
        F.field("Review notice", f"#{notice.id} ({notice.status}): flask alerts notices --all")
    if alert.reviewed_at is not None:
        who = alert.reviewed_by.username if alert.reviewed_by is not None else "unknown user"
        F.field("Reviewed", f"{F.when(alert.reviewed_at)} by {who}")
        if alert.review_comment:
            F.field("Comment", alert.review_comment)
    elif alert.status == "active":
        F.field("Review", f"flask alerts review {alert.id} --as USERNAME [--comment TEXT]")

    F.heading(f"Provenance ({F.plural(len(alert.events), 'event')}; alerts are never deleted)")
    if alert.events:
        F.table(["AT (UTC)", "EVENT", "RELEASE", "RULE", "CLASS", "RUN", "BY"], _event_rows(alert.events), flex=2)
        _echo_event_details(alert.events)
    else:
        F.echo("  No events recorded.")
    F.advisory_footer()


# ---------------------------------------------------------------------------
# review
# ---------------------------------------------------------------------------


@alerts_cli.command("review")
@click.argument("alert_id", type=int)
@_as_option(required=True)
@click.option("--comment", metavar="TEXT", help=f"Optional comment (at most {review.COMMENT_MAX_LENGTH:,} characters).")
def review_command(alert_id: int, as_user: str, comment: str | None) -> None:
    """Mark an active alert or note as reviewed (it stays current).

    Reviewing records that a clinician has looked at the recommendation; GE-EHR never changes
    therapy itself.

    \b
    Examples:
      flask alerts review 12 --as dr.rao
      flask alerts review 12 --as dr.rao --comment "Referred to diabetes genetics clinic"
    """
    user = F.acting_user(as_user, roles=C.CLINICAL_ROLES, action="review alerts")
    alert = _load_alert(alert_id)
    with F.saving(f"Could not review {alert.kind} #{alert.id}"):
        review.review_alert(alert, comment=comment, user=user)
    F.say(
        f"Marked {alert.kind} #{alert.id} ({_rule_label(alert)} {F.severity_label(alert.severity)}, "
        f"{F.patient_text(alert.patient)}) as reviewed by {user.username} at {F.when(alert.reviewed_at)}."
    )
    if alert.review_comment:
        F.field("Comment", alert.review_comment)


# ---------------------------------------------------------------------------
# notices / acknowledge
# ---------------------------------------------------------------------------


@alerts_cli.command("notices")
@click.option("--all", "show_all", is_flag=True, help="Include acknowledged notices (default: open ones only).")
@click.option(
    "--limit", type=click.IntRange(min=1), default=DEFAULT_NOTICE_LIMIT, show_default=True, help="Notices to show."
)
@_as_option(required=False)
def notices_command(show_all: bool, limit: int, as_user: str | None) -> None:
    """List review notices: alerts withdrawn after a downgrade or a rule revision.

    A notice asks a clinician to check any treatment change made on the basis of the withdrawn
    alerts; GE-EHR never reverses therapy automatically.

    \b
    Examples:
      flask alerts notices
      flask alerts notices --all
    """
    F.acting_user(as_user, roles=C.CLINICAL_ROLES, action="view review notices")
    status = "all" if show_all else "open"
    counts = queries.notice_counts()
    page = db.paginate(queries.notices_statement(status), page=1, per_page=limit, max_per_page=None, error_out=False)
    F.echo(f"Review notices: {counts['open']} open, {counts['acknowledged']} acknowledged.")
    if not page.items:
        F.echo("No open review notices." if not show_all else "No review notices yet.")
        if not show_all and counts["acknowledged"]:
            F.echo("Acknowledged ones: flask alerts notices --all")
        return
    for notice in page.items:
        F.echo("")
        F.notice_block(notice, indent=0)
    if page.total and page.total > len(page.items):
        F.echo(f"\nShowing {len(page.items)} of {page.total} notices; raise --limit to see more.")
    F.advisory_footer()


@alerts_cli.command("acknowledge")
@click.argument("notice_id", type=int)
@_as_option(required=True)
@click.option("--comment", metavar="TEXT", help=f"Optional comment (at most {review.COMMENT_MAX_LENGTH:,} characters).")
def acknowledge_command(notice_id: int, as_user: str, comment: str | None) -> None:
    """Acknowledge an open review notice.

    \b
    Examples:
      flask alerts acknowledge 1 --as dr.rao
      flask alerts acknowledge 1 --as dr.rao --comment "Therapy unchanged; patient informed"
    """
    user = F.acting_user(as_user, roles=C.CLINICAL_ROLES, action="acknowledge review notices")
    notice = _load_notice(notice_id)
    with F.saving(f"Could not acknowledge review notice #{notice.id}"):
        review.acknowledge_notice(notice, comment=comment, user=user)
    F.say(
        f"Acknowledged review notice #{notice.id} ({F.variant_text(notice.variant_key)}: "
        f"{F.class_change(notice.old_classification, notice.new_classification)}, {F.patient_text(notice.patient)}) "
        f"as {user.username} at {F.when(notice.acknowledged_at)}."
    )
    if notice.acknowledgement_comment:
        F.field("Comment", notice.acknowledgement_comment)
    remaining = db.session.scalars(select(ReviewNotice.id).where(ReviewNotice.status == "open")).all()
    F.echo(f"{F.plural(len(remaining), 'open review notice')} left." + (" See: flask alerts notices" if remaining else ""))


def register(app: Flask) -> None:
    """Register the ``alerts`` command group."""
    app.cli.add_command(alerts_cli)
