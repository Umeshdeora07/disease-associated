"""Tests for evaluation.cohort: Synthea conversion, compact files, generator, families, VCF writer."""

from __future__ import annotations

import copy
import csv
import gzip
import shutil
import statistics
from collections import Counter
from datetime import date
from pathlib import Path

import pytest

from evaluation.cohort import (
    COMPACT_COLUMNS,
    GRCH38_CHROMOSOME_LENGTHS,
    REFERENCE_DATE,
    CohortMedication,
    CohortPatient,
    classify_diabetes_condition,
    format_patient_vcf,
    generate_cohort,
    is_glucose_lowering,
    load_compact_cohort,
    load_synthea_csv,
    make_families,
    months_between,
    strip_synthea_name_suffix,
    write_compact_cohort,
    write_patient_vcf,
)
from ge_ehr.constants import DIABETES_TYPES, DRUG_CLASSES, GRCH38_CONTIG_LENGTHS, suggest_drug_class
from ge_ehr.services.vcf_parser import parse_patient_vcf

SYNTHEA_MINI = Path(__file__).parent / "data" / "synthea_mini"

# Fixture patient ids (tests/data/synthea_mini/patients.csv)
TYPE2_ON_THREE_DRUGS = "a1b2c3d4-0000-4000-8000-00000000000a"
PREDIABETES_ONLY = "b1b2c3d4-0000-4000-8000-00000000000b"
DECEASED = "c1b2c3d4-0000-4000-8000-00000000000c"
TYPE1_CHILD = "d1b2c3d4-0000-4000-8000-00000000000d"
CF_RELATED = "e1b2c3d4-0000-4000-8000-00000000000e"
GESTATIONAL_THEN_TYPE2 = "f1b2c3d4-0000-4000-8000-00000000000f"
GESTATIONAL_ONLY = "01b2c3d4-0000-4000-8000-000000000010"
UNTYPED_COMPLICATION_ONLY = "11b2c3d4-0000-4000-8000-000000000011"
PREDIABETES_WITH_TYPE2_COMPLICATION = "21b2c3d4-0000-4000-8000-000000000012"

METFORMIN = "24 HR Metformin hydrochloride 500 MG Extended Release Oral Tablet"
HUMULIN = "insulin isophane, human 70 UNT/ML / insulin, regular, human 30 UNT/ML Injectable Suspension [Humulin]"
# Synthea's CSV export replaces the commas with spaces; the loader collapses the whitespace.
HUMULIN_FROM_SYNTHEA_CSV = "insulin isophane human 70 UNT/ML / insulin regular human 30 UNT/ML Injectable Suspension [Humulin]"
HUMALOG = "Insulin Lispro 100 UNT/ML Injectable Solution [Humalog]"
LIRAGLUTIDE = "3 ML liraglutide 6 MG/ML Pen Injector"
CANAGLIFLOZIN = "canagliflozin 100 MG Oral Tablet"


def make_patient(synthetic_id: str = "P-1", **overrides) -> CohortPatient:
    values = {
        "synthetic_id": synthetic_id,
        "given_name": "Ana",
        "family_name": "Silva",
        "sex": "female",
        "birth_date": date(1980, 5, 17),
        "diabetes_type": "type2",
        "age_at_diagnosis_months": 480,
        "medications": [CohortMedication(METFORMIN, "metformin", date(2020, 6, 1))],
        "family_id": None,
    }
    values.update(overrides)
    return CohortPatient(**values)


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("start", "end", "months"),
    [
        (date(2000, 1, 15), date(2000, 1, 15), 0),
        (date(2000, 1, 15), date(2000, 2, 14), 0),
        (date(2000, 1, 15), date(2000, 2, 15), 1),
        (date(2000, 1, 31), date(2000, 2, 29), 0),
        (date(1960, 7, 20), date(2010, 3, 15), 595),
        (date(2000, 1, 15), date(2001, 1, 15), 12),
        (date(2010, 1, 1), date(2009, 1, 1), 0),  # end before start
    ],
)
def test_months_between(start: date, end: date, months: int) -> None:
    assert months_between(start, end) == months


@pytest.mark.parametrize(
    ("raw", "clean"),
    [
        ("Abe604", "Abe"),
        ("Kuhn96", "Kuhn"),
        ("José Emilio366", "José Emilio"),
        ("Marianela210 Katelynn452", "Marianela Katelynn"),
        ("O'Keefe57", "O'Keefe"),
        ("Smith-Jones12", "Smith-Jones"),
        ("Ana", "Ana"),
        ("  Lou12 ", "Lou"),
        ("123", "123"),  # a name made only of digits is left alone
    ],
)
def test_strip_synthea_name_suffix(raw: str, clean: str) -> None:
    assert strip_synthea_name_suffix(raw) == clean


@pytest.mark.parametrize(
    ("code", "description", "expected"),
    [
        ("44054006", "Diabetes mellitus type 2 (disorder)", "type2"),
        ("46635009", "Diabetes mellitus type 1 (disorder)", "type1"),
        ("427089005", "Diabetes mellitus due to cystic fibrosis (disorder)", "other"),
        ("11687002", "Gestational diabetes mellitus (disorder)", "gestational"),
        ("73211009", "Diabetes mellitus (disorder)", "unknown"),
        ("714628002", "Prediabetes (finding)", None),
        # description fallback for codes outside the table
        ("1", "Type 2 diabetes mellitus without complication (disorder)", "type2"),
        ("1", "Type 1 diabetes mellitus (disorder)", "type1"),
        ("1", "Diabetes mellitus type II", "type2"),
        ("1", "Permanent neonatal diabetes mellitus", "neonatal"),
        ("1", "Maturity-onset diabetes of the young, type 3", "monogenic"),
        ("1", "Secondary diabetes mellitus (disorder)", "other"),
        ("1", "Diabetes mellitus without complication (disorder)", "unknown"),
        # complications, prediabetes, hyperglycaemia, screening, diet and unrelated findings are not
        ("368581000119106", "Neuropathy due to type 2 diabetes mellitus (disorder)", None),
        ("90781000119102", "Microalbuminuria due to type 2 diabetes mellitus (disorder)", None),
        ("1551000119108", "Nonproliferative diabetic retinopathy due to type II diabetes mellitus", None),
        ("127013003", "Disorder of kidney due to diabetes mellitus (disorder)", None),
        ("1", "Prediabetes (finding)", None),
        ("80394007", "Hyperglycemia (disorder)", None),
        ("160670007", "Diabetic diet (finding)", None),
        ("395123002", "Urine screening test for diabetes (procedure)", None),
        ("444814009", "Viral sinusitis (disorder)", None),
    ],
)
def test_classify_diabetes_condition(code: str, description: str, expected: str | None) -> None:
    assert classify_diabetes_condition(code, description) == expected


@pytest.mark.parametrize(
    ("name", "drug_class"),
    [
        # every glucose-lowering drug Synthea's metabolic syndrome modules prescribe
        (METFORMIN, "metformin"),
        (HUMULIN, "insulin"),
        (HUMULIN_FROM_SYNTHEA_CSV, "insulin"),
        (HUMALOG, "insulin"),
        (LIRAGLUTIDE, "other"),
        (CANAGLIFLOZIN, "other"),
        # other classes recognised by keyword
        ("Glipizide 5 MG Oral Tablet", "sulfonylurea"),
        ("Gliclazide 80 MG Oral Tablet", "sulfonylurea"),
        ("glyburide 5 MG Oral Tablet", "sulfonylurea"),
        ("Semaglutide 0.5 MG/DOSE Pen Injector [Ozempic]", "other"),
        ("sitagliptin 100 MG Oral Tablet [Januvia]", "other"),
        ("empagliflozin 10 MG Oral Tablet", "other"),
        ("pioglitazone 30 MG Oral Tablet", "other"),
        ("acarbose 50 MG Oral Tablet", "other"),
        ("repaglinide 1 MG Oral Tablet", "other"),
        ("tirzepatide 5 MG/0.5ML Auto-Injector", "other"),
    ],
)
def test_glucose_lowering_drugs_and_their_class(name: str, drug_class: str) -> None:
    assert is_glucose_lowering(name)
    assert suggest_drug_class(name) == drug_class


@pytest.mark.parametrize(
    "name",
    [
        "lisinopril 10 MG Oral Tablet",
        "Acetaminophen 325 MG Oral Tablet",
        "Simvastatin 10 MG Oral Tablet",
        "Amoxicillin 250 MG / Clavulanate 125 MG Oral Tablet",
        "24 HR metoprolol succinate 100 MG Extended Release Oral Tablet",
        "",
    ],
)
def test_other_drugs_are_not_glucose_lowering(name: str) -> None:
    assert not is_glucose_lowering(name)


def test_cohort_patient_validates_its_values() -> None:
    with pytest.raises(ValueError, match="Unknown sex 'f'"):
        make_patient(sex="f")
    with pytest.raises(ValueError, match="Unknown diabetes type 'type3'"):
        make_patient(diabetes_type="type3")
    with pytest.raises(ValueError, match="age_at_diagnosis_months must be >= 0"):
        make_patient(age_at_diagnosis_months=-1)
    with pytest.raises(ValueError, match="Unknown drug class 'biguanide'"):
        make_patient(medications=[CohortMedication(METFORMIN, "biguanide", None)])
    with pytest.raises(ValueError, match="synthetic_id must not be empty"):
        make_patient(synthetic_id="")


# ---------------------------------------------------------------------------
# Synthea CSV conversion
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def synthea_patients() -> dict[str, CohortPatient]:
    patients = load_synthea_csv(SYNTHEA_MINI)
    return {patient.synthetic_id: patient for patient in patients}


def test_synthea_patients_are_living_and_sorted_by_id() -> None:
    patients = load_synthea_csv(SYNTHEA_MINI)
    ids = [patient.synthetic_id for patient in patients]
    assert ids == sorted(ids)
    assert DECEASED not in ids
    assert len(ids) == 8


def test_synthea_type2_patient(synthea_patients: dict[str, CohortPatient]) -> None:
    patient = synthea_patients[TYPE2_ON_THREE_DRUGS]
    assert (patient.given_name, patient.family_name, patient.sex) == ("Abe", "Kuhn", "male")
    assert patient.birth_date == date(1960, 7, 20)
    assert patient.diabetes_type == "type2"
    # earliest diabetes diagnosis 2010-03-15 (prediabetes in 2005 does not count)
    assert patient.age_at_diagnosis_months == 595
    # open prescriptions only; lisinopril is not glucose-lowering; stopped metformin/canagliflozin dropped
    assert patient.medications == [
        CohortMedication(METFORMIN, "metformin", date(2013, 1, 10)),
        CohortMedication(HUMULIN_FROM_SYNTHEA_CSV, "insulin", date(2016, 5, 5)),
        CohortMedication(LIRAGLUTIDE, "other", date(2018, 8, 8)),
    ]
    assert patient.family_id is None


def test_synthea_prediabetes_is_not_diabetes(synthea_patients: dict[str, CohortPatient]) -> None:
    prediabetic = synthea_patients[PREDIABETES_ONLY]  # also had transient hyperglycaemia
    assert (prediabetic.given_name, prediabetic.family_name, prediabetic.sex) == ("Ana María", "O'Keefe", "female")
    assert prediabetic.diabetes_type == "none"
    assert prediabetic.age_at_diagnosis_months is None
    assert prediabetic.medications == []


def test_synthea_complications_are_not_diagnoses(synthea_patients: dict[str, CohortPatient]) -> None:
    # Synthea assigns "... due to diabetes mellitus" kidney conditions from CKD state, and gives
    # insulin to every patient with one, even when the only diagnosis is prediabetes.
    patient = synthea_patients[PREDIABETES_WITH_TYPE2_COMPLICATION]
    assert (patient.given_name, patient.family_name) == ("Rocco", "Bins")
    assert patient.diabetes_type == "none"
    assert patient.age_at_diagnosis_months is None
    # the current prescription is kept as recorded
    assert patient.medications == [CohortMedication(HUMULIN_FROM_SYNTHEA_CSV, "insulin", date(2023, 2, 28))]
    untyped = synthea_patients[UNTYPED_COMPLICATION_ONLY]
    assert untyped.diabetes_type == "none"
    assert untyped.age_at_diagnosis_months is None


def test_synthea_type1_child_deduplicates_open_prescriptions(synthea_patients: dict[str, CohortPatient]) -> None:
    patient = synthea_patients[TYPE1_CHILD]
    assert (patient.given_name, patient.family_name) == ("Lashaunda", "Leffler")
    assert patient.diabetes_type == "type1"
    assert patient.age_at_diagnosis_months == 44  # 2008-05-10 → 2012-02-01
    assert patient.medications == [CohortMedication(HUMALOG, "insulin", date(2012, 2, 3))]


def test_synthea_cf_related_diabetes_on_sulfonylurea(synthea_patients: dict[str, CohortPatient]) -> None:
    patient = synthea_patients[CF_RELATED]
    assert patient.diabetes_type == "other"  # the cause-specific diagnosis beats the later type 2 code
    assert patient.age_at_diagnosis_months == 199  # 1999-11-30 → 2016-07-19
    assert patient.medications == [CohortMedication("Glipizide 5 MG Oral Tablet", "sulfonylurea", date(2017, 1, 1))]


def test_synthea_gestational_diabetes_counts_only_when_alone(synthea_patients: dict[str, CohortPatient]) -> None:
    later_type2 = synthea_patients[GESTATIONAL_THEN_TYPE2]
    assert later_type2.diabetes_type == "type2"  # from the description, the code is not in the table
    assert later_type2.age_at_diagnosis_months == 463  # 1982-03-15 → 2020-10-15, not the 2012 pregnancy
    gestational = synthea_patients[GESTATIONAL_ONLY]
    assert gestational.diabetes_type == "gestational"
    assert gestational.age_at_diagnosis_months == 365  # 1990-08-08 → 2021-01-20


def test_synthea_missing_file(tmp_path: Path) -> None:
    shutil.copy(SYNTHEA_MINI / "patients.csv", tmp_path)
    with pytest.raises(FileNotFoundError, match="conditions.csv"):
        load_synthea_csv(tmp_path)


def test_synthea_missing_column(tmp_path: Path) -> None:
    for name in ("patients.csv", "conditions.csv", "medications.csv"):
        shutil.copy(SYNTHEA_MINI / name, tmp_path)
    rows = list(csv.reader((tmp_path / "medications.csv").open(encoding="utf-8", newline="")))
    stop = rows[0].index("STOP")
    with (tmp_path / "medications.csv").open("w", encoding="utf-8", newline="") as handle:
        csv.writer(handle).writerows([row[:stop] + row[stop + 1 :] for row in rows])
    with pytest.raises(ValueError, match=r"medications\.csv: missing column\(s\) STOP"):
        load_synthea_csv(tmp_path)


def test_synthea_bad_date_reports_file_and_line(tmp_path: Path) -> None:
    for name in ("patients.csv", "conditions.csv", "medications.csv"):
        shutil.copy(SYNTHEA_MINI / name, tmp_path)
    text = (tmp_path / "conditions.csv").read_text(encoding="utf-8")
    (tmp_path / "conditions.csv").write_text(text.replace("2010-03-15", "15/03/2010"), encoding="utf-8")
    with pytest.raises(ValueError, match=r"conditions\.csv line 3: START '15/03/2010' is not a date"):
        load_synthea_csv(tmp_path)


# ---------------------------------------------------------------------------
# Compact cohort files
# ---------------------------------------------------------------------------


def test_compact_round_trip_gzip(tmp_path: Path) -> None:
    cohort = load_synthea_csv(SYNTHEA_MINI)
    cohort[0] = make_patient(cohort[0].synthetic_id, family_id="FAM-0001", given_name="Zoë")
    path = tmp_path / "cohort.csv.gz"
    assert write_compact_cohort(cohort, path) == len(cohort)
    assert load_compact_cohort(path) == cohort


def test_compact_round_trip_plain_csv_and_header(tmp_path: Path) -> None:
    cohort = generate_cohort(40, seed=3)
    path = tmp_path / "cohort.csv"
    write_compact_cohort(cohort, path)
    with path.open(encoding="utf-8", newline="") as handle:
        rows = list(csv.reader(handle))
    assert tuple(rows[0]) == COMPACT_COLUMNS
    assert len(rows) == 41
    assert load_compact_cohort(path) == cohort


def test_compact_file_is_deterministic_and_sorted(tmp_path: Path) -> None:
    cohort = generate_cohort(25, seed=11)
    first, second = tmp_path / "a.csv.gz", tmp_path / "b.csv.gz"
    write_compact_cohort(cohort, first)
    write_compact_cohort(list(reversed(cohort)), second)
    assert first.read_bytes() == second.read_bytes()
    assert first.read_bytes()[4:8] == b"\x00\x00\x00\x00"  # gzip header mtime is zero
    assert [patient.synthetic_id for patient in load_compact_cohort(second)] == sorted(p.synthetic_id for p in cohort)


def test_compact_medications_column_is_json(tmp_path: Path) -> None:
    patient = make_patient(
        medications=[
            CohortMedication(HUMALOG, "insulin", date(2021, 3, 1)),
            CohortMedication(METFORMIN, "metformin", None),
        ]
    )
    path = tmp_path / "one.csv.gz"
    write_compact_cohort([patient], path)
    with gzip.open(path, "rt", encoding="utf-8", newline="") as handle:
        row = list(csv.DictReader(handle))[0]
    assert row["medications"] == (
        f'[{{"drug_name":"{METFORMIN}","drug_class":"metformin","start_date":null}},'
        f'{{"drug_name":"{HUMALOG}","drug_class":"insulin","start_date":"2021-03-01"}}]'
    )
    assert row["age_at_diagnosis_months"] == "480"
    assert row["family_id"] == ""


def test_compact_rejects_duplicate_ids(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="Duplicate synthetic_id 'P-1'"):
        write_compact_cohort([make_patient(), make_patient()], tmp_path / "dup.csv.gz")


def _write_plain(path: Path, header: list[str], *rows: list[str]) -> None:
    with path.open("w", encoding="utf-8", newline="") as handle:
        csv.writer(handle).writerows([header, *rows])


def test_compact_rejects_wrong_header(tmp_path: Path) -> None:
    path = tmp_path / "bad.csv"
    _write_plain(path, ["id", "name"], ["1", "x"])
    with pytest.raises(ValueError, match="bad.csv: not a compact cohort file"):
        load_compact_cohort(path)


@pytest.mark.parametrize(
    ("column", "value", "message"),
    [
        ("sex", "F", "Unknown sex 'F'"),
        ("birth_date", "17/05/1980", "Invalid isoformat string"),
        ("age_at_diagnosis_months", "forty", "invalid literal for int"),
        ("medications", "[{", "not valid JSON"),
        ("medications", "{}", "must be a JSON list"),
        ("medications", '[{"drug_name": "x"}]', "drug_class"),
    ],
)
def test_compact_reports_invalid_values_with_line(tmp_path: Path, column: str, value: str, message: str) -> None:
    good = tmp_path / "good.csv"
    write_compact_cohort([make_patient("P-1"), make_patient("P-2")], good)
    with good.open(encoding="utf-8", newline="") as handle:
        rows = list(csv.reader(handle))
    rows[2][COMPACT_COLUMNS.index(column)] = value
    bad = tmp_path / "bad.csv"
    _write_plain(bad, *rows)
    with pytest.raises(ValueError, match=rf"bad\.csv line 3: .*{message}"):
        load_compact_cohort(bad)


def test_compact_rejects_short_rows(tmp_path: Path) -> None:
    path = tmp_path / "short.csv"
    _write_plain(path, list(COMPACT_COLUMNS), ["P-1", "Ana"])
    with pytest.raises(ValueError, match=r"short\.csv line 2: expected 9 columns, found 2"):
        load_compact_cohort(path)


# ---------------------------------------------------------------------------
# Generator
# ---------------------------------------------------------------------------


def test_generate_cohort_is_deterministic() -> None:
    assert generate_cohort(300, seed=42) == generate_cohort(300, seed=42)
    assert generate_cohort(300, seed=42) != generate_cohort(300, seed=43)
    # a prefix of a larger cohort is the smaller cohort
    assert generate_cohort(300, seed=42)[:50] == generate_cohort(50, seed=42)


def test_generate_cohort_sizes() -> None:
    assert generate_cohort(0, seed=1) == []
    with pytest.raises(ValueError, match="cohort size must be >= 0"):
        generate_cohort(-1, seed=1)
    cohort = generate_cohort(12, seed=1)
    assert [patient.synthetic_id for patient in cohort] == [f"GEN-{index:06d}" for index in range(1, 13)]


@pytest.fixture(scope="module")
def large_cohort() -> list[CohortPatient]:
    return generate_cohort(6000, seed=2026)


def test_generated_demographics_are_realistic(large_cohort: list[CohortPatient]) -> None:
    total = len(large_cohort)
    female = sum(patient.sex == "female" for patient in large_cohort) / total
    assert 0.48 < female < 0.54
    ages = [months_between(patient.birth_date, REFERENCE_DATE) // 12 for patient in large_cohort]
    assert min(ages) >= 0 and max(ages) <= 90
    children = sum(age < 18 for age in ages) / total
    assert 0.19 < children < 0.25
    assert 30 <= statistics.median(ages) <= 45
    assert all(patient.given_name and patient.family_name for patient in large_cohort)
    assert len({patient.family_name for patient in large_cohort}) >= 40


def test_generated_diabetes_distribution(large_cohort: list[CohortPatient]) -> None:
    types = Counter(patient.diabetes_type for patient in large_cohort)
    assert set(types) <= set(DIABETES_TYPES)
    diabetic = len(large_cohort) - types["none"]
    assert 0.075 < diabetic / len(large_cohort) < 0.12  # ~9.5 % expected
    assert types["type2"] / diabetic > 0.8
    assert 0.03 < types["type1"] / diabetic < 0.15
    for patient in large_cohort:
        age_months = months_between(patient.birth_date, REFERENCE_DATE)
        if patient.diabetes_type == "none":
            assert patient.age_at_diagnosis_months is None
            assert patient.medications == []
        else:
            assert 0 <= patient.age_at_diagnosis_months <= age_months
    type1_ages = [p.age_at_diagnosis_months / 12 for p in large_cohort if p.diabetes_type == "type1"]
    type2_ages = [p.age_at_diagnosis_months / 12 for p in large_cohort if p.diabetes_type == "type2"]
    assert statistics.median(type1_ages) < 20 < statistics.median(type2_ages)


def test_generated_medications(large_cohort: list[CohortPatient]) -> None:
    classes = Counter()
    for patient in large_cohort:
        drug_classes = {medication.drug_class for medication in patient.medications}
        classes.update(drug_classes)
        if patient.diabetes_type == "type1":
            assert "insulin" in drug_classes
        for medication in patient.medications:
            assert medication.drug_class in DRUG_CLASSES
            assert medication.drug_class == suggest_drug_class(medication.drug_name)
            assert is_glucose_lowering(medication.drug_name)
            assert medication.start_date is not None
            assert patient.birth_date <= medication.start_date <= REFERENCE_DATE
        assert patient.medications == sorted(patient.medications, key=lambda m: (m.start_date, m.drug_name))
    type2 = [patient for patient in large_cohort if patient.diabetes_type == "type2"]
    untreated = sum(not patient.medications for patient in type2) / len(type2)
    on_metformin = sum(any(m.drug_class == "metformin" for m in p.medications) for p in type2) / len(type2)
    assert 0.08 < untreated < 0.25
    assert 0.65 < on_metformin < 0.9
    assert all(classes[drug_class] > 0 for drug_class in DRUG_CLASSES)


def test_generated_medications_start_after_diagnosis(large_cohort: list[CohortPatient]) -> None:
    for patient in large_cohort:
        if patient.medications:
            earliest = min(medication.start_date for medication in patient.medications)
            assert months_between(patient.birth_date, earliest) >= patient.age_at_diagnosis_months


def test_generate_cohort_reference_date() -> None:
    cohort = generate_cohort(200, seed=5, reference_date=date(2000, 1, 1))
    assert all(patient.birth_date <= date(2000, 1, 1) for patient in cohort)
    assert all(m.start_date <= date(2000, 1, 1) for patient in cohort for m in patient.medications)


# ---------------------------------------------------------------------------
# Families
# ---------------------------------------------------------------------------


def test_make_families_groups_and_surnames() -> None:
    cohort = generate_cohort(200, seed=8)
    original = copy.deepcopy(cohort)
    families = make_families(cohort, 12, seed=4)
    assert cohort == original  # input untouched
    assert len(families) == len(cohort)
    groups: dict[str, list[CohortPatient]] = {}
    for patient in families:
        if patient.family_id:
            groups.setdefault(patient.family_id, []).append(patient)
    assert sorted(groups) == [f"FAM-{number:04d}" for number in range(1, 13)]
    for members in groups.values():
        assert 2 <= len(members) <= 5
        oldest = min(members, key=lambda patient: patient.birth_date)
        originals = {p.synthetic_id: p for p in cohort}
        assert {member.family_name for member in members} == {originals[oldest.synthetic_id].family_name}
    for before, after in zip(cohort, families):
        assert before.synthetic_id == after.synthetic_id
        if after.family_id is None:
            assert after == before
        else:
            assert (after.given_name, after.birth_date, after.medications) == (
                before.given_name,
                before.birth_date,
                before.medications,
            )


def test_make_families_is_deterministic() -> None:
    cohort = generate_cohort(100, seed=8)
    assert make_families(cohort, 6, seed=1) == make_families(cohort, 6, seed=1)
    assert make_families(cohort, 6, seed=1) != make_families(cohort, 6, seed=2)


def test_make_families_keeps_existing_families() -> None:
    cohort = make_families(generate_cohort(60, seed=9), 3, seed=1)
    existing = {p.synthetic_id: p.family_id for p in cohort if p.family_id}
    extended = make_families(cohort, 4, seed=2)
    for patient in extended:
        if patient.synthetic_id in existing:
            assert patient.family_id == existing[patient.synthetic_id]
    new_ids = {p.family_id for p in extended if p.family_id} - set(existing.values())
    assert new_ids == {"FAM-0004", "FAM-0005", "FAM-0006", "FAM-0007"}


def test_make_families_fits_small_cohorts() -> None:
    cohort = generate_cohort(6, seed=1)
    families = make_families(cohort, 3, seed=7)
    assert Counter(patient.family_id for patient in families) == {"FAM-0001": 2, "FAM-0002": 2, "FAM-0003": 2}


def test_make_families_errors() -> None:
    cohort = generate_cohort(5, seed=1)
    with pytest.raises(ValueError, match="Cannot form 3 families of 2-5 relatives: only 5 patient"):
        make_families(cohort, 3, seed=1)
    with pytest.raises(ValueError, match="number of families must be >= 0"):
        make_families(cohort, -1, seed=1)
    assert make_families(cohort, 0, seed=1) == cohort


# ---------------------------------------------------------------------------
# Patient VCF writer
# ---------------------------------------------------------------------------

VARIANTS = [
    ("chr12", 120978900, "g", "a", "heterozygous"),
    ("7", 44145200, "C", "T", "homozygous"),
    ("X", 1_000_000, "A", "G", "hemizygous"),
    ("1", 1_000_000, "A", "G", "heterozygous"),
    ("11", 17387100, "C", "T", "heterozygous"),
]


def test_format_patient_vcf_header() -> None:
    text = format_patient_vcf("SAMPLE-1", VARIANTS)
    lines = text.splitlines()
    assert lines[0] == "##fileformat=VCFv4.2"
    assert "##reference=GRCh38" in lines
    assert "##ge_ehr_synthetic=true" in lines
    contigs = {
        line.split("ID=")[1].split(",")[0]: int(line.split("length=")[1].split(",")[0])
        for line in lines
        if line.startswith("##contig=")
    }
    assert contigs == GRCH38_CHROMOSOME_LENGTHS
    assert contigs["1"] == 248_956_422
    for chrom, length in GRCH38_CONTIG_LENGTHS.items():
        assert contigs[chrom] == length
    assert '##FORMAT=<ID=GT,Number=1,Type=String,Description="Genotype">' in lines
    assert lines[len(lines) - len(VARIANTS) - 1].split("\t") == [
        "#CHROM", "POS", "ID", "REF", "ALT", "QUAL", "FILTER", "INFO", "FORMAT", "SAMPLE-1",
    ]
    assert text.endswith("\n") and "\r" not in text


def test_format_patient_vcf_records_sorted_and_normalised() -> None:
    lines = format_patient_vcf("SAMPLE-1", VARIANTS).splitlines()
    records = [line.split("\t") for line in lines if not line.startswith("#")]
    assert [(r[0], r[1], r[3], r[4]) for r in records] == [
        ("1", "1000000", "A", "G"),
        ("7", "44145200", "C", "T"),
        ("11", "17387100", "C", "T"),
        ("12", "120978900", "G", "A"),
        ("X", "1000000", "A", "G"),
    ]
    genotypes = {r[0]: r[9].split(":")[0] for r in records}
    assert genotypes == {"1": "0/1", "7": "1/1", "11": "0/1", "12": "0/1", "X": "1"}
    for record in records:
        assert (record[2], record[6], record[7], record[8]) == (".", "PASS", ".", "GT:DP:GQ")
        depth, quality = map(int, record[9].split(":")[1:])
        assert 25 <= depth <= 80 and 60 <= quality <= 99
        assert 100 <= int(record[5]) < 1000


def test_format_patient_vcf_is_deterministic() -> None:
    assert format_patient_vcf("S", VARIANTS) == format_patient_vcf("S", list(reversed(VARIANTS)))
    assert format_patient_vcf("S", iter(VARIANTS)) == format_patient_vcf("S", VARIANTS)
    # QUAL/DP/GQ depend on the sample, so two patients with the same variant get different values
    assert format_patient_vcf("S", VARIANTS) != format_patient_vcf("T", VARIANTS).replace("\tT\n", "\tS\n")
    assert format_patient_vcf("S", []).splitlines()[-1].startswith("#CHROM")


def test_patient_vcf_parses_with_the_f1_parser(tmp_path: Path) -> None:
    path = tmp_path / "patient.vcf"
    write_patient_vcf(path, "SAMPLE-1", VARIANTS)
    parsed = parse_patient_vcf(path.read_bytes(), path.name)
    assert parsed.sample_id == "SAMPLE-1"
    assert parsed.warnings == []
    assert parsed.total_records == 5
    assert parsed.off_panel_skipped == 2  # chr1 and chrX
    assert {(v.gene, v.chrom, v.pos, v.ref, v.alt, v.zygosity) for v in parsed.variants} == {
        ("HNF1A", "12", 120978900, "G", "A", "heterozygous"),
        ("GCK", "7", 44145200, "C", "T", "homozygous"),
        ("KCNJ11", "11", 17387100, "C", "T", "heterozygous"),
    }


def test_header_only_vcf_is_accepted(tmp_path: Path) -> None:
    path = tmp_path / "empty.vcf"
    write_patient_vcf(path, "SAMPLE-0", [])
    parsed = parse_patient_vcf(path.read_bytes(), path.name)
    assert parsed.variants == [] and parsed.total_records == 0


@pytest.mark.parametrize(
    ("sample", "variants", "message"),
    [
        ("", [], "Invalid VCF sample id ''"),
        ("two words", [], "Invalid VCF sample id 'two words'"),
        ("S", [("23", 10, "A", "G", "heterozygous")], "Unknown GRCh38 chromosome '23'"),
        ("S", [("7", 0, "A", "G", "heterozygous")], "Position 0 on chromosome 7 is invalid"),
        ("S", [("7", 159_345_974, "A", "G", "heterozygous")], "from 1 to 159,345,973"),
        ("S", [("7", "100", "A", "G", "heterozygous")], "Position '100' on chromosome 7"),
        ("S", [("7", 100, "A", "<DEL>", "heterozygous")], "ALT allele '<DEL>'"),
        ("S", [("7", 100, "", "G", "heterozygous")], "REF allele ''"),
        ("S", [("7", 100, "A", "a", "heterozygous")], "REF and ALT are identical"),
        ("S", [("7", 100, "A", "G", "het")], "Unknown zygosity 'het'"),
        ("S", [("7", 100, "A", "G", "heterozygous"), ("chr7", 100, "a", "g", "homozygous")], "listed more than once"),
    ],
)
def test_format_patient_vcf_rejects_invalid_input(sample: str, variants: list, message: str) -> None:
    with pytest.raises(ValueError, match=message):
        format_patient_vcf(sample, variants)
