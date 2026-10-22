"""Tests for the ``flask clinvar`` command group: import, activate, list and sync (SPEC §5.3).

Activation delegates to ``reevaluation.activate_release`` (Reeval work package), which these
tests replace with a stand-in that records the call and returns a finished run.
"""

from __future__ import annotations

import gzip
from collections.abc import Callable
from datetime import date
from pathlib import Path
from typing import Any

import pytest
from flask import Flask
from flask.testing import FlaskCliRunner
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import func, select

from ge_ehr.commands import clinvar_cmds
from ge_ehr.models import KbRelease, KnownVariant, ReevaluationRun, User, utcnow
from ge_ehr.services import reevaluation
from ge_ehr.services.clinvar import import_release, write_clinvar_vcf
from tests.factories import make_release

REPO_ROOT = Path(__file__).resolve().parent.parent
FORMAT_FIXTURE = Path(__file__).parent / "data" / "clinvar_format_sample.vcf"
DEMO_RELEASE_1 = REPO_ROOT / "data" / "demo" / "clinvar" / "demo_release_1.vcf.gz"
OVERLAY_EXAMPLE = REPO_ROOT / "data" / "demo" / "expert_panel_overlay_example.csv"

ActivationCalls = list[tuple[int, str, User | None]]


@pytest.fixture
def activations(monkeypatch: pytest.MonkeyPatch, db: SQLAlchemy) -> ActivationCalls:
    """Replace ``activate_release`` with a stand-in that makes the release current."""
    calls: ActivationCalls = []

    def activate_release(release: KbRelease, *, strategy: str = "change_driven", user: User | None = None) -> ReevaluationRun:
        calls.append((release.id, strategy, user))
        previous = KbRelease.current()
        if previous is not None:
            previous.is_current = False
        sequence = (db.session.scalar(select(func.max(KbRelease.sequence))) or 0) + 1
        release.is_current, release.sequence, release.activated_at = True, sequence, utcnow()
        run = ReevaluationRun(
            trigger="clinvar_release", strategy=strategy, new_release_id=release.id, status="success",
            duration_ms=12.5, stored_keys_compared=15, changed_variants=6, carriers_affected=9,
            patients_reevaluated=9, records_examined=9, alerts_created=7, alerts_superseded=4,
            alerts_confirmed=2, notes_created=1, review_notices=1, notifications=6, finished_at=utcnow(),
        )
        db.session.add(run)
        db.session.flush()
        return run

    monkeypatch.setattr(reevaluation, "activate_release", activate_release)
    return calls


@pytest.fixture
def failing_activation(monkeypatch: pytest.MonkeyPatch, db: SQLAlchemy) -> Callable[[], None]:
    """Make ``activate_release`` fail after changing the session (the command must roll back)."""

    def install(error: Exception | None = None) -> None:
        def activate_release(release: KbRelease, **_: Any) -> ReevaluationRun:
            release.is_current = True
            db.session.flush()
            raise error or RuntimeError("rule set is not loaded")

        monkeypatch.setattr(reevaluation, "activate_release", activate_release)

    return install


def releases(db: SQLAlchemy) -> list[tuple[str, bool, int | None]]:
    db.session.expire_all()
    rows = db.session.scalars(select(KbRelease).order_by(KbRelease.id))
    return [(release.label, release.is_current, release.sequence) for release in rows]


def clinvar_file(path: Path, file_date: str, *records: dict[str, Any]) -> Path:
    write_clinvar_vcf(
        records or [{"chrom": "7", "pos": 44145200, "ref": "C", "alt": "T", "gene": "GCK", "clnsig": "Pathogenic"}],
        path,
        file_date=file_date,
    )
    return path


# ---------------------------------------------------------------------------
# import
# ---------------------------------------------------------------------------


def test_import_without_activation(runner: FlaskCliRunner, db: SQLAlchemy, activations: ActivationCalls) -> None:
    result = runner.invoke(args=["clinvar", "import", str(FORMAT_FIXTURE)])
    assert result.exit_code == 0, result.output
    release = db.session.scalars(select(KbRelease)).one()
    assert result.output.splitlines() == [
        f"Imported synthetic ClinVar-format release 'ClinVar 2024-06-30' (id {release.id}, fileDate 2024-06-30): "
        "18 panel records.",
        "  Classes: P 5, LP 4, VUS 4, LB 2, B 1, NC 2",
        "  Genes:   GCK 6, HNF1A 3, HNF4A 2, HNF1B 1, KCNJ11 2, ABCC8 3, INS 1",
        f"Not activated. Activate it with: flask clinvar activate {release.id}",
    ]
    assert activations == []
    assert releases(db) == [("ClinVar 2024-06-30", False, None)]


def test_import_with_label_date_overlay_and_activation(
    runner: FlaskCliRunner, db: SQLAlchemy, activations: ActivationCalls
) -> None:
    result = runner.invoke(
        args=[
            "clinvar", "import", str(DEMO_RELEASE_1), "--label", "  R1 (2025-01, synthetic) ", "--date", "2025-01-06",
            "--expert-panel", str(OVERLAY_EXAMPLE), "--activate", "--strategy", "full",
        ]
    )
    assert result.exit_code == 0, result.output
    release = db.session.scalars(select(KbRelease)).one()
    assert (release.label, release.release_date, release.is_current, release.sequence) == (
        "R1 (2025-01, synthetic)", date(2025, 1, 6), True, 1
    )
    assert activations == [(release.id, "full", None)]
    lines = result.output.splitlines()
    assert "  Expert-panel overlay applied to 3 variant(s)." in lines
    run = db.session.scalars(select(ReevaluationRun)).one()
    assert lines[-3:] == [
        f"Activated release 'R1 (2025-01, synthetic)' (id {release.id}, sequence 1). "
        f"Re-evaluation run #{run.id} (Full recomputation, success) in 12 ms:",
        "  15 stored keys compared, 6 changed, 9 carriers affected, 9 patients re-evaluated, 9 records examined",
        "  Alerts: 7 created, 4 superseded, 2 confirmed; notes 1; review notices 1; notifications 6",
    ]
    overrides = db.session.scalar(
        select(func.count(KnownVariant.id)).where(KnownVariant.classification_source == "expert_panel_overlay")
    )
    assert overrides == 3


def test_import_default_strategy_is_change_driven(
    runner: FlaskCliRunner, db: SQLAlchemy, activations: ActivationCalls
) -> None:
    result = runner.invoke(args=["clinvar", "import", str(FORMAT_FIXTURE), "--activate"])
    assert result.exit_code == 0, result.output
    assert [strategy for _, strategy, _ in activations] == ["change_driven"]
    assert "(Change-driven, success)" in result.output


def test_import_of_a_duplicate_file_fails_cleanly(runner: FlaskCliRunner, db: SQLAlchemy) -> None:
    assert runner.invoke(args=["clinvar", "import", str(FORMAT_FIXTURE)]).exit_code == 0
    result = runner.invoke(args=["clinvar", "import", str(FORMAT_FIXTURE)])
    assert result.exit_code == 1
    assert "Error: clinvar_format_sample.vcf was already imported as release 'ClinVar 2024-06-30'" in result.output
    assert len(releases(db)) == 1


def test_import_with_an_invalid_overlay_rolls_back(runner: FlaskCliRunner, db: SQLAlchemy, tmp_path: Path) -> None:
    overlay = tmp_path / "panel.csv"
    overlay.write_text("clinvar_variation_id,classification,panel,date\n9100004,Pathogenicish,VCEP,\n", encoding="utf-8")
    result = runner.invoke(args=["clinvar", "import", str(FORMAT_FIXTURE), "--expert-panel", str(overlay)])
    assert result.exit_code == 1
    assert "Error: Expert-panel overlay panel.csv, line 2: unknown classification 'Pathogenicish'" in result.output
    assert releases(db) == []


def test_import_of_an_invalid_file(runner: FlaskCliRunner, db: SQLAlchemy, tmp_path: Path) -> None:
    path = tmp_path / "grch37.vcf"
    path.write_text("##fileformat=VCFv4.1\n##reference=GRCh37\n#CHROM\tPOS\tID\tREF\tALT\tQUAL\tFILTER\tINFO\n", encoding="utf-8")
    result = runner.invoke(args=["clinvar", "import", str(path)])
    assert result.exit_code == 1 and "is a GRCh37 file" in result.output
    assert releases(db) == []


@pytest.mark.parametrize(
    ("args", "message"),
    [
        (["clinvar", "import", "/no/such/file.vcf.gz"], "does not exist"),
        (["clinvar", "import", str(FORMAT_FIXTURE), "--date", "06/01/2025"], "'06/01/2025' does not match the format '%Y-%m-%d'"),
        (["clinvar", "import", str(FORMAT_FIXTURE), "--strategy", "patient"], "'patient' is not one of 'change_driven', 'full'"),
        (["clinvar", "import", str(FORMAT_FIXTURE), "--expert-panel", "/no/overlay.csv"], "does not exist"),
    ],
)
def test_import_usage_errors(runner: FlaskCliRunner, db: SQLAlchemy, args: list[str], message: str) -> None:
    result = runner.invoke(args=args)
    assert result.exit_code == 2 and message in result.output


def test_failed_activation_after_import_keeps_the_imported_release(
    runner: FlaskCliRunner, db: SQLAlchemy, failing_activation: Callable[..., None]
) -> None:
    failing_activation()
    result = runner.invoke(args=["clinvar", "import", str(FORMAT_FIXTURE), "--activate"])
    assert result.exit_code == 1
    release_id = db.session.scalars(select(KbRelease.id)).one()
    assert (
        f"Error: Activation of release 'ClinVar 2024-06-30' (id {release_id}) failed: rule set is not loaded. "
        "The release was not made current."
    ) in result.output
    assert releases(db) == [("ClinVar 2024-06-30", False, None)]


# ---------------------------------------------------------------------------
# activate
# ---------------------------------------------------------------------------


def test_activate_release(runner: FlaskCliRunner, db: SQLAlchemy, activations: ActivationCalls) -> None:
    first = make_release("R1", current=True)
    second = import_release(FORMAT_FIXTURE, label="R2")
    db.session.commit()
    result = runner.invoke(args=["clinvar", "activate", str(second.id), "--strategy", "full"])
    assert result.exit_code == 0, result.output
    assert activations == [(second.id, "full", None)]
    assert f"Activated release 'R2' (id {second.id}, sequence 2)." in result.output
    assert releases(db) == [("R1", False, 1), ("R2", True, 2)]
    assert first.id != second.id


def test_activate_the_current_release_is_a_no_op(runner: FlaskCliRunner, db: SQLAlchemy, activations: ActivationCalls) -> None:
    release = make_release("R1", current=True)
    db.session.commit()
    result = runner.invoke(args=["clinvar", "activate", str(release.id)])
    assert result.exit_code == 0
    assert result.output == f"Release 'R1' (id {release.id}) is already the current release; nothing to do.\n"
    assert activations == []


def test_activate_unknown_release(runner: FlaskCliRunner, db: SQLAlchemy) -> None:
    result = runner.invoke(args=["clinvar", "activate", "42"])
    assert result.exit_code == 1
    assert "Error: There is no knowledge-base release with id 42; see 'flask clinvar list'." in result.output


def test_activate_failure_rolls_back(
    runner: FlaskCliRunner, db: SQLAlchemy, failing_activation: Callable[..., None]
) -> None:
    release = make_release("R9")
    db.session.commit()
    failing_activation(ValueError(""))
    result = runner.invoke(args=["clinvar", "activate", str(release.id)])
    assert result.exit_code == 1
    assert f"Activation of release 'R9' (id {release.id}) failed: ValueError. The release was not made current." in result.output
    assert releases(db) == [("R9", False, None)]


def test_activate_against_the_real_reevaluation_module_reports_its_error(runner: FlaskCliRunner, db: SQLAlchemy) -> None:
    """Whatever ``activate_release`` raises ends as a clean ClickException, never a traceback."""
    release = make_release("R1")
    db.session.commit()
    result = runner.invoke(args=["clinvar", "activate", str(release.id)])
    if result.exit_code == 0:  # the Reeval package's implementation is installed
        assert "Activated release 'R1'" in result.output
    else:
        assert result.exit_code == 1 and "Error: Activation of release 'R1'" in result.output
        assert result.exception is None or isinstance(result.exception, SystemExit)


# ---------------------------------------------------------------------------
# list
# ---------------------------------------------------------------------------


def test_list_without_releases(runner: FlaskCliRunner, db: SQLAlchemy) -> None:
    result = runner.invoke(args=["clinvar", "list"])
    assert result.exit_code == 0
    assert result.output == "No knowledge-base releases. Import one with: flask clinvar import PATH\n"


def test_list_releases(runner: FlaskCliRunner, db: SQLAlchemy) -> None:
    current = make_release("R1 (2025-01, synthetic)", current=True, file_date="2025-01-06", record_count=1515,
                           release_date=date(2025, 1, 6))
    pending = make_release("ClinVar 2026-01-05", source="clinvar", is_synthetic=False, file_date=None, record_count=0)
    pending.release_date = None
    db.session.commit()
    lines = runner.invoke(args=["clinvar", "list"]).output.splitlines()
    assert lines[0].split() == ["ID", "LABEL", "SOURCE", "FILE", "DATE", "RELEASE", "DATE", "RECORDS", "CURRENT", "SEQ",
                                "IMPORTED", "(UTC)"]
    assert set(lines[1]) <= {"-", " "}
    imported = current.imported_at.strftime("%Y-%m-%d %H:%M")
    assert lines[2].split() == [str(current.id), "R1", "(2025-01,", "synthetic)", "synthetic", "2025-01-06",
                                "2025-01-06", "1515", "yes", "1", *imported.split()]
    assert lines[3].split() == [str(pending.id), "ClinVar", "2026-01-05", "clinvar", "-", "-", "0", "-",
                                *pending.imported_at.strftime("%Y-%m-%d %H:%M").split()]


# ---------------------------------------------------------------------------
# sync
# ---------------------------------------------------------------------------


def test_sync_imports_and_activates_a_newer_release(
    runner: FlaskCliRunner, db: SQLAlchemy, activations: ActivationCalls, tmp_path: Path
) -> None:
    make_release("R1", current=True, file_date="2024-01-07")
    db.session.commit()
    source = tmp_path / "clinvar.vcf.gz"
    source.write_bytes(gzip.compress(FORMAT_FIXTURE.read_bytes()))
    out = tmp_path / "real"
    result = runner.invoke(args=["clinvar", "sync", "--url", str(source), "--output-dir", str(out), "--strategy", "full"])
    assert result.exit_code == 0, result.output
    lines = result.output.splitlines()
    assert lines[0] == f"Fetching {source} ..."
    assert lines[1] == f"Wrote {out / 'clinvar_20240630_7genes.vcf.gz'}: 19 of 23 ClinVar records are in the 7-gene panel."
    assert lines[2].startswith("Imported synthetic ClinVar-format release 'ClinVar 2024-06-30'")
    synced = db.session.scalars(select(KbRelease).filter_by(label="ClinVar 2024-06-30")).one()
    assert synced.file_name == "clinvar_20240630_7genes.vcf.gz" and synced.record_count == 18
    assert activations == [(synced.id, "full", None)]
    assert releases(db) == [("R1", False, 1), ("ClinVar 2024-06-30", True, 2)]


def test_sync_skips_a_release_that_is_not_newer(
    runner: FlaskCliRunner, db: SQLAlchemy, activations: ActivationCalls, tmp_path: Path
) -> None:
    make_release("Current", current=True, file_date=None, release_date=date(2024, 6, 30))
    db.session.commit()
    out = tmp_path / "real"
    result = runner.invoke(args=["clinvar", "sync", "--url", str(FORMAT_FIXTURE), "--output-dir", str(out)])
    assert result.exit_code == 0, result.output
    assert result.output.splitlines()[-1] == (
        f"The ClinVar release at {FORMAT_FIXTURE} is not newer than the current release (data of 2024-06-30); "
        "nothing to do."
    )
    assert activations == [] and not out.exists()
    assert len(releases(db)) == 1


def test_sync_activates_a_subset_imported_by_an_earlier_failed_run(
    runner: FlaskCliRunner, db: SQLAlchemy, failing_activation: Callable[..., None], monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    out = tmp_path / "real"
    failing_activation()
    first = runner.invoke(args=["clinvar", "sync", "--url", str(FORMAT_FIXTURE), "--output-dir", str(out)])
    assert first.exit_code == 1 and "was not made current" in first.output
    assert releases(db) == [("ClinVar 2024-06-30", False, None)]

    calls: list[int] = []

    def activate_release(release: KbRelease, *, strategy: str = "change_driven", user: User | None = None) -> ReevaluationRun:
        calls.append(release.id)
        release.is_current, release.sequence = True, 1
        run = ReevaluationRun(trigger="clinvar_release", strategy=strategy, status="success")
        db.session.add(run)
        db.session.flush()
        return run

    monkeypatch.setattr(reevaluation, "activate_release", activate_release)
    second = runner.invoke(args=["clinvar", "sync", "--url", str(FORMAT_FIXTURE), "--output-dir", str(out)])
    assert second.exit_code == 0, second.output
    release_id = db.session.scalars(select(KbRelease.id)).one()
    assert f"clinvar_20240630_7genes.vcf.gz was already imported as release 'ClinVar 2024-06-30' (id {release_id})." in second.output
    assert calls == [release_id]
    assert releases(db) == [("ClinVar 2024-06-30", True, 1)]


def test_sync_of_an_already_current_subset_is_a_no_op(
    runner: FlaskCliRunner, db: SQLAlchemy, activations: ActivationCalls, tmp_path: Path
) -> None:
    out = tmp_path / "real"
    assert runner.invoke(args=["clinvar", "sync", "--url", str(FORMAT_FIXTURE), "--output-dir", str(out)]).exit_code == 0
    release = db.session.scalars(select(KbRelease)).one()
    release.file_date = None  # e.g. a release imported with its fileDate unknown: no date to compare
    release.release_date = None
    db.session.commit()
    result = runner.invoke(args=["clinvar", "sync", "--url", str(FORMAT_FIXTURE), "--output-dir", str(out)])
    assert result.exit_code == 0, result.output
    assert result.output.splitlines()[-1] == "It is already the current release; nothing to do."
    assert len(activations) == 1


def test_sync_uses_the_configured_url(
    app: Flask, runner: FlaskCliRunner, db: SQLAlchemy, activations: ActivationCalls, tmp_path: Path
) -> None:
    newer = clinvar_file(tmp_path / "configured.vcf", "2026-02-01")
    app.config["CLINVAR_SYNC_URL"] = newer.as_uri()
    result = runner.invoke(args=["clinvar", "sync", "--output-dir", str(tmp_path / "real")])
    assert result.exit_code == 0, result.output
    assert result.output.startswith(f"Fetching {newer.as_uri()} ...")
    assert releases(db) == [("ClinVar 2026-02-01", True, 1)]


def test_sync_reports_download_errors(runner: FlaskCliRunner, db: SQLAlchemy, tmp_path: Path) -> None:
    result = runner.invoke(args=["clinvar", "sync", "--url", str(tmp_path / "missing.vcf.gz"), "--output-dir", str(tmp_path)])
    assert result.exit_code == 1
    assert "Error: Cannot open " in result.output and "No such file or directory" in result.output
    assert releases(db) == []


def test_default_subset_dir_is_the_git_ignored_real_folder(app: Flask) -> None:
    assert clinvar_cmds.default_subset_dir() == REPO_ROOT / "data" / "clinvar" / "real"
