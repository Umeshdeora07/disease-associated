"""Local install on Windows, macOS and Linux (portability work package).

* ``run.py``: binds 127.0.0.1:5000 with waitress by default (``--dev``: the Flask development
  server), works without a ``.env`` file, creates the instance folder, the tables and a
  persistent random SECRET_KEY, and reports bad settings, a taken port or Ctrl+C without a
  traceback.
* :func:`ge_ehr.config.instance_secret_key`: persistence, exclusive creation, permissions.
* Text files: every text ``open()``/``read_text()``/``write_text()`` in ``ge_ehr/``,
  ``evaluation/``, ``scripts/`` and ``run.py`` names its encoding (Windows defaults to the ANSI
  code page, not UTF-8), no ``NamedTemporaryFile`` is reopened while open (Windows refuses), and
  the files GE-EHR writes are UTF-8 with LF line endings.
* ``requirements*.txt`` and ``pyproject.toml`` pin the same packages.
"""

from __future__ import annotations

import ast
import errno
import importlib.util
import json
import logging
import os
import re
import stat
import subprocess
import sys
import tomllib
from collections.abc import Iterator
from pathlib import Path
from types import ModuleType
from typing import Any

import dotenv.main
import flask
import pytest
from sqlalchemy import inspect

from ge_ehr import config as config_module
from ge_ehr.config import DEVELOPMENT_SECRET_KEY, SECRET_KEY_FILE_NAME, instance_secret_key
from ge_ehr.extensions import db
from tests.factories import make_user

REPO_ROOT = Path(__file__).resolve().parent.parent
RUN_PY = REPO_ROOT / "run.py"
HEX_KEY = re.compile(r"[0-9a-f]{64}")


def _load_run_script() -> ModuleType:
    """``run.py`` as a module (under its own name: ``run`` alone would be ambiguous)."""
    spec = importlib.util.spec_from_file_location("ge_ehr_run_script", RUN_PY)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


run = _load_run_script()


class FakeServer:
    """Stands in for waitress / the Flask development server: records each call."""

    def __init__(self, error: BaseException | None = None) -> None:
        self.calls: list[tuple[flask.Flask, str, int, int]] = []
        self.error = error

    def __call__(self, app: flask.Flask, host: str, port: int, threads: int) -> None:
        self.calls.append((app, host, port, threads))
        if self.error is not None:
            raise self.error

    @property
    def app(self) -> flask.Flask:
        return self.calls[-1][0]


@pytest.fixture
def local_install(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[Path]:
    """A fresh local install: no .env file anywhere, no settings, instance folder ``tmp/instance``.

    ``find_dotenv`` finds nothing, so ``create_app`` runs python-dotenv's real loading code on a
    computer without ``.env``. Returns the (not yet existing) instance folder.
    """
    instance = tmp_path / "instance"
    monkeypatch.setattr(dotenv.main, "find_dotenv", lambda *args, **kwargs: "")
    monkeypatch.setattr(flask.Flask, "auto_find_instance_path", lambda self: str(instance))
    for name in (
        "FLASK_CONFIG", "SECRET_KEY", "DATABASE_URL", "DEMO_PASSWORD", "CLINVAR_SYNC_URL", "LOG_LEVEL",
        "TRUSTED_PROXIES", "GE_EHR_VCF_BACKEND", "WERKZEUG_RUN_MAIN",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.chdir(tmp_path)
    package_logger = logging.getLogger("ge_ehr")
    handlers, level = list(package_logger.handlers), package_logger.level
    try:
        yield instance
    finally:  # the development configuration adds a stderr handler to the package logger
        package_logger.handlers[:] = handlers
        package_logger.setLevel(level)


def _dispose(app: flask.Flask) -> None:
    with app.app_context():
        db.engine.dispose()


# ---------------------------------------------------------------------------
# run.py: options
# ---------------------------------------------------------------------------


def test_defaults_are_this_computer_port_5000_and_waitress() -> None:
    args = run.parse_args([])
    assert (args.host, args.port, args.dev, args.threads) == ("127.0.0.1", 5000, False, 8)
    args = run.parse_args(["--port", "8080", "--dev", "--threads", "2", "--host", "localhost"])
    assert (args.host, args.port, args.dev, args.threads) == ("localhost", 8080, True, 2)


@pytest.mark.parametrize("port", ["0", "-1", "65536", "abc", ""])
def test_invalid_ports_are_refused(port: str, capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exit_info:
        run.parse_args(["--port", port])
    assert exit_info.value.code == 2
    assert "--port" in capsys.readouterr().err


@pytest.mark.parametrize(
    ("host", "loopback"),
    [
        ("127.0.0.1", True), ("127.1.2.3", True), ("::1", True), ("[::1]", True), ("localhost", True),
        ("LOCALHOST", True), ("0.0.0.0", False), ("::", False), ("192.168.1.20", False), ("my-pc", False),
    ],
)
def test_is_loopback(host: str, loopback: bool) -> None:
    assert run.is_loopback(host) is loopback


@pytest.mark.parametrize(
    ("host", "port", "url"),
    [
        ("127.0.0.1", 5000, "http://127.0.0.1:5000"),
        ("localhost", 5000, "http://localhost:5000"),
        ("0.0.0.0", 8080, "http://127.0.0.1:8080"),
        ("::", 5000, "http://127.0.0.1:5000"),
        ("::1", 5000, "http://[::1]:5000"),
    ],
)
def test_browser_url(host: str, port: int, url: str) -> None:
    assert run.browser_url(host, port) == url


def test_help_works_from_another_folder(tmp_path: Path) -> None:
    result = subprocess.run(
        [sys.executable, str(RUN_PY), "--help"], cwd=tmp_path, capture_output=True, text=True, timeout=60, check=False
    )
    assert result.returncode == 0, result.stderr
    assert "--dev" in result.stdout and "127.0.0.1" in result.stdout and "--port" in result.stdout


# ---------------------------------------------------------------------------
# run.py: start-up
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("database", ["default", "elsewhere"])
def test_first_start_without_env_file_creates_instance_tables_and_key(
    local_install: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    database: str,
) -> None:
    if database == "elsewhere":  # create_app then leaves the instance folder alone; run.py creates it
        monkeypatch.setenv("DATABASE_URL", f"sqlite:///{(tmp_path / 'elsewhere.db').as_posix()}")
    assert not local_install.exists()
    server = FakeServer()
    try:
        assert run.main([], serve=server) == 0
        ((app, host, port, threads),) = server.calls
        assert (host, port, threads) == ("127.0.0.1", 5000, 8)
        assert app.config["CONFIG_NAME"] == "development" and app.debug is False
        assert app.instance_path == str(local_install) and local_install.is_dir()
        key_file = local_install / SECRET_KEY_FILE_NAME
        key = key_file.read_text(encoding="utf-8").strip()
        assert HEX_KEY.fullmatch(key) and app.config["SECRET_KEY"] == key != DEVELOPMENT_SECRET_KEY
        expected_db = local_install / "ge_ehr.db" if database == "default" else tmp_path / "elsewhere.db"
        assert expected_db.is_file()
        with app.app_context():
            assert {"users", "patients", "audit_log"} <= set(inspect(db.engine).get_table_names())
    finally:
        if server.calls:
            _dispose(server.app)
    banner = capsys.readouterr().out
    assert "Open http://127.0.0.1:5000 in your browser. Press Ctrl+C to stop." in banner
    assert "Server:     waitress, configuration 'development'" in banner
    assert "VCF reader:" in banner
    assert "The database has no users yet" in banner and "flask --app wsgi seed-demo" in banner
    assert "Demo-Pass-2026!" in banner and "WARNING" not in banner
    assert banner.isascii()  # any Windows console code page can print it


def test_the_secret_key_survives_restarts_and_differs_per_install(local_install: Path, tmp_path: Path) -> None:
    keys = []
    for _restart in range(2):
        server = FakeServer()
        assert run.main([], serve=server) == 0
        keys.append(server.app.config["SECRET_KEY"])
        _dispose(server.app)
    assert keys[0] == keys[1]
    assert instance_secret_key(tmp_path / "another-install") != keys[0]


def test_a_configured_secret_key_is_used_and_no_key_file_written(
    local_install: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SECRET_KEY", "configured-secret-key-0123456789")
    server = FakeServer()
    assert run.main([], serve=server) == 0
    assert server.app.config["SECRET_KEY"] == "configured-secret-key-0123456789"
    assert not (local_install / SECRET_KEY_FILE_NAME).exists()
    _dispose(server.app)


def test_dev_uses_the_flask_development_server(
    local_install: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    waitress_server, flask_server = FakeServer(), FakeServer()
    monkeypatch.setattr(run, "serve_with_waitress", waitress_server)
    monkeypatch.setattr(run, "serve_with_flask", flask_server)
    assert run.main(["--dev", "--port", "5050"]) == 0
    assert not waitress_server.calls and [call[1:3] for call in flask_server.calls] == [("127.0.0.1", 5050)]
    assert "Server:     Flask development server (auto-reload, debugger)" in capsys.readouterr().out
    _dispose(flask_server.app)

    monkeypatch.setenv("WERKZEUG_RUN_MAIN", "true")  # the reloader's child process: no second banner
    assert run.main(["--dev"]) == 0
    assert capsys.readouterr().out == ""
    _dispose(flask_server.app)

    monkeypatch.delenv("WERKZEUG_RUN_MAIN")
    assert run.main([]) == 0
    assert len(waitress_server.calls) == 1 and len(flask_server.calls) == 2
    _dispose(waitress_server.app)


def test_dev_refuses_other_computers(local_install: Path, capsys: pytest.CaptureFixture[str]) -> None:
    server = FakeServer()
    assert run.main(["--dev", "--host", "0.0.0.0"], serve=server) == 2
    assert not server.calls and not local_install.exists()
    assert "only allowed on 127.0.0.1" in capsys.readouterr().err


def test_listening_on_the_network_warns(local_install: Path, capsys: pytest.CaptureFixture[str]) -> None:
    server = FakeServer()
    assert run.main(["--host", "0.0.0.0"], serve=server) == 0
    assert server.calls[0][1] == "0.0.0.0"
    banner = capsys.readouterr().out
    assert "Open http://127.0.0.1:5000" in banner and "WARNING: listening on 0.0.0.0" in banner
    _dispose(server.app)


def test_no_demo_hint_once_there_are_users(local_install: Path) -> None:
    app = run.build_app()
    with app.app_context():
        make_user("dr.local")
        db.session.commit()
    banner = "\n".join(run.describe(app, "127.0.0.1", 5000, dev=False))
    assert "no users yet" not in banner and "Database:   sqlite:///" in banner
    _dispose(app)


@pytest.mark.parametrize(
    ("variable", "value", "message"),
    [
        ("FLASK_CONFIG", "production", "run.py: SECRET_KEY must be set in the environment for the production"),
        ("GE_EHR_VCF_BACKEND", "htslib", "run.py: GE_EHR_VCF_BACKEND='htslib' is not valid"),
        ("LOG_LEVEL", "LOUD", "run.py: LOG_LEVEL 'LOUD' is not a valid logging level."),
        ("FLASK_CONFIG", "staging", "run.py: Unknown configuration 'staging'"),
    ],
)
def test_bad_settings_are_reported_without_a_traceback(
    local_install: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    variable: str,
    value: str,
    message: str,
) -> None:
    monkeypatch.setenv(variable, value)
    server = FakeServer()
    assert run.main([], serve=server) == 1
    assert not server.calls
    err = capsys.readouterr().err
    assert err.startswith(message) and "Traceback" not in err


def test_a_taken_port_is_reported(local_install: Path, capsys: pytest.CaptureFixture[str]) -> None:
    server = FakeServer(OSError(errno.EADDRINUSE, "Address already in use"))
    assert run.main(["--port", "5000"], serve=server) == 1
    err = capsys.readouterr().err
    assert "cannot listen on 127.0.0.1:5000" in err and "python run.py --port 5001" in err
    _dispose(server.app)


@pytest.mark.parametrize("error", [None, KeyboardInterrupt()], ids=["waitress", "flask"])
def test_ctrl_c_stops_cleanly(
    local_install: Path, capsys: pytest.CaptureFixture[str], error: BaseException | None
) -> None:
    """waitress returns on Ctrl+C, the Flask development server raises KeyboardInterrupt."""
    server = FakeServer(error)
    assert run.main([], serve=server) == 0
    assert capsys.readouterr().out.rstrip().endswith("GE-EHR stopped.")
    _dispose(server.app)


def test_serve_with_waitress_passes_the_options(monkeypatch: pytest.MonkeyPatch) -> None:
    import waitress

    calls: list[tuple[Any, dict[str, Any]]] = []
    monkeypatch.setattr(waitress, "serve", lambda app, **options: calls.append((app, options)))
    app = flask.Flask("fake")
    run.serve_with_waitress(app, "127.0.0.1", 5001, 4)
    assert calls == [(app, {"host": "127.0.0.1", "port": 5001, "threads": 4, "ident": "GE-EHR"})]


def test_serve_with_flask_enables_the_debugger_and_reloader(monkeypatch: pytest.MonkeyPatch) -> None:
    app = flask.Flask("fake")
    calls: list[dict[str, Any]] = []
    monkeypatch.setattr(app, "run", lambda **options: calls.append(options))
    run.serve_with_flask(app, "127.0.0.1", 5002, 8)
    assert calls == [{"host": "127.0.0.1", "port": 5002, "debug": True, "use_reloader": True}]


# ---------------------------------------------------------------------------
# Persistent SECRET_KEY
# ---------------------------------------------------------------------------


def test_instance_secret_key_is_created_once(tmp_path: Path) -> None:
    folder = tmp_path / "missing" / "instance"
    key = instance_secret_key(folder)
    assert HEX_KEY.fullmatch(key)
    assert (folder / SECRET_KEY_FILE_NAME).read_bytes() == key.encode("ascii") + b"\n"  # LF on every platform
    assert instance_secret_key(folder) == key == instance_secret_key(str(folder))


@pytest.mark.skipif(os.name == "nt", reason="POSIX permission bits")
def test_instance_secret_key_is_private_to_its_owner(tmp_path: Path) -> None:
    instance_secret_key(tmp_path)
    assert stat.S_IMODE((tmp_path / SECRET_KEY_FILE_NAME).stat().st_mode) == 0o600


def test_an_edited_key_file_is_read_as_text(tmp_path: Path) -> None:
    (tmp_path / SECRET_KEY_FILE_NAME).write_bytes(b"  my-own-long-secret-key-text\r\n")  # e.g. saved by Notepad
    assert instance_secret_key(tmp_path) == "my-own-long-secret-key-text"


def test_an_empty_key_file_is_replaced(tmp_path: Path) -> None:
    (tmp_path / SECRET_KEY_FILE_NAME).write_bytes(b"")
    key = instance_secret_key(tmp_path)
    assert HEX_KEY.fullmatch(key) and instance_secret_key(tmp_path) == key


def test_a_short_key_is_refused(tmp_path: Path) -> None:
    (tmp_path / SECRET_KEY_FILE_NAME).write_text("short\n", encoding="utf-8")
    with pytest.raises(RuntimeError, match="shorter than 16 characters; delete the file"):
        instance_secret_key(tmp_path)


def test_a_key_created_by_a_parallel_start_wins(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Two processes starting at once: the one that loses the exclusive create reads the winner's key."""
    path = tmp_path / SECRET_KEY_FILE_NAME
    real_open = os.open

    def open_after_the_other_process(file: Any, flags: int, mode: int = 0o777) -> int:
        if Path(file) == path and flags & os.O_EXCL:
            monkeypatch.setattr(config_module.os, "open", real_open)
            path.write_text("the-other-processes-secret-key\n", encoding="utf-8")
            raise FileExistsError(errno.EEXIST, "File exists", str(file))
        return real_open(file, flags, mode)

    monkeypatch.setattr(config_module.os, "open", open_after_the_other_process)
    assert instance_secret_key(tmp_path) == "the-other-processes-secret-key"


# ---------------------------------------------------------------------------
# Text files: explicit UTF-8, no reopened NamedTemporaryFile
# ---------------------------------------------------------------------------


def _source_files() -> list[Path]:
    files = [path for folder in ("ge_ehr", "evaluation", "scripts") for path in (REPO_ROOT / folder).rglob("*.py")]
    return sorted(files) + [RUN_PY, REPO_ROOT / "wsgi.py"]


def _text_mode(call: ast.Call, position: int) -> str | None:
    """The literal ``mode`` of an open() call (argument ``position`` or ``mode=``); None if not literal."""
    node = call.args[position] if len(call.args) > position else None
    for keyword in call.keywords:
        if keyword.arg == "mode":
            node = keyword.value
    if node is None:
        return "r"
    return node.value if isinstance(node, ast.Constant) and isinstance(node.value, str) else None


def _is_name(node: ast.expr | None, *names: str) -> bool:
    return isinstance(node, ast.Name) and node.id in names


def text_io_problems(path: Path, root: Path = REPO_ROOT) -> list[str]:
    """Calls in ``path`` that read or write text in the locale's encoding, and reopened temporary files."""
    problems = []
    for call in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if not isinstance(call, ast.Call):
            continue
        function = call.func
        name = function.attr if isinstance(function, ast.Attribute) else getattr(function, "id", None)
        owner = function.value if isinstance(function, ast.Attribute) else None
        keywords = {keyword.arg: keyword.value for keyword in call.keywords}
        where = f"{path.relative_to(root).as_posix()}:{call.lineno}"
        if name == "NamedTemporaryFile":
            if not any(_is_false(keywords.get(key)) for key in ("delete", "delete_on_close")):
                problems.append(f"{where}: NamedTemporaryFile without delete=False cannot be reopened on Windows")
            continue
        if "encoding" in keywords or None in keywords or any(isinstance(arg, ast.Starred) for arg in call.args):
            continue
        if name in ("read_text", "write_text", "TextIOWrapper"):
            problems.append(f"{where}: {name}() without encoding=")
        elif name == "open" and not _is_name(owner, "os"):
            # open(file, mode) / io.open / gzip.open take the mode second, Path.open(mode) first
            mode = _text_mode(call, 1 if owner is None or _is_name(owner, "io", "gzip", "builtins") else 0)
            binary = mode is None or "b" in mode or (_is_name(owner, "gzip") and "t" not in mode)
            if not binary:  # (a mode that is not a literal is not checked)
                problems.append(f"{where}: open() in text mode without encoding=")
        elif name == "fdopen" and "b" not in (_text_mode(call, 1) or "b"):
            problems.append(f"{where}: os.fdopen() in text mode without encoding=")
    return sorted(problems, key=lambda problem: int(problem.rsplit(":", 2)[-2]))


def _is_false(node: ast.expr | None) -> bool:
    return isinstance(node, ast.Constant) and node.value is False


def test_every_text_file_call_names_its_encoding() -> None:
    problems = [problem for path in _source_files() for problem in text_io_problems(path)]
    assert problems == []


def test_the_encoding_check_finds_what_windows_would_get_wrong(tmp_path: Path) -> None:
    sample = tmp_path / "sample.py"
    sample.write_text(
        "import gzip, io, os, tempfile\n"
        "from pathlib import Path\n"
        "open('a')\n"  # 3
        "open('a', 'w')\n"  # 4
        "open('a', 'rb')\n"
        "open('a', encoding='utf-8')\n"
        "Path('a').read_text()\n"  # 7
        "Path('a').write_text('x', encoding='utf-8')\n"
        "Path('a').open('w')\n"  # 9
        "Path('a').open(mode='wb')\n"
        "gzip.open('a.gz', 'rt')\n"  # 11
        "gzip.open('a.gz')\n"
        "io.TextIOWrapper(raw)\n"  # 13
        "os.open('a', os.O_RDONLY)\n"
        "os.fdopen(3, 'w')\n"  # 15
        "tempfile.NamedTemporaryFile(suffix='.vcf')\n"  # 16
        "tempfile.NamedTemporaryFile(delete=True)\n"  # 17
        "tempfile.NamedTemporaryFile(delete=False)\n"
        "open('a', mode)\n",
        encoding="utf-8",
    )
    lines = [int(problem.split(":")[1]) for problem in text_io_problems(sample, tmp_path)]
    assert lines == [3, 4, 7, 9, 11, 13, 15, 16, 17]


_WRITE_AND_READ_FILES = r"""
import io, json, os, sys, warnings
from datetime import date
from pathlib import Path

out = Path(sys.argv[1])
with warnings.catch_warnings(record=True) as caught:
    warnings.simplefilter("always", EncodingWarning)
    from evaluation.cohort import CohortMedication, CohortPatient, load_compact_cohort, write_compact_cohort
    from evaluation.cohort import write_patient_vcf
    from evaluation.run import write_results
    from ge_ehr.config import instance_secret_key
    from ge_ehr.demo import load_demo_patients
    from ge_ehr.services.clinvar import iter_clinvar_records, load_expert_panel_overlay, read_vcf_header_meta
    from ge_ehr.services.vcf_parser import parse_patient_vcf

    instance_secret_key(out / "instance")
    patient = CohortPatient("GEN-000001", "Zoë", "Ñúñez-Ørsted", "female", date(1990, 5, 17), "type1", 120,
                            [CohortMedication("insulin glargine", "insulin", date(2020, 1, 2))], None)
    for name in ("cohort.csv", "cohort.csv.gz"):
        write_compact_cohort([patient], out / name)
        assert load_compact_cohort(out / name) == [patient]
    write_patient_vcf(out / "patient.vcf", "Pátient-1", [])
    write_results(json.loads(Path("evaluation/results/results.json").read_text(encoding="utf-8")), out / "results")
    assert load_demo_patients()
    (out / "overlay.csv").write_text(
        "clinvar_variation_id,classification,panel,date\n9000003,P,Grüppe VCEP — synthetic,2025-06-02\n",
        encoding="utf-8")
    assert load_expert_panel_overlay(out / "overlay.csv")[9000003].panel == "Grüppe VCEP — synthetic"
    source = Path("tests/data/clinvar_format_sample.vcf").read_text(encoding="utf-8")
    (out / "clinvar.vcf").write_text(source.replace("##fileformat=VCFv4.1\n", "##fileformat=VCFv4.1\n##source=Ünïcode\n"),
                                     encoding="utf-8")
    assert read_vcf_header_meta(out / "clinvar.vcf")["source"] == "Ünïcode"
    for backend in ("python", "cyvcf2", "auto"):
        os.environ["GE_EHR_VCF_BACKEND"] = backend
        try:
            list(iter_clinvar_records(out / "clinvar.vcf"))
            parse_patient_vcf(Path("data/demo/vcfs/DEMO-0001.vcf").read_bytes())
        except RuntimeError:  # cyvcf2 not installed
            pass
folders = [Path.cwd() / name for name in ("ge_ehr", "evaluation", "scripts")]
ours = [w for w in caught if issubclass(w.category, EncodingWarning)
        and any(Path(w.filename).resolve().is_relative_to(folder) for folder in folders)]
print(json.dumps([f"{w.filename}:{w.lineno}: {w.message}" for w in ours]))
"""


def test_files_are_written_and_read_as_utf8_whatever_the_locale(tmp_path: Path) -> None:
    """Run the text-file writers and readers with Python's EncodingWarning switched on (PEP 597):
    any call that would fall back to the locale's encoding (cp1252 on Windows) is reported."""
    env = {key: value for key, value in os.environ.items() if key != "GE_EHR_VCF_BACKEND"}
    result = subprocess.run(
        [sys.executable, "-X", "warn_default_encoding", "-c", _WRITE_AND_READ_FILES, str(tmp_path)],
        cwd=REPO_ROOT, env=env, capture_output=True, text=True, encoding="utf-8", timeout=300, check=False,
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout.strip().splitlines()[-1]) == []

    written = {
        "instance/secret_key": None,
        "cohort.csv": "Ñúñez-Ørsted",
        "patient.vcf": "Pátient-1",
        "results/results.json": None,
        "results/RESULTS.md": None,
    }
    for name, text in written.items():
        data = (tmp_path / name).read_bytes()
        decoded = data.decode("utf-8")  # UTF-8, not the locale's code page
        assert b"\r\n" not in data, f"{name} has CRLF line endings"
        if text is not None:
            assert text in decoded
    assert json.loads((tmp_path / "results/results.json").read_text(encoding="utf-8")) == json.loads(
        (REPO_ROOT / "evaluation/results/results.json").read_text(encoding="utf-8")
    )


# ---------------------------------------------------------------------------
# Requirements
# ---------------------------------------------------------------------------


def _requirements(name: str) -> list[str]:
    """Requirement lines of a requirements file (comments, blank and ``-r`` lines dropped)."""
    lines = []
    for raw in (REPO_ROOT / name).read_text(encoding="utf-8").splitlines():
        line = raw.split(" #", 1)[0].strip()
        if line and not line.startswith(("#", "-r")):
            lines.append(line)
    return lines


def _normalised(requirement: str) -> str:
    return re.sub(r"\s+", "", requirement).replace("'", '"').lower()


def test_pyproject_pins_the_same_packages_as_the_requirements_files() -> None:
    project = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    assert sorted(map(_normalised, project["dependencies"])) == sorted(map(_normalised, _requirements("requirements.txt")))
    assert sorted(map(_normalised, project["optional-dependencies"]["dev"])) == sorted(
        map(_normalised, _requirements("requirements-dev.txt"))
    )


def test_requirements_are_pinned_and_install_on_windows() -> None:
    runtime, dev = _requirements("requirements.txt"), _requirements("requirements-dev.txt")
    assert "-r requirements.txt" in (REPO_ROOT / "requirements-dev.txt").read_text(encoding="utf-8").splitlines()
    for requirement in runtime + dev:
        assert re.fullmatch(r"[A-Za-z0-9_.\-]+==[0-9][0-9A-Za-z.]*(;.+)?", requirement.replace(" ", "")), requirement
    names = {requirement.split("==")[0].lower() for requirement in runtime}
    assert {"flask", "waitress", "python-dotenv", "cyvcf2"} <= names
    assert "gunicorn" not in names  # POSIX only; waitress serves the app everywhere
    (cyvcf2_line,) = [requirement for requirement in runtime if requirement.lower().startswith("cyvcf2")]
    assert _normalised(cyvcf2_line).endswith(';platform_system!="windows"')  # no Windows wheels
