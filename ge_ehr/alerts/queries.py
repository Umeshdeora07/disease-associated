"""Read-side queries of the alerts pages: inbox filters and ordering, summaries, notices.

List queries eager-load everything their rows display, so a page costs a fixed number of
queries whatever its size.

Owned by the UI-A work package.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from sqlalchemy import Select, case, func, select
from sqlalchemy.orm import joinedload, selectinload

from .. import constants as C
from ..extensions import db
from ..models import Alert, AlertEvent, PatientVariant, ReevaluationRun, ReviewNotice, Rule
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
    "current": "Current (active or reviewed)",
    "active": "Active — awaiting review",
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
    ).tuples()
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
    counts = dict(
        db.session.execute(select(ReviewNotice.status, func.count(ReviewNotice.id)).group_by(ReviewNotice.status)).tuples()
    )
    return {key: sum(counts.get(status, 0) for status in statuses) for key, statuses in NOTICE_FILTERS.items()}
