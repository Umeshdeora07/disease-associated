"""F2 — ClinVar VCF import, writer and release diff (SPEC §5.3).

Owned by the KB work package. Interface stub created by the Foundation work package: the
dataclasses are final; the function bodies are implemented by the KB package.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from datetime import date
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from ..models import KbRelease, User

logger = logging.getLogger(__name__)


@dataclass
class ClinVarRecord:
    """One (bi-allelic) ClinVar VCF record restricted to the fields GE-EHR uses."""

    chrom: str
    pos: int
    ref: str
    alt: str
    variation_id: int | None = None
    allele_id: int | None = None
    gene: str | None = None
    clnsig: str | None = None
    clnrevstat: str | None = None
    clndn: str | None = None
    clnhgvs: str | None = None
    mc: str | None = None


@dataclass(frozen=True)
class KeyChange:
    """A stored variant key whose class or mechanism differs between two releases."""

    variant_key_id: int
    gene: str | None
    old_class: str
    new_class: str
    old_mechanism: str
    new_mechanism: str
    category: str


def iter_clinvar_records(path: str | os.PathLike[str]) -> Iterator[ClinVarRecord]:
    """Yield panel records of a ClinVar VCF (multi-allelic split; ``.``/symbolic ALTs skipped)."""
    raise NotImplementedError


def read_vcf_header_meta(path: str | os.PathLike[str]) -> dict[str, Any]:
    """Return header metadata: ``fileDate``, ``source`` and the ``ge_ehr_synthetic`` flag."""
    raise NotImplementedError


def import_release(
    path: str | os.PathLike[str],
    *,
    label: str | None = None,
    release_date: date | None = None,
    expert_panel_csv: str | os.PathLike[str] | None = None,
    user: User | None = None,
    notes: str | None = None,
) -> KbRelease:
    """Import a ClinVar VCF as a new, non-current :class:`~ge_ehr.models.KbRelease`.

    Raises ValueError for a file whose sha256 was already imported.
    """
    raise NotImplementedError


def write_clinvar_vcf(
    records: Iterable[dict[str, Any] | ClinVarRecord],
    path: str | os.PathLike[str],
    *,
    file_date: str,
    synthetic: bool = True,
) -> None:
    """Write a ClinVar-format VCF 4.1 (gzip/bgzip when ``path`` ends in ``.gz``), sorted by position.

    ``synthetic=True`` adds the mandatory synthetic header lines (SPEC §0).
    """
    raise NotImplementedError


def diff_releases(
    old: KbRelease | None, new: KbRelease, variant_key_ids: Iterable[int] | None = None
) -> list[KeyChange]:
    """Keys whose class or mechanism differs between ``old`` (None ⇒ all NC) and ``new``."""
    raise NotImplementedError
