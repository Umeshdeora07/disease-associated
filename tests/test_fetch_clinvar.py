"""Tests for downloading 7-gene ClinVar subsets (``clinvar.fetch_clinvar_subset``) and the
``scripts/fetch_clinvar.py`` command line. No test touches the network: sources are local files,
``file://`` URLs and an HTTP server on 127.0.0.1."""

from __future__ import annotations

import gzip
import importlib.util
import io
import socket
import subprocess
import sys
import threading
import urllib.error
from collections.abc import Iterator
from datetime import date
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
from flask_sqlalchemy import SQLAlchemy

from ge_ehr.services import clinvar
from ge_ehr.services.clinvar import (
    ClinVarDownloadError,
    SubsetResult,
    clinvar_vcf_url,
    fetch_clinvar_subset,
    import_release,
    subset_file_name,
)
from ge_ehr.services.vcf_parser import BGZF_EOF

REPO_ROOT = Path(__file__).resolve().parent.parent
FORMAT_FIXTURE = Path(__file__).parent / "data" / "clinvar_format_sample.vcf"
SUBSET_NAME = "clinvar_20240630_7genes.vcf.gz"
# The subset keeps 19 of the fixture's 23 records verbatim: the importer later also drops the
# ALT "." record (7:44152000), leaving the 18 classified records.
SUBSET_GENE_COUNTS = {"GCK": 7, "HNF1A": 3, "HNF4A": 2, "HNF1B": 1, "KCNJ11": 2, "ABCC8": 3, "INS": 1}


def fixture_lines() -> list[str]:
    return FORMAT_FIXTURE.read_text(encoding="utf-8").splitlines()


def subset_lines(path: Path) -> list[str]:
    return gzip.decompress(path.read_bytes()).decode().splitlines()


def clinvar_text(*records: str, header: tuple[str, ...] | None = None) -> str:
    lines = header if header is not None else (
        "##fileformat=VCFv4.1", "##fileDate=2025-03-02", "##reference=GRCh38",
        "##ge_ehr_synthetic=true", "##source=GE-EHR synthetic ClinVar-format release (NOT real ClinVar data)",
        "#CHROM\tPOS\tID\tREF\tALT\tQUAL\tFILTER\tINFO",
    )
    return "\n".join([*lines, *records]) + "\n"


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


class _QuietHandler(SimpleHTTPRequestHandler):
    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002 — signature of the base class
        """Keep test output clean."""


@pytest.fixture
def http_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[tuple[str, Path]]:
    """``(base_url, directory)`` of an HTTP server on 127.0.0.1 serving a temporary directory."""
    for name in ("NO_PROXY", "no_proxy"):
        monkeypatch.setenv(name, "127.0.0.1,localhost")
    root = tmp_path / "www"
    root.mkdir()
    server = ThreadingHTTPServer(("127.0.0.1", 0), partial(_QuietHandler, directory=str(root)))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}", root
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def load_fetch_script(monkeypatch: pytest.MonkeyPatch) -> ModuleType:
    module_name = "ge_ehr_script_fetch_clinvar"
    spec = importlib.util.spec_from_file_location(module_name, REPO_ROOT / "scripts" / "fetch_clinvar.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, module_name, module)
    spec.loader.exec_module(module)
    return module


# ---------------------------------------------------------------------------
# URLs and names
# ---------------------------------------------------------------------------


def test_clinvar_vcf_urls() -> None:
    base = "https://ftp.ncbi.nlm.nih.gov/pub/clinvar/vcf_GRCh38"
    assert clinvar_vcf_url() == f"{base}/clinvar.vcf.gz"
    assert clinvar_vcf_url("20230107") == f"{base}/archive_2.0/2023/clinvar_20230107.vcf.gz"
    for bad in ("2023-01-07", "20231307", "2023", "latest"):
        with pytest.raises(ValueError, match="must be a date written as YYYYMMDD, e.g. 20250106"):
            clinvar_vcf_url(bad)


def test_subset_file_name() -> None:
    assert subset_file_name("2024-06-30") == SUBSET_NAME
    assert subset_file_name("20240630") == SUBSET_NAME
    with pytest.raises(ValueError, match="is not a date"):
        subset_file_name("June")


# ---------------------------------------------------------------------------
# Filtering
# ---------------------------------------------------------------------------


def assert_fixture_subset(result: SubsetResult | None, output_dir: Path, source: str) -> None:
    assert result is not None
    assert result.path == output_dir / SUBSET_NAME
    assert (result.file_date, result.source, result.total_records, result.kept_records) == ("2024-06-30", source, 23, 19)
    assert result.gene_counts == SUBSET_GENE_COUNTS
    assert list(output_dir.iterdir()) == [result.path], "no .part file is left behind"
    data = result.path.read_bytes()
    assert data[12:14] == b"BC" and data.endswith(BGZF_EOF)

    original, subset = fixture_lines(), subset_lines(result.path)
    header = [line for line in original if line.startswith("#")]
    assert subset[: len(header) - 1] == header[:-1], "the ClinVar header is kept verbatim"
    assert subset[len(header) - 1].startswith(
        "##ge_ehr_subset=GCK,HNF1A,HNF4A,HNF1B,KCNJ11,ABCC8,INS (GENEINFO or GRCh38 panel region) from "
    )
    assert subset[len(header) - 1].endswith("; 19 of 23 records")
    assert subset[len(header)] == header[-1]
    kept = subset[len(header) + 1 :]
    assert kept == [line for line in original if not line.startswith("#") and line in kept]
    positions = {tuple(line.split("\t")[:2]) for line in kept}
    assert ("7", "44139500") in positions, "kept by GENEINFO although outside the padded region"
    assert ("7", "44152000") in positions, "an ALT '.' record is kept verbatim (the importer skips it)"
    assert {("1", "1000123"), ("11", "2170000"), ("MT", "3243"), ("X", "100000")}.isdisjoint(positions)


def test_subset_of_a_local_plain_file(tmp_path: Path) -> None:
    assert_fixture_subset(fetch_clinvar_subset(FORMAT_FIXTURE, tmp_path), tmp_path, str(FORMAT_FIXTURE))


def test_subset_of_a_gzip_file_url(tmp_path: Path) -> None:
    source = tmp_path / "clinvar.vcf.gz"
    source.write_bytes(gzip.compress(FORMAT_FIXTURE.read_bytes()))
    out = tmp_path / "out"
    url = source.as_uri()
    assert_fixture_subset(fetch_clinvar_subset(url, out), out, url)


def test_subset_streamed_over_http(tmp_path: Path, http_root: tuple[str, Path]) -> None:
    base, root = http_root
    (root / "clinvar.vcf.gz").write_bytes(gzip.compress(FORMAT_FIXTURE.read_bytes()))
    out = tmp_path / "out"
    assert_fixture_subset(fetch_clinvar_subset(f"{base}/clinvar.vcf.gz", out), out, f"{base}/clinvar.vcf.gz")


def test_subset_imports_as_a_release(db: SQLAlchemy, tmp_path: Path) -> None:
    result = fetch_clinvar_subset(FORMAT_FIXTURE, tmp_path)
    assert result is not None
    release = import_release(result.path)
    assert release.record_count == 18 and release.file_date == "2024-06-30" and release.is_synthetic


def test_newer_than_skips_old_releases_without_writing(tmp_path: Path) -> None:
    assert fetch_clinvar_subset(FORMAT_FIXTURE, tmp_path, newer_than=date(2024, 6, 30)) is None
    assert fetch_clinvar_subset(FORMAT_FIXTURE, tmp_path, newer_than=date(2025, 1, 1)) is None
    assert list(tmp_path.iterdir()) == []
    assert fetch_clinvar_subset(FORMAT_FIXTURE, tmp_path, newer_than=date(2024, 6, 29)) is not None


def test_existing_subset_is_replaced(tmp_path: Path) -> None:
    (tmp_path / SUBSET_NAME).write_bytes(b"stale")
    result = fetch_clinvar_subset(FORMAT_FIXTURE, tmp_path)
    assert result is not None and result.path.read_bytes() != b"stale"


def test_undecodable_bytes_do_not_abort_the_stream(tmp_path: Path) -> None:
    source = tmp_path / "odd.vcf"
    text = clinvar_text("7\t44145200\t1\tC\tT\t.\t.\tCLNDN=Caf\udcff;GENEINFO=GCK:2645", "", "")
    source.write_bytes(text.encode("utf-8", "surrogateescape"))
    result = fetch_clinvar_subset(source, tmp_path / "out")
    assert result is not None and result.kept_records == 1
    assert "CLNDN=Caf�" in subset_lines(result.path)[-1]


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ("", "has no '#CHROM' header line"),
        ("not a vcf\n", "is not a VCF file (its first line is not '##fileformat=VCF...')."),
        ("##fileformat=VCFv4.1\n##fileDate=2025-01-01\n", "has no '#CHROM' header line; the file is incomplete or not a VCF."),
        ("##fileformat=VCFv4.1\n##fileDate=2025-01-01\n7\t1\n", "has no '#CHROM' header line"),
        (clinvar_text(header=("##fileformat=VCFv4.1", "##reference=GRCh37", "##fileDate=2025-01-01", "#CHROM")),
         "is a GRCh37 ClinVar file; use the GRCh38 release (vcf_GRCh38)."),
        (clinvar_text(header=("##fileformat=VCFv4.1", "##reference=GRCh38", "#CHROM")),
         "has no valid ##fileDate header line; is it a ClinVar VCF?"),
        (clinvar_text("7\tabc\t1\tC\tT\t.\t.\t."), "source.vcf, line 7: not a valid VCF record."),
        (clinvar_text("7\t44145200\t1\tC"), "source.vcf, line 7: not a valid VCF record."),
        (clinvar_text("chr\t44145200\t1\tC\tT\t.\t.\t."), "source.vcf, line 7: not a valid VCF record."),
    ],
)
def test_invalid_sources_are_reported(tmp_path: Path, text: str, message: str) -> None:
    source = tmp_path / "source.vcf"
    source.write_text(text, encoding="utf-8")
    out = tmp_path / "out"
    with pytest.raises(ClinVarDownloadError, match=message.replace("(", "\\(").replace(")", "\\)").replace(".", "\\.")):
        fetch_clinvar_subset(source, out)
    assert not out.exists() or list(out.iterdir()) == []


def test_missing_local_file(tmp_path: Path) -> None:
    with pytest.raises(ClinVarDownloadError, match="Cannot open .*missing.vcf.gz: No such file or directory."):
        fetch_clinvar_subset(tmp_path / "missing.vcf.gz", tmp_path)


def test_truncated_gzip_download(tmp_path: Path) -> None:
    source = tmp_path / "clinvar.vcf.gz"
    data = gzip.compress(FORMAT_FIXTURE.read_bytes() * 20)
    source.write_bytes(data[: len(data) // 2])
    with pytest.raises(ClinVarDownloadError, match="clinvar.vcf.gz is truncated or not a valid gzip file; download it again."):
        fetch_clinvar_subset(source, tmp_path / "out")


def test_local_bgzip_file_cut_at_a_block_boundary(tmp_path: Path) -> None:
    complete = fetch_clinvar_subset(FORMAT_FIXTURE, tmp_path / "first")
    assert complete is not None
    cut = tmp_path / "cut.vcf.gz"
    cut.write_bytes(complete.path.read_bytes()[: -len(BGZF_EOF)])
    with pytest.raises(ClinVarDownloadError, match="cut.vcf.gz is incomplete: its bgzip end-of-file marker is missing"):
        fetch_clinvar_subset(cut, tmp_path / "out")


def test_http_404_explains_archived_releases(http_root: tuple[str, Path], tmp_path: Path) -> None:
    base, _root = http_root
    with pytest.raises(ClinVarDownloadError) as info:
        fetch_clinvar_subset(f"{base}/archive_2.0/2023/clinvar_20230101.vcf.gz", tmp_path)
    assert "failed: HTTP 404" in str(info.value)
    assert "Archived releases exist only for specific dates; see " in str(info.value)


def test_other_http_errors_are_reported(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def unavailable(request: Any, timeout: float) -> None:
        raise urllib.error.HTTPError(request.full_url, 503, "Service Unavailable", {}, None)  # type: ignore[arg-type]

    monkeypatch.setattr(clinvar.urllib.request, "urlopen", unavailable)
    with pytest.raises(ClinVarDownloadError) as info:
        fetch_clinvar_subset("https://ftp.ncbi.nlm.nih.gov/pub/clinvar/vcf_GRCh38/clinvar.vcf.gz", tmp_path)
    assert str(info.value) == (
        "Download of https://ftp.ncbi.nlm.nih.gov/pub/clinvar/vcf_GRCh38/clinvar.vcf.gz failed: "
        "HTTP 503 Service Unavailable."
    )


def test_unreachable_host_gets_a_helpful_message(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("NO_PROXY", "no_proxy"):
        monkeypatch.setenv(name, "127.0.0.1,localhost")
    url = f"http://127.0.0.1:{free_port()}/clinvar.vcf.gz"
    with pytest.raises(ClinVarDownloadError) as info:
        fetch_clinvar_subset(url, tmp_path)
    message = str(info.value)
    assert message.startswith(f"Could not reach {url}: ")
    assert "NCBI may be unreachable from this network" in message and "(--input)" in message


class _InterruptedStream(io.RawIOBase):
    """A download that dies after ``limit`` bytes."""

    def __init__(self, data: bytes, limit: int) -> None:
        self._data, self._limit, self._offset = data, limit, 0

    def readable(self) -> bool:
        return True

    def readinto(self, buffer: Any) -> int:
        if self._offset >= self._limit:
            raise ConnectionResetError("Connection reset by peer")
        chunk = self._data[self._offset : min(self._limit, self._offset + len(buffer))]
        buffer[: len(chunk)] = chunk
        self._offset += len(chunk)
        return len(chunk)


def test_interrupted_download_writes_nothing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    data = gzip.compress(FORMAT_FIXTURE.read_bytes() * 10)
    monkeypatch.setattr(clinvar, "_open_source", lambda location: _InterruptedStream(data, len(data) // 2))
    with pytest.raises(ClinVarDownloadError, match="The download of clinvar.vcf.gz was interrupted .*nothing was written. Run it again."):
        fetch_clinvar_subset("https://example.org/clinvar.vcf.gz", tmp_path)
    assert list(tmp_path.iterdir()) == []


def test_failed_write_removes_the_partial_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def write_half(path: Path, text: str, *, bgzip: bool) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"partial")
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(clinvar, "_write_vcf_text", write_half)
    with pytest.raises(OSError, match="No space left on device"):
        fetch_clinvar_subset(FORMAT_FIXTURE, tmp_path)
    assert list(tmp_path.iterdir()) == []


# ---------------------------------------------------------------------------
# scripts/fetch_clinvar.py
# ---------------------------------------------------------------------------


def test_script_with_a_local_input_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    script = load_fetch_script(monkeypatch)
    assert script.main(["--input", str(FORMAT_FIXTURE), "--output-dir", str(tmp_path)]) == 0
    output = capsys.readouterr().out.splitlines()
    assert output[0] == f"Reading {FORMAT_FIXTURE} ..."
    assert output[1] == "ClinVar fileDate 2024-06-30: kept 19 of 23 records."
    assert output[2:9] == [f"  {gene:<7} {count:>7,}" for gene, count in SUBSET_GENE_COUNTS.items()]
    assert output[9] == f"Wrote {tmp_path / SUBSET_NAME}"
    assert (tmp_path / SUBSET_NAME).is_file()


@pytest.mark.parametrize(
    ("argv", "expected_source"),
    [
        ([], "https://ftp.ncbi.nlm.nih.gov/pub/clinvar/vcf_GRCh38/clinvar.vcf.gz"),
        (["--date", "20230107"], "https://ftp.ncbi.nlm.nih.gov/pub/clinvar/vcf_GRCh38/archive_2.0/2023/clinvar_20230107.vcf.gz"),
        (["--url", "https://mirror.example.org/clinvar.vcf.gz"], "https://mirror.example.org/clinvar.vcf.gz"),
    ],
)
def test_script_source_selection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, argv: list[str], expected_source: str
) -> None:
    script = load_fetch_script(monkeypatch)
    calls: list[tuple[str, Path, date | None]] = []

    def fake_fetch(source: str, output_dir: Path, *, newer_than: date | None = None) -> SubsetResult:
        calls.append((source, output_dir, newer_than))
        return SubsetResult(output_dir / SUBSET_NAME, "2024-06-30", source, 10, 1, dict.fromkeys(SUBSET_GENE_COUNTS, 0))

    monkeypatch.setattr(script, "fetch_clinvar_subset", fake_fetch)
    assert script.main([*argv, "--output-dir", str(tmp_path)]) == 0
    assert calls == [(expected_source, tmp_path, None)]


def test_script_default_output_dir_is_the_git_ignored_real_folder(monkeypatch: pytest.MonkeyPatch) -> None:
    script = load_fetch_script(monkeypatch)
    assert script.build_parser().parse_args([]).output_dir == REPO_ROOT / "data" / "clinvar" / "real"
    assert "data/clinvar/real/" in (REPO_ROOT / ".gitignore").read_text(encoding="utf-8")


def test_script_newer_than(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    script = load_fetch_script(monkeypatch)
    argv = ["--input", str(FORMAT_FIXTURE), "--output-dir", str(tmp_path), "--newer-than", "2024-06-30"]
    assert script.main(argv) == 0
    assert capsys.readouterr().out.splitlines()[-1] == "The release is not newer than 2024-06-30; nothing was written."
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize(
    ("argv", "message"),
    [
        (["--date", "2023-01-07"], "must be a date written as YYYYMMDD"),
        (["--newer-than", "yesterday"], "'yesterday' is not a date written as YYYY-MM-DD"),
        (["--date", "20230107", "--url", "https://x"], "not allowed with argument"),
    ],
)
def test_script_rejects_bad_arguments(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], argv: list[str], message: str
) -> None:
    script = load_fetch_script(monkeypatch)
    with pytest.raises(SystemExit) as info:
        script.main(argv)
    assert info.value.code == 2
    assert message in capsys.readouterr().err


def test_script_reports_download_errors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    script = load_fetch_script(monkeypatch)
    for name in ("NO_PROXY", "no_proxy"):
        monkeypatch.setenv(name, "127.0.0.1,localhost")
    assert script.main(["--url", f"http://127.0.0.1:{free_port()}/x.vcf.gz", "--output-dir", str(tmp_path)]) == 1
    assert capsys.readouterr().err.startswith("error: Could not reach http://127.0.0.1:")


def test_script_runs_as_a_program(tmp_path: Path) -> None:
    completed = subprocess.run(
        [sys.executable, "scripts/fetch_clinvar.py", "--input", str(FORMAT_FIXTURE), "--output-dir", str(tmp_path)],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert completed.returncode == 0, completed.stderr
    assert "kept 19 of 23 records" in completed.stdout and (tmp_path / SUBSET_NAME).is_file()
