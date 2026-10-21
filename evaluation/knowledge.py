"""Synthetic ClinVar-format knowledge bases and controlled change sets (SPEC §10, paper §5).

* :func:`generate_base_release` builds a deterministic synthetic base release K_old of ~9,000
  ClinVar-like records over the seven panel genes, with realistic per-gene sizes (ABCC8 largest,
  INS smallest), a ClinVar-like significance mix (including both ``Conflicting_*`` wordings,
  ``not_provided`` and ``risk_factor``), ClinGen expert-panel review on a subset of GCK, HNF1A and
  HNF4A records, and KCNJ11/ABCC8 conditions that mix neonatal diabetes (activating variants) with
  congenital hyperinsulinism (inactivating variants).
* :func:`apply_change_set` derives K_new from K_old with a *controlled* set of injected changes
  (:data:`CHANGE_KINDS`: upgrades, downgrades, within-actionable and within-non-actionable
  changes) on chosen keys, plus random changes and newly added records on keys carried by nobody.
  It returns the new records and the ground truth of every injected change.
* :func:`classify_records` normalises records exactly as GE-EHR's import does, without a
  database, so ground truth can be computed independently of the re-evaluation engine.
* :func:`load_real_pair` finds a real ClinVar release pair (``scripts/fetch_clinvar.py`` subsets
  in ``data/clinvar/real/``) when one has been downloaded.

Files are written with :func:`ge_ehr.services.clinvar.write_clinvar_vcf` (``synthetic=True``), so
every generated release carries the mandatory synthetic header lines (SPEC §0). Variation and
allele IDs start at 90,000,000 / 91,000,000, far outside the range of real ClinVar IDs.

Owned by the Eval work package.
"""

from __future__ import annotations

import logging
import os
import random
import re
from collections import Counter
from collections.abc import Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import date
from pathlib import Path
from typing import Any

from ge_ehr import constants as C
from ge_ehr.services import classification
from ge_ehr.services.clinvar import ClinVarRecord, iter_clinvar_records, parse_file_date, read_vcf_header_meta

logger = logging.getLogger(__name__)

Key = tuple[str, int, str, str]
"""A GRCh38 variant key ``(chrom, pos, ref, alt)`` as stored by GE-EHR."""

BASE_RELEASE_SIZE = 9_000

# Records per gene in a 9,000-record release: ABCC8 is by far the largest gene in ClinVar's
# monogenic-diabetes panel and INS the smallest. Other totals are allocated proportionally.
GENE_RECORD_SHARES: dict[str, int] = {
    "ABCC8": 2_900,
    "HNF1A": 1_600,
    "GCK": 1_450,
    "HNF1B": 1_050,
    "HNF4A": 900,
    "KCNJ11": 750,
    "INS": 350,
}

# Raw CLNSIG values and their share of records (ClinVar-like: VUS dominate, then benign classes).
SIGNIFICANCE_MIX: tuple[tuple[str, float], ...] = (
    ("Pathogenic", 0.085),
    ("Likely_pathogenic", 0.075),
    ("Pathogenic/Likely_pathogenic", 0.02),
    ("Uncertain_significance", 0.40),
    ("Conflicting_classifications_of_pathogenicity", 0.05),
    ("Conflicting_interpretations_of_pathogenicity", 0.02),
    ("Likely_benign", 0.21),
    ("Benign", 0.08),
    ("Benign/Likely_benign", 0.03),
    ("not_provided", 0.02),
    ("risk_factor", 0.01),
)

# ClinGen's Monogenic Diabetes Expert Panel curates GCK, HNF1A and HNF4A.
EXPERT_PANEL_GENES: frozenset[str] = frozenset({"GCK", "HNF1A", "HNF4A"})
EXPERT_PANEL_SHARE = 0.12
EXPERT_PANEL_STATUS = "reviewed_by_expert_panel"
MULTIPLE_SUBMITTERS_STATUS = "criteria_provided,_multiple_submitters,_no_conflicts"
CONFLICTING_STATUS = "criteria_provided,_conflicting_classifications"
_REVIEW_STATUS_MIX: tuple[tuple[str, float], ...] = (
    ("criteria_provided,_single_submitter", 0.55),
    (MULTIPLE_SUBMITTERS_STATUS, 0.30),
    ("no_assertion_criteria_provided", 0.15),
)

KATP_GENES: frozenset[str] = frozenset({"KCNJ11", "ABCC8"})
NEONATAL_DIABETES = "Permanent_neonatal_diabetes_mellitus"

# CLNDN values per gene (ClinVar style: "_" for spaces, "|" between conditions) with weights.
# For KCNJ11/ABCC8 the condition decides the mechanism (classification.infer_mechanism): neonatal
# diabetes and MODY → activating, familial hyperinsulinism → inactivating, both or none → unknown.
CONDITION_MIX: dict[str, tuple[tuple[str, float], ...]] = {
    "GCK": (
        ("Maturity-onset_diabetes_of_the_young_type_2", 0.60),
        ("Monogenic_diabetes", 0.15),
        ("not_provided", 0.15),
        ("Hyperinsulinemic_hypoglycemia,_familial,_3", 0.05),
        ("Permanent_neonatal_diabetes_mellitus|Maturity-onset_diabetes_of_the_young_type_2", 0.05),
    ),
    "HNF1A": (
        ("Maturity-onset_diabetes_of_the_young_type_3", 0.65),
        ("Monogenic_diabetes", 0.15),
        ("not_provided", 0.15),
        ("Hepatic_adenomas,_familial", 0.05),
    ),
    "HNF4A": (
        ("Maturity-onset_diabetes_of_the_young_type_1", 0.60),
        ("Monogenic_diabetes", 0.15),
        ("not_provided", 0.15),
        ("Fanconi_renotubular_syndrome_4,_with_maturity-onset_diabetes_of_the_young", 0.10),
    ),
    "HNF1B": (
        ("Renal_cysts_and_diabetes_syndrome", 0.55),
        ("Maturity-onset_diabetes_of_the_young_type_5", 0.20),
        ("not_provided", 0.25),
    ),
    "KCNJ11": (
        (NEONATAL_DIABETES, 0.30),
        ("Transient_neonatal_diabetes_mellitus_3", 0.10),
        ("Maturity-onset_diabetes_of_the_young_type_13", 0.05),
        ("Hyperinsulinemic_hypoglycemia,_familial,_2", 0.30),
        ("not_provided", 0.15),
        ("Hyperinsulinemic_hypoglycemia,_familial,_2|Permanent_neonatal_diabetes_mellitus", 0.10),
    ),
    "ABCC8": (
        (NEONATAL_DIABETES, 0.20),
        ("Transient_neonatal_diabetes_mellitus_2", 0.10),
        ("Maturity-onset_diabetes_of_the_young_type_12", 0.05),
        ("Hyperinsulinemic_hypoglycemia,_familial,_1", 0.40),
        ("not_provided", 0.15),
        ("Hyperinsulinemic_hypoglycemia,_familial,_1|Permanent_neonatal_diabetes_mellitus", 0.10),
    ),
    "INS": (
        (NEONATAL_DIABETES, 0.40),
        ("Maturity-onset_diabetes_of_the_young_type_10", 0.35),
        ("not_provided", 0.25),
    ),
}

# Sequence Ontology molecular consequences, by variant type and significance band.
_CONSEQUENCES: dict[str, tuple[tuple[str, float], ...]] = {
    "snv_pathogenic": (
        ("SO:0001583|missense_variant", 0.60),
        ("SO:0001587|stop_gained", 0.25),
        ("SO:0001575|splice_donor_variant", 0.15),
    ),
    "snv_uncertain": (
        ("SO:0001583|missense_variant", 0.75),
        ("SO:0001627|intron_variant", 0.10),
        ("SO:0001819|synonymous_variant", 0.10),
        ("SO:0001575|splice_donor_variant", 0.05),
    ),
    "snv_benign": (
        ("SO:0001819|synonymous_variant", 0.45),
        ("SO:0001627|intron_variant", 0.40),
        ("SO:0001583|missense_variant", 0.15),
    ),
    "indel_pathogenic": (("SO:0001589|frameshift_variant", 1.0),),
    "deletion_other": (("SO:0001822|inframe_deletion", 0.6), ("SO:0001627|intron_variant", 0.4)),
    "insertion_other": (("SO:0001821|inframe_insertion", 0.6), ("SO:0001627|intron_variant", 0.4)),
}

FIRST_VARIATION_ID = 90_000_000
FIRST_ALLELE_ID = 91_000_000
_BASES = "ACGT"
_SNV_SHARE = 0.85
_DELETION_SHARE = 0.08  # the rest are insertions


# ---------------------------------------------------------------------------
# Normalised view of a release (no database)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class KnownRecord:
    """How GE-EHR classifies one record: normalised class, mechanism and expert-panel flag."""

    key: Key
    gene: str
    classification: str
    mechanism: str
    is_expert_panel: bool
    review_stars: int
    clnsig: str | None


def record_key(record: ClinVarRecord) -> Key:
    """The variant key of a record."""
    return (record.chrom, record.pos, record.ref, record.alt)


def key_label(key: Key) -> str:
    """``"7-44145200-C-T"`` — the label GE-EHR shows for a variant key."""
    return "-".join(str(part) for part in key)


def normalise_record(record: ClinVarRecord) -> KnownRecord:
    """Classify one record with the KB package's pure normalisation functions (as the import does)."""
    gene = record.gene or C.gene_for_position(record.chrom, record.pos) or ""
    return KnownRecord(
        key=record_key(record),
        gene=gene,
        classification=classification.normalize_clinvar_significance(record.clnsig),
        mechanism=classification.infer_mechanism(gene, record.clndn),
        is_expert_panel=classification.is_expert_panel_status(record.clnrevstat),
        review_stars=classification.review_stars(record.clnrevstat),
        clnsig=record.clnsig,
    )


def classify_records(records: Iterable[ClinVarRecord]) -> dict[Key, KnownRecord]:
    """Normalised classification of every key of a release, independent of the database.

    A key listed more than once keeps the record with more review stars (the first one on a
    tie), exactly like :func:`ge_ehr.services.clinvar.import_release`.
    """
    known: dict[Key, KnownRecord] = {}
    for record in records:
        normalised = normalise_record(record)
        previous = known.get(normalised.key)
        if previous is None or normalised.review_stars > previous.review_stars:
            known[normalised.key] = normalised
    return known


def release_summary(records: Sequence[ClinVarRecord]) -> dict[str, Any]:
    """Counts describing a release: records per gene, class, raw significance and mechanism."""
    known = classify_records(records)
    genes = Counter(record.gene for record in known.values())
    classes = Counter(record.classification for record in known.values())
    mechanisms = Counter(
        record.mechanism for record in known.values() if record.gene in KATP_GENES and record.classification != "NC"
    )
    return {
        "records": len(known),
        "genes": {gene: genes.get(gene, 0) for gene in C.GENES},
        "classes": {cls: classes.get(cls, 0) for cls in C.CLASSES},
        "raw_significance": dict(sorted(Counter(record.clnsig or "missing" for record in records).items())),
        "expert_panel_records": sum(record.is_expert_panel for record in known.values()),
        "expert_panel_by_gene": {
            gene: sum(record.is_expert_panel and record.gene == gene for record in known.values())
            for gene in C.GENES
        },
        "katp_mechanisms": {mechanism: mechanisms.get(mechanism, 0) for mechanism in C.MECHANISMS},
    }


# ---------------------------------------------------------------------------
# Base release
# ---------------------------------------------------------------------------


def _weighted[T](rng: random.Random, options: Sequence[tuple[T, float]]) -> T:
    """Pick one option by weight with a single ``rng.random()`` draw."""
    total = sum(weight for _option, weight in options)
    point = rng.random() * total
    for option, weight in options:
        point -= weight
        if point < 0:
            return option
    return options[-1][0]


def gene_sizes(total: int) -> dict[str, int]:
    """Records per gene for a release of ``total`` records (largest-remainder allocation, ≥ 1 each)."""
    if total < len(C.GENES):
        raise ValueError(f"A release needs at least {len(C.GENES)} records (one per gene), got {total}.")
    scale = total / sum(GENE_RECORD_SHARES.values())
    exact = {gene: share * scale for gene, share in GENE_RECORD_SHARES.items()}
    sizes = {gene: max(1, int(value)) for gene, value in exact.items()}
    by_remainder = sorted(GENE_RECORD_SHARES, key=lambda gene: (exact[gene] - int(exact[gene]), gene), reverse=True)
    index = 0
    while sum(sizes.values()) < total:
        sizes[by_remainder[index % len(by_remainder)]] += 1
        index += 1
    while sum(sizes.values()) > total:
        largest = max(sizes, key=lambda gene: sizes[gene])
        sizes[largest] -= 1
    return {gene: sizes[gene] for gene in C.GENES}


def random_key(gene: str, rng: random.Random, taken: set[Key]) -> Key:
    """A new random variant key inside ``gene``'s padded GRCh38 region, added to ``taken``.

    85 % SNVs, 8 % small deletions and 7 % small insertions (VCF-style, sharing the first base).
    Alleles are synthetic and not checked against the reference genome (SPEC §11).
    """
    info = C.GENE_INFO[gene]
    while True:
        pos = rng.randint(info.start, info.end - 5)
        kind = rng.random()
        first = rng.choice(_BASES)
        if kind < _SNV_SHARE:
            ref, alt = first, rng.choice([base for base in _BASES if base != first])
        elif kind < _SNV_SHARE + _DELETION_SHARE:
            ref = first + "".join(rng.choice(_BASES) for _ in range(rng.randint(1, 4)))
            alt = first
        else:
            ref = first
            alt = first + "".join(rng.choice(_BASES) for _ in range(rng.randint(1, 3)))
        key = (info.chrom, pos, ref, alt)
        if key not in taken:
            taken.add(key)
            return key


def novel_keys(gene: str, count: int, rng: random.Random, taken: set[Key]) -> list[Key]:
    """``count`` new keys in ``gene`` that are not in ``taken`` (private variants absent from ClinVar)."""
    return [random_key(gene, rng, taken) for _ in range(count)]


def _hgvs(gene: str, key: Key) -> str:
    """Genomic HGVS on the RefSeq chromosome, e.g. ``NC_000007.14:g.44145200C>T``."""
    _chrom, pos, ref, alt = key
    prefix = f"{C.GENE_INFO[gene].refseq_chrom}:g."
    if len(ref) == 1 and len(alt) == 1:
        return f"{prefix}{pos}{ref}>{alt}"
    if len(ref) > 1:
        start, end = pos + 1, pos + len(ref) - 1
        return f"{prefix}{start}del" if start == end else f"{prefix}{start}_{end}del"
    return f"{prefix}{pos}_{pos + 1}ins{alt[1:]}"


def _consequence(key: Key, clnsig: str, rng: random.Random) -> str:
    _chrom, _pos, ref, alt = key
    cls = classification.normalize_clinvar_significance(clnsig)
    if len(ref) == 1 and len(alt) == 1:
        if cls in C.ACTIONABLE:
            mix = _CONSEQUENCES["snv_pathogenic"]
        elif cls in ("LB", "B"):
            mix = _CONSEQUENCES["snv_benign"]
        else:
            mix = _CONSEQUENCES["snv_uncertain"]
    elif cls in C.ACTIONABLE:
        mix = _CONSEQUENCES["indel_pathogenic"]
    else:
        mix = _CONSEQUENCES["deletion_other" if len(ref) > 1 else "insertion_other"]
    return _weighted(rng, mix)


def _review_status(gene: str, clnsig: str, rng: random.Random) -> str:
    if clnsig.startswith("Conflicting_classifications"):
        return CONFLICTING_STATUS
    if clnsig.startswith("Conflicting_interpretations"):
        return "criteria_provided,_conflicting_interpretations"
    if clnsig in ("not_provided", "risk_factor"):
        return "no_assertion_criteria_provided"
    if gene in EXPERT_PANEL_GENES and rng.random() < EXPERT_PANEL_SHARE:
        return EXPERT_PANEL_STATUS
    return _weighted(rng, _REVIEW_STATUS_MIX)


def _make_record(key: Key, gene: str, clnsig: str, rng: random.Random, ids: tuple[int, int]) -> ClinVarRecord:
    chrom, pos, ref, alt = key
    return ClinVarRecord(
        chrom=chrom,
        pos=pos,
        ref=ref,
        alt=alt,
        variation_id=ids[0],
        allele_id=ids[1],
        gene=gene,
        clnsig=clnsig,
        clnrevstat=_review_status(gene, clnsig, rng),
        clndn=_weighted(rng, CONDITION_MIX[gene]),
        clnhgvs=_hgvs(gene, key),
        mc=_consequence(key, clnsig, rng),
    )


def generate_base_release(seed: int, *, total: int = BASE_RELEASE_SIZE) -> list[ClinVarRecord]:
    """Deterministic synthetic ClinVar-format base release of ``total`` records (default 9,000).

    Per-gene sizes follow :data:`GENE_RECORD_SHARES`, raw significances :data:`SIGNIFICANCE_MIX`,
    conditions :data:`CONDITION_MIX`; ~12 % of GCK/HNF1A/HNF4A records (other than
    ``Conflicting_*`` / ``not_provided``) carry ``reviewed_by_expert_panel``. Records are sorted
    by position and numbered with synthetic variation/allele IDs from 90,000,000 / 91,000,000.
    The same ``seed`` and ``total`` always give the same records.
    """
    rng = random.Random(f"ge-ehr-base-release/{seed}")
    taken: set[Key] = set()
    drafts: list[tuple[Key, str, str]] = []
    for gene, size in gene_sizes(total).items():
        for _ in range(size):
            key = random_key(gene, rng, taken)
            drafts.append((key, gene, _weighted(rng, SIGNIFICANCE_MIX)))
    drafts.sort(key=lambda draft: _position_order(draft[0]))
    return [
        _make_record(key, gene, clnsig, rng, (FIRST_VARIATION_ID + index, FIRST_ALLELE_ID + index))
        for index, (key, gene, clnsig) in enumerate(drafts)
    ]


def _position_order(key: Key) -> tuple[int, int, str, str]:
    chrom, pos, ref, alt = key
    return (int(chrom) if chrom.isdigit() else 99, pos, ref, alt)


# ---------------------------------------------------------------------------
# Controlled change sets
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ChangeKind:
    """One kind of injected change.

    ``source_classes`` are the normalised classes a record must have to be eligible (``{"NC"}``:
    the key must be absent from the base release, i.e. a newly added record). ``new_clnsig`` is
    the raw significance afterwards (None: the record is removed; ``""``: unchanged).
    ``group`` is the paper's change category (Table IV) the kind exercises.
    """

    name: str
    label: str
    group: str
    source_classes: frozenset[str]
    new_clnsig: str | None
    description: str
    genes: frozenset[str] = field(default_factory=lambda: frozenset(C.GENES))


CHANGE_KINDS: dict[str, ChangeKind] = {
    kind.name: kind
    for kind in (
        ChangeKind("vus_to_lp", "VUS → LP", "upgrade", frozenset({"VUS"}), "Likely_pathogenic",
                   "VUS reclassified as likely pathogenic"),
        ChangeKind("vus_to_p", "VUS → P", "upgrade", frozenset({"VUS"}), "Pathogenic",
                   "VUS reclassified as pathogenic"),
        ChangeKind("nc_to_lp", "NC → LP (new record)", "upgrade", frozenset({"NC"}), "Likely_pathogenic",
                   "variant absent from the base release newly submitted as likely pathogenic"),
        ChangeKind("lp_to_vus", "LP → VUS", "downgrade", frozenset({"LP"}), "Uncertain_significance",
                   "likely pathogenic variant downgraded to uncertain significance"),
        ChangeKind("p_to_vus", "P → VUS (conflicting)", "downgrade", frozenset({"P"}),
                   "Conflicting_classifications_of_pathogenicity",
                   "pathogenic variant now has conflicting classifications (normalised to VUS)"),
        ChangeKind("p_removed", "P → removed", "downgrade", frozenset({"P"}), None,
                   "pathogenic record withdrawn from ClinVar (key becomes not classified)"),
        ChangeKind("lp_to_p", "LP → P", "within_actionable", frozenset({"LP"}), "Pathogenic",
                   "likely pathogenic variant upgraded to pathogenic"),
        ChangeKind("p_to_lp", "P → LP", "within_actionable", frozenset({"P"}), "Likely_pathogenic",
                   "pathogenic variant changed to likely pathogenic"),
        ChangeKind("katp_activating", "KATP P/LP: mechanism → activating", "within_actionable",
                   frozenset({"P", "LP"}), "", "KCNJ11/ABCC8 condition updated to permanent neonatal diabetes "
                   "(class unchanged; mechanism becomes activating)", KATP_GENES),
        ChangeKind("vus_to_lb", "VUS → LB", "within_non_actionable", frozenset({"VUS"}), "Likely_benign",
                   "VUS reclassified as likely benign"),
        ChangeKind("lb_to_b", "LB → B", "within_non_actionable", frozenset({"LB"}), "Benign",
                   "likely benign variant reclassified as benign"),
        ChangeKind("vus_to_conflicting", "VUS → Conflicting", "within_non_actionable", frozenset({"VUS"}),
                   "Conflicting_classifications_of_pathogenicity",
                   "VUS now has conflicting classifications: a raw CLNSIG change that GE-EHR normalises to "
                   "VUS in both releases, so it is not a classification change"),
    )
}
ADDED_KIND = "added"
"""Ground-truth kind of a brand-new record on a key carried by nobody (ClinVar growth)."""


def is_eligible(kind: str, record: ClinVarRecord | KnownRecord | None, gene: str | None = None) -> bool:
    """True when a change of ``kind`` can be applied to ``record`` (None: a key absent from the release).

    ``record`` may be a raw record or its :func:`normalise_record` result. ``gene`` restricts a
    newly added key (``record`` None) to the kind's genes. ``vus_to_conflicting`` needs a plain
    ``Uncertain_significance`` record; ``katp_activating`` a KCNJ11/ABCC8 P/LP record whose
    mechanism is not already activating.
    """
    spec = CHANGE_KINDS[kind]
    if record is None:
        return "NC" in spec.source_classes and (gene is None or gene in spec.genes)
    known = record if isinstance(record, KnownRecord) else normalise_record(record)
    if known.classification not in spec.source_classes or known.gene not in spec.genes:
        return False
    if kind == "vus_to_conflicting":
        return known.clnsig == "Uncertain_significance"
    if kind == "katp_activating":
        return known.mechanism != "activating"
    return True


def change_category(old_class: str, new_class: str, old_mechanism: str, new_mechanism: str) -> str | None:
    """The engine's change category, derived as :func:`ge_ehr.services.clinvar.diff_releases` does.

    None when neither the normalised class nor the mechanism changes (e.g. VUS → Conflicting); a
    mechanism-only change is within the (unchanged) class's band.
    """
    if old_class != new_class:
        return C.categorize_change(old_class, new_class)
    if old_mechanism != new_mechanism:
        return "within_actionable" if C.is_actionable(new_class) else "within_non_actionable"
    return None


@dataclass(frozen=True)
class ChangeSetSpec:
    """What :func:`apply_change_set` injects.

    ``forced`` maps chosen keys to a change kind (keys of ``nc_to_lp`` must be absent from the
    base release); ``uncarried`` asks for that many additional random changes per kind on keys
    that are neither in ``carried_keys`` nor forced; ``added_uncarried`` adds that many brand-new
    records with a random significance on new keys (ClinVar growth).
    """

    forced: Mapping[Key, str] = field(default_factory=dict)
    uncarried: Mapping[str, int] = field(default_factory=dict)
    carried_keys: frozenset[Key] = frozenset()
    added_uncarried: int = 0


@dataclass(frozen=True)
class InjectedChange:
    """Ground truth of one injected change."""

    key: Key
    gene: str
    kind: str
    carried: bool
    old_clnsig: str | None
    new_clnsig: str | None
    old_class: str
    new_class: str
    old_mechanism: str
    new_mechanism: str

    @property
    def category(self) -> str | None:
        """The engine's change category (:func:`change_category`); None when nothing GE-EHR sees changed."""
        return change_category(self.old_class, self.new_class, self.old_mechanism, self.new_mechanism)

    @property
    def label(self) -> str:
        return key_label(self.key)

    def as_dict(self) -> dict[str, Any]:
        """JSON-ready representation."""
        return {
            "key": self.label,
            "gene": self.gene,
            "kind": self.kind,
            "carried": self.carried,
            "old_clnsig": self.old_clnsig,
            "new_clnsig": self.new_clnsig,
            "old_class": self.old_class,
            "new_class": self.new_class,
            "old_mechanism": self.old_mechanism,
            "new_mechanism": self.new_mechanism,
            "category": self.category,
        }


def _changed_record(record: ClinVarRecord, kind: str, rng: random.Random) -> ClinVarRecord:
    """The record after a change of ``kind`` (review status updated like a re-curation would)."""
    spec = CHANGE_KINDS[kind]
    if kind == "katp_activating":
        return replace(record, clndn=NEONATAL_DIABETES, clnrevstat=MULTIPLE_SUBMITTERS_STATUS)
    clnsig = spec.new_clnsig or ""
    if clnsig.startswith("Conflicting"):
        status = CONFLICTING_STATUS
    elif spec.group == "upgrade" and record.gene in EXPERT_PANEL_GENES and rng.random() < 0.5:
        status = EXPERT_PANEL_STATUS  # ClinGen MDEP re-curation (paper §1: half of GCK VUS upgraded)
    else:
        status = MULTIPLE_SUBMITTERS_STATUS
    return replace(record, clnsig=clnsig, clnrevstat=status, mc=_consequence(record_key(record), clnsig, rng))


def _injected(
    key: Key, kind: str, carried: bool, old: ClinVarRecord | None, new: ClinVarRecord | None
) -> InjectedChange:
    old_known = normalise_record(old) if old is not None else None
    new_known = normalise_record(new) if new is not None else None
    gene = (new_known or old_known).gene if (new_known or old_known) else ""
    return InjectedChange(
        key=key,
        gene=gene,
        kind=kind,
        carried=carried,
        old_clnsig=old.clnsig if old is not None else None,
        new_clnsig=new.clnsig if new is not None else None,
        old_class=old_known.classification if old_known else "NC",
        new_class=new_known.classification if new_known else "NC",
        old_mechanism=old_known.mechanism if old_known else "unknown",
        new_mechanism=new_known.mechanism if new_known else "unknown",
    )


def _check_spec(spec: ChangeSetSpec, by_key: Mapping[Key, ClinVarRecord]) -> None:
    unknown = sorted({kind for kind in spec.forced.values() if kind not in CHANGE_KINDS} |
                     {kind for kind in spec.uncarried if kind not in CHANGE_KINDS})
    if unknown:
        raise ValueError(f"Unknown change kind(s) {', '.join(unknown)}; expected one of {', '.join(CHANGE_KINDS)}.")
    negative = sorted(kind for kind, count in spec.uncarried.items() if count < 0)
    if negative or spec.added_uncarried < 0:
        what = ", ".join(negative) if negative else "added_uncarried"
        raise ValueError(f"Change counts must be >= 0 (negative for {what}).")
    for key, kind in sorted(spec.forced.items()):
        record = by_key.get(key)
        gene = C.gene_for_position(key[0], key[1])
        if gene is None:
            raise ValueError(f"Forced change {kind} on {key_label(key)}: the key is outside the gene panel.")
        if not is_eligible(kind, record, gene):
            state = "absent from the base release" if record is None else f"classified {normalise_record(record).classification}"
            raise ValueError(
                f"Forced change {kind} ({CHANGE_KINDS[kind].label}) cannot be applied to {key_label(key)} ({gene}): "
                f"the key is {state}."
            )


def apply_change_set(
    records: Sequence[ClinVarRecord], spec: ChangeSetSpec, seed: int
) -> tuple[list[ClinVarRecord], list[InjectedChange]]:
    """Derive K_new from ``records`` (K_old) and return ``(new_records, ground_truth)``.

    Forced changes are applied first; then, per kind in :data:`CHANGE_KINDS` order, ``uncarried``
    random changes on eligible keys that nobody carries (never a key of ``carried_keys`` or a
    forced key; ``nc_to_lp`` adds new LP records on fresh keys); then ``added_uncarried`` new
    records with a random significance. The input is not modified. Ground truth lists every
    injected change (``carried`` = key in ``carried_keys``), forced changes first in key order.

    Raises ValueError for an unknown kind, a negative count, an ineligible forced key, or when
    fewer eligible uncarried keys exist than requested.
    """
    rng = random.Random(f"ge-ehr-change-set/{seed}")
    by_key = {record_key(record): record for record in records}
    if len(by_key) != len(records):
        raise ValueError("The base release lists a variant key more than once; change sets need unique keys.")
    _check_spec(spec, by_key)

    next_ids = [
        max((record.variation_id or 0 for record in records), default=FIRST_VARIATION_ID - 1) + 1,
        max((record.allele_id or 0 for record in records), default=FIRST_ALLELE_ID - 1) + 1,
    ]

    def new_ids() -> tuple[int, int]:
        ids = (next_ids[0], next_ids[1])
        next_ids[0] += 1
        next_ids[1] += 1
        return ids

    taken = set(by_key) | set(spec.carried_keys) | set(spec.forced)
    replaced: dict[Key, ClinVarRecord | None] = {}
    added: list[ClinVarRecord] = []
    truth: list[InjectedChange] = []

    for key, kind in sorted(spec.forced.items(), key=lambda item: _position_order(item[0])):
        carried = key in spec.carried_keys
        old = by_key.get(key)
        if old is None:
            gene = C.gene_for_position(key[0], key[1]) or ""
            new = _make_record(key, gene, CHANGE_KINDS[kind].new_clnsig or "", rng, new_ids())
            added.append(new)
        else:
            new = None if CHANGE_KINDS[kind].new_clnsig is None else _changed_record(old, kind, rng)
            replaced[key] = new
        truth.append(_injected(key, kind, carried, old, new))

    for kind, change in CHANGE_KINDS.items():
        count = spec.uncarried.get(kind, 0)
        if not count:
            continue
        if "NC" in change.source_classes:
            for _ in range(count):
                gene = _weighted(rng, [(gene, float(share)) for gene, share in GENE_RECORD_SHARES.items()])
                key = random_key(gene, rng, taken)
                new = _make_record(key, gene, change.new_clnsig or "", rng, new_ids())
                added.append(new)
                truth.append(_injected(key, kind, False, None, new))
            continue
        candidates = [
            record
            for key, record in by_key.items()
            if key not in spec.carried_keys and key not in spec.forced and key not in replaced
            and is_eligible(kind, record)
        ]
        if len(candidates) < count:
            raise ValueError(
                f"The change set asks for {count} uncarried {change.label} change(s) but only {len(candidates)} "
                "eligible key(s) carried by nobody exist in the base release."
            )
        for old in rng.sample(candidates, count):
            key = record_key(old)
            new = None if change.new_clnsig is None else _changed_record(old, kind, rng)
            replaced[key] = new
            truth.append(_injected(key, kind, False, old, new))

    for _ in range(spec.added_uncarried):
        gene = _weighted(rng, [(gene, float(share)) for gene, share in GENE_RECORD_SHARES.items()])
        key = random_key(gene, rng, taken)
        new = _make_record(key, gene, _weighted(rng, SIGNIFICANCE_MIX), rng, new_ids())
        added.append(new)
        truth.append(_injected(key, ADDED_KIND, False, None, new))

    new_records = [replaced.get(key, record) for key, record in by_key.items()]
    result = [record for record in new_records if record is not None] + added
    result.sort(key=lambda record: _position_order(record_key(record)))
    logger.info(
        "Change set: %d injected change(s) (%d on carried keys), %d record(s) added, %d removed",
        len(truth),
        sum(change.carried for change in truth),
        len(added),
        sum(record is None for record in replaced.values()),
    )
    return result, truth


def summarize_changes(changes: Iterable[InjectedChange]) -> dict[str, Any]:
    """Counts of injected changes per kind and per category, split into carried / carried by nobody."""
    per_kind: dict[str, Counter[str]] = {}
    per_category: dict[str, Counter[str]] = {}
    for change in changes:
        where = "carried" if change.carried else "uncarried"
        per_kind.setdefault(change.kind, Counter())[where] += 1
        per_category.setdefault(change.category or "no_class_change", Counter())[where] += 1
    kinds = [*CHANGE_KINDS, ADDED_KIND]
    return {
        "kinds": {
            kind: {
                "label": CHANGE_KINDS[kind].label if kind in CHANGE_KINDS else "new record (random class)",
                "group": CHANGE_KINDS[kind].group if kind in CHANGE_KINDS else "follows the new class",
                "carried": per_kind[kind]["carried"],
                "uncarried": per_kind[kind]["uncarried"],
            }
            for kind in kinds
            if kind in per_kind
        },
        "categories": {
            category: {"carried": counts["carried"], "uncarried": counts["uncarried"]}
            for category, counts in sorted(per_category.items(), key=lambda item: _category_order(item[0]))
        },
    }


def _category_order(category: str) -> int:
    return C.CHANGE_CATEGORIES.index(category) if category in C.CHANGE_CATEGORIES else len(C.CHANGE_CATEGORIES)


# ---------------------------------------------------------------------------
# Real ClinVar release pairs
# ---------------------------------------------------------------------------

REAL_SUBSET_PATTERN = re.compile(r"clinvar_(\d{8})_7genes\.vcf\.gz")


@dataclass(frozen=True)
class RealReleasePair:
    """The oldest and newest 7-gene ClinVar subsets found in a directory."""

    old_path: Path
    new_path: Path
    old_date: date
    new_date: date


def load_real_pair(directory: str | os.PathLike[str], *, allow_synthetic: bool = False) -> RealReleasePair | None:
    """Find a real ClinVar release pair written by ``scripts/fetch_clinvar.py``.

    Looks for ``clinvar_<YYYYMMDD>_7genes.vcf.gz`` files in ``directory`` and returns the oldest
    and the newest by their ``##fileDate``; None when the directory does not exist or holds fewer
    than two subsets. A file flagged ``##ge_ehr_synthetic=true`` is refused with ValueError (a
    synthetic file must never pass for real ClinVar data) unless ``allow_synthetic`` is set, which
    the tests use to exercise this code path. Two subsets with the same fileDate also raise
    ValueError.
    """
    folder = Path(directory)
    if not folder.is_dir():
        return None
    dated: list[tuple[date, Path]] = []
    for path in sorted(folder.iterdir()):
        if not REAL_SUBSET_PATTERN.fullmatch(path.name):
            continue
        meta = read_vcf_header_meta(path)
        if meta["ge_ehr_synthetic"] and not allow_synthetic:
            raise ValueError(
                f"{path.name} is a GE-EHR synthetic file (##ge_ehr_synthetic=true), not a real ClinVar subset; "
                f"remove it from {folder} or fetch real releases with scripts/fetch_clinvar.py."
            )
        file_date = parse_file_date(meta["fileDate"])
        if file_date is None:
            raise ValueError(f"{path.name} has no valid ##fileDate header line; fetch it again with scripts/fetch_clinvar.py.")
        dated.append((file_date, path))
    if len(dated) < 2:
        return None
    dated.sort()
    dates = [file_date for file_date, _path in dated]
    if len(set(dates)) != len(dates):
        duplicate = next(file_date for file_date in dates if dates.count(file_date) > 1)
        raise ValueError(f"Two ClinVar subsets in {folder} have the same fileDate {duplicate.isoformat()}.")
    (old_date, old_path), (new_date, new_path) = dated[0], dated[-1]
    return RealReleasePair(old_path, new_path, old_date, new_date)


def read_release(path: str | os.PathLike[str]) -> list[ClinVarRecord]:
    """All panel records of a ClinVar(-format) VCF (see :func:`ge_ehr.services.clinvar.iter_clinvar_records`)."""
    return list(iter_clinvar_records(path))


def transitions(
    old: Mapping[Key, KnownRecord], new: Mapping[Key, KnownRecord]
) -> dict[Key, tuple[KnownRecord | None, KnownRecord | None]]:
    """Keys whose class or mechanism differs between two classified releases (absent = NC/unknown)."""
    changed: dict[Key, tuple[KnownRecord | None, KnownRecord | None]] = {}
    for key in old.keys() | new.keys():
        before, after = old.get(key), new.get(key)
        state_before = (before.classification, before.mechanism) if before else ("NC", "unknown")
        state_after = (after.classification, after.mechanism) if after else ("NC", "unknown")
        if state_before != state_after:
            changed[key] = (before, after)
    return changed


def natural_changes(
    old: Mapping[Key, KnownRecord], new: Mapping[Key, KnownRecord], carried_keys: Collection[Key]
) -> list[InjectedChange]:
    """The changes of a real release pair as ground-truth records; ``kind`` is the change category."""
    carried = set(carried_keys)
    result: list[InjectedChange] = []
    for key, (before, after) in sorted(transitions(old, new).items(), key=lambda item: _position_order(item[0])):
        old_class, old_mechanism = (before.classification, before.mechanism) if before else ("NC", "unknown")
        new_class, new_mechanism = (after.classification, after.mechanism) if after else ("NC", "unknown")
        present = after if after is not None else before
        result.append(
            InjectedChange(
                key=key,
                gene=present.gene if present is not None else "",
                kind=change_category(old_class, new_class, old_mechanism, new_mechanism) or "unchanged",
                carried=key in carried,
                old_clnsig=before.clnsig if before else None,
                new_clnsig=after.clnsig if after else None,
                old_class=old_class,
                new_class=new_class,
                old_mechanism=old_mechanism,
                new_mechanism=new_mechanism,
            )
        )
    return result
