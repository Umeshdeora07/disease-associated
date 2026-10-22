"""VCF backends: cyvcf2 (htslib) and the built-in pure-Python reader give identical results.

* Backend selection by ``GE_EHR_VCF_BACKEND`` (``auto`` falls back when cyvcf2 is missing, as on
  Windows) — these tests run everywhere.
* Equivalence: every patient VCF fixture and demo file (``data/demo/vcfs/*.vcf`` including
  ``malformed.vcf``, ``tests/data/*.vcf``; plain, gzip and bgzip) and every ClinVar-format file
  (``data/demo/clinvar/*.vcf.gz``, ``tests/data/*``) is read with both backends and the results
  — or the errors and their line numbers — must be identical, down to the typed INFO values.
  These tests need cyvcf2 and are skipped without it.
"""

from __future__ import annotations

import gzip
import math
import os
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from ge_ehr import create_app
from ge_ehr.services import clinvar, vcf_backend, vcf_parser
from ge_ehr.services.clinvar import _bgzf_blocks, iter_clinvar_records
from ge_ehr.services.vcf_backend import (
    VCF_BACKEND_ENV,
    SitesVcfReader,
    configured_vcf_backend,
    cyvcf2,
    describe_vcf_backend,
    info_entries,
)
from ge_ehr.services.vcf_parser import VcfError, parse_patient_vcf

REPO_ROOT = Path(__file__).resolve().parent.parent
DATA = Path(__file__).parent / "data"
DEMO = REPO_ROOT / "data" / "demo"
DEMO_VCFS = sorted((DEMO / "vcfs").glob("*.vcf"))
DEMO_RELEASES = sorted((DEMO / "clinvar").glob("*.vcf.gz"))
FIXTURE_VCFS = sorted(DATA.glob("*.vcf*"))
PATIENT_FILES = DEMO_VCFS + FIXTURE_VCFS
CLINVAR_FILES = DEMO_RELEASES + FIXTURE_VCFS + DEMO_VCFS
CLINVAR_FORMAT_FILES = DEMO_RELEASES + [DATA / "clinvar_format_sample.vcf"]

requires_cyvcf2 = pytest.mark.skipif(cyvcf2 is None, reason="cyvcf2 (htslib) is not installed")


def file_id(path: Path) -> str:
    return path.relative_to(REPO_ROOT).as_posix()


def test_the_inventory_covers_every_fixture_and_demo_file() -> None:
    assert len(DEMO_VCFS) == 16 and DEMO / "vcfs" / "malformed.vcf" in DEMO_VCFS
    assert [path.name for path in DEMO_RELEASES] == ["demo_release_1.vcf.gz", "demo_release_2.vcf.gz"]
    assert {path.name for path in FIXTURE_VCFS} >= {
        "clinvar_format_sample.vcf",
        "patient_grch37.vcf",
        "patient_grch38.vcf",
    }


# ---------------------------------------------------------------------------
# Backend selection
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(("value", "expected"), [(None, "auto"), ("", "auto"), (" Python ", "python"), ("CYVCF2", "cyvcf2")])
def test_configured_backend_reads_the_environment(monkeypatch: pytest.MonkeyPatch, value: str | None, expected: str) -> None:
    if value is None:
        monkeypatch.delenv(VCF_BACKEND_ENV, raising=False)
    else:
        monkeypatch.setenv(VCF_BACKEND_ENV, value)
    assert configured_vcf_backend() == expected


def test_an_unknown_backend_is_refused_with_the_choices(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(VCF_BACKEND_ENV, "htslib")
    with pytest.raises(ValueError, match=r"GE_EHR_VCF_BACKEND='htslib' is not valid; use one of auto, cyvcf2, python"):
        vcf_backend.vcf_backend()


def test_auto_falls_back_to_python_without_cyvcf2(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(vcf_backend, "cyvcf2", None)
    for value in ("auto", "python"):
        monkeypatch.setenv(VCF_BACKEND_ENV, value)
        assert vcf_backend.vcf_backend() == "python"
    monkeypatch.setenv(VCF_BACKEND_ENV, "auto")
    assert describe_vcf_backend() == "built-in pure-Python reader (cyvcf2 not installed)"
    monkeypatch.setenv(VCF_BACKEND_ENV, "python")
    assert describe_vcf_backend() == "built-in pure-Python reader (selected)"


def test_requiring_a_missing_cyvcf2_is_an_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(vcf_backend, "cyvcf2", None)
    monkeypatch.setenv(VCF_BACKEND_ENV, "cyvcf2")
    with pytest.raises(RuntimeError, match="cyvcf2 package is not installed"):
        vcf_backend.vcf_backend()


@requires_cyvcf2
def test_auto_prefers_cyvcf2_when_installed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(VCF_BACKEND_ENV, "auto")
    assert vcf_backend.vcf_backend() == "cyvcf2"
    assert describe_vcf_backend() == f"cyvcf2 {cyvcf2.__version__} (htslib)"


@pytest.mark.parametrize(
    ("value", "missing_cyvcf2", "error", "message"),
    [
        ("pyton", False, ValueError, "is not valid"),
        ("cyvcf2", True, RuntimeError, "cyvcf2 package is not installed"),
    ],
)
def test_create_app_checks_the_backend_at_start_up(
    monkeypatch: pytest.MonkeyPatch, value: str, missing_cyvcf2: bool, error: type[Exception], message: str
) -> None:
    if missing_cyvcf2:
        monkeypatch.setattr(vcf_backend, "cyvcf2", None)
    monkeypatch.setenv(VCF_BACKEND_ENV, value)
    with pytest.raises(error, match=message):
        create_app("testing")


def test_without_cyvcf2_nothing_touches_it(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """With cyvcf2 gone (``auto``), patient VCFs and ClinVar files are read by the built-in reader."""
    for module in (vcf_backend, vcf_parser, clinvar):
        monkeypatch.setattr(module, "cyvcf2", None)
    monkeypatch.setenv(VCF_BACKEND_ENV, "auto")
    parsed = parse_patient_vcf((DEMO / "vcfs" / "DEMO-0001.vcf").read_bytes())
    assert [(v.gene, v.pos, v.ref, v.alt, v.zygosity) for v in parsed.variants] == [
        ("HNF1A", 120978900, "G", "A", "heterozygous"),
        ("HNF1A", 121001000, "C", "T", "heterozygous"),
    ]
    assert len(list(iter_clinvar_records(DEMO_RELEASES[0]))) > 1000


def test_cyvcf2_import_failure_falls_back_in_a_fresh_interpreter() -> None:
    """The import-time fallback itself: ``import cyvcf2`` failing (as on Windows) must not matter."""
    script = (
        "import sys; sys.modules['cyvcf2'] = None\n"  # makes `import cyvcf2` raise ImportError
        "from pathlib import Path\n"
        "from ge_ehr.services import vcf_backend\n"
        "from ge_ehr.services.clinvar import iter_clinvar_records\n"
        "from ge_ehr.services.vcf_parser import parse_patient_vcf\n"
        "assert vcf_backend.cyvcf2 is None and vcf_backend.vcf_backend() == 'python'\n"
        "parsed = parse_patient_vcf(Path('data/demo/vcfs/DEMO-0003.vcf').read_bytes())\n"
        "records = list(iter_clinvar_records('data/demo/clinvar/demo_release_2.vcf.gz'))\n"
        "print(vcf_backend.describe_vcf_backend(), len(parsed.variants), len(records) > 1000)\n"
    )
    env = {key: value for key, value in os.environ.items() if key != VCF_BACKEND_ENV}
    result = subprocess.run(
        [sys.executable, "-c", script], cwd=REPO_ROOT, env=env, capture_output=True, text=True, timeout=120, check=False
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "built-in pure-Python reader (cyvcf2 not installed) 1 True"


# ---------------------------------------------------------------------------
# Equivalence on every fixture and demo file
# ---------------------------------------------------------------------------


def both_backends(monkeypatch: pytest.MonkeyPatch, read: Callable[[], Any], errors: type[Exception]) -> list[Any]:
    """``read()`` with the cyvcf2 and the python backend: its result, or the error it raised."""
    outcomes: list[Any] = []
    for backend in ("cyvcf2", "python"):
        monkeypatch.setenv(VCF_BACKEND_ENV, backend)
        assert vcf_backend.vcf_backend() == backend
        try:
            outcomes.append(("result", read()))
        except errors as exc:
            outcomes.append(("error", str(exc), getattr(exc, "line", None)))
    return outcomes


def compressed_forms(data: bytes) -> dict[str, bytes]:
    return {"plain": data, "gzip": gzip.compress(data, mtime=0), "bgzip": b"".join(_bgzf_blocks(data))}


@requires_cyvcf2
@pytest.mark.parametrize("path", PATIENT_FILES, ids=file_id)
@pytest.mark.parametrize("form", ["plain", "gzip", "bgzip"])
def test_patient_vcf_backends_agree_on_every_file(monkeypatch: pytest.MonkeyPatch, path: Path, form: str) -> None:
    data = compressed_forms(path.read_bytes())[form]
    cyvcf2_outcome, python_outcome = both_backends(monkeypatch, lambda: parse_patient_vcf(data, path.name), VcfError)
    assert python_outcome == cyvcf2_outcome


@requires_cyvcf2
def test_backends_agree_on_the_demo_files_actual_content(monkeypatch: pytest.MonkeyPatch) -> None:
    """Guard against vacuous equality: the demo files parse to variants, malformed.vcf to an error."""
    outcomes = {
        path.name: both_backends(monkeypatch, lambda path=path: parse_patient_vcf(path.read_bytes()), VcfError)
        for path in DEMO_VCFS
    }
    malformed = outcomes.pop("malformed.vcf")
    assert malformed[0] == malformed[1] == (
        "error",
        "Line 12: POS must be a positive whole number, found '12O994300'.",
        12,
    )
    for name, (cyvcf2_outcome, python_outcome) in outcomes.items():
        assert cyvcf2_outcome[0] == "result" and cyvcf2_outcome[1].variants, name
        assert python_outcome == cyvcf2_outcome, name


def _patient_vcf(*records: str) -> bytes:
    header = (
        "##fileformat=VCFv4.2\n##reference=GRCh38\n"
        "#CHROM\tPOS\tID\tREF\tALT\tQUAL\tFILTER\tINFO\tFORMAT\tS1\tS2\n"
    )
    return (header + "".join(record + "\n" for record in records)).encode()


GENOTYPE_EDGE_CASES = [
    "0/1", "1/0", "0|1", "1|0", "1|1", "1/1", "1", "0", ".", "./.", ".|1", "1/.", "0/0", "2/1", "1|2",
    "0/1|2", "0|1/2", "1/1/1", "./1|1", "2",
]


@requires_cyvcf2
def test_backends_agree_on_genotypes_filters_and_alleles(monkeypatch: pytest.MonkeyPatch) -> None:
    records = [
        f"chr7\t{44145200 + index}\t.\tC\tT,G\t.\tPASS\t.\tGT:DP\t{genotype}:9\t0/1"
        for index, genotype in enumerate(GENOTYPE_EDGE_CASES)
    ]
    records += [
        "7\t44146000\trs1\tc\tt\t50\t.\t.\tGT\t0/1\t.",  # lower-case bases, FILTER '.'
        "7\t44146001\t.\tC\tT\t.\tq10;LowQual\t.\tGT\t1/1\t0/0",  # failed filters
        "7\t44146002\t.\tC\tT\t.\tPASS\t.\tDP:GT\t7\t0/1",  # trailing GT dropped (missing call)
        "7\t44146003\t.\tCAG\t*,<DEL>,C[2:321682[,A\t.\tPASS\t.\tGT\t1/2\t3/4",  # spanning, symbolic, breakend
        "7\t44146003\t.\tCAG\tA\t.\tPASS\t.\tGT\t0/1\t0/0",  # duplicate key
        "12\t120978900\t.\tG\tA\t.\tPASS\t.\tGT:GQ\t0/1:.\t1/1",  # another chromosome
        "1\t1000000\t.\tA\tG\t.\tPASS\t.\tGT\t0/1\t0/1",  # off panel
        "X\t5\t.\tN\t.\t.\tPASS\t.\tGT\t0\t0",  # no ALT, haploid
    ]
    data = _patient_vcf(*records)
    cyvcf2_outcome, python_outcome = both_backends(monkeypatch, lambda: parse_patient_vcf(data), VcfError)
    assert cyvcf2_outcome[0] == "result" and len(cyvcf2_outcome[1].variants) > 20
    assert python_outcome == cyvcf2_outcome


@requires_cyvcf2
@pytest.mark.parametrize("alt_count", [65_534, 65_535])
def test_backends_agree_on_htslibs_alt_allele_limit(monkeypatch: pytest.MonkeyPatch, alt_count: int) -> None:
    alts = ",".join(f"A{'C' * (index % 9)}{'G' * (index // 9 % 9)}{'T' * (index // 81)}" for index in range(alt_count))
    data = _patient_vcf(f"chr7\t44145200\t.\tC\t{alts}\t.\tPASS\t.\tGT\t0/1\t0/0")
    cyvcf2_outcome, python_outcome = both_backends(monkeypatch, lambda: parse_patient_vcf(data), VcfError)
    assert python_outcome == cyvcf2_outcome
    assert cyvcf2_outcome[0] == ("result" if alt_count == 65_534 else "error")


@requires_cyvcf2
@pytest.mark.parametrize("path", CLINVAR_FILES, ids=file_id)
def test_clinvar_backends_agree_on_every_file(monkeypatch: pytest.MonkeyPatch, path: Path) -> None:
    cyvcf2_outcome, python_outcome = both_backends(monkeypatch, lambda: list(iter_clinvar_records(path)), ValueError)
    assert python_outcome == cyvcf2_outcome
    if path in CLINVAR_FORMAT_FILES:
        assert cyvcf2_outcome[0] == "result" and cyvcf2_outcome[1]


def _same(left: Any, right: Any) -> bool:
    """Equal values of the same type (NaN equals NaN; tuples element by element)."""
    if isinstance(left, tuple) and isinstance(right, tuple):
        return len(left) == len(right) and all(_same(a, b) for a, b in zip(left, right, strict=True))
    if isinstance(left, float) and isinstance(right, float) and math.isnan(left) and math.isnan(right):
        return True
    return type(left) is type(right) and left == right


def assert_reads_like_htslib(path: Path) -> int:
    """Compare every record of a sites-only VCF field by field with cyvcf2; returns the record count."""
    assert cyvcf2 is not None
    opener = gzip.open if path.read_bytes()[:2] == b"\x1f\x8b" else open
    with opener(path, "rt", encoding="utf-8", errors="replace", newline="\n") as handle:
        raw_lines = [line.rstrip("\r\n") for line in handle]
    reader = cyvcf2.VCF(str(path))
    count = 0
    try:
        with SitesVcfReader(path) as text_reader:
            declared = set(text_reader.header.info_types)
            for text_record, variant in zip(text_reader.records(), reader, strict=True):
                where = f"{path.name} line {text_record.line}"
                assert (text_record.CHROM, text_record.POS, text_record.ID, text_record.REF, text_record.ALT) == (
                    variant.CHROM, variant.POS, variant.ID, variant.REF, variant.ALT
                ), where
                column = raw_lines[text_record.line - 1].split("\t")[7]
                for key in declared | set(info_entries(column)) | {"NOT_THERE"}:
                    python_value, htslib_value = text_record.INFO.get(key), variant.INFO.get(key)
                    assert _same(python_value, htslib_value), (where, key, python_value, htslib_value)
                count += 1
    finally:
        reader.close()
    return count


@requires_cyvcf2
@pytest.mark.parametrize("path", CLINVAR_FORMAT_FILES, ids=file_id)
def test_sites_reader_matches_htslib_value_for_value(path: Path) -> None:
    assert assert_reads_like_htslib(path) > 0


TRICKY_VALUES = [
    "5", "5x", "-", "+", " 5", "abc", ".", "-0", "007", "2147483647", "2147483648", "-2147483641",
    "-2147483640", "99999999999", "1,2", "1,.,3", ",", "1,", "1.5", "1e3", "0x10", "0x1p3", "inf", "-inf",
    "nan", "NaN(1)", "1e40", "-1e40", "1e-50", ".5", "5.", "e5", "\x07abc", "", "3.4028235e38",
    "3.4028236e38", "Infinity", "infinit", "0x", "1e", "1e+", "+.5e-3x", "a=b", "é",
]


@requires_cyvcf2
def test_typed_info_values_match_htslib(tmp_path: Path) -> None:
    """Integer, Float, String, Character and Flag values, including malformed ones, as htslib parses them."""
    lines = [
        "##fileformat=VCFv4.2",
        '##INFO=<ID=I,Number=.,Type=Integer,Description="integer">',
        '##INFO=<ID=F,Number=.,Type=Float,Description="float">',
        '##INFO=<ID=S,Number=.,Type=String,Description="string">',
        '##INFO=<ID=G,Number=0,Type=Flag,Description="flag">',
        '##INFO=<ID=C,Number=1,Type=Character,Description="character">',
        '##INFO=<ID=I,Number=1,Type=String,Description="a second definition is ignored">',
        "#CHROM\tPOS\tID\tREF\tALT\tQUAL\tFILTER\tINFO",
    ]
    position = 1
    for value in TRICKY_VALUES:
        for key in "IFSGCU":  # U is not declared in the header
            lines.append(f"7\t{position}\t.\tC\tT\t.\t.\t{key}={value}")
            position += 1
    lines += [f"7\t{position + index}\t.\tC\tT\t.\t.\t{key}" for index, key in enumerate("IFSGCU")]
    lines.append(f"7\t{position + 6}\t.\tC\tT\t.\t.\tI=1;I=2;F=3;;S=x;")  # repeated key, empty entry, trailing ';'
    path = tmp_path / "tricky.vcf"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")
    assert assert_reads_like_htslib(path) == len(TRICKY_VALUES) * 6 + 7
