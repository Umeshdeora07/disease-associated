"""Read models of the dashboard (``/``): the clinical overview and the laboratory overview.

Each builder issues a fixed number of aggregate queries (related rows are eager-loaded), so the
dashboard cost does not grow with the number of patients shown. Lab technicians get
:func:`lab_dashboard`, which contains no alert information (SPEC §8.1).

Owned by the UI-B work package.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import case, distinct, exists, func, select
from sqlalchemy.orm import joinedload

from .. import constants as C
from ..extensions import db
from ..models import Alert, GenomicUpload, KbRelease, Patient, PatientVariant, ReevaluationRun, ReviewNotice, RuleVersion
from ..services.rules import rule_code_sort_key

#: Items per "needs attention" list (alerts and notices each), recent runs and recent uploads.
ATTENTION_ITEMS = 6
RECENT_RUNS = 5
RECENT_UPLOADS = 8
AWAITING_VCF_SHOWN = 6

#: "Needs attention": active (not yet reviewed) high-priority alerts raised by a reclassification.
_ATTENTION_ALERT = (
    (Alert.status == "active")
    & (Alert.kind == "alert")
    & Alert.is_reclassified.is_(True)
    & (Alert.priority == "high")
)


@dataclass(frozen=True)
class RuleBar:
    """Current alerts of one rule; ``percent`` is relative to the busiest rule (0–100)."""

    code: str
    title: str
    severity: str
    count: int
    percent: float


@dataclass(frozen=True)
class ClinicalDashboard:
    """Everything the clinician / administrator dashboard shows."""

    patients: int
    actionable_carriers: int
    alerts_by_severity: dict[str, int]
    open_notices: int
    last_release_run: ReevaluationRun | None
    reclassified_in_last_run: int
    release: KbRelease | None
    rule_versions: list[RuleVersion]
    attention_alerts: list[Alert]
    attention_alerts_total: int
    attention_notices: list[ReviewNotice]
    recent_runs: list[ReevaluationRun]
    rule_bars: list[RuleBar]

    @property
    def current_alerts(self) -> int:
        """Current (active or reviewed) alerts of every severity."""
        return sum(self.alerts_by_severity.values())

    @property
    def needs_attention(self) -> bool:
        """True when a reclassification alert awaits review or a review notice is open."""
        return bool(self.attention_alerts or self.attention_notices)

    @property
    def attention_alerts_hidden(self) -> int:
        """Attention alerts beyond those listed."""
        return self.attention_alerts_total - len(self.attention_alerts)

    @property
    def attention_notices_hidden(self) -> int:
        """Open review notices beyond those listed."""
        return self.open_notices - len(self.attention_notices)


def _patient_count() -> int:
    return db.session.scalar(select(func.count(Patient.id))) or 0


def _alerts_by_severity() -> dict[str, int]:
    rows = db.session.execute(
        select(Alert.severity, func.count(Alert.id))
        .where(Alert.kind == "alert", Alert.status.in_(C.CURRENT_ALERT_STATUSES))
        .group_by(Alert.severity)
    )
    counts = dict(rows.all())
    return {severity: counts.get(severity, 0) for severity in C.SEVERITIES}


def _last_release_run() -> ReevaluationRun | None:
    """The latest successful release activation (the "last run" a reclassification comes from)."""
    return db.session.scalars(
        select(ReevaluationRun)
        .options(joinedload(ReevaluationRun.old_release), joinedload(ReevaluationRun.new_release))
        .where(ReevaluationRun.trigger == "clinvar_release", ReevaluationRun.status == "success")
        .order_by(ReevaluationRun.id.desc())
        .limit(1)
    ).first()


def _rule_bars(rule_versions: list[RuleVersion]) -> list[RuleBar]:
    """Current alerts per current rule (rules without alerts included), in code order."""
    counts = dict(
        db.session.execute(
            select(Alert.rule_code, func.count(Alert.id))
            .where(Alert.kind == "alert", Alert.status.in_(C.CURRENT_ALERT_STATUSES))
            .group_by(Alert.rule_code)
        )
        .all()
    )
    busiest = max(counts.values(), default=0)
    bars = [
        RuleBar(
            version.code,
            version.title,
            version.severity,
            counts.get(version.code, 0),
            round(100 * counts.get(version.code, 0) / busiest, 2) if busiest else 0.0,
        )
        for version in rule_versions
    ]
    return sorted(bars, key=lambda bar: rule_code_sort_key(bar.code))


def clinical_dashboard() -> ClinicalDashboard:
    """KPIs, current knowledge base, items needing attention, recent runs and alerts per rule."""
    actionable = db.session.scalar(
        select(func.count(distinct(PatientVariant.patient_id))).where(
            PatientVariant.current_classification.in_(sorted(C.ACTIONABLE))
        )
    )
    open_notices = db.session.scalar(select(func.count(ReviewNotice.id)).where(ReviewNotice.status == "open"))
    last_run = _last_release_run()
    reclassified = 0
    if last_run is not None:
        reclassified = (
            db.session.scalar(
                select(func.count(Alert.id)).where(
                    Alert.created_run_id == last_run.id, Alert.kind == "alert", Alert.is_reclassified.is_(True)
                )
            )
            or 0
        )
    severity_rank = case(C.SEVERITY_ORDER, value=Alert.severity, else_=-1)
    attention_alerts = db.session.scalars(
        select(Alert)
        .options(joinedload(Alert.patient), joinedload(Alert.variant_key))
        .where(_ATTENTION_ALERT)
        .order_by(severity_rank.desc(), Alert.created_at.desc(), Alert.id.desc())
        .limit(ATTENTION_ITEMS)
    ).all()
    attention_total = db.session.scalar(select(func.count(Alert.id)).where(_ATTENTION_ALERT)) or 0
    attention_notices = db.session.scalars(
        select(ReviewNotice)
        .options(joinedload(ReviewNotice.patient), joinedload(ReviewNotice.variant_key), joinedload(ReviewNotice.run))
        .where(ReviewNotice.status == "open")
        .order_by(ReviewNotice.created_at.desc(), ReviewNotice.id.desc())
        .limit(ATTENTION_ITEMS)
    ).all()
    recent_runs = db.session.scalars(
        select(ReevaluationRun)
        .options(joinedload(ReevaluationRun.old_release), joinedload(ReevaluationRun.new_release))
        .order_by(ReevaluationRun.started_at.desc(), ReevaluationRun.id.desc())
        .limit(RECENT_RUNS)
    ).all()
    rule_versions = RuleVersion.current_versions()
    return ClinicalDashboard(
        patients=_patient_count(),
        actionable_carriers=actionable or 0,
        alerts_by_severity=_alerts_by_severity(),
        open_notices=open_notices or 0,
        last_release_run=last_run,
        reclassified_in_last_run=reclassified,
        release=KbRelease.current(),
        rule_versions=sorted(rule_versions, key=lambda version: rule_code_sort_key(version.code)),
        attention_alerts=list(attention_alerts),
        attention_alerts_total=attention_total,
        attention_notices=list(attention_notices),
        recent_runs=list(recent_runs),
        rule_bars=_rule_bars(rule_versions),
    )


@dataclass(frozen=True)
class LabDashboard:
    """The laboratory view: patients, VCF uploads and patients still awaiting a VCF (no alerts)."""

    patients: int
    awaiting_vcf: int
    uploads_accepted: int
    uploads_rejected: int
    recent_uploads: list[GenomicUpload]
    awaiting_patients: list[Patient]


def lab_dashboard() -> LabDashboard:
    """Counts of patients and uploads, the latest uploads and patients without an accepted VCF."""
    uploads = dict(
        db.session.execute(select(GenomicUpload.status, func.count(GenomicUpload.id)).group_by(GenomicUpload.status))
        .all()
    )
    has_accepted = exists().where(GenomicUpload.patient_id == Patient.id, GenomicUpload.status == "accepted")
    awaiting = db.session.scalar(select(func.count(Patient.id)).where(~has_accepted)) or 0
    awaiting_patients = db.session.scalars(
        select(Patient).where(~has_accepted).order_by(Patient.created_at.desc(), Patient.id.desc()).limit(AWAITING_VCF_SHOWN)
    ).all()
    recent = db.session.scalars(
        select(GenomicUpload)
        .options(joinedload(GenomicUpload.patient), joinedload(GenomicUpload.uploaded_by))
        .order_by(GenomicUpload.uploaded_at.desc(), GenomicUpload.id.desc())
        .limit(RECENT_UPLOADS)
    ).all()
    return LabDashboard(
        patients=_patient_count(),
        awaiting_vcf=awaiting,
        uploads_accepted=uploads.get("accepted", 0),
        uploads_rejected=uploads.get("rejected", 0),
        recent_uploads=list(recent),
        awaiting_patients=list(awaiting_patients),
    )
