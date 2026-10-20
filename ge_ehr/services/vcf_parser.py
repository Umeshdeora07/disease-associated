"""F1 — patient VCF validation and parsing with cyvcf2 (SPEC §4).

Owned by the KB work package. Interface stub created by the Foundation work package: the
exception and dataclasses are final; the function bodies are implemented by the KB package.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)


class VcfError(ValueError):
    """A patient VCF was rejected. The message is user-facing; ``line`` is 1-based or None."""

    def __init__(self, message: str, line: int | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.line = line

    def __str__(self) -> str:
        return f"Line {self.line}: {self.message}" if self.line is not None else self.message


@dataclass(frozen=True)
class ParsedVariant:
    """One panel variant from the first sample (multi-allelic records are split)."""

    chrom: str
    pos: int
    ref: str
    alt: str
    zygosity: str
    gene: str
    filter: str
    genotype: str


@dataclass
class ParsedVcf:
    """Result of :func:`parse_patient_vcf`: kept panel variants plus skip counters."""

    sample_id: str | None
    fileformat: str
    variants: list[ParsedVariant] = field(default_factory=list)
    total_records: int = 0
    off_panel_skipped: int = 0
    non_pass_skipped: int = 0
    ref_calls_skipped: int = 0
    warnings: list[str] = field(default_factory=list)

    @property
    def panel_variants(self) -> int:
        """Number of kept variants inside the 7-gene panel."""
        return len(self.variants)


def parse_patient_vcf(data: bytes, filename: str = "upload.vcf") -> ParsedVcf:
    """Validate and parse a patient VCF (plain or gzip/bgzip, GRCh38).

    Raises :class:`VcfError` (with the offending line number where applicable) for malformed
    input; see SPEC §4 for the full list of checks.
    """
    raise NotImplementedError
