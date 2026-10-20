"""Synthetic patient cohorts for the GE-EHR evaluation (SPEC §10, paper §5).

* :func:`load_synthea_csv` converts a Synthea CSV export (``patients.csv``, ``conditions.csv``,
  ``medications.csv``) into :class:`CohortPatient` records: demographics, the diabetes type and
  age at diagnosis from the SNOMED CT diagnoses, and the *current* glucose-lowering medications
  mapped to GE-EHR drug classes with :func:`ge_ehr.constants.suggest_drug_class`.
* :func:`write_compact_cohort` / :func:`load_compact_cohort` store cohorts as small, byte-for-byte
  reproducible ``.csv.gz`` files (``data/cohorts/``) with a JSON ``medications`` column.
* :func:`generate_cohort` is a deterministic pure-Python fallback generator with realistic
  distributions, used by the tests and whenever Synthea output is not available.
* :func:`make_families` groups patients into families of 2–5 relatives sharing a surname, so the
  evaluation can give one variant to several relatives (autosomal dominant inheritance).
* :func:`format_patient_vcf` / :func:`write_patient_vcf` write a GRCh38 patient VCF in the style
  of the demo VCFs (``data/demo/vcfs/``).

Variant assignment and clinical overrides are deliberately not done here (Eval work package).
All data handled by this module is synthetic. Owned by the Cohort work package.
"""

from __future__ import annotations

import csv
import gzip
import io
import json
import logging
import os
import random
import re
import zlib
from collections.abc import Iterable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from datetime import date, timedelta
from pathlib import Path
from typing import Literal, NamedTuple, TextIO

from ge_ehr.constants import DIABETES_TYPES, DRUG_CLASSES, SEXES, normalize_chrom, suggest_drug_class

logger = logging.getLogger(__name__)

# "Today" of the synthetic cohorts: the Synthea reference date used by scripts/run_synthea.sh and
# the default of generate_cohort, so ages and dates are reproducible.
REFERENCE_DATE = date(2026, 10, 1)


# ---------------------------------------------------------------------------
# Cohort records
# ---------------------------------------------------------------------------


class CohortMedication(NamedTuple):
    """A current glucose-lowering medication: ``(drug_name, drug_class, start_date)``."""

    drug_name: str
    drug_class: str
    start_date: date | None


@dataclass
class CohortPatient:
    """One synthetic patient of a cohort (demographics, diabetes diagnosis, current therapy).

    ``synthetic_id`` is the Synthea patient UUID (or ``GEN-nnnnnn`` for generated patients);
    ``age_at_diagnosis_months`` is None when there is no diabetes diagnosis.
    """

    synthetic_id: str
    given_name: str
    family_name: str
    sex: str
    birth_date: date
    diabetes_type: str
    age_at_diagnosis_months: int | None
    medications: list[CohortMedication] = field(default_factory=list)
    family_id: str | None = None

    def __post_init__(self) -> None:
        if not self.synthetic_id:
            raise ValueError("synthetic_id must not be empty.")
        if self.sex not in SEXES:
            raise ValueError(f"Unknown sex {self.sex!r}; expected one of {', '.join(SEXES)}.")
        if self.diabetes_type not in DIABETES_TYPES:
            raise ValueError(
                f"Unknown diabetes type {self.diabetes_type!r}; expected one of {', '.join(DIABETES_TYPES)}."
            )
        if self.age_at_diagnosis_months is not None and self.age_at_diagnosis_months < 0:
            raise ValueError(f"age_at_diagnosis_months must be >= 0, got {self.age_at_diagnosis_months}.")
        for medication in self.medications:
            if medication.drug_class not in DRUG_CLASSES:
                raise ValueError(
                    f"Unknown drug class {medication.drug_class!r} for {medication.drug_name!r}; "
                    f"expected one of {', '.join(DRUG_CLASSES)}."
                )


def months_between(start: date, end: date) -> int:
    """Whole calendar months from ``start`` to ``end`` (0 when ``end`` is not after ``start``).

    >>> months_between(date(2000, 1, 15), date(2001, 1, 14)), months_between(date(2000, 1, 15), date(2001, 1, 15))
    (11, 12)
    """
    months = (end.year - start.year) * 12 + end.month - start.month - (end.day < start.day)
    return max(months, 0)


def _add_months(start: date, months: int) -> date:
    """``start`` plus ``months`` calendar months (the day is clamped to the month's length)."""
    year, month_index = divmod(start.month - 1 + months, 12)
    year += start.year
    month = month_index + 1
    next_month = date(year + month // 12, month % 12 + 1, 1)
    return date(year, month, min(start.day, (next_month - timedelta(days=1)).day))


# ---------------------------------------------------------------------------
# Diabetes diagnoses and glucose-lowering medications
# ---------------------------------------------------------------------------

# SNOMED CT diabetes diagnoses → GE-EHR diabetes type. Synthea's metabolic syndrome module emits
# 44054006 and the cystic fibrosis module 427089005; the others are mapped in case a module
# (or a custom one) emits them.
SNOMED_DIABETES_TYPES: dict[str, str] = {
    "44054006": "type2",  # Diabetes mellitus type 2 (disorder)
    "46635009": "type1",  # Diabetes mellitus type 1 (disorder)
    "11687002": "gestational",  # Gestational diabetes mellitus (disorder)
    "427089005": "other",  # Diabetes mellitus due to cystic fibrosis (disorder)
    "73211009": "unknown",  # Diabetes mellitus (disorder), type not stated
}
# Prediabetes is a risk state, not diabetes: such patients count as "none".
SNOMED_NOT_DIABETES: frozenset[str] = frozenset({"714628002"})  # Prediabetes (finding)

# Fallback for codes missing above: patterns matched at the *start* of the description (so
# "Neuropathy due to type 2 diabetes mellitus" is not read as a diagnosis), specific before generic.
_DIABETES_DESCRIPTIONS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("gestational", re.compile(r"gestational diabetes", re.IGNORECASE)),
    ("neonatal", re.compile(r"(?:permanent |transient )?neonatal diabetes", re.IGNORECASE)),
    ("monogenic", re.compile(r"maturity[- ]onset diabetes of the young|mody\b|monogenic diabetes", re.IGNORECASE)),
    ("type1", re.compile(r"(?:diabetes mellitus type (?:1|i)|type (?:1|i) diabetes)\b", re.IGNORECASE)),
    ("type2", re.compile(r"(?:diabetes mellitus type (?:2|ii)|type (?:2|ii) diabetes)\b", re.IGNORECASE)),
    ("other", re.compile(r"secondary diabetes|diabetes mellitus due to|drug[- ]induced diabetes", re.IGNORECASE)),
    ("unknown", re.compile(r"diabetes mellitus\b", re.IGNORECASE)),
)
# Complications ("Disorder of kidney due to diabetes mellitus", "Microalbuminuria due to type 2
# diabetes mellitus", ...) are deliberately not diagnoses: Synthea's kidney submodule assigns them
# from chronic-kidney-disease state to any patient, infants without diabetes included, and its
# own diabetes model is the HbA1c-based diagnosis (prediabetes at <= 6.5 %, 44054006 above).

# When a patient has several diabetes diagnoses the most specific type wins; a diagnosis with a
# stated cause ("other", e.g. cystic fibrosis-related) beats type 2, which Synthea's metabolic
# module codes for any HbA1c above 6.5 %. Gestational diabetes resolves after pregnancy, so it
# only counts when it is the patient's only diagnosis.
_TYPE_PRECEDENCE: tuple[str, ...] = ("monogenic", "neonatal", "type1", "other", "type2", "unknown", "gestational")

# Glucose-lowering drugs that suggest_drug_class files under "other" (insulin, metformin and the
# sulfonylureas are recognised by suggest_drug_class itself).
_OTHER_GLUCOSE_LOWERING_WORDS: tuple[str, ...] = (
    # GLP-1 receptor agonists (and the dual GIP/GLP-1 agonist tirzepatide)
    "liraglutide", "semaglutide", "dulaglutide", "exenatide", "lixisenatide", "albiglutide", "tirzepatide",
    "victoza", "ozempic", "rybelsus", "trulicity", "byetta", "bydureon", "adlyxin", "mounjaro",
    # DPP-4 inhibitors
    "sitagliptin", "saxagliptin", "linagliptin", "alogliptin", "vildagliptin",
    "januvia", "onglyza", "tradjenta", "nesina", "galvus",
    # SGLT2 inhibitors
    "canagliflozin", "dapagliflozin", "empagliflozin", "ertugliflozin", "sotagliflozin",
    "invokana", "farxiga", "forxiga", "jardiance", "steglatro",
    # thiazolidinediones
    "pioglitazone", "rosiglitazone", "actos", "avandia",
    # alpha-glucosidase inhibitors, meglitinides, amylin analogue
    "acarbose", "miglitol", "precose", "glyset",
    "repaglinide", "nateglinide", "prandin", "starlix",
    "pramlintide", "symlin",
)
_OTHER_GLUCOSE_LOWERING = re.compile(
    r"\b(?:" + "|".join(re.escape(word) for word in _OTHER_GLUCOSE_LOWERING_WORDS) + r")", re.IGNORECASE
)


def classify_diabetes_condition(code: str, description: str) -> str | None:
    """Map a SNOMED CT condition to a GE-EHR diabetes type, or None if it is not a diabetes diagnosis.

    Prediabetes, hyperglycaemia, diabetes complications and unrelated conditions give None.

    >>> classify_diabetes_condition("44054006", "Diabetes mellitus type 2 (disorder)")
    'type2'
    >>> classify_diabetes_condition("714628002", "Prediabetes (finding)") is None
    True
    """
    code = code.strip()
    if code in SNOMED_NOT_DIABETES:
        return None
    if code in SNOMED_DIABETES_TYPES:
        return SNOMED_DIABETES_TYPES[code]
    text = description.strip()
    for diabetes_type, pattern in _DIABETES_DESCRIPTIONS:
        if pattern.match(text):
            return diabetes_type
    return None


def is_glucose_lowering(drug_name: str) -> bool:
    """True for insulins, metformin, sulfonylureas, GLP-1/GIP agonists, DPP-4 and SGLT2 inhibitors,
    thiazolidinediones, alpha-glucosidase inhibitors, meglitinides and pramlintide."""
    return suggest_drug_class(drug_name) != "other" or _OTHER_GLUCOSE_LOWERING.search(drug_name or "") is not None


# ---------------------------------------------------------------------------
# Synthea CSV conversion
# ---------------------------------------------------------------------------

SYNTHEA_PATIENT_COLUMNS: tuple[str, ...] = ("Id", "BIRTHDATE", "DEATHDATE", "FIRST", "LAST", "GENDER")
SYNTHEA_CONDITION_COLUMNS: tuple[str, ...] = ("START", "PATIENT", "CODE", "DESCRIPTION")
SYNTHEA_MEDICATION_COLUMNS: tuple[str, ...] = ("START", "STOP", "PATIENT", "DESCRIPTION")
_SYNTHEA_SEX: dict[str, str] = {"F": "female", "M": "male"}
# Synthea appends numbers to names to make them obviously fake ("Abe604 Kuhn96").
_SYNTHEA_NAME_NUMBER = re.compile(r"(?<=[^\d\s])\d+(?=\s|$)")


def strip_synthea_name_suffix(name: str) -> str:
    """Remove Synthea's numeric name suffixes: ``"Abe604"`` → ``"Abe"``, ``"José Emilio366"`` → ``"José Emilio"``."""
    return _SYNTHEA_NAME_NUMBER.sub("", name).strip()


@dataclass
class _SyntheaPatient:
    given_name: str
    family_name: str
    sex: str
    birth_date: date
    # earliest START per diabetes type
    diagnoses: dict[str, date] = field(default_factory=dict)
    # current medications by drug name → earliest START of an open prescription
    medications: dict[str, date] = field(default_factory=dict)


def load_synthea_csv(directory: str | os.PathLike[str]) -> list[CohortPatient]:
    """Convert the Synthea CSV export in ``directory`` into cohort patients, sorted by Synthea Id.

    Reads ``patients.csv``, ``conditions.csv`` and ``medications.csv`` row by row with the
    :mod:`csv` module, keeping in memory only the patients and their diabetes-relevant rows.

    * Deceased patients (``DEATHDATE`` set) are skipped: the cohort models a current EHR.
    * Names lose Synthea's numeric suffixes; ``GENDER`` ``F``/``M`` → ``female``/``male``.
    * Diabetes type from the SNOMED CT diagnoses (:func:`classify_diabetes_condition`, most
      specific type wins); patients without one (including prediabetes only) → ``"none"``.
      ``age_at_diagnosis_months`` = whole months from ``BIRTHDATE`` to the earliest diabetes
      diagnosis ``START`` (gestational diabetes counts only when it is the only diagnosis).
    * Current medications = rows with an empty ``STOP`` whose description is glucose-lowering
      (:func:`is_glucose_lowering`), classed with :func:`suggest_drug_class`; one entry per drug
      name (whitespace collapsed: Synthea's CSV export replaces commas in descriptions with
      spaces) with the earliest open ``START``. They are kept as recorded, also for patients
      without diabetes: Synthea prescribes insulin to every patient with its "Disorder of kidney
      due to diabetes mellitus" condition, prediabetic ones included.

    Raises ``FileNotFoundError`` for a missing file and ``ValueError`` (file name and line number
    in the message) for a missing column or an unreadable date.
    """
    base = Path(directory)
    patients: dict[str, _SyntheaPatient] = {}
    deceased = 0
    patients_path = base / "patients.csv"
    for line, row in _read_csv(patients_path, SYNTHEA_PATIENT_COLUMNS):
        if row["DEATHDATE"].strip():
            deceased += 1
            continue
        patients[row["Id"]] = _SyntheaPatient(
            given_name=strip_synthea_name_suffix(row["FIRST"]),
            family_name=strip_synthea_name_suffix(row["LAST"]),
            sex=_SYNTHEA_SEX.get(row["GENDER"].strip().upper(), "unknown"),
            birth_date=_parse_date(row["BIRTHDATE"], patients_path, line, "BIRTHDATE"),
        )

    conditions_path = base / "conditions.csv"
    for line, row in _read_csv(conditions_path, SYNTHEA_CONDITION_COLUMNS):
        patient = patients.get(row["PATIENT"])
        if patient is None:
            continue
        diabetes_type = classify_diabetes_condition(row["CODE"], row["DESCRIPTION"])
        if diabetes_type is None:
            continue
        onset = _parse_date(row["START"], conditions_path, line, "START")
        known = patient.diagnoses.get(diabetes_type)
        patient.diagnoses[diabetes_type] = onset if known is None else min(known, onset)

    medications_path = base / "medications.csv"
    for line, row in _read_csv(medications_path, SYNTHEA_MEDICATION_COLUMNS):
        if row["STOP"].strip():
            continue
        patient = patients.get(row["PATIENT"])
        drug_name = " ".join(row["DESCRIPTION"].split())
        if patient is None or not is_glucose_lowering(drug_name):
            continue
        start = _parse_date(row["START"], medications_path, line, "START")
        known = patient.medications.get(drug_name)
        patient.medications[drug_name] = start if known is None else min(known, start)

    logger.info(
        "Loaded %d living Synthea patients from %s (%d deceased skipped)", len(patients), base, deceased
    )
    return [_cohort_patient(synthetic_id, patients[synthetic_id]) for synthetic_id in sorted(patients)]


def _cohort_patient(synthetic_id: str, source: _SyntheaPatient) -> CohortPatient:
    diabetes_type = next((kind for kind in _TYPE_PRECEDENCE if kind in source.diagnoses), "none")
    age_at_diagnosis: int | None = None
    if diabetes_type != "none":
        onsets = [
            onset for kind, onset in source.diagnoses.items() if kind != "gestational" or diabetes_type == "gestational"
        ]
        age_at_diagnosis = months_between(source.birth_date, min(onsets))
    medications = sorted(
        (CohortMedication(name, suggest_drug_class(name), start) for name, start in source.medications.items()),
        key=_medication_order,
    )
    return CohortPatient(
        synthetic_id=synthetic_id,
        given_name=source.given_name,
        family_name=source.family_name,
        sex=source.sex,
        birth_date=source.birth_date,
        diabetes_type=diabetes_type,
        age_at_diagnosis_months=age_at_diagnosis,
        medications=medications,
    )


def _read_csv(path: Path, required: Sequence[str]) -> Iterator[tuple[int, dict[str, str]]]:
    """Yield ``(line_number, row)`` of a CSV file after checking its header has ``required``."""
    with open(path, newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        missing = [column for column in required if column not in (reader.fieldnames or ())]
        if missing:
            raise ValueError(
                f"{path.name}: missing column(s) {', '.join(missing)}; expected a Synthea CSV export "
                f"with the columns {', '.join(required)}."
            )
        for row in reader:
            yield reader.line_num, row


def _parse_date(value: str, path: Path, line: int, column: str) -> date:
    """Parse ``YYYY-MM-DD`` (a trailing time such as ``T10:39:29Z`` is ignored)."""
    try:
        return date.fromisoformat(value.strip()[:10])
    except ValueError:
        raise ValueError(f"{path.name} line {line}: {column} {value!r} is not a date (YYYY-MM-DD).") from None


def _medication_order(medication: CohortMedication) -> tuple[date, str]:
    return (medication.start_date or date.min, medication.drug_name)


# ---------------------------------------------------------------------------
# Compact cohort files (data/cohorts/*.csv.gz)
# ---------------------------------------------------------------------------

COMPACT_COLUMNS: tuple[str, ...] = (
    "synthetic_id",
    "given_name",
    "family_name",
    "sex",
    "birth_date",
    "diabetes_type",
    "age_at_diagnosis_months",
    "family_id",
    "medications",
)


def write_compact_cohort(patients: Iterable[CohortPatient], path: str | os.PathLike[str]) -> int:
    """Write ``patients`` as a compact cohort CSV (gzip-compressed when ``path`` ends with ``.gz``).

    Rows are sorted by ``synthetic_id`` and each patient's medications by start date and name;
    the ``medications`` column holds a JSON list of ``{"drug_name", "drug_class", "start_date"}``.
    The gzip header carries no timestamp or file name, so equal cohorts give identical bytes.
    Returns the number of patients written; raises ``ValueError`` on a duplicate ``synthetic_id``.
    """
    rows = sorted(patients, key=lambda patient: patient.synthetic_id)
    for previous, current in zip(rows, rows[1:]):
        if previous.synthetic_id == current.synthetic_id:
            raise ValueError(f"Duplicate synthetic_id {current.synthetic_id!r} in the cohort.")
    with _open_text(Path(path), "w") as handle:
        writer = csv.writer(handle, lineterminator="\n")
        writer.writerow(COMPACT_COLUMNS)
        for patient in rows:
            medications = [
                {
                    "drug_name": medication.drug_name,
                    "drug_class": medication.drug_class,
                    "start_date": medication.start_date.isoformat() if medication.start_date else None,
                }
                for medication in sorted(patient.medications, key=_medication_order)
            ]
            writer.writerow(
                (
                    patient.synthetic_id,
                    patient.given_name,
                    patient.family_name,
                    patient.sex,
                    patient.birth_date.isoformat(),
                    patient.diabetes_type,
                    "" if patient.age_at_diagnosis_months is None else patient.age_at_diagnosis_months,
                    patient.family_id or "",
                    json.dumps(medications, ensure_ascii=False, separators=(",", ":")),
                )
            )
    return len(rows)


def load_compact_cohort(path: str | os.PathLike[str]) -> list[CohortPatient]:
    """Read a cohort written by :func:`write_compact_cohort` (``.csv`` or ``.csv.gz``), in file order.

    Raises ``ValueError`` naming the file and line for a wrong header or an invalid value.
    """
    source = Path(path)
    patients: list[CohortPatient] = []
    with _open_text(source, "r") as handle:
        reader = csv.reader(handle)
        header = tuple(next(reader, ()))
        if header != COMPACT_COLUMNS:
            raise ValueError(
                f"{source.name}: not a compact cohort file; the header must be {','.join(COMPACT_COLUMNS)}."
            )
        for row in reader:
            try:
                patients.append(_compact_row(row))
            except (ValueError, TypeError, KeyError) as exc:
                raise ValueError(f"{source.name} line {reader.line_num}: {exc}") from None
    return patients


def _compact_row(row: list[str]) -> CohortPatient:
    if len(row) != len(COMPACT_COLUMNS):
        raise ValueError(f"expected {len(COMPACT_COLUMNS)} columns, found {len(row)}.")
    values = dict(zip(COMPACT_COLUMNS, row))
    try:
        entries = json.loads(values["medications"])
    except json.JSONDecodeError:
        raise ValueError("the medications column is not valid JSON.") from None
    if not isinstance(entries, list):
        raise ValueError("the medications column must be a JSON list.")
    medications = [
        CohortMedication(
            str(entry["drug_name"]),
            str(entry["drug_class"]),
            date.fromisoformat(entry["start_date"]) if entry["start_date"] else None,
        )
        for entry in entries
    ]
    age = values["age_at_diagnosis_months"]
    return CohortPatient(
        synthetic_id=values["synthetic_id"],
        given_name=values["given_name"],
        family_name=values["family_name"],
        sex=values["sex"],
        birth_date=date.fromisoformat(values["birth_date"]),
        diabetes_type=values["diabetes_type"],
        age_at_diagnosis_months=int(age) if age else None,
        medications=medications,
        family_id=values["family_id"] or None,
    )


@contextmanager
def _open_text(path: Path, mode: Literal["r", "w"]) -> Iterator[TextIO]:
    """Open a UTF-8 text file, through a timestamp-free gzip stream when the name ends with ``.gz``."""
    if path.suffix != ".gz":
        with open(path, mode, encoding="utf-8", newline="") as handle:
            yield handle
        return
    with (
        open(path, mode + "b") as raw,
        gzip.GzipFile(filename="", mode=mode + "b", fileobj=raw, mtime=0) as compressed,
        io.TextIOWrapper(compressed, encoding="utf-8", newline="") as handle,
    ):
        yield handle


# ---------------------------------------------------------------------------
# Deterministic pure-Python generator (fallback when Synthea output is unavailable)
# ---------------------------------------------------------------------------

# Age bands (inclusive years, population share) and the diagnosed-diabetes prevalence per band,
# approximating US figures (CDC National Diabetes Statistics Report): ~9-10 % overall.
_AGE_BANDS: tuple[tuple[int, int, float, float], ...] = (
    (0, 17, 0.22, 0.004),
    (18, 44, 0.36, 0.04),
    (45, 64, 0.25, 0.15),
    (65, 90, 0.17, 0.25),
)
# Diabetes type mix among diagnosed patients: children mostly type 1, adults mostly type 2.
_CHILD_TYPE_MIX: tuple[tuple[str, float], ...] = (("type1", 0.85), ("type2", 0.15))
_ADULT_TYPE_MIX: tuple[tuple[str, float], ...] = (("type1", 0.06), ("type2", 0.91), ("other", 0.03))

_BASAL_INSULINS = (
    "Insulin Glargine 100 UNT/ML Pen Injector [Lantus]",
    "Insulin Degludec 100 UNT/ML Pen Injector [Tresiba]",
    "Insulin Detemir 100 UNT/ML Pen Injector [Levemir]",
)
_PRANDIAL_INSULINS = (
    "Insulin Lispro 100 UNT/ML Injectable Solution [Humalog]",
    "Insulin Aspart 100 UNT/ML Injectable Solution [Novolog]",
)
_PREMIXED_INSULINS = (
    "insulin isophane, human 70 UNT/ML / insulin, regular, human 30 UNT/ML Injectable Suspension [Humulin]",
)
_METFORMIN = (
    "24 HR Metformin hydrochloride 500 MG Extended Release Oral Tablet",
    "Metformin hydrochloride 1000 MG Oral Tablet",
)
_SULFONYLUREAS = (
    "Glipizide 5 MG Oral Tablet",
    "Gliclazide 80 MG Oral Tablet",
    "Glimepiride 2 MG Oral Tablet",
    "Glyburide 5 MG Oral Tablet",
)
_OTHER_AGENTS = (
    "3 ML liraglutide 6 MG/ML Pen Injector",
    "canagliflozin 100 MG Oral Tablet",
    "empagliflozin 10 MG Oral Tablet",
    "sitagliptin 100 MG Oral Tablet",
    "pioglitazone 30 MG Oral Tablet",
)
_DrugPools = tuple[tuple[str, ...], ...]
# Therapy regimens per diabetes type: (weight, drugs started at diagnosis, drugs added later);
# one drug is drawn from each pool.
_REGIMENS: dict[str, tuple[tuple[float, _DrugPools, _DrugPools], ...]] = {
    "type1": (
        (0.88, (_BASAL_INSULINS, _PRANDIAL_INSULINS), ()),
        (0.05, (_BASAL_INSULINS, _PRANDIAL_INSULINS), (_METFORMIN,)),
        (0.07, (_PREMIXED_INSULINS,), ()),
    ),
    "type2": (
        (0.15, (), ()),
        (0.40, (_METFORMIN,), ()),
        (0.12, (_METFORMIN,), (_SULFONYLUREAS,)),
        (0.15, (_METFORMIN,), (_OTHER_AGENTS,)),
        (0.13, (_METFORMIN,), (_BASAL_INSULINS,)),
        (0.05, (_SULFONYLUREAS,), ()),
    ),
    "other": (
        (0.40, (_BASAL_INSULINS,), ()),
        (0.30, (_METFORMIN,), ()),
        (0.30, (), ()),
    ),
}

_FEMALE_NAMES = (
    "Amara", "Ana", "Beatriz", "Chloe", "Dalia", "Elena", "Fatima", "Grace", "Hana", "Ines", "Ingrid",
    "Jasmine", "Keiko", "Leila", "Lucia", "Maya", "Mei", "Nadia", "Nia", "Olga", "Priya", "Rosa",
    "Sakura", "Sofia", "Tamar", "Valentina", "Wanjiru", "Yara", "Zainab", "Zoe",
)
_MALE_NAMES = (
    "Adebayo", "Ahmed", "Andrei", "Arjun", "Benjamin", "Carlos", "Daniel", "Diego", "Emeka", "Ethan",
    "Farid", "Hiroshi", "Ibrahim", "Jonas", "Kenji", "Kwame", "Liam", "Luca", "Mateo", "Mikhail",
    "Noah", "Omar", "Pedro", "Rahul", "Samuel", "Tariq", "Thanh", "Tomas", "Wei", "Yusuf",
)
_FAMILY_NAMES = (
    "Abara", "Achebe", "Alvarez", "Andersson", "Bakshi", "Bergstrom", "Castellano", "Chandra", "Costa",
    "Dimitrov", "Duarte", "Eriksen", "Farouk", "Fontaine", "Gallagher", "Haddad", "Hakimi", "Ibarra",
    "Iwasaki", "Jovanovic", "Kamau", "Kaur", "Kowalczyk", "Lindgren", "Lombardi", "Mahlangu", "Moreau",
    "Nakamura", "Novak", "Nwosu", "Okafor", "Olsen", "Ortega", "Petrov", "Quispe", "Rahman", "Rossi",
    "Sandoval", "Schreiber", "Shapiro", "Silva", "Takahashi", "Tran", "Umarov", "Varga", "Vasquez",
    "Weber", "Xu", "Yilmaz", "Zielinski",
)


def generate_cohort(n: int, seed: int, *, reference_date: date = REFERENCE_DATE) -> list[CohortPatient]:
    """Generate ``n`` synthetic patients deterministically from ``seed`` (no Synthea needed).

    Distributions: ages 0–90 in four bands (22 % children); 51 % female; diagnosed diabetes in
    ~0.4 % of children and 4 % / 15 % / 25 % of adults aged 18–44 / 45–64 / 65+ (~9.5 % overall);
    children are mostly type 1, adults 91 % type 2, 6 % type 1, 3 % other; type 1 is diagnosed
    around age 11 and type 2 around 50; type 1 patients are on insulin, type 2 patients on
    lifestyle only (15 %), metformin (40 %), metformin plus a sulfonylurea, a newer agent
    (GLP-1/SGLT2/DPP-4/TZD) or basal insulin, or a sulfonylurea alone. Medication start dates lie
    between diagnosis and ``reference_date``. Ids are ``GEN-000001`` … in order.
    """
    if n < 0:
        raise ValueError(f"The cohort size must be >= 0, got {n}.")
    rng = random.Random(seed)
    return [_generate_patient(rng, index, reference_date) for index in range(1, n + 1)]


def _generate_patient(rng: random.Random, index: int, reference_date: date) -> CohortPatient:
    low, high, _share, prevalence = _weighted_choice(rng, [(band, band[2]) for band in _AGE_BANDS])
    age_years = rng.randint(low, high)
    birth_date = reference_date - timedelta(days=int((age_years + rng.random()) * 365.2425))
    sex = "female" if rng.random() < 0.51 else "male"
    given_name = rng.choice(_FEMALE_NAMES if sex == "female" else _MALE_NAMES)
    family_name = rng.choice(_FAMILY_NAMES)
    age_months = months_between(birth_date, reference_date)

    diabetes_type, age_at_diagnosis, medications = "none", None, []
    if rng.random() < prevalence:
        diabetes_type = _weighted_choice(rng, _CHILD_TYPE_MIX if age_years < 18 else _ADULT_TYPE_MIX)
        age_at_diagnosis = _diagnosis_age_months(rng, diabetes_type, age_months)
        diagnosed_on = _add_months(birth_date, age_at_diagnosis)
        medications = _generate_medications(rng, diabetes_type, diagnosed_on, reference_date)
    return CohortPatient(
        synthetic_id=f"GEN-{index:06d}",
        given_name=given_name,
        family_name=family_name,
        sex=sex,
        birth_date=birth_date,
        diabetes_type=diabetes_type,
        age_at_diagnosis_months=age_at_diagnosis,
        medications=medications,
    )


def _diagnosis_age_months(rng: random.Random, diabetes_type: str, age_months: int) -> int:
    """Age at diagnosis in months, never after the patient's current age."""
    if diabetes_type == "type1":
        years = rng.gauss(11.0, 6.0)
    elif diabetes_type == "type2":
        years = rng.gauss(50.0, 12.0) if age_months >= 18 * 12 else rng.gauss(14.0, 2.0)
    else:
        years = rng.uniform(15.0, max(15.0, age_months / 12))
    return min(max(int(years * 12), 6), age_months)


def _generate_medications(
    rng: random.Random, diabetes_type: str, diagnosed_on: date, reference_date: date
) -> list[CohortMedication]:
    initial, add_ons = _weighted_choice(
        rng, [((initial, add_ons), weight) for weight, initial, add_ons in _REGIMENS[diabetes_type]]
    )
    first_start = min(diagnosed_on + timedelta(days=rng.randint(0, 60)), reference_date)
    starts = [first_start] * len(initial) + [
        first_start + timedelta(days=rng.randint(0, (reference_date - first_start).days)) for _ in add_ons
    ]
    medications = []
    for pool, start in zip((*initial, *add_ons), starts):
        name = rng.choice(pool)
        medications.append(CohortMedication(name, suggest_drug_class(name), start))
    return sorted(medications, key=_medication_order)


def _weighted_choice[T](rng: random.Random, options: Sequence[tuple[T, float]]) -> T:
    """Pick one option by weight using a single ``rng.random()`` draw (stable across Python versions)."""
    total = sum(weight for _option, weight in options)
    point = rng.random() * total
    for option, weight in options:
        point -= weight
        if point < 0:
            return option
    return options[-1][0]


# ---------------------------------------------------------------------------
# Families
# ---------------------------------------------------------------------------

MIN_FAMILY_SIZE = 2
MAX_FAMILY_SIZE = 5


def make_families(cohort: Sequence[CohortPatient], n_families: int, seed: int) -> list[CohortPatient]:
    """Return a copy of ``cohort`` in which ``n_families`` groups of 2–5 relatives share a family.

    Members are drawn deterministically (``random.Random(seed)``) from patients without a
    ``family_id``; every member gets the family id (``FAM-0001``, … skipping ids already used)
    and the surname of the family's oldest member. Patients outside the new families are
    returned unchanged; the input is not modified. Raises ``ValueError`` when there are fewer
    than ``2 * n_families`` patients without a family.
    """
    if n_families < 0:
        raise ValueError(f"The number of families must be >= 0, got {n_families}.")
    available = [index for index, patient in enumerate(cohort) if patient.family_id is None]
    if MIN_FAMILY_SIZE * n_families > len(available):
        raise ValueError(
            f"Cannot form {n_families} families of {MIN_FAMILY_SIZE}-{MAX_FAMILY_SIZE} relatives: only "
            f"{len(available)} patient(s) have no family yet (at least {MIN_FAMILY_SIZE * n_families} needed)."
        )
    rng = random.Random(seed)
    sizes = [rng.randint(MIN_FAMILY_SIZE, MAX_FAMILY_SIZE) for _ in range(n_families)]
    while sum(sizes) > len(available):  # shrink the largest families until everyone fits
        largest = max(range(n_families), key=lambda family: sizes[family])
        sizes[largest] -= 1
    rng.shuffle(available)

    used_ids = {patient.family_id for patient in cohort if patient.family_id}
    result = list(cohort)
    offset = 0
    for size, family_id in zip(sizes, _new_family_ids(used_ids)):
        members = available[offset : offset + size]
        offset += size
        surname = min((cohort[index] for index in members), key=lambda patient: patient.birth_date).family_name
        for index in members:
            result[index] = replace(cohort[index], family_id=family_id, family_name=surname)
    return result


def _new_family_ids(used: set[str | None]) -> Iterator[str]:
    """Yield ``FAM-0001``, ``FAM-0002``, … skipping ids in ``used``."""
    number = 0
    while True:
        number += 1
        family_id = f"FAM-{number:04d}"
        if family_id not in used:
            yield family_id


# ---------------------------------------------------------------------------
# Patient VCFs
# ---------------------------------------------------------------------------

# GRCh38 primary-assembly chromosome lengths (Genome Reference Consortium, GCA_000001405.15).
GRCH38_CHROMOSOME_LENGTHS: dict[str, int] = {
    "1": 248_956_422, "2": 242_193_529, "3": 198_295_559, "4": 190_214_555, "5": 181_538_259,
    "6": 170_805_979, "7": 159_345_973, "8": 145_138_636, "9": 138_394_717, "10": 133_797_422,
    "11": 135_086_622, "12": 133_275_309, "13": 114_364_328, "14": 107_043_718, "15": 101_991_189,
    "16": 90_338_345, "17": 83_257_441, "18": 80_373_285, "19": 58_617_616, "20": 64_444_167,
    "21": 46_709_983, "22": 50_818_468, "X": 156_040_895, "Y": 57_227_415, "MT": 16_569,
}
_CHROMOSOME_ORDER: dict[str, int] = {name: order for order, name in enumerate(GRCH38_CHROMOSOME_LENGTHS)}
PATIENT_VCF_SOURCE = "GE-EHR synthetic patient VCF (synthetic data only; not derived from a real person)"
_GENOTYPES: dict[str, str] = {"heterozygous": "0/1", "homozygous": "1/1", "hemizygous": "1"}
_ALLELE = re.compile(r"[ACGTN]+")
_SAMPLE_ID = re.compile(r"[^\s]+")

VcfVariant = tuple[str, int, str, str, str]
"""``(chrom, pos, ref, alt, zygosity)`` with a 1-based GRCh38 position."""


def format_patient_vcf(sample_id: str, variants: Iterable[VcfVariant]) -> str:
    """Return a single-sample GRCh38 VCF 4.2 text for ``variants``.

    Header: ``##reference=GRCh38``, a ``##contig`` line with the GRCh38 length of every primary
    chromosome, the synthetic marker ``##ge_ehr_synthetic=true``, FORMAT ``GT:DP:GQ``. Records are
    sorted by chromosome and position, FILTER ``PASS``; GT is ``0/1`` (heterozygous), ``1/1``
    (homozygous) or ``1`` (hemizygous); QUAL, DP and GQ are plausible values derived
    deterministically from the sample and variant, so the output is reproducible. Chromosome
    names are normalised (``chr7`` → ``7``) and alleles upper-cased.

    Raises ``ValueError`` for an invalid sample id, chromosome, position, allele or zygosity, or
    a variant listed twice.
    """
    if not _SAMPLE_ID.fullmatch(sample_id or ""):
        raise ValueError(f"Invalid VCF sample id {sample_id!r}: it must be non-empty and contain no whitespace.")
    records: dict[tuple[str, int, str, str], str] = {}
    for chrom, pos, ref, alt, zygosity in variants:
        key = _vcf_key(chrom, pos, ref, alt)
        if key in records:
            raise ValueError(f"Variant {'-'.join(map(str, key))} is listed more than once.")
        if zygosity not in _GENOTYPES:
            raise ValueError(
                f"Unknown zygosity {zygosity!r} for {'-'.join(map(str, key))}; expected one of {', '.join(_GENOTYPES)}."
            )
        records[key] = _GENOTYPES[zygosity]

    lines = [
        "##fileformat=VCFv4.2",
        f"##source={PATIENT_VCF_SOURCE}",
        "##ge_ehr_synthetic=true",
        "##reference=GRCh38",
        *(f"##contig=<ID={name},length={length},assembly=GRCh38>" for name, length in GRCH38_CHROMOSOME_LENGTHS.items()),
        '##FILTER=<ID=PASS,Description="All filters passed">',
        '##FORMAT=<ID=GT,Number=1,Type=String,Description="Genotype">',
        '##FORMAT=<ID=DP,Number=1,Type=Integer,Description="Read depth">',
        '##FORMAT=<ID=GQ,Number=1,Type=Integer,Description="Conditional genotype quality">',
        "\t".join(("#CHROM", "POS", "ID", "REF", "ALT", "QUAL", "FILTER", "INFO", "FORMAT", sample_id)),
    ]
    for key in sorted(records, key=lambda item: (_CHROMOSOME_ORDER[item[0]], item[1], item[2], item[3])):
        chrom, pos, ref, alt = key
        digest = zlib.crc32(f"{sample_id}|{chrom}|{pos}|{ref}|{alt}".encode())
        depth, quality = 25 + digest % 56, 60 + (digest >> 8) % 40
        qual = 100 + (digest >> 16) % 900
        sample = f"{records[key]}:{depth}:{quality}"
        lines.append("\t".join((chrom, str(pos), ".", ref, alt, str(qual), "PASS", ".", "GT:DP:GQ", sample)))
    return "\n".join(lines) + "\n"


def write_patient_vcf(path: str | os.PathLike[str], sample_id: str, variants: Iterable[VcfVariant]) -> None:
    """Write :func:`format_patient_vcf` output to ``path`` (plain text, LF line endings)."""
    Path(path).write_text(format_patient_vcf(sample_id, variants), encoding="utf-8", newline="\n")


def _vcf_key(chrom: str, pos: int, ref: str, alt: str) -> tuple[str, int, str, str]:
    name = normalize_chrom(chrom)
    if name not in GRCH38_CHROMOSOME_LENGTHS:
        raise ValueError(f"Unknown GRCh38 chromosome {chrom!r}; expected 1-22, X, Y or MT.")
    if isinstance(pos, bool) or not isinstance(pos, int) or not 1 <= pos <= GRCH38_CHROMOSOME_LENGTHS[name]:
        raise ValueError(
            f"Position {pos!r} on chromosome {name} is invalid: it must be a whole number from 1 to "
            f"{GRCH38_CHROMOSOME_LENGTHS[name]:,}."
        )
    ref_allele, alt_allele = str(ref).upper(), str(alt).upper()
    for label, allele in (("REF", ref_allele), ("ALT", alt_allele)):
        if not _ALLELE.fullmatch(allele):
            raise ValueError(f"{label} allele {allele!r} at {name}:{pos} must consist only of A, C, G, T and N.")
    if ref_allele == alt_allele:
        raise ValueError(f"REF and ALT are identical ({ref_allele}) at {name}:{pos}.")
    return name, pos, ref_allele, alt_allele
