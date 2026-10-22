"""ORM models for GE-EHR (SPEC §3).

SQLAlchemy 2.0 style declarative models on Flask-SQLAlchemy's ``db.Model``. Only portable column
types are used, so the schema works on SQLite and PostgreSQL. All datetimes are naive UTC values
produced by :func:`utcnow`. Enumerated columns carry CHECK constraints built from
:mod:`ge_ehr.constants`.

Owned by the Foundation work package.
"""

from __future__ import annotations

import hashlib
import hmac
from collections.abc import Iterable
from datetime import date, datetime, timezone
from typing import Any

from flask import current_app, has_app_context
from flask_login import UserMixin
from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    select,
    text,
)
from sqlalchemy.orm import Mapped, WriteOnlyMapped, mapped_column, relationship, validates
from werkzeug.security import check_password_hash, generate_password_hash

from . import constants as C
from .extensions import db


def utcnow() -> datetime:
    """Current time as a naive UTC datetime (the storage convention of every timestamp)."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def utc_today() -> date:
    """Today's date in UTC."""
    return utcnow().date()


def _one_of(column: str, values: Iterable[str], *, nullable: bool = False) -> CheckConstraint:
    """CHECK constraint restricting ``column`` to ``values`` (named ``ck_<table>_<column>``)."""
    allowed = ", ".join(f"'{value}'" for value in values)
    condition = f'"{column}" IN ({allowed})'
    if nullable:
        condition = f'"{column}" IS NULL OR {condition}'
    return CheckConstraint(condition, name=column)


class Model(db.Model):
    """Abstract base: scalar column defaults are applied at construction time.

    SQLAlchemy only applies ``default=`` values on INSERT, so ``ReevaluationRun().alerts_created``
    would be None until the first flush; this base makes counters, flags and statuses usable
    immediately (``run.alerts_created += 1`` works on a fresh object).
    """

    __abstract__ = True

    def __init__(self, **kwargs: Any) -> None:
        for column in self.__table__.columns:
            default = column.default
            if column.key not in kwargs and default is not None and default.is_scalar:
                kwargs[column.key] = default.arg
        super().__init__(**kwargs)

    def __repr__(self) -> str:
        return f"<{type(self).__name__} id={self.id}>"


# ---------------------------------------------------------------------------
# Users and audit
# ---------------------------------------------------------------------------


class User(UserMixin, Model):
    """An account; ``role`` is one of :data:`ge_ehr.constants.ROLES`."""

    __tablename__ = "users"
    __table_args__ = (_one_of("role", C.ROLES),)

    id: Mapped[int] = mapped_column(primary_key=True)
    username: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    full_name: Mapped[str] = mapped_column(String(128))
    email: Mapped[str | None] = mapped_column(String(255))
    password_hash: Mapped[str] = mapped_column(String(255), default="")
    role: Mapped[str] = mapped_column(String(32), index=True)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    must_change_password: Mapped[bool] = mapped_column(Boolean, default=False)
    failed_logins: Mapped[int] = mapped_column(Integer, default=0)
    locked_until: Mapped[datetime | None] = mapped_column(DateTime)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime)

    @validates("username")
    def _normalise_username(self, _key: str, value: str | None) -> str | None:
        return value.strip().lower() if value is not None else None

    @validates("role")
    def _validate_role(self, _key: str, value: str) -> str:
        if value not in C.ROLES:
            raise ValueError(f"Unknown role {value!r}; expected one of {', '.join(C.ROLES)}.")
        return value

    @property
    def is_active(self) -> bool:
        """Flask-Login hook: deactivated accounts cannot sign in."""
        return bool(self.active)

    @property
    def role_label(self) -> str:
        return C.ROLE_LABELS[self.role]

    def set_password(self, password: str) -> None:
        """Hash and store ``password`` with Werkzeug scrypt (work factor from app config)."""
        method = current_app.config["PASSWORD_HASH_METHOD"] if has_app_context() else "scrypt"
        self.password_hash = generate_password_hash(password, method=method)

    def check_password(self, password: str) -> bool:
        """Return True when ``password`` matches the stored hash."""
        if not self.password_hash:
            return False
        return check_password_hash(self.password_hash, password)

    def has_role(self, *roles: str) -> bool:
        """True when the user's role is one of ``roles``."""
        return self.role in roles

    @property
    def session_fingerprint(self) -> str:
        """Short digest of the password hash; changing the password invalidates other sessions."""
        return hashlib.sha256((self.password_hash or "").encode()).hexdigest()[:16]

    def get_id(self) -> str:
        """Flask-Login session id: ``"<id>:<fingerprint>"``."""
        return f"{self.id}:{self.session_fingerprint}"

    @classmethod
    def from_session_id(cls, session_id: str) -> User | None:
        """Load the active user named by a :meth:`get_id` value, or None if stale or invalid."""
        user_id, _, fingerprint = session_id.partition(":")
        if not user_id.isdigit():
            return None
        user = db.session.get(cls, int(user_id))
        if user is None or not user.is_active or not hmac.compare_digest(user.session_fingerprint, fingerprint):
            return None
        return user

    def is_locked(self, at: datetime | None = None) -> bool:
        """True while a login lockout is in force."""
        return self.locked_until is not None and self.locked_until > (at or utcnow())

    def __repr__(self) -> str:
        return f"<User {self.username!r} role={self.role}>"


class AuditLog(Model):
    """One security-relevant action (append-only)."""

    __tablename__ = "audit_log"
    __table_args__ = (Index("ix_audit_log_entity", "entity_type", "entity_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
    user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), index=True)
    username: Mapped[str | None] = mapped_column(String(64), index=True)
    action: Mapped[str] = mapped_column(String(64), index=True)
    entity_type: Mapped[str | None] = mapped_column(String(64))
    entity_id: Mapped[int | None] = mapped_column(Integer)
    detail: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    ip_address: Mapped[str | None] = mapped_column(String(45))

    user: Mapped[User | None] = relationship()

    def __repr__(self) -> str:
        return f"<AuditLog {self.action!r} user={self.username!r} at={self.at}>"


# ---------------------------------------------------------------------------
# Patients (F1)
# ---------------------------------------------------------------------------


class Patient(Model):
    """A synthetic patient with diagnosis, diabetes medications and genomic data."""

    __tablename__ = "patients"
    __table_args__ = (_one_of("sex", C.SEXES),)

    id: Mapped[int] = mapped_column(primary_key=True)
    mrn: Mapped[str] = mapped_column(String(32), unique=True, index=True)
    given_name: Mapped[str] = mapped_column(String(100))
    family_name: Mapped[str] = mapped_column(String(100), index=True)
    sex: Mapped[str] = mapped_column(String(16), default="unknown")
    birth_date: Mapped[date] = mapped_column(Date)
    family_id: Mapped[str | None] = mapped_column(String(64), index=True)
    external_id: Mapped[str | None] = mapped_column(String(64), index=True)
    notes: Mapped[str | None] = mapped_column(Text)
    is_synthetic: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)

    diagnosis: Mapped[Diagnosis | None] = relationship(
        back_populates="patient", uselist=False, cascade="all, delete-orphan"
    )
    medications: Mapped[list[Medication]] = relationship(
        back_populates="patient", cascade="all, delete-orphan", order_by="Medication.id"
    )
    variants: Mapped[list[PatientVariant]] = relationship(back_populates="patient", order_by="PatientVariant.id")
    uploads: Mapped[list[GenomicUpload]] = relationship(back_populates="patient", order_by="GenomicUpload.id")
    alerts: Mapped[list[Alert]] = relationship(back_populates="patient", order_by="Alert.id")
    review_notices: Mapped[list[ReviewNotice]] = relationship(
        back_populates="patient", order_by="ReviewNotice.id"
    )

    @property
    def display_name(self) -> str:
        return f"{self.given_name} {self.family_name}".strip()

    @property
    def age_years(self) -> int:
        """Completed years of age today (UTC)."""
        today, born = utc_today(), self.birth_date
        return today.year - born.year - ((today.month, today.day) < (born.month, born.day))

    @property
    def active_medications(self) -> list[Medication]:
        return [medication for medication in self.medications if medication.is_active]

    @property
    def current_drug_classes(self) -> frozenset[str]:
        return frozenset(medication.drug_class for medication in self.active_medications)

    def __repr__(self) -> str:
        return f"<Patient {self.mrn!r}>"


class Diagnosis(Model):
    """Diabetes diagnosis of a patient (one per patient)."""

    __tablename__ = "diagnoses"
    __table_args__ = (_one_of("diabetes_type", C.DIABETES_TYPES),)

    id: Mapped[int] = mapped_column(primary_key=True)
    patient_id: Mapped[int] = mapped_column(ForeignKey("patients.id"), unique=True, index=True)
    diabetes_type: Mapped[str] = mapped_column(String(16), default="unknown")
    age_at_diagnosis_months: Mapped[int | None] = mapped_column(Integer)
    diagnosed_on: Mapped[date | None] = mapped_column(Date)
    notes: Mapped[str | None] = mapped_column(Text)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)

    patient: Mapped[Patient] = relationship(back_populates="diagnosis")


class Medication(Model):
    """A diabetes medication; active while ``end_date`` is None."""

    __tablename__ = "medications"
    __table_args__ = (_one_of("drug_class", C.DRUG_CLASSES),)

    id: Mapped[int] = mapped_column(primary_key=True)
    patient_id: Mapped[int] = mapped_column(ForeignKey("patients.id"), index=True)
    drug_name: Mapped[str] = mapped_column(String(128))
    drug_class: Mapped[str] = mapped_column(String(16))
    dose: Mapped[str | None] = mapped_column(String(64))
    start_date: Mapped[date | None] = mapped_column(Date)
    end_date: Mapped[date | None] = mapped_column(Date)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    created_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), index=True)

    patient: Mapped[Patient] = relationship(back_populates="medications")
    created_by: Mapped[User | None] = relationship()

    @property
    def is_active(self) -> bool:
        return self.end_date is None


class GenomicUpload(Model):
    """One patient VCF upload; rejected uploads are kept (without variants) for audit."""

    __tablename__ = "genomic_uploads"
    __table_args__ = (_one_of("status", C.UPLOAD_STATUSES),)

    id: Mapped[int] = mapped_column(primary_key=True)
    patient_id: Mapped[int] = mapped_column(ForeignKey("patients.id"), index=True)
    filename: Mapped[str] = mapped_column(String(255))
    sha256: Mapped[str] = mapped_column(String(64), index=True)
    size_bytes: Mapped[int] = mapped_column(Integer, default=0)
    sample_id: Mapped[str | None] = mapped_column(String(128))
    uploaded_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), index=True)
    uploaded_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    status: Mapped[str] = mapped_column(String(16))
    error: Mapped[str | None] = mapped_column(Text)
    total_records: Mapped[int] = mapped_column(Integer, default=0)
    panel_variants: Mapped[int] = mapped_column(Integer, default=0)
    off_panel_skipped: Mapped[int] = mapped_column(Integer, default=0)
    non_pass_skipped: Mapped[int] = mapped_column(Integer, default=0)
    ref_calls_skipped: Mapped[int] = mapped_column(Integer, default=0)

    patient: Mapped[Patient] = relationship(back_populates="uploads")
    uploaded_by: Mapped[User | None] = relationship()
    variants: Mapped[list[PatientVariant]] = relationship(back_populates="upload")


# ---------------------------------------------------------------------------
# Variants
# ---------------------------------------------------------------------------


class VariantKey(Model):
    """A GRCh38 variant key (chrom, pos, ref, alt), shared by patients and KB releases."""

    __tablename__ = "variant_keys"
    __table_args__ = (
        UniqueConstraint("chrom", "pos", "ref", "alt"),
        Index("ix_variant_keys_chrom_pos", "chrom", "pos"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    chrom: Mapped[str] = mapped_column(String(8))
    pos: Mapped[int] = mapped_column(Integer)
    ref: Mapped[str] = mapped_column(Text)
    alt: Mapped[str] = mapped_column(Text)
    gene: Mapped[str | None] = mapped_column(String(16), index=True)

    @property
    def label(self) -> str:
        """``"7-44145200-C-T"``."""
        return f"{self.chrom}-{self.pos}-{self.ref}-{self.alt}"

    @staticmethod
    def normalize(chrom: str, pos: int | str, ref: str, alt: str) -> tuple[str, int, str, str]:
        """Normalise a key: chromosome without ``chr`` prefix (``MT`` for mito), upper-case alleles."""
        try:
            position = int(pos)
        except (TypeError, ValueError):
            raise ValueError(f"Variant position must be a positive integer, got {pos!r}.") from None
        if position < 1:
            raise ValueError(f"Variant position must be a positive integer, got {pos!r}.")
        ref_allele, alt_allele = str(ref).strip().upper(), str(alt).strip().upper()
        if not ref_allele or not alt_allele:
            raise ValueError("Variant REF and ALT alleles must not be empty.")
        return C.normalize_chrom(chrom), position, ref_allele, alt_allele

    @classmethod
    def find(cls, chrom: str, pos: int | str, ref: str, alt: str) -> VariantKey | None:
        """Return the stored key (after normalisation) or None."""
        chrom, pos, ref, alt = cls.normalize(chrom, pos, ref, alt)
        statement = select(cls).filter_by(chrom=chrom, pos=pos, ref=ref, alt=alt)
        return db.session.execute(statement).scalar_one_or_none()

    @classmethod
    def get_or_create(cls, chrom: str, pos: int | str, ref: str, alt: str, gene: str | None = None) -> VariantKey:
        """Return the key for (chrom, pos, ref, alt), creating and flushing it when new.

        ``gene`` defaults to the panel gene containing the position; an existing key without a
        gene is completed when ``gene`` is given.
        """
        chrom, pos, ref, alt = cls.normalize(chrom, pos, ref, alt)
        key = db.session.execute(select(cls).filter_by(chrom=chrom, pos=pos, ref=ref, alt=alt)).scalar_one_or_none()
        if key is None:
            key = cls(chrom=chrom, pos=pos, ref=ref, alt=alt, gene=gene or C.gene_for_position(chrom, pos))
            db.session.add(key)
            db.session.flush()
        elif gene and key.gene is None:
            key.gene = gene
        return key

    def __repr__(self) -> str:
        return f"<VariantKey {self.label}>"


class PatientVariant(Model):
    """A variant observed in a patient (union across uploads) with its current classification."""

    __tablename__ = "patient_variants"
    __table_args__ = (
        UniqueConstraint("patient_id", "variant_key_id"),
        _one_of("zygosity", C.ZYGOSITIES),
        _one_of("current_classification", C.CLASSES),
        _one_of("reported_classification", C.CLASSES),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    # patient_id is covered by the leading column of the (patient_id, variant_key_id) unique index.
    patient_id: Mapped[int] = mapped_column(ForeignKey("patients.id"))
    variant_key_id: Mapped[int] = mapped_column(ForeignKey("variant_keys.id"), index=True)
    upload_id: Mapped[int | None] = mapped_column(ForeignKey("genomic_uploads.id"), index=True)
    zygosity: Mapped[str] = mapped_column(String(16), default="heterozygous")
    gene: Mapped[str | None] = mapped_column(String(16), index=True)
    current_classification: Mapped[str] = mapped_column(String(4), default="NC", index=True)
    classification_release_id: Mapped[int | None] = mapped_column(ForeignKey("kb_releases.id"), index=True)
    reported_classification: Mapped[str] = mapped_column(String(4), default="NC")
    reclassified_count: Mapped[int] = mapped_column(Integer, default=0)
    last_reclassified_at: Mapped[datetime | None] = mapped_column(DateTime)
    last_reclassified_run_id: Mapped[int | None] = mapped_column(ForeignKey("reevaluation_runs.id"), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    patient: Mapped[Patient] = relationship(back_populates="variants")
    variant_key: Mapped[VariantKey] = relationship()
    upload: Mapped[GenomicUpload | None] = relationship(back_populates="variants")
    classification_release: Mapped[KbRelease | None] = relationship()
    last_reclassified_run: Mapped[ReevaluationRun | None] = relationship()

    @property
    def is_amended(self) -> bool:
        """True once a release activation changed the classification (FHIR status ``amended``)."""
        return (self.reclassified_count or 0) > 0

    @property
    def label(self) -> str:
        return self.variant_key.label

    def __repr__(self) -> str:
        return f"<PatientVariant patient={self.patient_id} key={self.variant_key_id} {self.current_classification}>"


# ---------------------------------------------------------------------------
# Knowledge base (F2)
# ---------------------------------------------------------------------------


class KbRelease(Model):
    """One imported ClinVar(-format) release; at most one is current."""

    __tablename__ = "kb_releases"
    __table_args__ = (_one_of("source", C.RELEASE_SOURCES),)

    id: Mapped[int] = mapped_column(primary_key=True)
    label: Mapped[str] = mapped_column(String(128))
    source: Mapped[str] = mapped_column(String(16), default="synthetic")
    release_date: Mapped[date | None] = mapped_column(Date)
    file_name: Mapped[str | None] = mapped_column(String(255))
    file_sha256: Mapped[str | None] = mapped_column(String(64), unique=True)
    file_date: Mapped[str | None] = mapped_column(String(16))
    imported_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    imported_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), index=True)
    record_count: Mapped[int] = mapped_column(Integer, default=0)
    gene_counts: Mapped[dict[str, int]] = mapped_column(JSON, default=dict)
    class_counts: Mapped[dict[str, int]] = mapped_column(JSON, default=dict)
    is_current: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    activated_at: Mapped[datetime | None] = mapped_column(DateTime)
    sequence: Mapped[int | None] = mapped_column(Integer, unique=True)
    is_synthetic: Mapped[bool] = mapped_column(Boolean, default=True)
    notes: Mapped[str | None] = mapped_column(Text)

    imported_by: Mapped[User | None] = relationship()
    # Write-only: a release holds thousands of records; query with ``release.known_variants.select()``.
    known_variants: WriteOnlyMapped[KnownVariant] = relationship(back_populates="release")

    @classmethod
    def current(cls) -> KbRelease | None:
        """The currently active release, or None before the first activation."""
        statement = select(cls).where(cls.is_current.is_(True)).order_by(cls.id.desc()).limit(1)
        return db.session.execute(statement).scalar_one_or_none()

    def __repr__(self) -> str:
        return f"<KbRelease {self.label!r} current={self.is_current}>"


class KnownVariant(Model):
    """A classified variant within one release."""

    __tablename__ = "known_variants"
    __table_args__ = (
        UniqueConstraint("release_id", "variant_key_id"),
        Index("ix_known_variants_release_variation", "release_id", "clinvar_variation_id"),
        Index("ix_known_variants_release_gene", "release_id", "gene"),
        _one_of("classification", C.CLASSES),
        _one_of("mechanism", C.MECHANISMS),
        _one_of("classification_source", C.CLASSIFICATION_SOURCES),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    # release_id is covered by the leading column of the (release_id, variant_key_id) unique index.
    release_id: Mapped[int] = mapped_column(ForeignKey("kb_releases.id"))
    variant_key_id: Mapped[int] = mapped_column(ForeignKey("variant_keys.id"), index=True)
    clinvar_variation_id: Mapped[int | None] = mapped_column(Integer)
    allele_id: Mapped[int | None] = mapped_column(Integer)
    gene: Mapped[str | None] = mapped_column(String(16))
    classification: Mapped[str] = mapped_column(String(4))
    clinvar_significance: Mapped[str | None] = mapped_column(Text)
    review_status: Mapped[str | None] = mapped_column(Text)
    review_stars: Mapped[int] = mapped_column(Integer, default=0)
    is_expert_panel: Mapped[bool] = mapped_column(Boolean, default=False)
    classification_source: Mapped[str] = mapped_column(String(32), default="clinvar")
    condition: Mapped[str | None] = mapped_column(Text)
    mechanism: Mapped[str] = mapped_column(String(16), default="unknown")
    hgvs: Mapped[str | None] = mapped_column(Text)
    molecular_consequence: Mapped[str | None] = mapped_column(Text)

    release: Mapped[KbRelease] = relationship(back_populates="known_variants")
    variant_key: Mapped[VariantKey] = relationship()


# ---------------------------------------------------------------------------
# Rules (F3)
# ---------------------------------------------------------------------------


class Rule(Model):
    """A decision-support rule code (D1…D6) with its versions."""

    __tablename__ = "rules"

    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(String(8), unique=True, index=True)
    title: Mapped[str] = mapped_column(String(255))
    description: Mapped[str | None] = mapped_column(Text)

    versions: Mapped[list[RuleVersion]] = relationship(back_populates="rule", order_by="RuleVersion.version")

    @property
    def current_version(self) -> RuleVersion | None:
        return next((version for version in self.versions if version.is_current), None)

    def __repr__(self) -> str:
        return f"<Rule {self.code}>"


class RuleVersion(Model):
    """One immutable version of a rule (definition JSON as in SPEC §6.1)."""

    __tablename__ = "rule_versions"
    __table_args__ = (
        UniqueConstraint("rule_id", "version"),
        _one_of("severity", C.SEVERITIES),
        _one_of("kind", C.RULE_KINDS),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    # rule_id is covered by the leading column of the (rule_id, version) unique index.
    rule_id: Mapped[int] = mapped_column(ForeignKey("rules.id"))
    code: Mapped[str] = mapped_column(String(8), index=True)
    version: Mapped[int] = mapped_column(Integer)
    definition: Mapped[dict[str, Any]] = mapped_column(JSON)
    genes: Mapped[list[str]] = mapped_column(JSON, default=list)
    severity: Mapped[str] = mapped_column(String(8))
    title: Mapped[str] = mapped_column(String(255))
    recommendation: Mapped[str] = mapped_column(Text)
    source: Mapped[str | None] = mapped_column(Text)
    kind: Mapped[str] = mapped_column(String(16), default="treatment")
    is_current: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    rule_set_label: Mapped[str | None] = mapped_column(String(128))
    change_note: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    imported_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), index=True)

    rule: Mapped[Rule] = relationship(back_populates="versions")
    imported_by: Mapped[User | None] = relationship()

    @property
    def label(self) -> str:
        """``"D4 v2"``."""
        return f"{self.code} v{self.version}"

    @classmethod
    def current_versions(cls) -> list[RuleVersion]:
        """The current version of every rule, ordered by code."""
        statement = select(cls).where(cls.is_current.is_(True)).order_by(cls.code)
        return list(db.session.execute(statement).scalars())

    def __repr__(self) -> str:
        return f"<RuleVersion {self.label} current={self.is_current}>"


# ---------------------------------------------------------------------------
# Alerts, provenance and review notices
# ---------------------------------------------------------------------------

_CURRENT_ALERT_CONDITION = "status IN ({})".format(", ".join(f"'{s}'" for s in C.CURRENT_ALERT_STATUSES))


class Alert(Model):
    """An alert or note produced by a rule for one (patient, variant). Never deleted.

    Invariant (enforced by a partial unique index): at most one *current* (active/reviewed)
    alert per (patient_id, rule_code, variant_key_id, kind). When a finding is replaced, the old
    row must be superseded in the same flush as (or before) the new one is inserted.
    """

    __tablename__ = "alerts"
    __table_args__ = (
        Index(
            "uq_alerts_current_finding",
            "patient_id",
            "rule_code",
            "variant_key_id",
            "kind",
            unique=True,
            sqlite_where=text(_CURRENT_ALERT_CONDITION),
            postgresql_where=text(_CURRENT_ALERT_CONDITION),
        ),
        Index("ix_alerts_patient_status", "patient_id", "status"),
        Index("ix_alerts_status_kind_severity", "status", "kind", "severity"),
        _one_of("kind", C.FINDING_KINDS),
        _one_of("severity", C.SEVERITIES, nullable=True),
        _one_of("status", C.ALERT_STATUSES),
        _one_of("trigger", C.TRIGGERS),
        _one_of("priority", C.PRIORITIES),
        _one_of("classification", C.CLASSES),
        _one_of("superseded_reason", C.SUPERSEDED_REASONS, nullable=True),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    patient_id: Mapped[int] = mapped_column(ForeignKey("patients.id"), index=True)
    patient_variant_id: Mapped[int | None] = mapped_column(ForeignKey("patient_variants.id"), index=True)
    variant_key_id: Mapped[int] = mapped_column(ForeignKey("variant_keys.id"), index=True)
    rule_code: Mapped[str] = mapped_column(String(8), index=True)
    created_rule_version_id: Mapped[int] = mapped_column(ForeignKey("rule_versions.id"), index=True)
    confirmed_rule_version_id: Mapped[int] = mapped_column(ForeignKey("rule_versions.id"), index=True)
    kind: Mapped[str] = mapped_column(String(8), default="alert")
    severity: Mapped[str | None] = mapped_column(String(8))
    title: Mapped[str] = mapped_column(String(255))
    message: Mapped[str] = mapped_column(Text)
    source: Mapped[str | None] = mapped_column(Text)
    classification: Mapped[str] = mapped_column(String(4))
    status: Mapped[str] = mapped_column(String(16), default="active", index=True)
    trigger: Mapped[str] = mapped_column(String(32))
    is_reclassified: Mapped[bool] = mapped_column(Boolean, default=False)
    priority: Mapped[str] = mapped_column(String(8), default="normal")
    notify: Mapped[bool] = mapped_column(Boolean, default=False)
    created_release_id: Mapped[int | None] = mapped_column(ForeignKey("kb_releases.id"), index=True)
    confirmed_release_id: Mapped[int | None] = mapped_column(ForeignKey("kb_releases.id"), index=True)
    created_run_id: Mapped[int | None] = mapped_column(ForeignKey("reevaluation_runs.id"), index=True)
    superseded_run_id: Mapped[int | None] = mapped_column(ForeignKey("reevaluation_runs.id"), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
    superseded_at: Mapped[datetime | None] = mapped_column(DateTime)
    superseded_reason: Mapped[str | None] = mapped_column(String(32))
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime)
    reviewed_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), index=True)
    review_comment: Mapped[str | None] = mapped_column(Text)
    review_notice_id: Mapped[int | None] = mapped_column(ForeignKey("review_notices.id"), index=True)

    patient: Mapped[Patient] = relationship(back_populates="alerts")
    variant_key: Mapped[VariantKey] = relationship()
    patient_variant: Mapped[PatientVariant | None] = relationship()
    created_rule_version: Mapped[RuleVersion] = relationship(foreign_keys=[created_rule_version_id])
    confirmed_rule_version: Mapped[RuleVersion] = relationship(foreign_keys=[confirmed_rule_version_id])
    created_release: Mapped[KbRelease | None] = relationship(foreign_keys=[created_release_id])
    confirmed_release: Mapped[KbRelease | None] = relationship(foreign_keys=[confirmed_release_id])
    created_run: Mapped[ReevaluationRun | None] = relationship(foreign_keys=[created_run_id])
    superseded_run: Mapped[ReevaluationRun | None] = relationship(foreign_keys=[superseded_run_id])
    events: Mapped[list[AlertEvent]] = relationship(
        back_populates="alert", order_by=lambda: [AlertEvent.at, AlertEvent.id]
    )
    review_notice: Mapped[ReviewNotice | None] = relationship(back_populates="alerts")
    reviewed_by: Mapped[User | None] = relationship(foreign_keys=[reviewed_by_id])

    @property
    def is_current(self) -> bool:
        """True while the alert is active or reviewed (not superseded)."""
        return self.status in C.CURRENT_ALERT_STATUSES

    @property
    def is_note(self) -> bool:
        return self.kind == "note"

    def __repr__(self) -> str:
        return f"<Alert {self.rule_code} {self.kind} patient={self.patient_id} {self.status}>"


class AlertEvent(Model):
    """Provenance event of an alert (created / confirmed / reviewed / superseded).

    ``created`` and ``confirmed`` events snapshot the finding's ``title``, ``message``, ``source``
    and ``severity`` as they stood from that moment on, so the point-in-time view shows the text
    of the time even after a later confirmation rewrote the alert's text (SPEC §7.5).
    """

    __tablename__ = "alert_events"
    __table_args__ = (_one_of("event", C.ALERT_EVENTS), _one_of("severity", C.SEVERITIES, nullable=True))

    id: Mapped[int] = mapped_column(primary_key=True)
    alert_id: Mapped[int] = mapped_column(ForeignKey("alerts.id"), index=True)
    event: Mapped[str] = mapped_column(String(16))
    release_id: Mapped[int | None] = mapped_column(ForeignKey("kb_releases.id"), index=True)
    rule_version_id: Mapped[int | None] = mapped_column(ForeignKey("rule_versions.id"), index=True)
    classification: Mapped[str | None] = mapped_column(String(4))
    run_id: Mapped[int | None] = mapped_column(ForeignKey("reevaluation_runs.id"), index=True)
    user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), index=True)
    at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    detail: Mapped[str | None] = mapped_column(Text)
    title: Mapped[str | None] = mapped_column(String(255))
    message: Mapped[str | None] = mapped_column(Text)
    source: Mapped[str | None] = mapped_column(Text)
    severity: Mapped[str | None] = mapped_column(String(8))

    alert: Mapped[Alert] = relationship(back_populates="events")
    release: Mapped[KbRelease | None] = relationship()
    rule_version: Mapped[RuleVersion | None] = relationship()
    run: Mapped[ReevaluationRun | None] = relationship()
    user: Mapped[User | None] = relationship()


class ReviewNotice(Model):
    """Asks a clinician to review alerts superseded by a downgrade or rule revision."""

    __tablename__ = "review_notices"
    __table_args__ = (
        _one_of("status", C.NOTICE_STATUSES),
        _one_of("old_classification", C.CLASSES),
        _one_of("new_classification", C.CLASSES),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    patient_id: Mapped[int] = mapped_column(ForeignKey("patients.id"), index=True)
    variant_key_id: Mapped[int] = mapped_column(ForeignKey("variant_keys.id"), index=True)
    run_id: Mapped[int | None] = mapped_column(ForeignKey("reevaluation_runs.id"), index=True)
    old_classification: Mapped[str] = mapped_column(String(4))
    new_classification: Mapped[str] = mapped_column(String(4))
    message: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(16), default="open", index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    acknowledged_at: Mapped[datetime | None] = mapped_column(DateTime)
    acknowledged_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), index=True)
    acknowledgement_comment: Mapped[str | None] = mapped_column(Text)

    patient: Mapped[Patient] = relationship(back_populates="review_notices")
    variant_key: Mapped[VariantKey] = relationship()
    run: Mapped[ReevaluationRun | None] = relationship()
    acknowledged_by: Mapped[User | None] = relationship()
    alerts: Mapped[list[Alert]] = relationship(back_populates="review_notice", order_by="Alert.id")


# ---------------------------------------------------------------------------
# Re-evaluation (F5)
# ---------------------------------------------------------------------------

RUN_COUNTERS: tuple[str, ...] = (
    "stored_keys_compared",
    "changed_variants",
    "carriers_affected",
    "patients_reevaluated",
    "records_examined",
    "alerts_created",
    "alerts_superseded",
    "alerts_confirmed",
    "notes_created",
    "review_notices",
    "notifications",
)


class ReevaluationRun(Model):
    """One run of the re-evaluation engine, with work and outcome counters."""

    __tablename__ = "reevaluation_runs"
    __table_args__ = (
        _one_of("trigger", C.TRIGGERS),
        _one_of("strategy", C.RUN_STRATEGIES),
        _one_of("status", C.RUN_STATUSES),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    trigger: Mapped[str] = mapped_column(String(32))
    strategy: Mapped[str] = mapped_column(String(16), default="change_driven")
    old_release_id: Mapped[int | None] = mapped_column(ForeignKey("kb_releases.id"), index=True)
    new_release_id: Mapped[int | None] = mapped_column(ForeignKey("kb_releases.id"), index=True)
    rule_change: Mapped[list[dict[str, Any]] | None] = mapped_column(JSON)
    started_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime)
    duration_ms: Mapped[float | None] = mapped_column(Float)
    status: Mapped[str] = mapped_column(String(16), default="running", index=True)
    error: Mapped[str | None] = mapped_column(Text)
    initiated_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), index=True)
    stored_keys_compared: Mapped[int] = mapped_column(Integer, default=0)
    changed_variants: Mapped[int] = mapped_column(Integer, default=0)
    carriers_affected: Mapped[int] = mapped_column(Integer, default=0)
    patients_reevaluated: Mapped[int] = mapped_column(Integer, default=0)
    records_examined: Mapped[int] = mapped_column(Integer, default=0)
    alerts_created: Mapped[int] = mapped_column(Integer, default=0)
    alerts_superseded: Mapped[int] = mapped_column(Integer, default=0)
    alerts_confirmed: Mapped[int] = mapped_column(Integer, default=0)
    notes_created: Mapped[int] = mapped_column(Integer, default=0)
    review_notices: Mapped[int] = mapped_column(Integer, default=0)
    notifications: Mapped[int] = mapped_column(Integer, default=0)
    summary: Mapped[dict[str, Any] | None] = mapped_column(JSON)

    old_release: Mapped[KbRelease | None] = relationship(foreign_keys=[old_release_id])
    new_release: Mapped[KbRelease | None] = relationship(foreign_keys=[new_release_id])
    initiated_by: Mapped[User | None] = relationship()
    changes: Mapped[list[VariantChange]] = relationship(back_populates="run", order_by="VariantChange.id")

    def __repr__(self) -> str:
        return f"<ReevaluationRun {self.id} {self.trigger}/{self.strategy} {self.status}>"


class VariantChange(Model):
    """A stored variant key whose classification or mechanism changed in a run."""

    __tablename__ = "variant_changes"
    __table_args__ = (
        _one_of("category", C.CHANGE_CATEGORIES),
        _one_of("old_classification", C.CLASSES),
        _one_of("new_classification", C.CLASSES),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("reevaluation_runs.id"), index=True)
    variant_key_id: Mapped[int] = mapped_column(ForeignKey("variant_keys.id"), index=True)
    gene: Mapped[str | None] = mapped_column(String(16))
    old_classification: Mapped[str] = mapped_column(String(4))
    new_classification: Mapped[str] = mapped_column(String(4))
    old_mechanism: Mapped[str | None] = mapped_column(String(16))
    new_mechanism: Mapped[str | None] = mapped_column(String(16))
    category: Mapped[str] = mapped_column(String(32))
    carrier_count: Mapped[int] = mapped_column(Integer, default=0)

    run: Mapped[ReevaluationRun] = relationship(back_populates="changes")
    variant_key: Mapped[VariantKey] = relationship()
