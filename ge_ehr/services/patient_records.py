"""F1 — patient, diagnosis, medication and VCF-ingest service (SPEC §6.4).

Every change to a patient record goes through this module, which validates the values (raising
:class:`ValueError` with a message suitable for showing next to a form field), writes an audit
row and — for changes that can alter decision support — re-runs F2/F3 for that patient only
(paper §3.1) with :func:`ge_ehr.services.alerting.evaluate_patient_now`:

* diagnosis change → trigger ``diagnosis_change``, superseded reason ``diagnosis_changed``;
* medication added or stopped → trigger ``medication_change``, reason ``therapy_changed``;
* accepted VCF upload → trigger ``vcf_upload``.

Alerts superseded by therapy or diagnosis changes never raise review notices (SPEC §6.4).

**VCF ingest.** A rejected file (:class:`~ge_ehr.services.vcf_parser.VcfError`, including an
oversized one) is stored as a ``rejected`` :class:`~ge_ehr.models.GenomicUpload` without variants
and audited, then the error is re-raised; the caller commits so the rejection is kept. An
accepted file is stored with its counters; its panel variants are merged into the patient's
:class:`~ge_ehr.models.PatientVariant` rows (the union over all uploads: a variant reported again
takes the zygosity and ``upload_id`` of the latest upload, variants missing from a later upload
are kept). New variants are classified under the current release (SPEC §7.1 bookkeeping):
``current_classification = reported_classification`` = the class under that release and
``classification_release_id`` = that release, or ``NC`` / NULL while no release is current.

Services flush but never commit; the caller commits. Owned by the Reeval work package.
"""

from __future__ import annotations

import hashlib
import logging
import re
from collections.abc import Mapping, Sequence
from datetime import date, datetime
from itertools import batched
from typing import Any

from flask import current_app
from sqlalchemy import select
from sqlalchemy.orm import InstrumentedAttribute

from .. import constants as C
from ..constants import suggest_drug_class
from ..extensions import db
from ..models import (
    Diagnosis,
    GenomicUpload,
    KbRelease,
    Medication,
    Patient,
    PatientVariant,
    User,
    VariantKey,
    utc_today,
    utcnow,
)
from ..security import audit
from . import alerting, classification, vcf_parser
from .alerting import ReconcileResult
from .vcf_parser import ParsedVariant, VcfError

__all__ = [
    "PATIENT_FIELDS",
    "add_medication",
    "create_patient",
    "ingest_vcf",
    "stop_medication",
    "suggest_drug_class",
    "update_diagnosis",
    "update_patient",
]

logger = logging.getLogger(__name__)

#: Demographic fields :func:`update_patient` accepts.
PATIENT_FIELDS: tuple[str, ...] = (
    "mrn",
    "given_name",
    "family_name",
    "sex",
    "birth_date",
    "family_id",
    "external_id",
    "notes",
)
NOTES_MAX_LENGTH = 10_000
EARLIEST_BIRTH_DATE = date(1900, 1, 1)
# Age at diagnosis above ~125 years is certainly a data-entry error.
MAX_AGE_AT_DIAGNOSIS_MONTHS = 1_500
DEFAULT_UPLOAD_FILENAME = "upload.vcf"
DEFAULT_PATIENT_VCF_MAX_BYTES = 20 * 1024 * 1024

# Medical record numbers: letters and digits, optionally separated by . _ / - (e.g. DEMO-0001).
_MRN_PATTERN = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9._/-]*[A-Za-z0-9])?")
_CONTROL_CHARACTERS = re.compile(r"[\x00-\x1f\x7f]")
_PATH_SEPARATORS = re.compile(r"[\\/]")
# Free-text fields whose content is not copied into the audit log (only that they changed).
_FREE_TEXT_FIELDS = frozenset({"notes"})


# ---------------------------------------------------------------------------
# Field validation (messages are shown next to form fields)
# ---------------------------------------------------------------------------


def _max_length(column: InstrumentedAttribute[Any]) -> int:
    """Declared length of a String column (the single source of truth for field limits)."""
    return column.property.columns[0].type.length


def _text(value: Any, label: str, *, max_length: int, required: bool) -> str | None:
    """A stripped single-line text value; blank → None (or an error when required)."""
    if value is None:
        text = ""
    elif isinstance(value, str):
        text = value.strip()
    else:
        raise ValueError(f"{label} must be text.")
    if not text:
        if required:
            raise ValueError(f"{label} is required.")
        return None
    if _CONTROL_CHARACTERS.search(text):
        raise ValueError(f"{label} must not contain control characters such as tabs or line breaks.")
    if len(text) > max_length:
        raise ValueError(f"{label} must be at most {max_length} characters (got {len(text)}).")
    return text


def _notes(value: Any, label: str = "Notes") -> str | None:
    """Optional multi-line free text."""
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(f"{label} must be text.")  # noqa: TRY004 — form errors are ValueErrors (§6.4)
    text = value.strip()
    if len(text) > NOTES_MAX_LENGTH:
        raise ValueError(f"{label} must be at most {NOTES_MAX_LENGTH:,} characters (got {len(text):,}).")
    return text or None


def _choice(value: Any, allowed: Sequence[str], labels: Mapping[str, str], label: str) -> str:
    """One of ``allowed`` (case-insensitive)."""
    choice = value.strip().lower() if isinstance(value, str) else value
    if choice not in allowed:
        options = ", ".join(f"{labels[option]} ({option})" for option in allowed)
        raise ValueError(f"{label} must be one of: {options}; got {value!r}.")
    return choice


def _date(value: Any, label: str) -> date | None:
    """A date, an ISO ``YYYY-MM-DD`` string or None/blank."""
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        try:
            return date.fromisoformat(value.strip())
        except ValueError:
            pass
    raise ValueError(f"{label} must be a date in YYYY-MM-DD format, got {value!r}.")


def _not_in_future(value: date | None, label: str) -> date | None:
    if value is not None and value > utc_today():
        raise ValueError(f"{label} ({value.isoformat()}) cannot be in the future.")
    return value


def _birth_date(value: Any) -> date:
    birth_date = _date(value, "Birth date")
    if birth_date is None:
        raise ValueError("Birth date is required.")
    _not_in_future(birth_date, "Birth date")
    if birth_date < EARLIEST_BIRTH_DATE:
        raise ValueError(f"Birth date must be on or after {EARLIEST_BIRTH_DATE.isoformat()}.")
    return birth_date


def _months(value: Any, label: str) -> int | None:
    """A whole number of months (int or digit string) or None/blank."""
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    if isinstance(value, bool):
        raise ValueError(f"{label} must be a whole number of months.")  # noqa: TRY004 — form error
    if isinstance(value, str):
        text = value.strip()
        if not text.isdigit():
            raise ValueError(f"{label} must be a whole number of months, got {value!r}.")
        value = int(text)
    if not isinstance(value, int):
        raise ValueError(f"{label} must be a whole number of months, got {value!r}.")  # noqa: TRY004
    if value < 0:
        raise ValueError(f"{label} cannot be negative.")
    if value > MAX_AGE_AT_DIAGNOSIS_MONTHS:
        raise ValueError(f"{label} must be at most {MAX_AGE_AT_DIAGNOSIS_MONTHS:,} months.")
    return value


def _age_in_months(birth_date: date, on: date) -> int:
    """Completed months between ``birth_date`` and ``on``."""
    return (on.year - birth_date.year) * 12 + on.month - birth_date.month - (on.day < birth_date.day)


def _patient_values(fields: Mapping[str, Any]) -> dict[str, Any]:
    """Validate the given demographic fields (only those present in ``fields``)."""
    validators = {
        "mrn": _mrn,
        "given_name": lambda value: _text(
            value, "Given name", max_length=_max_length(Patient.given_name), required=True
        ),
        "family_name": lambda value: _text(
            value, "Family name", max_length=_max_length(Patient.family_name), required=True
        ),
        "sex": lambda value: _choice(value, C.SEXES, C.SEX_LABELS, "Sex"),
        "birth_date": _birth_date,
        "family_id": lambda value: _text(value, "Family ID", max_length=_max_length(Patient.family_id), required=False),
        "external_id": lambda value: _text(
            value, "External ID", max_length=_max_length(Patient.external_id), required=False
        ),
        "notes": _notes,
    }
    return {name: validators[name](value) for name, value in fields.items()}


def _mrn(value: Any) -> str:
    mrn = _text(value, "MRN", max_length=_max_length(Patient.mrn), required=True)
    if not _MRN_PATTERN.fullmatch(mrn):
        raise ValueError(
            "MRN may contain only letters, digits and the separators . _ / - (between letters or digits), "
            f"e.g. DEMO-0001; got {mrn!r}."
        )
    return mrn


def _check_mrn_unique(mrn: str, patient: Patient | None = None) -> None:
    statement = select(Patient.id).where(Patient.mrn == mrn)
    if patient is not None:
        statement = statement.where(Patient.id != patient.id)
    if db.session.scalar(statement) is not None:
        raise ValueError(f"A patient with MRN {mrn} already exists.")


def _diagnosis_values(
    birth_date: date,
    *,
    diabetes_type: Any,
    age_at_diagnosis_months: Any,
    diagnosed_on: Any,
    notes: Any,
) -> dict[str, Any]:
    """Validate a complete diagnosis record against the patient's birth date."""
    kind = _choice(diabetes_type, C.DIABETES_TYPES, C.DIABETES_TYPE_LABELS, "Diabetes type")
    age = _months(age_at_diagnosis_months, "Age at diagnosis")
    on = _not_in_future(_date(diagnosed_on, "Diagnosis date"), "Diagnosis date")
    if kind == "none" and (age is not None or on is not None):
        raise ValueError("Age at diagnosis and diagnosis date must be empty when the patient has no diabetes.")
    current_age = _age_in_months(birth_date, utc_today())
    if age is not None and age > current_age:
        raise ValueError(
            f"Age at diagnosis ({age} months) cannot exceed the patient's current age ({current_age} months)."
        )
    if on is not None and on < birth_date:
        raise ValueError(
            f"Diagnosis date ({on.isoformat()}) cannot be before the birth date ({birth_date.isoformat()})."
        )
    return {
        "diabetes_type": kind,
        "age_at_diagnosis_months": age,
        "diagnosed_on": on,
        "notes": _notes(notes, "Diagnosis notes"),
    }


def _apply(target: Any, values: Mapping[str, Any]) -> dict[str, Any]:
    """Set the attributes that differ; return the audit-ready changes ``{field: {old, new}}``."""
    changes: dict[str, Any] = {}
    for name, value in values.items():
        old = getattr(target, name)
        if old == value:
            continue
        setattr(target, name, value)
        changes[name] = {"changed": True} if name in _FREE_TEXT_FIELDS else {"old": old, "new": value}
    return changes


def _outcome(result: ReconcileResult) -> dict[str, int]:
    """Counts of a reconciliation for audit details."""
    return {
        "alerts_created": sum(alert.kind == "alert" for alert in result.created),
        "notes_created": sum(alert.kind == "note" for alert in result.created),
        "superseded": len(result.superseded),
        "confirmed": len(result.confirmed),
    }


# ---------------------------------------------------------------------------
# Patients and diagnoses
# ---------------------------------------------------------------------------


def create_patient(
    *,
    mrn: str,
    given_name: str,
    family_name: str,
    sex: str,
    birth_date: date,
    family_id: str | None = None,
    notes: str | None = None,
    diabetes_type: str = "unknown",
    age_at_diagnosis_months: int | None = None,
    diagnosed_on: date | None = None,
    external_id: str | None = None,
    user: User | None = None,
) -> Patient:
    """Create a (synthetic) patient together with its diagnosis record; audits ``patient_created``.

    A new patient has no variants, so no evaluation runs. Dates may be given as ``date`` objects
    or ISO strings, the age at diagnosis as an int or digit string. Raises :class:`ValueError`
    with a form-ready message for an invalid value or a duplicate MRN.
    """
    values = _patient_values(
        {
            "mrn": mrn,
            "given_name": given_name,
            "family_name": family_name,
            "sex": sex,
            "birth_date": birth_date,
            "family_id": family_id,
            "external_id": external_id,
            "notes": notes,
        }
    )
    diagnosis = _diagnosis_values(
        values["birth_date"],
        diabetes_type=diabetes_type,
        age_at_diagnosis_months=age_at_diagnosis_months,
        diagnosed_on=diagnosed_on,
        notes=None,
    )
    _check_mrn_unique(values["mrn"])
    patient = Patient(**values, is_synthetic=True)
    patient.diagnosis = Diagnosis(**diagnosis)
    db.session.add(patient)
    db.session.flush()
    audit(
        "patient_created",
        "patient",
        patient.id,
        detail={
            "mrn": patient.mrn,
            "family_id": patient.family_id,
            "diabetes_type": diagnosis["diabetes_type"],
            "age_at_diagnosis_months": diagnosis["age_at_diagnosis_months"],
        },
        user=user,
    )
    logger.info("Created patient %s (id %d)", patient.mrn, patient.id)
    return patient


def update_patient(patient: Patient, *, user: User | None = None, **fields: Any) -> Patient:
    """Update demographic fields of ``patient`` (any of :data:`PATIENT_FIELDS`).

    Only the given fields are validated and changed; ``patient_updated`` is audited with the
    changed fields (none changed → no audit row). Demographics do not affect decision support,
    so no evaluation runs. Raises :class:`ValueError` for an unknown field, an invalid value, a
    duplicate MRN, or a birth date that the recorded age at diagnosis would exceed.
    """
    unknown = sorted(set(fields) - set(PATIENT_FIELDS))
    if unknown:
        raise ValueError(f"Unknown patient field(s): {', '.join(unknown)}; allowed: {', '.join(PATIENT_FIELDS)}.")
    values = _patient_values(fields)
    if "mrn" in values:
        _check_mrn_unique(values["mrn"], patient)
    if "birth_date" in values and patient.diagnosis is not None:
        diagnosis = patient.diagnosis
        _diagnosis_values(
            values["birth_date"],
            diabetes_type=diagnosis.diabetes_type,
            age_at_diagnosis_months=diagnosis.age_at_diagnosis_months,
            diagnosed_on=diagnosis.diagnosed_on,
            notes=diagnosis.notes,
        )
    changes = _apply(patient, values)
    if changes:
        db.session.flush()
        audit("patient_updated", "patient", patient.id, detail={"mrn": patient.mrn, "changes": changes}, user=user)
    return patient


def update_diagnosis(
    patient: Patient,
    *,
    diabetes_type: str,
    age_at_diagnosis_months: int | None,
    diagnosed_on: date | None = None,
    notes: str | None = None,
    user: User | None = None,
) -> ReconcileResult:
    """Replace the diagnosis record, then re-evaluate the patient.

    Every field is replaced (a form submits all of them): pass the current value to keep one.
    Re-evaluation uses trigger ``diagnosis_change`` and superseded reason ``diagnosis_changed``
    (no review notices). ``diagnosis_updated`` is audited when a value changed. Raises
    :class:`ValueError` for an invalid value; nothing is changed in that case.
    """
    values = _diagnosis_values(
        patient.birth_date,
        diabetes_type=diabetes_type,
        age_at_diagnosis_months=age_at_diagnosis_months,
        diagnosed_on=diagnosed_on,
        notes=notes,
    )
    diagnosis = patient.diagnosis
    if diagnosis is None:
        diagnosis = patient.diagnosis = Diagnosis()
    changes = _apply(diagnosis, values)
    db.session.flush()
    result = alerting.evaluate_patient_now(
        patient, trigger="diagnosis_change", user=user, superseded_reason="diagnosis_changed"
    )
    if changes:
        audit(
            "diagnosis_updated",
            "patient",
            patient.id,
            detail={"mrn": patient.mrn, "changes": changes, "evaluation": _outcome(result)},
            user=user,
        )
    return result


# ---------------------------------------------------------------------------
# Medications
# ---------------------------------------------------------------------------


def add_medication(
    patient: Patient,
    *,
    drug_name: str,
    drug_class: str,
    dose: str | None = None,
    start_date: date | None = None,
    user: User | None = None,
) -> tuple[Medication, ReconcileResult]:
    """Record a current diabetes medication, then re-evaluate (trigger ``medication_change``).

    ``drug_class`` is one of :data:`~ge_ehr.constants.DRUG_CLASSES` (forms can pre-fill it with
    :func:`suggest_drug_class`). Superseded alerts get reason ``therapy_changed`` and raise no
    review notice. Audits ``medication_added``. Raises :class:`ValueError` for an invalid value
    (e.g. a start date in the future or before the birth date).
    """
    name = _text(drug_name, "Drug name", max_length=_max_length(Medication.drug_name), required=True)
    therapy = _choice(drug_class, C.DRUG_CLASSES, C.DRUG_CLASS_LABELS, "Drug class")
    dose_text = _text(dose, "Dose", max_length=_max_length(Medication.dose), required=False)
    start = _not_in_future(_date(start_date, "Start date"), "Start date")
    if start is not None and start < patient.birth_date:
        raise ValueError(
            f"Start date ({start.isoformat()}) cannot be before the birth date ({patient.birth_date.isoformat()})."
        )
    medication = Medication(
        patient=patient, drug_name=name, drug_class=therapy, dose=dose_text, start_date=start, created_by=user
    )
    db.session.add(medication)
    db.session.flush()
    result = alerting.evaluate_patient_now(
        patient, trigger="medication_change", user=user, superseded_reason="therapy_changed"
    )
    audit(
        "medication_added",
        "medication",
        medication.id,
        detail={
            "patient_id": patient.id,
            "mrn": patient.mrn,
            "drug_name": name,
            "drug_class": therapy,
            "dose": dose_text,
            "start_date": start,
            "evaluation": _outcome(result),
        },
        user=user,
    )
    return medication, result


def stop_medication(
    medication: Medication, *, end_date: date | None = None, user: User | None = None
) -> ReconcileResult:
    """Stop a medication (``end_date`` defaults to today, UTC), then re-evaluate.

    Trigger ``medication_change``, superseded reason ``therapy_changed`` (no review notices).
    Audits ``medication_stopped``. Raises :class:`ValueError` when the medication was already
    stopped or the end date is in the future or before the start date.
    """
    if medication.end_date is not None:
        raise ValueError(f"{medication.drug_name} was already stopped on {medication.end_date.isoformat()}.")
    end = _not_in_future(_date(end_date, "End date"), "End date") or utc_today()
    if medication.start_date is not None and end < medication.start_date:
        raise ValueError(
            f"End date ({end.isoformat()}) cannot be before the start date ({medication.start_date.isoformat()})."
        )
    medication.end_date = end
    db.session.flush()
    patient = medication.patient
    result = alerting.evaluate_patient_now(
        patient, trigger="medication_change", user=user, superseded_reason="therapy_changed"
    )
    audit(
        "medication_stopped",
        "medication",
        medication.id,
        detail={
            "patient_id": patient.id,
            "mrn": patient.mrn,
            "drug_name": medication.drug_name,
            "drug_class": medication.drug_class,
            "end_date": end,
            "evaluation": _outcome(result),
        },
        user=user,
    )
    return result


# ---------------------------------------------------------------------------
# VCF ingest
# ---------------------------------------------------------------------------


def _upload_filename(filename: str | None) -> str:
    """The file's base name (client paths and control characters removed), for display only."""
    name = _PATH_SEPARATORS.split(filename or "")[-1]
    name = _CONTROL_CHARACTERS.sub("", name).strip()
    return name[: _max_length(GenomicUpload.filename)] or DEFAULT_UPLOAD_FILENAME


def _megabytes(size: int) -> str:
    return f"{size / (1024 * 1024):.1f} MB"


def _check_upload_size(size: int) -> None:
    limit = int(current_app.config.get("PATIENT_VCF_MAX_BYTES", DEFAULT_PATIENT_VCF_MAX_BYTES))
    if size > limit:
        raise VcfError(
            f"The file is {_megabytes(size)}; patient VCFs may be at most {_megabytes(limit)}. "
            "Upload a VCF restricted to the gene panel (or compress it with bgzip)."
        )


def ingest_vcf(
    patient: Patient, data: bytes, filename: str, *, user: User | None = None
) -> tuple[GenomicUpload, ReconcileResult | None]:
    """Parse and store a patient VCF, classify new variants and re-evaluate the patient.

    The size limit is the app's ``PATIENT_VCF_MAX_BYTES`` (20 MB). On
    :class:`~ge_ehr.services.vcf_parser.VcfError` (oversized or malformed file) a ``rejected``
    upload with the error text is stored and audited (``vcf_upload_rejected``), then the error is
    re-raised — the caller commits so the rejection is kept. On success the ``accepted`` upload
    (sha256, size, sample and record counters) is stored, the panel variants are merged into the
    patient's variants (see the module docstring), ``vcf_upload_accepted`` is audited (including
    the parser's warnings) and the patient is evaluated with trigger ``vcf_upload``.

    Returns the upload and the reconciliation result, or None as the result when the file holds
    no panel variant (nothing to evaluate).
    """
    name = _upload_filename(filename)
    upload = GenomicUpload(
        patient=patient,
        filename=name,
        sha256=hashlib.sha256(data).hexdigest(),
        size_bytes=len(data),
        uploaded_by=user,
        uploaded_at=utcnow(),
    )
    try:
        _check_upload_size(len(data))
        parsed = vcf_parser.parse_patient_vcf(data, name)
    except VcfError as exc:
        upload.status = "rejected"
        upload.error = str(exc)
        db.session.add(upload)
        db.session.flush()
        audit(
            "vcf_upload_rejected",
            "genomic_upload",
            upload.id,
            detail={
                "patient_id": patient.id,
                "mrn": patient.mrn,
                "filename": name,
                "sha256": upload.sha256,
                "size_bytes": upload.size_bytes,
                "error": exc.message,
                "line": exc.line,
            },
            user=user,
        )
        logger.info("Rejected VCF %r for patient %s: %s", name, patient.mrn, exc)
        raise

    upload.status = "accepted"
    upload.sample_id = parsed.sample_id
    upload.total_records = parsed.total_records
    upload.panel_variants = parsed.panel_variants
    upload.off_panel_skipped = parsed.off_panel_skipped
    upload.non_pass_skipped = parsed.non_pass_skipped
    upload.ref_calls_skipped = parsed.ref_calls_skipped
    db.session.add(upload)
    db.session.flush()
    added, updated = _merge_variants(patient, upload, parsed.variants)
    audit(
        "vcf_upload_accepted",
        "genomic_upload",
        upload.id,
        detail={
            "patient_id": patient.id,
            "mrn": patient.mrn,
            "filename": name,
            "sha256": upload.sha256,
            "size_bytes": upload.size_bytes,
            "sample_id": upload.sample_id,
            "total_records": upload.total_records,
            "panel_variants": upload.panel_variants,
            "off_panel_skipped": upload.off_panel_skipped,
            "non_pass_skipped": upload.non_pass_skipped,
            "ref_calls_skipped": upload.ref_calls_skipped,
            "new_variants": len(added),
            "updated_variants": len(updated),
            "warnings": parsed.warnings,
        },
        user=user,
    )
    logger.info(
        "Accepted VCF %r for patient %s: %d panel variant(s), %d new",
        name,
        patient.mrn,
        upload.panel_variants,
        len(added),
    )
    if not parsed.variants:
        return upload, None
    return upload, alerting.evaluate_patient_now(patient, trigger="vcf_upload", user=user)


def _merge_variants(
    patient: Patient, upload: GenomicUpload, variants: Sequence[ParsedVariant]
) -> tuple[list[PatientVariant], list[PatientVariant]]:
    """Upsert the patient's variants from one upload; returns (new, updated) PatientVariants.

    New variants get their first classification under the current release (SPEC §7.1
    bookkeeping): ``current_classification = reported_classification`` and
    ``classification_release_id`` = that release (``NC`` / NULL while no release is current).
    """
    if not variants:
        return [], []
    keys = {
        (parsed.chrom, parsed.pos, parsed.ref, parsed.alt): key
        for parsed, key in zip(variants, _variant_keys(variants), strict=True)
    }
    statement = select(PatientVariant).where(PatientVariant.patient_id == patient.id)
    existing = {variant.variant_key_id: variant for variant in db.session.scalars(statement)}
    release = KbRelease.current()
    known = classification.classify_keys(release, {key.id for key in keys.values()} - existing.keys())
    added: list[PatientVariant] = []
    updated: list[PatientVariant] = []
    for parsed in variants:
        key = keys[(parsed.chrom, parsed.pos, parsed.ref, parsed.alt)]
        variant = existing.get(key.id)
        if variant is None:
            cls = known[key.id].classification
            variant = PatientVariant(
                patient=patient,
                variant_key=key,
                upload=upload,
                zygosity=parsed.zygosity,
                gene=parsed.gene,
                current_classification=cls,
                reported_classification=cls,
                classification_release=release,
            )
            db.session.add(variant)
            existing[key.id] = variant
            added.append(variant)
        else:
            # Latest upload wins: zygosity and upload follow the most recent report of the variant.
            variant.upload = upload
            variant.zygosity = parsed.zygosity
            variant.gene = variant.gene or parsed.gene
            updated.append(variant)
    db.session.flush()
    return added, updated


def _variant_keys(variants: Sequence[ParsedVariant]) -> list[VariantKey]:
    """Bulk get-or-create of the VariantKeys of an upload, in the order of ``variants``.

    Existing keys are found with one query per chunk of positions; missing ones are created with
    a single flush. An existing key without a gene gets the panel gene from the upload.
    """
    identities = [VariantKey.normalize(v.chrom, v.pos, v.ref, v.alt) for v in variants]
    genes = {identity: variant.gene for identity, variant in zip(identities, variants, strict=True)}
    chroms = sorted({identity[0] for identity in genes})
    found: dict[tuple[str, int, str, str], VariantKey] = {}
    for chunk in batched(sorted({identity[1] for identity in genes}), classification.IN_CLAUSE_CHUNK):
        statement = select(VariantKey).where(VariantKey.chrom.in_(chroms), VariantKey.pos.in_(chunk))
        for key in db.session.scalars(statement):
            identity = (key.chrom, key.pos, key.ref, key.alt)
            if identity in genes:
                found[identity] = key
                key.gene = key.gene or genes[identity]
    missing = [
        VariantKey(chrom=chrom, pos=pos, ref=ref, alt=alt, gene=gene)
        for (chrom, pos, ref, alt), gene in genes.items()
        if (chrom, pos, ref, alt) not in found
    ]
    if missing:
        db.session.add_all(missing)
        db.session.flush()
        found.update({(key.chrom, key.pos, key.ref, key.alt): key for key in missing})
    return [found[identity] for identity in identities]
