"""Test data factories shared by every work package's tests.

Factories add objects to ``db.session`` and ``flush()`` (so ids are assigned) but never commit:
the test decides when to commit. Commit before issuing requests with the test client, because
a request that fails may roll the shared session back.

Usage: ``from tests.factories import make_patient, add_patient_variant``.
"""

from __future__ import annotations

import itertools
from collections.abc import Iterable, Mapping
from datetime import date, datetime
from typing import Any

from sqlalchemy import func, select, update

from ge_ehr.constants import gene_for_position, suggest_drug_class
from ge_ehr.extensions import db
from ge_ehr.models import (
    Alert,
    AlertEvent,
    Diagnosis,
    KbRelease,
    KnownVariant,
    Medication,
    Patient,
    PatientVariant,
    ReevaluationRun,
    ReviewNotice,
    Rule,
    RuleVersion,
    User,
    VariantKey,
    utcnow,
)

TEST_PASSWORD = "Test-Pass-123!"

_sequence = itertools.count(1)


def _next() -> int:
    return next(_sequence)


# ---------------------------------------------------------------------------
# Users
# ---------------------------------------------------------------------------


def make_user(
    username: str | None = None,
    *,
    role: str = "clinician",
    full_name: str | None = None,
    password: str = TEST_PASSWORD,
    email: str | None = None,
    active: bool = True,
    must_change_password: bool = False,
) -> User:
    """A user with password :data:`TEST_PASSWORD` (unique username when none is given)."""
    username = username or f"user{_next()}"
    user = User(
        username=username,
        full_name=full_name or username.replace(".", " ").title(),
        email=email,
        role=role,
        active=active,
        must_change_password=must_change_password,
    )
    user.set_password(password)
    db.session.add(user)
    db.session.flush()
    return user


# ---------------------------------------------------------------------------
# Patients
# ---------------------------------------------------------------------------


def make_patient(*, diagnosis: Mapping[str, Any] | str | None = None, **overrides: Any) -> Patient:
    """A synthetic patient with a diagnosis row.

    ``diagnosis`` is a diabetes type (``"type1"``) or a mapping of :class:`Diagnosis` fields,
    e.g. ``{"diabetes_type": "neonatal", "age_at_diagnosis_months": 2}``; the default is an
    ``unknown`` diagnosis. Other keyword arguments override :class:`Patient` fields.
    """
    number = _next()
    fields: dict[str, Any] = {
        "mrn": f"TEST-{number:05d}",
        "given_name": "Test",
        "family_name": f"Patient{number}",
        "sex": "female",
        "birth_date": date(1990, 1, 15),
        "is_synthetic": True,
    }
    fields.update(overrides)
    diagnosis_fields: dict[str, Any] = {"diabetes_type": "unknown"}
    if isinstance(diagnosis, str):
        diagnosis_fields["diabetes_type"] = diagnosis
    elif diagnosis:
        diagnosis_fields.update(diagnosis)
    patient = Patient(**fields)
    patient.diagnosis = Diagnosis(**diagnosis_fields)
    db.session.add(patient)
    db.session.flush()
    return patient


def add_medication(
    patient: Patient,
    drug_name: str = "Insulin glargine",
    drug_class: str | None = None,
    *,
    dose: str | None = None,
    start_date: date | None = None,
    end_date: date | None = None,
    created_by: User | None = None,
) -> Medication:
    """A medication; ``drug_class`` defaults to :func:`suggest_drug_class` of the name."""
    medication = Medication(
        patient=patient,
        drug_name=drug_name,
        drug_class=drug_class or suggest_drug_class(drug_name),
        dose=dose,
        start_date=start_date,
        end_date=end_date,
        created_by=created_by,
    )
    db.session.add(medication)
    db.session.flush()
    return medication


# ---------------------------------------------------------------------------
# Knowledge base
# ---------------------------------------------------------------------------


def make_release(
    label: str | None = None,
    current: bool = False,
    sequence: int | None = None,
    release_date: date | None = None,
    **overrides: Any,
) -> KbRelease:
    """A synthetic KB release. ``current=True`` also clears the flag on other releases and
    assigns the next activation sequence unless one is given."""
    if current:
        db.session.execute(update(KbRelease).where(KbRelease.is_current.is_(True)).values(is_current=False))
        if sequence is None:
            sequence = (db.session.scalar(select(func.max(KbRelease.sequence))) or 0) + 1
    fields: dict[str, Any] = {
        "label": label or f"Test release {_next()}",
        "source": "synthetic",
        "is_synthetic": True,
        "release_date": release_date or date(2025, 1, 1),
        "is_current": current,
        "sequence": sequence,
        "activated_at": utcnow() if current else None,
    }
    fields.update(overrides)
    release = KbRelease(**fields)
    db.session.add(release)
    db.session.flush()
    return release


def add_known_variant(
    release: KbRelease,
    chrom: str,
    pos: int,
    ref: str,
    alt: str,
    classification: str,
    gene: str | None = None,
    mechanism: str = "unknown",
    **kw: Any,
) -> KnownVariant:
    """A classified variant in ``release`` (extra keyword arguments are KnownVariant fields)."""
    key = VariantKey.get_or_create(chrom, pos, ref, alt, gene=gene)
    known = KnownVariant(
        release=release,
        variant_key=key,
        gene=gene or key.gene,
        classification=classification,
        mechanism=mechanism,
        **kw,
    )
    db.session.add(known)
    db.session.flush()
    return known


def add_patient_variant(
    patient: Patient,
    chrom: str,
    pos: int,
    ref: str,
    alt: str,
    zygosity: str = "heterozygous",
    classification: str = "NC",
    release: KbRelease | None = None,
    gene: str | None = None,
) -> PatientVariant:
    """A variant carried by ``patient``, classified as ``classification`` under ``release``."""
    key = VariantKey.get_or_create(chrom, pos, ref, alt, gene=gene)
    variant = PatientVariant(
        patient=patient,
        variant_key=key,
        zygosity=zygosity,
        gene=gene or key.gene or gene_for_position(chrom, pos),
        current_classification=classification,
        reported_classification=classification,
        classification_release=release,
    )
    db.session.add(variant)
    db.session.flush()
    return variant


# ---------------------------------------------------------------------------
# Rules, alerts, runs
# ---------------------------------------------------------------------------


def rule_definition(
    code: str = "D2",
    version: int = 1,
    *,
    genes: Iterable[str] = ("HNF1A",),
    classes: Iterable[str] = ("P", "LP"),
    mechanism: str | None = None,
    clinical: Mapping[str, Any] | None = None,
    severity: str = "high",
    kind: str = "treatment",
    title: str | None = None,
    recommendation: str = "Consider low-dose sulfonylurea as first-line treatment.",
    source: str = "Test source",
    change_note: str = "Initial version",
) -> dict[str, Any]:
    """A rule definition dict in the SPEC §6.1 format."""
    return {
        "code": code,
        "version": version,
        "kind": kind,
        "title": title or f"Rule {code}",
        "genetic": {"genes": list(genes), "classes": list(classes), "mechanism": mechanism},
        "clinical": dict(clinical) if clinical is not None else None,
        "severity": severity,
        "recommendation": recommendation,
        "source": source,
        "change_note": change_note,
    }


def make_rule_version(
    definition_dict: Mapping[str, Any],
    current: bool = True,
    *,
    rule_set_label: str = "Test rules",
    imported_by: User | None = None,
) -> RuleVersion:
    """Store a rule version (creating its Rule); ``current=True`` demotes other versions."""
    definition = dict(definition_dict)
    code = definition["code"]
    rule = db.session.execute(select(Rule).filter_by(code=code)).scalar_one_or_none()
    if rule is None:
        rule = Rule(code=code, title=definition["title"], description=definition.get("change_note"))
        db.session.add(rule)
        db.session.flush()
    if current:
        db.session.execute(update(RuleVersion).where(RuleVersion.rule_id == rule.id).values(is_current=False))
    version = RuleVersion(
        rule=rule,
        code=code,
        version=definition["version"],
        definition=definition,
        genes=list(definition["genetic"]["genes"]),
        severity=definition["severity"],
        title=definition["title"],
        recommendation=definition["recommendation"],
        source=definition.get("source"),
        kind=definition.get("kind", "treatment"),
        is_current=current,
        rule_set_label=rule_set_label,
        change_note=definition.get("change_note"),
        imported_by=imported_by,
    )
    db.session.add(version)
    db.session.flush()
    return version


def make_run(trigger: str = "manual", **overrides: Any) -> ReevaluationRun:
    """A re-evaluation run (status ``running`` unless overridden)."""
    run = ReevaluationRun(trigger=trigger, **overrides)
    db.session.add(run)
    db.session.flush()
    return run


def make_alert(
    patient_variant: PatientVariant,
    rule_version: RuleVersion,
    *,
    kind: str = "alert",
    release: KbRelease | None = None,
    trigger: str = "vcf_upload",
    created_at: datetime | None = None,
    **overrides: Any,
) -> Alert:
    """A current alert (or note) of ``rule_version`` for ``patient_variant`` plus its ``created`` event."""
    fields: dict[str, Any] = {
        "patient_id": patient_variant.patient_id,
        "patient_variant": patient_variant,
        "variant_key_id": patient_variant.variant_key_id,
        "rule_code": rule_version.code,
        "created_rule_version": rule_version,
        "confirmed_rule_version": rule_version,
        "kind": kind,
        "severity": rule_version.severity if kind == "alert" else None,
        "title": rule_version.title,
        "message": rule_version.recommendation,
        "source": rule_version.source,
        "classification": patient_variant.current_classification,
        "trigger": trigger,
        "created_release": release,
        "confirmed_release": release,
        "notify": kind == "alert" and rule_version.severity != "info",
        "created_at": created_at or utcnow(),
    }
    fields.update(overrides)
    alert = Alert(**fields)
    db.session.add(alert)
    db.session.flush()
    db.session.add(
        AlertEvent(
            alert=alert,
            event="created",
            release=release,
            rule_version=rule_version,
            classification=alert.classification,
            at=alert.created_at,
        )
    )
    db.session.flush()
    return alert


def make_review_notice(
    patient_variant: PatientVariant,
    *,
    old_classification: str = "LP",
    new_classification: str = "VUS",
    run: ReevaluationRun | None = None,
    alerts: Iterable[Alert] = (),
    **overrides: Any,
) -> ReviewNotice:
    """An open review notice for ``patient_variant`` linking ``alerts``."""
    notice = ReviewNotice(
        patient_id=patient_variant.patient_id,
        variant_key_id=patient_variant.variant_key_id,
        run=run,
        old_classification=old_classification,
        new_classification=new_classification,
        message=overrides.pop(
            "message", f"Variant downgraded from {old_classification} to {new_classification}; review alerts."
        ),
        **overrides,
    )
    notice.alerts.extend(alerts)
    db.session.add(notice)
    db.session.flush()
    return notice
