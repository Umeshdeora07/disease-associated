"""Tests for the ClinVar knowledge base (``ge_ehr.services.clinvar``, SPEC §5.3) and the demo
releases built by ``scripts/build_demo_releases.py`` (SPEC §11)."""

from __future__ import annotations

import gzip
import hashlib
import importlib.util
import random
import sys
import time
from collections import Counter
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import date
from pathlib import Path
from types import ModuleType
from typing import Any

import cyvcf2
import pytest
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import event, func, insert, select

from ge_ehr import constants as C
from ge_ehr.models import AuditLog, KbRelease, KnownVariant, VariantKey
from ge_ehr.services import clinvar
from ge_ehr.services.classification import normalize_clinvar_significance
from ge_ehr.services.clinvar import (
    ClinVarRecord,
    KeyChange,
    diff_releases,
    escape_info_value,
    geneinfo_symbols,
    import_release,
    iter_clinvar_records,
    load_expert_panel_overlay,
    panel_gene,
    parse_file_date,
    read_vcf_header_meta,
    readable_condition,
    unescape_info_value,
    write_clinvar_vcf,
)
from ge_ehr.services.vcf_parser import BGZF_EOF
from tests.factories import add_known_variant, make_release, make_user

REPO_ROOT = Path(__file__).resolve().parent.parent
FORMAT_FIXTURE = Path(__file__).parent / "data" / "clinvar_format_sample.vcf"
DEMO_DIR = REPO_ROOT / "data" / "demo" / "clinvar"
DEMO_RELEASE_1 = DEMO_DIR / "demo_release_1.vcf.gz"
DEMO_RELEASE_2 = DEMO_DIR / "demo_release_2.vcf.gz"
OVERLAY_EXAMPLE = REPO_ROOT / "data" / "demo" / "expert_panel_overlay_example.csv"

# (chrom, pos, ref, alt, variation_id, gene, class, mechanism, stars, expert panel) of the
# 18 panel records of the format fixture; 5 of its 23 records are outside the panel or have no ALT.
FIXTURE_RECORDS = [
    ("7", 44139500, "G", "T", 9100002, "GCK", "VUS", "unknown", 1, False),
    ("7", 44141010, "C", "T", 9100003, "GCK", "LB", "unknown", 1, False),
    ("7", 44146321, "C", "T", 9100004, "GCK", "P", "unknown", 3, True),
    ("7", 44147002, "CTGA", "C", 9100005, "GCK", "LP", "unknown", 2, False),
    ("7", 44151234, "G", "A", 9100006, "GCK", "VUS", "unknown", 1, False),
    ("7", 44160000, "T", "C", 9100008, "GCK", "NC", "unknown", 0, False),
    ("11", 2159000, "G", "A", 9100009, "INS", "LP", "unknown", 1, False),
    ("11", 17388500, "C", "T", 9100011, "KCNJ11", "P", "activating", 2, False),
    ("11", 17389900, "G", "A", 9100012, "KCNJ11", "P", "inactivating", 1, False),
    ("11", 17420000, "A", "G", 9100013, "ABCC8", "LP", "unknown", 1, False),
    ("11", 17430000, "T", "G", 9100014, "ABCC8", "P", "activating", 4, True),
    ("11", 17470000, "C", "A", 9100015, "ABCC8", "LB", "unknown", 2, False),
    ("12", 120975000, "G", "C", 9100016, "HNF1A", "P", "unknown", 3, True),
    ("12", 120988888, "T", "C", 9100017, "HNF1A", "VUS", "unknown", 1, False),
    ("12", 121000000, "A", "AT", 9100018, "HNF1A", "LP", "unknown", 1, False),
    ("17", 37700123, "G", "A", 9100019, "HNF1B", "B", "unknown", 2, False),
    ("20", 44405000, "C", "G", 9100020, "HNF4A", "NC", "unknown", 0, False),
    ("20", 44420000, "A", "G", 9100021, "HNF4A", "VUS", "unknown", 1, False),
]

# SPEC §11: code → (key, gene, R1 class, R2 class)
DEMO_TABLE = {
    "V01": ("12-120978900-G-A", "HNF1A", "P", "P"),
    "V02": ("7-44145200-C-T", "GCK", "P", "P"),
    "V03": ("20-44401300-C-T", "HNF4A", "LP", "P"),
    "V04": ("11-17387100-C-T", "KCNJ11", "P", "P"),
    "V05": ("11-2160500-G-A", "INS", "LP", "LP"),
    "V06": ("12-120994300-C-T", "HNF1A", "VUS", "LP"),
    "V07": ("7-44150500-G-A", "GCK", "LP", "VUS"),
    "V08": ("12-120999100-A-G", "HNF1A", "VUS", "LB"),
    "V09": ("11-17410300-G-A", "ABCC8", "P", "P"),
    "V10": ("20-44410200-G-T", "HNF4A", "VUS", "LP"),
    "V11": ("7-44160100-T-C", "GCK", "VUS", "P"),
    "B01": ("12-121001000-C-T", "HNF1A", "B", "B"),
    "B02": ("7-44180000-A-G", "GCK", "LB", "LB"),
    "B03": ("17-37700000-G-A", "HNF1B", "B", "B"),
    "B04": ("11-17450000-C-T", "ABCC8", "LB", "LB"),
}


@contextmanager
def count_queries(db: SQLAlchemy) -> Iterator[list[str]]:
    statements: list[str] = []

    def record(conn, cursor, statement, parameters, context, executemany) -> None:  # noqa: ANN001, ARG001
        statements.append(statement)

    event.listen(db.engine, "before_cursor_execute", record)
    try:
        yield statements
    finally:
        event.remove(db.engine, "before_cursor_execute", record)


def cv(chrom: str, pos: int, ref: str = "C", alt: str = "T", **fields: Any) -> dict[str, Any]:
    """A ClinVar record dict for :func:`write_clinvar_vcf` (gene defaults to the panel gene)."""
    return {"chrom": chrom, "pos": pos, "ref": ref, "alt": alt, "gene": C.gene_for_position(chrom, pos), **fields}


def known(db: SQLAlchemy, release: KbRelease) -> dict[str, KnownVariant]:
    rows = db.session.execute(
        select(KnownVariant).where(KnownVariant.release_id == release.id).join(KnownVariant.variant_key)
    ).scalars()
    return {row.variant_key.label: row for row in rows}


def release_count(db: SQLAlchemy) -> int:
    return db.session.scalar(select(func.count(KbRelease.id)))


def load_script(name: str, monkeypatch: pytest.MonkeyPatch) -> ModuleType:
    """Import ``scripts/<name>.py`` (not a package) as a module registered for this test only."""
    module_name = f"ge_ehr_script_{name}"
    spec = importlib.util.spec_from_file_location(module_name, REPO_ROOT / "scripts" / f"{name}.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, module_name, module)  # dataclasses resolve annotations through it
    spec.loader.exec_module(module)
    return module


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("value", "expected"),
    [("2025-01-06", date(2025, 1, 6)), ("20250106", date(2025, 1, 6)), (" 2026-01-05 ", date(2026, 1, 5)),
     ("2025-13-01", None), ("Jan 2025", None), ("", None), (None, None)],
)
def test_parse_file_date(value: str | None, expected: date | None) -> None:
    assert parse_file_date(value) == expected


def test_geneinfo_symbols() -> None:
    assert geneinfo_symbols("GCK:2645") == ["GCK"]
    assert geneinfo_symbols("INS-IGF2:723961|INS:3630") == ["INS-IGF2", "INS"]
    assert geneinfo_symbols("") == [] and geneinfo_symbols(None) == []


@pytest.mark.parametrize(
    ("chrom", "pos", "geneinfo", "gene"),
    [
        ("7", 44145200, "GCK:2645", "GCK"),
        ("chr7", 44145200, None, "GCK"),  # region only
        ("7", 44141010, "YKT6:10652", "GCK"),  # region wins over a non-panel GENEINFO gene
        ("7", 44139500, "GCK:2645", "GCK"),  # outside the padded region, listed in GENEINFO
        ("11", 17388500, "KCNJ11:3767|ABCC8:6833", "KCNJ11"),  # several panel genes: the region decides
        ("11", 17420000, "KCNJ11:3767|ABCC8:6833", "ABCC8"),
        ("11", 17420000, "KCNJ11:3767", "KCNJ11"),  # GENEINFO takes precedence over the region
        ("11", 2159000, "INS-IGF2:723961|INS:3630", "INS"),
        ("11", 2170000, "INS-IGF2:723961|IGF2:3481", None),
        ("1", 1000000, "AGRN:375790", None),
        ("1", 1000000, None, None),
    ],
)
def test_panel_gene(chrom: str, pos: int, geneinfo: str | None, gene: str | None) -> None:
    assert panel_gene(chrom, pos, geneinfo) == gene


def test_readable_condition() -> None:
    assert readable_condition("Permanent_neonatal_diabetes_mellitus") == "Permanent neonatal diabetes mellitus"
    assert readable_condition("MODY_type_2|not_provided") == "MODY type 2; not provided"
    assert readable_condition("Hyperinsulinemic_hypoglycemia,_familial,_2") == "Hyperinsulinemic hypoglycemia, familial, 2"
    assert readable_condition("|") is None and readable_condition("") is None and readable_condition(None) is None


def test_info_value_escaping_round_trips() -> None:
    raw = "50% risk; see note=1\tand more"
    escaped = escape_info_value(raw)
    assert escaped == "50%25_risk%3B_see_note%3D1_and_more"
    assert ";" not in escaped and "=" not in escaped and " " not in escaped
    assert unescape_info_value(escaped) == "50%_risk;_see_note=1_and_more"
    assert unescape_info_value("a%2Cb%3ac%09") == "a,b:c\t"
    assert unescape_info_value("100%_sure") == "100%_sure"


# ---------------------------------------------------------------------------
# Reading ClinVar VCFs
# ---------------------------------------------------------------------------


def test_iter_clinvar_records_reads_the_real_format_fixture() -> None:
    records = list(iter_clinvar_records(FORMAT_FIXTURE))
    assert [(r.chrom, r.pos, r.ref, r.alt, r.variation_id, r.gene) for r in records] == [
        row[:6] for row in FIXTURE_RECORDS
    ]
    by_id = {record.variation_id: record for record in records}
    assert by_id[9100011] == ClinVarRecord(
        chrom="11",
        pos=17388500,
        ref="C",
        alt="T",
        variation_id=9100011,
        allele_id=9200011,
        gene="KCNJ11",
        clnsig="Pathogenic|risk_factor",
        clnrevstat="criteria_provided,_multiple_submitters,_no_conflicts",
        clndn="Permanent_neonatal_diabetes_mellitus|Diabetes_mellitus,_transient_neonatal,_3",
        clnhgvs="NC_000011.10:g.17388500C>T",
        mc="SO:0001583|missense_variant",
    )
    assert by_id[9100009].mc == "SO:0001583|missense_variant,SO:0001627|intron_variant"
    assert by_id[9100006].clnsig == "Conflicting_classifications_of_pathogenicity"
    assert by_id[9100005].clnhgvs == "NC_000007.14:g.44147003_44147005del"
    skipped = {9100001, 9100007, 9100010, 9100022, 9100023}  # off panel, ALT ".", MT, X
    assert skipped.isdisjoint(by_id)


def test_iter_clinvar_records_splits_multi_allelic_and_skips_unusable_alleles(tmp_path: Path) -> None:
    path = tmp_path / "multi.vcf"
    path.write_text(
        "##fileformat=VCFv4.1\n##fileDate=2025-02-01\n" + "\n".join(C.SYNTHETIC_HEADER_LINES) + "\n"
        '##INFO=<ID=CLNSIG,Number=.,Type=String,Description="x">\n'
        '##INFO=<ID=GENEINFO,Number=1,Type=String,Description="x">\n'
        "#CHROM\tPOS\tID\tREF\tALT\tQUAL\tFILTER\tINFO\n"
        "chr7\t44145200\t9900001\tc\tt,G,<DEL>,*\t.\t.\tCLNSIG=Pathogenic;GENEINFO=GCK:2645\n"
        "7\t44145300\t.\tN\tA\t.\t.\tCLNSIG=Benign\n"
        "7\t44145400\t9900003\tR\tA\t.\t.\tCLNSIG=Benign\n"
        "7\t44145500\tnot-a-number\tA\tG\t.\t.\t.\n"
    )
    records = list(iter_clinvar_records(path))
    assert [(r.chrom, r.pos, r.ref, r.alt, r.variation_id, r.clnsig) for r in records] == [
        ("7", 44145200, "C", "T", 9900001, "Pathogenic"),
        ("7", 44145200, "C", "G", 9900001, "Pathogenic"),
        ("7", 44145300, "N", "A", None, "Benign"),
        ("7", 44145500, "A", "G", None, None),
    ]
    assert records[-1].allele_id is None and records[-1].gene == "GCK"


def test_numeric_multi_value_info_fields_are_read_as_text(tmp_path: Path) -> None:
    path = tmp_path / "numbers.vcf"
    path.write_text(
        "##fileformat=VCFv4.1\n" + "\n".join(C.SYNTHETIC_HEADER_LINES) + "\n"
        '##INFO=<ID=ALLELEID,Number=.,Type=Integer,Description="x">\n'
        '##INFO=<ID=MC,Number=.,Type=Integer,Description="x">\n'
        "#CHROM\tPOS\tID\tREF\tALT\tQUAL\tFILTER\tINFO\n"
        "7\t44145200\t9900001\tC\tT\t.\t.\tALLELEID=7,8;MC=1,2\n"
    )
    (record,) = iter_clinvar_records(path)
    assert (record.allele_id, record.mc) == (None, "1,2")


def test_iter_clinvar_records_rejects_unreadable_and_truncated_files(tmp_path: Path) -> None:
    not_vcf = tmp_path / "notes.vcf"
    not_vcf.write_text("this is not a VCF\n")
    with pytest.raises(ValueError, match="notes.vcf could not be read as a VCF file"):
        list(iter_clinvar_records(not_vcf))

    data = DEMO_RELEASE_1.read_bytes()
    cut = tmp_path / "cut.vcf.gz"
    cut.write_bytes(data[: -len(BGZF_EOF)])
    with pytest.raises(ValueError, match="cut.vcf.gz is incomplete: its bgzip end-of-file marker is missing"):
        list(iter_clinvar_records(cut))

    middle = tmp_path / "middle.vcf.gz"
    middle.write_bytes(data[: len(data) // 2] + BGZF_EOF)
    with pytest.raises(ValueError, match="middle.vcf.gz could not be read completely: the file is truncated"):
        list(iter_clinvar_records(middle))


def test_read_vcf_header_meta(tmp_path: Path) -> None:
    assert read_vcf_header_meta(FORMAT_FIXTURE) == {
        "fileformat": "VCFv4.1",
        "fileDate": "2024-06-30",
        "source": C.SYNTHETIC_HEADER_LINES[1].removeprefix("##source="),
        "reference": "GRCh38",
        "ge_ehr_synthetic": True,
    }
    assert read_vcf_header_meta(DEMO_RELEASE_2)["fileDate"] == "2026-01-05"
    bare = tmp_path / "bare.vcf"
    bare.write_text("##fileformat=VCFv4.2\n##comment\n##ge_ehr_synthetic=no\n#CHROM\tPOS\n##fileDate=2025-01-01\n")
    assert read_vcf_header_meta(bare) == {
        "fileformat": "VCFv4.2", "fileDate": None, "source": None, "reference": None, "ge_ehr_synthetic": False
    }
    broken = tmp_path / "broken.vcf.gz"
    broken.write_bytes(gzip.compress(b"##fileformat=VCFv4.1\n" * 1000)[:60])
    with pytest.raises(ValueError, match="broken.vcf.gz could not be read"):
        read_vcf_header_meta(broken)


# ---------------------------------------------------------------------------
# Writing ClinVar-format VCFs
# ---------------------------------------------------------------------------


def test_write_clinvar_vcf_header_and_record_format(tmp_path: Path) -> None:
    path = tmp_path / "release.vcf"
    write_clinvar_vcf(
        [
            ClinVarRecord("chr11", 17387100, "c", "t", 9000004, 10000004, "KCNJ11", "Pathogenic",
                          "criteria provided, multiple submitters, no conflicts", "Permanent neonatal diabetes mellitus",
                          "NC_000011.10:g.17387100C>T", "SO:0001583|missense_variant"),
            cv("7", 44145200, variation_id=9000002, clnsig="Pathogenic", clnrevstat="reviewed by expert panel"),
        ],
        path,
        file_date="2025-01-06",
    )
    lines = path.read_text().splitlines()
    header = [line for line in lines if line.startswith("##")]
    assert header[:5] == [
        "##fileformat=VCFv4.1",
        "##fileDate=2025-01-06",
        "##source=GE-EHR synthetic ClinVar-format release (NOT real ClinVar data)",
        "##reference=GRCh38",
        "##ge_ehr_synthetic=true",
    ]
    assert set(C.SYNTHETIC_HEADER_LINES) <= set(header)
    assert '##ID=<Description="ClinVar Variation ID">' in header
    info_ids = [line.split(",")[0].removeprefix("##INFO=<ID=") for line in header if line.startswith("##INFO")]
    assert info_ids == ["ALLELEID", "CLNDN", "CLNHGVS", "CLNREVSTAT", "CLNSIG", "GENEINFO", "MC"]
    assert "##contig=<ID=7,length=159345973,assembly=GRCh38>" in header
    assert "##contig=<ID=11,length=135086622,assembly=GRCh38>" in header
    assert lines[len(header)] == "#CHROM\tPOS\tID\tREF\tALT\tQUAL\tFILTER\tINFO"
    assert lines[len(header) + 1 :] == [
        "7\t44145200\t9000002\tC\tT\t.\t.\tCLNREVSTAT=reviewed_by_expert_panel;CLNSIG=Pathogenic;GENEINFO=GCK:2645",
        "11\t17387100\t9000004\tC\tT\t.\t.\tALLELEID=10000004;CLNDN=Permanent_neonatal_diabetes_mellitus;"
        "CLNHGVS=NC_000011.10:g.17387100C>T;CLNREVSTAT=criteria_provided,_multiple_submitters,_no_conflicts;"
        "CLNSIG=Pathogenic;GENEINFO=KCNJ11:3767;MC=SO:0001583|missense_variant",
    ]


def test_write_clinvar_vcf_without_synthetic_flag(tmp_path: Path) -> None:
    path = tmp_path / "derived.vcf"
    write_clinvar_vcf([cv("7", 44145200)], path, file_date="2025-01-06", synthetic=False)
    header = path.read_text().splitlines()[:4]
    assert header[2] == "##source=ClinVar"
    assert not any(line in path.read_text() for line in C.SYNTHETIC_HEADER_LINES)


def test_write_clinvar_vcf_sorts_by_chromosome_then_position_deterministically(tmp_path: Path) -> None:
    records = [
        cv("20", 44401300), cv("7", 44145200, "C", "G"), cv("11", 2160500), cv("7", 44145200, "C", "A"),
        cv("12", 120978900), cv("X", 100, gene=None), cv("MT", 3243, gene=None), cv("11", 17387100), cv("17", 37700000),
    ]
    first, second = tmp_path / "a.vcf.gz", tmp_path / "b.vcf.gz"
    write_clinvar_vcf(records, first, file_date="2025-01-06")
    shuffled = records[:]
    random.Random(3).shuffle(shuffled)
    write_clinvar_vcf(shuffled, second, file_date="2025-01-06")
    assert first.read_bytes() == second.read_bytes()
    body = [line.split("\t")[:5] for line in gzip.decompress(first.read_bytes()).decode().splitlines() if line[0] != "#"]
    assert [f"{c}-{p}-{r}-{a}" for c, p, _, r, a in body] == [
        "7-44145200-C-A", "7-44145200-C-G", "11-2160500-C-T", "11-17387100-C-T", "12-120978900-C-T",
        "17-37700000-C-T", "20-44401300-C-T", "X-100-C-T", "MT-3243-C-T",
    ]


def test_write_clinvar_vcf_gz_is_bgzf_readable_by_htslib_and_gzip(tmp_path: Path) -> None:
    rng = random.Random(11)
    records = [
        cv("12", 120_970_000 + i, clnsig="Uncertain significance", clndn="x" * rng.randint(10, 200),
           variation_id=9_500_000 + i)
        for i in range(3000)
    ]
    path = tmp_path / "big.vcf.gz"
    write_clinvar_vcf(records, path, file_date="2025-03-01")
    data = path.read_bytes()
    assert data[:4] == b"\x1f\x8b\x08\x04" and data[12:14] == b"BC" and data.endswith(BGZF_EOF)
    assert data.count(b"\x1f\x8b\x08\x04\x00\x00\x00\x00\x00\xff\x06\x00BC") > 2  # several blocks
    assert sum(not line.startswith("#") for line in gzip.decompress(data).decode().splitlines()) == 3000
    reader = cyvcf2.VCF(str(path))
    assert sum(1 for _ in reader) == 3000
    reader.close()


def test_written_records_round_trip_through_the_reader(tmp_path: Path) -> None:
    original = [
        ClinVarRecord("7", 44145200, "C", "T", 9000002, 10000002, "GCK", "Pathogenic", "reviewed_by_expert_panel",
                      "Maturity-onset diabetes of the young type 2|Hyperglycemia", "NC_000007.14:g.44145200C>T",
                      "SO:0001583|missense_variant"),
        ClinVarRecord("11", 17410300, "GA", "G", None, None, "ABCC8", "Likely pathogenic; low=penetrance 5%",
                      None, None, None, None),
    ]
    path = tmp_path / "round.vcf.gz"
    write_clinvar_vcf(original, path, file_date="2025-01-06")
    back = list(iter_clinvar_records(path))
    assert back[0] == ClinVarRecord(
        "7", 44145200, "C", "T", 9000002, 10000002, "GCK", "Pathogenic", "reviewed_by_expert_panel",
        "Maturity-onset_diabetes_of_the_young_type_2|Hyperglycemia", "NC_000007.14:g.44145200C>T",
        "SO:0001583|missense_variant",
    )
    assert back[1] == ClinVarRecord("11", 17410300, "GA", "G", gene="ABCC8", clnsig="Likely_pathogenic;_low=penetrance_5%")


def test_record_without_info_values_gets_a_dot(tmp_path: Path) -> None:
    path = tmp_path / "bare.vcf"
    write_clinvar_vcf([{"chrom": "7", "pos": 44145200, "ref": "C", "alt": "T"}], path, file_date="2025-01-06")
    assert path.read_text().splitlines()[-1] == "7\t44145200\t.\tC\tT\t.\t.\t."


@pytest.mark.parametrize(
    ("record", "file_date", "message"),
    [
        (cv("7", 44145200, colour="red"), "2025-01-06", r"Unknown ClinVar record field\(s\) colour; expected allele_id"),
        ({"chrom": "7", "pos": 44145200, "ref": "C"}, "2025-01-06", "lacks alt"),
        ({"chrom": "", "pos": 44145200, "ref": "C", "alt": "T"}, "2025-01-06", "lacks chrom"),
        (cv("7", 0), "2025-01-06", "Variant position must be a positive integer"),
        (cv("7", 44145200, "C", "<DEL>"), "2025-01-06", "REF and ALT must consist of A, C, G, T, N"),
        (cv("7", 44145200, gene="TP53"), "2025-01-06", "Gene 'TP53' is not a panel gene"),
        (cv("7", 44145200), "06/01/2025", "fileDate '06/01/2025' is not a YYYY-MM-DD date."),
    ],
)
def test_write_clinvar_vcf_rejects_invalid_input(tmp_path: Path, record: dict[str, Any], file_date: str, message: str) -> None:
    with pytest.raises(ValueError, match=message):
        write_clinvar_vcf([record], tmp_path / "x.vcf", file_date=file_date)


# ---------------------------------------------------------------------------
# Import
# ---------------------------------------------------------------------------


def test_import_release_from_the_real_format_fixture(db: SQLAlchemy) -> None:
    admin = make_user("kb.admin", role="admin")
    release = import_release(FORMAT_FIXTURE, user=admin, notes="Format fixture")
    db.session.commit()

    assert release.label == "ClinVar 2024-06-30"
    assert release.release_date == date(2024, 6, 30) and release.file_date == "2024-06-30"
    assert release.file_name == "clinvar_format_sample.vcf"
    assert release.file_sha256 == hashlib.sha256(FORMAT_FIXTURE.read_bytes()).hexdigest()
    assert (release.source, release.is_synthetic, release.is_current) == ("synthetic", True, False)
    assert release.sequence is None and release.activated_at is None
    assert release.imported_by == admin and release.notes == "Format fixture"
    assert release.record_count == 18
    assert release.gene_counts == {"GCK": 6, "HNF1A": 3, "HNF4A": 2, "HNF1B": 1, "KCNJ11": 2, "ABCC8": 3, "INS": 1}
    assert release.class_counts == {"P": 5, "LP": 4, "VUS": 4, "LB": 2, "B": 1, "NC": 2}

    rows = known(db, release)
    assert sorted(
        (kv.variant_key.chrom, kv.variant_key.pos, kv.variant_key.ref, kv.variant_key.alt, kv.clinvar_variation_id,
         kv.gene, kv.classification, kv.mechanism, kv.review_stars, kv.is_expert_panel)
        for kv in rows.values()
    ) == sorted(FIXTURE_RECORDS)
    kcnj11 = rows["11-17388500-C-T"]
    assert kcnj11.clinvar_significance == "Pathogenic|risk_factor"
    assert kcnj11.review_status == "criteria_provided,_multiple_submitters,_no_conflicts"
    assert kcnj11.condition == "Permanent neonatal diabetes mellitus; Diabetes mellitus, transient neonatal, 3"
    assert kcnj11.allele_id == 9200011 and kcnj11.classification_source == "clinvar"
    assert kcnj11.hgvs == "NC_000011.10:g.17388500C>T" and kcnj11.molecular_consequence == "SO:0001583|missense_variant"
    assert kcnj11.variant_key.gene == "KCNJ11"
    assert rows["11-17389900-G-A"].mechanism == "inactivating"
    assert rows["11-17420000-A-G"].mechanism == "unknown"  # listed for neonatal diabetes and hyperinsulinism

    entry = db.session.execute(select(AuditLog).filter_by(action="release_imported")).scalar_one()
    assert (entry.entity_type, entry.entity_id, entry.user_id, entry.username) == ("kb_release", release.id, admin.id, "kb.admin")
    assert entry.detail["record_count"] == 18 and entry.detail["is_synthetic"] is True
    assert entry.detail["file_sha256"] == release.file_sha256 and entry.detail["expert_panel_overlay"] is None


def test_import_release_of_a_non_synthetic_clinvar_file(db: SQLAlchemy, tmp_path: Path) -> None:
    path = tmp_path / "clinvar_derived.vcf"
    write_clinvar_vcf([cv("7", 44145200, clnsig="Pathogenic")], path, file_date="2025-02-02", synthetic=False)
    release = import_release(path)
    assert (release.source, release.is_synthetic, release.label) == ("clinvar", False, "ClinVar 2025-02-02")


def test_import_release_label_date_and_missing_file_date(db: SQLAlchemy, tmp_path: Path) -> None:
    path = tmp_path / "nodate.vcf"
    write_clinvar_vcf([cv("7", 44145200, clnsig="Benign")], path, file_date="2025-02-02")
    path.write_text(path.read_text().replace("##fileDate=2025-02-02\n", ""))
    release = import_release(path)
    assert (release.label, release.release_date, release.file_date) == ("ClinVar nodate.vcf", None, None)

    other = tmp_path / "other.vcf"
    write_clinvar_vcf([cv("7", 44145300)], other, file_date="2025-02-02")
    labelled = import_release(other, label="R1 (2025-01, synthetic)", release_date=date(2025, 1, 6))
    assert (labelled.label, labelled.release_date, labelled.file_date) == ("R1 (2025-01, synthetic)", date(2025, 1, 6), "2025-02-02")


def test_import_release_refuses_a_duplicate_file(db: SQLAlchemy, tmp_path: Path) -> None:
    first = import_release(FORMAT_FIXTURE)
    db.session.commit()
    copy = tmp_path / "renamed.vcf"
    copy.write_bytes(FORMAT_FIXTURE.read_bytes())
    for path in (FORMAT_FIXTURE, copy):
        with pytest.raises(ValueError) as info:
            import_release(path)
        assert str(info.value) == (
            f"{path.name} was already imported as release 'ClinVar 2024-06-30' (id {first.id}); "
            "each file can be imported only once."
        )
    assert release_count(db) == 1
    assert clinvar.release_for_file(copy) == first
    assert clinvar.file_sha256(copy) == first.file_sha256


def test_release_for_file_of_an_unknown_file(db: SQLAlchemy) -> None:
    assert clinvar.release_for_file(FORMAT_FIXTURE) is None


@pytest.mark.parametrize(
    ("content", "message"),
    [
        ("this is not a VCF\n", "bad.vcf is not a VCF file: its first line must be '##fileformat=VCFv4.x'."),
        ("##fileformat=VCFv4.1\n##reference=GRCh37\n#CHROM\tPOS\tID\tREF\tALT\tQUAL\tFILTER\tINFO\n",
         "bad.vcf is a GRCh37 file \\(##reference=GRCh37\\); GE-EHR needs the GRCh38 ClinVar VCF"),
        ("##fileformat=VCFv4.1\n##fileDate=2025-01-01\n",
         "bad.vcf could not be read as a VCF file"),
    ],
)
def test_import_release_rejects_invalid_files_before_writing(
    db: SQLAlchemy, tmp_path: Path, content: str, message: str
) -> None:
    path = tmp_path / "bad.vcf"
    path.write_text(content)
    with pytest.raises(ValueError, match=message):
        import_release(path)
    assert release_count(db) == 0


def test_import_release_rejects_a_truncated_bgzip_file_before_writing(db: SQLAlchemy, tmp_path: Path) -> None:
    path = tmp_path / "cut.vcf.gz"
    path.write_bytes(DEMO_RELEASE_1.read_bytes()[: -len(BGZF_EOF)])
    with pytest.raises(ValueError, match="is incomplete"):
        import_release(path)
    assert release_count(db) == 0
    assert db.session.scalar(select(func.count(VariantKey.id))) == 0


def test_import_release_of_a_missing_file(db: SQLAlchemy, tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError, match="ClinVar VCF not found"):
        import_release(tmp_path / "missing.vcf.gz")


def test_import_release_reuses_and_completes_existing_variant_keys(db: SQLAlchemy, tmp_path: Path) -> None:
    existing = VariantKey(chrom="7", pos=44146321, ref="C", alt="T", gene=None)
    db.session.add(existing)
    same_pos_other_chrom = VariantKey.get_or_create("12", 44146321, "C", "T")
    db.session.flush()
    release = import_release(FORMAT_FIXTURE)
    rows = known(db, release)
    assert rows["7-44146321-C-T"].variant_key_id == existing.id and existing.gene == "GCK"
    assert same_pos_other_chrom.id not in {row.variant_key_id for row in rows.values()}

    path = tmp_path / "second.vcf"
    write_clinvar_vcf([cv("7", 44146321, clnsig="Likely pathogenic"), cv("7", 44146322)], path, file_date="2025-02-02")
    second = import_release(path)
    second_rows = known(db, second)
    assert second_rows["7-44146321-C-T"].variant_key_id == existing.id
    assert db.session.scalar(select(func.count(VariantKey.id))) == 18 + 1 + 1


def test_duplicate_keys_in_a_file_keep_the_best_reviewed_record(db: SQLAlchemy, tmp_path: Path, caplog) -> None:  # noqa: ANN001
    path = tmp_path / "dupes.vcf"
    write_clinvar_vcf(
        [
            cv("7", 44145200, variation_id=9900001, clnsig="Uncertain significance", clnrevstat="criteria provided, single submitter"),
            cv("7", 44145200, variation_id=9900002, clnsig="Pathogenic", clnrevstat="reviewed by expert panel"),
            cv("7", 44145200, variation_id=9900003, clnsig="Benign", clnrevstat="reviewed by expert panel"),
        ],
        path,
        file_date="2025-02-02",
    )
    release = import_release(path)
    row = known(db, release)["7-44145200-C-T"]
    assert (release.record_count, row.clinvar_variation_id, row.classification) == (1, 9900002, "P")
    assert "lists 2 variant key(s) more than once" in caplog.text


def test_import_of_about_ten_thousand_records_is_fast_and_bulk(db: SQLAlchemy, tmp_path: Path) -> None:
    rng = random.Random(5)
    seen: set[tuple[str, int]] = set()
    records = []
    significances = ("Pathogenic", "Likely_pathogenic", "Uncertain_significance", "Likely_benign", "Benign")
    while len(records) < 10_000:
        info = C.GENE_INFO[rng.choice(C.GENES)]
        pos = rng.randint(info.start, info.end)
        if (info.chrom, pos) in seen:
            continue
        seen.add((info.chrom, pos))
        records.append(cv(info.chrom, pos, rng.choice("ACGT"), "N", variation_id=9_600_000 + len(records),
                          clnsig=rng.choice(significances), clnrevstat="criteria_provided,_single_submitter",
                          clndn="Monogenic_diabetes"))
    path = tmp_path / "large.vcf.gz"
    write_clinvar_vcf(records, path, file_date="2025-04-01")
    started = time.perf_counter()
    with count_queries(db) as statements:
        release = import_release(path)
        db.session.commit()
    elapsed = time.perf_counter() - started
    assert release.record_count == 10_000 and sum(release.class_counts.values()) == 10_000
    assert db.session.scalar(select(func.count(KnownVariant.id))) == 10_000
    assert elapsed < 10, f"import of 10,000 records took {elapsed:.1f} s"
    assert len(statements) < 60, "VariantKeys and KnownVariants are inserted in bulk, not row by row"


# ---------------------------------------------------------------------------
# Expert-panel overlay
# ---------------------------------------------------------------------------


def test_example_overlay_overrides_the_clinvar_classification(db: SQLAlchemy) -> None:
    release = import_release(DEMO_RELEASE_1, label="R1", expert_panel_csv=OVERLAY_EXAMPLE)
    rows = known(db, release)

    # ClinVar (R1) says V03 LP, V08 VUS and V10 VUS; the expert-panel overlay takes precedence.
    for label, clinvar_says, panel_says in (
        ("20-44401300-C-T", "Likely_pathogenic", "P"),
        ("12-120999100-A-G", "Uncertain_significance", "LB"),
        ("20-44410200-G-T", "Uncertain_significance", "LP"),
    ):
        row = rows[label]
        assert (row.classification, row.is_expert_panel, row.classification_source) == (
            panel_says, True, "expert_panel_overlay"
        )
        assert row.clinvar_significance == clinvar_says  # the raw CLNSIG is kept for provenance
        assert row.review_stars == 3
    untouched = rows["7-44150500-G-A"]  # V07
    assert (untouched.classification, untouched.classification_source, untouched.is_expert_panel) == (
        "LP", "clinvar", False
    )
    assert release.class_counts["P"] == 176 + 1 and release.class_counts["LB"] == 301 + 1
    assert release.notes == (
        "Expert-panel overlay expert_panel_overlay_example.csv (ClinGen Monogenic Diabetes VCEP (synthetic example)) "
        "applied to 3 variant(s)."
    )
    entry = db.session.execute(select(AuditLog).where(AuditLog.entity_id == release.id)).scalar_one()
    assert entry.detail["expert_panel_overlay"] == "expert_panel_overlay_example.csv"
    assert entry.detail["expert_panel_overrides"] == 3


def test_overlay_takes_precedence_over_a_conflicting_clinvar_class(db: SQLAlchemy, tmp_path: Path) -> None:
    vcf = tmp_path / "base.vcf"
    write_clinvar_vcf(
        [
            cv("12", 120994300, variation_id=9000006, clnsig="Uncertain significance", clnrevstat="criteria provided, single submitter"),
            cv("7", 44150500, variation_id=9000007, clnsig="Likely pathogenic", clnrevstat="practice guideline"),
            cv("7", 44150600, variation_id=None, clnsig="Benign"),
        ],
        vcf,
        file_date="2025-02-02",
    )
    csv_path = tmp_path / "panel.csv"
    csv_path.write_text(
        "﻿clinvar_variation_id,classification,panel,date\n"
        "9000006,Likely pathogenic,Synthetic VCEP,2025-06-02\n"
        "9000007,VUS,Synthetic VCEP,\n"
        "9000007,Uncertain significance,Synthetic VCEP,2025-06-03\n"
        "9999999,B,Other synthetic panel,2025-06-04\n",
        encoding="utf-8",
    )
    release = import_release(vcf, expert_panel_csv=csv_path)
    rows = known(db, release)
    assert (rows["12-120994300-C-T"].classification, rows["12-120994300-C-T"].review_stars) == ("LP", 3)
    assert (rows["7-44150500-C-T"].classification, rows["7-44150500-C-T"].review_stars) == ("VUS", 4)
    assert rows["7-44150600-C-T"].classification_source == "clinvar"
    assert release.class_counts["LP"] == 1 and release.class_counts["VUS"] == 1 and release.class_counts["B"] == 1
    assert release.notes == (
        "Expert-panel overlay panel.csv (Other synthetic panel, Synthetic VCEP) applied to 2 variant(s). "
        "1 overlay variation ID(s) were not found in this file."
    )


def test_load_expert_panel_overlay_parses_codes_and_labels() -> None:
    entries = load_expert_panel_overlay(OVERLAY_EXAMPLE)
    assert {variation_id: entry.classification for variation_id, entry in entries.items()} == {
        9000003: "P", 9000008: "LB", 9000010: "LP"
    }
    assert entries[9000010].date == date(2025, 9, 15)
    assert entries[9000003].panel == "ClinGen Monogenic Diabetes VCEP (synthetic example)"


@pytest.mark.parametrize(
    ("content", "message"),
    [
        ("", "missing column\\(s\\) clinvar_variation_id, classification, panel, date"),
        ("clinvar_variation_id,classification\n1,P\n", "missing column\\(s\\) panel, date; the header line must be"),
        ("clinvar_variation_id,classification,panel,date\nabc,P,X,\n", "line 2: clinvar_variation_id 'abc' is not a positive whole number."),
        ("clinvar_variation_id,classification,panel,date\n0,P,X,\n", "line 2: clinvar_variation_id '0' is not a positive whole number."),
        ("clinvar_variation_id,classification,panel,date\n12,Probably bad,X,\n",
         "line 2: unknown classification 'Probably bad'; use one of P, LP, VUS, LB, B or Pathogenic, Likely pathogenic, "
         "Uncertain significance, Likely benign, Benign."),
        ("clinvar_variation_id,classification,panel,date\n12,NC,X,\n", "line 2: unknown classification 'NC'"),
        ("clinvar_variation_id,classification,panel,date\n12,P,,\n", "line 2: the panel column is empty"),
        ("clinvar_variation_id,classification,panel,date\n12,P,X,06/02/2025\n", "line 2: date '06/02/2025' is not a valid YYYY-MM-DD date."),
        ("clinvar_variation_id,classification,panel,date\n12,P,X,2025-01-01,extra\n", "line 2: the row has more columns than the header line."),
        ("clinvar_variation_id,classification,panel,date\n12,P,X,\n13,B,X,\n12,LB,X,\n",
         "line 4: variation ID 12 is listed twice with different classifications \\(P and LB\\)."),
        ('clinvar_variation_id,classification,panel,date\n12,P,"X\n', "is not a valid CSV file"),
    ],
)
def test_invalid_overlay_files_are_rejected_with_a_clear_message(
    db: SQLAlchemy, tmp_path: Path, content: str, message: str
) -> None:
    path = tmp_path / "overlay.csv"
    path.write_text(content, encoding="utf-8")
    with pytest.raises(ValueError, match=message):
        import_release(FORMAT_FIXTURE, expert_panel_csv=path)
    assert release_count(db) == 0


def test_overlay_that_is_not_utf8_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "overlay.csv"
    path.write_bytes(b"clinvar_variation_id,classification,panel,date\n12,P,Pan\xe9l,\n")
    with pytest.raises(ValueError, match="Expert-panel overlay overlay.csv is not a UTF-8 text CSV file."):
        load_expert_panel_overlay(path)


# ---------------------------------------------------------------------------
# Release diff
# ---------------------------------------------------------------------------


@pytest.fixture
def release_pair(db: SQLAlchemy) -> dict[str, Any]:
    old, new = make_release("old"), make_release("new")
    pairs = {
        "upgrade": (("7", 44145200), "VUS", "LP"),
        "downgrade": (("7", 44145300), "LP", "VUS"),
        "within_actionable": (("7", 44145400), "LP", "P"),
        "within_non_actionable": (("7", 44145500), "VUS", "LB"),
        "unchanged": (("7", 44145600), "P", "P"),
        "removed": (("7", 44145700), "P", None),
        "added": (("7", 44145800), None, "P"),
        "added_benign": (("7", 44145900), None, "B"),
        "nc_both": (("7", 44146000), "NC", "NC"),
    }
    keys: dict[str, int] = {}
    for name, ((chrom, pos), old_class, new_class) in pairs.items():
        key = VariantKey.get_or_create(chrom, pos, "C", "T")
        keys[name] = key.id
        if old_class:
            add_known_variant(old, chrom, pos, "C", "T", old_class)
        if new_class:
            add_known_variant(new, chrom, pos, "C", "T", new_class)
    for name, old_mechanism, new_mechanism, cls in (
        ("mech_actionable", "unknown", "activating", "P"),
        ("mech_non_actionable", "activating", "unknown", "VUS"),
    ):
        pos = 17_385_000 + len(keys)
        keys[name] = VariantKey.get_or_create("11", pos, "C", "T").id
        add_known_variant(old, "11", pos, "C", "T", cls, mechanism=old_mechanism)
        add_known_variant(new, "11", pos, "C", "T", cls, mechanism=new_mechanism)
    keys["nobody"] = VariantKey.get_or_create("12", 120978900, "G", "A").id
    return {"old": old, "new": new, "keys": keys}


def test_diff_releases_reports_class_and_mechanism_changes(db: SQLAlchemy, release_pair: dict[str, Any]) -> None:
    keys = release_pair["keys"]
    changes = diff_releases(release_pair["old"], release_pair["new"])
    assert changes == sorted(changes, key=lambda change: change.variant_key_id)
    by_key = {change.variant_key_id: change for change in changes}
    assert by_key[keys["upgrade"]] == KeyChange(keys["upgrade"], "GCK", "VUS", "LP", "unknown", "unknown", "upgrade")
    assert by_key[keys["downgrade"]].category == "downgrade"
    assert by_key[keys["within_actionable"]].category == "within_actionable"
    assert by_key[keys["within_non_actionable"]].category == "within_non_actionable"
    assert by_key[keys["removed"]] == KeyChange(keys["removed"], "GCK", "P", "NC", "unknown", "unknown", "downgrade")
    assert by_key[keys["added"]] == KeyChange(keys["added"], "GCK", "NC", "P", "unknown", "unknown", "upgrade")
    assert by_key[keys["added_benign"]].category == "within_non_actionable"
    assert by_key[keys["mech_actionable"]] == KeyChange(
        keys["mech_actionable"], "KCNJ11", "P", "P", "unknown", "activating", "within_actionable"
    )
    assert by_key[keys["mech_non_actionable"]].category == "within_non_actionable"
    assert {keys["unchanged"], keys["nc_both"], keys["nobody"]}.isdisjoint(by_key)
    assert len(changes) == 9


def test_diff_releases_restricted_to_stored_keys(db: SQLAlchemy, release_pair: dict[str, Any]) -> None:
    keys = release_pair["keys"]
    wanted = {keys["upgrade"], keys["unchanged"], keys["nobody"], keys["mech_actionable"]}
    with count_queries(db) as statements:
        changes = diff_releases(release_pair["old"], release_pair["new"], iter(wanted))
    assert [change.variant_key_id for change in changes] == sorted([keys["upgrade"], keys["mech_actionable"]])
    assert len(statements) == 2  # one bulk lookup per release
    with count_queries(db) as statements:
        assert diff_releases(release_pair["old"], release_pair["new"], []) == []
    assert statements == []


def test_diff_against_no_previous_release_is_an_initial_classification(
    db: SQLAlchemy, release_pair: dict[str, Any]
) -> None:
    keys = release_pair["keys"]
    with count_queries(db) as statements:
        changes = diff_releases(None, release_pair["new"], keys.values())
    assert len(statements) == 1
    assert {change.old_class for change in changes} == {"NC"}
    new_classes = {change.variant_key_id: change.new_class for change in changes}
    assert keys["nc_both"] not in new_classes and keys["removed"] not in new_classes
    assert new_classes[keys["upgrade"]] == "LP"
    assert {change.category for change in changes} == {"upgrade", "within_non_actionable"}


def test_diff_releases_with_more_keys_than_one_query_chunk(db: SQLAlchemy) -> None:
    old, new = make_release(), make_release()
    db.session.execute(
        insert(VariantKey),
        [{"chrom": "12", "pos": 120_971_000 + offset, "ref": "A", "alt": "G", "gene": "HNF1A"} for offset in range(2000)],
    )
    key_ids = list(db.session.scalars(select(VariantKey.id).order_by(VariantKey.id)))
    for release, reclassify in ((old, False), (new, True)):
        db.session.execute(
            insert(KnownVariant),
            [
                {"release_id": release.id, "variant_key_id": key_id, "gene": "HNF1A", "mechanism": "unknown",
                 "classification": "LB" if reclassify and index % 4 == 0 else "VUS"}
                for index, key_id in enumerate(key_ids)
            ],
        )
    with count_queries(db) as statements:
        changes = diff_releases(old, new, key_ids)
    assert len(changes) == 500 and {change.category for change in changes} == {"within_non_actionable"}
    assert len(statements) == 6  # ceil(2,000 / 900) chunks per release


# ---------------------------------------------------------------------------
# Demo releases (SPEC §11)
# ---------------------------------------------------------------------------


@pytest.fixture
def demo_releases(db: SQLAlchemy) -> tuple[KbRelease, KbRelease]:
    r1 = import_release(DEMO_RELEASE_1, label="R1 (2025-01, synthetic)")
    r2 = import_release(DEMO_RELEASE_2, label="R2 (2026-01, synthetic)")
    db.session.commit()
    return r1, r2


def test_demo_release_headers_are_synthetic() -> None:
    for path, file_date in ((DEMO_RELEASE_1, "2025-01-06"), (DEMO_RELEASE_2, "2026-01-05")):
        header = [line for line in gzip.decompress(path.read_bytes()).decode().splitlines() if line.startswith("##")]
        assert header[:2] == ["##fileformat=VCFv4.1", f"##fileDate={file_date}"]
        assert set(C.SYNTHETIC_HEADER_LINES) <= set(header) and "##reference=GRCh38" in header
        assert path.read_bytes().endswith(BGZF_EOF)


def test_demo_variants_have_the_spec_classes(db: SQLAlchemy, demo_releases: tuple[KbRelease, KbRelease]) -> None:
    r1, r2 = (known(db, release) for release in demo_releases)
    for code, (label, gene, class_1, class_2) in DEMO_TABLE.items():
        assert (r1[label].gene, r1[label].classification) == (gene, class_1), code
        assert (r2[label].gene, r2[label].classification) == (gene, class_2), code
        assert r1[label].clinvar_variation_id >= 9_000_000
    for rows in (r1, r2):
        assert rows["12-120978900-G-A"].is_expert_panel and rows["7-44145200-C-T"].is_expert_panel  # V01, V02
        assert rows["12-120978900-G-A"].review_status == "reviewed_by_expert_panel"
        for label in ("11-17387100-C-T", "11-17410300-G-A"):  # V04, V09
            assert rows[label].condition == "Permanent neonatal diabetes mellitus"
            assert rows[label].mechanism == "activating"
        assert rows["11-17450000-C-T"].mechanism == "unknown"  # B04 (benign ABCC8)
    expert = {label for label, row in r1.items() if row.is_expert_panel and label in {v[0] for v in DEMO_TABLE.values()}}
    assert expert == {"12-120978900-G-A", "7-44145200-C-T"}
    assert "1-1000000-A-G" not in r1  # X01 is off panel


def test_demo_releases_carry_realistic_filler(db: SQLAlchemy, demo_releases: tuple[KbRelease, KbRelease]) -> None:
    r1, r2 = demo_releases
    assert (r1.record_count, r2.record_count) == (1515, 1565)
    assert (r1.file_date, r2.file_date, r1.is_synthetic, r2.source) == ("2025-01-06", "2026-01-05", True, "synthetic")
    rows_1 = known(db, r1)
    demo_labels = {row[0] for row in DEMO_TABLE.values()}
    filler = [row for label, row in rows_1.items() if label not in demo_labels]
    assert len(filler) == 1500
    assert min(row.clinvar_variation_id for row in rows_1.values()) >= 9_000_000
    assert all(C.gene_for_position(row.variant_key.chrom, row.variant_key.pos) == row.gene for row in filler)
    assert {(row.variant_key.chrom, row.variant_key.pos) for row in filler}.isdisjoint(
        {tuple(label.split("-")[:2]) for label in demo_labels}
    )
    classes = Counter(row.classification for row in filler)
    assert set(classes) == set(C.CLASSES) and classes["VUS"] == max(classes.values())
    assert sum(row.clinvar_significance.startswith("Conflicting") for row in filler) > 50
    katp = [row for row in filler if row.gene in ("KCNJ11", "ABCC8")]
    assert {row.mechanism for row in katp} == {"activating", "inactivating", "unknown"}
    assert set(r1.gene_counts) == set(C.GENES) and all(count > 50 for count in r1.gene_counts.values())


def test_demo_release_2_reclassifies_filler_and_adds_records(
    db: SQLAlchemy, demo_releases: tuple[KbRelease, KbRelease]
) -> None:
    r1, r2 = demo_releases
    changes = diff_releases(r1, r2)
    demo_ids = {
        db.session.execute(select(VariantKey.id).filter_by(chrom=k[0], pos=int(k[1]), ref=k[2], alt=k[3])).scalar_one(): code
        for code, (label, *_rest) in DEMO_TABLE.items()
        for k in [label.split("-")]
    }
    demo_changes = {demo_ids[change.variant_key_id]: change.category for change in changes if change.variant_key_id in demo_ids}
    assert demo_changes == {
        "V03": "within_actionable",
        "V06": "upgrade",
        "V07": "downgrade",
        "V08": "within_non_actionable",
        "V10": "upgrade",
        "V11": "upgrade",
    }
    filler_changes = [change for change in changes if change.variant_key_id not in demo_ids]
    added = [change for change in filler_changes if change.old_class == "NC"]
    reclassified = [change for change in filler_changes if change.old_class != "NC"]
    assert len(added) == 50
    assert 0.02 * 1500 <= len(reclassified) <= 0.04 * 1500
    assert all(change.old_class != change.new_class for change in reclassified)
    assert {change.category for change in reclassified} == set(C.CHANGE_CATEGORIES)


def test_build_demo_releases_reproduces_the_committed_files(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    script = load_script("build_demo_releases", monkeypatch)
    assert script.main(["--output-dir", str(tmp_path)]) == 0
    for name in ("demo_release_1.vcf.gz", "demo_release_2.vcf.gz"):
        assert (tmp_path / name).read_bytes() == (DEMO_DIR / name).read_bytes(), name
    output = capsys.readouterr().out
    assert "demo_release_1.vcf.gz (1515 records)" in output and "demo_release_2.vcf.gz (1565 records)" in output


def test_build_demo_releases_records_follow_the_spec_table(monkeypatch: pytest.MonkeyPatch) -> None:
    script = load_script("build_demo_releases", monkeypatch)
    release_1, release_2 = script.build_releases()
    for records, column in ((release_1, 2), (release_2, 3)):
        by_key = {f"{r['chrom']}-{r['pos']}-{r['ref']}-{r['alt']}": r for r in records}
        for code, row in DEMO_TABLE.items():
            assert normalize_clinvar_significance(by_key[row[0]]["clnsig"]) == row[column], code
    assert all(record["variation_id"] >= 9_000_000 for record in release_1 + release_2)
    assert len({record["variation_id"] for record in release_2}) == len(release_2)
