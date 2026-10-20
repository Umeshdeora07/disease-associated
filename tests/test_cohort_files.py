"""Tests for scripts/build_cohorts.py and the committed compact cohorts in data/cohorts/."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import re
import shutil
import sys
from pathlib import Path
from types import ModuleType

import pytest

from evaluation.cohort import load_compact_cohort

REPO_ROOT = Path(__file__).resolve().parent.parent
COHORT_DIR = REPO_ROOT / "data" / "cohorts"
SYNTHEA_MINI = Path(__file__).parent / "data" / "synthea_mini"
UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")

METADATA = {
    "runID": "00000000-0000-4000-8000-000000000000",
    "seed": 30,
    "clinicianSeed": 30,
    "referenceTime": "20261001",
    "endTime": "20261001",
    "version": "abc1234\n",
    "patientCount": 7,
    "javaVersion": "21.0.1",
    "generatorThreads": 4,
    "runStartTime": "2026-10-20T09:00:00Z",
    "runTimeInSeconds": 12,
    "exporter.years_of_history": "0",
    "state": "Massachusetts",
    "modules": "*",
}


@pytest.fixture(scope="module")
def build_cohorts() -> ModuleType:
    spec = importlib.util.spec_from_file_location("build_cohorts", REPO_ROOT / "scripts" / "build_cohorts.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules.setdefault("build_cohorts", module)
    spec.loader.exec_module(module)
    return module


def make_synthea_output(root: Path, *, seed: int = 30) -> Path:
    """A Synthea output directory as written by scripts/run_synthea.sh, holding the mini fixture."""
    shutil.copytree(SYNTHEA_MINI, root / "csv")
    (root / "metadata").mkdir()
    (root / "metadata" / "run.json").write_text(json.dumps({**METADATA, "seed": seed}), encoding="utf-8")
    (root / "run_info.txt").write_text(
        "jar_url=https://example.invalid/synthea.jar\njar_sha256=deadbeef\nsynthea_args=-s 30 -p 7 Massachusetts\n",
        encoding="utf-8",
    )
    return root


# ---------------------------------------------------------------------------
# scripts/build_cohorts.py
# ---------------------------------------------------------------------------


def test_build_cohort_keeps_the_first_patients_by_id(build_cohorts: ModuleType, tmp_path: Path) -> None:
    source = make_synthea_output(tmp_path / "synthea")
    spec = build_cohorts.CohortSpec("correctness", "Correctness cohort", 5, "mini_5.csv.gz", "correctness")
    built = build_cohorts.build_cohort(spec, source, tmp_path)
    assert built.living_patients == 8  # the deceased fixture patient is excluded
    patients = load_compact_cohort(tmp_path / "mini_5.csv.gz")
    assert patients == built.patients
    ids = [patient.synthetic_id for patient in patients]
    assert len(ids) == 5 and ids == sorted(ids)
    assert built.sha256 == hashlib.sha256((tmp_path / "mini_5.csv.gz").read_bytes()).hexdigest()
    assert built.run.reference_date.isoformat() == "2026-10-01"


def test_build_cohort_errors(build_cohorts: ModuleType, tmp_path: Path) -> None:
    source = make_synthea_output(tmp_path / "synthea")
    too_big = build_cohorts.CohortSpec("scale", "Scale cohort", 10, "big.csv.gz", "scale")
    with pytest.raises(SystemExit, match="Synthea produced only 8 living patients; the scale cohort needs 10"):
        build_cohorts.build_cohort(too_big, source, tmp_path)
    with pytest.raises(SystemExit, match="run 'scripts/run_synthea.sh scale' first"):
        build_cohorts.build_cohort(too_big, tmp_path / "missing", tmp_path)
    (source / "metadata" / "run.json").unlink()
    with pytest.raises(SystemExit, match="expected exactly one Synthea run metadata file, found 0"):
        build_cohorts.build_cohort(too_big, source, tmp_path)


def test_build_cohort_requires_run_info(build_cohorts: ModuleType, tmp_path: Path) -> None:
    source = make_synthea_output(tmp_path / "synthea")
    (source / "run_info.txt").unlink()
    spec = build_cohorts.CohortSpec("correctness", "Correctness cohort", 5, "x.csv.gz", "correctness")
    with pytest.raises(SystemExit, match=r"run_info\.txt is missing"):
        build_cohorts.build_cohort(spec, source, tmp_path)


def test_main_writes_cohorts_and_readme(build_cohorts: ModuleType, tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(
        build_cohorts,
        "COHORTS",
        (
            build_cohorts.CohortSpec("correctness", "Correctness cohort", 3, "c.csv.gz", "correctness"),
            build_cohorts.CohortSpec("scale", "Scale cohort", 7, "s.csv.gz", "scale"),
        ),
    )
    correctness = make_synthea_output(tmp_path / "c", seed=30)
    scale = make_synthea_output(tmp_path / "s", seed=10000)
    out = tmp_path / "out"
    argv = ["--correctness-dir", str(correctness), "--scale-dir", str(scale), "--output-dir", str(out)]
    assert build_cohorts.main(argv) == 0
    readme = (out / "README.md").read_text(encoding="utf-8")
    first_bytes = {name: (out / name).read_bytes() for name in ("c.csv.gz", "s.csv.gz")}
    assert len(load_compact_cohort(out / "c.csv.gz")) == 3
    assert len(load_compact_cohort(out / "s.csv.gz")) == 7
    # reproducible: a second run gives identical files
    assert build_cohorts.main(argv) == 0
    assert (out / "README.md").read_text(encoding="utf-8") == readme
    assert {name: (out / name).read_bytes() for name in first_bytes} == first_bytes
    for fragment in (
        "**All patients are synthetic**",
        "| `c.csv.gz` | Correctness cohort | 3 | 30 |",
        "| `s.csv.gz` | Scale cohort | 7 | 10000 |",
        "Synthea build `abc1234`",
        "jar SHA-256 `deadbeef`",
        "seed 10000, clinician seed 30, reference and end date",
        "      -s 30 -p 7 Massachusetts",
        "Synthea produced 8 living patients. The file keeps the first",
        "| Diabetes prevalence (any type) | 4 (57.1 %) |",
        "| Diabetes type: Type 2 (`type2`) | 1 (14.3 %) |",
        "| Current medications: Insulin (`insulin`) | 3 record(s), 3 patient(s) |",
        "| Current medications: Sulfonylurea (`sulfonylurea`) | 1 record(s), 1 patient(s) |",
        "| Diabetic patients on a current glucose-lowering drug | 3 of 4 |",
        "| Patients without diabetes on a current glucose-lowering drug | 1 |",
        "| Diabetes type: Other (`other`) | 1 (14.3 %) |",
        "| 3 ML liraglutide 6 MG/ML Pen Injector | `other` | 1 |",
    ):
        assert fragment in readme, fragment
    assert hashlib.sha256(first_bytes["s.csv.gz"]).hexdigest() in readme


# ---------------------------------------------------------------------------
# Committed cohorts (data/cohorts/)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("file_name", "size"), [("synthea_correctness_30.csv.gz", 30), ("synthea_scale_10000.csv.gz", 10_000)]
)
def test_committed_cohort_files(file_name: str, size: int) -> None:
    path = COHORT_DIR / file_name
    patients = load_compact_cohort(path)
    assert len(patients) == size
    ids = [patient.synthetic_id for patient in patients]
    assert ids == sorted(ids) and len(set(ids)) == size
    assert all(UUID.fullmatch(synthetic_id) for synthetic_id in ids)  # Synthea patient UUIDs
    assert all(patient.family_id is None for patient in patients)
    assert all(not re.search(r"\d", patient.given_name + patient.family_name) for patient in patients)
    readme = (COHORT_DIR / "README.md").read_text(encoding="utf-8")
    assert f"| `{file_name}` |" in readme
    assert hashlib.sha256(path.read_bytes()).hexdigest() in readme


def test_committed_scale_cohort_has_diabetic_patients_on_therapy() -> None:
    patients = load_compact_cohort(COHORT_DIR / "synthea_scale_10000.csv.gz")
    diabetic = [patient for patient in patients if patient.diabetes_type != "none"]
    assert 0.03 < len(diabetic) / len(patients) < 0.2
    assert any(patient.medications for patient in diabetic)
    for patient in diabetic:
        assert patient.age_at_diagnosis_months is not None
