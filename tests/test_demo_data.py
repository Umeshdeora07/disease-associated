"""The demo dataset (data/demo/patients.json, data/demo/vcfs/) must match SPEC §11 exactly."""

from __future__ import annotations

import json
import re
from datetime import date
from pathlib import Path
from typing import Any

import pytest

from evaluation.cohort import format_patient_vcf, months_between
from ge_ehr.constants import DIABETES_TYPES, SEXES, gene_for_position, suggest_drug_class
from ge_ehr.services.vcf_parser import VcfError, parse_patient_vcf

DEMO_DIR = Path(__file__).resolve().parent.parent / "data" / "demo"
VCF_DIR = DEMO_DIR / "vcfs"

# SPEC §11 variant table: id → (gene, chrom, pos, ref, alt)
SPEC_VARIANTS: dict[str, tuple[str | None, str, int, str, str]] = {
    "V01": ("HNF1A", "12", 120978900, "G", "A"),
    "V02": ("GCK", "7", 44145200, "C", "T"),
    "V03": ("HNF4A", "20", 44401300, "C", "T"),
    "V04": ("KCNJ11", "11", 17387100, "C", "T"),
    "V05": ("INS", "11", 2160500, "G", "A"),
    "V06": ("HNF1A", "12", 120994300, "C", "T"),
    "V07": ("GCK", "7", 44150500, "G", "A"),
    "V08": ("HNF1A", "12", 120999100, "A", "G"),
    "V09": ("ABCC8", "11", 17410300, "G", "A"),
    "V10": ("HNF4A", "20", 44410200, "G", "T"),
    "V11": ("GCK", "7", 44160100, "T", "C"),
    "B01": ("HNF1A", "12", 121001000, "C", "T"),
    "B02": ("GCK", "7", 44180000, "A", "G"),
    "B03": ("HNF1B", "17", 37700000, "G", "A"),
    "B04": ("ABCC8", "11", 17450000, "C", "T"),
    "X01": (None, "1", 1000000, "A", "G"),
}

# SPEC §11 patient table: mrn → (scenarios, variant ids, diabetes type, age at dx (months), drug classes)
SPEC_PATIENTS: dict[str, tuple[list[str], list[str], str, int | None, set[str]]] = {
    "DEMO-0001": (["S1"], ["V01", "B01"], "type1", 180, {"insulin"}),
    "DEMO-0002": (["S2"], ["V01"], "type2", 300, {"sulfonylurea"}),
    "DEMO-0003": (["S3"], ["V02"], "type2", 360, {"metformin"}),
    "DEMO-0004": (["S4"], ["V02", "B02"], "other", 240, set()),
    "DEMO-0005": (["S5", "RS3"], ["V03"], "type2", 264, {"metformin"}),
    "DEMO-0006": (["S6"], ["V04"], "neonatal", 2, {"insulin"}),
    "DEMO-0007": (["S7"], ["V05"], "type1", 96, {"insulin"}),
    "DEMO-0008": (["S8", "RS1"], ["V06", "B03"], "type1", 192, {"insulin"}),
    "DEMO-0009": (["S9"], ["B01", "B02", "B04", "X01"], "type2", 480, {"metformin"}),
    "DEMO-0010": (["RS2"], ["V07"], "type2", 420, {"metformin"}),
    "DEMO-0011": (["RS4"], ["V08"], "type1", 150, {"insulin"}),
    "DEMO-0012": (["RS5"], ["V09"], "neonatal", 3, {"insulin"}),
    "DEMO-0013": (["RS6"], ["V10"], "type2", 324, {"metformin"}),
    "DEMO-0014": (["RS6"], ["V10"], "type1", 168, {"insulin"}),
    "DEMO-0015": (["RS6"], ["V10"], "none", None, set()),
}
FAMILY_RS6 = {"DEMO-0013", "DEMO-0014", "DEMO-0015"}

PATIENT_KEYS = {
    "mrn", "given_name", "family_name", "sex", "birth_date", "family_id", "diagnosis", "medications", "vcf",
    "scenarios", "variants", "notes",
}
MEDICATION_KEYS = {"drug_name", "drug_class", "dose", "start_date"}
VARIANT_KEYS = {"id", "gene", "chrom", "pos", "ref", "alt", "zygosity"}


def load_patients() -> list[dict[str, Any]]:
    return json.loads((DEMO_DIR / "patients.json").read_text(encoding="utf-8"))


PATIENTS = load_patients()
BY_MRN = {patient["mrn"]: patient for patient in PATIENTS}


def _variant_tuples(patient: dict[str, Any]) -> list[tuple[str, int, str, str, str]]:
    return [(v["chrom"], v["pos"], v["ref"], v["alt"], v["zygosity"]) for v in patient["variants"]]


# ---------------------------------------------------------------------------
# patients.json
# ---------------------------------------------------------------------------


def test_patients_json_lists_the_fifteen_demo_patients_in_order() -> None:
    assert isinstance(PATIENTS, list)
    assert [patient["mrn"] for patient in PATIENTS] == [f"DEMO-{number:04d}" for number in range(1, 16)]
    assert list(BY_MRN) == list(SPEC_PATIENTS)


@pytest.mark.parametrize("mrn", SPEC_PATIENTS)
def test_patient_record_shape(mrn: str) -> None:
    patient = BY_MRN[mrn]
    assert set(patient) == PATIENT_KEYS
    assert set(patient["diagnosis"]) == {"diabetes_type", "age_at_diagnosis_months", "diagnosed_on"}
    assert all(set(medication) == MEDICATION_KEYS for medication in patient["medications"])
    assert all(set(variant) == VARIANT_KEYS for variant in patient["variants"])
    assert patient["vcf"] == f"{mrn}.vcf"
    assert patient["sex"] in SEXES
    assert patient["given_name"].strip() and patient["family_name"].strip()
    assert len(patient["given_name"]) <= 100 and len(patient["family_name"]) <= 100
    assert "synthetic" in patient["notes"].lower()


@pytest.mark.parametrize("mrn", SPEC_PATIENTS)
def test_patient_matches_spec_table(mrn: str) -> None:
    scenarios, variant_ids, diabetes_type, age_months, drug_classes = SPEC_PATIENTS[mrn]
    patient = BY_MRN[mrn]
    assert patient["scenarios"] == scenarios
    assert [variant["id"] for variant in patient["variants"]] == variant_ids
    for variant in patient["variants"]:
        gene, chrom, pos, ref, alt = SPEC_VARIANTS[variant["id"]]
        assert (variant["gene"], variant["chrom"], variant["pos"], variant["ref"], variant["alt"]) == (
            gene, chrom, pos, ref, alt,
        )
        assert variant["zygosity"] == "heterozygous"
        assert gene_for_position(chrom, pos) == gene
    assert patient["diagnosis"]["diabetes_type"] == diabetes_type
    assert patient["diagnosis"]["age_at_diagnosis_months"] == age_months
    assert {medication["drug_class"] for medication in patient["medications"]} == drug_classes


def test_diagnosis_types_are_valid_and_dates_consistent() -> None:
    today = date.today()
    for patient in PATIENTS:
        birth = date.fromisoformat(patient["birth_date"])
        assert 0 < months_between(birth, today) // 12 < 100
        diagnosis = patient["diagnosis"]
        assert diagnosis["diabetes_type"] in DIABETES_TYPES
        if diagnosis["diabetes_type"] == "none":
            assert diagnosis["age_at_diagnosis_months"] is None and diagnosis["diagnosed_on"] is None
            continue
        diagnosed_on = date.fromisoformat(diagnosis["diagnosed_on"])
        assert months_between(birth, diagnosed_on) == diagnosis["age_at_diagnosis_months"], patient["mrn"]
        assert diagnosed_on <= today


def test_medications_are_consistent() -> None:
    for patient in PATIENTS:
        diagnosed_on = patient["diagnosis"]["diagnosed_on"]
        for medication in patient["medications"]:
            assert suggest_drug_class(medication["drug_name"]) == medication["drug_class"], medication
            assert len(medication["drug_name"]) <= 128
            assert medication["dose"] and len(medication["dose"]) <= 64
            start = date.fromisoformat(medication["start_date"])
            assert date.fromisoformat(diagnosed_on) <= start <= date.today(), patient["mrn"]


def test_sulfonylurea_of_s2_is_gliclazide() -> None:
    (medication,) = BY_MRN["DEMO-0002"]["medications"]
    assert "gliclazide" in medication["drug_name"].lower()


def test_family_rs6() -> None:
    for patient in PATIENTS:
        expected = "FAM-RS6" if patient["mrn"] in FAMILY_RS6 else None
        assert patient["family_id"] == expected
    family = [BY_MRN[mrn] for mrn in sorted(FAMILY_RS6)]
    assert len({patient["family_name"] for patient in family}) == 1
    mother, son, daughter = family
    assert (mother["sex"], son["sex"], daughter["sex"]) == ("female", "male", "female")
    mother_birth = date.fromisoformat(mother["birth_date"])
    for child in (son, daughter):
        assert 18 <= months_between(mother_birth, date.fromisoformat(child["birth_date"])) // 12 <= 45


def test_names_are_distinct() -> None:
    assert len({(p["given_name"], p["family_name"]) for p in PATIENTS}) == len(PATIENTS)
    surnames = {p["family_name"] for p in PATIENTS if p["mrn"] not in FAMILY_RS6}
    assert len(surnames) == len(PATIENTS) - len(FAMILY_RS6)


def test_v11_is_carried_by_nobody() -> None:
    assert all(variant["id"] != "V11" for patient in PATIENTS for variant in patient["variants"])
    assert all("44160100" not in path.read_text(encoding="utf-8") for path in VCF_DIR.glob("*.vcf"))


# ---------------------------------------------------------------------------
# Demo VCFs
# ---------------------------------------------------------------------------


def test_vcf_directory_contents() -> None:
    expected = {f"{mrn}.vcf" for mrn in SPEC_PATIENTS} | {"malformed.vcf", "README.md"}
    assert {path.name for path in VCF_DIR.iterdir()} == expected


@pytest.mark.parametrize("mrn", SPEC_PATIENTS)
def test_demo_vcf_parses_to_the_expected_panel_variants(mrn: str) -> None:
    patient = BY_MRN[mrn]
    parsed = parse_patient_vcf((VCF_DIR / patient["vcf"]).read_bytes(), patient["vcf"])
    assert parsed.sample_id == mrn
    assert parsed.fileformat == "VCFv4.2"
    assert parsed.warnings == []
    assert parsed.total_records == len(patient["variants"])
    expected = {
        (gene, chrom, pos, ref, alt, "heterozygous")
        for gene, chrom, pos, ref, alt in (SPEC_VARIANTS[v] for v in SPEC_PATIENTS[mrn][1])
        if gene is not None
    }
    assert {(v.gene, v.chrom, v.pos, v.ref, v.alt, v.zygosity) for v in parsed.variants} == expected
    assert len(parsed.variants) == len(expected)
    assert all(variant.filter == "PASS" and variant.genotype == "0/1" for variant in parsed.variants)
    assert parsed.off_panel_skipped == (1 if mrn == "DEMO-0009" else 0)
    assert parsed.non_pass_skipped == parsed.ref_calls_skipped == 0


@pytest.mark.parametrize("mrn", SPEC_PATIENTS)
def test_demo_vcf_header_and_records(mrn: str) -> None:
    lines = (VCF_DIR / f"{mrn}.vcf").read_text(encoding="utf-8").splitlines()
    assert lines[0] == "##fileformat=VCFv4.2"
    assert "##reference=GRCh38" in lines
    contig_lengths = dict(re.findall(r"^##contig=<ID=([^,]+),length=(\d+)", "\n".join(lines), re.MULTILINE))
    assert {chrom: int(contig_lengths[chrom]) for chrom in ("1", "7", "11", "12", "17", "20")} == {
        "1": 248_956_422, "7": 159_345_973, "11": 135_086_622, "12": 133_275_309, "17": 83_257_441, "20": 64_444_167,
    }
    header = lines[[line.startswith("#CHROM") for line in lines].index(True)]
    assert header.split("\t") == ["#CHROM", "POS", "ID", "REF", "ALT", "QUAL", "FILTER", "INFO", "FORMAT", mrn]
    records = [line.split("\t") for line in lines if not line.startswith("#")]
    order = [str(n) for n in range(1, 23)]
    assert [(order.index(r[0]), int(r[1])) for r in records] == sorted((order.index(r[0]), int(r[1])) for r in records)
    for record in records:
        assert record[6] == "PASS" and record[8] == "GT:DP:GQ" and record[9].startswith("0/1:")


@pytest.mark.parametrize("mrn", SPEC_PATIENTS)
def test_demo_vcf_is_reproducible_from_patients_json(mrn: str) -> None:
    patient = BY_MRN[mrn]
    assert (VCF_DIR / patient["vcf"]).read_text(encoding="utf-8") == format_patient_vcf(mrn, _variant_tuples(patient))


def test_malformed_vcf_is_rejected_at_the_bad_position() -> None:
    data = (VCF_DIR / "malformed.vcf").read_bytes()
    lines = data.decode("utf-8").splitlines()
    bad_pos_line = next(number for number, line in enumerate(lines, 1) if "12O994300" in line)
    with pytest.raises(VcfError) as rejected:
        parse_patient_vcf(data, "malformed.vcf")
    assert rejected.value.line == bad_pos_line
    assert "POS must be a positive whole number" in rejected.value.message


def test_malformed_vcf_also_has_a_short_data_line() -> None:
    data = (VCF_DIR / "malformed.vcf").read_bytes().replace(b"12O994300", b"120994300")
    lines = data.decode("utf-8").splitlines()
    with pytest.raises(VcfError) as rejected:
        parse_patient_vcf(data, "malformed.vcf")
    assert rejected.value.line == len(lines)
    assert len(lines[-1].split("\t")) < 10
    assert "Expected 10 tab-separated columns" in rejected.value.message


def test_malformed_vcf_is_marked_synthetic() -> None:
    text = (VCF_DIR / "malformed.vcf").read_text(encoding="utf-8")
    assert text.startswith("##fileformat=VCFv4.2\n")
    assert "##ge_ehr_synthetic=true" in text
