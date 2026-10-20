"""Tests for F1 patient VCF validation and parsing (``ge_ehr.services.vcf_parser``, SPEC §4)."""

from __future__ import annotations

import gzip
import random
from pathlib import Path
from typing import Any

import cyvcf2
import pytest

from ge_ehr.services import vcf_parser
from ge_ehr.services.vcf_parser import BGZF_EOF, ParsedVariant, ParsedVcf, VcfError, parse_patient_vcf

DATA = Path(__file__).parent / "data"
GRCH38_FIXTURE = DATA / "patient_grch38.vcf"
GRCH37_FIXTURE = DATA / "patient_grch37.vcf"

HEADER_LINES = (
    "##fileformat=VCFv4.2",
    "##contig=<ID=chr7,length=159345973>",
)
COLUMNS = "#CHROM\tPOS\tID\tREF\tALT\tQUAL\tFILTER\tINFO\tFORMAT\tSAMPLE1"


def record(
    chrom: str = "chr7",
    pos: Any = 44145200,
    ref: str = "C",
    alt: str = "T",
    filter_: str = "PASS",
    fmt: str = "GT",
    *samples: str,
    info: str = ".",
) -> str:
    """One tab-separated data line (sample columns default to a single ``0/1``)."""
    return "\t".join([chrom, str(pos), ".", ref, alt, "50", filter_, info, fmt, *(samples or ("0/1",))])


def vcf(*records: str, header: tuple[str, ...] = HEADER_LINES, columns: str = COLUMNS) -> bytes:
    return ("\n".join([*header, columns, *records]) + "\n").encode()


def parse(*records: str, **kwargs: Any) -> ParsedVcf:
    return parse_patient_vcf(vcf(*records, **kwargs))


def rejected(data: bytes) -> VcfError:
    with pytest.raises(VcfError) as info:
        parse_patient_vcf(data)
    return info.value


def keys(parsed: ParsedVcf) -> list[str]:
    return [f"{v.chrom}-{v.pos}-{v.ref}-{v.alt}" for v in parsed.variants]


def bgzip_with_htslib(source: Path, target: Path) -> bytes:
    """Re-write a VCF as BGZF with htslib (cyvcf2's Writer), independently of GE-EHR's writer."""
    reader = cyvcf2.VCF(str(source))
    writer = cyvcf2.Writer(str(target), reader, mode="wz")
    for variant in reader:
        writer.write_record(variant)
    writer.close()
    reader.close()
    return target.read_bytes()


# ---------------------------------------------------------------------------
# A realistic GRCh38 lab VCF
# ---------------------------------------------------------------------------

EXPECTED_FIXTURE_VARIANTS = [
    ParsedVariant("7", 44145200, "C", "T", "heterozygous", "GCK", "PASS", "0/1"),
    ParsedVariant("7", 44180000, "A", "G", "homozygous", "GCK", "PASS", "1/1"),
    ParsedVariant("11", 17387100, "C", "T", "heterozygous", "KCNJ11", "PASS", "1/2"),
    ParsedVariant("11", 17387100, "C", "G", "heterozygous", "KCNJ11", "PASS", "1/2"),
    ParsedVariant("11", 17410300, "G", "A", "hemizygous", "ABCC8", "PASS", "1"),
    ParsedVariant("12", 120978900, "G", "A", "heterozygous", "HNF1A", ".", "0|1"),
    ParsedVariant("17", 37700000, "G", "A", "heterozygous", "HNF1B", "PASS", "0/1"),
    ParsedVariant("20", 44401300, "CT", "C", "homozygous", "HNF4A", "PASS", "1/1"),
    ParsedVariant("20", 44410200, "G", "GTT", "heterozygous", "HNF4A", "PASS", "0/1"),
]


def assert_fixture_parsed(parsed: ParsedVcf) -> None:
    assert parsed.sample_id == "SYNTH-0001"
    assert parsed.fileformat == "VCFv4.2"
    assert parsed.variants == EXPECTED_FIXTURE_VARIANTS
    assert parsed.panel_variants == 9
    assert parsed.total_records == 14
    assert parsed.off_panel_skipped == 2  # chr1 (X01-like) and chrM
    assert parsed.non_pass_skipped == 1  # LowQual
    assert parsed.ref_calls_skipped == 2  # 0/0 and ./.
    assert parsed.warnings == [
        "1 symbolic or breakend ALT allele(s) (e.g. <DEL> at 17:37710000) were skipped: "
        "structural variants are not supported."
    ]


def test_parses_realistic_grch38_lab_vcf() -> None:
    assert_fixture_parsed(parse_patient_vcf(GRCH38_FIXTURE.read_bytes(), "patient_grch38.vcf"))


def test_gzip_and_bgzip_input_parse_like_plain_text(tmp_path: Path) -> None:
    plain = GRCH38_FIXTURE.read_bytes()
    assert_fixture_parsed(parse_patient_vcf(gzip.compress(plain), "patient.vcf.gz"))
    assert_fixture_parsed(parse_patient_vcf(bgzip_with_htslib(GRCH38_FIXTURE, tmp_path / "p.vcf.gz")))


def test_crlf_line_endings_bom_and_trailing_whitespace_are_accepted() -> None:
    text = GRCH38_FIXTURE.read_text().replace("\n", " \t\r\n")
    assert_fixture_parsed(parse_patient_vcf(("﻿" + text).encode()))


def test_old_mac_carriage_return_line_endings_are_accepted() -> None:
    assert_fixture_parsed(parse_patient_vcf(GRCH38_FIXTURE.read_bytes().replace(b"\n", b"\r")))


def test_blank_lines_are_ignored() -> None:
    parsed = parse_patient_vcf(vcf("", record(), "   ", ""))
    assert keys(parsed) == ["7-44145200-C-T"] and parsed.total_records == 1


@pytest.mark.parametrize("version", ["VCFv4.1", "VCFv4.2", "VCFv4.3"])
def test_supported_vcf_versions(version: str) -> None:
    parsed = parse(record(), header=(f"##fileformat={version}",))
    assert parsed.fileformat == version and parsed.panel_variants == 1


def test_parsed_variant_and_result_types() -> None:
    parsed = parse(record())
    assert isinstance(parsed.variants[0], ParsedVariant)
    with pytest.raises(AttributeError):
        parsed.variants[0].alt = "G"  # type: ignore[misc]  # frozen dataclass


# ---------------------------------------------------------------------------
# Genotypes and zygosity
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("genotype", "zygosity"),
    [
        ("0/1", "heterozygous"),
        ("1/0", "heterozygous"),
        ("0|1", "heterozygous"),
        ("1|0", "heterozygous"),
        ("./1", "heterozygous"),
        ("1/.", "heterozygous"),
        ("1/1", "homozygous"),
        ("1|1", "homozygous"),
        ("1", "hemizygous"),
    ],
)
def test_zygosity_from_genotype(genotype: str, zygosity: str) -> None:
    variant = parse(record("chr7", 44145200, "C", "T", "PASS", "GT", genotype)).variants[0]
    assert (variant.zygosity, variant.genotype) == (zygosity, genotype)


@pytest.mark.parametrize("genotype", ["0/0", "0|0", "./.", ".|.", ".", "0"])
def test_genotypes_without_alt_allele_are_reference_calls(genotype: str) -> None:
    parsed = parse(record("chr7", 44145200, "C", "T", "PASS", "GT", genotype))
    assert parsed.variants == [] and parsed.ref_calls_skipped == 1 and parsed.total_records == 1


def test_multi_allelic_record_is_split_per_carried_allele() -> None:
    parsed = parse(
        record("chr7", 44145200, "C", "T,G,A", "PASS", "GT", "1/3"),
        record("chr7", 44145300, "C", "T,G", "PASS", "GT", "2/2"),
    )
    assert [(f"{v.pos}-{v.alt}", v.zygosity, v.genotype) for v in parsed.variants] == [
        ("44145200-T", "heterozygous", "1/3"),
        ("44145200-A", "heterozygous", "1/3"),
        ("44145300-G", "homozygous", "2/2"),
    ]


def test_genotype_is_taken_from_gt_wherever_it_is_in_format() -> None:
    parsed = parse(record("chr7", 44145200, "C", "T", "PASS", "DP:AD:GT:GQ", "30:0,30:1/1:99"))
    assert parsed.variants[0].zygosity == "homozygous"


def test_gt_dropped_from_a_shortened_sample_column_is_a_missing_call() -> None:
    parsed = parse(record("chr7", 44145200, "C", "T", "PASS", "DP:GT", "12"))
    assert parsed.ref_calls_skipped == 1 and parsed.variants == []


def test_spanning_deletion_allele_is_skipped_silently() -> None:
    parsed = parse(record("chr7", 44145200, "C", "*,T", "PASS", "GT", "1/2"))
    assert keys(parsed) == ["7-44145200-C-T"] and parsed.warnings == []


def test_only_the_first_sample_is_used_with_a_warning() -> None:
    columns = COLUMNS + "\tMOTHER\tFATHER"
    parsed = parse(
        record("chr7", 44145200, "C", "T", "PASS", "GT", "0/0", "0/1", "1/1"),
        record("chr7", 44145300, "C", "T", "PASS", "GT", "1/1", "0/0", "0/0"),
        columns=columns,
    )
    assert parsed.sample_id == "SAMPLE1"
    assert keys(parsed) == ["7-44145300-C-T"] and parsed.ref_calls_skipped == 1
    assert parsed.warnings == [
        "The file contains 3 samples (SAMPLE1, MOTHER, FATHER); only the first sample, 'SAMPLE1', was imported."
    ]


def test_many_samples_are_abbreviated_in_the_warning() -> None:
    names = [f"S{i}" for i in range(1, 8)]
    columns = "\t".join(["#CHROM", "POS", "ID", "REF", "ALT", "QUAL", "FILTER", "INFO", "FORMAT", *names])
    parsed = parse(record("chr7", 44145200, "C", "T", "PASS", "GT", *(["0/1"] * 7)), columns=columns)
    assert parsed.warnings[0].startswith("The file contains 7 samples (S1, S2, S3, S4, S5, …)")


# ---------------------------------------------------------------------------
# FILTER, panel and key normalisation
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(("filter_value", "kept"), [("PASS", "PASS"), (".", ".")])
def test_pass_and_missing_filter_are_kept(filter_value: str, kept: str) -> None:
    assert parse(record(filter_=filter_value)).variants[0].filter == kept


@pytest.mark.parametrize("filter_value", ["LowQual", "q10;s50", "PASS;q10", "a b"])
def test_other_filters_are_skipped_and_counted(filter_value: str) -> None:
    parsed = parse(record(filter_=filter_value))
    assert parsed.variants == [] and parsed.non_pass_skipped == 1


def test_off_panel_records_are_counted_and_chromosomes_normalised() -> None:
    parsed = parse(
        record("1", 1000000, "A", "G"),  # X01 of the demo dataset
        record("chrM", 3243, "A", "G", "PASS", "GT", "1"),
        record("M", 3243, "A", "C", "PASS", "GT", "1"),
        record("CHR7", 44145200, "c", "t"),
        record("7", 44139999, "A", "G"),  # one base before the padded GCK region
        record("7", 44140000, "A", "G"),  # first base of the region
    )
    assert keys(parsed) == ["7-44145200-C-T", "7-44140000-A-G"]
    assert parsed.off_panel_skipped == 4 and parsed.total_records == 6


def test_skip_counters_are_exclusive_and_ordered_panel_then_filter_then_genotype() -> None:
    parsed = parse(
        record("1", 1000000, "A", "G", "LowQual", "GT", "0/0"),  # off panel only
        record("chr7", 44145200, "C", "T", "LowQual", "GT", "0/0"),  # non-PASS only
        record("chr7", 44145300, "C", "T", "PASS", "GT", "0/0"),  # reference call
    )
    assert (parsed.off_panel_skipped, parsed.non_pass_skipped, parsed.ref_calls_skipped) == (1, 1, 1)


def test_keys_are_upper_case_and_not_left_normalised() -> None:
    parsed = parse(record("chr20", 44401300, "ctt", "ct"))
    assert keys(parsed) == ["20-44401300-CTT-CT"]


def test_duplicate_records_keep_the_first_occurrence_with_a_warning() -> None:
    parsed = parse(
        record("chr7", 44145200, "C", "T", "PASS", "GT", "0/1"),
        record("7", 44145200, "C", "T", "PASS", "GT", "1/1"),
    )
    assert [v.zygosity for v in parsed.variants] == ["heterozygous"]
    assert parsed.warnings == ["1 variant(s) were listed more than once (e.g. 7-44145200-C-T); the first occurrence was used."]


@pytest.mark.parametrize("alt", ["<DEL>", "<*>", "<NON_REF>", "C[2:321682[", "]13:123456]T", ".A", "G."])
def test_symbolic_and_breakend_alleles_are_skipped_with_a_warning(alt: str) -> None:
    parsed = parse(record("chr7", 44145200, "C", alt))
    assert parsed.variants == [] and parsed.total_records == 1
    assert parsed.warnings == [
        f"1 symbolic or breakend ALT allele(s) (e.g. {alt.upper()} at 7:44145200) were skipped: "
        "structural variants are not supported."
    ]


def test_header_only_file_is_accepted_with_a_warning() -> None:
    parsed = parse()
    assert parsed.variants == [] and parsed.total_records == 0 and parsed.sample_id == "SAMPLE1"
    assert parsed.warnings == ["The file contains no variant records (header only); no variants were imported."]


def test_missing_build_information_is_a_warning() -> None:
    parsed = parse(record(), header=("##fileformat=VCFv4.2",))
    assert parsed.warnings == [
        "The header has no ##reference or ##contig lines, so the genome build could not be checked; "
        "GRCh38 coordinates were assumed."
    ]


# ---------------------------------------------------------------------------
# Genome build
# ---------------------------------------------------------------------------


def test_grch37_fixture_is_rejected() -> None:
    error = rejected(GRCH37_FIXTURE.read_bytes())
    assert error.line == 3 and error.message.startswith("GRCh38 required")


@pytest.mark.parametrize(
    "reference",
    [
        "GRCh37",
        "file:///refs/hg19.fa",
        "file:///refs/hs37d5.fa.gz",
        "/data/human_g1k_v37.fasta",
        "b37",
        "GRCh37.p13",
    ],
)
def test_grch37_reference_line_is_rejected(reference: str) -> None:
    error = rejected(vcf(record(), header=("##fileformat=VCFv4.2", f"##reference={reference}")))
    assert error.line == 2
    assert error.message.startswith("GRCh38 required: the ##reference line names a GRCh37/hg19 genome")


@pytest.mark.parametrize("reference", ["GRCh38", "file:///refs/Homo_sapiens_assembly38.fasta", "hg38", "GRCh38.p14"])
def test_grch38_reference_line_is_accepted(reference: str) -> None:
    parsed = parse(record(), header=("##fileformat=VCFv4.2", f"##reference={reference}"))
    assert parsed.panel_variants == 1 and parsed.warnings == []


@pytest.mark.parametrize(("contig", "length"), [("7", 159_138_663), ("chr11", 135_006_516), ("chr20", 63_025_520)])
def test_grch37_contig_lengths_are_rejected(contig: str, length: int) -> None:
    header = ("##fileformat=VCFv4.2", "##contig=<ID=chr1,length=248956422>", f"##contig=<ID={contig},length={length}>")
    error = rejected(vcf(record(), header=header))
    assert error.line == 3
    assert f"contig {contig} has length {length:,}, its GRCh37/hg19 length" in error.message


def test_grch37_assembly_attribute_is_rejected() -> None:
    error = rejected(vcf(record(), header=("##fileformat=VCFv4.2", "##contig=<ID=7,assembly=hg19>")))
    assert error.line == 2 and "declares assembly 'hg19'" in error.message


def test_contig_lines_without_length_or_with_odd_names_are_not_build_checked() -> None:
    header = (
        "##fileformat=VCFv4.2",
        "##contig=<ID=chr7>",
        "##contig=<ID=chr,length=159138663>",
        "##contig=<ID=7,length=not-a-number>",
    )
    assert parse(record(), header=header).panel_variants == 1


# ---------------------------------------------------------------------------
# Rejections with line numbers
# ---------------------------------------------------------------------------


def test_empty_file_is_rejected() -> None:
    error = rejected(b"")
    assert error.message == "The file is empty." and error.line is None


@pytest.mark.parametrize(
    ("data", "message"),
    [
        (b"#CHROM\tPOS\n", "The first line must declare the VCF version, e.g. '##fileformat=VCFv4.2' (found '#CHROM\\tPOS')."),
        (b"\n", "The first line must declare the VCF version, e.g. '##fileformat=VCFv4.2'."),
        (b"##fileformat=VCFv4.0\n", "VCF version 'VCFv4.0' is not supported; GE-EHR accepts VCFv4.1, VCFv4.2, VCFv4.3."),
        (b"##fileformat=VCFv5\n#CHROM\n", "VCF version 'VCFv5' is not supported; GE-EHR accepts VCFv4.1, VCFv4.2, VCFv4.3."),
    ],
)
def test_first_line_must_declare_a_supported_version(data: bytes, message: str) -> None:
    error = rejected(data)
    assert (error.line, error.message) == (1, message)


@pytest.mark.parametrize(
    ("meta", "message"),
    [
        ("##fileformat=VCFv4.2", "The ##fileformat line may appear only once, as the first line."),
        ("##just a comment", "Header lines must have the form '##key=value' (found '##just a comment')."),
        ('##INFO=<ID=DP,Number=1,Type=Integer,Description="Depth"', "The ##INFO header line is incomplete: it must end with '>'."),
        ('##FORMAT=<Number=1,Type=String,Description="Genotype">', "The ##FORMAT header line has no ID=... attribute."),
    ],
)
def test_malformed_meta_lines_are_rejected(meta: str, message: str) -> None:
    error = rejected(vcf(record(), header=("##fileformat=VCFv4.2", meta)))
    assert (error.line, error.message) == (2, message)


def test_pedigree_lines_need_no_id_attribute() -> None:
    header = ("##fileformat=VCFv4.2", "##PEDIGREE=<Derived=SAMPLE1,Original=GERMLINE>")
    assert parse(record(), header=header).panel_variants == 1


def test_missing_column_header_line_is_rejected() -> None:
    error = rejected(b"##fileformat=VCFv4.2\n##source=x\n")
    assert error.line is None and "no '#CHROM' column header line" in error.message


def test_record_before_column_header_is_rejected() -> None:
    error = rejected(("##fileformat=VCFv4.2\n" + record() + "\n").encode())
    assert error.line == 2 and error.message.startswith("A variant record appears before the '#CHROM' column header line.")


@pytest.mark.parametrize(
    ("columns", "message"),
    [
        (COLUMNS.replace("\t", " "), "The '#CHROM' column header line must be separated by tab characters, not spaces."),
        ("#CHROM\tPOS\tID\tREF\tALT\tQUAL\tFILTER", "The column header line must start with the tab-separated columns"),
        ("#chrom\tpos\tid\tref\talt\tqual\tfilter\tinfo\tformat\ts1", "The column header line must start with the"),
        ("#CHROM\tPOS\tID\tREF\tALT\tQUAL\tFILTER\tINFO", "has no FORMAT and sample columns"),
        ("#CHROM\tPOS\tID\tREF\tALT\tQUAL\tFILTER\tINFO\tFORMAT", "has a FORMAT column but no sample column"),
        ("#CHROM\tPOS\tID\tREF\tALT\tQUAL\tFILTER\tINFO\tSAMPLE1\tSAMPLE2", "The 9th column of the '#CHROM' header line must be FORMAT (found 'SAMPLE1')."),
        ("#CHROM\tPOS\tID\tREF\tALT\tQUAL\tFILTER\tINFO\tFORMAT\t\tS2", "contains an empty sample name"),
        ("#CHROM\tPOS\tID\tREF\tALT\tQUAL\tFILTER\tINFO\tFORMAT\tS1\tS1", "Sample name 'S1' appears more than once"),
    ],
)
def test_invalid_column_header_lines_are_rejected(columns: str, message: str) -> None:
    error = rejected(vcf(record(), columns=columns))
    assert error.line == 3 and message in error.message


def test_header_line_after_column_header_is_rejected() -> None:
    error = rejected(vcf(record(), "##INFO=<ID=DP>"))
    assert (error.line, error.message) == (5, "Header lines must come before the '#CHROM' column header line.")


@pytest.mark.parametrize(
    ("line", "message"),
    [
        ("chr7\t44145200\t.\tC\tT\t50\tPASS\t.\tGT", "Expected 10 tab-separated columns (as in the '#CHROM' header line) but found 9."),
        (record() + "\t0/1", "Expected 10 tab-separated columns (as in the '#CHROM' header line) but found 11."),
        ("chr7 44145200 . C T 50 PASS . GT 0/1", "Columns must be separated by tab characters, not spaces."),
        (record(chrom="7,1"), "CHROM '7,1' is not a valid chromosome name"),
        (record(chrom="chr7<x>"), "CHROM 'chr7<x>' is not a valid chromosome name"),
        (record(chrom="chr"), "CHROM 'chr' is not a valid chromosome name"),
        (record(chrom="*7"), "CHROM '*7' is not a valid chromosome name"),
        (record(pos=0), "POS must be a positive whole number, found '0'."),
        (record(pos=-5), "POS must be a positive whole number, found '-5'."),
        (record(pos="1e5"), "POS must be a positive whole number, found '1e5'."),
        (record(pos="4_4145200"), "POS must be a positive whole number"),
        (record(pos="9" * 5000), "POS must be a positive whole number, found '9999999999999999999999999999999999999999…'."),
        (record(pos=2**31), "POS must be a positive whole number"),
        (record(ref="R"), "REF allele 'R' is not valid: it must consist only of the bases A, C, G, T and N."),
        (record(ref="."), "REF allele '.' is not valid"),
        (record(alt="X"), "ALT allele 'X' is not valid"),
        (record(alt="T,,G"), "ALT allele '' is not valid"),
        (record(alt="T,."), "ALT allele '.' is not valid"),
        (record(alt="<DEL"), "ALT allele '<DEL' is not valid"),
        (record(filter_=""), "The FILTER column is empty; use PASS, '.' or the names of failed filters."),
        (record(fmt="DP:GQ"), "The FORMAT column has no GT (genotype) field"),
        (record(fmt="."), "The FORMAT column has no GT (genotype) field"),
    ],
)
def test_invalid_data_lines_are_rejected_with_their_line_number(line: str, message: str) -> None:
    error = rejected(vcf(record("chr7", 44145100), line))
    assert error.line == 5 and message in error.message
    assert str(error) == f"Line 5: {error.message}"


@pytest.mark.parametrize(
    ("genotype", "message"),
    [
        ("0/x", "Genotype (GT) '0/x' of sample 'SAMPLE1' is not valid; expected a value such as 0/1, 1/1, 0|1 or 1 (haploid)."),
        ("0-1", "Genotype (GT) '0-1' of sample 'SAMPLE1' is not valid"),
        ("/1", "Genotype (GT) '/1' of sample 'SAMPLE1' is not valid"),
        ("0/2", "Genotype '0/2' refers to ALT allele '2', but the record lists only 1 ALT allele(s)."),
        ("0/" + "1" * 50, "but the record lists only 1 ALT allele(s)."),
    ],
)
def test_invalid_genotypes_are_rejected(genotype: str, message: str) -> None:
    error = rejected(vcf(record("chr7", 44145200, "C", "T", "PASS", "GT", genotype)))
    assert error.line == 4 and message in error.message


def test_genotype_with_alt_allele_on_a_no_alt_record_is_rejected() -> None:
    error = rejected(vcf(record("chr7", 44145200, "C", ".", "PASS", "GT", "0/1")))
    assert "but the record lists only 0 ALT allele(s)." in error.message


def test_no_alt_record_with_reference_genotype_is_a_reference_call() -> None:
    assert parse(record("chr7", 44145200, "C", ".", "PASS", "GT", "0/0")).ref_calls_skipped == 1


def test_empty_sample_column_is_rejected() -> None:
    line = "\t".join(["chr7", "44145200", ".", "C", "T", "50", "PASS", ".", "GT", ""]) + "\tX"
    error = rejected(vcf(line, columns=COLUMNS + "\tSAMPLE2"))
    assert error.line == 4 and error.message.startswith("The genotype column of sample 'SAMPLE1' is empty")


# ---------------------------------------------------------------------------
# Encodings, compression and hostile input
# ---------------------------------------------------------------------------


def test_invalid_utf8_is_rejected_with_its_line() -> None:
    data = vcf(record(), header=("##fileformat=VCFv4.2", "##source=caf\xe9"))
    error = rejected(data.replace("caf\xe9".encode(), b"caf\xe9"))
    assert error.line == 2 and "not valid text (UTF-8)" in error.message


def test_nul_bytes_are_rejected_with_their_line() -> None:
    error = rejected(vcf(record(info="DP=1\x00")))
    assert error.line == 4 and error.message == "The file contains binary data (NUL bytes) and is not a text VCF."


@pytest.mark.parametrize(
    ("data", "message"),
    [
        (b"BCF\x02\x02rest", "This is a BCF (binary VCF) file."),
        (b"PK\x03\x04rest", "This is a ZIP archive"),
        ("##fileformat=VCFv4.2\n".encode("utf-16"), "The file is saved as UTF-16 text."),
        (gzip.compress(gzip.compress(b"##fileformat=VCFv4.2\n")), "The file is compressed twice."),
    ],
)
def test_known_binary_formats_get_a_specific_message(data: bytes, message: str) -> None:
    assert message in rejected(data).message


def test_truncated_gzip_is_rejected() -> None:
    compressed = gzip.compress(GRCH38_FIXTURE.read_bytes())
    error = rejected(compressed[: len(compressed) // 2])
    assert error.message.startswith("The file is gzip-compressed but incomplete or corrupt")


def test_bgzip_file_cut_at_a_block_boundary_is_rejected(tmp_path: Path) -> None:
    data = bgzip_with_htslib(GRCH38_FIXTURE, tmp_path / "p.vcf.gz")
    assert data.endswith(BGZF_EOF)
    # Without the end-of-file block, gzip itself sees a complete stream.
    assert gzip.decompress(data[: -len(BGZF_EOF)]) == gzip.decompress(data)
    error = rejected(data[: -len(BGZF_EOF)])
    assert error.message.startswith("The file is gzip-compressed but incomplete or corrupt")


def test_corrupt_gzip_body_is_rejected() -> None:
    compressed = bytearray(gzip.compress(GRCH38_FIXTURE.read_bytes()))
    compressed[30:40] = b"\xff" * 10
    assert "incomplete or corrupt" in rejected(bytes(compressed)).message


def test_decompression_is_capped(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(vcf_parser, "MAX_DECOMPRESSED_BYTES", 1024)
    error = rejected(gzip.compress(GRCH38_FIXTURE.read_bytes()))
    assert error.message.startswith("The decompressed file is larger than 0 MB.")


def test_binary_garbage_is_rejected() -> None:
    rng = random.Random(4)
    for _ in range(50):
        garbage = bytes(rng.randrange(256) for _ in range(rng.randrange(1, 400)))
        with pytest.raises(VcfError):
            parse_patient_vcf(garbage)


def test_very_long_lines_and_alleles_are_handled() -> None:
    parsed = parse(
        record("chr7", 44145200, "C", "T", info="X=" + "A" * 2_000_000),
        record("chr7", 44145300, "C" * 50_000, "C"),
        record("chr7", 44145400, "C", ",".join(["A", "G", "T"] * 300), "PASS", "GT", "0/900"),
    )
    assert parsed.panel_variants == 3
    assert parsed.variants[2].alt == "T" and len(parsed.variants[1].ref) == 50_000


def test_content_htslib_would_choke_on_never_reaches_it() -> None:
    header = (
        "##fileformat=VCFv4.2",
        '##INFO=<ID=DP,Number=1,Type=Bogus,Description="broken type">',
        '##FORMAT=<ID=GT,Number=1,Type=Integer,Description="GT declared as an integer">',
        '##FORMAT=<ID=AD,Number=R,Type=Integer,Description="depths">',
    )
    parsed = parse(
        record("chr7", 44145200, "C", "T", "PASS", "GT:AD", "0/1:abc,zz", info="DP=abc;=;;X=="),
        header=header,
    )
    assert keys(parsed) == ["7-44145200-C-T"]


def test_fuzzed_fixture_never_raises_anything_but_vcf_error() -> None:
    """Random byte edits and truncations of a valid VCF either parse or raise VcfError."""
    original = GRCH38_FIXTURE.read_bytes()
    alphabet = b"\t\n\r ,;:|/.<>[]*=#0123456789ACGTNacgtXYZ\x00\xff"
    rng = random.Random(7)
    outcomes = {"parsed": 0, "rejected": 0}
    for _ in range(300):
        data = bytearray(original)
        for _ in range(rng.randrange(1, 6)):
            position = rng.randrange(len(data))
            action = rng.random()
            if action < 0.5:
                data[position] = rng.choice(alphabet)
            elif action < 0.8:
                del data[position : position + rng.randrange(1, 40)]
            else:
                data[position:position] = bytes(rng.choice(alphabet) for _ in range(rng.randrange(1, 20)))
        if rng.random() < 0.3:
            data = data[: rng.randrange(1, len(data))]
        if rng.random() < 0.2:
            data = bytearray(gzip.compress(bytes(data)))
        try:
            result = parse_patient_vcf(bytes(data))
        except VcfError as error:
            assert error.message
            outcomes["rejected"] += 1
        else:
            assert isinstance(result, ParsedVcf)
            outcomes["parsed"] += 1
    assert outcomes["parsed"] > 0 and outcomes["rejected"] > 0


# ---------------------------------------------------------------------------
# htslib failures are mapped back to line numbers
# ---------------------------------------------------------------------------


_REAL_VCF = cyvcf2.VCF


class _FailingReader:
    """Stands in for cyvcf2.VCF: yields ``good`` records from the real file, then fails or stops."""

    def __init__(self, path: str, *, good: int, error: Exception | None) -> None:
        self._reader = _REAL_VCF(path)
        self._good, self._error = good, error

    def __iter__(self):  # noqa: ANN204 — generator protocol of the stand-in
        for index, variant in enumerate(self._reader):
            if index == self._good:
                if self._error is not None:
                    raise self._error
                return
            yield variant

    def close(self) -> None:
        self._reader.close()


def test_htslib_read_error_reports_the_records_line(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        vcf_parser.cyvcf2, "VCF", lambda path: _FailingReader(path, good=1, error=Exception("bcf_read failed"))
    )
    error = rejected(vcf(record("chr7", 44145200), "", record("chr7", 44145300)))
    assert error.line == 6
    assert error.message == "This variant record could not be read; check its CHROM, POS, REF, ALT, FILTER and GT values."


def test_htslib_silently_stopping_early_is_detected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(vcf_parser.cyvcf2, "VCF", lambda path: _FailingReader(path, good=1, error=None))
    error = rejected(vcf(record("chr7", 44145200), record("chr7", 44145300)))
    assert error.line == 5
    assert error.message == "The file could not be read completely: it has 2 variant records but only 1 could be read."


def test_htslib_open_failure_is_a_vcf_error(monkeypatch: pytest.MonkeyPatch) -> None:
    def refuse(path: str) -> None:
        raise OSError("Error reading file")

    monkeypatch.setattr(vcf_parser.cyvcf2, "VCF", refuse)
    assert rejected(vcf(record())).message == "The file could not be read as a VCF (Error reading file)."


# ---------------------------------------------------------------------------
# VcfError
# ---------------------------------------------------------------------------


def test_vcf_error_is_a_value_error_with_user_facing_text() -> None:
    with_line, without_line = VcfError("Bad POS.", line=12), VcfError("The file is empty.")
    assert isinstance(with_line, ValueError)
    assert (str(with_line), with_line.message, with_line.line) == ("Line 12: Bad POS.", "Bad POS.", 12)
    assert (str(without_line), without_line.line) == ("The file is empty.", None)
