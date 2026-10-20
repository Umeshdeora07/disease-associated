"""F2 — ClinVar significance normalisation and classification lookup (SPEC §5.1, §5.2).

ClinVar's aggregate VCF encodes the germline classification (``CLNSIG``), review status
(``CLNREVSTAT``) and condition names (``CLNDN``) as underscore-for-space strings. This module maps
them onto the ACMG/AMP classes of :data:`ge_ehr.constants.CLASSES`, derives ClinVar review stars,
infers the KATP-channel mechanism that rule D4 needs, and classifies stored variant keys under a
knowledge-base release with bulk queries. Variants absent from a release are *not classified*
(``NC``), never VUS (paper §3).

Owned by the KB work package.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Iterable
from dataclasses import dataclass
from itertools import batched
from typing import TYPE_CHECKING

from sqlalchemy import select
from sqlalchemy.orm import joinedload

from .. import constants as C
from ..extensions import db
from ..models import KnownVariant, PatientVariant
from .rules import ClassifiedVariant

if TYPE_CHECKING:
    from ..models import KbRelease, Patient

logger = logging.getLogger(__name__)

# Bound parameters per ``IN (...)`` clause. SQLite builds before 3.32 allow only 999 host
# parameters per statement; 900 leaves room for the other parameters of the query.
IN_CLAUSE_CHUNK = 900


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


# ---------------------------------------------------------------------------
# ClinVar term normalisation (SPEC §5.1)
# ---------------------------------------------------------------------------

_SIGNIFICANCE_TERMS: dict[str, str] = {
    "pathogenic": "P",
    "pathogenic, low penetrance": "P",
    "likely pathogenic": "LP",
    "likely pathogenic, low penetrance": "LP",
    "uncertain significance": "VUS",
    "likely benign": "LB",
    "benign": "B",
}

_REVIEW_STARS: dict[str, int] = {
    "practice guideline": 4,
    "reviewed by expert panel": 3,
    "criteria provided, multiple submitters, no conflicts": 2,
    "criteria provided, single submitter": 1,
}
_EXPERT_PANEL_STATUSES = frozenset({"practice guideline", "reviewed by expert panel"})

# Accepted spellings of a class in user-supplied files (expert-panel overlay CSV).
_CLASS_SPELLINGS: dict[str, str] = {
    **{code.lower(): code for code in C.CLASSES if code != "NC"},
    **{label.lower(): code for code, label in C.CLASS_LABELS.items() if code != "NC"},
}

_KATP_CHANNEL_GENES = frozenset({"KCNJ11", "ABCC8"})
_DIABETES_PATTERN = re.compile(r"neonatal|diabetes", re.IGNORECASE)
_HYPERINSULINISM_PATTERN = re.compile(r"hyperinsulin|hypoglyc", re.IGNORECASE)
_WHITESPACE = re.compile(r"\s+")
_COMMA = re.compile(r"\s*,\s*")


def _normalise_term(value: str) -> str:
    """Lower-case a ClinVar term with ``_`` read as a space and canonical ``", "`` separators."""
    text = _WHITESPACE.sub(" ", value.replace("_", " ")).strip().lower()
    return _COMMA.sub(", ", text)


def normalize_clinvar_significance(clnsig: str | None) -> str:
    """Map a raw CLNSIG value to one of :data:`ge_ehr.constants.CLASSES` (SPEC §5.1).

    Only the primary term (before the first ``|``) counts; secondary terms such as
    ``risk_factor`` are ignored. ``/``-combined terms resolve conservatively within their band
    (``Pathogenic/Likely_pathogenic`` → LP, ``Benign/Likely_benign`` → LB), non-ACMG components
    such as ``Likely_risk_allele`` are dropped, every ``Conflicting_*`` aggregate is VUS and
    anything without an ACMG term (``not_provided``, ``drug_response``, ``association``, ...) is NC.

    >>> normalize_clinvar_significance("Pathogenic/Likely_pathogenic|risk_factor")
    'LP'
    """
    if not clnsig:
        return "NC"
    term = _normalise_term(clnsig.split("|", 1)[0])
    if term.startswith("conflicting"):
        return "VUS"
    parts = (part.strip() for part in term.split("/"))
    classes = {_SIGNIFICANCE_TERMS[part] for part in parts if part in _SIGNIFICANCE_TERMS}
    if not classes:
        return "NC"
    if len(classes) == 1:
        return classes.pop()
    if classes <= C.ACTIONABLE:
        return "LP"
    if classes <= {"LB", "B"}:
        return "LB"
    # Components from different bands: ClinVar reports those as "Conflicting_*", so treat alike.
    return "VUS"


def review_stars(clnrevstat: str | None) -> int:
    """ClinVar review stars (0–4) for a raw CLNREVSTAT value.

    ``practice_guideline`` 4, ``reviewed_by_expert_panel`` 3,
    ``criteria_provided,_multiple_submitters,_no_conflicts`` 2,
    ``criteria_provided,_single_submitter`` and ``criteria_provided,_conflicting_*`` 1, else 0.
    """
    status = _normalise_term(clnrevstat or "")
    if status in _REVIEW_STARS:
        return _REVIEW_STARS[status]
    return 1 if status.startswith("criteria provided, conflicting") else 0


def is_expert_panel_status(clnrevstat: str | None) -> bool:
    """True for ``reviewed_by_expert_panel`` and ``practice_guideline``."""
    return _normalise_term(clnrevstat or "") in _EXPERT_PANEL_STATUSES


def infer_mechanism(gene: str | None, condition: str | None) -> str:
    """Infer the variant mechanism from its condition (KCNJ11/ABCC8 only; other genes → ``unknown``).

    Activating KATP-channel variants cause neonatal diabetes and inactivating ones congenital
    hyperinsulinism, so: a condition text mentioning ``neonatal``/``diabetes`` but no
    hyperinsulinism/hypoglycaemia → ``activating``; the reverse → ``inactivating``; both or
    neither → ``unknown``. ``condition`` may be ClinVar's raw CLNDN (``_`` for spaces, ``|``
    between conditions) or readable text; the whole text is searched either way.
    """
    if (gene or "").upper() not in _KATP_CHANNEL_GENES or not condition:
        return "unknown"
    mentions_diabetes = _DIABETES_PATTERN.search(condition) is not None
    mentions_hyperinsulinism = _HYPERINSULINISM_PATTERN.search(condition) is not None
    if mentions_diabetes and not mentions_hyperinsulinism:
        return "activating"
    if mentions_hyperinsulinism and not mentions_diabetes:
        return "inactivating"
    return "unknown"


def classification_from_text(value: str | None) -> str | None:
    """Parse a class written as a code (``LP``) or label (``Likely pathogenic``), else None.

    Case-insensitive; ClinVar-style underscores are accepted and ``VUS`` is a code. ``NC`` /
    "Not classified" is not a classification and returns None.
    """
    return _CLASS_SPELLINGS.get(_normalise_term(value or ""))


# ---------------------------------------------------------------------------
# Classification lookup (SPEC §5.2)
# ---------------------------------------------------------------------------


def classify_keys(release: KbRelease | None, variant_key_ids: Iterable[int]) -> dict[int, KnownClass]:
    """Classify variant keys under ``release`` with one bulk query.

    The query is issued in chunks of :data:`IN_CLAUSE_CHUNK` keys for large key sets (SQLite
    limits the number of bound parameters). Keys absent from the release (or every key when
    ``release`` is None) map to :data:`NOT_CLASSIFIED`.
    """
    key_ids = set(variant_key_ids)
    result = dict.fromkeys(key_ids, NOT_CLASSIFIED)
    if release is None or not key_ids:
        return result
    columns = (
        KnownVariant.variant_key_id,
        KnownVariant.id,
        KnownVariant.classification,
        KnownVariant.mechanism,
        KnownVariant.is_expert_panel,
        KnownVariant.review_status,
        KnownVariant.review_stars,
        KnownVariant.condition,
    )
    for chunk in batched(sorted(key_ids), IN_CLAUSE_CHUNK):
        statement = select(*columns).where(
            KnownVariant.release_id == release.id, KnownVariant.variant_key_id.in_(chunk)
        )
        for row in db.session.execute(statement):
            result[row.variant_key_id] = KnownClass(
                classification=row.classification,
                mechanism=row.mechanism,
                known_variant_id=row.id,
                is_expert_panel=row.is_expert_panel,
                review_status=row.review_status,
                review_stars=row.review_stars,
                condition=row.condition,
            )
    return result


def classified_variants_for_patient(
    patient: Patient, release: KbRelease | None, variant_key_ids: Iterable[int] | None = None
) -> list[ClassifiedVariant]:
    """The patient's variants classified under ``release`` (optionally restricted to some keys).

    Two queries regardless of the number of variants: the patient's variants with their keys,
    then :func:`classify_keys`. Ordered by PatientVariant id.
    """
    statement = (
        select(PatientVariant)
        .where(PatientVariant.patient_id == patient.id)
        .options(joinedload(PatientVariant.variant_key))
        .order_by(PatientVariant.id)
    )
    variants = list(db.session.execute(statement).scalars())
    if variant_key_ids is not None:
        wanted = set(variant_key_ids)
        variants = [variant for variant in variants if variant.variant_key_id in wanted]
    classes = classify_keys(release, (variant.variant_key_id for variant in variants))
    classified: list[ClassifiedVariant] = []
    for variant in variants:
        known = classes[variant.variant_key_id]
        classified.append(
            ClassifiedVariant(
                patient_variant_id=variant.id,
                variant_key_id=variant.variant_key_id,
                gene=variant.gene or variant.variant_key.gene or "",
                classification=known.classification,
                mechanism=known.mechanism,
                zygosity=variant.zygosity,
                label=variant.variant_key.label,
                is_expert_panel=known.is_expert_panel,
            )
        )
    return classified
