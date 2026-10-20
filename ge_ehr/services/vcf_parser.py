"""F1 — patient VCF validation and parsing with cyvcf2 (SPEC §4).

Parsing runs in two passes:

1. **Validation in pure Python** over the decompressed text. htslib can crash or silently truncate
   on malformed input, so every structural problem is rejected first with a :class:`VcfError`
   whose message (and line number) a lab technician can act on. The validated text is written to
   a temporary file, normalised: LF line endings, no blank lines, first sample only.
2. **cyvcf2** reads that copy: multi-allelic records are split, zygosity comes from the first
   sample's GT, and the FILTER and 7-gene panel filters are applied with counters.

Each record is counted in at most one skip counter, checked in this order: outside the panel
(``off_panel_skipped``), FILTER other than ``PASS``/``.`` (``non_pass_skipped``), no ALT allele in
the genotype (``ref_calls_skipped``). Variant keys are exact GRCh38 (chrom, pos, ref, alt) keys;
no left-normalisation is done (a documented limitation in the paper).

Owned by the KB work package.
"""

from __future__ import annotations

import gzip
import io
import logging
import os
import re
import tempfile
import zlib
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import TextIO

import cyvcf2

from ..constants import GRCH37_CONTIG_LENGTHS, GRCH38_CONTIG_LENGTHS, gene_for_position, normalize_chrom

logger = logging.getLogger(__name__)

# Patient VCFs rarely define every INFO/FORMAT/contig they use; htslib would then print one
# warning per record to stderr. Input is validated before htslib sees it, so keep errors only
# (unless an operator chose a level with cyvcf2's own CYVCF2_HTSLIB_LOG_LEVEL variable).
HTS_LOG_ERROR = 1
if "CYVCF2_HTSLIB_LOG_LEVEL" not in os.environ:
    cyvcf2.cyvcf2.set_htslib_log_level(HTS_LOG_ERROR)

SUPPORTED_VERSIONS: tuple[str, ...] = ("VCFv4.1", "VCFv4.2", "VCFv4.3")
FIXED_COLUMNS: tuple[str, ...] = ("#CHROM", "POS", "ID", "REF", "ALT", "QUAL", "FILTER", "INFO")
# Decompression cap: a gzip "bomb" must not exhaust memory. Panel VCFs are far smaller.
MAX_DECOMPRESSED_BYTES = 256 * 1024 * 1024
MAX_POSITION = 2**31 - 1

GZIP_MAGIC = b"\x1f\x8b"
_BINARY_SIGNATURES: dict[bytes, str] = {
    GZIP_MAGIC: "The file is compressed twice. Decompress it once (gunzip) and upload the resulting .vcf.gz.",
    b"BCF\x02": "This is a BCF (binary VCF) file. Convert it to VCF first, e.g. 'bcftools view -Oz -o out.vcf.gz in.bcf'.",
    b"PK\x03\x04": "This is a ZIP archive (e.g. .zip or .xlsx), not a VCF. Upload the .vcf or .vcf.gz file itself.",
}

_FILEFORMAT = re.compile(r"##fileformat=(\S*)")
_META = re.compile(r"##([^=\s]+)=(.*)")
_ID_ATTRIBUTE = re.compile(r"(?:<|,)ID=([^,>]+)")
_LENGTH_ATTRIBUTE = re.compile(r"(?:<|,)length=([^,>]*)")
_ASSEMBLY_ATTRIBUTE = re.compile(r"(?:<|,)assembly=([^,>]*)")
_STRUCTURED_KEYS_WITH_ID = frozenset({"INFO", "FORMAT", "FILTER", "ALT", "contig", "SAMPLE", "PEDIGREE"})
GRCH37_NAME = re.compile(r"(?<![a-z0-9])(?:grch37|hg19|b37|hs37|g1k_v37)(?!\d)", re.IGNORECASE)
_DIGITS = re.compile(r"[0-9]+")
_BASES = re.compile(r"[ACGTN]+", re.IGNORECASE)
_SYMBOLIC_ALLELE = re.compile(r"<[^<>\s]+>")
_BREAKEND_ALLELE = re.compile(r"[ACGTN]*[\[\]][^\[\]\s]+[\[\]][ACGTN]*|\.[ACGTN]+|[ACGTN]+\.", re.IGNORECASE)
_GENOTYPE = re.compile(r"(?:\.|[0-9]+)(?:[/|](?:\.|[0-9]+))*")
_GENOTYPE_SEPARATOR = re.compile(r"[/|]")


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


@dataclass
class _ValidatedHeader:
    """What the validation pass learnt about the file."""

    fileformat: str
    samples: list[str] = field(default_factory=list)
    record_count: int = 0
    build_declared: bool = False
    warnings: list[str] = field(default_factory=list)


def parse_patient_vcf(data: bytes, filename: str = "upload.vcf") -> ParsedVcf:
    """Validate and parse a patient VCF (plain or gzip/bgzip, GRCh38).

    Raises :class:`VcfError` (with the offending line number where applicable) for malformed
    input; see SPEC §4 for the full list of checks. A file without variant records is accepted
    (0 variants) with a warning. ``filename`` is only used in log messages.
    """
    text = _decode(_decompress(data))
    with tempfile.TemporaryDirectory(prefix="ge_ehr_vcf_") as workdir:
        normalised_path = os.path.join(workdir, "validated.vcf")
        with open(normalised_path, "w", encoding="utf-8", newline="\n") as normalised:
            header = _validate(text, normalised)
        del text
        parsed = ParsedVcf(sample_id=header.samples[0], fileformat=header.fileformat, warnings=header.warnings)
        if header.record_count == 0:
            parsed.warnings.append("The file contains no variant records (header only); no variants were imported.")
        else:
            _read_records(normalised_path, header, parsed)
    logger.info(
        "Parsed patient VCF %r: %d records, %d panel variants, %d off panel, %d non-PASS, %d reference calls",
        filename,
        parsed.total_records,
        parsed.panel_variants,
        parsed.off_panel_skipped,
        parsed.non_pass_skipped,
        parsed.ref_calls_skipped,
    )
    return parsed


# ---------------------------------------------------------------------------
# Bytes → text
# ---------------------------------------------------------------------------


def _decompress(data: bytes) -> bytes:
    """Return the VCF bytes, gunzipping gzip/bgzip input (detected by its magic number)."""
    if not data:
        raise VcfError("The file is empty.")
    if not data.startswith(GZIP_MAGIC):
        return data
    try:
        with gzip.GzipFile(fileobj=io.BytesIO(data)) as stream:
            raw = stream.read(MAX_DECOMPRESSED_BYTES + 1)
    except (OSError, EOFError, zlib.error):
        raise VcfError(
            "The file is gzip-compressed but could not be decompressed: it is truncated or corrupt. "
            "Compress the VCF again (bgzip or gzip) and upload it again."
        ) from None
    if len(raw) > MAX_DECOMPRESSED_BYTES:
        raise VcfError(
            f"The decompressed file is larger than {MAX_DECOMPRESSED_BYTES // 2**20} MB. "
            "Restrict the VCF to the 7-gene panel regions before uploading."
        )
    return raw


def _decode(raw: bytes) -> str:
    """Decode UTF-8 text (a byte-order mark is dropped); binary content is rejected."""
    for signature, message in _BINARY_SIGNATURES.items():
        if raw.startswith(signature):
            raise VcfError(message)
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise VcfError(
            "The file contains bytes that are not valid text (UTF-8). Upload a plain-text VCF (.vcf) "
            "or a gzip-compressed VCF (.vcf.gz).",
            line=raw.count(b"\n", 0, exc.start) + 1,
        ) from None
    nul = text.find("\x00")
    if nul != -1:
        raise VcfError(
            "The file contains binary data (NUL bytes) and is not a text VCF.", line=text.count("\n", 0, nul) + 1
        )
    return text.removeprefix("﻿")


def _iter_lines(text: str) -> Iterator[tuple[int, str]]:
    """Yield ``(line_number, line)`` without building a list; CR of CRLF endings is dropped."""
    start, number, length = 0, 0, len(text)
    while start < length:
        end = text.find("\n", start)
        if end == -1:
            end = length
        number += 1
        yield number, text[start:end].removesuffix("\r")
        start = end + 1


def _bounded_int(text: str, maximum: int) -> int:
    """``int(text)`` for plain digit strings in ``0..maximum``; -1 for anything else.

    Guards against hostile values: ``int()`` accepts signs, spaces and underscores and refuses
    strings of more than 4,300 digits with an error.
    """
    if not _DIGITS.fullmatch(text) or len(text) > len(str(maximum)):
        return -1
    value = int(text)
    return value if value <= maximum else -1


def _show(value: str, limit: int = 40) -> str:
    """Quote a (possibly huge or hostile) value for an error message."""
    return repr(value if len(value) <= limit else value[:limit] + "…")


# ---------------------------------------------------------------------------
# Pass 1: validation
# ---------------------------------------------------------------------------


def _validate(text: str, out: TextIO) -> _ValidatedHeader:
    """Validate the whole file and write the normalised copy (first sample only) to ``out``."""
    lines = _iter_lines(text)
    first_line = next(lines, (1, ""))[1]
    header = _ValidatedHeader(fileformat=_check_fileformat(first_line))
    out.write(first_line.strip() + "\n")

    for number, line in lines:
        if line.startswith("##"):
            _check_meta_line(line, number, header)
            out.write(line + "\n")
        elif line.startswith("#"):
            header.samples = _check_column_header(line, number)
            out.write("\t".join(line.split("\t")[:10]) + "\n")
            break
        elif line.strip():
            raise VcfError(
                "A variant record appears before the '#CHROM' column header line. The header must end "
                "with a tab-separated '#CHROM POS ID REF ALT QUAL FILTER INFO FORMAT <sample>' line.",
                line=number,
            )
    else:
        raise VcfError(
            "The file has no '#CHROM' column header line (expected after the '##' header lines), "
            "so it is not a complete VCF."
        )

    if not header.build_declared:
        header.warnings.append(
            "The header has no ##reference or ##contig lines, so the genome build could not be checked; "
            "GRCh38 coordinates were assumed."
        )
    if len(header.samples) > 1:
        shown = ", ".join(header.samples[:5]) + (", …" if len(header.samples) > 5 else "")
        header.warnings.append(
            f"The file contains {len(header.samples)} samples ({shown}); only the first sample, "
            f"{header.samples[0]!r}, was imported."
        )

    column_count = len(FIXED_COLUMNS) + 1 + len(header.samples)
    for number, line in lines:
        if not line.strip():
            continue
        if line.startswith("#"):
            raise VcfError("Header lines must come before the '#CHROM' column header line.", line=number)
        fields = line.split("\t")
        _check_record(fields, number, column_count, header.samples[0])
        out.write("\t".join(fields[:10]) + "\n")
        header.record_count += 1
    return header


def _check_fileformat(line: str) -> str:
    match = _FILEFORMAT.fullmatch(line.strip())
    if match is None:
        found = f" (found {_show(line)})" if line.strip() else ""
        raise VcfError(
            f"The first line must declare the VCF version, e.g. '##fileformat=VCFv4.2'{found}.", line=1
        )
    version = match.group(1)
    if version not in SUPPORTED_VERSIONS:
        raise VcfError(
            f"VCF version {_show(version)} is not supported; GE-EHR accepts {', '.join(SUPPORTED_VERSIONS)}.",
            line=1,
        )
    return version


def _check_meta_line(line: str, number: int, header: _ValidatedHeader) -> None:
    """Check one ``##key=value`` line; reject GRCh37/hg19 reference and contig declarations."""
    match = _META.fullmatch(line)
    if match is None:
        raise VcfError(f"Header lines must have the form '##key=value' (found {_show(line)}).", line=number)
    key, value = match.groups()
    if key == "fileformat":
        raise VcfError("The ##fileformat line may appear only once, as the first line.", line=number)
    if value.startswith("<"):
        if not value.endswith(">"):
            raise VcfError(f"The ##{key} header line is incomplete: it must end with '>'.", line=number)
        if key in _STRUCTURED_KEYS_WITH_ID and _ID_ATTRIBUTE.search(value) is None:
            raise VcfError(f"The ##{key} header line has no ID=... attribute.", line=number)
    if key == "reference":
        header.build_declared = True
        if GRCH37_NAME.search(value):
            raise VcfError(
                f"GRCh38 required: the ##reference line names a GRCh37/hg19 genome ({_show(value)}). "
                "Lift the variants over to GRCh38 (or call them on GRCh38) and upload again.",
                line=number,
            )
    elif key == "contig":
        header.build_declared = True
        _check_contig(value, number)


def _check_contig(value: str, number: int) -> None:
    assembly = _ASSEMBLY_ATTRIBUTE.search(value)
    if assembly and GRCH37_NAME.search(assembly.group(1)):
        raise VcfError(
            f"GRCh38 required: the ##contig line declares assembly {_show(assembly.group(1))}. "
            "Lift the variants over to GRCh38 (or call them on GRCh38) and upload again.",
            line=number,
        )
    contig_id, length = _ID_ATTRIBUTE.search(value), _LENGTH_ATTRIBUTE.search(value)
    if contig_id is None or length is None:
        return
    chrom, contig_length = normalize_chrom(contig_id.group(1)), _bounded_int(length.group(1), MAX_POSITION)
    if chrom in GRCH37_CONTIG_LENGTHS and GRCH37_CONTIG_LENGTHS[chrom] == contig_length:
        raise VcfError(
            f"GRCh38 required: contig {contig_id.group(1)} has length {contig_length:,}, its GRCh37/hg19 "
            f"length (GRCh38: {GRCH38_CONTIG_LENGTHS[chrom]:,}). Lift the variants over to GRCh38 "
            "(or call them on GRCh38) and upload again.",
            line=number,
        )


def _check_column_header(line: str, number: int) -> list[str]:
    """Check the ``#CHROM`` line and return its sample names."""
    columns = line.split("\t")
    if len(columns) == 1 and " " in line:
        raise VcfError(
            "The '#CHROM' column header line must be separated by tab characters, not spaces.", line=number
        )
    if tuple(columns[: len(FIXED_COLUMNS)]) != FIXED_COLUMNS:
        raise VcfError(
            f"The column header line must start with the tab-separated columns {' '.join(FIXED_COLUMNS)} "
            f"(found {_show(line, 80)}).",
            line=number,
        )
    if len(columns) < len(FIXED_COLUMNS) + 2:
        raise VcfError(
            "The '#CHROM' header line has no FORMAT and sample columns: a patient VCF must contain the "
            "genotypes of at least one sample.",
            line=number,
        )
    if columns[len(FIXED_COLUMNS)] != "FORMAT":
        raise VcfError(
            f"The 9th column of the '#CHROM' header line must be FORMAT (found {_show(columns[8])}).", line=number
        )
    samples = columns[len(FIXED_COLUMNS) + 1 :]
    if any(not sample.strip() for sample in samples):
        raise VcfError("The '#CHROM' header line contains an empty sample name.", line=number)
    seen: set[str] = set()
    for sample in samples:
        if sample in seen:
            raise VcfError(f"Sample name {_show(sample)} appears more than once in the '#CHROM' header line.", line=number)
        seen.add(sample)
    return samples


def _check_record(fields: list[str], number: int, column_count: int, sample: str) -> None:
    """Validate one data line (already split on tabs)."""
    if len(fields) != column_count:
        hint = ""
        if len(fields) < column_count and any(" " in value for value in fields):
            hint = " Columns must be separated by tab characters, not spaces."
        raise VcfError(
            f"Expected {column_count} tab-separated columns (as in the '#CHROM' header line) but found "
            f"{len(fields)}.{hint}",
            line=number,
        )
    chrom, pos, _id, ref, alt, _qual, filter_value, _info, format_keys, sample_value = fields[:10]
    if not chrom or any(char.isspace() for char in chrom):
        raise VcfError("The CHROM column is empty or contains spaces.", line=number)
    if _bounded_int(pos, MAX_POSITION) < 1:
        raise VcfError(f"POS must be a positive whole number, found {_show(pos)}.", line=number)
    if not _BASES.fullmatch(ref):
        raise VcfError(
            f"REF allele {_show(ref)} is not valid: it must consist only of the bases A, C, G, T and N.", line=number
        )
    alt_count = 0 if alt == "." else _check_alt_alleles(alt, number)
    if not filter_value:
        raise VcfError("The FILTER column is empty; use PASS, '.' or the names of failed filters.", line=number)
    keys = format_keys.split(":")
    if "GT" not in keys:
        raise VcfError(
            "The FORMAT column has no GT (genotype) field, so zygosity cannot be determined. "
            "Export the VCF with genotypes.",
            line=number,
        )
    if not sample_value:
        raise VcfError(f"The genotype column of sample {sample!r} is empty; use './.' for a missing call.", line=number)
    values = sample_value.split(":")
    gt_index = keys.index("GT")
    genotype = values[gt_index] if gt_index < len(values) else "."
    if not _GENOTYPE.fullmatch(genotype):
        raise VcfError(
            f"Genotype (GT) {_show(genotype)} of sample {sample!r} is not valid; expected a value such as "
            "0/1, 1/1, 0|1 or 1 (haploid).",
            line=number,
        )
    for allele in _GENOTYPE_SEPARATOR.split(genotype):
        if allele != "." and _bounded_int(allele, alt_count) < 0:
            raise VcfError(
                f"Genotype {_show(genotype)} refers to ALT allele {_show(allele, 12)}, but the record lists only "
                f"{alt_count} ALT allele(s).",
                line=number,
            )


def _check_alt_alleles(alt: str, number: int) -> int:
    """Validate a comma-separated ALT column and return the number of alleles."""
    alleles = alt.split(",")
    for allele in alleles:
        if not (
            _BASES.fullmatch(allele)
            or allele == "*"
            or _SYMBOLIC_ALLELE.fullmatch(allele)
            or _BREAKEND_ALLELE.fullmatch(allele)
        ):
            raise VcfError(
                f"ALT allele {_show(allele)} is not valid: use the bases A, C, G, T and N, '*', '.' for no "
                "alternate allele, or a symbolic allele such as <DEL>.",
                line=number,
            )
    return len(alleles)


# ---------------------------------------------------------------------------
# Pass 2: cyvcf2
# ---------------------------------------------------------------------------


def _read_records(path: str, header: _ValidatedHeader, parsed: ParsedVcf) -> None:
    """Read the validated copy with cyvcf2 and fill ``parsed`` (variants, counters, warnings)."""
    seen: set[tuple[str, int, str, str]] = set()
    symbolic: list[str] = []
    duplicates: list[str] = []
    try:
        reader = cyvcf2.VCF(path)
    except OSError as exc:
        raise VcfError(f"The file could not be read as a VCF ({exc}).") from None
    try:
        for record in _htslib_records(reader):
            parsed.total_records += 1
            chrom, pos = normalize_chrom(record.CHROM), record.POS
            gene = gene_for_position(chrom, pos)
            if gene is None:
                parsed.off_panel_skipped += 1
                continue
            if record.FILTER is not None:
                parsed.non_pass_skipped += 1
                continue
            *alleles, phased = record.genotypes[0]
            carried = sorted({allele for allele in alleles if allele > 0})
            if not carried:
                parsed.ref_calls_skipped += 1
                continue
            genotype = ("|" if phased else "/").join("." if allele < 0 else str(allele) for allele in alleles)
            filter_value = "PASS" if "PASS" in record.FILTERS else "."
            ref = record.REF.upper()
            for index in carried:
                alt = record.ALT[index - 1].upper()
                if alt == "*":  # spanning deletion: the allele belongs to an upstream record
                    continue
                if not _BASES.fullmatch(alt):
                    symbolic.append(f"{alt} at {chrom}:{pos}")
                    continue
                key = (chrom, pos, ref, alt)
                if key in seen:
                    duplicates.append(f"{chrom}-{pos}-{ref}-{alt}")
                    continue
                seen.add(key)
                parsed.variants.append(
                    ParsedVariant(chrom, pos, ref, alt, _zygosity(alleles, index), gene, filter_value, genotype)
                )
    finally:
        reader.close()
    if parsed.total_records != header.record_count:
        raise VcfError(
            f"The file could not be read completely: it has {header.record_count} variant records but only "
            f"{parsed.total_records} could be parsed."
        )
    if symbolic:
        parsed.warnings.append(
            f"{len(symbolic)} symbolic or breakend ALT allele(s) (e.g. {symbolic[0]}) were skipped: "
            "structural variants are not supported."
        )
    if duplicates:
        parsed.warnings.append(
            f"{len(duplicates)} variant(s) were listed more than once (e.g. {duplicates[0]}); "
            "the first occurrence was used."
        )


def _htslib_records(reader: cyvcf2.VCF) -> Iterator[cyvcf2.Variant]:
    """Iterate cyvcf2 records, turning any htslib parse failure into a :class:`VcfError`."""
    iterator = iter(reader)
    while True:
        try:
            record = next(iterator)
        except StopIteration:
            return
        except Exception as exc:  # noqa: BLE001 — cyvcf2 reports htslib failures with assorted types
            raise VcfError(f"The file could not be parsed as a VCF ({exc}).") from None
        yield record


def _zygosity(alleles: list[int], alt_index: int) -> str:
    """Zygosity of ALT allele ``alt_index`` (carried at least once) in a called genotype."""
    if len(alleles) == 1:
        return "hemizygous"
    if all(allele == alt_index for allele in alleles):
        return "homozygous"
    return "heterozygous"
