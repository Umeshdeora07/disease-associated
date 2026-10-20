"""F1 — patient, diagnosis, medication and VCF-ingest service (SPEC §6.4).

Owned by the Reeval work package. Interface stub created by the Foundation work package; the
function bodies are implemented by the Reeval package.
"""

from __future__ import annotations

import logging
from datetime import date
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from ..models import GenomicUpload, Medication, Patient, User
    from .alerting import ReconcileResult

logger = logging.getLogger(__name__)


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
    """Create a patient together with its diagnosis record."""
    raise NotImplementedError


def update_patient(patient: Patient, *, user: User | None = None, **fields: Any) -> Patient:
    """Update demographic fields of ``patient``."""
    raise NotImplementedError


def update_diagnosis(
    patient: Patient,
    *,
    diabetes_type: str,
    age_at_diagnosis_months: int | None,
    diagnosed_on: date | None = None,
    notes: str | None = None,
    user: User | None = None,
) -> ReconcileResult:
    """Update the diagnosis, then re-evaluate (trigger ``diagnosis_change``)."""
    raise NotImplementedError


def add_medication(
    patient: Patient,
    *,
    drug_name: str,
    drug_class: str,
    dose: str | None = None,
    start_date: date | None = None,
    user: User | None = None,
) -> tuple[Medication, ReconcileResult]:
    """Record a diabetes medication, then re-evaluate (trigger ``medication_change``)."""
    raise NotImplementedError


def stop_medication(medication: Medication, *, end_date: date | None = None, user: User | None = None) -> ReconcileResult:
    """Stop a medication, then re-evaluate (trigger ``medication_change``)."""
    raise NotImplementedError


def ingest_vcf(
    patient: Patient, data: bytes, filename: str, *, user: User | None = None
) -> tuple[GenomicUpload, ReconcileResult | None]:
    """Parse and store a patient VCF, classify new variants and re-evaluate.

    On :class:`~ge_ehr.services.vcf_parser.VcfError` a rejected upload is stored, audited
    (``vcf_upload_rejected``) and the error re-raised.
    """
    raise NotImplementedError
