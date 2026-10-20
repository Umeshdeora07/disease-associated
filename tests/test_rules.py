"""Tests for the pure rule engine (``services/rules.py``): Table III rules, Table V scenarios
S1–S9 under Algorithm 1, clinical conditions, descriptions and strict rule-set validation."""

from __future__ import annotations

import copy
import json
import random
from pathlib import Path
from typing import Any

import pytest

from ge_ehr.constants import GENES
from ge_ehr.services.rules import (
    RULES_DATA_DIR,
    ClassifiedVariant,
    ClinicalContext,
    Finding,
    RuleDefinitionError,
    RuleSpec,
    clinical_condition_holds,
    describe_clinical_condition,
    describe_genetic_criterion,
    evaluate,
    format_age_months,
    load_rule_set_file,
    parse_rule_set_json,
    rule_code_sort_key,
    validate_rule_set,
)
from tests.factories import rule_definition


def _specs(name: str) -> list[RuleSpec]:
    doc = load_rule_set_file(RULES_DATA_DIR / name)
    return [RuleSpec.from_definition(defn, rule_version_id=index) for index, defn in enumerate(doc["rules"], 1)]


RULES_V1 = _specs("ruleset_v1.json")
RULES_V2 = _specs("ruleset_v2.json")
RULE_V1 = {rule.code: rule for rule in RULES_V1}
RULE_V2 = {rule.code: rule for rule in RULES_V2}

D2_NOTE_ON_SULFONYLUREA = (
    "Genetic criterion of D2 met (HNF1A pathogenic) but therapy condition not met: "
    "already receiving a sulfonylurea. No alert raised."
)


def variant(
    gene: str, classification: str, key: int = 1, *, mechanism: str = "unknown", patient_variant_id: int | None = None
) -> ClassifiedVariant:
    return ClassifiedVariant(
        patient_variant_id=patient_variant_id if patient_variant_id is not None else key * 10,
        variant_key_id=key,
        gene=gene,
        classification=classification,
        mechanism=mechanism,
        zygosity="heterozygous",
        label=f"label-{key}",
    )


def context(*drug_classes: str, age: int | None = None, diabetes_type: str | None = "type2") -> ClinicalContext:
    return ClinicalContext(frozenset(drug_classes), age, diabetes_type)


def summary(findings: list[Finding]) -> list[tuple[str, int, str, str | None]]:
    return [(finding.rule_code, finding.variant_key_id, finding.kind, finding.severity) for finding in findings]


# ---------------------------------------------------------------------------
# Table III — every rule
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("drug_class", ["insulin", "metformin", "sulfonylurea", "other"])
def test_d1_gck_on_any_glucose_lowering_drug(drug_class: str) -> None:
    findings = evaluate([variant("GCK", "LP")], context(drug_class), [RULE_V1["D1"]])
    assert summary(findings) == [("D1", 1, "alert", "medium")]
    assert findings[0].message == (
        "Medication usually not required outside pregnancy; review whether glucose-lowering treatment can be stopped."
    )


@pytest.mark.parametrize(("gene", "code", "severity"), [("HNF1A", "D2", "high"), ("HNF4A", "D3", "medium")])
@pytest.mark.parametrize("drug_class", ["insulin", "metformin"])
@pytest.mark.parametrize("classification", ["P", "LP"])
def test_d2_d3_on_insulin_or_metformin(gene: str, code: str, severity: str, drug_class: str, classification: str) -> None:
    findings = evaluate([variant(gene, classification)], context(drug_class), [RULE_V1[code]])
    assert summary(findings) == [(code, 1, "alert", severity)]
    assert findings[0].message == RULE_V1[code].recommendation
    assert findings[0].source == RULE_V1[code].source


def test_d4_activating_kcnj11_diagnosed_in_infancy_on_insulin() -> None:
    findings = evaluate(
        [variant("KCNJ11", "LP", mechanism="activating")], context("insulin", age=2, diabetes_type="neonatal"), RULES_V1
    )
    assert summary(findings) == [("D4", 1, "alert", "high"), ("D5", 1, "alert", "low")]
    assert findings[0].message == "Refer to specialist to consider transfer to high-dose sulfonylurea."


@pytest.mark.parametrize("gene", GENES)
@pytest.mark.parametrize("classification", ["P", "LP"])
def test_d5_cascade_testing_for_every_panel_gene(gene: str, classification: str) -> None:
    findings = evaluate([variant(gene, classification)], context(), [RULE_V1["D5"]])
    assert summary(findings) == [("D5", 1, "alert", "low")]
    assert "cascade testing of first-degree relatives" in findings[0].message


@pytest.mark.parametrize("gene", ["GCK", "HNF1A", "HNF4A"])
def test_d6_vus_monitoring(gene: str) -> None:
    findings = evaluate([variant(gene, "VUS")], context(), RULES_V1)
    assert summary(findings) == [("D6", 1, "alert", "info")]
    assert findings[0].message == "No treatment change; variant flagged and monitored for reclassification."


@pytest.mark.parametrize("classification", ["LB", "B", "NC"])
def test_non_actionable_non_vus_classes_produce_nothing(classification: str) -> None:
    variants = [variant(gene, classification, key) for key, gene in enumerate(GENES, 1)]
    assert evaluate(variants, context("insulin", age=2), RULES_V2) == []


# ---------------------------------------------------------------------------
# Table V — scenarios S1–S9 (and the pure part of RS5)
# ---------------------------------------------------------------------------

SCENARIOS = {
    "S1 HNF1A pathogenic, on insulin": (
        [variant("HNF1A", "P", 1), variant("HNF1A", "B", 2)],
        context("insulin", age=180, diabetes_type="type1"),
        [("D2", 1, "alert", "high"), ("D5", 1, "alert", "low")],
    ),
    "S2 HNF1A pathogenic, already on sulfonylurea": (
        [variant("HNF1A", "P", 1)],
        context("sulfonylurea", age=300),
        [("D2", 1, "note", None), ("D5", 1, "alert", "low")],
    ),
    "S3 GCK pathogenic, on metformin": (
        [variant("GCK", "P", 1)],
        context("metformin", age=360),
        [("D1", 1, "alert", "medium"), ("D5", 1, "alert", "low")],
    ),
    "S4 GCK pathogenic, no medication": (
        [variant("GCK", "P", 1), variant("GCK", "LB", 2)],
        context(age=240, diabetes_type="other"),
        [("D1", 1, "note", None), ("D5", 1, "alert", "low")],
    ),
    "S5 HNF4A likely pathogenic, on metformin": (
        [variant("HNF4A", "LP", 1)],
        context("metformin", age=264),
        [("D3", 1, "alert", "medium"), ("D5", 1, "alert", "low")],
    ),
    "S6 KCNJ11 pathogenic, diagnosed in infancy, on insulin": (
        [variant("KCNJ11", "P", 1, mechanism="activating")],
        context("insulin", age=2, diabetes_type="neonatal"),
        [("D4", 1, "alert", "high"), ("D5", 1, "alert", "low")],
    ),
    "S7 any actionable variant (INS)": (
        [variant("INS", "LP", 1)],
        context("insulin", age=96, diabetes_type="type1"),
        [("D5", 1, "alert", "low")],
    ),
    "S8 HNF1A VUS": (
        [variant("HNF1A", "VUS", 1), variant("HNF1B", "B", 2)],
        context("insulin", age=192, diabetes_type="type1"),
        [("D6", 1, "alert", "info")],
    ),
    "S9 only benign variants": (
        [variant("HNF1A", "B", 1), variant("GCK", "LB", 2), variant("ABCC8", "LB", 3)],
        context("metformin", age=480),
        [],
    ),
}


@pytest.mark.parametrize("rules", [RULES_V1, RULES_V2], ids=["rules-v1", "rules-v2"])
@pytest.mark.parametrize("scenario", list(SCENARIOS))
def test_table_v_scenarios(scenario: str, rules: list[RuleSpec]) -> None:
    variants, ctx, expected = SCENARIOS[scenario]
    assert summary(evaluate(variants, ctx, rules)) == expected


def test_s2_note_explains_the_failed_therapy_condition() -> None:
    variants, ctx, _ = SCENARIOS["S2 HNF1A pathogenic, already on sulfonylurea"]
    note = evaluate(variants, ctx, RULES_V1)[0]
    assert note.message == D2_NOTE_ON_SULFONYLUREA
    assert (note.title, note.source, note.classification) == (RULE_V1["D2"].title, RULE_V1["D2"].source, "P")


def test_s4_note_says_no_medication() -> None:
    variants, ctx, _ = SCENARIOS["S4 GCK pathogenic, no medication"]
    assert evaluate(variants, ctx, RULES_V1)[0].message == (
        "Genetic criterion of D1 met (GCK pathogenic) but therapy condition not met: "
        "no current glucose-lowering medication. No alert raised."
    )


def test_rs5_abcc8_is_covered_by_d4_v2_only() -> None:
    variants = [variant("ABCC8", "P", 1, mechanism="activating")]
    ctx = context("insulin", age=3, diabetes_type="neonatal")
    assert summary(evaluate(variants, ctx, RULES_V1)) == [("D5", 1, "alert", "low")]
    assert summary(evaluate(variants, ctx, RULES_V2)) == [("D4", 1, "alert", "high"), ("D5", 1, "alert", "low")]


# ---------------------------------------------------------------------------
# Boundary cases
# ---------------------------------------------------------------------------


def test_d4_age_exactly_twelve_months_is_not_infancy() -> None:
    kcnj11 = [variant("KCNJ11", "P", mechanism="activating")]
    assert summary(evaluate(kcnj11, context("insulin", age=11), [RULE_V1["D4"]])) == [("D4", 1, "alert", "high")]
    note = evaluate(kcnj11, context("insulin", age=12), [RULE_V1["D4"]])[0]
    assert note.kind == "note" and note.severity is None
    assert note.message == (
        "Genetic criterion of D4 met (KCNJ11 pathogenic, activating) but therapy condition not met: "
        "diagnosed at 12 months (rule requires diagnosis before 12 months). No alert raised."
    )


def test_d4_unknown_age_at_diagnosis_gives_a_note() -> None:
    note = evaluate([variant("KCNJ11", "LP", mechanism="activating")], context("insulin"), [RULE_V1["D4"]])[0]
    assert note.kind == "note"
    assert note.message.endswith("therapy condition not met: age at diagnosis unknown. No alert raised.")


def test_d4_adult_diagnosis_and_no_insulin_lists_both_failures() -> None:
    note = evaluate([variant("KCNJ11", "LP", mechanism="activating")], context("metformin", age=360), [RULE_V1["D4"]])[0]
    assert note.message == (
        "Genetic criterion of D4 met (KCNJ11 likely pathogenic, activating) but therapy condition not met: "
        "not receiving insulin; diagnosed at 30 years (rule requires diagnosis before 12 months). No alert raised."
    )


@pytest.mark.parametrize("mechanism", ["unknown", "inactivating"])
def test_kcnj11_without_activating_mechanism_never_meets_d4(mechanism: str) -> None:
    findings = evaluate([variant("KCNJ11", "P", mechanism=mechanism)], context("insulin", age=2), RULES_V2)
    assert summary(findings) == [("D5", 1, "alert", "low")], "no D4 alert and no D4 note"


def test_multiple_variants_in_one_gene_give_one_finding_per_variant() -> None:
    variants = [variant("HNF1A", "LP", 7), variant("HNF1A", "P", 3), variant("HNF1A", "VUS", 5)]
    findings = evaluate(variants, context("insulin"), RULES_V1)
    assert summary(findings) == [
        ("D2", 3, "alert", "high"),
        ("D2", 7, "alert", "high"),
        ("D5", 3, "alert", "low"),
        ("D5", 7, "alert", "low"),
        ("D6", 5, "alert", "info"),
    ]
    assert [finding.patient_variant_id for finding in findings] == [30, 70, 30, 70, 50]
    assert [finding.classification for finding in findings] == ["P", "LP", "P", "LP", "VUS"]


def test_vus_in_hnf1b_is_not_monitored_by_d6() -> None:
    assert evaluate([variant("HNF1B", "VUS")], context("insulin"), RULES_V2) == []


def test_d2_patient_on_insulin_and_sulfonylurea_gets_a_note() -> None:
    note = evaluate([variant("HNF1A", "P")], context("insulin", "sulfonylurea"), [RULE_V1["D2"]])[0]
    assert note.message == D2_NOTE_ON_SULFONYLUREA


def test_other_drug_class_only() -> None:
    variants = [variant("GCK", "P", 1), variant("HNF1A", "LP", 2)]
    findings = evaluate(variants, context("other"), RULES_V1)
    assert summary(findings) == [
        ("D1", 1, "alert", "medium"),
        ("D2", 2, "note", None),
        ("D5", 1, "alert", "low"),
        ("D5", 2, "alert", "low"),
    ]
    assert "therapy condition not met: not receiving insulin or metformin." in findings[1].message


def test_findings_carry_rule_and_variant_provenance() -> None:
    finding = evaluate([variant("HNF1A", "LP", 4, patient_variant_id=99)], context("insulin"), [RULE_V1["D2"]])[0]
    assert finding == Finding(
        rule_code="D2",
        rule_version_id=RULE_V1["D2"].rule_version_id,
        variant_key_id=4,
        patient_variant_id=99,
        kind="alert",
        severity="high",
        title="HNF1A diabetes on insulin or metformin",
        message="Consider low-dose sulfonylurea as first-line treatment.",
        source=RULE_V1["D2"].source,
        classification="LP",
    )
    assert finding.key == ("D2", 4, "alert", "high")


def test_evaluation_order_is_deterministic() -> None:
    variants = [variant(gene, "P", key) for key, gene in enumerate(["INS", "GCK", "HNF1A", "HNF4A"], 1)]
    expected = summary(evaluate(variants, context("insulin"), RULES_V1))
    shuffled_rules, shuffled_variants = RULES_V1[:], variants[:]
    for seed in range(5):
        random.Random(seed).shuffle(shuffled_rules)
        random.Random(seed + 100).shuffle(shuffled_variants)
        assert summary(evaluate(shuffled_variants, context("insulin"), shuffled_rules)) == expected
    assert [code for code, *_ in expected] == ["D1", "D2", "D3", "D5", "D5", "D5", "D5"]


def test_rule_codes_are_ordered_naturally() -> None:
    d10 = RuleSpec.from_definition(rule_definition("D10", genes=["INS"], severity="low"), rule_version_id=10)
    d9 = RuleSpec.from_definition(rule_definition("D9", genes=["INS"], severity="low"), rule_version_id=9)
    findings = evaluate([variant("INS", "P")], context(), [d10, RULE_V1["D5"], d9])
    assert [finding.rule_code for finding in findings] == ["D5", "D9", "D10"]
    assert sorted(["D10", "X1", "D2", "D1"], key=rule_code_sort_key) == ["D1", "D2", "D10", "X1"]


def test_evaluate_with_no_variants_or_no_rules() -> None:
    assert evaluate([], context("insulin"), RULES_V1) == []
    assert evaluate([variant("HNF1A", "P")], context("insulin"), []) == []


def test_evaluate_rejects_two_versions_of_one_rule() -> None:
    with pytest.raises(RuleDefinitionError, match=r"Cannot evaluate D4 v1 and D4 v2 together; pass one version per rule code\."):
        evaluate([variant("KCNJ11", "P", mechanism="activating")], context("insulin"), [RULE_V2["D4"], RULE_V1["D4"]])


def test_evaluate_rejects_duplicate_variant_keys() -> None:
    with pytest.raises(ValueError, match=r"Variant key 3 is listed more than once\."):
        evaluate([variant("HNF1A", "P", 3), variant("HNF1A", "LP", 3)], context("insulin"), RULES_V1)


# ---------------------------------------------------------------------------
# Clinical conditions
# ---------------------------------------------------------------------------


D2_CLINICAL = {"any_of_drug_classes": ["insulin", "metformin"], "none_of_drug_classes": ["sulfonylurea"]}
D4_CLINICAL = {"any_of_drug_classes": ["insulin"], "max_age_at_diagnosis_months": 12}


@pytest.mark.parametrize(
    ("clinical", "ctx", "expected"),
    [
        (None, context(), (True, "no therapy condition")),
        ({}, context("insulin"), (True, "no therapy condition")),
        ({"any_of_drug_classes": None, "max_age_at_diagnosis_months": None}, context(), (True, "no therapy condition")),
        (D2_CLINICAL, context("insulin"), (True, "receiving insulin; not receiving a sulfonylurea")),
        (D2_CLINICAL, context("metformin", "insulin", "other"), (True, "receiving insulin and metformin; not receiving a sulfonylurea")),
        (D2_CLINICAL, context("sulfonylurea"), (False, "already receiving a sulfonylurea")),
        (D2_CLINICAL, context("insulin", "sulfonylurea"), (False, "already receiving a sulfonylurea")),
        (D2_CLINICAL, context(), (False, "no current glucose-lowering medication")),
        (D2_CLINICAL, context("other"), (False, "not receiving insulin or metformin")),
        (D4_CLINICAL, context("insulin", age=0), (True, "receiving insulin; diagnosed at 0 months (before 12 months)")),
        (D4_CLINICAL, context("insulin", age=1), (True, "receiving insulin; diagnosed at 1 month (before 12 months)")),
        (D4_CLINICAL, context("insulin", age=12), (False, "diagnosed at 12 months (rule requires diagnosis before 12 months)")),
        (D4_CLINICAL, context("insulin", age=360), (False, "diagnosed at 30 years (rule requires diagnosis before 12 months)")),
        (D4_CLINICAL, context("insulin"), (False, "age at diagnosis unknown")),
        (D4_CLINICAL, context("metformin", age=2), (False, "not receiving insulin")),
        (D4_CLINICAL, context(), (False, "no current glucose-lowering medication; age at diagnosis unknown")),
        (
            {"any_of_drug_classes": ["insulin", "metformin", "sulfonylurea"]},
            context("other"),
            (False, "not receiving insulin, metformin or a sulfonylurea"),
        ),
        (
            {"none_of_drug_classes": ["insulin", "other"]},
            context("other", "insulin", "metformin"),
            (False, "already receiving insulin and another glucose-lowering drug"),
        ),
        (
            {"none_of_drug_classes": ["sulfonylurea"], "max_age_at_diagnosis_months": 72},
            context("sulfonylurea", age=100),
            (False, "already receiving a sulfonylurea; diagnosed at 8 years 4 months (rule requires diagnosis before 6 years)"),
        ),
    ],
)
def test_clinical_condition_holds(clinical: dict[str, Any] | None, ctx: ClinicalContext, expected: tuple[bool, str]) -> None:
    assert clinical_condition_holds(clinical, ctx) == expected


def test_clinical_condition_holds_accepts_normalised_conditions() -> None:
    assert clinical_condition_holds(RULE_V1["D2"].clinical, context("insulin"))[0] is True
    assert clinical_condition_holds(RULE_V1["D2"].clinical, context("sulfonylurea"))[0] is False


def test_clinical_condition_holds_rejects_malformed_conditions() -> None:
    with pytest.raises(RuleDefinitionError, match=r"^Clinical condition: unknown field\(s\) in 'clinical': 'min_age'"):
        clinical_condition_holds({"min_age": 3}, context())
    with pytest.raises(RuleDefinitionError, match=r"^Clinical condition: 'clinical' must be an object or null, got list"):
        clinical_condition_holds(["insulin"], context())  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("clinical", "expected"),
    [
        (None, "No therapy condition"),
        ({}, "No therapy condition"),
        ({"any_of_drug_classes": None, "none_of_drug_classes": None, "max_age_at_diagnosis_months": None}, "No therapy condition"),
        (RULE_V1["D1"].clinical, "Any glucose-lowering medication"),
        (RULE_V1["D2"].clinical, "On insulin or metformin · not on a sulfonylurea"),
        (RULE_V1["D3"].clinical, "On insulin or metformin · not on a sulfonylurea"),
        (RULE_V1["D4"].clinical, "On insulin · diagnosed before 12 months of age"),
        (RULE_V1["D5"].clinical, "No therapy condition"),
        (RULE_V1["D6"].clinical, "No therapy condition"),
        ({"none_of_drug_classes": ["sulfonylurea"]}, "Not on a sulfonylurea"),
        ({"none_of_drug_classes": ["insulin", "metformin", "sulfonylurea", "other"]}, "Not on any glucose-lowering medication"),
        ({"max_age_at_diagnosis_months": 72}, "Diagnosed before 6 years of age"),
        ({"any_of_drug_classes": ["other", "sulfonylurea"]}, "On a sulfonylurea or another glucose-lowering drug"),
    ],
)
def test_describe_clinical_condition(clinical: dict[str, Any] | None, expected: str) -> None:
    assert describe_clinical_condition(clinical) == expected


@pytest.mark.parametrize(
    ("rule", "expected"),
    [
        (RULE_V1["D1"], "GCK · P/LP"),
        (RULE_V1["D2"], "HNF1A · P/LP"),
        (RULE_V1["D4"], "KCNJ11 · P/LP · activating"),
        (RULE_V2["D4"], "KCNJ11 or ABCC8 · P/LP · activating"),
        (RULE_V1["D5"], "Any of the 7 panel genes · P/LP"),
        (RULE_V1["D6"], "GCK, HNF1A or HNF4A · VUS"),
    ],
)
def test_describe_genetic_criterion(rule: RuleSpec, expected: str) -> None:
    assert describe_genetic_criterion(rule) == expected


@pytest.mark.parametrize(
    ("months", "expected"),
    [(0, "0 months"), (1, "1 month"), (12, "12 months"), (23, "23 months"), (24, "2 years"),
     (25, "2 years 1 month"), (30, "2 years 6 months"), (360, "30 years")],
)
def test_format_age_months(months: int, expected: str) -> None:
    assert format_age_months(months) == expected


# ---------------------------------------------------------------------------
# RuleSpec.from_definition
# ---------------------------------------------------------------------------


def test_from_definition_builds_a_normalised_spec() -> None:
    defn = rule_definition(
        "D4",
        2,
        genes=["ABCC8", "KCNJ11"],
        classes=["LP", "P"],
        mechanism="activating",
        clinical={"any_of_drug_classes": ["metformin", "insulin"], "none_of_drug_classes": None, "max_age_at_diagnosis_months": 12},
        title="Neonatal diabetes",
        source="Kang 2021",
    )
    spec = RuleSpec.from_definition(defn, rule_version_id=42)
    assert spec == RuleSpec(
        rule_version_id=42,
        code="D4",
        version=2,
        title="Neonatal diabetes",
        kind="treatment",
        genes=frozenset({"KCNJ11", "ABCC8"}),
        classes=frozenset({"P", "LP"}),
        mechanism="activating",
        clinical={"any_of_drug_classes": ("insulin", "metformin"), "max_age_at_diagnosis_months": 12},
        severity="high",
        recommendation=defn["recommendation"],
        source="Kang 2021",
    )
    assert spec.label == "D4 v2"
    with pytest.raises(TypeError):
        spec.clinical["max_age_at_diagnosis_months"] = 24  # type: ignore[index]


def test_from_definition_optional_fields() -> None:
    defn = rule_definition("D5", severity="low", kind="cascade")
    del defn["genetic"]["mechanism"], defn["change_note"]
    defn["clinical"] = {"any_of_drug_classes": None}
    spec = RuleSpec.from_definition(defn)
    assert (spec.mechanism, spec.clinical, spec.rule_version_id) == (None, None, None)


def _mutated(path: str, value: Any = None, *, delete: bool = False) -> dict[str, Any]:
    """A valid D2 v1 definition with one (dotted) field replaced or deleted."""
    defn = copy.deepcopy(rule_definition("D2", clinical=D2_CLINICAL))
    *parents, last = path.split(".")
    target = defn
    for name in parents:
        target = target[name]
    if delete:
        del target[last]
    else:
        target[last] = value
    return defn


INVALID_DEFINITIONS = [
    (_mutated("code", "X1"), "Rule definition: 'code' must be 'D' followed by digits (at most 8 characters, e.g. 'D2'), got 'X1'."),
    (_mutated("code", "d2"), "Rule definition: 'code' must be 'D' followed by digits"),
    (_mutated("code", "D"), "Rule definition: 'code' must be 'D' followed by digits"),
    (_mutated("code", "D2a"), "Rule definition: 'code' must be 'D' followed by digits"),
    (_mutated("code", 2), "Rule definition: 'code' must be 'D' followed by digits (at most 8 characters, e.g. 'D2'), got 2."),
    (_mutated("code", "D12345678"), "Rule definition: 'code' must be 'D' followed by digits (at most 8 characters"),
    (_mutated("version", 0), "Rule D2: 'version' must be a positive integer, got number 0."),
    (_mutated("version", -1), "Rule D2: 'version' must be a positive integer, got number -1."),
    (_mutated("version", "1"), "Rule D2: 'version' must be a positive integer, got string '1'."),
    (_mutated("version", 1.5), "Rule D2: 'version' must be a positive integer, got number 1.5."),
    (_mutated("version", True), "Rule D2: 'version' must be a positive integer, got boolean True."),
    (
        _mutated("priority", "high"),
        "Rule D2 v1: unknown field(s) in the rule: 'priority'; allowed: code, version, kind, title, genetic, clinical, "
        "severity, recommendation, source, change_note.",
    ),
    (_mutated("severity", delete=True), "Rule D2 v1: missing required field(s) 'severity'."),
    (_mutated("code", delete=True), "Rule definition: missing required field(s) 'code'."),
    (_mutated("clinical", delete=True), "Rule D2 v1: missing required field(s) 'clinical' (use null for no therapy condition)."),
    (_mutated("kind", "diagnostic"), "Rule D2 v1: 'kind' must be one of treatment, cascade, monitoring; got 'diagnostic'."),
    (_mutated("title", ""), "Rule D2 v1: 'title' must be a non-empty string, got string ''."),
    (_mutated("title", "   "), "Rule D2 v1: 'title' must be a non-empty string, got string '   '."),
    (_mutated("title", None), "Rule D2 v1: 'title' must be a non-empty string, got null None."),
    (_mutated("title", "x" * 256), "Rule D2 v1: 'title' must be at most 255 characters (got 256)."),
    (_mutated("title", b"D2"), "Rule D2 v1: 'title' must be a non-empty string, got bytes b'D2'."),
    (_mutated("recommendation", ""), "Rule D2 v1: 'recommendation' must be a non-empty string"),
    (_mutated("source", 7), "Rule D2 v1: 'source' must be a non-empty string, got number 7."),
    (_mutated("genetic", ["HNF1A"]), "Rule D2 v1: 'genetic' must be an object, got list ['HNF1A']."),
    (_mutated("genetic.gene", ["HNF1A"]), "Rule D2 v1: unknown field(s) in 'genetic': 'gene'; allowed: genes, classes, mechanism."),
    (_mutated("genetic.classes", delete=True), "Rule D2 v1: missing required field(s) in 'genetic': 'classes'."),
    (_mutated("genetic.genes", []), "Rule D2 v1: 'genetic.genes' must be a non-empty list of genes, got list []."),
    (_mutated("genetic.genes", "HNF1A"), "Rule D2 v1: 'genetic.genes' must be a non-empty list of genes, got string 'HNF1A'."),
    (
        _mutated("genetic.genes", ["HNF1A", "HNF1C"]),
        "Rule D2 v1: 'genetic.genes' contains unknown gene 'HNF1C'; allowed: GCK, HNF1A, HNF4A, HNF1B, KCNJ11, ABCC8, INS.",
    ),
    (_mutated("genetic.genes", ["MODY1", 3]), "Rule D2 v1: 'genetic.genes' contains unknown genes 'MODY1', 3;"),
    (_mutated("genetic.genes", ["HNF1A", "GCK", "HNF1A"]), "Rule D2 v1: 'genetic.genes' lists 'HNF1A' more than once."),
    (
        _mutated("genetic.classes", ["Pathogenic"]),
        "Rule D2 v1: 'genetic.classes' contains unknown class 'Pathogenic'; allowed: P, LP, VUS, LB, B, NC.",
    ),
    (
        _mutated("genetic.mechanism", "gain-of-function"),
        "Rule D2 v1: 'genetic.mechanism' must be null or one of activating, inactivating, unknown; got 'gain-of-function'.",
    ),
    (_mutated("clinical", ["insulin"]), "Rule D2 v1: 'clinical' must be an object or null, got list ['insulin']."),
    (
        _mutated("clinical.min_age", 3),
        "Rule D2 v1: unknown field(s) in 'clinical': 'min_age'; allowed: any_of_drug_classes, none_of_drug_classes, "
        "max_age_at_diagnosis_months.",
    ),
    (
        _mutated("clinical.any_of_drug_classes", ["insulin", "statin"]),
        "Rule D2 v1: 'clinical.any_of_drug_classes' contains unknown drug class 'statin'; allowed: insulin, metformin, "
        "sulfonylurea, other.",
    ),
    (
        _mutated("clinical.none_of_drug_classes", []),
        "Rule D2 v1: 'clinical.none_of_drug_classes' must be null or a non-empty list of drug classes, got list [].",
    ),
    (
        _mutated("clinical.none_of_drug_classes", ["metformin", "insulin"]),
        "Rule D2 v1: drug classes 'insulin', 'metformin' cannot be in both 'clinical.any_of_drug_classes' and "
        "'clinical.none_of_drug_classes'.",
    ),
    (
        _mutated("clinical.none_of_drug_classes", ["insulin"]),
        "Rule D2 v1: drug class 'insulin' cannot be in both",
    ),
    (
        _mutated("clinical.max_age_at_diagnosis_months", 0),
        "Rule D2 v1: 'clinical.max_age_at_diagnosis_months' must be null or a positive integer number of months, got number 0.",
    ),
    (_mutated("clinical.max_age_at_diagnosis_months", "12"), "got string '12'."),
    (_mutated("clinical.max_age_at_diagnosis_months", 12.5), "got number 12.5."),
    (_mutated("clinical.max_age_at_diagnosis_months", True), "got boolean True."),
    (_mutated("severity", "urgent"), "Rule D2 v1: 'severity' must be one of high, medium, low, info; got 'urgent'."),
    (_mutated("severity", None), "Rule D2 v1: 'severity' must be one of high, medium, low, info; got None."),
    (_mutated("change_note", 5), "Rule D2 v1: 'change_note' must be a string or null, got number 5."),
]


@pytest.mark.parametrize(("defn", "message"), INVALID_DEFINITIONS)
def test_from_definition_rejects_invalid_definitions(defn: dict[str, Any], message: str) -> None:
    with pytest.raises(RuleDefinitionError) as excinfo:
        RuleSpec.from_definition(defn)
    assert str(excinfo.value).startswith(message) or message in str(excinfo.value), str(excinfo.value)


def test_from_definition_rejects_non_objects() -> None:
    with pytest.raises(RuleDefinitionError, match=r"^Rule definition: must be an object, got list\.$"):
        RuleSpec.from_definition(["D2"])  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# validate_rule_set / parse_rule_set_json / load_rule_set_file
# ---------------------------------------------------------------------------


def _doc(*rules: dict[str, Any], **meta: Any) -> dict[str, Any]:
    return {"rule_set": "Test rules", "label": "t1", **meta, "rules": list(rules)}


def test_validate_rule_set_returns_copies_of_the_definitions() -> None:
    doc = _doc(rule_definition("D2"), rule_definition("D4", 1, genes=["KCNJ11"]), rule_definition("D4", 2, genes=["KCNJ11"]))
    definitions = validate_rule_set(doc)
    assert definitions == doc["rules"]
    definitions[0]["genetic"]["genes"].append("GCK")
    assert doc["rules"][0]["genetic"]["genes"] == ["HNF1A"], "the caller's document is not aliased"


def test_validate_rule_set_metadata_is_optional() -> None:
    assert len(validate_rule_set({"rules": [rule_definition("D2")]})) == 1


@pytest.mark.parametrize(
    ("doc", "message"),
    [
        ([rule_definition("D2")], "Rule set must be an object with a 'rules' list, got list."),
        ({"rule_set": "x"}, "Rule set: 'rules' must be a non-empty list of rule definitions, got null."),
        (_doc(), "Rule set: 'rules' must be a non-empty list of rule definitions, got list."),
        ({"rules": {"D2": rule_definition("D2")}}, "Rule set: 'rules' must be a non-empty list of rule definitions, got object."),
        (_doc(rule_definition("D2"), version=2), "Rule set: unknown field(s) in the document: 'version'; allowed: rule_set, label, rules."),
        (_doc(rule_definition("D2"), label=""), "Rule set: 'label' must be a non-empty string when given, got ''."),
        (_doc(rule_definition("D2"), rule_set=5), "Rule set: 'rule_set' must be a non-empty string when given, got 5."),
        (_doc(rule_definition("D2"), label="x" * 129), "Rule set: 'label' must be at most 128 characters (got 129)."),
        (_doc(rule_definition("D2"), rule_definition("Q3")), "Rule #2: 'code' must be 'D' followed by digits"),
        (_doc(rule_definition("D2"), "D3"), "Rule #2: must be an object, got string."),
        (
            _doc(rule_definition("D2"), rule_definition("D3"), rule_definition("D2", recommendation="Other text.")),
            "Rule set lists D2 v1 more than once (rules #1 and #3).",
        ),
        (_doc(rule_definition("D2", severity="severe")), "Rule D2 v1: 'severity' must be one of high, medium, low, info"),
    ],
)
def test_validate_rule_set_errors(doc: Any, message: str) -> None:
    with pytest.raises(RuleDefinitionError) as excinfo:
        validate_rule_set(doc)
    assert str(excinfo.value).startswith(message), str(excinfo.value)


def test_parse_rule_set_json() -> None:
    doc = _doc(rule_definition("D2"))
    text = json.dumps(doc)
    assert parse_rule_set_json(text) == doc
    assert parse_rule_set_json(text.encode()) == doc
    assert parse_rule_set_json(b"\xef\xbb\xbf" + text.encode()) == doc, "a UTF-8 byte-order mark is accepted"


@pytest.mark.parametrize(
    ("data", "message"),
    [
        ('{"rules": [', r"^Rule set is not valid JSON: Expecting value \(line 1, column 12\)\.$"),
        ("", r"^Rule set is not valid JSON: Expecting value \(line 1, column 1\)\.$"),
        (b"\xff\xfe{}", r"^Rule set file is not UTF-8 encoded text\.$"),
        ("[]", r"^Rule set must be an object with a 'rules' list, got list\.$"),
    ],
)
def test_parse_rule_set_json_errors(data: str | bytes, message: str) -> None:
    with pytest.raises(RuleDefinitionError, match=message):
        parse_rule_set_json(data)


def test_load_rule_set_file(tmp_path: Path) -> None:
    path = tmp_path / "rules.json"
    path.write_text(json.dumps(_doc(rule_definition("D2"))), encoding="utf-8")
    assert load_rule_set_file(path)["label"] == "t1"
    with pytest.raises(FileNotFoundError):
        load_rule_set_file(tmp_path / "missing.json")
