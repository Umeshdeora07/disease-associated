"""F2 — ClinVar VCF import, writer, release diff and 7-gene download subsets (SPEC §5.3).

* :func:`iter_clinvar_records` reads a ClinVar(-format) VCF with cyvcf2 and yields the panel
  records; :func:`import_release` stores one file as a new, non-current :class:`KbRelease` with
  bulk inserts (optionally applying an expert-panel overlay CSV).
* :func:`write_clinvar_vcf` writes deterministic ClinVar-format VCFs (used for the synthetic demo
  and evaluation releases; bgzip-compressed when the name ends in ``.gz``).
* :func:`diff_releases` lists the stored keys whose class or mechanism differs between releases.
* :func:`fetch_clinvar_subset` streams a real ClinVar GRCh38 VCF (URL or local file; stdlib
  ``urllib`` + ``gzip`` only) and writes its 7-gene subset. It backs ``scripts/fetch_clinvar.py``
  and ``flask clinvar sync``.

Owned by the KB work package.
"""

from __future__ import annotations

import csv
import gzip
import hashlib
import http.client
import io
import logging
import os
import re
import struct
import time
import urllib.error
import urllib.request
import zlib
from collections import Counter
from collections.abc import Iterable, Iterator
from dataclasses import asdict, dataclass, fields
from datetime import date, datetime
from itertools import batched, chain
from pathlib import Path
from typing import IO, TYPE_CHECKING, Any, BinaryIO

import cyvcf2
from sqlalchemy import insert, select

from .. import constants as C
from ..extensions import db
from ..models import KbRelease, KnownVariant, VariantKey
from ..security import audit
from .classification import (
    IN_CLAUSE_CHUNK,
    classification_from_text,
    infer_mechanism,
    is_expert_panel_status,
    normalize_clinvar_significance,
    review_stars,
)

# vcf_parser also lowers htslib's log level to errors only when it is imported.
from .vcf_parser import GRCH37_NAME, GZIP_MAGIC

if TYPE_CHECKING:
    from ..models import User

logger = logging.getLogger(__name__)

INSERT_BATCH = 1000

# NCBI Gene IDs of the panel genes, written into GENEINFO as "SYMBOL:ID" like ClinVar does.
NCBI_GENE_IDS: dict[str, int] = {
    "GCK": 2645,
    "HNF1A": 6927,
    "HNF4A": 3172,
    "HNF1B": 6928,
    "KCNJ11": 3767,
    "ABCC8": 6833,
    "INS": 3630,
}

# INFO fields GE-EHR reads and writes, with ClinVar's own header definitions.
CLINVAR_INFO_DEFINITIONS: tuple[tuple[str, str, str, str], ...] = (
    ("ALLELEID", "1", "Integer", "the ClinVar Allele ID"),
    ("CLNDN", ".", "String",
     "ClinVar's preferred disease name for the concept specified by disease identifiers in CLNDISDB"),
    ("CLNHGVS", ".", "String", "Top-level (primary assembly, alt, or patch) HGVS expression."),
    ("CLNREVSTAT", ".", "String", "ClinVar review status of germline classification for the Variation ID"),
    ("CLNSIG", ".", "String",
     "Aggregate germline classification for this single variant; multiple values are separated by a vertical bar"),
    ("GENEINFO", "1", "String",
     "Gene(s) for the variant reported as gene symbol:gene id. The gene symbol and id are delimited by a colon (:) "
     "and each pair is delimited by a vertical bar (|)"),
    ("MC", ".", "String",
     "comma separated list of molecular consequence in the form of Sequence Ontology ID|molecular_consequence"),
)

OVERLAY_COLUMNS: tuple[str, ...] = ("clinvar_variation_id", "classification", "panel", "date")

CLINVAR_GRCH38_BASE_URL = "https://ftp.ncbi.nlm.nih.gov/pub/clinvar/vcf_GRCh38"
DOWNLOAD_TIMEOUT_SECONDS = 60
_USER_AGENT = "GE-EHR ClinVar subset fetcher (research prototype)"

_BASES = re.compile(r"[ACGTN]+")
_DIGITS = re.compile(r"[0-9]+")
_WHITESPACE = re.compile(r"\s")
_URL = re.compile(r"[A-Za-z][A-Za-z0-9+.-]*://")
_GENEINFO = re.compile(r"(?:^|;)GENEINFO=([^;]*)")
_SUBSET_HEADER_KEY = "ge_ehr_subset"

# BGZF (blocked gzip, as written by bgzip): independent gzip members of at most 64 KiB carrying
# a "BC" extra field with the block size, followed by an empty end-of-file block.
_BGZF_BLOCK_INPUT = 0xFF00
_BGZF_EOF = bytes.fromhex("1f8b08040000000000ff0600424302001b0003000000000000000000")


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


@dataclass(frozen=True)
class OverlayEntry:
    """One validated row of an expert-panel overlay CSV."""

    variation_id: int
    classification: str
    panel: str
    date: date | None


@dataclass(frozen=True)
class SubsetResult:
    """Outcome of :func:`fetch_clinvar_subset`."""

    path: Path
    file_date: str
    source: str
    total_records: int
    kept_records: int
    gene_counts: dict[str, int]


class ClinVarDownloadError(RuntimeError):
    """Downloading or subsetting a ClinVar VCF failed; the message says why and what to do."""


# ---------------------------------------------------------------------------
# Small helpers shared by reader, writer and subsetter
# ---------------------------------------------------------------------------


def parse_file_date(value: str | None) -> date | None:
    """Parse a ``##fileDate`` value (``2025-01-06`` as ClinVar writes it, or ``20250106``)."""
    text = (value or "").strip()
    for pattern in ("%Y-%m-%d", "%Y%m%d"):
        try:
            return datetime.strptime(text, pattern).date()
        except ValueError:
            continue
    return None


def geneinfo_symbols(geneinfo: str | None) -> list[str]:
    """Gene symbols of a GENEINFO value: ``"GCK:2645|LOC105375288:105375288"`` → ``["GCK", "LOC105375288"]``."""
    return [entry.split(":", 1)[0].strip() for entry in (geneinfo or "").split("|") if entry.strip()]


def panel_gene(chrom: str, pos: int, geneinfo: str | None) -> str | None:
    """The panel gene of a ClinVar record, or None when the record is outside the panel.

    A record belongs to the panel when GENEINFO lists a panel gene or its position lies in a
    padded panel region. With several panel genes listed (KCNJ11 and ABCC8 are neighbours), the
    one whose region contains the position wins; GENEINFO otherwise takes precedence.
    """
    region_gene = C.gene_for_position(chrom, pos)
    listed = [symbol for symbol in geneinfo_symbols(geneinfo) if symbol in C.GENE_INFO]
    if region_gene in listed or not listed:
        return region_gene
    return listed[0]


def readable_condition(clndn: str | None) -> str | None:
    """ClinVar CLNDN as display text: ``_`` → space, ``|``-separated conditions joined by ``; ``."""
    if not clndn:
        return None
    return "; ".join(part.replace("_", " ").strip() for part in clndn.split("|") if part.strip()) or None


def _info_text(value: Any) -> str | None:
    """A cyvcf2 INFO value as text (None for missing values and flags)."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, tuple):
        return ",".join(str(item) for item in value)
    return str(value)


def _int_or_none(value: Any) -> int | None:
    text = str(value).strip() if value is not None else ""
    return int(text) if _DIGITS.fullmatch(text) and len(text) <= 18 else None


def _open_binary_text(path: Path) -> IO[str]:
    """Open a plain or gzip/bgzip file as UTF-8 text (undecodable bytes are replaced)."""
    raw = path.open("rb")
    if raw.peek(2)[:2] == GZIP_MAGIC:
        return io.TextIOWrapper(gzip.GzipFile(fileobj=raw), encoding="utf-8", errors="replace")
    return io.TextIOWrapper(raw, encoding="utf-8", errors="replace")


# ---------------------------------------------------------------------------
# Reading ClinVar VCFs
# ---------------------------------------------------------------------------


def iter_clinvar_records(path: str | os.PathLike[str]) -> Iterator[ClinVarRecord]:
    """Yield panel records of a ClinVar VCF (multi-allelic split; ``.``/symbolic ALTs skipped).

    Records are kept when GENEINFO lists a panel gene or the position lies in a panel region
    (see :func:`panel_gene`). Alleles that are not plain bases (``.``, ``*``, ``<DEL>``,
    breakends) and records with a non-ACGTN REF are skipped. Raises ValueError when the file
    cannot be read as a VCF.
    """
    name = os.fspath(path)
    try:
        reader = cyvcf2.VCF(name)
    except OSError as exc:
        raise ValueError(f"{Path(name).name} could not be read as a VCF file ({exc}).") from None
    try:
        records = iter(reader)
        while True:
            try:
                record = next(records)
            except StopIteration:
                return
            except Exception as exc:  # noqa: BLE001 — cyvcf2 reports htslib failures with assorted types
                raise ValueError(f"{Path(name).name} could not be parsed as a VCF ({exc}).") from None
            yield from _split_record(record)
    finally:
        reader.close()


def _split_record(record: cyvcf2.Variant) -> Iterator[ClinVarRecord]:
    chrom = C.normalize_chrom(record.CHROM)
    info = record.INFO
    gene = panel_gene(chrom, record.POS, _info_text(info.get("GENEINFO")))
    ref = record.REF.upper()
    if gene is None or not _BASES.fullmatch(ref):
        return
    shared = {
        "variation_id": _int_or_none(record.ID),
        "allele_id": _int_or_none(info.get("ALLELEID")),
        "gene": gene,
        "clnsig": _info_text(info.get("CLNSIG")),
        "clnrevstat": _info_text(info.get("CLNREVSTAT")),
        "clndn": _info_text(info.get("CLNDN")),
        "clnhgvs": _info_text(info.get("CLNHGVS")),
        "mc": _info_text(info.get("MC")),
    }
    for alt in record.ALT:
        allele = alt.upper()
        if _BASES.fullmatch(allele):
            yield ClinVarRecord(chrom=chrom, pos=record.POS, ref=ref, alt=allele, **shared)


def read_vcf_header_meta(path: str | os.PathLike[str]) -> dict[str, Any]:
    """Return header metadata: ``fileDate``, ``source`` and the ``ge_ehr_synthetic`` flag.

    Also ``fileformat`` and ``reference``. Values are the first occurrence (None when absent);
    reading stops at the ``#CHROM`` line. Raises ValueError for unreadable files.
    """
    meta: dict[str, Any] = {"fileformat": None, "fileDate": None, "source": None, "reference": None,
                            "ge_ehr_synthetic": False}
    try:
        with _open_binary_text(Path(path)) as stream:
            while line := stream.readline(1 << 20):
                if not line.startswith("##"):
                    break
                key, separator, value = line[2:].rstrip("\r\n").partition("=")
                if not separator:
                    continue
                if key == "ge_ehr_synthetic":
                    meta["ge_ehr_synthetic"] = value.strip().lower() == "true"
                elif key in meta and meta[key] is None:
                    meta[key] = value.strip()
    except (OSError, EOFError, zlib.error) as exc:
        raise ValueError(f"{Path(path).name} could not be read ({exc}).") from None
    return meta


# ---------------------------------------------------------------------------
# Expert-panel overlay CSV
# ---------------------------------------------------------------------------


def load_expert_panel_overlay(path: str | os.PathLike[str]) -> dict[int, OverlayEntry]:
    """Read and validate an expert-panel overlay CSV, keyed by ClinVar variation ID.

    Columns ``clinvar_variation_id,classification,panel,date``; ``classification`` is a code
    (``P``, ``LP``, ``VUS``, ``LB``, ``B``) or label (``Likely pathogenic``); ``date`` is
    ``YYYY-MM-DD`` or empty. Raises ValueError naming the file, line and problem.
    """
    name = Path(path).name
    entries: dict[int, OverlayEntry] = {}
    try:
        with open(path, newline="", encoding="utf-8-sig") as handle:
            reader = csv.DictReader(handle)
            missing = [column for column in OVERLAY_COLUMNS if column not in (reader.fieldnames or [])]
            if missing:
                raise ValueError(
                    f"Expert-panel overlay {name}: missing column(s) {', '.join(missing)}; the header line "
                    f"must be '{','.join(OVERLAY_COLUMNS)}'."
                )
            for row in reader:
                entry = _overlay_entry(row, f"Expert-panel overlay {name}, line {reader.line_num}")
                previous = entries.get(entry.variation_id)
                if previous is not None and previous.classification != entry.classification:
                    raise ValueError(
                        f"Expert-panel overlay {name}, line {reader.line_num}: variation ID {entry.variation_id} "
                        f"is listed twice with different classifications ({previous.classification} and "
                        f"{entry.classification})."
                    )
                entries[entry.variation_id] = entry
    except UnicodeDecodeError:
        raise ValueError(f"Expert-panel overlay {name} is not a UTF-8 text CSV file.") from None
    except csv.Error as exc:
        raise ValueError(f"Expert-panel overlay {name} is not a valid CSV file ({exc}).") from None
    return entries


def _overlay_entry(row: dict[str | None, Any], where: str) -> OverlayEntry:
    if None in row:
        raise ValueError(f"{where}: the row has more columns than the header line.")
    values = {column: (row.get(column) or "").strip() for column in OVERLAY_COLUMNS}
    variation_id = _int_or_none(values["clinvar_variation_id"])
    if not variation_id:
        raise ValueError(
            f"{where}: clinvar_variation_id {values['clinvar_variation_id']!r} is not a positive whole number."
        )
    classification = classification_from_text(values["classification"])
    if classification is None:
        allowed = ", ".join(code for code in C.CLASSES if code != "NC")
        labels = ", ".join(label for code, label in C.CLASS_LABELS.items() if code != "NC")
        raise ValueError(
            f"{where}: unknown classification {values['classification']!r}; use one of {allowed} or {labels}."
        )
    if not values["panel"]:
        raise ValueError(f"{where}: the panel column is empty; name the expert panel behind the classification.")
    panel_date = None
    if values["date"]:
        try:
            panel_date = datetime.strptime(values["date"], "%Y-%m-%d").date()
        except ValueError:
            raise ValueError(f"{where}: date {values['date']!r} is not a valid YYYY-MM-DD date.") from None
    return OverlayEntry(variation_id, classification, values["panel"], panel_date)


# ---------------------------------------------------------------------------
# Import
# ---------------------------------------------------------------------------


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

    Creates the release and its KnownVariants with bulk inserts (VariantKeys are looked up and
    created in bulk too), computes ``record_count``/``gene_counts``/``class_counts`` and the file's
    sha256, takes ``source``/``is_synthetic`` from the header, defaults ``label`` to
    ``"ClinVar <fileDate>"`` and ``release_date`` to the fileDate. An optional overlay CSV
    (:func:`load_expert_panel_overlay`) overrides the class of matching variation IDs, sets
    ``is_expert_panel`` and ``classification_source="expert_panel_overlay"`` (review stars are
    raised to at least 3, the expert-panel level). Adds a ``release_imported`` audit entry and
    flushes; the caller commits.

    Raises ValueError for a file whose sha256 was already imported, a GRCh37 or non-VCF file,
    or an invalid overlay; FileNotFoundError when ``path`` does not exist.
    """
    started = time.perf_counter()
    file_path = Path(path)
    if not file_path.is_file():
        raise FileNotFoundError(f"ClinVar VCF not found: {file_path}")
    digest = _sha256(file_path)
    existing = db.session.execute(select(KbRelease).filter_by(file_sha256=digest)).scalar_one_or_none()
    if existing is not None:
        raise ValueError(
            f"{file_path.name} was already imported as release {existing.label!r} (id {existing.id}); "
            "each file can be imported only once."
        )
    meta = read_vcf_header_meta(file_path)
    _check_clinvar_header(meta, file_path.name)
    overlay = load_expert_panel_overlay(expert_panel_csv) if expert_panel_csv is not None else {}

    synthetic = bool(meta["ge_ehr_synthetic"])
    file_date = meta["fileDate"]
    release = KbRelease(
        label=label or f"ClinVar {file_date or file_path.name}",
        source="synthetic" if synthetic else "clinvar",
        is_synthetic=synthetic,
        release_date=release_date or parse_file_date(file_date),
        file_name=file_path.name,
        file_sha256=digest,
        file_date=file_date,
        imported_by=user,
        notes=notes,
        is_current=False,
    )
    db.session.add(release)
    db.session.flush()

    rows = _known_variant_rows(file_path, overlay)
    key_ids = _variant_key_ids({key: row["gene"] for key, row in rows.items()})
    for key, row in rows.items():
        row["release_id"] = release.id
        row["variant_key_id"] = key_ids[key]
    for batch in batched(rows.values(), INSERT_BATCH):
        db.session.execute(insert(KnownVariant), list(batch))

    gene_counts = Counter(row["gene"] for row in rows.values())
    class_counts = Counter(row["classification"] for row in rows.values())
    release.record_count = len(rows)
    release.gene_counts = {gene: gene_counts.get(gene, 0) for gene in C.GENES}
    release.class_counts = {cls: class_counts.get(cls, 0) for cls in C.CLASSES}
    overlay_applied = sum(row["classification_source"] == "expert_panel_overlay" for row in rows.values())
    if expert_panel_csv is not None:
        _report_overlay(release, overlay, rows.values(), overlay_applied, Path(expert_panel_csv).name)
    audit(
        "release_imported",
        "kb_release",
        release.id,
        detail={
            "label": release.label,
            "file_name": release.file_name,
            "file_sha256": digest,
            "file_date": file_date,
            "record_count": release.record_count,
            "is_synthetic": synthetic,
            "expert_panel_overlay": Path(expert_panel_csv).name if expert_panel_csv is not None else None,
            "expert_panel_overrides": overlay_applied,
        },
        user=user,
    )
    db.session.flush()
    logger.info(
        "Imported %s as release %r (id %d): %d records in %.0f ms",
        file_path.name,
        release.label,
        release.id,
        release.record_count,
        (time.perf_counter() - started) * 1000,
    )
    return release


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1 << 20):
            digest.update(chunk)
    return digest.hexdigest()


def _check_clinvar_header(meta: dict[str, Any], name: str) -> None:
    if not (meta["fileformat"] or "").startswith("VCF"):
        raise ValueError(f"{name} is not a VCF file: its first line must be '##fileformat=VCFv4.x'.")
    if meta["reference"] and GRCH37_NAME.search(meta["reference"]):
        raise ValueError(
            f"{name} is a GRCh37 file (##reference={meta['reference']}); GE-EHR needs the GRCh38 ClinVar VCF "
            "(vcf_GRCh38)."
        )


def _known_variant_rows(path: Path, overlay: dict[int, OverlayEntry]) -> dict[tuple[str, int, str, str], dict[str, Any]]:
    """KnownVariant insert rows keyed by variant key.

    A key listed twice keeps the record with more review stars (the first one on a tie).
    """
    rows: dict[tuple[str, int, str, str], dict[str, Any]] = {}
    duplicates = 0
    for record in iter_clinvar_records(path):
        key = (record.chrom, record.pos, record.ref, record.alt)
        row = _known_variant_row(record, overlay)
        previous = rows.get(key)
        if previous is not None:
            duplicates += 1
            if row["review_stars"] <= previous["review_stars"]:
                continue
        rows[key] = row
    if duplicates:
        logger.warning("%s lists %d variant key(s) more than once; kept the best-reviewed record", path.name, duplicates)
    return rows


def _known_variant_row(record: ClinVarRecord, overlay: dict[int, OverlayEntry]) -> dict[str, Any]:
    classification = normalize_clinvar_significance(record.clnsig)
    stars = review_stars(record.clnrevstat)
    expert_panel = is_expert_panel_status(record.clnrevstat)
    source = "clinvar"
    override = overlay.get(record.variation_id) if record.variation_id is not None else None
    if override is not None:
        classification, expert_panel, source, stars = override.classification, True, "expert_panel_overlay", max(stars, 3)
    return {
        "clinvar_variation_id": record.variation_id,
        "allele_id": record.allele_id,
        "gene": record.gene,
        "classification": classification,
        "clinvar_significance": record.clnsig,
        "review_status": record.clnrevstat,
        "review_stars": stars,
        "is_expert_panel": expert_panel,
        "classification_source": source,
        "condition": readable_condition(record.clndn),
        "mechanism": infer_mechanism(record.gene, record.clndn),
        "hgvs": record.clnhgvs,
        "molecular_consequence": record.mc,
    }


def _variant_key_ids(genes: dict[tuple[str, int, str, str], str | None]) -> dict[tuple[str, int, str, str], int]:
    """Bulk get-or-create of VariantKeys: ids for every key of ``genes`` (key → panel gene).

    Existing keys are found with chunked ``pos IN (...)`` queries (matched in Python); missing
    ones are inserted in batches with RETURNING. Existing keys without a gene get one, as in
    :meth:`VariantKey.get_or_create`.
    """
    found: dict[tuple[str, int, str, str], int] = {}
    genes_to_complete: dict[int, str] = {}
    for chunk in batched(sorted({key[1] for key in genes}), IN_CLAUSE_CHUNK):
        statement = select(VariantKey.id, VariantKey.chrom, VariantKey.pos, VariantKey.ref, VariantKey.alt,
                           VariantKey.gene).where(VariantKey.pos.in_(chunk))
        for row in db.session.execute(statement):
            key = (row.chrom, row.pos, row.ref, row.alt)
            if key in genes:
                found[key] = row.id
                if row.gene is None and genes[key]:
                    genes_to_complete[row.id] = genes[key]
    for chunk in batched(genes_to_complete, IN_CLAUSE_CHUNK):
        for variant_key in db.session.execute(select(VariantKey).where(VariantKey.id.in_(chunk))).scalars():
            variant_key.gene = genes_to_complete[variant_key.id]
    new_rows = [
        {"chrom": chrom, "pos": pos, "ref": ref, "alt": alt, "gene": gene}
        for (chrom, pos, ref, alt), gene in genes.items()
        if (chrom, pos, ref, alt) not in found
    ]
    statement = insert(VariantKey).returning(VariantKey.id, sort_by_parameter_order=True)
    for batch in batched(new_rows, INSERT_BATCH):
        ids = db.session.execute(statement, list(batch)).scalars().all()
        for row, key_id in zip(batch, ids, strict=True):
            found[(row["chrom"], row["pos"], row["ref"], row["alt"])] = key_id
    return found


def _report_overlay(
    release: KbRelease,
    overlay: dict[int, OverlayEntry],
    rows: Iterable[dict[str, Any]],
    applied: int,
    overlay_name: str,
) -> None:
    """Record the overlay's effect in the release notes; log variation IDs missing from the file."""
    unmatched = sorted(set(overlay) - {row["clinvar_variation_id"] for row in rows})
    if unmatched:
        logger.warning(
            "Expert-panel overlay %s: %d variation ID(s) are not in release %r (e.g. %d)",
            overlay_name,
            len(unmatched),
            release.label,
            unmatched[0],
        )
    panels = ", ".join(sorted({entry.panel for entry in overlay.values()})) or "no rows"
    note = f"Expert-panel overlay {overlay_name} ({panels}) applied to {applied} variant(s)."
    if unmatched:
        note += f" {len(unmatched)} overlay variation ID(s) were not found in this file."
    release.notes = f"{release.notes}\n{note}" if release.notes else note


# ---------------------------------------------------------------------------
# Writing ClinVar-format VCFs
# ---------------------------------------------------------------------------


def write_clinvar_vcf(
    records: Iterable[dict[str, Any] | ClinVarRecord],
    path: str | os.PathLike[str],
    *,
    file_date: str,
    synthetic: bool = True,
) -> None:
    """Write a ClinVar-format VCF 4.1 (gzip/bgzip when ``path`` ends in ``.gz``), sorted by position.

    ``synthetic=True`` adds the mandatory synthetic header lines (SPEC §0); ``synthetic=False``
    writes ``##source=ClinVar`` and is only for records derived from real ClinVar data. Records
    are dicts with :class:`ClinVarRecord` field names or ClinVarRecord objects; ``gene`` must be a
    panel gene (GENEINFO needs its NCBI Gene ID). INFO values are escaped: whitespace → ``_``,
    ``;`` → ``%3B``, ``=`` → ``%3D``. Output is deterministic (BGZF blocks carry no timestamp).
    Raises ValueError for an invalid record or fileDate.
    """
    if parse_file_date(file_date) is None:
        raise ValueError(f"fileDate {file_date!r} is not a YYYY-MM-DD date.")
    ordered = sorted((_as_record(item) for item in records), key=_record_sort_key)
    lines = _clinvar_header(file_date, synthetic, sorted({record.chrom for record in ordered}, key=_chrom_sort_key))
    lines.extend(_record_line(record) for record in ordered)
    target = Path(path)
    _write_vcf_text(target, "\n".join(lines) + "\n", bgzip=target.suffix == ".gz")


def _as_record(item: dict[str, Any] | ClinVarRecord) -> ClinVarRecord:
    names = {field.name for field in fields(ClinVarRecord)}
    values = asdict(item) if isinstance(item, ClinVarRecord) else dict(item)
    unknown = sorted(set(values) - names)
    if unknown:
        raise ValueError(f"Unknown ClinVar record field(s) {', '.join(unknown)}; expected {', '.join(sorted(names))}.")
    missing = [name for name in ("chrom", "pos", "ref", "alt") if values.get(name) in (None, "")]
    if missing:
        raise ValueError(f"ClinVar record {values!r} lacks {', '.join(missing)}.")
    chrom, pos, ref, alt = VariantKey.normalize(values["chrom"], values["pos"], values["ref"], values["alt"])
    if not (_BASES.fullmatch(ref) and _BASES.fullmatch(alt)):
        raise ValueError(f"ClinVar record {chrom}-{pos}-{ref}-{alt}: REF and ALT must consist of A, C, G, T, N.")
    return ClinVarRecord(**{**values, "chrom": chrom, "pos": pos, "ref": ref, "alt": alt})


def _chrom_sort_key(chrom: str) -> tuple[int, str]:
    if chrom.isdigit():
        return (int(chrom), "")
    return ({"X": 23, "Y": 24, "MT": 25}.get(chrom, 26), chrom)


def _record_sort_key(record: ClinVarRecord) -> tuple[tuple[int, str], int, str, str]:
    return (_chrom_sort_key(record.chrom), record.pos, record.ref, record.alt)


def _clinvar_header(file_date: str, synthetic: bool, chroms: list[str]) -> list[str]:
    lines = ["##fileformat=VCFv4.1", f"##fileDate={file_date}"]
    if synthetic:
        lines.append(C.SYNTHETIC_HEADER_LINES[1])
    else:
        lines.append("##source=ClinVar")
    lines.append("##reference=GRCh38")
    if synthetic:
        lines.append(C.SYNTHETIC_HEADER_LINES[0])
    lines.append('##ID=<Description="ClinVar Variation ID">')
    lines.extend(
        f'##INFO=<ID={key},Number={number},Type={kind},Description="{description}">'
        for key, number, kind, description in CLINVAR_INFO_DEFINITIONS
    )
    for chrom in chroms:
        length = C.GRCH38_CONTIG_LENGTHS.get(chrom)
        lines.append(f"##contig=<ID={chrom},length={length},assembly=GRCh38>" if length else f"##contig=<ID={chrom}>")
    lines.append("#CHROM\tPOS\tID\tREF\tALT\tQUAL\tFILTER\tINFO")
    return lines


def _record_line(record: ClinVarRecord) -> str:
    info = (
        ("ALLELEID", record.allele_id),
        ("CLNDN", record.clndn),
        ("CLNHGVS", record.clnhgvs),
        ("CLNREVSTAT", record.clnrevstat),
        ("CLNSIG", record.clnsig),
        ("GENEINFO", _geneinfo(record.gene)),
        ("MC", record.mc),
    )
    info_text = ";".join(f"{key}={escape_info_value(value)}" for key, value in info if value not in (None, ""))
    variation_id = str(record.variation_id) if record.variation_id is not None else "."
    return "\t".join((record.chrom, str(record.pos), variation_id, record.ref, record.alt, ".", ".", info_text or "."))


def escape_info_value(value: Any) -> str:
    """Escape an INFO value: whitespace → ``_`` (ClinVar style), ``;`` → ``%3B``, ``=`` → ``%3D``."""
    return _WHITESPACE.sub("_", str(value)).replace(";", "%3B").replace("=", "%3D")


def _geneinfo(gene: str | None) -> str | None:
    if gene is None:
        return None
    if gene not in NCBI_GENE_IDS:
        raise ValueError(f"Gene {gene!r} is not a panel gene; GENEINFO can only be written for {', '.join(C.GENES)}.")
    return f"{gene}:{NCBI_GENE_IDS[gene]}"


def _bgzf_blocks(data: bytes) -> Iterator[bytes]:
    """Compress ``data`` as BGZF: one gzip member per 65,280 input bytes, then the EOF block."""
    for start in range(0, len(data), _BGZF_BLOCK_INPUT):
        chunk = data[start : start + _BGZF_BLOCK_INPUT]
        compressor = zlib.compressobj(6, zlib.DEFLATED, -15)
        deflated = compressor.compress(chunk) + compressor.flush()
        # ID1 ID2 CM FLG(FEXTRA) MTIME XFL OS XLEN | SI1='B' SI2='C' SLEN=2 BSIZE (total size - 1)
        header = struct.pack("<4BI2BH2BHH", 0x1F, 0x8B, 8, 4, 0, 0, 0xFF, 6, 66, 67, 2, len(deflated) + 25)
        yield header + deflated + struct.pack("<II", zlib.crc32(chunk), len(chunk))
    yield _BGZF_EOF


def _write_vcf_text(path: Path, text: str, *, bgzip: bool) -> None:
    """Write ``text`` as UTF-8, BGZF-compressed when ``bgzip`` is true (parent folders are created)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    data = text.encode("utf-8")
    with path.open("wb") as handle:
        if bgzip:
            for block in _bgzf_blocks(data):
                handle.write(block)
        else:
            handle.write(data)


# ---------------------------------------------------------------------------
# Release diff
# ---------------------------------------------------------------------------

_UNCLASSIFIED: tuple[str, str, str | None] = ("NC", "unknown", None)


def diff_releases(
    old: KbRelease | None, new: KbRelease, variant_key_ids: Iterable[int] | None = None
) -> list[KeyChange]:
    """Keys whose class or mechanism differs between ``old`` (None ⇒ all NC) and ``new``.

    Restricted to ``variant_key_ids`` when given (otherwise every key of either release). Keys
    absent from a release count as NC/unknown. The category comes from
    :func:`~ge_ehr.constants.categorize_change`; a mechanism-only change is
    ``within_actionable`` or ``within_non_actionable`` by the (unchanged) class. One bulk lookup
    per release; ordered by variant key id.
    """
    key_ids = None if variant_key_ids is None else set(variant_key_ids)
    if key_ids is not None and not key_ids:
        return []
    new_classes = _release_classes(new, key_ids)
    old_classes = _release_classes(old, key_ids) if old is not None else {}
    candidates = key_ids if key_ids is not None else new_classes.keys() | old_classes.keys()
    changes: list[KeyChange] = []
    for key_id in sorted(candidates):
        old_class, old_mechanism, old_gene = old_classes.get(key_id, _UNCLASSIFIED)
        new_class, new_mechanism, new_gene = new_classes.get(key_id, _UNCLASSIFIED)
        if old_class != new_class:
            category = C.categorize_change(old_class, new_class)
        elif old_mechanism != new_mechanism:
            category = "within_actionable" if C.is_actionable(new_class) else "within_non_actionable"
        else:
            continue
        changes.append(
            KeyChange(key_id, new_gene or old_gene, old_class, new_class, old_mechanism, new_mechanism, category)
        )
    return changes


def _release_classes(release: KbRelease, key_ids: set[int] | None) -> dict[int, tuple[str, str, str | None]]:
    """``variant_key_id → (classification, mechanism, gene)`` of a release (optionally some keys)."""
    statement = select(
        KnownVariant.variant_key_id, KnownVariant.classification, KnownVariant.mechanism, KnownVariant.gene
    ).where(KnownVariant.release_id == release.id)
    if key_ids is None:
        rows: Iterable[Any] = db.session.execute(statement)
    else:
        rows = chain.from_iterable(
            db.session.execute(statement.where(KnownVariant.variant_key_id.in_(chunk)))
            for chunk in batched(sorted(key_ids), IN_CLAUSE_CHUNK)
        )
    return {row.variant_key_id: (row.classification, row.mechanism, row.gene) for row in rows}


# ---------------------------------------------------------------------------
# Downloading real ClinVar releases (stdlib only: urllib + gzip)
# ---------------------------------------------------------------------------


def clinvar_vcf_url(archive_date: str | None = None) -> str:
    """URL of the current ClinVar GRCh38 VCF, or of the archived one for ``archive_date`` (YYYYMMDD)."""
    if archive_date is None:
        return f"{CLINVAR_GRCH38_BASE_URL}/clinvar.vcf.gz"
    day = parse_file_date(archive_date) if _DIGITS.fullmatch(archive_date) else None
    if day is None:
        raise ValueError(f"Archive date {archive_date!r} must be a date written as YYYYMMDD, e.g. 20250106.")
    return f"{CLINVAR_GRCH38_BASE_URL}/archive_2.0/{day.year}/clinvar_{archive_date}.vcf.gz"


def subset_file_name(file_date: str) -> str:
    """``clinvar_<YYYYMMDD>_7genes.vcf.gz`` for a ``##fileDate`` value."""
    day = parse_file_date(file_date)
    if day is None:
        raise ValueError(f"fileDate {file_date!r} is not a date.")
    return f"clinvar_{day:%Y%m%d}_7genes.vcf.gz"


def fetch_clinvar_subset(
    source: str | os.PathLike[str], output_dir: str | os.PathLike[str], *, newer_than: date | None = None
) -> SubsetResult | None:
    """Stream a ClinVar GRCh38 VCF and write its 7-gene subset to ``output_dir``.

    ``source`` is a URL (``https://``, ``ftp://``, ``file://``) or a local path; plain and
    gzip/bgzip input are accepted. Records are kept verbatim when GENEINFO lists a panel gene or
    the position lies in a panel region; a ``##ge_ehr_subset`` provenance line is added. The
    output (``clinvar_<YYYYMMDD>_7genes.vcf.gz``, bgzip) is written to a ``.part`` file and renamed
    when complete, so an interrupted run never leaves a truncated subset. Returns None without
    writing anything when ``newer_than`` is given and the file's fileDate is not later.
    Raises :class:`ClinVarDownloadError` with an actionable message.
    """
    location = os.fspath(source)
    with _open_source(location) as stream:
        return _subset_stream(stream, Path(output_dir), location, newer_than)


def _open_source(location: str) -> BinaryIO:
    if not _URL.match(location):
        try:
            return open(location, "rb")
        except OSError as exc:
            raise ClinVarDownloadError(f"Cannot open {location}: {exc.strerror or exc}.") from None
    request = urllib.request.Request(location, headers={"User-Agent": _USER_AGENT})
    try:
        return urllib.request.urlopen(request, timeout=DOWNLOAD_TIMEOUT_SECONDS)  # noqa: S310 — URL given by the operator
    except urllib.error.HTTPError as exc:
        hint = ""
        if exc.code == 404:
            hint = (
                " Archived releases exist only for specific dates; see "
                f"{CLINVAR_GRCH38_BASE_URL}/archive_2.0/ for the available files."
            )
        raise ClinVarDownloadError(f"Download of {location} failed: HTTP {exc.code} {exc.reason}.{hint}") from None
    except (urllib.error.URLError, OSError) as exc:
        reason = getattr(exc, "reason", exc)
        raise ClinVarDownloadError(
            f"Could not reach {location}: {reason}. NCBI may be unreachable from this network; download the "
            "file on a machine with internet access and pass the local file instead (--input)."
        ) from None


def _subset_stream(stream: BinaryIO, output_dir: Path, location: str, newer_than: date | None) -> SubsetResult | None:
    name = location.rstrip("/").rsplit("/", 1)[-1]
    reader = io.BufferedReader(stream) if not hasattr(stream, "peek") else stream
    try:
        binary = gzip.GzipFile(fileobj=reader) if reader.peek(2)[:2] == GZIP_MAGIC else reader
        lines = io.TextIOWrapper(binary, encoding="utf-8")
        header = _read_subset_header(lines, name)
        file_date = _subset_file_date(header, name)
        if newer_than is not None and parse_file_date(file_date) <= newer_than:
            logger.info("%s (fileDate %s) is not newer than %s; skipped", name, file_date, newer_than)
            return None
        kept, total, gene_counts = _filter_subset_records(lines, name, len(header))
    except (EOFError, zlib.error, gzip.BadGzipFile):
        raise ClinVarDownloadError(f"{name} is truncated or not a valid gzip file; download it again.") from None
    except UnicodeDecodeError:
        raise ClinVarDownloadError(f"{name} is not a text VCF file.") from None
    except (TimeoutError, ConnectionError, http.client.HTTPException, urllib.error.URLError) as exc:
        raise ClinVarDownloadError(f"The download of {name} was interrupted ({exc}); nothing was written. Run it again.") from None

    provenance = (
        f"##{_SUBSET_HEADER_KEY}={','.join(C.GENES)} (GENEINFO or GRCh38 panel region) from {name}; "
        f"{len(kept)} of {total} records"
    )
    text = "\n".join([*header[:-1], provenance, header[-1], *kept]) + "\n"
    target = output_dir / subset_file_name(file_date)
    partial = target.with_name(target.name + ".part")
    try:
        _write_vcf_text(partial, text, bgzip=True)
        os.replace(partial, target)
    except BaseException:
        partial.unlink(missing_ok=True)
        raise
    logger.info("Wrote %s: %d of %d ClinVar records in the panel", target, len(kept), total)
    return SubsetResult(target, file_date, location, total, len(kept), {gene: gene_counts.get(gene, 0) for gene in C.GENES})


def _read_subset_header(lines: IO[str], name: str) -> list[str]:
    """Read the header (``##`` lines and the ``#CHROM`` line) of a ClinVar VCF."""
    header: list[str] = []
    for line in lines:
        text = line.rstrip("\r\n")
        if not header and not text.startswith("##fileformat=VCF"):
            raise ClinVarDownloadError(f"{name} is not a VCF file (its first line is not '##fileformat=VCF...').")
        header.append(text)
        if text.startswith("#CHROM"):
            return header
        if not text.startswith("##"):
            break
    raise ClinVarDownloadError(f"{name} has no '#CHROM' header line; the file is incomplete or not a VCF.")


def _subset_file_date(header: list[str], name: str) -> str:
    values = dict(line[2:].partition("=")[::2] for line in header if line.startswith("##") and "=" in line)
    reference = values.get("reference", "")
    if GRCH37_NAME.search(reference):
        raise ClinVarDownloadError(f"{name} is a GRCh37 ClinVar file; use the GRCh38 release (vcf_GRCh38).")
    file_date = values.get("fileDate", "").strip()
    if parse_file_date(file_date) is None:
        raise ClinVarDownloadError(f"{name} has no valid ##fileDate header line; is it a ClinVar VCF?")
    return file_date


def _filter_subset_records(lines: IO[str], name: str, header_lines: int) -> tuple[list[str], int, Counter[str]]:
    kept: list[str] = []
    gene_counts: Counter[str] = Counter()
    total = 0
    for number, line in enumerate(lines, start=header_lines + 1):
        text = line.rstrip("\r\n")
        if not text:
            continue
        total += 1
        columns = text.split("\t", 8)
        if len(columns) < 8 or not _DIGITS.fullmatch(columns[1]) or not columns[0]:
            raise ClinVarDownloadError(f"{name}, line {number}: not a valid VCF record.")
        geneinfo = _GENEINFO.search(columns[7])
        gene = panel_gene(columns[0], int(columns[1]), geneinfo.group(1) if geneinfo else None)
        if gene is not None:
            kept.append(text)
            gene_counts[gene] += 1
    return kept, total, gene_counts
