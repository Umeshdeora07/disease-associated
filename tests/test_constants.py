"""Tests for ge_ehr.constants."""

from __future__ import annotations

import itertools

import pytest

from ge_ehr import constants as C


def test_gene_info_matches_panel() -> None:
    assert tuple(C.GENE_INFO) == C.GENES
    for symbol, info in C.GENE_INFO.items():
        assert info.symbol == symbol
        assert info.start < info.end
        assert info.hgnc_id.startswith("HGNC:")
        assert info.transcript.startswith("NM_")
        assert info.refseq_chrom.startswith("NC_")


def test_panel_regions_do_not_overlap() -> None:
    for first, second in itertools.combinations(C.GENE_INFO.values(), 2):
        if first.chrom == second.chrom:
            assert first.end < second.start or second.end < first.start, (first.symbol, second.symbol)


@pytest.mark.parametrize(
    ("chrom", "pos", "gene"),
    [
        ("7", 44_145_200, "GCK"),
        ("chr7", 44_145_200, "GCK"),
        ("CHR12", 120_978_900, "HNF1A"),
        ("20", 44_401_300, "HNF4A"),
        ("17", 37_700_000, "HNF1B"),
        ("11", 17_387_100, "KCNJ11"),
        ("11", 17_390_999, "KCNJ11"),
        ("11", 17_391_000, "ABCC8"),
        ("11", 2_160_500, "INS"),
        ("7", 44_140_000, "GCK"),
        ("7", 44_200_000, "GCK"),
        ("7", 44_139_999, None),
        ("7", 44_200_001, None),
        ("1", 1_000_000, None),
        ("chrX", 44_145_200, None),
    ],
)
def test_gene_for_position(chrom: str, pos: int, gene: str | None) -> None:
    assert C.gene_for_position(chrom, pos) == gene


@pytest.mark.parametrize(
    ("raw", "normalised"),
    [("chr7", "7"), ("7", "7"), ("Chr11", "11"), ("chrM", "MT"), ("M", "MT"), ("MT", "MT"), ("x", "X"), (" chr12 ", "12")],
)
def test_normalize_chrom(raw: str, normalised: str) -> None:
    assert C.normalize_chrom(raw) == normalised


@pytest.mark.parametrize("raw", ["", "chr", "   "])
def test_normalize_chrom_rejects_empty_names(raw: str) -> None:
    with pytest.raises(ValueError, match="Invalid chromosome name"):
        C.normalize_chrom(raw)


def test_classes_bands_and_labels() -> None:
    assert set(C.CLASS_LABELS) == set(C.CLASSES) == set(C.CLASS_ORDER)
    assert C.ACTIONABLE == {"P", "LP"}
    assert [C.band(cls) for cls in C.CLASSES] == ["actionable"] * 2 + ["non_actionable"] * 4
    assert [C.is_actionable(cls) for cls in C.CLASSES] == [True, True, False, False, False, False]
    assert sorted(C.CLASSES, key=C.CLASS_ORDER.get, reverse=True) == list(C.CLASSES)


def test_band_rejects_unknown_class() -> None:
    with pytest.raises(ValueError, match="Unknown classification 'Pathogenic'"):
        C.band("Pathogenic")


@pytest.mark.parametrize(
    ("old", "new", "category"),
    [
        ("VUS", "LP", "upgrade"),
        ("NC", "P", "upgrade"),
        ("B", "LP", "upgrade"),
        ("LP", "VUS", "downgrade"),
        ("P", "NC", "downgrade"),
        ("LP", "P", "within_actionable"),
        ("P", "LP", "within_actionable"),
        ("VUS", "LB", "within_non_actionable"),
        ("NC", "VUS", "within_non_actionable"),
        ("B", "NC", "within_non_actionable"),
        ("P", "P", None),
        ("NC", "NC", None),
    ],
)
def test_categorize_change(old: str, new: str, category: str | None) -> None:
    assert C.categorize_change(old, new) == category


def test_categorize_change_covers_every_pair() -> None:
    for old, new in itertools.product(C.CLASSES, repeat=2):
        category = C.categorize_change(old, new)
        assert (category is None) == (old == new)
        assert category is None or category in C.CHANGE_CATEGORIES


def test_categorize_change_rejects_unknown_class() -> None:
    with pytest.raises(ValueError):
        C.categorize_change("P", "pathogenic")


def test_label_maps_cover_their_codes() -> None:
    assert set(C.DRUG_CLASS_LABELS) == set(C.DRUG_CLASSES)
    assert set(C.SEVERITY_ORDER) == set(C.SEVERITY_LABELS) == set(C.SEVERITIES)
    assert set(C.ROLE_LABELS) == set(C.ROLES)
    assert set(C.CLINICAL_ROLES) <= set(C.ROLES)
    assert set(C.DIABETES_TYPE_LABELS) == set(C.DIABETES_TYPES)
    assert set(C.TRIGGER_LABELS) == set(C.TRIGGERS)
    assert set(C.CHANGE_CATEGORY_LABELS) == set(C.CHANGE_CATEGORIES)
    assert set(C.MECHANISM_LABELS) == set(C.MECHANISMS)
    assert set(C.SEX_LABELS) == set(C.SEXES)
    assert set(C.ALERT_STATUS_LABELS) == set(C.ALERT_STATUSES)
    assert set(C.CURRENT_ALERT_STATUSES) < set(C.ALERT_STATUSES)
    assert set(C.SUPERSEDED_REASON_LABELS) == set(C.SUPERSEDED_REASONS)
    assert set(C.RUN_STRATEGY_LABELS) == set(C.RUN_STRATEGIES)


def test_loinc_codes() -> None:
    assert C.LOINC == "http://loinc.org"
    assert set(C.CLASS_TO_LOINC) == set(C.CLASSES) - {"NC"}
    assert C.CLASS_TO_LOINC["P"] == ("LA6668-3", "Pathogenic")
    assert set(C.ZYGOSITY_TO_LOINC) == set(C.ZYGOSITIES)
    assert C.ZYGOSITY_TO_LOINC["heterozygous"] == ("LA6706-1", "Heterozygous")


def test_synthetic_header_lines() -> None:
    assert C.SYNTHETIC_HEADER_LINES == (
        "##ge_ehr_synthetic=true",
        "##source=GE-EHR synthetic ClinVar-format release (NOT real ClinVar data)",
    )


@pytest.mark.parametrize(
    ("name", "drug_class"),
    [
        ("Insulin glargine", "insulin"),
        ("insulin isophane, human 70 UNT/ML [Humulin]", "insulin"),
        ("Lantus SoloStar", "insulin"),
        ("NovoRapid FlexPen", "insulin"),
        ("Tresiba", "insulin"),
        ("Metformin 500 mg", "metformin"),
        ("24 HR Metformin hydrochloride 500 MG Extended Release Oral Tablet", "metformin"),
        ("Glucophage XR", "metformin"),
        ("Janumet (sitagliptin/metformin)", "metformin"),
        ("Glipizide 5 MG Oral Tablet", "sulfonylurea"),
        ("Glyburide", "sulfonylurea"),
        ("Glibenclamide", "sulfonylurea"),
        ("Glimepiride", "sulfonylurea"),
        ("Amaryl", "sulfonylurea"),
        ("GLICLAZIDE 80 mg", "sulfonylurea"),
        ("Diamicron MR", "sulfonylurea"),
        ("Tolbutamide", "sulfonylurea"),
        ("Chlorpropamide", "sulfonylurea"),
        ("Glucovance (glyburide and metformin)", "sulfonylurea"),
        ("Liraglutide", "other"),
        ("Empagliflozin", "other"),
        ("Sitagliptin", "other"),
        ("Pioglitazone", "other"),
        ("", "other"),
    ],
)
def test_suggest_drug_class(name: str, drug_class: str) -> None:
    assert C.suggest_drug_class(name) == drug_class
