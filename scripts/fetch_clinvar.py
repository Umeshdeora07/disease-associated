#!/usr/bin/env python3
"""Download a real ClinVar GRCh38 VCF and write its 7-gene subset (SPEC §5.3).

The VCF is streamed from NCBI (or read from a local file) and filtered on the fly: a record is
kept when its GENEINFO lists GCK, HNF1A, HNF4A, HNF1B, KCNJ11, ABCC8 or INS, or when its
position lies in a padded GRCh38 panel region. Only the subset is stored, as
``data/clinvar/real/clinvar_<YYYYMMDD>_7genes.vcf.gz`` (git-ignored). Networking and
decompression use the standard library only (``urllib`` + ``gzip``); the filtering code lives in
:func:`ge_ehr.services.clinvar.fetch_clinvar_subset`, shared with ``flask clinvar sync``.

The output is written to a ``.part`` file and renamed once complete, so an interrupted run
never leaves a truncated subset behind; simply run it again. NCBI is not reachable from every
network (it is not from the GE-EHR build sandbox): download the file on another machine and pass
it with ``--input``.

Examples (from the repository root):

    .venv/bin/python scripts/fetch_clinvar.py                       # current release
    .venv/bin/python scripts/fetch_clinvar.py --date 20230107       # archived weekly release
    .venv/bin/python scripts/fetch_clinvar.py --input ~/Downloads/clinvar_20230107.vcf.gz
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from ge_ehr.services.clinvar import ClinVarDownloadError, clinvar_vcf_url, fetch_clinvar_subset  # noqa: E402

DEFAULT_OUTPUT_DIR = REPO_ROOT / "data" / "clinvar" / "real"


def build_parser() -> argparse.ArgumentParser:
    """The command-line interface."""
    parser = argparse.ArgumentParser(
        description="Stream a ClinVar GRCh38 VCF and keep the records of the 7 monogenic-diabetes genes.",
    )
    source = parser.add_mutually_exclusive_group()
    source.add_argument(
        "--date",
        metavar="YYYYMMDD",
        help="archived release date (vcf_GRCh38/archive_2.0/<year>/clinvar_<date>.vcf.gz); default: current release",
    )
    source.add_argument("--url", help="download this URL instead (https://, ftp:// or file://)")
    source.add_argument("--input", type=Path, metavar="FILE", help="read an already downloaded ClinVar VCF(.gz)")
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=DEFAULT_OUTPUT_DIR,
        metavar="DIR",
        help=f"where the subset is written (default: {DEFAULT_OUTPUT_DIR})",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    """Run the fetcher; returns the process exit status."""
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        source = str(args.input) if args.input else args.url or clinvar_vcf_url(args.date)
    except ValueError as exc:
        parser.error(str(exc))
    print(f"Reading {source} ...", flush=True)
    try:
        result = fetch_clinvar_subset(source, args.output_dir)
    except (ClinVarDownloadError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    assert result is not None  # fetch_clinvar_subset only skips files when newer_than is given
    print(f"ClinVar fileDate {result.file_date}: kept {result.kept_records:,} of {result.total_records:,} records.")
    for gene, count in result.gene_counts.items():
        print(f"  {gene:<7} {count:>7,}")
    print(f"Wrote {result.path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
