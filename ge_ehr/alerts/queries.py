"""Read-side queries of the alerts pages: inbox filters and ordering, summaries, notices.

List queries eager-load everything their rows display, so a page costs a fixed number of
queries whatever its size.

Owned by the UI-A work package.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from sqlalchemy import Select, case, func, select
from sqlalchemy.orm import joinedload, selectinload

from .. import constants as C
from ..extensions import db
from ..models import Alert, AlertEvent, KbRelease, PatientVariant, ReevaluationRun, ReviewNotice, Rule
from ..services.rules import RuleSpec, describe_clinical_condition, describe_genetic_criterion, rule_code_sort_key

#: ``status`` filter of the inbox → alert statuses it selects (``current`` is the default).
INBOX_STATUSES: dict[str, tuple[str, ...]] = {
    "current": C.CURRENT_ALERT_STATUSES,
    "active": ("active",),
    "reviewed": ("reviewed",),
    "superseded": ("superseded",),
    "all": C.ALERT_STATUSES,
}
INBOX_STATUS_LABELS: dict[str, str] = {
    "current": "Current",
    "active": "Awaiting review",
    "reviewed": "Reviewed",
    "superseded": "Superseded",
    "all": "All statuses",
}
#: ``kind`` filter of the inbox → finding kinds it selects (alerts only by default).
INBOX_KINDS: dict[str, tuple[str, ...]] = {"alert": ("alert",), "note": ("note",), "all": C.FINDING_KINDS}
INBOX_KIND_LABELS: dict[str, str] = {"alert": "Alerts", "note": "Information notes", "all": "Alerts and notes"}
#: ``status`` filter of the review-notice list.
NOTICE_FILTERS: dict[str, tuple[str, ...]] = {
    "open": ("open",),
    "acknowledged": ("acknowledged",),
    "all": C.NOTICE_STATUSES,
}
NOTICE_FILTER_LABELS: dict[str, str] = {"open": "Open", "acknowledged": "Acknowledged", "all": "All"}
#: Closing advice of a review-notice message (SPEC §7.1). The notice cards word their advice by
#: cause instead: it applies only when advice was withdrawn or lowered.
REVIEW_ADVICE = "Review any treatment change made on the basis of this alert; GE-EHR never reverses therapy automatically."
NEW_ALERT_ADVICE = "No advice was withdrawn or lowered: review the new alert linked below."


@dataclass(frozen=True)
class InboxFilters:
    """Filters of the alerts inbox (``?severity=&status=&rule=&kind=&reclassified=1``)."""

    severity: str = ""
    status: str = "current"
    rule: str = ""
    kind: str = "alert"
    reclassified: bool = False

    @classmethod
    def from_args(cls, args: Mapping[str, str], rule_codes: Sequence[str]) -> InboxFilters:
        """Read the filters from query arguments; unknown values fall back to the defaults."""
        severity = args.get("severity", "").strip()
        status = args.get("status", "").strip()
        rule = args.get("rule", "").strip()
        kind = args.get("kind", "").strip()
        return cls(
            severity=severity if severity in C.SEVERITIES else "",
            status=status if status in INBOX_STATUSES else "current",
            rule=rule if rule in rule_codes else "",
            kind=kind if kind in INBOX_KINDS else "alert",
            reclassified=args.get("reclassified", "") == "1",
        )

    @property
    def is_default(self) -> bool:
        return self == InboxFilters()

    def query_args(self) -> dict[str, str]:
        """The non-default filters as query arguments."""
        return {
            "severity": self.severity,
            "status": "" if self.status == "current" else self.status,
            "rule": self.rule,
            "kind": "" if self.kind == "alert" else self.kind,
            "reclassified": "1" if self.reclassified else "",
        }

    def args_with(self, **changes: str) -> dict[str, str]:
        """Query arguments of these filters with ``changes`` applied, empty values dropped (for links)."""
        return {name: value for name, value in {**self.query_args(), **changes}.items() if value}


def rule_codes() -> list[str]:
    """Stored rule codes in natural order (D1 … D6)."""
    return sorted(db.session.scalars(select(Rule.code)), key=rule_code_sort_key)


def _severity_rank() -> case:
    """SQL ordering key: high severity first, notes (no severity) last."""
    return case(
        {severity: -order for severity, order in C.SEVERITY_ORDER.items()},
        value=Alert.severity,
        else_=1,
    )


def inbox_statement(filters: InboxFilters) -> Select[tuple[Alert]]:
    """Alerts matching ``filters``: high priority first, then severity, newest first.

    Patient, variant, release and rule version of every row are loaded with the page.
    """
    statement = (
        select(Alert)
        .where(Alert.status.in_(INBOX_STATUSES[filters.status]), Alert.kind.in_(INBOX_KINDS[filters.kind]))
        .options(
            joinedload(Alert.patient),
            joinedload(Alert.variant_key),
            joinedload(Alert.confirmed_release),
            joinedload(Alert.confirmed_rule_version),
        )
        .order_by(
            case((Alert.priority == "high", 0), else_=1),
            _severity_rank(),
            Alert.created_at.desc(),
            Alert.id.desc(),
        )
    )
    if filters.severity:
        statement = statement.where(Alert.severity == filters.severity)
    if filters.rule:
        statement = statement.where(Alert.rule_code == filters.rule)
    if filters.reclassified:
        statement = statement.where(Alert.is_reclassified.is_(True))
    return statement


@dataclass(frozen=True)
class InboxSummary:
    """Counts shown above the inbox (current alerts only)."""

    by_severity: Mapping[str, int]
    reclassified: int
    high_priority: int
    awaiting_review: int
    open_notices: int


def inbox_summary() -> InboxSummary:
    """Current alerts by severity, reclassified / high-priority / unreviewed counts, open notices (two queries)."""
    rows = db.session.execute(
        select(Alert.severity, Alert.status, Alert.is_reclassified, Alert.priority, func.count(Alert.id))
        .where(Alert.status.in_(C.CURRENT_ALERT_STATUSES), Alert.kind == "alert")
        .group_by(Alert.severity, Alert.status, Alert.is_reclassified, Alert.priority)
    )
    by_severity: Counter[str] = Counter()
    reclassified = high_priority = awaiting = 0
    for severity, status, is_reclassified, priority, count in rows:
        by_severity[severity] += count
        reclassified += count if is_reclassified else 0
        high_priority += count if priority == "high" else 0
        awaiting += count if status == "active" else 0
    open_notices = db.session.scalar(select(func.count(ReviewNotice.id)).where(ReviewNotice.status == "open"))
    return InboxSummary(
        by_severity={severity: by_severity[severity] for severity in C.SEVERITIES},
        reclassified=reclassified,
        high_priority=high_priority,
        awaiting_review=awaiting,
        open_notices=open_notices or 0,
    )


def load_alert(alert_id: int) -> Alert:
    """An alert with everything its detail page shows (404 if absent)."""
    return db.get_or_404(
        Alert,
        alert_id,
        options=[
            joinedload(Alert.patient),
            joinedload(Alert.variant_key),
            joinedload(Alert.patient_variant).joinedload(PatientVariant.classification_release),
            joinedload(Alert.created_release),
            joinedload(Alert.confirmed_release),
            joinedload(Alert.created_rule_version),
            joinedload(Alert.confirmed_rule_version),
            joinedload(Alert.created_run),
            joinedload(Alert.superseded_run),
            joinedload(Alert.reviewed_by),
            joinedload(Alert.review_notice),
            selectinload(Alert.events).options(
                joinedload(AlertEvent.release),
                joinedload(AlertEvent.rule_version),
                joinedload(AlertEvent.run),
                joinedload(AlertEvent.user),
            ),
        ],
        description=f"Alert {alert_id} does not exist.",
    )


@dataclass(frozen=True)
class RuleCriteria:
    """Human-readable criteria of the rule version behind an alert."""

    genetic: str
    clinical: str


def rule_criteria(alert: Alert) -> RuleCriteria:
    """The genetic and clinical criteria of the alert's confirmed rule version."""
    spec = RuleSpec.from_definition(alert.confirmed_rule_version.definition)
    return RuleCriteria(genetic=describe_genetic_criterion(spec), clinical=describe_clinical_condition(spec.clinical))


@dataclass(frozen=True)
class NoticeOutcome:
    """A superseded alert of a review notice and the finding of the same rule that replaced it."""

    alert: Alert
    replacement: Alert | None

    @property
    def change(self) -> str:
        """``withdrawn`` (the rule no longer raises an alert), ``lowered`` or ``raised`` (severity),
        or ``replaced`` (same severity)."""
        if self.replacement is None or self.replacement.kind != "alert":
            return "withdrawn"
        old = C.SEVERITY_ORDER.get(self.alert.severity or "", -1)
        new = C.SEVERITY_ORDER.get(self.replacement.severity or "", -1)
        return "lowered" if new < old else "raised" if new > old else "replaced"


@dataclass(frozen=True)
class NoticeView:
    """A review notice with its cause: what replaced each superseded alert and, for a rule
    revision, the rule versions that changed (SPEC §7.3)."""

    notice: ReviewNotice
    outcomes: list[NoticeOutcome]
    rule_changes: list[dict[str, Any]]

    @property
    def is_rule_revision(self) -> bool:
        return self.notice.run is not None and self.notice.run.trigger == "rule_revision"

    @property
    def release(self) -> KbRelease | None:
        """The release whose activation raised the notice (None for a rule revision)."""
        run = self.notice.run
        return run.new_release if run is not None and run.trigger == "clinvar_release" else None

    @property
    def shows_class_change(self) -> bool:
        """Release notices show the class change; others only when the class differs."""
        if self.is_rule_revision:
            return False
        return self.release is not None or self.notice.old_classification != self.notice.new_classification

    @property
    def withdraws_advice(self) -> bool:
        """True when an alert stopped firing or its severity decreased (the treatment-review advice applies)."""
        return not self.outcomes or any(outcome.change in ("withdrawn", "lowered") for outcome in self.outcomes)

    @property
    def advice(self) -> str:
        """What to check: the treatment-review advice when advice was withdrawn or lowered,
        otherwise the new alert (an escalation or a revised recommendation)."""
        return REVIEW_ADVICE if self.withdraws_advice else NEW_ALERT_ADVICE

    @property
    def replacements(self) -> list[Alert]:
        return [outcome.replacement for outcome in self.outcomes if outcome.replacement is not None]

    @property
    def summary(self) -> str:
        """The notice message without its closing advice (the card words the advice by cause)."""
        return self.notice.message.removesuffix(REVIEW_ADVICE).rstrip()


def notice_views(notices: Iterable[ReviewNotice]) -> list[NoticeView]:
    """``notices`` with their outcomes and rule changes (one query for the replacements of all).

    The replacements of a notice's alerts are the findings its run created for the same patient
    and variant; each superseded alert is matched with the one of the same rule.
    """
    notices = list(notices)
    run_ids = {notice.run_id for notice in notices if notice.run_id is not None}
    created: defaultdict[tuple[int | None, int, int], list[Alert]] = defaultdict(list)
    if run_ids:
        statement = select(Alert).where(
            Alert.created_run_id.in_(sorted(run_ids)),
            Alert.patient_id.in_(sorted({notice.patient_id for notice in notices})),
        )
        for alert in db.session.scalars(statement):
            created[(alert.created_run_id, alert.patient_id, alert.variant_key_id)].append(alert)
    views = []
    for notice in notices:
        candidates = created.get((notice.run_id, notice.patient_id, notice.variant_key_id), []) if notice.run_id else []
        outcomes = [
            NoticeOutcome(
                alert=alert,
                replacement=min(
                    (found for found in candidates if found.rule_code == alert.rule_code),
                    key=lambda found: (found.kind != "alert", found.id),
                    default=None,
                ),
            )
            for alert in notice.alerts
        ]
        codes = {alert.rule_code for alert in notice.alerts}
        changes = (notice.run.rule_change or []) if notice.run is not None else []
        views.append(
            NoticeView(
                notice=notice,
                outcomes=outcomes,
                rule_changes=[change for change in changes if change.get("code") in codes] or list(changes),
            )
        )
    return views


def notices_statement(status: str) -> Select[tuple[ReviewNotice]]:
    """Review notices with ``status`` filter key (see :data:`NOTICE_FILTERS`), open and newest first."""
    return (
        select(ReviewNotice)
        .where(ReviewNotice.status.in_(NOTICE_FILTERS[status]))
        .options(
            joinedload(ReviewNotice.patient),
            joinedload(ReviewNotice.variant_key),
            joinedload(ReviewNotice.acknowledged_by),
            joinedload(ReviewNotice.run).joinedload(ReevaluationRun.new_release),
            selectinload(ReviewNotice.alerts),
        )
        .order_by(case((ReviewNotice.status == "open", 0), else_=1), ReviewNotice.created_at.desc(), ReviewNotice.id.desc())
    )


def notice_counts() -> dict[str, int]:
    """Number of review notices per status filter key."""
    rows = db.session.execute(
        select(ReviewNotice.status, func.count(ReviewNotice.id)).group_by(ReviewNotice.status)
    )
    counts = {notice_status: count for notice_status, count in rows}
    return {key: sum(counts.get(status, 0) for status in statuses) for key, statuses in NOTICE_FILTERS.items()}
