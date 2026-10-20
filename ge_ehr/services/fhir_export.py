"""F4 — FHIR R4 Bundle export using the HL7 Genomics Reporting variant profiles (SPEC §9).

Owned by the FHIR work package. Interface stub created by the Foundation work package; the
function bodies are implemented by the FHIR package.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from ..models import Patient

logger = logging.getLogger(__name__)


def build_patient_bundle(patient: Patient, *, base_url: str | None = None) -> dict[str, Any]:
    """Build the JSON-ready FHIR collection Bundle for ``patient``."""
    raise NotImplementedError


def validate_bundle(bundle: dict[str, Any]) -> None:
    """Validate ``bundle`` with the ``fhir.resources.R4B`` models; raise ValueError if invalid."""
    raise NotImplementedError
