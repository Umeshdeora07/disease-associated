"""Tests for ClinVar term normalisation and classification lookup (SPEC §5.1, §5.2)."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

import pytest
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import event, insert

from ge_ehr.models import KnownVariant, VariantKey
from ge_ehr.services import classification
from ge_ehr.services.classification import (
    IN_CLAUSE_CHUNK,
    NOT_CLASSIFIED,
    KnownClass,
    classification_from_text,
    classified_variants_for_patient,
    classify_keys,
    infer_mechanism,
    is_expert_panel_status,
    normalize_clinvar_significance,
    review_stars,
)
from ge_ehr.services.rules import ClassifiedVariant
from tests.factories import add_known_variant, add_patient_variant, make_patient, make_release


@contextmanager
def count_queries(db: SQLAlchemy) -> Iterator[list[str]]:
    """Collect the SQL statements executed inside the block."""
    statements: list[str] = []

    def record(conn, cursor, statement, parameters, context, executemany) -> None:  # noqa: ANN001, ARG001
        statements.append(statement)

    event.listen(db.engine, "before_cursor_execute", record)
    try:
        yield statements
    finally:
        event.remove(db.engine, "before_cursor_execute", record)


# ---------------------------------------------------------------------------
# normalize_clinvar_significance
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("clnsig", "expected"),
    [
        # every CLNSIG value named in SPEC §5.1
        ("Pathogenic", "P"),
        ("Likely_pathogenic", "LP"),
        ("Pathogenic/Likely_pathogenic", "LP"),
        ("Pathogenic,_low_penetrance", "P"),
        ("Likely_pathogenic,_low_penetrance", "LP"),
        ("Uncertain_significance", "VUS"),
        ("Conflicting_interpretations_of_pathogenicity", "VUS"),
        ("Conflicting_classifications_of_pathogenicity", "VUS"),
        ("Likely_benign", "LB"),
        ("Benign", "B"),
        ("Benign/Likely_benign", "LB"),
        ("not_provided", "NC"),
        ("drug_response", "NC"),
        ("risk_factor", "NC"),
        ("association", "NC"),
        ("protective", "NC"),
        ("Affects", "NC"),
        ("other", "NC"),
        ("no_classification_for_the_single_variant", "NC"),
        ("Uncertain_risk_allele", "NC"),
        (None, "NC"),
        ("", "NC"),
        # secondary terms after '|' are ignored
        ("Pathogenic|risk_factor", "P"),
        ("Likely_benign|other", "LB"),
        ("Conflicting_classifications_of_pathogenicity|drug_response|other", "VUS"),
        ("drug_response|Pathogenic", "NC"),
        ("risk_factor|Uncertain_significance", "NC"),
        # combinations seen in ClinVar
        ("Pathogenic/Likely_pathogenic/Pathogenic,_low_penetrance", "LP"),
        ("Pathogenic/Likely_pathogenic/Likely_risk_allele", "LP"),
        ("Likely_pathogenic/Likely_risk_allele", "LP"),
        ("Uncertain_significance/Uncertain_risk_allele", "VUS"),
        ("Likely_risk_allele", "NC"),
        ("no_classifications_from_unflagged_records", "NC"),
        ("Pathogenic/Benign", "VUS"),
        # older releases attach non-ACMG terms with ",_"
        ("Pathogenic,_other", "P"),
        ("Pathogenic/Likely_pathogenic,_other", "LP"),
        ("Benign/Likely_benign,_drug_response", "LB"),
        ("Uncertain_significance,_risk_factor", "VUS"),
        ("Conflicting_interpretations_of_pathogenicity,_other", "VUS"),
        # case, spaces and spacing are irrelevant
        ("PATHOGENIC", "P"),
        ("likely pathogenic", "LP"),
        ("Likely  pathogenic", "LP"),
        (" Benign / Likely benign ", "LB"),
        ("Pathogenic, low penetrance", "P"),
        ("Pathogenic ,low_penetrance", "P"),
        ("conflicting classifications of pathogenicity", "VUS"),
    ],
)
def test_normalize_clinvar_significance(clnsig: str | None, expected: str) -> None:
    assert normalize_clinvar_significance(clnsig) == expected


# ---------------------------------------------------------------------------
# Review status
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("status", "stars", "expert"),
    [
        ("practice_guideline", 4, True),
        ("reviewed_by_expert_panel", 3, True),
        ("criteria_provided,_multiple_submitters,_no_conflicts", 2, False),
        ("criteria_provided,_single_submitter", 1, False),
        ("criteria_provided,_conflicting_interpretations", 1, False),
        ("criteria_provided,_conflicting_classifications", 1, False),
        ("no_assertion_criteria_provided", 0, False),
        ("no_classification_provided", 0, False),
        ("no_interpretation_for_the_single_variant", 0, False),
        ("no_classification_for_the_single_variant", 0, False),
        ("criteria_provided", 0, False),
        ("", 0, False),
        (None, 0, False),
        ("Reviewed by expert panel", 3, True),
        ("criteria provided, multiple submitters, no conflicts", 2, False),
        ("criteria_provided,single_submitter", 1, False),
        ("PRACTICE_GUIDELINE", 4, True),
    ],
)
def test_review_stars_and_expert_panel_status(status: str | None, stars: int, expert: bool) -> None:
    assert review_stars(status) == stars
    assert is_expert_panel_status(status) is expert


# ---------------------------------------------------------------------------
# Mechanism
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("gene", "condition", "mechanism"),
    [
        ("KCNJ11", "Permanent_neonatal_diabetes_mellitus", "activating"),
        ("ABCC8", "Permanent neonatal diabetes mellitus", "activating"),
        ("ABCC8", "Transient_neonatal_diabetes_mellitus|not_provided", "activating"),
        ("KCNJ11", "Diabetes_mellitus,_transient_neonatal,_3", "activating"),
        ("ABCC8", "Maturity-onset_diabetes_of_the_young", "activating"),
        ("KCNJ11", "Hyperinsulinemic_hypoglycemia,_familial,_2", "inactivating"),
        ("ABCC8", "Congenital hyperinsulinism", "inactivating"),
        ("ABCC8", "Hyperinsulinaemic hypoglycaemia", "inactivating"),
        ("KCNJ11", "Neonatal_hypoglycemia", "inactivating"),
        ("KCNJ11", "Permanent_neonatal_diabetes_mellitus|Hyperinsulinemic_hypoglycemia,_familial,_2", "unknown"),
        ("ABCC8", "Permanent neonatal diabetes mellitus; Hyperinsulinemic hypoglycemia, familial, 1", "unknown"),
        ("KCNJ11", "not_provided", "unknown"),
        ("KCNJ11", "not specified", "unknown"),
        ("KCNJ11", "", "unknown"),
        ("KCNJ11", None, "unknown"),
        ("kcnj11", "Permanent neonatal diabetes mellitus", "activating"),
        ("GCK", "Permanent neonatal diabetes mellitus", "unknown"),
        ("INS", "Permanent neonatal diabetes mellitus", "unknown"),
        ("HNF1A", "Hyperinsulinemic hypoglycemia", "unknown"),
        ("", "Permanent neonatal diabetes mellitus", "unknown"),
    ],
)
def test_infer_mechanism(gene: str, condition: str | None, mechanism: str) -> None:
    assert infer_mechanism(gene, condition) == mechanism


@pytest.mark.parametrize(
    ("text", "code"),
    [
        ("P", "P"),
        ("lp", "LP"),
        ("VUS", "VUS"),
        ("LB", "LB"),
        ("b", "B"),
        ("Pathogenic", "P"),
        ("Likely pathogenic", "LP"),
        ("likely_pathogenic", "LP"),
        ("Uncertain significance", "VUS"),
        ("Likely benign", "LB"),
        ("BENIGN", "B"),
        (" Likely  benign ", "LB"),
        ("NC", None),
        ("Not classified", None),
        ("Pathogenic/Likely_pathogenic", None),
        ("probably bad", None),
        ("", None),
        (None, None),
    ],
)
def test_classification_from_text(text: str | None, code: str | None) -> None:
    assert classification_from_text(text) == code


# ---------------------------------------------------------------------------
# classify_keys
# ---------------------------------------------------------------------------


def test_classify_keys_without_release_is_all_not_classified(db: SQLAlchemy) -> None:
    key = VariantKey.get_or_create("7", 44145200, "C", "T")
    with count_queries(db) as statements:
        assert classify_keys(None, [key.id, 999]) == {key.id: NOT_CLASSIFIED, 999: NOT_CLASSIFIED}
    assert statements == []


def test_classify_keys_returns_release_classification_and_not_classified_for_absent_keys(db: SQLAlchemy) -> None:
    release, other = make_release("R1"), make_release("R2")
    known = add_known_variant(
        release,
        "11",
        17387100,
        "C",
        "T",
        "P",
        gene="KCNJ11",
        mechanism="activating",
        is_expert_panel=True,
        review_status="reviewed_by_expert_panel",
        review_stars=3,
        condition="Permanent neonatal diabetes mellitus",
    )
    add_known_variant(other, "7", 44145200, "C", "T", "LP")  # in another release only
    absent = VariantKey.get_or_create("7", 44145200, "C", "T").id
    result = classify_keys(release, iter([known.variant_key_id, absent]))
    assert result == {
        known.variant_key_id: KnownClass(
            "P", "activating", known.id, True, "reviewed_by_expert_panel", 3, "Permanent neonatal diabetes mellitus"
        ),
        absent: NOT_CLASSIFIED,
    }
    assert result[absent].classification == "NC"  # "not classified", never VUS


def test_classify_keys_with_no_keys_issues_no_query(db: SQLAlchemy) -> None:
    release = make_release()
    with count_queries(db) as statements:
        assert classify_keys(release, []) == {}
    assert statements == []


def test_classify_keys_is_one_bulk_query_per_chunk_for_large_key_sets(db: SQLAlchemy) -> None:
    release = make_release()
    count = IN_CLAUSE_CHUNK * 2 + 300
    db.session.execute(
        insert(VariantKey),
        [{"chrom": "12", "pos": 120_970_000 + i, "ref": "A", "alt": "G", "gene": "HNF1A"} for i in range(count)],
    )
    key_ids = list(db.session.scalars(VariantKey.__table__.select().with_only_columns(VariantKey.id)))
    classified_ids = key_ids[::2]
    db.session.execute(
        insert(KnownVariant),
        [
            {"release_id": release.id, "variant_key_id": key_id, "gene": "HNF1A", "classification": "VUS",
             "mechanism": "unknown", "classification_source": "clinvar", "review_stars": 1, "is_expert_panel": False}
            for key_id in classified_ids
        ],
    )
    with count_queries(db) as statements:
        result = classify_keys(release, key_ids + key_ids)  # duplicates are harmless
    assert len(statements) == 3  # ceil(2,100 / 900) chunked IN queries, never one per key
    assert len(result) == count
    assert sum(known.classification == "VUS" for known in result.values()) == len(classified_ids)
    assert sum(known is NOT_CLASSIFIED for known in result.values()) == count - len(classified_ids)


def test_chunk_size_respects_old_sqlite_variable_limit() -> None:
    assert IN_CLAUSE_CHUNK < 999


# ---------------------------------------------------------------------------
# classified_variants_for_patient
# ---------------------------------------------------------------------------


def test_classified_variants_for_patient(db: SQLAlchemy) -> None:
    release = make_release()
    patient, other_patient = make_patient(), make_patient()
    gck = add_patient_variant(patient, "7", 44145200, "C", "T")
    kcnj11 = add_patient_variant(patient, "11", 17387100, "C", "T", zygosity="homozygous")
    unknown = add_patient_variant(patient, "12", 120978900, "G", "A")
    add_patient_variant(other_patient, "20", 44401300, "C", "T")
    add_known_variant(release, "7", 44145200, "C", "T", "P", is_expert_panel=True, review_stars=3)
    add_known_variant(release, "11", 17387100, "C", "T", "LP", mechanism="activating")

    variants = classified_variants_for_patient(patient, release)
    assert variants == [
        ClassifiedVariant(gck.id, gck.variant_key_id, "GCK", "P", "unknown", "heterozygous", "7-44145200-C-T", True),
        ClassifiedVariant(
            kcnj11.id, kcnj11.variant_key_id, "KCNJ11", "LP", "activating", "homozygous", "11-17387100-C-T", False
        ),
        ClassifiedVariant(
            unknown.id, unknown.variant_key_id, "HNF1A", "NC", "unknown", "heterozygous", "12-120978900-G-A", False
        ),
    ]


def test_classified_variants_for_patient_restricted_to_keys_and_without_release(db: SQLAlchemy) -> None:
    release = make_release()
    patient = make_patient()
    first = add_patient_variant(patient, "7", 44145200, "C", "T")
    second = add_patient_variant(patient, "7", 44150500, "G", "A")
    add_known_variant(release, "7", 44150500, "G", "A", "LP")

    restricted = classified_variants_for_patient(patient, release, {second.variant_key_id, 424242})
    assert [(v.patient_variant_id, v.classification) for v in restricted] == [(second.id, "LP")]
    assert classified_variants_for_patient(patient, release, []) == []
    assert [v.classification for v in classified_variants_for_patient(patient, None)] == ["NC", "NC"]
    assert first.id < second.id


def test_classified_variants_for_patient_falls_back_to_the_key_gene(db: SQLAlchemy) -> None:
    patient = make_patient()
    variant = add_patient_variant(patient, "7", 44145200, "C", "T")
    variant.gene = None
    db.session.flush()
    assert classified_variants_for_patient(patient, None)[0].gene == "GCK"


def test_classified_variants_for_patient_uses_a_constant_number_of_queries(db: SQLAlchemy) -> None:
    release = make_release()
    patient = make_patient()
    for offset in range(40):
        add_patient_variant(patient, "12", 120_980_000 + offset, "C", "T")
        add_known_variant(release, "12", 120_980_000 + offset, "C", "T", "VUS")
    db.session.expire_all()
    assert patient.id and release.id  # reload both outside the counted block
    with count_queries(db) as statements:
        variants = classified_variants_for_patient(patient, release)
    assert len(variants) == 40 and {v.classification for v in variants} == {"VUS"}
    assert len(statements) == 2  # patient variants with keys, then one classification query


def test_module_exports_the_rules_classified_variant() -> None:
    assert classification.ClassifiedVariant is ClassifiedVariant
