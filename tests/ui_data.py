"""Test data shared by the UI-A route tests (patients and alerts blueprints).

:func:`build_clinic` creates a small, realistic clinic through the real services: a current
release, four rules (D1, D2, D5, D6 as in SPEC §6.1) and patients whose VCFs are ingested with
:func:`ge_ehr.services.patient_records.ingest_vcf`, so alerts, notes and their provenance events
are produced by the decision-support engine rather than written by hand.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date

from ge_ehr.constants import GENES
from ge_ehr.extensions import db
from ge_ehr.models import KbRelease, Patient, RuleVersion, User
from ge_ehr.services import patient_records
from tests.factories import add_known_variant, make_release, make_rule_version, rule_definition

#: (chrom, pos, ref, alt) of the variants used by the clinic.
HNF1A_P = ("12", 120978900, "G", "A")
GCK_LP = ("7", 44150500, "G", "A")
HNF1A_VUS = ("12", 120994300, "C", "T")
OFF_PANEL = ("1", 1000000, "A", "G")

#: GRCh38 contig lengths written into generated VCF headers.
_CONTIGS = {"1": 248_956_422, "7": 159_345_973, "11": 135_086_622, "12": 133_275_309, "17": 83_257_441, "20": 64_444_167}


def vcf_bytes(*variants: tuple[str, int, str, str], genotype: str = "0/1", sample: str = "SAMPLE1") -> bytes:
    """A minimal synthetic GRCh38 VCF 4.2 with one sample carrying ``variants``."""
    lines = ["##fileformat=VCFv4.2", "##source=GE-EHR test VCF (synthetic data only)", "##reference=GRCh38"]
    lines += [f"##contig=<ID={chrom},length={length},assembly=GRCh38>" for chrom, length in _CONTIGS.items()]
    lines += [
        '##FILTER=<ID=PASS,Description="All filters passed">',
        '##FORMAT=<ID=GT,Number=1,Type=String,Description="Genotype">',
        "\t".join(["#CHROM", "POS", "ID", "REF", "ALT", "QUAL", "FILTER", "INFO", "FORMAT", sample]),
    ]
    lines += [f"{chrom}\t{pos}\t.\t{ref}\t{alt}\t50\tPASS\t.\tGT\t{genotype}" for chrom, pos, ref, alt in variants]
    return ("\n".join(lines) + "\n").encode()


@dataclass
class Clinic:
    """The objects created by :func:`build_clinic`."""

    release: KbRelease
    rules: dict[str, RuleVersion]
    carrier: Patient
    gck_patient: Patient
    vus_patient: Patient
    plain: Patient


def make_rules() -> dict[str, RuleVersion]:
    """Current versions of D1, D2, D5 and D6 (SPEC §6.1, Table III)."""
    definitions = [
        rule_definition(
            "D1",
            genes=("GCK",),
            clinical={"any_of_drug_classes": ["insulin", "metformin", "sulfonylurea", "other"]},
            severity="medium",
            title="GCK diabetes on glucose-lowering medication",
            recommendation="Medication usually not required outside pregnancy; review whether it can be stopped.",
            source="Kang 2021",
        ),
        rule_definition(
            "D2",
            genes=("HNF1A",),
            clinical={"any_of_drug_classes": ["insulin", "metformin"], "none_of_drug_classes": ["sulfonylurea"]},
            severity="high",
            title="HNF1A diabetes on insulin or metformin",
            recommendation="Consider low-dose sulfonylurea as first-line treatment.",
            source="Kang 2021; Naylor 2024",
        ),
        rule_definition(
            "D5",
            genes=GENES,
            clinical=None,
            severity="low",
            kind="cascade",
            title="Monogenic diabetes variant: family cascade testing",
            recommendation="Offer genetic counselling and cascade testing of first-degree relatives.",
            source="Autosomal dominant inheritance",
        ),
        rule_definition(
            "D6",
            genes=("GCK", "HNF1A", "HNF4A"),
            classes=("VUS",),
            clinical=None,
            severity="info",
            kind="monitoring",
            title="Variant of uncertain significance",
            recommendation="No treatment change; variant flagged and monitored for reclassification.",
            source="ClinGen MDEP",
        ),
    ]
    return {definition["code"]: make_rule_version(definition) for definition in definitions}


def add_patient(
    mrn: str,
    *,
    given_name: str = "Test",
    family_name: str = "Patient",
    diabetes_type: str = "type2",
    age_at_diagnosis_months: int | None = 300,
    medications: Iterable[tuple[str, str]] = (),
    variants: Iterable[tuple[str, int, str, str]] = (),
    family_id: str | None = None,
    user: User | None = None,
) -> Patient:
    """Create a patient through the services, add ``(drug name, class)`` medications, ingest a VCF."""
    patient = patient_records.create_patient(
        mrn=mrn,
        given_name=given_name,
        family_name=family_name,
        sex="female",
        birth_date=date(1980, 5, 17),
        family_id=family_id,
        diabetes_type=diabetes_type,
        age_at_diagnosis_months=age_at_diagnosis_months if diabetes_type != "none" else None,
        user=user,
    )
    for drug_name, drug_class in medications:
        patient_records.add_medication(patient, drug_name=drug_name, drug_class=drug_class, user=user)
    variants = list(variants)
    if variants:
        patient_records.ingest_vcf(patient, vcf_bytes(*variants), f"{mrn}.vcf", user=user)
    return patient


def build_clinic(user: User | None = None) -> Clinic:
    """A current release, rules D1/D2/D5/D6 and four patients (committed).

    * ``carrier`` (UI-0001, HNF1A P, insulin, family FAM-1): D2 high + D5 low alerts;
    * ``gck_patient`` (UI-0002, GCK LP, metformin, family FAM-1): D1 medium + D5 low alerts;
    * ``vus_patient`` (UI-0003, HNF1A VUS, insulin): D6 info alert;
    * ``plain`` (UI-0004, no VCF): nothing.
    """
    release = make_release("UI R1 (2025-01, synthetic)", current=True)
    add_known_variant(
        release,
        *HNF1A_P,
        "P",
        gene="HNF1A",
        review_stars=3,
        review_status="reviewed_by_expert_panel",
        is_expert_panel=True,
        condition="Maturity-onset diabetes of the young type 3",
        hgvs="NC_000012.12:g.120978900G>A",
    )
    add_known_variant(release, *GCK_LP, "LP", gene="GCK", review_stars=1, condition="MODY2")
    add_known_variant(release, *HNF1A_VUS, "VUS", gene="HNF1A")
    rules = make_rules()
    carrier = add_patient(
        "UI-0001",
        given_name="Maya",
        family_name="Okonkwo",
        diabetes_type="type1",
        age_at_diagnosis_months=180,
        medications=[("Insulin glargine", "insulin")],
        variants=[HNF1A_P, OFF_PANEL],
        family_id="FAM-1",
        user=user,
    )
    gck_patient = add_patient(
        "UI-0002",
        given_name="Hannah",
        family_name="Lindqvist",
        medications=[("Metformin", "metformin")],
        variants=[GCK_LP],
        family_id="FAM-1",
        user=user,
    )
    vus_patient = add_patient(
        "UI-0003",
        given_name="Arjun",
        family_name="Mehta",
        diabetes_type="type1",
        medications=[("Insulin degludec", "insulin")],
        variants=[HNF1A_VUS],
        user=user,
    )
    plain = add_patient("UI-0004", given_name="Eleanor", family_name="Fitzgerald", user=user)
    db.session.commit()
    return Clinic(
        release=release, rules=rules, carrier=carrier, gck_patient=gck_patient, vus_patient=vus_patient, plain=plain
    )
