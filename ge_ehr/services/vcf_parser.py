"""F1 — patient VCF validation and parsing with cyvcf2 (SPEC §4), with a pure-Python fallback.

Parsing runs in two passes:

1. **Validation in pure Python** over the decompressed text. htslib can crash or silently truncate
   on malformed input, so every structural problem is rejected first with a :class:`VcfError`
   whose message (and line number) a lab technician can act on.
2. **cyvcf2** reads a minimal, normalised copy written by the validation pass to a temporary
   file. The copy keeps only what GE-EHR uses — CHROM, POS, REF, ALT, the FILTER verdict and the
   first sample's GT — under a fixed header, so header lines, INFO and other FORMAT fields of the
   upload never reach htslib. cyvcf2 splits multi-allelic records, decodes the genotype (zygosity)
   and the FILTER and 7-gene panel filters are applied with counters.

   Where cyvcf2 is not installed (it has no Windows wheels) or ``GE_EHR_VCF_BACKEND=python``
   selects it (:func:`~ge_ehr.services.vcf_backend.vcf_backend`), :class:`_NormalisedRecord`
   decodes the same normalised lines in memory exactly as htslib does (genotype alleles and
   phasing, FILTER verdict, htslib's 65,534-ALT-allele limit), so both backends return identical
   results and errors.

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
from array import array
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any, TextIO

from ..constants import GRCH37_CONTIG_LENGTHS, GRCH38_CONTIG_LENGTHS, gene_for_position, normalize_chrom

# The normalised copy declares no contigs, so htslib adds each one with a warning; vcf_backend
# keeps htslib to errors only when it imports cyvcf2 (``cyvcf2`` is None when not installed).
from .vcf_backend import HTS_LOG_ERROR, cyvcf2, vcf_backend  # noqa: F401 — HTS_LOG_ERROR re-exported

logger = logging.getLogger(__name__)

SUPPORTED_VERSIONS: tuple[str, ...] = ("VCFv4.1", "VCFv4.2", "VCFv4.3")
FIXED_COLUMNS: tuple[str, ...] = ("#CHROM", "POS", "ID", "REF", "ALT", "QUAL", "FILTER", "INFO")
# Decompression cap: a gzip "bomb" must not exhaust memory. Panel VCFs are far smaller.
MAX_DECOMPRESSED_BYTES = 256 * 1024 * 1024
MAX_POSITION = 2**31 - 1

GZIP_MAGIC = b"\x1f\x8b"
# BGZF (bgzip) files are gzip members carrying a "BC" extra field and end with this empty block;
# without it the file was cut short at a block boundary, which gzip itself cannot detect.
BGZF_EOF = bytes.fromhex("1f8b08040000000000ff0600424302001b0003000000000000000000")
_BGZF_PREFIX = b"\x1f\x8b\x08\x04"
_BGZF_SUBFIELD_OFFSET = 12

_BINARY_SIGNATURES: dict[bytes, str] = {
    GZIP_MAGIC: "The file is compressed twice. Decompress it once (gunzip) and upload the resulting .vcf.gz.",
    b"BCF\x02": (
        "This is a BCF (binary VCF) file. Convert it to VCF first, e.g. 'bcftools view -Oz -o out.vcf.gz in.bcf'."
    ),
    b"PK\x03\x04": "This is a ZIP archive (e.g. .zip or .xlsx), not a VCF. Upload the .vcf or .vcf.gz file itself.",
    b"\xff\xfe": "The file is saved as UTF-16 text. Save it as plain (UTF-8 or ASCII) text and upload it again.",
    b"\xfe\xff": "The file is saved as UTF-16 text. Save it as plain (UTF-8 or ASCII) text and upload it again.",
}

# The normalised copy read by cyvcf2: a fixed header, one fixed sample name and a FILTER verdict.
FAILED_FILTER = "FAIL"
_NORMALISED_HEADER = (
    "##fileformat=VCFv4.2\n"
    f'##FILTER=<ID={FAILED_FILTER},Description="Failed one or more filters in the uploaded file">\n'
    '##FORMAT=<ID=GT,Number=1,Type=String,Description="Genotype">\n'
    "#CHROM\tPOS\tID\tREF\tALT\tQUAL\tFILTER\tINFO\tFORMAT\tSAMPLE\n"
)
_PASSING_FILTERS = frozenset({"PASS", "."})

_FILEFORMAT = re.compile(r"##fileformat=(\S*)")
_META = re.compile(r"##([^=\s]+)=(.*)")
_ID_ATTRIBUTE = re.compile(r"(?:<|,)ID=([^,>]+)")
_LENGTH_ATTRIBUTE = re.compile(r"(?:<|,)length=([^,>]*)")
_ASSEMBLY_ATTRIBUTE = re.compile(r"(?:<|,)assembly=([^,>]*)")
_STRUCTURED_KEYS_WITH_ID = frozenset({"INFO", "FORMAT", "FILTER", "ALT", "contig", "SAMPLE"})
GRCH37_NAME = re.compile(r"(?<![a-z0-9])(?:grch37|hg19|b37|hs37|g1k_v37)(?!\d)", re.IGNORECASE)
# Contig names as allowed by VCF 4.3 §1.4.7 (no whitespace, commas, brackets or quotes).
_CHROM = re.compile(r"[0-9A-Za-z!#$%&+./:;?@^_|~-][0-9A-Za-z!#$%&*+./:;=?@^_|~-]*")
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
class _ValidatedFile:
    """What the validation pass learnt about the file."""

    fileformat: str
    samples: list[str] = field(default_factory=list)
    # Line number (in the upload) of each data record, in order; maps htslib failures back.
    record_lines: array[int] = field(default_factory=lambda: array("q"))
    build_declared: bool = False
    warnings: list[str] = field(default_factory=list)

    @property
    def record_count(self) -> int:
        return len(self.record_lines)

    def line_of(self, index: int) -> int | None:
        """Line number of the ``index``-th (0-based) data record, None past the last one."""
        return self.record_lines[index] if index < len(self.record_lines) else None


def parse_patient_vcf(data: bytes, filename: str = "upload.vcf") -> ParsedVcf:
    """Validate and parse a patient VCF (plain or gzip/bgzip, GRCh38).

    Raises :class:`VcfError` (with the offending line number where applicable) for malformed
    input; see SPEC §4 for the full list of checks. A file without variant records is accepted
    (0 variants) with a warning. ``filename`` is only used in log messages.
    """
    text = _decode(_decompress(data))
    if vcf_backend() == "cyvcf2":
        with tempfile.TemporaryDirectory(prefix="ge_ehr_vcf_") as workdir:
            normalised_path = os.path.join(workdir, "normalised.vcf")
            with open(normalised_path, "w", encoding="utf-8", newline="\n") as normalised:
                validated = _validate(text, normalised)
            del text
            parsed = _parsed_vcf(validated)
            if validated.record_count:
                _read_records(normalised_path, validated, parsed)
    else:
        buffer = io.StringIO(newline="\n")
        validated = _validate(text, buffer)
        del text
        parsed = _parsed_vcf(validated)
        if validated.record_count:
            buffer.seek(0)
            _read_records_in_python(buffer, validated, parsed)
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


def _parsed_vcf(validated: _ValidatedFile) -> ParsedVcf:
    parsed = ParsedVcf(sample_id=validated.samples[0], fileformat=validated.fileformat, warnings=validated.warnings)
    if validated.record_count == 0:
        parsed.warnings.append("The file contains no variant records (header only); no variants were imported.")
    return parsed


# ---------------------------------------------------------------------------
# Bytes → text
# ---------------------------------------------------------------------------


def is_truncated_bgzf(head: bytes, tail: bytes) -> bool:
    """True for a BGZF (bgzip) file whose last bytes are not the BGZF end-of-file block.

    ``head`` is the start of the file (at least 14 bytes), ``tail`` its last 28 bytes. Plain gzip
    files carry no such marker and are never reported (gzip detects their truncation itself).
    """
    is_bgzf = head.startswith(_BGZF_PREFIX) and head[_BGZF_SUBFIELD_OFFSET : _BGZF_SUBFIELD_OFFSET + 2] == b"BC"
    return is_bgzf and tail != BGZF_EOF


def _decompress(data: bytes) -> bytes:
    """Return the VCF bytes, gunzipping gzip/bgzip input (detected by its magic number)."""
    if not data:
        raise VcfError("The file is empty.")
    if not data.startswith(GZIP_MAGIC):
        return data
    truncated = VcfError(
        "The file is gzip-compressed but incomplete or corrupt (the upload or an earlier copy was probably cut "
        "short). Compress the VCF again (bgzip or gzip) and upload it again."
    )
    if is_truncated_bgzf(data, data[-len(BGZF_EOF) :]):
        raise truncated
    try:
        with gzip.GzipFile(fileobj=io.BytesIO(data)) as stream:
            raw = stream.read(MAX_DECOMPRESSED_BYTES + 1)
    except (OSError, EOFError, zlib.error):
        raise truncated from None
    if len(raw) > MAX_DECOMPRESSED_BYTES:
        raise VcfError(
            f"The decompressed file is larger than {MAX_DECOMPRESSED_BYTES // 2**20} MB. "
            "Restrict the VCF to the 7-gene panel regions before uploading."
        )
    return raw


def _decode(raw: bytes) -> str:
    """Decode UTF-8 text (a byte-order mark is dropped); binary content is rejected.

    Old Mac-style files that end lines with a lone carriage return are read like LF files.
    """
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
    if "\n" not in text and "\r" in text:
        text = text.replace("\r", "\n")
    return text.removeprefix("﻿")


def _iter_lines(text: str) -> Iterator[tuple[int, str]]:
    """Yield ``(line_number, line)`` without building a list; trailing whitespace (CR of CRLF
    endings, stray spaces or tabs left by spreadsheet exports) is dropped."""
    start, number, length = 0, 0, len(text)
    while start < length:
        end = text.find("\n", start)
        if end == -1:
            end = length
        number += 1
        yield number, text[start:end].rstrip()
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


def _is_valid_chrom(name: str) -> bool:
    """True for a VCF 4.3 contig name that is more than a bare ``chr`` prefix."""
    return _CHROM.fullmatch(name) is not None and name.lower() != "chr"


def _show(value: str, limit: int = 40) -> str:
    """Quote a (possibly huge or hostile) value for an error message."""
    return repr(value if len(value) <= limit else value[:limit] + "…")


# ---------------------------------------------------------------------------
# Pass 1: validation
# ---------------------------------------------------------------------------


def _validate(text: str, out: TextIO) -> _ValidatedFile:
    """Validate the whole file and write the normalised copy (see the module docstring) to ``out``."""
    lines = _iter_lines(text)
    validated = _ValidatedFile(fileformat=_check_fileformat(next(lines, (1, ""))[1]))

    for number, line in lines:
        if line.startswith("##"):
            _check_meta_line(line, number, validated)
        elif line.startswith("#"):
            validated.samples = _check_column_header(line, number)
            break
        elif line:
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

    if not validated.build_declared:
        validated.warnings.append(
            "The header has no ##reference or ##contig lines, so the genome build could not be checked; "
            "GRCh38 coordinates were assumed."
        )
    if len(validated.samples) > 1:
        shown = ", ".join(validated.samples[:5]) + (", …" if len(validated.samples) > 5 else "")
        validated.warnings.append(
            f"The file contains {len(validated.samples)} samples ({shown}); only the first sample, "
            f"{validated.samples[0]!r}, was imported."
        )

    out.write(_NORMALISED_HEADER)
    column_count = len(FIXED_COLUMNS) + 1 + len(validated.samples)
    for number, line in lines:
        if not line:
            continue
        if line.startswith("#"):
            raise VcfError("Header lines must come before the '#CHROM' column header line.", line=number)
        out.write(_check_record(line.split("\t"), number, column_count, validated.samples[0]))
        validated.record_lines.append(number)
    return validated


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


def _check_meta_line(line: str, number: int, validated: _ValidatedFile) -> None:
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
        validated.build_declared = True
        if GRCH37_NAME.search(value):
            raise VcfError(
                f"GRCh38 required: the ##reference line names a GRCh37/hg19 genome ({_show(value)}). "
                "Lift the variants over to GRCh38 (or call them on GRCh38) and upload again.",
                line=number,
            )
    elif key == "contig":
        validated.build_declared = True
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
    if contig_id is None or length is None or not _is_valid_chrom(contig_id.group(1)):
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
    if len(columns) == len(FIXED_COLUMNS):
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
    if not samples:
        raise VcfError(
            "The '#CHROM' header line has a FORMAT column but no sample column: a patient VCF must contain "
            "the genotypes of at least one sample.",
            line=number,
        )
    if any(not sample.strip() for sample in samples):
        raise VcfError("The '#CHROM' header line contains an empty sample name.", line=number)
    seen: set[str] = set()
    for sample in samples:
        if sample in seen:
            raise VcfError(
                f"Sample name {_show(sample)} appears more than once in the '#CHROM' header line.", line=number
            )
        seen.add(sample)
    return samples


def _check_record(fields: list[str], number: int, column_count: int, sample: str) -> str:
    """Validate one data line (already split on tabs) and return its normalised copy."""
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
    if not _is_valid_chrom(chrom):
        raise VcfError(
            f"CHROM {_show(chrom)} is not a valid chromosome name (e.g. 7 or chr7; no spaces, commas, quotes "
            "or angle brackets).",
            line=number,
        )
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
    # Trailing FORMAT fields may be dropped from a sample column (VCF 4.2 §1.6.2): GT is missing.
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
    verdict = filter_value if filter_value in _PASSING_FILTERS else FAILED_FILTER
    return f"{chrom}\t{pos}\t.\t{ref}\t{alt}\t.\t{verdict}\t.\tGT\t{genotype}\n"


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


def _read_records(path: str, validated: _ValidatedFile, parsed: ParsedVcf) -> None:
    """Read the normalised copy with cyvcf2 and fill ``parsed`` (variants, counters, warnings)."""
    seen: set[tuple[str, int, str, str]] = set()
    symbolic: list[str] = []
    duplicates: list[str] = []
    try:
        reader = cyvcf2.VCF(path)
    except OSError as exc:
        raise VcfError(f"The file could not be read as a VCF ({exc}).") from None
    try:
        records = iter(reader)
        while True:
            try:
                record = next(records)
            except StopIteration:
                break
            except Exception:  # noqa: BLE001 — cyvcf2 reports htslib failures with assorted types
                raise VcfError(
                    "This variant record could not be read; check its CHROM, POS, REF, ALT, FILTER and GT values.",
                    line=validated.line_of(parsed.total_records),
                ) from None
            parsed.total_records += 1
            _add_record(record, parsed, seen, symbolic, duplicates)
    finally:
        reader.close()
    if parsed.total_records != validated.record_count:
        raise VcfError(
            f"The file could not be read completely: it has {validated.record_count} variant records but only "
            f"{parsed.total_records} could be read.",
            line=validated.line_of(parsed.total_records),
        )
    _add_skip_warnings(parsed, symbolic, duplicates)


def _read_records_in_python(normalised: TextIO, validated: _ValidatedFile, parsed: ParsedVcf) -> None:
    """The pure-Python twin of :func:`_read_records`: decode the normalised lines like htslib."""
    seen: set[tuple[str, int, str, str]] = set()
    symbolic: list[str] = []
    duplicates: list[str] = []
    for _ in range(_NORMALISED_HEADER.count("\n")):
        normalised.readline()
    for index, line in enumerate(normalised):
        record = _NormalisedRecord.parse(line.removesuffix("\n"))
        if record is None:
            raise VcfError(
                "This variant record could not be read; check its CHROM, POS, REF, ALT, FILTER and GT values.",
                line=validated.line_of(index),
            )
        parsed.total_records += 1
        _add_record(record, parsed, seen, symbolic, duplicates)
    _add_skip_warnings(parsed, symbolic, duplicates)


# htslib stores the allele count in 16 bits and refuses records with 65,535 alleles or more.
_HTSLIB_MAX_ALLELES = 65_534


@dataclass(frozen=True)
class _NormalisedRecord:
    """One line of the normalised copy, decoded with cyvcf2's attribute names and values."""

    CHROM: str
    POS: int
    REF: str
    ALT: list[str]
    FILTER: str | None
    FILTERS: list[str]
    genotypes: list[list[Any]]

    @classmethod
    def parse(cls, line: str) -> _NormalisedRecord | None:
        """Decode ``CHROM POS . REF ALT . VERDICT . GT genotype``; None where htslib fails."""
        chrom, pos, _id, ref, alt, _qual, verdict, _info, _format, genotype = line.split("\t")
        alts = [] if alt == "." else alt.split(",")
        if len(alts) > _HTSLIB_MAX_ALLELES:
            return None
        alleles: list[Any] = [-1 if allele == "." else int(allele) for allele in _GENOTYPE_SEPARATOR.split(genotype)]
        # cyvcf2 reports a genotype as phased when its first separator is "|" (htslib keeps the
        # phase bit on the second allele); haploid calls are unphased.
        separator = _GENOTYPE_SEPARATOR.search(genotype)
        alleles.append(separator is not None and separator.group() == "|")
        return cls(
            CHROM=chrom,
            POS=int(pos),
            REF=ref,
            ALT=alts,
            FILTER=None if verdict in _PASSING_FILTERS else verdict,
            FILTERS=[] if verdict == "." else [verdict],
            genotypes=[alleles],
        )


def _add_skip_warnings(parsed: ParsedVcf, symbolic: list[str], duplicates: list[str]) -> None:
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


def _add_record(
    record: Any,
    parsed: ParsedVcf,
    seen: set[tuple[str, int, str, str]],
    symbolic: list[str],
    duplicates: list[str],
) -> None:
    """Apply the panel, FILTER and genotype filters to one record (a cyvcf2 ``Variant`` or a
    :class:`_NormalisedRecord`) and keep its carried ALT alleles."""
    chrom, pos = normalize_chrom(record.CHROM), record.POS
    gene = gene_for_position(chrom, pos)
    if gene is None:
        parsed.off_panel_skipped += 1
        return
    if record.FILTER is not None:
        parsed.non_pass_skipped += 1
        return
    *alleles, phased = record.genotypes[0]
    carried = sorted({allele for allele in alleles if allele > 0})
    if not carried:
        parsed.ref_calls_skipped += 1
        return
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
        zygosity = _zygosity(alleles, index)
        parsed.variants.append(ParsedVariant(chrom, pos, ref, alt, zygosity, gene, filter_value, genotype))


def _zygosity(alleles: list[int], alt_index: int) -> str:
    """Zygosity of ALT allele ``alt_index`` (carried at least once) in a called genotype."""
    if len(alleles) == 1:
        return "hemizygous"
    if all(allele == alt_index for allele in alleles):
        return "homozygous"
    return "heterozygous"
