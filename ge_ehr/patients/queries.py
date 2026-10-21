"""Read-side queries and view models of the patient pages (no state changes here).

Every function loads what its page needs with a fixed number of queries — eager loading and
grouped aggregates instead of per-row lookups — so list pages stay fast for large cohorts.
Classifications come from :mod:`ge_ehr.services.classification`, the point-in-time view from
:func:`ge_ehr.services.reevaluation.alerts_as_of`.

Owned by the UI-A work package.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from itertools import batched

from sqlalchemy import Select, and_, func, or_, select
from sqlalchemy.orm import joinedload, selectinload

from .. import constants as C
from ..extensions import db
from ..models import (
    Alert,
    AlertEvent,
    Diagnosis,
    GenomicUpload,
    KbRelease,
    KnownVariant,
    Medication,
    Patient,
    PatientVariant,
    ReevaluationRun,
    ReviewNotice,
    RuleVersion,
    utc_today,
    utcnow,
)
from ..services import classification, reevaluation
from ..services.classification import IN_CLAUSE_CHUNK, KnownClass
from ..services.rules import rule_code_sort_key
from ..templating import months_to_age

#: Severities in display order (alerts are grouped by these on the recommendations page).
SEVERITY_GROUPS: tuple[str, ...] = tuple(sorted(C.SEVERITIES, key=lambda severity: -C.SEVERITY_ORDER[severity]))
#: Current alerts listed in the alert summary of the patient page.
SUMMARY_ALERT_LIMIT = 5
#: Change points offered as shortcuts by the point-in-time selector.
CHANGE_POINT_LIMIT = 8
#: Accepted ``?at=`` formats are those of :meth:`datetime.fromisoformat` (``YYYY-MM-DDTHH:MM[:SS[.fff]]``).
POINT_IN_TIME_HELP = "Enter a date and time as YYYY-MM-DDTHH:MM (UTC), e.g. 2026-01-15T09:30."


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------


def _like_pattern(text: str) -> str:
    """A case-insensitive ``LIKE`` pattern matching ``text`` literally anywhere."""
    escaped = text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


def age_text(birth_date: date, on: date | None = None) -> str:
    """Age for display: ``"8 months"`` below two years, otherwise ``"34 years"``."""
    today = on or utc_today()
    months = (today.year - birth_date.year) * 12 + today.month - birth_date.month - (today.day < birth_date.day)
    return months_to_age(max(months, 0))


def parse_point_in_time(value: str) -> datetime:
    """Parse a ``?at=`` value (ISO 8601; naive values are UTC) into naive UTC.

    Raises :class:`ValueError` with a user-facing message for anything else.
    """
    try:
        moment = datetime.fromisoformat(value.strip())
    except ValueError:
        raise ValueError(f"{value!r} is not a valid point in time. {POINT_IN_TIME_HELP}") from None
    if moment.tzinfo is not None:
        moment = moment.astimezone(UTC).replace(tzinfo=None)
    return moment


def point_in_time_value(moment: datetime) -> str:
    """``?at=`` / ``datetime-local`` value of ``moment`` with only the precision it needs.

    Minutes, seconds or milliseconds (``2026-01-15T09:30``, ``…T09:30:15``, ``…T09:30:15.250``):
    the demo dataset records several changes within one second, so a change point must keep its
    milliseconds to select the state right after it.
    """
    if moment.microsecond:
        return moment.isoformat(timespec="milliseconds")
    if moment.second:
        return moment.isoformat(timespec="seconds")
    return moment.isoformat(timespec="minutes")


def point_in_time_step(moment: datetime | None) -> str:
    """``step`` attribute of the ``datetime-local`` input showing ``moment`` (seconds; ``"60"`` is the default)."""
    if moment is not None and moment.microsecond:
        return "0.001"
    if moment is not None and moment.second:
        return "1"
    return "60"


def format_moment(moment: datetime) -> str:
    """``2026-01-15 09:30 UTC``, with seconds and milliseconds only when they are not zero."""
    text = point_in_time_value(moment).replace("T", " ")
    return f"{text} UTC"


def _ceil_to_millisecond(moment: datetime) -> datetime:
    """``moment`` rounded up to a whole millisecond (so the state *at* it includes what happened then)."""
    remainder = moment.microsecond % 1000
    return moment + timedelta(microseconds=1000 - remainder) if remainder else moment


def _sort_alerts(alerts: Iterable[Alert]) -> list[Alert]:
    """High priority first, then severity (highest first), rule code and creation order."""
    return sorted(
        alerts,
        key=lambda alert: (
            alert.priority != "high",
            -C.SEVERITY_ORDER.get(alert.severity or "", -1),
            rule_code_sort_key(alert.rule_code),
            alert.id,
        ),
    )


# ---------------------------------------------------------------------------
# Patient list
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PatientListFilters:
    """Search and filters of the patient list (``?q=&family=&diabetes_type=&alerts=1``)."""

    q: str = ""
    family: str = ""
    diabetes_type: str = ""
    with_alerts: bool = False

    @classmethod
    def from_args(cls, args: Mapping[str, str], *, allow_alert_filter: bool) -> PatientListFilters:
        """Read the filters from query arguments; unknown values are ignored.

        The alert filter is honoured only for roles that may see alerts, so it cannot be used
        to infer recommendations.
        """
        diabetes_type = args.get("diabetes_type", "").strip()
        return cls(
            q=args.get("q", "").strip()[:100],
            family=args.get("family", "").strip()[:64],
            diabetes_type=diabetes_type if diabetes_type in C.DIABETES_TYPES else "",
            with_alerts=allow_alert_filter and args.get("alerts", "") == "1",
        )

    @property
    def active(self) -> bool:
        """True when any search or filter is applied."""
        return bool(self.q or self.family or self.diabetes_type or self.with_alerts)

    def query_args(self) -> dict[str, str]:
        """The filters as query arguments (empty values are dropped by the pagination macro)."""
        return {
            "q": self.q,
            "family": self.family,
            "diabetes_type": self.diabetes_type,
            "alerts": "1" if self.with_alerts else "",
        }


def patient_list_statement(filters: PatientListFilters) -> Select[tuple[Patient]]:
    """Patients matching ``filters``, ordered by MRN, with their diagnosis loaded."""
    statement = select(Patient).options(joinedload(Patient.diagnosis)).order_by(Patient.mrn)
    if filters.q:
        pattern = _like_pattern(filters.q)
        full_name = Patient.given_name + " " + Patient.family_name
        statement = statement.where(
            or_(
                Patient.mrn.ilike(pattern, escape="\\"),
                Patient.given_name.ilike(pattern, escape="\\"),
                Patient.family_name.ilike(pattern, escape="\\"),
                full_name.ilike(pattern, escape="\\"),
            )
        )
    if filters.family:
        statement = statement.where(Patient.family_id == filters.family)
    if filters.diabetes_type:
        statement = statement.where(Patient.diagnosis.has(Diagnosis.diabetes_type == filters.diabetes_type))
    if filters.with_alerts:
        statement = statement.where(
            Patient.alerts.any(and_(Alert.status.in_(C.CURRENT_ALERT_STATUSES), Alert.kind == "alert"))
        )
    return statement


@dataclass(frozen=True)
class PatientListRow:
    """One row of the patient list."""

    patient: Patient
    actionable_variants: int
    alert_counts: Mapping[str, int]
    last_upload: GenomicUpload | None

    @property
    def alert_total(self) -> int:
        return sum(self.alert_counts.values())

    @property
    def age(self) -> str:
        return age_text(self.patient.birth_date)


def patient_list_rows(patients: Sequence[Patient], *, include_alerts: bool) -> list[PatientListRow]:
    """Aggregates for one page of patients: three grouped queries regardless of the page size.

    ``alert_counts`` maps severity → number of current alerts (empty unless ``include_alerts``).
    """
    ids = [patient.id for patient in patients]
    if not ids:
        return []
    actionable_rows = db.session.execute(
        select(PatientVariant.patient_id, func.count(PatientVariant.id))
        .where(
            PatientVariant.patient_id.in_(ids),
            PatientVariant.current_classification.in_(sorted(C.ACTIONABLE)),
        )
        .group_by(PatientVariant.patient_id)
    )
    actionable = {patient_id: count for patient_id, count in actionable_rows}
    alert_counts: dict[int, dict[str, int]] = defaultdict(dict)
    if include_alerts:
        rows = db.session.execute(
            select(Alert.patient_id, Alert.severity, func.count(Alert.id))
            .where(
                Alert.patient_id.in_(ids),
                Alert.status.in_(C.CURRENT_ALERT_STATUSES),
                Alert.kind == "alert",
            )
            .group_by(Alert.patient_id, Alert.severity)
        )
        for patient_id, severity, count in rows:
            alert_counts[patient_id][severity] = count
    latest_ids = (
        select(func.max(GenomicUpload.id)).where(GenomicUpload.patient_id.in_(ids)).group_by(GenomicUpload.patient_id)
    )
    uploads = {
        upload.patient_id: upload
        for upload in db.session.scalars(select(GenomicUpload).where(GenomicUpload.id.in_(latest_ids)))
    }
    return [
        PatientListRow(
            patient=patient,
            actionable_variants=actionable.get(patient.id, 0),
            alert_counts={
                severity: alert_counts[patient.id][severity]
                for severity in SEVERITY_GROUPS
                if alert_counts[patient.id].get(severity)
            },
            last_upload=uploads.get(patient.id),
        )
        for patient in patients
    ]


def family_size(family_id: str) -> int:
    """Number of patients sharing ``family_id``."""
    return db.session.scalar(select(func.count(Patient.id)).where(Patient.family_id == family_id)) or 0


# ---------------------------------------------------------------------------
# Patient page
# ---------------------------------------------------------------------------


def load_patient(patient_id: int) -> Patient:
    """The patient with diagnosis, medications, uploads (with uploader) and variants (with keys); 404 if absent."""
    return db.get_or_404(
        Patient,
        patient_id,
        options=[
            joinedload(Patient.diagnosis),
            selectinload(Patient.medications),
            selectinload(Patient.uploads).joinedload(GenomicUpload.uploaded_by),
            selectinload(Patient.variants).joinedload(PatientVariant.variant_key),
        ],
        description=f"Patient {patient_id} does not exist.",
    )


def relatives(patient: Patient) -> list[Patient]:
    """Other patients with the same family ID, ordered by MRN (empty without a family ID)."""
    if not patient.family_id:
        return []
    statement = (
        select(Patient)
        .where(Patient.family_id == patient.family_id, Patient.id != patient.id)
        .order_by(Patient.mrn)
    )
    return list(db.session.scalars(statement))


def medications_for_display(patient: Patient) -> tuple[list[Medication], list[Medication]]:
    """(current, stopped) medications; current by start date (newest first), stopped by end date."""
    current = sorted(
        patient.active_medications, key=lambda medication: (medication.start_date or date.min, medication.id),
        reverse=True,
    )
    stopped = sorted(
        (medication for medication in patient.medications if not medication.is_active),
        key=lambda medication: (medication.end_date or date.min, medication.id),
        reverse=True,
    )
    return current, stopped


@dataclass(frozen=True)
class ClassificationStep:
    """The class of a variant key in one activated release."""

    release: KbRelease
    classification: str


@dataclass(frozen=True)
class ProfileVariant:
    """One row of the genomic profile."""

    variant: PatientVariant
    known: KnownClass
    hgvs: str | None
    history: tuple[ClassificationStep, ...]

    @property
    def changed_across_releases(self) -> bool:
        """True when the activated releases disagree about the class of this variant."""
        return len({step.classification for step in self.history}) > 1


@dataclass(frozen=True)
class GenomicProfile:
    """The patient's variants classified under the current release, with release history."""

    release: KbRelease | None
    variants: list[ProfileVariant]
    releases: list[KbRelease] = field(default_factory=list)

    @property
    def actionable_count(self) -> int:
        return sum(C.is_actionable(row.variant.current_classification) for row in self.variants)


def genomic_profile(patient: Patient) -> GenomicProfile:
    """Classify the patient's variants for display (two queries plus one per 900 variants).

    ``known`` is the current release's record (expert panel, review stars, condition); ``history``
    lists the class in every activated release in activation order (a key absent from a release
    is *not classified*); ``hgvs`` comes from the current release, else the latest release that
    has one. Variants are ordered actionable first, then by class, gene and position.
    """
    release = KbRelease.current()
    releases = reevaluation.release_history()
    variants = sorted(
        patient.variants,
        key=lambda variant: (
            -C.CLASS_ORDER.get(variant.current_classification, 0),
            variant.gene or "",
            variant.variant_key.pos,
        ),
    )
    key_ids = [variant.variant_key_id for variant in variants]
    known = classification.classify_keys(release, key_ids)
    release_ids = [item.id for item in releases]
    if release is not None and release.id not in release_ids:
        release_ids.append(release.id)
    records: dict[tuple[int, int], tuple[str, str | None]] = {}
    for chunk in batched(key_ids, IN_CLAUSE_CHUNK):
        statement = select(
            KnownVariant.release_id, KnownVariant.variant_key_id, KnownVariant.classification, KnownVariant.hgvs
        ).where(KnownVariant.variant_key_id.in_(chunk), KnownVariant.release_id.in_(release_ids))
        for release_id, key_id, cls, hgvs in db.session.execute(statement):
            records[(release_id, key_id)] = (cls, hgvs)

    def hgvs_for(key_id: int) -> str | None:
        ordered = ([release] if release is not None else []) + list(reversed(releases))
        for candidate in ordered:
            record = records.get((candidate.id, key_id))
            if record is not None and record[1]:
                return record[1]
        return None

    rows = [
        ProfileVariant(
            variant=variant,
            known=known[variant.variant_key_id],
            hgvs=hgvs_for(variant.variant_key_id),
            history=tuple(
                ClassificationStep(item, records.get((item.id, variant.variant_key_id), ("NC", None))[0])
                for item in releases
            ),
        )
        for variant in variants
    ]
    return GenomicProfile(release=release, variants=rows, releases=releases)


@dataclass(frozen=True)
class AlertSummary:
    """Alert summary card of the patient page (clinicians and administrators only)."""

    counts: Mapping[str, int]
    notes: int
    superseded: int
    open_notices: int
    top_alerts: list[Alert]

    @property
    def total(self) -> int:
        return sum(self.counts.values())


def alert_summary(patient: Patient) -> AlertSummary:
    """Current alerts by severity, notes, superseded findings and open review notices (three queries)."""
    current = list(
        db.session.scalars(
            select(Alert)
            .where(Alert.patient_id == patient.id, Alert.status.in_(C.CURRENT_ALERT_STATUSES))
            .options(joinedload(Alert.confirmed_rule_version), joinedload(Alert.variant_key))
        )
    )
    alerts = [alert for alert in current if alert.kind == "alert"]
    counts = Counter(alert.severity for alert in alerts)
    superseded = db.session.scalar(
        select(func.count(Alert.id)).where(Alert.patient_id == patient.id, Alert.status == "superseded")
    )
    open_notices = db.session.scalar(
        select(func.count(ReviewNotice.id)).where(ReviewNotice.patient_id == patient.id, ReviewNotice.status == "open")
    )
    return AlertSummary(
        counts={severity: counts[severity] for severity in SEVERITY_GROUPS if counts[severity]},
        notes=len(current) - len(alerts),
        superseded=superseded or 0,
        open_notices=open_notices or 0,
        top_alerts=_sort_alerts(alerts)[:SUMMARY_ALERT_LIMIT],
    )


# ---------------------------------------------------------------------------
# Recommendations page
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class FindingView:
    """An alert or note as it stood at the time viewed (now, or the point in time)."""

    alert: Alert
    status: str
    release: KbRelease | None
    rule_version: RuleVersion | None
    classification: str
    events: list[AlertEvent]

    @property
    def rule_label(self) -> str:
        return self.rule_version.label if self.rule_version is not None else self.alert.rule_code


@dataclass(frozen=True)
class ChangePoint:
    """A moment at which the patient's findings changed (a shortcut of the point-in-time selector)."""

    at: datetime
    label: str
    run_id: int | None
    created: int
    superseded: int

    @property
    def moment(self) -> datetime:
        """The change time rounded up to the millisecond: the state at it includes the change."""
        return _ceil_to_millisecond(self.at)

    @property
    def value(self) -> str:
        """``?at=`` value selecting the state right after the change."""
        return point_in_time_value(self.moment)

    @property
    def display(self) -> str:
        return format_moment(self.moment)


@dataclass
class RecommendationsView:
    """Everything the recommendations page shows; ``at`` is None for the current state."""

    at: datetime | None
    groups: list[tuple[str, list[FindingView]]]
    notes: list[FindingView]
    open_notices: list[ReviewNotice] = field(default_factory=list)
    acknowledged_notices: list[ReviewNotice] = field(default_factory=list)
    history: list[Alert] = field(default_factory=list)
    change_points: list[ChangePoint] = field(default_factory=list)

    @property
    def alert_count(self) -> int:
        return sum(len(findings) for _severity, findings in self.groups)

    @property
    def at_display(self) -> str:
        """The viewed point in time for display (empty for the current state)."""
        return format_moment(self.at) if self.at is not None else ""

    @property
    def at_value(self) -> str:
        """Value of the point-in-time input (empty for the current state)."""
        return point_in_time_value(self.at) if self.at is not None else ""

    @property
    def at_step(self) -> str:
        """``step`` of the point-in-time input, fine enough for :attr:`at_value`."""
        return point_in_time_step(self.at)

    @property
    def counts(self) -> dict[str, int]:
        return {severity: len(findings) for severity, findings in self.groups}


def _finding_view(alert: Alert, at: datetime) -> FindingView:
    """A finding current at ``at``: its review status and evidence (release, rule version, class) then."""
    evidence = reevaluation.provenance_as_of(alert, at)
    return FindingView(
        alert=alert,
        status="reviewed" if alert.reviewed_at is not None and alert.reviewed_at <= at else "active",
        release=evidence.release if evidence is not None else alert.confirmed_release,
        rule_version=evidence.rule_version if evidence is not None else alert.confirmed_rule_version,
        classification=(evidence.classification if evidence is not None else None) or alert.classification,
        events=[event for event in alert.events if event.at <= at],
    )


def _group(findings: Iterable[FindingView]) -> tuple[list[tuple[str, list[FindingView]]], list[FindingView]]:
    """(alerts grouped by severity in display order, notes)."""
    by_severity: dict[str, list[FindingView]] = defaultdict(list)
    notes: list[FindingView] = []
    for finding in findings:
        if finding.alert.kind == "note":
            notes.append(finding)
        else:
            by_severity[finding.alert.severity or "info"].append(finding)
    groups = [(severity, by_severity[severity]) for severity in SEVERITY_GROUPS if by_severity[severity]]
    return groups, notes


def _patient_alerts(patient: Patient) -> list[Alert]:
    """All alerts and notes of the patient with their provenance (a fixed number of queries)."""
    statement = (
        select(Alert)
        .where(Alert.patient_id == patient.id)
        .options(
            joinedload(Alert.variant_key),
            joinedload(Alert.patient_variant),
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
        )
    )
    return list(db.session.scalars(statement).unique())


def _change_points(alerts: Iterable[Alert]) -> list[ChangePoint]:
    """Moments at which findings were created or superseded, newest first."""
    labels: dict[datetime, str] = {}
    runs: dict[datetime, int | None] = {}
    created: Counter[datetime] = Counter()
    superseded: Counter[datetime] = Counter()
    for alert in alerts:
        created[alert.created_at] += 1
        # A creation names the trigger; it wins over a supersession reason at the same moment.
        labels[alert.created_at] = C.TRIGGER_LABELS[alert.trigger]
        runs.setdefault(alert.created_at, alert.created_run_id)
        if alert.superseded_at is not None:
            superseded[alert.superseded_at] += 1
            labels.setdefault(alert.superseded_at, C.SUPERSEDED_REASON_LABELS.get(alert.superseded_reason or "", ""))
            runs.setdefault(alert.superseded_at, alert.superseded_run_id)
    moments = sorted(set(created) | set(superseded), reverse=True)[:CHANGE_POINT_LIMIT]
    return [
        ChangePoint(
            at=moment,
            label=labels[moment] or "Re-evaluation",
            run_id=runs.get(moment),
            created=created[moment],
            superseded=superseded[moment],
        )
        for moment in moments
    ]


def _notices(patient: Patient) -> list[ReviewNotice]:
    statement = (
        select(ReviewNotice)
        .where(ReviewNotice.patient_id == patient.id)
        .options(
            joinedload(ReviewNotice.variant_key),
            joinedload(ReviewNotice.acknowledged_by),
            joinedload(ReviewNotice.run).joinedload(ReevaluationRun.new_release),
            selectinload(ReviewNotice.alerts),
        )
        .order_by(ReviewNotice.created_at.desc(), ReviewNotice.id.desc())
    )
    return list(db.session.scalars(statement).unique())


def recommendations_view(patient: Patient) -> RecommendationsView:
    """Current alerts and notes, review notices, superseded history and change points."""
    now = utcnow()
    alerts = _patient_alerts(patient)
    current = [_finding_view(alert, now) for alert in _sort_alerts(alert for alert in alerts if alert.is_current)]
    groups, notes = _group(current)
    notices = _notices(patient)
    history = sorted(
        (alert for alert in alerts if not alert.is_current),
        key=lambda alert: (alert.superseded_at or alert.created_at, alert.id),
        reverse=True,
    )
    return RecommendationsView(
        at=None,
        groups=groups,
        notes=notes,
        open_notices=[notice for notice in notices if notice.status == "open"],
        acknowledged_notices=[notice for notice in notices if notice.status != "open"],
        history=history,
        change_points=_change_points(alerts),
    )


def recommendations_as_of(patient: Patient, at: datetime) -> RecommendationsView:
    """The findings that were current at ``at`` with the evidence they rested on then (§7.5)."""
    # Loading every alert with its provenance first lets alerts_as_of reuse those objects.
    alerts = _patient_alerts(patient)
    current = [_finding_view(alert, at) for alert in _sort_alerts(reevaluation.alerts_as_of(patient, at))]
    groups, notes = _group(current)
    return RecommendationsView(at=at, groups=groups, notes=notes, change_points=_change_points(alerts))
