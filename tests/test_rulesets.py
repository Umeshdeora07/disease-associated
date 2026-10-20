"""Tests for the bundled rule sets ``data/rules/ruleset_v1.json`` / ``ruleset_v2.json`` (Table III)."""

from __future__ import annotations

import json
from typing import Any

import pytest

from ge_ehr.constants import GENES
from ge_ehr.services.rules import RULES_DATA_DIR, RuleSpec, load_rule_set_file, validate_rule_set

V1_PATH = RULES_DATA_DIR / "ruleset_v1.json"
V2_PATH = RULES_DATA_DIR / "ruleset_v2.json"

LOW_DOSE_SU = {"any_of_drug_classes": ["insulin", "metformin"], "none_of_drug_classes": ["sulfonylurea"], "max_age_at_diagnosis_months": None}
INFANCY_ON_INSULIN = {"any_of_drug_classes": ["insulin"], "none_of_drug_classes": None, "max_age_at_diagnosis_months": 12}

# Table III: code -> (kind, genes, classes, mechanism, clinical, severity, recommendation, cited authors)
TABLE_III: dict[str, tuple[Any, ...]] = {
    "D1": (
        "treatment", ["GCK"], ["P", "LP"], None,
        {"any_of_drug_classes": ["insulin", "metformin", "sulfonylurea", "other"], "none_of_drug_classes": None,
         "max_age_at_diagnosis_months": None},
        "medium",
        "Medication usually not required outside pregnancy; review whether glucose-lowering treatment can be stopped.",
        ("Kang E", "De Sousa SMC"),
    ),
    "D2": (
        "treatment", ["HNF1A"], ["P", "LP"], None, LOW_DOSE_SU, "high",
        "Consider low-dose sulfonylurea as first-line treatment.",
        ("Kang E", "Naylor RN"),
    ),
    "D3": (
        "treatment", ["HNF4A"], ["P", "LP"], None, LOW_DOSE_SU, "medium",
        "Consider low-dose sulfonylurea (supporting evidence is more limited than for HNF1A).",
        ("Kang E", "Naylor RN"),
    ),
    "D5": (
        "cascade", list(GENES), ["P", "LP"], None, None, "low",
        "Offer genetic counselling and cascade testing of first-degree relatives (50% risk, autosomal dominant inheritance).",
        ("Autosomal dominant inheritance",),
    ),
    "D6": (
        "monitoring", ["GCK", "HNF1A", "HNF4A"], ["VUS"], None, None, "info",
        "No treatment change; variant flagged and monitored for reclassification.",
        ("ClinGen Monogenic Diabetes Variant Curation Expert Panel", "Richards S", "ACMG"),
    ),
}
D4_RECOMMENDATION = "Refer to specialist to consider transfer to high-dose sulfonylurea."


def _load(path: Any) -> dict[str, Any]:
    return load_rule_set_file(path)


def _by_code(doc: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {rule["code"]: rule for rule in doc["rules"]}


@pytest.mark.parametrize("path", [V1_PATH, V2_PATH], ids=["v1", "v2"])
def test_rule_set_files_validate(path: Any) -> None:
    raw = path.read_text(encoding="utf-8")
    assert raw.endswith("\n")
    doc = json.loads(raw)
    assert validate_rule_set(doc) == doc["rules"]
    assert doc["rule_set"] == "GE-EHR monogenic diabetes rules"
    assert [rule["code"] for rule in doc["rules"]] == ["D1", "D2", "D3", "D4", "D5", "D6"]
    for rule in doc["rules"]:
        RuleSpec.from_definition(rule)
        assert rule["change_note"], f"{rule['code']} documents its version"


def test_rule_set_labels() -> None:
    assert _load(V1_PATH)["label"] == "v1 (2025)"
    assert _load(V2_PATH)["label"] == "v2 (2026)"


@pytest.mark.parametrize("path", [V1_PATH, V2_PATH], ids=["v1", "v2"])
@pytest.mark.parametrize("code", list(TABLE_III))
def test_rules_match_table_iii(path: Any, code: str) -> None:
    kind, genes, classes, mechanism, clinical, severity, recommendation, authors = TABLE_III[code]
    rule = _by_code(_load(path))[code]
    assert rule["version"] == 1
    assert rule["kind"] == kind
    assert rule["genetic"] == {"genes": genes, "classes": classes, "mechanism": mechanism}
    assert rule["clinical"] == clinical
    assert rule["severity"] == severity
    assert rule["recommendation"] == recommendation
    for author in authors:
        assert author in rule["source"], f"{code} cites {author}"


def test_d4_v1_covers_activating_kcnj11_only() -> None:
    d4 = _by_code(_load(V1_PATH))["D4"]
    assert (d4["version"], d4["kind"], d4["severity"]) == (1, "treatment", "high")
    assert d4["genetic"] == {"genes": ["KCNJ11"], "classes": ["P", "LP"], "mechanism": "activating"}
    assert d4["clinical"] == INFANCY_ON_INSULIN
    assert d4["recommendation"] == D4_RECOMMENDATION
    assert "Kang E" in d4["source"]


def test_d4_v2_adds_abcc8() -> None:
    d4_v1, d4_v2 = _by_code(_load(V1_PATH))["D4"], _by_code(_load(V2_PATH))["D4"]
    assert d4_v2["version"] == 2
    assert d4_v2["genetic"] == {"genes": ["KCNJ11", "ABCC8"], "classes": ["P", "LP"], "mechanism": "activating"}
    for field in ("kind", "clinical", "severity", "recommendation", "source"):
        assert d4_v2[field] == d4_v1[field], f"D4 v2 keeps the {field} of v1"
    assert "ABCC8" in d4_v2["change_note"] and "KCNJ11 only" in d4_v2["change_note"]


def test_v1_and_v2_differ_only_in_d4() -> None:
    v1, v2 = _load(V1_PATH), _load(V2_PATH)
    assert {key: value for key, value in v1.items() if key not in ("rules", "label")} == {
        key: value for key, value in v2.items() if key not in ("rules", "label")
    }
    rules_v1, rules_v2 = _by_code(v1), _by_code(v2)
    assert rules_v1.keys() == rules_v2.keys()
    differing = sorted(code for code in rules_v1 if rules_v1[code] != rules_v2[code])
    assert differing == ["D4"]


def test_sources_are_full_citations() -> None:
    sources = {code: rule["source"] for code, rule in _by_code(_load(V2_PATH)).items()}
    assert "doi:10.23838/pfm.2021.00100" in sources["D1"]  # [1] Kang et al. 2021
    assert "doi:10.1007/s00592-025-02449-8" in sources["D1"]  # [4] De Sousa et al. 2025
    assert "Commun Med. 2024;4:145" in sources["D2"] and "Commun Med. 2024;4:145" in sources["D3"]  # [7] Naylor
    assert "https://cspec.genome.network" in sources["D6"]  # [5] ClinGen MDEP
    assert "Genet Med. 2015;17(5):405-424" in sources["D6"]  # [8] ACMG/AMP
