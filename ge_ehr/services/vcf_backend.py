"""VCF reading backends: cyvcf2 (htslib) when installed, an equivalent pure-Python reader otherwise.

The paper specifies cyvcf2, and it stays the primary parser wherever it can be installed. cyvcf2
has no wheels for Windows (and building it there fails), so GE-EHR also ships a pure-Python
reader that returns the same records and raises the same errors; the test suite runs both and
compares them on every fixture and demo file.

The backend is chosen per call by the ``GE_EHR_VCF_BACKEND`` environment variable:

* ``auto`` (default) — cyvcf2 when it can be imported, otherwise the pure-Python reader;
* ``cyvcf2`` — require cyvcf2 (an error names the problem when it is not installed);
* ``python`` — always the pure-Python reader (used by the tests to exercise the fallback).

This module also holds the pure-Python building blocks of the ClinVar reader
(:mod:`ge_ehr.services.clinvar`): the sites-only VCF text reader with its validation, the
header ``##INFO`` definitions and the typed INFO values, which follow htslib's rules (as read by
cyvcf2) so that both backends agree value for value. The patient VCF parser
(:mod:`ge_ehr.services.vcf_parser`) only needs :func:`vcf_backend` and :data:`cyvcf2`.

Owned by the portability work package.
"""

from __future__ import annotations

import gzip
import math
import os
import re
import struct
import zlib
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from types import ModuleType, TracebackType
from typing import BinaryIO

try:  # optional: no Windows wheels
    import cyvcf2 as _cyvcf2
except ImportError:  # pragma: no cover — exercised in the cyvcf2-free test run
    _cyvcf2 = None

cyvcf2: ModuleType | None = _cyvcf2

VCF_BACKEND_ENV = "GE_EHR_VCF_BACKEND"
VCF_BACKEND_CHOICES: tuple[str, ...] = ("auto", "cyvcf2", "python")

# htslib warns about every contig and INFO tag that a header does not declare; GE-EHR validates
# input before htslib sees it, so keep htslib to errors only (unless an operator chose a level with
# cyvcf2's own CYVCF2_HTSLIB_LOG_LEVEL variable).
HTS_LOG_ERROR = 1
if cyvcf2 is not None and "CYVCF2_HTSLIB_LOG_LEVEL" not in os.environ:
    cyvcf2.cyvcf2.set_htslib_log_level(HTS_LOG_ERROR)


def cyvcf2_available() -> bool:
    """True when the cyvcf2 package can be used."""
    return cyvcf2 is not None


def configured_vcf_backend() -> str:
    """The ``GE_EHR_VCF_BACKEND`` choice (``auto`` when unset); ValueError for an unknown value."""
    choice = (os.environ.get(VCF_BACKEND_ENV) or "auto").strip().lower()
    if choice not in VCF_BACKEND_CHOICES:
        raise ValueError(
            f"{VCF_BACKEND_ENV}={os.environ.get(VCF_BACKEND_ENV)!r} is not valid; use one of "
            f"{', '.join(VCF_BACKEND_CHOICES)}."
        )
    return choice


def vcf_backend() -> str:
    """The VCF reader to use now: ``"cyvcf2"`` or ``"python"`` (see the module docstring).

    Raises RuntimeError when ``GE_EHR_VCF_BACKEND=cyvcf2`` but cyvcf2 is not installed, and
    ValueError for an unknown value.
    """
    choice = configured_vcf_backend()
    if choice == "python":
        return "python"
    if cyvcf2 is None:
        if choice == "cyvcf2":
            raise RuntimeError(
                f"{VCF_BACKEND_ENV}=cyvcf2 but the cyvcf2 package is not installed (it has no Windows wheels). "
                f"Install it with 'pip install cyvcf2' or set {VCF_BACKEND_ENV}=auto to use the built-in reader."
            )
        return "python"
    return "cyvcf2"


def describe_vcf_backend() -> str:
    """One line for start-up banners, e.g. ``"cyvcf2 0.34.0 (htslib)"``."""
    if vcf_backend() == "cyvcf2":
        return f"cyvcf2 {getattr(cyvcf2, '__version__', '')} (htslib)".replace("  ", " ")
    reason = "selected" if configured_vcf_backend() == "python" else "cyvcf2 not installed"
    return f"built-in pure-Python reader ({reason})"


# ---------------------------------------------------------------------------
# Typed INFO values, as htslib parses them and cyvcf2 returns them
# ---------------------------------------------------------------------------

# htslib's BCF integer range: the lowest 8 int32 values are reserved for "missing" and friends.
BCF_INT32_MIN = -(2**31) + 8
BCF_INT32_MAX = 2**31 - 1
# A string value whose first byte is BCF's string "missing" marker reads as None in cyvcf2.
_BCF_STR_MISSING = "\x07"

_INT_PREFIX = re.compile(r"([+-]?)0*([0-9]*)")
# The prefix C's strtod() accepts (htslib parses Float values with it).
_FLOAT_PREFIX = re.compile(
    r"""[ \t\n\v\f\r]*
    (?P<number>[+-]?
      (?: (?P<hex>0[xX](?:[0-9a-fA-F]+(?:\.[0-9a-fA-F]*)?|\.[0-9a-fA-F]+)(?:[pP][+-]?[0-9]+)?)
        | (?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][+-]?[0-9]+)?
        | (?i:inf(?:inity)?|nan(?:\([0-9A-Za-z_]*\))?)
      ))""",
    re.VERBOSE,
)
_FLOAT32 = struct.Struct("<f")


def parse_info_integer(text: str) -> int | None:
    """One element of an Integer INFO value: the leading ``[+-]digits`` prefix, None if missing.

    Mirrors htslib: ``"5x"`` → 5, ``"-"`` → 0, ``" 5"``/``"abc"``/``"."`` → None, values outside
    BCF's int32 range → None.
    """
    match = _INT_PREFIX.match(text)
    assert match is not None  # the pattern matches the empty string
    sign, digits = match.groups()
    if not match.group(0):
        return None
    if len(digits) > 10:
        return None
    value = int(digits or "0")
    if sign == "-":
        value = -value
    return value if BCF_INT32_MIN <= value <= BCF_INT32_MAX else None


def parse_info_float(text: str) -> float | None:
    """One element of a Float INFO value: strtod's prefix rounded to float32, None if missing."""
    match = _FLOAT_PREFIX.match(text)
    if match is None:
        return None
    number = match.group("number")
    try:
        value = float.fromhex(number) if match.group("hex") else float(re.sub(r"\(.*\)", "", number))
    except OverflowError:
        value = -math.inf if number.startswith("-") else math.inf
    try:
        return float(_FLOAT32.unpack(_FLOAT32.pack(value))[0])
    except OverflowError:  # finite double beyond float32's range rounds to infinity, as in C
        return math.copysign(math.inf, value)


def typed_info_value(kind: str, value: str | None) -> object:
    """An INFO value as cyvcf2's ``INFO.get`` returns it, given the header ``Type``.

    ``value`` is None for a key written without ``=``: True for a Flag, False otherwise.
    Flags written with a value and String/Character values are returned as text. Integer and
    Float values are a scalar for one element and a tuple for comma-separated lists, with None
    for missing elements.
    """
    if value is None:
        return kind == "Flag"
    if kind in ("String", "Flag"):
        return None if value.startswith(_BCF_STR_MISSING) else value
    parse = parse_info_integer if kind == "Integer" else parse_info_float
    values = [parse(element) for element in value.split(",")]
    return values[0] if len(values) == 1 else tuple(values)


class TextInfo:
    """The INFO column of one record, read like cyvcf2's ``Variant.INFO`` (``get`` only)."""

    __slots__ = ("_column", "_entries", "_types")

    def __init__(self, column: str, types: dict[str, str]) -> None:
        self._column = column
        self._types = types
        self._entries: dict[str, str | None] | None = None

    def get(self, key: str, default: object = None) -> object:
        """The typed value of the first ``key`` entry, ``default`` when the key is absent."""
        if self._entries is None:
            self._entries = info_entries(self._column)
        if key not in self._entries:
            return default
        return typed_info_value(self._types.get(key, "String"), self._entries[key])


def info_entries(column: str) -> dict[str, str | None]:
    """``key → raw value`` (None without ``=``) of an INFO column; the first occurrence wins.

    As in htslib: ``.`` means no INFO, one trailing ``;`` is ignored, empty keys are skipped and
    a value runs to the next ``;`` (so it may contain ``=``).
    """
    if column == ".":
        return {}
    entries: dict[str, str | None] = {}
    for piece in column.removesuffix(";").split(";"):
        key, separator, value = piece.partition("=")
        if key and key not in entries:
            entries[key] = value if separator else None
    return entries


# ---------------------------------------------------------------------------
# Header lines
# ---------------------------------------------------------------------------

_INFO_TYPES = {"Integer": "Integer", "Float": "Float", "String": "String", "Character": "String", "Flag": "Flag"}


def _is_escaped(text: str, start: int, index: int) -> bool:
    """True when ``text[index]`` follows an odd number of backslashes (not before ``start``)."""
    count = 0
    index -= 1
    while index >= start and text[index] == "\\":
        count += 1
        index -= 1
    return count % 2 == 1


def _is_key_start(character: str) -> bool:
    return character.isascii() and (character.isalpha() or character == "_")


def _is_key_char(character: str) -> bool:
    return character.isascii() and (character.isalnum() or character in "_.")


class HeaderLineError(ValueError):
    """A header line that makes htslib refuse the whole header (``[`` without ``]``, ``IDX``)."""


def parse_structured_header_value(value: str) -> list[tuple[str, str]] | None:
    """Parse ``<key=value,...>`` of a structured header line like htslib 1.19, or None if malformed.

    Keys must match ``[A-Za-z_][0-9A-Za-z_.]*``; values are trimmed; ``"..."`` values keep their
    quotes (htslib stores them that way) and may contain escaped quotes, commas and ``>``;
    ``[...]`` values (VCF 4.3 lists) keep their brackets. A malformed line is ignored by htslib
    (None); a ``[`` without its ``]`` makes it refuse the whole header (:class:`HeaderLineError`).
    """
    pairs: list[tuple[str, str]] = []
    length, q, nopen = len(value), 0, 1
    while q < length and nopen > 0:
        q += 1
        p = q
        while q < length and value[q] == " ":
            p += 1
            q += 1
        if p == q and q < length and _is_key_start(value[q]):
            q += 1
            while q < length and _is_key_char(value[q]):
                q += 1
        key_end = q
        while q < length and value[q] == " ":
            q += 1
        if q >= length or value[q] != "=" or key_end == p:
            return None
        key = value[p:key_end]
        q += 1
        p = q
        while q < length and value[q] == " ":
            p += 1
            q += 1
        ending = ""
        if p < length and value[p] == '"':
            ending = '"'
            p += 1
        elif p < length and value[p] == "[":
            ending = "]"
        quoted = bool(ending)
        if quoted:
            q += 1
        while q < length:
            character = value[q]
            if quoted:
                if character == ending and not _is_escaped(value, p, q):
                    break
            else:
                if character == "<":
                    nopen += 1
                if character == ">":
                    nopen -= 1
                if not nopen or (character == "," and nopen == 1):
                    break
            q += 1
        end = q
        if ending == "]":
            if q >= length:
                raise HeaderLineError("a header line has a '[' without its closing ']'")
            end += 1
            q += 1
            quoted = False
        while end > p and value[end - 1] == " ":
            end -= 1
        pairs.append((key, f'"{value[p:end]}"' if quoted else value[p:end]))
        if quoted and q < length and value[q] == ending:
            q += 1
        if q < length and value[q] == ">":
            if nopen:
                nopen -= 1
            q += 1
    return pairs


def header_line_definition(line: str) -> tuple[str, str] | None:
    """``(ID, type)`` declared by a ``##INFO=<...>`` header line; None for any other line.

    As in htslib: the last ``ID`` and ``Type`` keys count, ``Character``, unknown and missing
    types read as String, a line that cannot be parsed or has no ID declares nothing. Raises
    :class:`HeaderLineError` for structured lines (of any kind) that htslib refuses, and — to stay
    clear of htslib's index bookkeeping — for ``IDX`` attributes (they occur only in BCF headers).
    """
    key, separator, value = line[2:].partition("=")
    if not separator or not key or not value.startswith("<"):
        return None
    pairs = parse_structured_header_value(value)
    if pairs is None:
        return None
    attributes = dict(pairs)
    if "IDX" in attributes:
        raise HeaderLineError("a header line has an IDX attribute (BCF index); convert the file with bcftools view")
    if key != "INFO" or "ID" not in attributes:
        return None
    return attributes["ID"], _INFO_TYPES.get(attributes.get("Type", ""), "String")


# ---------------------------------------------------------------------------
# Sites-only (ClinVar-format) VCF text reader
# ---------------------------------------------------------------------------

GZIP_MAGIC = b"\x1f\x8b"
FIXED_COLUMNS = "#CHROM\tPOS\tID\tREF\tALT\tQUAL\tFILTER\tINFO"
MAX_POSITION = 2**31 - 1
# htslib refuses records with 65,535 or more alleles or INFO entries; stay below both limits.
MAX_ALT_ALLELES = 65_000
MAX_INFO_ENTRIES = 65_000

# Contig names as the VCF 4.3 specification allows them (§1.4.7). htslib accepts more, but fails —
# or crashes — on some of it (",", "<", ">", a leading quote, surrounding spaces).
CONTIG_NAME = re.compile(r"[0-9A-Za-z!#$%&+./:;?@^_|~-][0-9A-Za-z!#$%&*+./:;=?@^_|~-]*")
_DIGITS = re.compile(r"[0-9]+")


def htslib_safe_name(name: str) -> bool:
    """True when htslib can declare ``name`` (an INFO key or FILTER name missing from the header).

    htslib adds such names to the header as ``##INFO=<ID=name,...>``; it cannot when the name
    starts with a space, ``"`` or ``[``, ends with a space or contains ``,``, ``<`` or ``>``, and
    then fails the record (or crashes). Everything else is accepted.
    """
    return not (name[0] in ' "[' or name[-1] == " " or "," in name or "<" in name or ">" in name)


class VcfReadError(ValueError):
    """A VCF could not be read. ``stage`` is ``"header"`` or ``"records"``; ``line`` is 1-based."""

    def __init__(self, reason: str, *, stage: str, line: int | None = None) -> None:
        super().__init__(reason)
        self.reason = reason
        self.stage = stage
        self.line = line


@dataclass
class TextRecord:
    """One data line of a sites-only VCF with cyvcf2's attribute names (``INFO`` has ``get``)."""

    CHROM: str
    POS: int
    ID: str | None
    REF: str
    ALT: list[str]
    INFO: TextInfo
    line: int


@dataclass
class TextHeader:
    """What the header declares: INFO types (htslib rules) and the ``#CHROM`` line number."""

    info_types: dict[str, str] = field(default_factory=dict)
    column_line: int = 0


class SitesVcfReader:
    """Pure-Python reader of a sites-only VCF (plain text, gzip or bgzip).

    The header is read and checked on construction; :meth:`records` yields validated
    :class:`TextRecord` objects. Every problem raises :class:`VcfReadError`. The checks reject
    everything htslib fails or crashes on (and a little more, where the VCF specification is
    stricter), so a line accepted here is read identically by htslib — the ClinVar reader runs
    them in lockstep when cyvcf2 is the backend. Trailing blank lines are ignored; a blank line
    between records is an error (htslib would crash on it).
    """

    def __init__(self, path: str | os.PathLike[str]) -> None:
        self.path = Path(path)
        self.line_number = 0
        self._raw: BinaryIO = self.path.open("rb")
        try:
            stream: BinaryIO = self._raw
            if self._raw.peek(2)[:2] == GZIP_MAGIC:  # type: ignore[attr-defined]
                stream = gzip.GzipFile(fileobj=self._raw)  # type: ignore[assignment]
            self._stream = stream
            self.header = self._read_header()
        except BaseException:
            self.close()
            raise

    def __enter__(self) -> SitesVcfReader:
        return self

    def __exit__(
        self, kind: type[BaseException] | None, error: BaseException | None, traceback: TracebackType | None
    ) -> None:
        self.close()

    def close(self) -> None:
        """Close the file (and the decompressor)."""
        stream = getattr(self, "_stream", None)
        if stream is not None and stream is not self._raw:
            stream.close()
        self._raw.close()

    # -- lines ---------------------------------------------------------------

    def _readline(self, stage: str) -> bytes | None:
        """The next line without its ``\\n`` and one ``\\r`` (htslib's rule), None at the end."""
        try:
            line = self._stream.readline()
        except (OSError, EOFError, zlib.error):
            raise VcfReadError("the compressed data is corrupt or incomplete", stage=stage) from None
        if not line:
            return None
        self.line_number += 1
        line = line.removesuffix(b"\n").removesuffix(b"\r")
        if b"\x00" in line:
            raise VcfReadError(
                f"line {self.line_number} contains NUL bytes (binary data)", stage=stage, line=self.line_number
            )
        return line

    def _fail(self, reason: str, stage: str = "records") -> VcfReadError:
        return VcfReadError(f"line {self.line_number}: {reason}", stage=stage, line=self.line_number)

    # -- header --------------------------------------------------------------

    def _read_header(self) -> TextHeader:
        first = self._readline("header")
        if first is None:
            raise VcfReadError("the file is empty", stage="header")
        if not first.startswith(b"##fileformat=VCF"):
            raise VcfReadError("its first line must be '##fileformat=VCFv4.x'", stage="header", line=1)
        header = TextHeader()
        while (line := self._readline("header")) is not None:
            if not line:
                continue
            text = line.decode("utf-8", "replace")
            if text.startswith("##"):
                try:
                    definition = header_line_definition(text)
                except HeaderLineError as exc:
                    raise self._fail(str(exc), "header") from None
                if definition is not None:
                    header.info_types.setdefault(*definition)  # the first definition wins
                continue
            if not text.startswith("#"):
                raise self._fail("a data line appears before the '#CHROM' column header line", "header")
            if text != FIXED_COLUMNS:
                if text.startswith(FIXED_COLUMNS + "\tFORMAT\t"):
                    raise self._fail(
                        "the file has FORMAT and sample columns, but a ClinVar VCF lists sites only "
                        "(is this a patient VCF?)",
                        "header",
                    )
                raise self._fail(
                    "the column header line must be the 8 tab-separated columns "
                    "#CHROM POS ID REF ALT QUAL FILTER INFO",
                    "header",
                )
            header.column_line = self.line_number
            return header
        raise VcfReadError("no '#CHROM' column header line follows the '##' header lines", stage="header")

    # -- records -------------------------------------------------------------

    def records(self) -> Iterator[TextRecord]:
        """Yield the validated data records; trailing blank lines end the file."""
        blank_line: int | None = None
        while (line := self._readline("records")) is not None:
            if not line:
                blank_line = blank_line or self.line_number
                continue
            if blank_line is not None:
                raise VcfReadError(
                    f"line {blank_line} is empty; remove blank lines between records", stage="records", line=blank_line
                )
            yield self._record(line)

    def _record(self, line: bytes) -> TextRecord:
        if line.startswith(b"#"):
            raise self._fail("header lines (starting with '#') must come before the '#CHROM' column header line")
        try:
            text = line.decode("utf-8")
        except UnicodeDecodeError:
            # cyvcf2 raises UnicodeDecodeError for these columns; ID and INFO text is decoded with
            # replacement characters by both backends.
            columns = line.split(b"\t")
            for index, name in ((0, "CHROM"), (3, "REF"), (4, "ALT")):
                if index >= len(columns):
                    break
                try:
                    columns[index].decode("utf-8")
                except UnicodeDecodeError:
                    raise self._fail(f"the {name} column contains bytes that are not valid UTF-8 text") from None
            text = line.decode("utf-8", "replace")
        fields = text.split("\t", 8)
        if len(fields) < 8:
            hint = " Columns must be separated by tab characters." if len(fields) == 1 and " " in text else ""
            raise self._fail(f"expected 8 tab-separated columns but found {len(fields)}.{hint}")
        chrom, pos, id_, ref, alt, _qual, filter_value, info = fields[:8]
        if not CONTIG_NAME.fullmatch(chrom) or chrom.lower() == "chr":
            raise self._fail(f"CHROM {_show(chrom)} is not a valid chromosome name")
        if not _DIGITS.fullmatch(pos) or len(pos.lstrip("0")) > 10 or int(pos) > MAX_POSITION:
            raise self._fail(f"POS {_show(pos)} is not a whole number between 0 and {MAX_POSITION:,}")
        if alt.count(",") >= MAX_ALT_ALLELES:
            raise self._fail(f"the record lists more than {MAX_ALT_ALLELES:,} ALT alleles")
        for name in filter_value.split(";"):
            if name and not htslib_safe_name(name):
                raise self._fail(f"FILTER name {_show(name)} is not valid")
        if info.count(";") >= MAX_INFO_ENTRIES:
            raise self._fail(f"the INFO column has more than {MAX_INFO_ENTRIES:,} entries")
        if info != ".":
            for piece in info.split(";"):
                key = piece.partition("=")[0]
                if key and not htslib_safe_name(key):
                    raise self._fail(
                        f"INFO key {_show(key)} is not valid (no ',', '<', '>' or surrounding spaces)"
                    )
        return TextRecord(
            CHROM=chrom,
            POS=int(pos),
            ID=None if id_ in ("", ".") else id_,
            REF=ref or ".",
            ALT=[] if alt == "." else [allele or "." for allele in alt.split(",")],
            INFO=TextInfo(info, self.header.info_types),
            line=self.line_number,
        )


def _show(value: str, limit: int = 40) -> str:
    """Quote a (possibly huge or hostile) value for an error message."""
    return repr(value if len(value) <= limit else value[:limit] + "…")
