# GE-EHR evaluation (paper §5)

This document describes how the GE-EHR prototype is evaluated against the two baselines of the
paper — an alert-once EHR and full recomputation — and how to reproduce the numbers. The measured
values are in [`evaluation/results/RESULTS.md`](../evaluation/results/RESULTS.md) (human-readable,
including the measured Table VI) and [`evaluation/results/results.json`](../evaluation/results/results.json)
(every raw number, the clinical overrides and the environment).

> **Synthetic data only.** The patients are Synthea simulations; the knowledge base is a synthetic
> ClinVar-format release (every generated file carries `##ge_ehr_synthetic=true` and
> `##source=GE-EHR synthetic ClinVar-format release (NOT real ClinVar data)`). Nothing here is
> real patient data or real ClinVar data, and nothing is fit for clinical use.

## 1. What is measured

The evaluation answers the paper's question: *when the knowledge behind a genomic alert changes,
does the EHR notice, and at what cost?* Two knowledge changes are applied, in this order, to
identical copies of the same initial database:

1. **A ClinVar release change** K_old → K_new with a controlled set of reclassifications
   (§3).
2. **A rule revision**: rules v1 → v2, where D4 v1 covers KCNJ11 only and D4 v2 adds ABCC8
   (paper Table III, RS5).

Three strategies process each change (§4); the result of each is compared with an independent
ground truth (§5) using the metrics of paper Table VI (§6).

## 2. Cohorts

Both cohorts come from **Synthea** (build `29a7d63`, Massachusetts, reference date 2026-10-01,
living patients only, all modules), converted by `scripts/build_cohorts.py` into the compact files
in `data/cohorts/`. Their provenance (jar checksum, seeds, exact command lines) and descriptive
statistics are in [`data/cohorts/README.md`](../data/cohorts/README.md):

| Cohort | File | Patients | Synthea seed | Diabetic | Notes |
|---|---|---:|---:|---:|---|
| Correctness | `synthea_correctness_30.csv.gz` | 30 | 30 | 1 | mapped onto the Table V scenarios |
| Scale | `synthea_scale_10000.csv.gz` | 10,000 | 10,000 | 641 | 640 type 2, 1 other; 353 non-diabetic patients on insulin |

Synthea models type 1 and type 2 diabetes only; it prescribes metformin, two insulins,
liraglutide and canagliflozin and **no sulfonylurea at all**. Monogenic phenotypes therefore have
to be introduced by the harness (`evaluation/strategies.py`), and every such change is a
documented **clinical override** recorded in `results.json` (`overrides`: MRN, Synthea id, field,
old value, new value, reason).

**Families.** `evaluation.cohort.make_families` forms families of 2–5 relatives sharing a family
id and surname: 120 families in the scale cohort (0.012 per patient), 2 in the correctness cohort.
All members of a family carry the family's selected variant.

**Variants.** Every scale patient carries 1–3 common benign polymorphisms (B/LB keys of K_old)
drawn from a pool of 48 with Zipf-like frequencies (20 % homozygous). A subset of patients
(single patients or whole families) carries one *selected* knowledge-base variant whose *role*
says what K_new does with it:

| Role group | Roles (keys per 10,000 patients) |
|---|---|
| Upgrade | VUS → LP (30), VUS → P (10), NC → LP (10: a private variant absent from K_old, newly submitted) |
| Downgrade | LP → VUS (15), P → VUS via `Conflicting_classifications_of_pathogenicity` (6), P → removed from ClinVar (4) |
| Within actionable | LP → P (15), P → LP (5), KCNJ11/ABCC8 P/LP whose condition changes to neonatal diabetes, so the mechanism becomes activating (6) |
| Within non-actionable | VUS → LB (20), LB → B (12), VUS → `Conflicting_classifications_of_pathogenicity` (8; normalised to VUS in both releases, so *not* a classification change for GE-EHR) |
| Unchanged | P (70), LP (60), VUS (250), activating KCNJ11/ABCC8 P/LP (24), private variants absent from ClinVar (40) |

One mid-frequency background polymorphism additionally changes LB → B, so a within-band change
reaches hundreds of carriers. Selected variants are drawn by gene with weights GCK 30 %,
HNF1A 30 %, HNF4A 12 %, HNF1B 8 %, ABCC8 8 %, KCNJ11 7 %, INS 5 %.

**Clinical overrides for carriers** (`GENE_PROFILE_MIX`): a carrier without diabetes gets a
diagnosis (MODY-typical onset 10–35 years; type 1 when treated with insulin in childhood,
otherwise type 2) and one current glucose-lowering therapy: GCK carriers untreated 45 % /
metformin 30 % / insulin 25 %; HNF1A and HNF4A carriers insulin 40 % / metformin 30 % /
**already on a sulfonylurea** (gliclazide) 15 % / untreated 15 %; HNF1B insulin 55 % / metformin
25 % / untreated 20 %; INS insulin 80 % / untreated 20 %. Carriers of **activating KCNJ11/ABCC8**
variants get a **neonatal diagnosis at 0–9 months** and insulin (75 %) or glibenclamide (25 %);
other KCNJ11/ABCC8 carriers keep Synthea's data. 35 % of relatives after the first family member
stay as Synthea made them (unaffected carriers). Medication names are Synthea's own product
names for insulin and metformin and real product names for the sulfonylureas, all mapped to drug
classes by `ge_ehr.constants.suggest_drug_class`.

**Correctness cohort and Table V.** The first 12 patients outside families become S1–S9, RS2,
RS4 and RS5 with exactly the variants and clinical state of the demo patients in SPEC §11 (e.g.
S1: HNF1A pathogenic V01 + benign B01, type 1 diagnosed at 180 months, on insulin; S6: KCNJ11
pathogenic activating V04, neonatal diagnosis at 2 months, on insulin). The largest family is
RS6: every member carries the HNF4A VUS V10 that K_new upgrades to LP (mother on metformin, child
on insulin, further relatives without diabetes). V11 (GCK VUS → P) changes in K_new but nobody
carries it (RS7). The remaining patients carry one key each for every other change kind, plus
background variants. The scenario keys V01–V11 and B01–B04 are picked from the synthetic base
release by gene, class, mechanism and raw significance (V01/V02 prefer expert-panel records), so
their coordinates differ from the demo releases.

**Loading.** Each cohort is loaded once into a *baseline* SQLite file:

* Correctness cohort — the real F1 path: rules v1 and K_old are imported and K_old activated;
  then every patient is created with `patient_records.create_patient`, medications are added with
  `add_medication`, and a per-patient VCF (`evaluation.cohort.format_patient_vcf`, which also
  contains the off-panel variant X01 for S9) is ingested with `patient_records.ingest_vcf`, which
  validates and parses it (cyvcf2), stores the variants and evaluates the patient at upload.
* Scale cohort — bulk inserts of patients, diagnoses, medications and unclassified
  PatientVariants (no VCF parsing and no GenomicUpload rows), followed by the first activation of
  K_old, i.e. the initial classification of every stored key (SPEC §7.1 step 5). This is ~100×
  faster than 10,000 uploads and does not affect what is measured: the initial state is verified
  to equal the ground truth under K_old + rules v1 for both cohorts (`initial_state_vs_ground_truth`).

K_new is imported (not activated) into the baseline last, so the import cost is excluded from all
timings and every strategy starts from a byte-identical copy.

## 3. Knowledge changes

**Base release K_old** (`evaluation/knowledge.py::generate_base_release(seed)`): 9,000 synthetic
ClinVar-format records over the seven genes with realistic relative sizes — ABCC8 2,900, HNF1A
1,600, GCK 1,450, HNF1B 1,050, HNF4A 900, KCNJ11 750, INS 350 — and a ClinVar-like significance
mix (VUS 40 %, both `Conflicting_*` wordings 7 %, LB 21 %, B 8 %, `Benign/Likely_benign` 3 %,
P 8.5 %, LP 7.5 %, `Pathogenic/Likely_pathogenic` 2 %, `not_provided` and `risk_factor` 3 %).
About 12 % of GCK, HNF1A and HNF4A records carry `reviewed_by_expert_panel` (ClinGen's
Monogenic Diabetes Expert Panel curates these genes). KCNJ11/ABCC8 conditions mix permanent and
transient neonatal diabetes (activating), familial hyperinsulinism (inactivating) and records
naming both or neither (unknown mechanism). Variation and allele IDs start at 90,000,000 /
91,000,000, far outside the real ClinVar range. Records are deterministic per seed.

**K_new** (`apply_change_set(records, spec, seed)`) applies a *controlled change set*:

* the **forced** changes of the cohorts' selected keys (the roles above) and the background
  LB → B change;
* **changes on keys carried by nobody** (per 9,000 records: VUS → LP 60, VUS → P 20, NC → LP 10,
  LP → VUS 30, P → VUS 10, P removed 10, LP → P 30, P → LP 10, KATP mechanism 5, VUS → LB 80,
  LB → B 60, VUS → Conflicting 40) and 300 brand-new records with a random significance (ClinVar
  growth). These must cost the change-driven engine nothing (RS7).

A re-curated record also gets a new review status (multiple submitters; half of the upgrades in
expert-panel genes become expert-panel reviewed). `apply_change_set` returns the new records and
the **ground truth of every injected change** (old/new raw significance, normalised class,
mechanism, change category, carried or not). Both releases are written with
`ge_ehr.services.clinvar.write_clinvar_vcf(..., synthetic=True)` and imported through the normal
`clinvar.import_release`, so the engine sees exactly what an administrator's import would give it.

**Rule revision**: `data/rules/ruleset_v1.json` → `ruleset_v2.json` (D4 v1 KCNJ11 → D4 v2 KCNJ11
or ABCC8; all other rules identical). Carriers of activating ABCC8 P/LP variants diagnosed in
infancy and on insulin must be newly alerted (RS5).

**Real ClinVar release pair.** `evaluation.knowledge.load_real_pair(data/clinvar/real)` picks the
oldest and newest `clinvar_<YYYYMMDD>_7genes.vcf.gz` subsets written by
`scripts/fetch_clinvar.py` (synthetic files are refused). When present, the scale cohort is
re-planned on the natural changes of that pair (carried keys chosen per change category among
the keys that actually changed) and evaluated the same way. NCBI is not reachable from the build
environment, so this part is reported as **pending**; see §8.

## 4. Strategies

Each strategy runs in its own isolated app (`create_app` with a config dict) on its own copy of the
baseline SQLite file (default rollback journal, no WAL):

| Strategy | Release change | Rule revision |
|---|---|---|
| **Alert-once** (baseline 1) | K_new is marked current (sequence, `is_current`) without any re-evaluation — an EHR that evaluated each patient once, at upload | rule versions imported and switched current without re-evaluation |
| **Full recomputation** (baseline 2) | `reevaluation.activate_release(K_new, strategy="full")`: every patient reconciled against K_new | `reevaluation.apply_rule_set(v2, strategy="full")`: every patient |
| **GE-EHR, change-driven** | `reevaluation.activate_release(K_new, strategy="change_driven")` — Algorithm 2: Δ over stored keys only, carriers of changed keys only | `apply_rule_set(v2)`: carriers of D4's old and new genes, D4 only |

The engine entry points are used unchanged; the harness only times them and inspects the
database afterwards.

## 5. Ground truth

`evaluation.metrics.ground_truth(plan, release, rules)` is computed **without the database or the
re-evaluation engine**: each planned patient's panel variants are classified from the in-memory
release records (`knowledge.classify_records`, which applies the KB package's pure normalisation
functions exactly as `import_release` does — CLNSIG → class, review stars, mechanism inference;
keys absent from the release are NC) and evaluated with the pure Algorithm 1,
`ge_ehr.services.rules.evaluate`, against the patient's planned diagnosis and current drug classes.
The result is the set of findings `(patient, rule, variant, kind, severity)` a correct system
shows. Three ground-truth states are used: GT₀ = K_old + rules v1 (initial state), GT₁ = K_new +
rules v1 (after the release), GT₂ = K_new + rules v2 (after the rule revision).

## 6. Metrics (paper Table VI)

For each knowledge change, with GT before and after the change:

* **Re-alert sensitivity** — (patient, variant) pairs of every carrier of a variant *upgraded into
  the actionable band* that have a **new current alert** from that variant, divided by all such
  pairs. For the rule revision: pairs with an alert that GT₂ newly indicates.
* **Stale alerts** — current alerts (kind `alert`) not in the ground truth after the change;
  **affected alerts** — alerts of the earlier ground truth that the change made obsolete (so
  "all affected stale" means stale = affected). **Missed alerts** — ground-truth alerts that are
  not current. Notes are reported separately (`stale_findings`, `missed_findings`), and
  `final_state_matches_ground_truth` compares every current alert *and* note.
* **Notification precision and recall** — a notification is a created alert with `notify=True`
  or a review notice. It is *correct* when it is a new alert expected by the ground truth
  (in GT_after ∖ GT_before, severity ≠ info) or a review notice for a (patient, variant) whose
  actionable alert (severity ≠ info) the ground truth removed. Precision = correct /
  notifications (n/a when there are none), recall = correct / expected notifications.
* **Records examined** — `ReevaluationRun.records_examined` (patient-variant records passed to
  Algorithm 1); 0 for alert-once. "Changed only" is verified exactly: the change-driven count must
  equal the number of patient-variant records of the changed stored keys (`delta.carrier_records`)
  and Δ must equal the injected changes that the cohorts carry (`delta.matches`).
* **Time per release / revision** — wall-clock milliseconds of the activation (or rule-revision)
  call, excluding the commit (reported separately as `commit_ms`). The scale cohort is run **3
  times** with the strategy order rotated; the median, minimum and maximum are reported, and the
  outcomes of all repeats must be identical (`outcomes_identical_across_repeats`).
* **Point-in-time correctness** — on the correctness cohort, after both knowledge changes:
  S1–S9 via `reevaluation.alerts_as_of(patient, T0)` (what the system recommended before any
  change, compared with SPEC §11's expectations); **S10** a malformed VCF
  (`data/demo/vcfs/malformed.vcf`) is rejected by `ingest_vcf` with a `VcfError`, the rejected
  upload is stored and the patient's variants are unchanged; **S11** the FHIR Bundle of the S1
  patient passes `fhir_export.validate_bundle`; **S12** a lab technician signed in through the real
  login form gets **403** on `patients.recommendations` (a clinician is the control and must not
  be denied). The score is passed / 12.
* **`alerts_as_of` reproduces the pre-change state** — for every patient (correctness cohort) or
  every patient with an alert history (scale cohort), `alerts_as_of(patient, t)` must return
  exactly the alerts that were current at the snapshot taken before the release and before the
  rule revision.
* **Table V re-evaluation scenarios RS1–RS7** are also checked on the correctness cohort (new
  reclassified high-priority D2 for RS1; superseded D1/D5, one review notice, silent D6 and FHIR
  `amended` for RS2; provenance moved to K_new without notification for RS3; D6 superseded
  silently for RS4; new notifying D4 under v2 for RS5; every family carrier alerted for RS6; no
  VariantChange row for the uncarried V11 in RS7). Alert-once fails RS1–RS6 by design.

`RESULTS.md` compares every measured value with the expectation of paper Table VI
(`table_vi_expectations` in `results.json`): alert-once 0 % sensitivity, all affected alerts stale,
no notifications, 0 records; full and GE-EHR 100 % sensitivity, 0 stale alerts, 100 % precision;
full examines all records, GE-EHR the changed ones only and is faster than full; point-in-time
correctness 100 % for all three.

## 7. Reproducing

```sh
.venv/bin/python -m evaluation.run                       # 30 + 10,000 patients, 3 timing repeats (~1.5 min)
.venv/bin/python -m evaluation.run --quick               # 30 + 300 patients, 1,500-record KB (~10 s) → evaluation/results/quick/
.venv/bin/pytest -q tests/test_evaluation.py             # the quick configuration's qualitative outcomes
```

Options: `--scale N`, `--correctness N`, `--seed S` (default 2026; every random choice is seeded,
so the same seed gives the same cohorts, releases and outcomes), `--kb-size N`, `--repeats N`,
`--out DIR` (default `evaluation/results`), `--real-dir DIR` (default `data/clinvar/real`),
`--work-dir DIR` (keep the databases, release files and VCFs; default a temporary directory).

To evaluate a real ClinVar release pair, fetch two archived releases several years apart where
NCBI is reachable, then rerun:

```sh
.venv/bin/python scripts/fetch_clinvar.py --date 20200105    # see the script's --help
.venv/bin/python scripts/fetch_clinvar.py --date 20250105
.venv/bin/python -m evaluation.run
```

## 8. Limitations

* **Synthetic knowledge base.** The controlled change set is designed to exercise every change
  category; its sizes and class mix are realistic in shape but invented. The real-release-pair
  evaluation (natural reclassifications) is implemented but **pending**: NCBI is not reachable
  from the build environment.
* **Synthetic phenotypes.** Synthea has no monogenic diabetes and no sulfonylurea users, so
  carriers' diagnoses and therapies are clinical overrides (all listed in `results.json`). The
  results show that the engine is correct on these states, not how often such states occur.
* **Exact matching.** Variant keys are matched exactly on GRCh38 (chrom, pos, ref, alt) without
  left-normalisation or liftover; patient and ClinVar representations of the same indel written
  differently would not match (paper limitation). The synthetic data never exercises this.
* **The ground truth uses the same rule definitions** as the engine. It validates the engine's
  bookkeeping (what is re-evaluated, superseded, notified and reproducible), not the clinical
  validity of the rules themselves.
* **SQLite timing.** Times are single-process, single-user SQLite on one machine (see the
  environment in `results.json`); absolute values depend on hardware and would differ on
  PostgreSQL under concurrent load. The comparison between strategies on identical copies is the
  meaningful part. The change-driven cost per re-evaluated patient is higher than the full
  strategy's (every re-evaluated patient changes and is flushed, whereas most patients of a full
  recomputation change nothing), so the speed-up is smaller than the reduction in records examined.
* **Baseline simplification.** The alert-once baseline does nothing when a release or rule
  changes; a real EHR of that kind would still use the new release for *new* uploads. No new
  uploads happen between the changes here, so the comparison is unaffected.
* **One rule revision, one release step.** Chains of several releases, rule revisions combined
  with simultaneous therapy changes, and concurrent users are covered by the unit tests of the
  re-evaluation engine, not by this evaluation.

## 9. Results summary

Measured on 2026-10-21 (seed 2026, 4 CPUs, SQLite 3.45); all 16 Table VI expectations are met.
On the 10,000-patient cohort, the release K_old → K_new changes 134 of the 633 stored keys
(carried by 563 patients) and must re-alert 86 carriers of upgraded variants:

| | Alert-once | Full recomputation | GE-EHR (change-driven) |
|---|---|---|---|
| Re-alert sensitivity | 0 % (0 / 86) | 100 % | 100 % |
| Stale alerts after release | 120 (all 120 affected) | 0 | 0 |
| Notification precision / recall | n/a / 0 % | 100 % / 100 % (158) | 100 % / 100 % (158) |
| Records examined | 0 | 17,309 (all) | 573 (changed only) |
| Time per release, median of 3 | 3.2 ms | 2,536 ms | 388 ms (6.5× faster) |
| Point-in-time correctness (S1–S12) | 100 % | 100 % | 100 % |

Full recomputation and GE-EHR both reach exactly the ground-truth state after the release and
after the rule revision (D4 v2 newly alerts 39 ABCC8 carriers; GE-EHR examines the 5,178 records
of KCNJ11/ABCC8 carriers instead of 17,309 and takes 2.5 s instead of 5.0 s), and `alerts_as_of`
reproduces the pre-change states for every checked patient. On the correctness cohort every
Table V scenario passes for both re-evaluating strategies; alert-once passes S1–S12 and RS7 and
fails RS1–RS6, which require re-evaluation.

See [`evaluation/results/RESULTS.md`](../evaluation/results/RESULTS.md) for the full measured
Table VI, the rule-revision table, the per-category breakdown, the Table V scenario results, the
knowledge-change description and the timing runs.
