# Demo patient VCFs (synthetic)

Single-sample patient VCFs for the GE-EHR demo dataset (SPEC §11), one per demo patient in
[`../patients.json`](../patients.json), plus one deliberately invalid file for scenario S10.
**Everything here is synthetic**: the patients are fictional, and the variant coordinates and
alleles were chosen to fall inside the padded GRCh38 panel regions. They are *not* checked
against the reference genome and do not come from a real person or from ClinVar.

## Format of the patient files

All `DEMO-00NN.vcf` files are written by `evaluation.cohort.write_patient_vcf` (the same writer
the evaluation harness uses for cohort patients). Each one has:

* `##fileformat=VCFv4.2`, `##ge_ehr_synthetic=true`, `##reference=GRCh38`, and one `##contig` line
  with the GRCh38 length of every primary chromosome (1–22, X, Y, MT; e.g. 12 = 133,275,309).
  The parser checks these lengths to tell GRCh38 files from GRCh37 ones.
* FORMAT `GT:DP:GQ` and one sample named after the patient's MRN (e.g. `DEMO-0001`).
* Records sorted by chromosome and position, `ID` `.`, FILTER `PASS`, INFO `.`; every demo
  variant is heterozygous (`GT` `0/1`). QUAL, DP and GQ are plausible values derived
  deterministically from the sample and the variant.

## Files

The variant ids refer to the SPEC §11 variant table (V = demo variant whose class matters for a
scenario, B = benign or likely benign background, X01 = off panel).

| File | Sample | Variants (all heterozygous) | Scenario(s) |
|---|---|---|---|
| `DEMO-0001.vcf` | DEMO-0001 | V01 HNF1A 12-120978900-G-A, B01 HNF1A 12-121001000-C-T | S1 |
| `DEMO-0002.vcf` | DEMO-0002 | V01 HNF1A 12-120978900-G-A | S2 |
| `DEMO-0003.vcf` | DEMO-0003 | V02 GCK 7-44145200-C-T | S3 |
| `DEMO-0004.vcf` | DEMO-0004 | V02 GCK 7-44145200-C-T, B02 GCK 7-44180000-A-G | S4 |
| `DEMO-0005.vcf` | DEMO-0005 | V03 HNF4A 20-44401300-C-T | S5, RS3 |
| `DEMO-0006.vcf` | DEMO-0006 | V04 KCNJ11 11-17387100-C-T | S6 |
| `DEMO-0007.vcf` | DEMO-0007 | V05 INS 11-2160500-G-A | S7 |
| `DEMO-0008.vcf` | DEMO-0008 | V06 HNF1A 12-120994300-C-T, B03 HNF1B 17-37700000-G-A | S8, RS1 |
| `DEMO-0009.vcf` | DEMO-0009 | B01 HNF1A, B02 GCK, B04 ABCC8 11-17450000-C-T, X01 1-1000000-A-G | S9 |
| `DEMO-0010.vcf` | DEMO-0010 | V07 GCK 7-44150500-G-A | RS2 |
| `DEMO-0011.vcf` | DEMO-0011 | V08 HNF1A 12-120999100-A-G | RS4 |
| `DEMO-0012.vcf` | DEMO-0012 | V09 ABCC8 11-17410300-G-A | RS5 |
| `DEMO-0013.vcf` | DEMO-0013 | V10 HNF4A 20-44410200-G-T | RS6 (family FAM-RS6, mother) |
| `DEMO-0014.vcf` | DEMO-0014 | V10 HNF4A 20-44410200-G-T | RS6 (son) |
| `DEMO-0015.vcf` | DEMO-0015 | V10 HNF4A 20-44410200-G-T | RS6 (daughter, no diabetes) |
| `malformed.vcf` | DEMO-S10 | — (invalid on purpose) | S10 |

`DEMO-0009.vcf` has four records. The parser keeps the three panel variants and skips X01 as
off panel (`off_panel_skipped = 1`). V11 (GCK 7-44160100-T-C) is in no VCF: it is carried by
nobody (RS7).

## `malformed.vcf` (scenario S10)

Upload rejection test. The header is valid, the first record is valid, and then the file has
two defects:

* **line 12**: POS is `12O994300`, with the letter O where a zero should be. This is not an
  integer, so the upload is rejected with
  `Line 12: POS must be a positive whole number, found '12O994300'.`
* **line 13**: a short data line with 6 instead of 10 tab-separated columns. If line 12 is
  fixed, the upload is still rejected, with
  `Line 13: Expected 10 tab-separated columns (as in the '#CHROM' header line) but found 6.`

Nothing is stored for a rejected upload except the rejected `GenomicUpload` record kept for
the audit trail.

## Regenerating

The patient VCFs can be rebuilt from `patients.json`; `tests/test_demo_data.py` checks that they
are byte-identical to this output:

```python
import json
from pathlib import Path
from evaluation.cohort import write_patient_vcf

demo = Path("data/demo")
for patient in json.loads((demo / "patients.json").read_text(encoding="utf-8")):
    write_patient_vcf(
        demo / "vcfs" / patient["vcf"],
        patient["mrn"],
        [(v["chrom"], v["pos"], v["ref"], v["alt"], v["zygosity"]) for v in patient["variants"]],
    )
```
