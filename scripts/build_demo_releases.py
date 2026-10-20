#!/usr/bin/env python3
"""Build the two synthetic demo knowledge-base releases (SPEC §11).

Writes ``data/demo/clinvar/demo_release_1.vcf.gz`` (fileDate 2025-01-06, "R1 (2025-01,
synthetic)") and ``demo_release_2.vcf.gz`` (fileDate 2026-01-05, "R2 (2026-01, synthetic)"):
ClinVar-format VCFs carrying the mandatory synthetic header lines (SPEC §0). Each holds the
demo variants V01–V11 and B01–B04 with the classes of the SPEC §11 table plus ~1,500
deterministic filler records (``random.Random(7)``) inside the panel regions, so release pages
look realistic. Release 2 is release 1 with ~3 % of the filler reclassified and ~50 new filler
records. Coordinates, alleles, IDs and classifications are synthetic — they are not checked
against the reference genome and are NOT real ClinVar data.

Run from the repository root (output is byte-for-byte reproducible):

    .venv/bin/python scripts/build_demo_releases.py [--output-dir data/demo/clinvar]
"""

from __future__ import annotations

import argparse
import random
import sys
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from ge_ehr.constants import CLASSES, GENE_INFO, GENES  # noqa: E402
from ge_ehr.services.classification import normalize_clinvar_significance  # noqa: E402
from ge_ehr.services.clinvar import write_clinvar_vcf  # noqa: E402

DEFAULT_OUTPUT_DIR = REPO_ROOT / "data" / "demo" / "clinvar"
RELEASE_FILES = {1: ("demo_release_1.vcf.gz", "2025-01-06"), 2: ("demo_release_2.vcf.gz", "2026-01-05")}
SEED = 7
FILLER_RECORDS = 1500
NEW_RECORDS_IN_RELEASE_2 = 50
RECLASSIFIED_FRACTION = 0.03

# Synthetic identifiers: demo variants 9000001-9000104, filler from 9001001 (all >= 9,000,000).
FILLER_FIRST_VARIATION_ID = 9_001_001
ALLELE_ID_OFFSET = 1_000_000

CLNSIG = {"P": "Pathogenic", "LP": "Likely pathogenic", "VUS": "Uncertain significance",
          "LB": "Likely benign", "B": "Benign"}
EXPERT_PANEL = "reviewed by expert panel"
MULTIPLE_SUBMITTERS = "criteria provided, multiple submitters, no conflicts"
SINGLE_SUBMITTER = "criteria provided, single submitter"
CONFLICTING = "criteria provided, conflicting classifications"
NO_CRITERIA = "no assertion criteria provided"
NEONATAL_DIABETES = "Permanent neonatal diabetes mellitus"

MODY_CONDITION = {
    "GCK": "Maturity-onset diabetes of the young type 2",
    "HNF1A": "Maturity-onset diabetes of the young type 3",
    "HNF4A": "Maturity-onset diabetes of the young type 1",
    "HNF1B": "Renal cysts and diabetes syndrome",
    "INS": "Maturity-onset diabetes of the young type 10",
    "KCNJ11": NEONATAL_DIABETES,
    "ABCC8": NEONATAL_DIABETES,
}
# KCNJ11/ABCC8 filler deliberately mixes neonatal diabetes (activating) and congenital
# hyperinsulinism (inactivating) conditions, as ClinVar does for these genes.
FILLER_CONDITIONS = {
    "GCK": (MODY_CONDITION["GCK"], "Monogenic diabetes", "not provided"),
    "HNF1A": (MODY_CONDITION["HNF1A"], "Monogenic diabetes", "not provided"),
    "HNF4A": (MODY_CONDITION["HNF4A"], "Monogenic diabetes", "not provided"),
    "HNF1B": (MODY_CONDITION["HNF1B"], "not provided"),
    "INS": (MODY_CONDITION["INS"], "Neonatal insulin-dependent diabetes mellitus", "not provided"),
    "KCNJ11": (NEONATAL_DIABETES, "Transient neonatal diabetes mellitus", "Hyperinsulinemic hypoglycemia, familial, 2",
               "Permanent neonatal diabetes mellitus|Hyperinsulinemic hypoglycemia, familial, 2", "not provided"),
    "ABCC8": (NEONATAL_DIABETES, "Transient neonatal diabetes mellitus", "Hyperinsulinemic hypoglycemia, familial, 1",
              "Transient neonatal diabetes mellitus|Hyperinsulinemic hypoglycemia, familial, 1", "not provided"),
}
GENE_WEIGHTS = {"GCK": 18, "HNF1A": 22, "HNF4A": 12, "HNF1B": 10, "KCNJ11": 8, "ABCC8": 24, "INS": 6}
# Raw CLNSIG strings with a ClinVar-like mix for these genes (VUS dominate).
SIGNIFICANCE_WEIGHTS = {
    "Pathogenic": 10,
    "Likely pathogenic": 8,
    "Pathogenic/Likely pathogenic": 4,
    "Pathogenic|risk factor": 0.5,
    "Likely pathogenic, low penetrance": 0.5,
    "Uncertain significance": 37,
    "Conflicting classifications of pathogenicity": 8,
    "Likely benign": 14,
    "Benign": 8,
    "Benign/Likely benign": 4,
    "not provided": 1,
}
# Reclassification moves between releases (old class → possible new CLNSIG values).
RECLASSIFICATIONS = {
    "VUS": ("Likely benign", "Likely benign", "Benign", "Likely pathogenic", "Pathogenic"),
    "LP": ("Pathogenic", "Pathogenic", "Uncertain significance"),
    "P": ("Likely pathogenic",),
    "LB": ("Benign", "Uncertain significance"),
    "B": ("Likely benign",),
    "NC": ("Uncertain significance",),
}
CONSEQUENCES = {
    "snv_damaging": ("SO:0001583|missense_variant", "SO:0001587|nonsense", "SO:0001575|splice_donor_variant"),
    "snv_benign": ("SO:0001819|synonymous_variant", "SO:0001627|intron_variant", "SO:0001583|missense_variant"),
    "deletion": ("SO:0001589|frameshift_variant", "SO:0001822|inframe_deletion"),
    "insertion": ("SO:0001589|frameshift_variant", "SO:0001821|inframe_insertion"),
}
BASES = "ACGT"


@dataclass(frozen=True)
class DemoVariant:
    """One row of the SPEC §11 demo table."""

    code: str
    gene: str
    key: str
    r1: str
    r2: str
    variation_id: int

    @property
    def chrom_pos_ref_alt(self) -> tuple[str, int, str, str]:
        chrom, pos, ref, alt = self.key.split("-")
        return chrom, int(pos), ref, alt


DEMO_VARIANTS: tuple[DemoVariant, ...] = (
    DemoVariant("V01", "HNF1A", "12-120978900-G-A", "P", "P", 9_000_001),
    DemoVariant("V02", "GCK", "7-44145200-C-T", "P", "P", 9_000_002),
    DemoVariant("V03", "HNF4A", "20-44401300-C-T", "LP", "P", 9_000_003),
    DemoVariant("V04", "KCNJ11", "11-17387100-C-T", "P", "P", 9_000_004),
    DemoVariant("V05", "INS", "11-2160500-G-A", "LP", "LP", 9_000_005),
    DemoVariant("V06", "HNF1A", "12-120994300-C-T", "VUS", "LP", 9_000_006),
    DemoVariant("V07", "GCK", "7-44150500-G-A", "LP", "VUS", 9_000_007),
    DemoVariant("V08", "HNF1A", "12-120999100-A-G", "VUS", "LB", 9_000_008),
    DemoVariant("V09", "ABCC8", "11-17410300-G-A", "P", "P", 9_000_009),
    DemoVariant("V10", "HNF4A", "20-44410200-G-T", "VUS", "LP", 9_000_010),
    DemoVariant("V11", "GCK", "7-44160100-T-C", "VUS", "P", 9_000_011),
    DemoVariant("B01", "HNF1A", "12-121001000-C-T", "B", "B", 9_000_101),
    DemoVariant("B02", "GCK", "7-44180000-A-G", "LB", "LB", 9_000_102),
    DemoVariant("B03", "HNF1B", "17-37700000-G-A", "B", "B", 9_000_103),
    DemoVariant("B04", "ABCC8", "11-17450000-C-T", "LB", "LB", 9_000_104),
)
EXPERT_PANEL_CODES = frozenset({"V01", "V02"})


def demo_record(variant: DemoVariant, release: int) -> dict[str, Any]:
    """The ClinVar-format record of a demo variant in release 1 or 2."""
    chrom, pos, ref, alt = variant.chrom_pos_ref_alt
    cls = variant.r1 if release == 1 else variant.r2
    if variant.code in EXPERT_PANEL_CODES:
        status = EXPERT_PANEL
    else:
        status = SINGLE_SUBMITTER if cls == "VUS" else MULTIPLE_SUBMITTERS
    benign = cls in ("LB", "B")
    return {
        "chrom": chrom,
        "pos": pos,
        "ref": ref,
        "alt": alt,
        "variation_id": variant.variation_id,
        "allele_id": variant.variation_id + ALLELE_ID_OFFSET,
        "gene": variant.gene,
        "clnsig": CLNSIG[cls],
        "clnrevstat": status,
        "clndn": "not specified" if benign else MODY_CONDITION[variant.gene],
        "clnhgvs": f"{GENE_INFO[variant.gene].refseq_chrom}:g.{pos}{ref}>{alt}",
        "mc": CONSEQUENCES["snv_benign"][0] if benign else CONSEQUENCES["snv_damaging"][0],
    }


def _review_status(rng: random.Random, clnsig: str) -> str:
    cls = normalize_clinvar_significance(clnsig)
    if clnsig.startswith("Conflicting"):
        return CONFLICTING
    if cls == "NC":
        return NO_CRITERIA
    if cls == "VUS":
        return rng.choices((SINGLE_SUBMITTER, MULTIPLE_SUBMITTERS, NO_CRITERIA), (70, 20, 10))[0]
    return rng.choices((SINGLE_SUBMITTER, MULTIPLE_SUBMITTERS, EXPERT_PANEL, NO_CRITERIA), (45, 40, 8, 7))[0]


def _alleles(rng: random.Random) -> tuple[str, str, str]:
    """Random synthetic alleles: (ref, alt, kind) with ~85 % SNVs, 10 % deletions, 5 % insertions."""
    kind = rng.choices(("snv", "deletion", "insertion"), (85, 10, 5))[0]
    if kind == "snv":
        ref = rng.choice(BASES)
        return ref, rng.choice(BASES.replace(ref, "")), kind
    if kind == "deletion":
        ref = "".join(rng.choice(BASES) for _ in range(rng.randint(2, 5)))
        return ref, ref[0], kind
    ref = rng.choice(BASES)
    return ref, ref + "".join(rng.choice(BASES) for _ in range(rng.randint(1, 4))), kind


def _hgvs(gene: str, pos: int, ref: str, alt: str) -> str:
    refseq = GENE_INFO[gene].refseq_chrom
    if len(ref) == len(alt) == 1:
        return f"{refseq}:g.{pos}{ref}>{alt}"
    if len(alt) == 1:  # deletion after the shared first base
        span = f"{pos + 1}" if len(ref) == 2 else f"{pos + 1}_{pos + len(ref) - 1}"
        return f"{refseq}:g.{span}del"
    return f"{refseq}:g.{pos}_{pos + 1}ins{alt[1:]}"


def _filler_record(rng: random.Random, variation_id: int, taken: set[tuple[str, int]]) -> dict[str, Any]:
    gene = rng.choices(list(GENE_WEIGHTS), list(GENE_WEIGHTS.values()))[0]
    info = GENE_INFO[gene]
    pos = rng.randint(info.start, info.end)
    while (info.chrom, pos) in taken:
        pos = rng.randint(info.start, info.end)
    taken.add((info.chrom, pos))
    ref, alt, kind = _alleles(rng)
    clnsig = rng.choices(list(SIGNIFICANCE_WEIGHTS), list(SIGNIFICANCE_WEIGHTS.values()))[0]
    benign = normalize_clinvar_significance(clnsig) in ("LB", "B")
    if kind == "snv":
        consequence = rng.choice(CONSEQUENCES["snv_benign" if benign else "snv_damaging"])
    else:
        consequence = rng.choice(CONSEQUENCES[kind])
    conditions = ("not specified", *FILLER_CONDITIONS[gene]) if benign else FILLER_CONDITIONS[gene]
    condition = rng.choice(conditions)
    return {
        "chrom": info.chrom,
        "pos": pos,
        "ref": ref,
        "alt": alt,
        "variation_id": variation_id,
        "allele_id": variation_id + ALLELE_ID_OFFSET,
        "gene": gene,
        "clnsig": clnsig,
        "clnrevstat": _review_status(rng, clnsig),
        "clndn": condition,
        "clnhgvs": _hgvs(gene, pos, ref, alt),
        "mc": consequence,
    }


def _reclassify(rng: random.Random, record: dict[str, Any]) -> dict[str, Any]:
    old_class = normalize_clinvar_significance(record["clnsig"])
    new_clnsig = rng.choice(RECLASSIFICATIONS[old_class])
    status = MULTIPLE_SUBMITTERS if rng.random() < 0.7 else EXPERT_PANEL
    return {**record, "clnsig": new_clnsig, "clnrevstat": status}


def build_releases() -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Records of demo release 1 and 2 (deterministic for :data:`SEED`)."""
    rng = random.Random(SEED)
    taken = {(variant.chrom_pos_ref_alt[0], variant.chrom_pos_ref_alt[1]) for variant in DEMO_VARIANTS}
    filler = [_filler_record(rng, FILLER_FIRST_VARIATION_ID + index, taken) for index in range(FILLER_RECORDS)]

    reclassified = set(rng.sample(range(FILLER_RECORDS), round(FILLER_RECORDS * RECLASSIFIED_FRACTION)))
    filler_2 = [_reclassify(rng, record) if index in reclassified else record for index, record in enumerate(filler)]
    first_new_id = FILLER_FIRST_VARIATION_ID + FILLER_RECORDS
    filler_2 += [_filler_record(rng, first_new_id + index, taken) for index in range(NEW_RECORDS_IN_RELEASE_2)]

    release_1 = [demo_record(variant, 1) for variant in DEMO_VARIANTS] + filler
    release_2 = [demo_record(variant, 2) for variant in DEMO_VARIANTS] + filler_2
    return release_1, release_2


def _class_summary(records: list[dict[str, Any]]) -> str:
    counts = Counter(normalize_clinvar_significance(record["clnsig"]) for record in records)
    genes = Counter(record["gene"] for record in records)
    return (
        ", ".join(f"{cls} {counts.get(cls, 0)}" for cls in CLASSES)
        + " | "
        + ", ".join(f"{gene} {genes.get(gene, 0)}" for gene in GENES)
    )


def main(argv: list[str] | None = None) -> int:
    """Write both demo releases; returns the process exit status."""
    parser = argparse.ArgumentParser(description="Build the synthetic demo ClinVar-format releases (SPEC §11).")
    parser.add_argument(
        "--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR, help=f"output folder (default: {DEFAULT_OUTPUT_DIR})"
    )
    args = parser.parse_args(argv)
    for number, records in enumerate(build_releases(), start=1):
        file_name, file_date = RELEASE_FILES[number]
        path = args.output_dir / file_name
        write_clinvar_vcf(records, path, file_date=file_date, synthetic=True)
        print(f"Wrote {path} ({len(records)} records): {_class_summary(records)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
