#!/usr/bin/env python3
"""Convert the Synthea cohorts into the committed compact cohort files (SPEC §10).

Reads the two Synthea output directories written by ``scripts/run_synthea.sh``. Each holds
``csv/`` (``patients.csv``, ``conditions.csv``, ``medications.csv``), ``metadata/`` (Synthea's run
metadata) and ``run_info.txt``. The script keeps the first N living patients by Synthea patient
Id and writes, with :func:`evaluation.cohort.write_compact_cohort`:

    data/cohorts/synthea_correctness_30.csv.gz     30 patients
    data/cohorts/synthea_scale_10000.csv.gz        10,000 patients
    data/cohorts/README.md                         provenance and summary statistics

The output is byte-for-byte reproducible for the same Synthea output. Families, monogenic
variants and clinical overrides are *not* added here: the evaluation harness does that.

    .venv/bin/python scripts/build_cohorts.py \
        [--correctness-dir synthea/output/correctness_30] [--scale-dir synthea/output/scale_10000] \
        [--output-dir data/cohorts]
"""

from __future__ import annotations

import argparse
import hashlib
import json
import statistics
import sys
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from evaluation.cohort import (  # noqa: E402
    COMPACT_COLUMNS,
    CohortPatient,
    load_synthea_csv,
    months_between,
    write_compact_cohort,
)
from ge_ehr.constants import DIABETES_TYPE_LABELS, DIABETES_TYPES, DRUG_CLASS_LABELS, DRUG_CLASSES  # noqa: E402

DEFAULT_SYNTHEA_OUTPUT = REPO_ROOT / "synthea" / "output"
DEFAULT_OUTPUT_DIR = REPO_ROOT / "data" / "cohorts"
AGE_BANDS: tuple[tuple[str, int, int], ...] = (("0-17", 0, 17), ("18-44", 18, 44), ("45-64", 45, 64), ("65+", 65, 200))


@dataclass(frozen=True)
class CohortSpec:
    """One committed cohort file."""

    name: str
    title: str
    size: int
    file_name: str
    run_target: str  # argument of scripts/run_synthea.sh


COHORTS: tuple[CohortSpec, ...] = (
    CohortSpec("correctness", "Correctness cohort", 30, "synthea_correctness_30.csv.gz", "correctness"),
    CohortSpec("scale", "Scale cohort", 10_000, "synthea_scale_10000.csv.gz", "scale"),
)


@dataclass(frozen=True)
class SyntheaRun:
    """Provenance of one Synthea run: its metadata JSON and the run_info.txt of run_synthea.sh."""

    metadata: dict[str, Any]
    run_info: dict[str, str]

    @property
    def reference_date(self) -> date:
        """Synthea's reference date (``-r``), used as "today" for ages."""
        return datetime.strptime(str(self.metadata["referenceTime"]), "%Y%m%d").date()


@dataclass(frozen=True)
class BuiltCohort:
    """A written cohort file with everything the README reports about it."""

    spec: CohortSpec
    path: Path
    sha256: str
    living_patients: int
    patients: list[CohortPatient]
    run: SyntheaRun


def read_synthea_run(directory: Path) -> SyntheaRun:
    """Read ``metadata/*.json`` (exactly one run) and ``run_info.txt`` of a Synthea output directory."""
    metadata_files = sorted((directory / "metadata").glob("*.json"))
    if len(metadata_files) != 1:
        raise SystemExit(
            f"{directory / 'metadata'}: expected exactly one Synthea run metadata file, found {len(metadata_files)}. "
            "Generate the cohort again with scripts/run_synthea.sh (it starts from an empty directory)."
        )
    metadata = json.loads(metadata_files[0].read_text(encoding="utf-8"))
    info_path = directory / "run_info.txt"
    if not info_path.is_file():
        raise SystemExit(f"{info_path} is missing; generate the cohort with scripts/run_synthea.sh.")
    run_info: dict[str, str] = {}
    for line in info_path.read_text(encoding="utf-8").splitlines():
        key, separator, value = line.partition("=")
        if separator:
            run_info[key.strip()] = value.strip()
    return SyntheaRun(metadata=metadata, run_info=run_info)


def build_cohort(spec: CohortSpec, synthea_dir: Path, output_dir: Path) -> BuiltCohort:
    """Convert one Synthea output directory and write the compact cohort file."""
    csv_dir = synthea_dir / "csv"
    if not (csv_dir / "patients.csv").is_file():
        raise SystemExit(
            f"{csv_dir / 'patients.csv'} not found: run 'scripts/run_synthea.sh {spec.run_target}' first "
            f"(or pass --{spec.name}-dir)."
        )
    run = read_synthea_run(synthea_dir)
    living = load_synthea_csv(csv_dir)
    if len(living) < spec.size:
        raise SystemExit(
            f"{csv_dir}: Synthea produced only {len(living)} living patients; the {spec.name} cohort needs {spec.size}."
        )
    selected = living[: spec.size]  # load_synthea_csv sorts by Synthea patient Id
    path = output_dir / spec.file_name
    write_compact_cohort(selected, path)
    return BuiltCohort(
        spec=spec,
        path=path,
        sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
        living_patients=len(living),
        patients=selected,
        run=run,
    )


# ---------------------------------------------------------------------------
# README
# ---------------------------------------------------------------------------


def _percent(count: int, total: int) -> str:
    share = 100 * count / total if total else 0.0
    return "< 0.1 %" if 0 < share < 0.05 else f"{share:.1f} %"


def _age_years(patient: CohortPatient, on: date) -> int:
    return months_between(patient.birth_date, on) // 12


def _cohort_rows(cohort: BuiltCohort) -> list[tuple[str, str]]:
    """Summary statistics as (label, value) rows."""
    patients, total, today = cohort.patients, len(cohort.patients), cohort.run.reference_date
    sexes = Counter(patient.sex for patient in patients)
    ages = [_age_years(patient, today) for patient in patients]
    bands = Counter(next(label for label, low, high in AGE_BANDS if low <= age <= high) for age in ages)
    diabetic = [patient for patient in patients if patient.diabetes_type != "none"]
    treated = [patient for patient in diabetic if patient.medications]
    rows = [
        ("Patients", f"{total:,}"),
        ("Female / male", f"{sexes['female']:,} / {sexes['male']:,}"),
        ("Age in years at the reference date (median, range)", f"{statistics.median(ages):g} ({min(ages)}–{max(ages)})"),
        ("Age bands " + " / ".join(label for label, _low, _high in AGE_BANDS),
         " / ".join(f"{bands[label]:,}" for label, _low, _high in AGE_BANDS)),
        ("Diabetes prevalence (any type)", f"{len(diabetic):,} ({_percent(len(diabetic), total)})"),
    ]
    types = Counter(patient.diabetes_type for patient in patients)
    rows += [
        (f"Diabetes type: {DIABETES_TYPE_LABELS[kind]} (`{kind}`)", f"{types[kind]:,} ({_percent(types[kind], total)})")
        for kind in DIABETES_TYPES
        if types[kind]
    ]
    diagnosis_ages = [patient.age_at_diagnosis_months for patient in diabetic if patient.age_at_diagnosis_months is not None]
    if diagnosis_ages:
        rows.append(("Age at diagnosis in years (median, diabetic patients)", f"{statistics.median(diagnosis_ages) / 12:.1f}"))
    treated_without_diabetes = sum(1 for p in patients if p.diabetes_type == "none" and p.medications)
    rows += [
        ("Diabetic patients on a current glucose-lowering drug", f"{len(treated):,} of {len(diabetic):,}"),
        ("Patients without diabetes on a current glucose-lowering drug", f"{treated_without_diabetes:,}"),
    ]
    records = Counter(medication.drug_class for patient in patients for medication in patient.medications)
    holders = Counter(drug_class for patient in patients for drug_class in {m.drug_class for m in patient.medications})
    rows += [
        (f"Current medications: {DRUG_CLASS_LABELS[drug_class]} (`{drug_class}`)",
         f"{records[drug_class]:,} record(s), {holders[drug_class]:,} patient(s)")
        for drug_class in DRUG_CLASSES
    ]
    return rows


def _drug_names(cohort: BuiltCohort) -> list[tuple[str, str, int]]:
    names = Counter((m.drug_name, m.drug_class) for patient in cohort.patients for m in patient.medications)
    return sorted(((name, drug_class, count) for (name, drug_class), count in names.items()), key=lambda r: (-r[2], r[0]))


def render_readme(cohorts: Sequence[BuiltCohort]) -> str:
    """Render ``data/cohorts/README.md`` (deterministic for the same inputs)."""
    lines = [
        "# Synthetic patient cohorts (Synthea)",
        "",
        "Compact cohort files for the GE-EHR evaluation (SPEC §10, paper §5), generated with",
        "[Synthea](https://github.com/synthetichealth/synthea) and converted by `scripts/build_cohorts.py`.",
        "**All patients are synthetic** (Synthea simulations); no real patient data is involved.",
        "This file is generated by `scripts/build_cohorts.py`; do not edit it by hand.",
        "",
        "| File | Cohort | Patients | Synthea seed | SHA-256 |",
        "|---|---|---:|---:|---|",
    ]
    for cohort in cohorts:
        lines.append(
            f"| `{cohort.path.name}` | {cohort.spec.title} | {len(cohort.patients):,} | {cohort.run.metadata['seed']} "
            f"| `{cohort.sha256}` |"
        )
    lines += [
        "",
        "## Format",
        "",
        "Gzip-compressed UTF-8 CSV with the header",
        f"`{','.join(COMPACT_COLUMNS)}`, one row per patient sorted by `synthetic_id`. Read the files with",
        "`evaluation.cohort.load_compact_cohort(path)`, which returns `CohortPatient` records.",
        "",
        "* `synthetic_id`: the Synthea patient UUID (`Id` in `patients.csv`).",
        "* `given_name`, `family_name`: Synthea `FIRST`/`LAST` without the numbers Synthea appends to make names",
        "  obviously fake (`Abe604` → `Abe`). `sex`: `female`/`male` from `GENDER`. `birth_date`: `BIRTHDATE`.",
        "* `diabetes_type`: from the SNOMED CT diagnoses in `conditions.csv`. `44054006` *Diabetes mellitus type 2*",
        "  → `type2`, `46635009` → `type1`, `427089005` *Diabetes mellitus due to cystic fibrosis* → `other`",
        "  (preferred over a later type 2 code), etc. Patients without a diabetes diagnosis are `none`; that",
        "  includes prediabetes (`714628002`) and patients with only complications. Synthea's kidney",
        "  submodule assigns *Disorder of kidney due to diabetes mellitus* and similar conditions from",
        "  chronic-kidney-disease state, even to infants without diabetes, so they are not diagnoses.",
        "* `age_at_diagnosis_months`: whole months from `BIRTHDATE` to the earliest diabetes diagnosis `START`;",
        "  empty for `none`.",
        "* `family_id`: empty. Families of 2–5 relatives are formed by the evaluation harness with",
        "  `evaluation.cohort.make_families`, which also gives the relatives a shared surname.",
        "* `medications`: JSON list of the **current** glucose-lowering medications. These are rows of",
        "  `medications.csv` with an empty `STOP` whose description names an insulin, metformin, a",
        "  sulfonylurea, a GLP-1 or GIP/GLP-1 agonist, a DPP-4 or SGLT2 inhibitor, a thiazolidinedione, an",
        "  alpha-glucosidase inhibitor, a meglitinide or pramlintide. Each entry is",
        "  `{drug_name, drug_class, start_date}`, with `drug_class` from `ge_ehr.constants.suggest_drug_class`",
        "  (`insulin`, `metformin`, `sulfonylurea`, everything else `other`). Prescriptions are kept as",
        "  recorded, also for patients without diabetes. Synthea's medication submodule gives insulin to",
        "  every patient with *Disorder of kidney due to diabetes mellitus*, including patients who only",
        "  have prediabetes (\"nephropathy → insulin\", a deliberate simplification in Synthea).",
        "",
        "Deceased patients are excluded; each run asks Synthea for living patients only.",
        "Monogenic variants and clinical overrides are assigned later by the evaluation harness.",
        "Synthea's diabetes care module prescribes only metformin, two insulins, liraglutide and",
        "canagliflozin. The cohorts therefore contain no sulfonylurea users; scenarios that need one",
        "(rules D2/D3 test for it) must come from the evaluation harness's clinical overrides.",
        "",
    ]
    for cohort in cohorts:
        metadata, run_info = cohort.run.metadata, cohort.run.run_info
        modules = metadata.get("modules", "*")
        lines += [
            f"## {cohort.spec.title}: `{cohort.path.name}`",
            "",
            "### Provenance",
            "",
            f"* Synthea build `{str(metadata.get('version', 'unknown')).strip()}` (git commit of the",
            f"  `master-branch-latest` release jar, {run_info.get('jar_url', 'unknown URL')}),",
            f"  jar SHA-256 `{run_info.get('jar_sha256', 'unknown')}`. Java {metadata.get('javaVersion', 'unknown')},",
            f"  {metadata.get('generatorThreads', '?')} generator threads.",
            f"* Generated {metadata.get('runStartTime', 'unknown')} in {metadata.get('runTimeInSeconds', '?')} s:",
            f"  seed {metadata['seed']}, clinician seed {metadata.get('clinicianSeed', '?')}, reference and end date",
            f"  {metadata['referenceTime']}, state {metadata.get('state', '?')}, modules"
            f" {'all (`*`)' if modules == '*' else f'`{modules}`'}, exporter.years_of_history"
            f" {metadata.get('exporter.years_of_history', '?')} (full history).",
            f"* Command: `scripts/run_synthea.sh {cohort.spec.run_target}`, which runs",
            "",
            "  ```",
            "  java -jar synthea-with-dependencies.jar --exporter.baseDirectory=<output dir> \\",
            f"      {run_info.get('synthea_args', '(arguments not recorded)')}",
            "  ```",
            "",
            f"* Selection: Synthea produced {cohort.living_patients:,} living patients. The file keeps the first",
            f"  {len(cohort.patients):,} by Synthea patient `Id`.",
            "",
            "### Contents",
            "",
            "| Statistic | Value |",
            "|---|---|",
            *(f"| {label} | {value} |" for label, value in _cohort_rows(cohort)),
            "",
        ]
        drug_names = _drug_names(cohort)
        if drug_names:
            lines += [
                "Current glucose-lowering medications by name:",
                "",
                "| Medication (Synthea description) | Class | Records |",
                "|---|---|---:|",
                *(f"| {name} | `{drug_class}` | {count:,} |" for name, drug_class, count in drug_names),
                "",
            ]
    lines += [
        "## Reproducing",
        "",
        "```sh",
        "scripts/run_synthea.sh all          # downloads the Synthea jar into synthea/ (git-ignored) if needed",
        ".venv/bin/python scripts/build_cohorts.py",
        "```",
        "",
        "Synthea writes rows in thread completion order, so its raw CSV files differ in row order between",
        "runs. Their content is identical for the same jar, seeds and dates, and the files here are",
        "byte-for-byte reproducible. A different Synthea build can give different patients.",
    ]
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    """Build both compact cohort files and the README; returns the process exit status."""
    parser = argparse.ArgumentParser(description="Convert the Synthea cohorts into data/cohorts/ (SPEC §10).")
    parser.add_argument("--correctness-dir", type=Path, default=DEFAULT_SYNTHEA_OUTPUT / "correctness_30",
                        help="Synthea output directory of the 30-patient run (default: %(default)s)")
    parser.add_argument("--scale-dir", type=Path, default=DEFAULT_SYNTHEA_OUTPUT / "scale_10000",
                        help="Synthea output directory of the 10,000-patient run (default: %(default)s)")
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR,
                        help="where to write the cohort files and README.md (default: %(default)s)")
    args = parser.parse_args(argv)
    sources = {"correctness": args.correctness_dir, "scale": args.scale_dir}
    args.output_dir.mkdir(parents=True, exist_ok=True)
    built = []
    for spec in COHORTS:
        cohort = build_cohort(spec, sources[spec.name], args.output_dir)
        diabetic = sum(1 for patient in cohort.patients if patient.diabetes_type != "none")
        print(f"Wrote {cohort.path} ({len(cohort.patients):,} patients, {diabetic:,} with diabetes)")
        built.append(cohort)
    readme = args.output_dir / "README.md"
    readme.write_text(render_readme(built), encoding="utf-8", newline="\n")
    print(f"Wrote {readme}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
