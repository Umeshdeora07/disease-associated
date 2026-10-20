"""Domain constants shared by every GE-EHR module (SPEC §2).

Gene panel (GRCh38), ACMG/AMP classification classes and bands, change categories, drug
classes, severities, roles, statuses, FHIR/LOINC codes, display labels and small pure helpers.
Owned by the Foundation work package.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# ---------------------------------------------------------------------------
# Gene panel
# ---------------------------------------------------------------------------

GENES: tuple[str, ...] = ("GCK", "HNF1A", "HNF4A", "HNF1B", "KCNJ11", "ABCC8", "INS")


@dataclass(frozen=True)
class GeneInfo:
    """One panel gene: padded GRCh38 region (1-based, inclusive) and reference sequences."""

    symbol: str
    hgnc_id: str
    chrom: str
    start: int
    end: int
    transcript: str
    refseq_chrom: str

    def contains(self, chrom: str, pos: int) -> bool:
        """Return True when ``chrom:pos`` (chrom already normalised) lies inside the region."""
        return chrom == self.chrom and self.start <= pos <= self.end


GENE_INFO: dict[str, GeneInfo] = {
    "GCK": GeneInfo("GCK", "HGNC:4195", "7", 44_140_000, 44_200_000, "NM_000162.5", "NC_000007.14"),
    "HNF1A": GeneInfo("HNF1A", "HGNC:11621", "12", 120_970_000, 121_010_000, "NM_000545.6", "NC_000012.12"),
    "HNF4A": GeneInfo("HNF4A", "HGNC:5024", "20", 44_350_000, 44_440_000, "NM_175914.4", "NC_000020.11"),
    "HNF1B": GeneInfo("HNF1B", "HGNC:11630", "17", 37_680_000, 37_750_000, "NM_000458.4", "NC_000017.11"),
    "KCNJ11": GeneInfo("KCNJ11", "HGNC:6257", "11", 17_380_000, 17_390_999, "NM_000525.4", "NC_000011.10"),
    "ABCC8": GeneInfo("ABCC8", "HGNC:59", "11", 17_391_000, 17_480_000, "NM_000352.6", "NC_000011.10"),
    "INS": GeneInfo("INS", "HGNC:6081", "11", 2_155_000, 2_163_500, "NM_000207.3", "NC_000011.10"),
}

# GRCh38 vs GRCh37 contig lengths used to reject GRCh37 VCFs.
GRCH38_CONTIG_LENGTHS: dict[str, int] = {
    "7": 159_345_973,
    "11": 135_086_622,
    "12": 133_275_309,
    "17": 83_257_441,
    "20": 64_444_167,
}
GRCH37_CONTIG_LENGTHS: dict[str, int] = {
    "7": 159_138_663,
    "11": 135_006_516,
    "12": 133_851_895,
    "17": 81_195_210,
    "20": 63_025_520,
}

_CHR_PREFIX = re.compile(r"^chr", re.IGNORECASE)


def normalize_chrom(chrom: str) -> str:
    """Normalise a chromosome name: drop a ``chr`` prefix, upper-case, ``M`` → ``MT``.

    >>> normalize_chrom("chr7"), normalize_chrom("chrM"), normalize_chrom("x")
    ('7', 'MT', 'X')
    """
    name = _CHR_PREFIX.sub("", str(chrom).strip()).upper()
    if not name:
        raise ValueError(f"Invalid chromosome name {chrom!r}.")
    return "MT" if name == "M" else name


def gene_for_position(chrom: str, pos: int) -> str | None:
    """Return the panel gene whose padded GRCh38 region contains ``chrom:pos``, else None.

    The chromosome name is normalised first, so ``chr11`` and ``11`` are equivalent.
    """
    name = normalize_chrom(chrom)
    for info in GENE_INFO.values():
        if info.contains(name, pos):
            return info.symbol
    return None


# ---------------------------------------------------------------------------
# Classification (ACMG/AMP five tiers + "not classified")
# ---------------------------------------------------------------------------

CLASSES: tuple[str, ...] = ("P", "LP", "VUS", "LB", "B", "NC")
CLASS_LABELS: dict[str, str] = {
    "P": "Pathogenic",
    "LP": "Likely pathogenic",
    "VUS": "Uncertain significance",
    "LB": "Likely benign",
    "B": "Benign",
    "NC": "Not classified",
}
CLASS_ORDER: dict[str, int] = {"P": 5, "LP": 4, "VUS": 3, "LB": 2, "B": 1, "NC": 0}
ACTIONABLE: frozenset[str] = frozenset({"P", "LP"})

BANDS: tuple[str, ...] = ("actionable", "non_actionable")


def _require_class(cls: str) -> str:
    if cls not in CLASS_ORDER:
        raise ValueError(f"Unknown classification {cls!r}; expected one of {', '.join(CLASSES)}.")
    return cls


def band(cls: str) -> str:
    """Return ``"actionable"`` for P/LP and ``"non_actionable"`` for VUS/LB/B/NC."""
    return "actionable" if _require_class(cls) in ACTIONABLE else "non_actionable"


def is_actionable(cls: str) -> bool:
    """True when the class is in the actionable band (pathogenic or likely pathogenic)."""
    return band(cls) == "actionable"


CHANGE_CATEGORIES: tuple[str, ...] = ("upgrade", "downgrade", "within_actionable", "within_non_actionable")
CHANGE_CATEGORY_LABELS: dict[str, str] = {
    "upgrade": "Upgrade into actionable band",
    "downgrade": "Downgrade out of actionable band",
    "within_actionable": "Within actionable band",
    "within_non_actionable": "Within non-actionable band",
}


def categorize_change(old_cls: str, new_cls: str) -> str | None:
    """Categorise a classification change (paper Table IV).

    Returns None when the class is unchanged; ``upgrade`` for non-actionable → actionable;
    ``downgrade`` for actionable → non-actionable; ``within_actionable`` for P ↔ LP;
    ``within_non_actionable`` for any change among VUS/LB/B/NC.
    """
    old_band, new_band = band(old_cls), band(new_cls)
    if old_cls == new_cls:
        return None
    if old_band != new_band:
        return "upgrade" if new_band == "actionable" else "downgrade"
    return "within_actionable" if new_band == "actionable" else "within_non_actionable"


# ---------------------------------------------------------------------------
# Variant mechanism, therapy, severity, roles
# ---------------------------------------------------------------------------

MECHANISMS: tuple[str, ...] = ("activating", "inactivating", "unknown")
MECHANISM_LABELS: dict[str, str] = {
    "activating": "Activating",
    "inactivating": "Inactivating",
    "unknown": "Unknown",
}

# All recorded drug classes are glucose-lowering (only diabetes therapy is recorded).
DRUG_CLASSES: tuple[str, ...] = ("insulin", "metformin", "sulfonylurea", "other")
DRUG_CLASS_LABELS: dict[str, str] = {
    "insulin": "Insulin",
    "metformin": "Metformin",
    "sulfonylurea": "Sulfonylurea",
    "other": "Other glucose-lowering drug",
}

SEVERITIES: tuple[str, ...] = ("high", "medium", "low", "info")
SEVERITY_ORDER: dict[str, int] = {"high": 3, "medium": 2, "low": 1, "info": 0}
SEVERITY_LABELS: dict[str, str] = {"high": "High", "medium": "Medium", "low": "Low", "info": "Info"}

ROLES: tuple[str, ...] = ("admin", "clinician", "lab_technician")
ROLE_LABELS: dict[str, str] = {
    "admin": "Administrator",
    "clinician": "Clinician",
    "lab_technician": "Lab technician",
}
# Roles allowed to see recommendations, alerts, notes and review notices (SPEC §8.1).
CLINICAL_ROLES: tuple[str, ...] = ("admin", "clinician")

# ---------------------------------------------------------------------------
# Alerts, findings, notices, re-evaluation runs
# ---------------------------------------------------------------------------

ALERT_STATUSES: tuple[str, ...] = ("active", "reviewed", "superseded")
CURRENT_ALERT_STATUSES: tuple[str, ...] = ("active", "reviewed")
ALERT_STATUS_LABELS: dict[str, str] = {"active": "Active", "reviewed": "Reviewed", "superseded": "Superseded"}
FINDING_KINDS: tuple[str, ...] = ("alert", "note")
PRIORITIES: tuple[str, ...] = ("normal", "high")
ALERT_EVENTS: tuple[str, ...] = ("created", "confirmed", "reviewed", "superseded")
SUPERSEDED_REASONS: tuple[str, ...] = (
    "downgrade",
    "reclassified",
    "therapy_changed",
    "diagnosis_changed",
    "rule_revised",
    "variant_removed",
)
SUPERSEDED_REASON_LABELS: dict[str, str] = {
    "downgrade": "Variant downgraded",
    "reclassified": "Variant reclassified",
    "therapy_changed": "Therapy changed",
    "diagnosis_changed": "Diagnosis changed",
    "rule_revised": "Rule revised",
    "variant_removed": "Variant removed",
}
NOTICE_STATUSES: tuple[str, ...] = ("open", "acknowledged")
RULE_KINDS: tuple[str, ...] = ("treatment", "cascade", "monitoring")

TRIGGERS: tuple[str, ...] = (
    "vcf_upload",
    "medication_change",
    "diagnosis_change",
    "clinvar_release",
    "rule_revision",
    "full_recompute",
    "manual",
)
TRIGGER_LABELS: dict[str, str] = {
    "vcf_upload": "VCF upload",
    "medication_change": "Medication change",
    "diagnosis_change": "Diagnosis change",
    "clinvar_release": "ClinVar release",
    "rule_revision": "Rule revision",
    "full_recompute": "Full recomputation",
    "manual": "Manual",
}
RUN_STRATEGIES: tuple[str, ...] = ("change_driven", "full", "patient")
RUN_STRATEGY_LABELS: dict[str, str] = {
    "change_driven": "Change-driven",
    "full": "Full recomputation",
    "patient": "Single patient",
}
RUN_STATUSES: tuple[str, ...] = ("running", "success", "failed")

# ---------------------------------------------------------------------------
# Patients, uploads, knowledge base
# ---------------------------------------------------------------------------

DIABETES_TYPES: tuple[str, ...] = (
    "type1",
    "type2",
    "monogenic",
    "neonatal",
    "gestational",
    "other",
    "none",
    "unknown",
)
DIABETES_TYPE_LABELS: dict[str, str] = {
    "type1": "Type 1",
    "type2": "Type 2",
    "monogenic": "Monogenic",
    "neonatal": "Neonatal",
    "gestational": "Gestational",
    "other": "Other",
    "none": "No diabetes",
    "unknown": "Unknown",
}
ZYGOSITIES: tuple[str, ...] = ("heterozygous", "homozygous", "hemizygous")
SEXES: tuple[str, ...] = ("female", "male", "other", "unknown")
SEX_LABELS: dict[str, str] = {"female": "Female", "male": "Male", "other": "Other", "unknown": "Unknown"}
UPLOAD_STATUSES: tuple[str, ...] = ("accepted", "rejected")
RELEASE_SOURCES: tuple[str, ...] = ("clinvar", "synthetic")
CLASSIFICATION_SOURCES: tuple[str, ...] = ("clinvar", "expert_panel_overlay")

# Mandatory header lines of every generated ClinVar-format file (SPEC §0).
SYNTHETIC_HEADER_LINES: tuple[str, ...] = (
    "##ge_ehr_synthetic=true",
    "##source=GE-EHR synthetic ClinVar-format release (NOT real ClinVar data)",
)

# User-facing disclaimers (SPEC §6.1, §8.3).
ADVISORY_DISCLAIMER = "Advisory — for specialist review"
SYNTHETIC_DATA_NOTICE = "Synthetic data only · Research prototype · Not for clinical use"

# ---------------------------------------------------------------------------
# FHIR / LOINC codes (SPEC §9)
# ---------------------------------------------------------------------------

LOINC = "http://loinc.org"
CLASS_TO_LOINC: dict[str, tuple[str, str]] = {
    "P": ("LA6668-3", "Pathogenic"),
    "LP": ("LA26332-9", "Likely pathogenic"),
    "VUS": ("LA26333-7", "Uncertain significance"),
    "LB": ("LA26334-5", "Likely benign"),
    "B": ("LA6675-8", "Benign"),
}
ZYGOSITY_TO_LOINC: dict[str, tuple[str, str]] = {
    "heterozygous": ("LA6706-1", "Heterozygous"),
    "homozygous": ("LA6705-3", "Homozygous"),
    "hemizygous": ("LA6707-9", "Hemizygous"),
}

# ---------------------------------------------------------------------------
# Medication name → drug class (SPEC §6.4 note)
# ---------------------------------------------------------------------------

# Checked in this order, so combination products containing a sulfonylurea count as
# sulfonylurea (rules D2/D3 must not recommend one to a patient already taking it) and
# insulin combinations count as insulin.
_DRUG_CLASS_KEYWORDS: tuple[tuple[str, tuple[str, ...]], ...] = (
    (
        "sulfonylurea",
        (
            "sulfonylurea", "sulphonylurea",
            "glipizide", "glucotrol",
            "glyburide", "glibenclamide", "diabeta", "glynase", "micronase", "daonil", "euglucon",
            "glimepiride", "amaryl",
            "gliclazide", "diamicron",
            "tolbutamide", "orinase",
            "chlorpropamide", "diabinese",
            "tolazamide", "acetohexamide",
            "glucovance", "metaglip",
        ),
    ),
    (
        "insulin",
        (
            "insulin",
            "glargine", "detemir", "degludec", "lispro", "aspart", "glulisine", "isophane",
            "lantus", "toujeo", "basaglar", "semglee", "levemir", "tresiba", "humalog",
            "admelog", "lyumjev", "novorapid", "novolog", "fiasp", "apidra", "humulin",
            "novolin", "actrapid", "insulatard", "xultophy", "soliqua",
        ),
    ),
    (
        "metformin",
        (
            "metformin", "glucophage", "glumetza", "fortamet", "riomet",
            "janumet", "jentadueto", "kombiglyze", "synjardy", "xigduo", "invokamet",
            "segluromet", "kazano", "actoplus met",
        ),
    ),
)
_DRUG_CLASS_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = tuple(
    (drug_class, re.compile(r"\b(?:" + "|".join(re.escape(word) for word in words) + r")", re.IGNORECASE))
    for drug_class, words in _DRUG_CLASS_KEYWORDS
)


def suggest_drug_class(drug_name: str) -> str:
    """Suggest the drug class of a (glucose-lowering) medication from its name.

    Matches common generic and brand names case-insensitively, e.g. ``"Lantus (insulin
    glargine)"`` → ``insulin``, ``"Gliclazide 80 mg"`` → ``sulfonylurea``,
    ``"24 HR Metformin hydrochloride 500 MG"`` → ``metformin``; anything else → ``other``.
    """
    name = drug_name or ""
    for drug_class, pattern in _DRUG_CLASS_PATTERNS:
        if pattern.search(name):
            return drug_class
    return "other"
