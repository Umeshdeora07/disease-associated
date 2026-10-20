"""F2 — ClinVar significance normalisation and classification lookup (SPEC §5.1, §5.2).

Owned by the KB work package. Interface stub created by the Foundation work package: the
dataclass and constant are final; the function bodies are implemented by the KB package.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..models import KbRelease, Patient
    from .rules import ClassifiedVariant

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class KnownClass:
    """Classification of one variant key under one release."""

    classification: str
    mechanism: str
    known_variant_id: int | None
    is_expert_panel: bool
    review_status: str | None
    review_stars: int
    condition: str | None


# Keys absent from a release are "not classified" — never VUS.
NOT_CLASSIFIED = KnownClass("NC", "unknown", None, False, None, 0, None)


def normalize_clinvar_significance(clnsig: str | None) -> str:
    """Map a raw CLNSIG value to one of :data:`ge_ehr.constants.CLASSES` (SPEC §5.1)."""
    raise NotImplementedError


def review_stars(clnrevstat: str | None) -> int:
    """ClinVar review stars (0–4) for a raw CLNREVSTAT value."""
    raise NotImplementedError


def is_expert_panel_status(clnrevstat: str | None) -> bool:
    """True for ``reviewed_by_expert_panel`` and ``practice_guideline``."""
    raise NotImplementedError


def infer_mechanism(gene: str, condition: str | None) -> str:
    """Infer the variant mechanism (KCNJ11/ABCC8 only; other genes → ``unknown``)."""
    raise NotImplementedError


def classify_keys(release: KbRelease | None, variant_key_ids: Iterable[int]) -> dict[int, KnownClass]:
    """Classify variant keys under ``release`` with one bulk query.

    Keys absent from the release (or every key when ``release`` is None) map to
    :data:`NOT_CLASSIFIED`.
    """
    raise NotImplementedError


def classified_variants_for_patient(
    patient: Patient, release: KbRelease | None, variant_key_ids: Iterable[int] | None = None
) -> list[ClassifiedVariant]:
    """The patient's variants classified under ``release`` (optionally restricted to some keys)."""
    raise NotImplementedError
