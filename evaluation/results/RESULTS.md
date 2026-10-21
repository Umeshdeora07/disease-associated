# GE-EHR evaluation results

> **Synthetic data only.** Synthea patients with documented clinical overrides and a synthetic ClinVar-format knowledge base — not real ClinVar data, not for clinical use.

Generated 2026-10-21T03:26:51+00:00 by `python -m evaluation.run --scale 10000 --correctness 30 --seed 2026` in 79 s (Python 3.13.16, SQLite 3.45.1, SQLAlchemy 2.1.5, 4 CPUs, Linux-6.18.44-fc-v139-x86_64-with-glibc2.39). Raw numbers: [`results.json`](results.json); methodology: [`docs/EVALUATION.md`](../../docs/EVALUATION.md).

## Table VI — measured (controlled change set, scale cohort of 10,000 patients)

| Metric | Alert-once | Full recomputation | GE-EHR (change-driven) |
|---|---|---|---|
| Re-alert sensitivity | 0.0 % (0 / 86) | 100.0 % (86 / 86) | 100.0 % (86 / 86) |
| Stale alerts after release | 120 (120 of 120 affected) | 0 | 0 |
| Missed alerts after release | 140 | 0 | 0 |
| Notification precision | n/a (no notifications) | 100.0 % (158 / 158) | 100.0 % (158 / 158) |
| Notification recall | 0.0 % (0 / 158) | 100.0 % (158 / 158) | 100.0 % (158 / 158) |
| Records examined | 0 | 17,309 (all 17,309) | 573 (changed only) |
| Patients re-evaluated | 0 of 10,000 | 10,000 of 10,000 | 563 of 10,000 |
| Time per release (10,000 patients), median [min–max] of 3 | 2.6 ms [2.4–3.3] | 6,685.4 ms [6,597.8–6,755.8] | 1,472.0 ms [1,425.5–1,543.7] |
| Point-in-time correctness (S1–S12, correctness cohort) | 100.0 % (12 / 12) | 100.0 % (12 / 12) | 100.0 % (12 / 12) |
| `alerts_as_of` reproduces the pre-release state | 100.0 % (670 / 670) patients | 100.0 % (707 / 707) patients | 100.0 % (707 / 707) patients |
| Final state equals ground truth | no | yes | yes |

Re-alert sensitivity counts (patient, variant) pairs of carriers of upgraded variants with a new current alert from that variant. Stale = current alerts not in the ground truth for K_new; affected = alerts the change made obsolete. Times are wall-clock milliseconds of the activation call (excluding the commit).

Paper Table VI expectations met: **16 of 16**.

| Metric | Strategy | Expected (paper) | Measured | Met |
|---|---|---|---|---|
| Re-alert sensitivity | Alert-once | 0 % | 0.0 % (0 / 86) | yes |
| Re-alert sensitivity | Full recomputation | 100 % | 100.0 % (86 / 86) | yes |
| Re-alert sensitivity | GE-EHR (change-driven) | 100 % | 100.0 % (86 / 86) | yes |
| Stale alerts after release | Alert-once | all affected | 120 (120 of 120 affected) | yes |
| Stale alerts after release | Full recomputation | 0 | 0 | yes |
| Stale alerts after release | GE-EHR (change-driven) | 0 | 0 | yes |
| Notification precision | Alert-once | n/a | n/a (no notifications) | yes |
| Notification precision | Full recomputation | 100 % | 100.0 % (158 / 158) | yes |
| Notification precision | GE-EHR (change-driven) | 100 % | 100.0 % (158 / 158) | yes |
| Records examined | Alert-once | 0 | 0 | yes |
| Records examined | Full recomputation | all | 17,309 of 17,309 | yes |
| Records examined | GE-EHR (change-driven) | changed only | 573 = records of the 134 changed stored keys | yes |
| Time per release | GE-EHR (change-driven) | lower than full | median 1,472.0 ms vs 6,685.4 ms | yes |
| Point-in-time correctness | Alert-once | 100 % | 100.0 % (12 / 12) | yes |
| Point-in-time correctness | Full recomputation | 100 % | 100.0 % (12 / 12) | yes |
| Point-in-time correctness | GE-EHR (change-driven) | 100 % | 100.0 % (12 / 12) | yes |

## Rule revision (D4 v1 → v2 adds ABCC8), scale cohort

| Metric | Alert-once | Full recomputation | GE-EHR (change-driven) |
|---|---|---|---|
| Newly indicated alerts raised | 0.0 % (0 / 39) | 100.0 % (39 / 39) | 100.0 % (39 / 39) |
| Stale alerts after revision | 120 | 0 | 0 |
| Missed alerts after revision | 179 | 0 | 0 |
| Notification precision | n/a (no notifications) | 100.0 % (39 / 39) | 100.0 % (39 / 39) |
| Notification recall | 0.0 % (0 / 39) | 100.0 % (39 / 39) | 100.0 % (39 / 39) |
| Records examined | 0 | 17,309 (all 17,309) | 5,178 (carriers of the revised rule's genes) |
| Time per revision, median [min–max] of 3 | 7.5 ms [7.2–7.8] | 4,953.2 ms [4,933.0–4,956.8] | 2,468.5 ms [2,422.3–2,516.1] |
| `alerts_as_of` reproduces the pre-revision state | 100.0 % (670 / 670) patients | 100.0 % (707 / 707) patients | 100.0 % (707 / 707) patients |
| Final state equals ground truth | no | yes | yes |

The rule revision follows the release, so its ground truth is K_new + rules v2; the alert-once baseline still shows the stale alerts it kept from the release.

## Per-category breakdown of the release (scale cohort)

| Change category | Carried keys | Carrier pairs | Expected new alerts | Expected removed alerts | Expected notifications | Alert-once | Full recomputation | GE-EHR (change-driven) |
|---|---|---|---|---|---|---|---|---|
| Upgrade into actionable band | 50 | 86 | 120 | 49 | 120 | 0 new / 0 superseded; stale 49, missed 120; notifications 0/0 correct | 120 new / 49 superseded; stale 0, missed 0; notifications 120/120 correct | 120 new / 49 superseded; stale 0, missed 0; notifications 120/120 correct |
| Downgrade out of actionable band | 25 | 36 | 18 | 49 | 36 | 0 new / 0 superseded; stale 49, missed 18; notifications 0/0 correct | 18 new / 49 superseded; stale 0, missed 0; notifications 36/36 correct | 18 new / 49 superseded; stale 0, missed 0; notifications 36/36 correct |
| Within actionable band | 26 | 49 | 2 | 0 | 2 | 0 new / 0 superseded; stale 0, missed 2; notifications 0/0 correct | 2 new / 0 superseded; stale 0, missed 0; notifications 2/2 correct | 2 new / 0 superseded; stale 0, missed 0; notifications 2/2 correct |
| Within non-actionable band | 33 | 402 | 0 | 22 | 0 | 0 new / 0 superseded; stale 22, missed 0; notifications 0/0 correct | 0 new / 22 superseded; stale 0, missed 0; notifications 0/0 correct | 0 new / 22 superseded; stale 0, missed 0; notifications 0/0 correct |
| Raw significance change only (no class change) | 8 | 12 | 0 | 0 | 0 | 0 new / 0 superseded; stale 0, missed 0; notifications 0/0 correct | 0 new / 0 superseded; stale 0, missed 0; notifications 0/0 correct | 0 new / 0 superseded; stale 0, missed 0; notifications 0/0 correct |
| Unchanged variants | 491 | 16,724 | 0 | 0 | 0 | 0 new / 0 superseded; stale 0, missed 0; notifications 0/0 correct | 0 new / 0 superseded; stale 0, missed 0; notifications 0/0 correct | 0 new / 0 superseded; stale 0, missed 0; notifications 0/0 correct |

## Correctness cohort (Table V scenarios)

**Correctness cohort:** 30 patients (2 families, 6 patients in families), 29 carriers of 23 selected variants, 54 patient-variant records over 39 distinct keys; 52 clinical overrides on 29 patients (listed in `results.json`). Loaded via VCF upload (F1) — import old release 616 ms, create patients and ingest vcfs 440 ms, import new release 483 ms. Initial state equals the ground truth under K_old + rules v1: yes (38 findings).

| Metric | Alert-once | Full recomputation | GE-EHR (change-driven) |
|---|---|---|---|
| Re-alert sensitivity | 0.0 % (0 / 8) | 100.0 % (8 / 8) | 100.0 % (8 / 8) |
| Stale alerts after release | 14 (14 of 14 affected) | 0 | 0 |
| Missed alerts after release | 15 | 0 | 0 |
| Notification precision | n/a (no notifications) | 100.0 % (16 / 16) | 100.0 % (16 / 16) |
| Notification recall | 0.0 % (0 / 16) | 100.0 % (16 / 16) | 100.0 % (16 / 16) |
| Records examined | 0 | 54 (all 54) | 16 (changed only) |
| Patients re-evaluated | 0 of 30 | 30 of 30 | 16 of 30 |
| Time per release (30 patients) | 2.4 ms | 214.8 ms | 108.0 ms |
| Point-in-time correctness (S1–S12, correctness cohort) | 100.0 % (12 / 12) | 100.0 % (12 / 12) | 100.0 % (12 / 12) |
| `alerts_as_of` reproduces the pre-release state | 100.0 % (30 / 30) patients | 100.0 % (30 / 30) patients | 100.0 % (30 / 30) patients |
| Final state equals ground truth | no | yes | yes |

| ID | Scenario | Expected | Alert-once | Full recomputation | GE-EHR (change-driven) |
|---|---|---|---|---|---|
| S1 | HNF1A pathogenic, on insulin | High alert, D2 | pass: as of T0: D2 high alert, D5 low alert | pass: as of T0: D2 high alert, D5 low alert | pass: as of T0: D2 high alert, D5 low alert |
| S2 | HNF1A pathogenic, already on sulfonylurea | Note only (no D2 alert) | pass: as of T0: D2 note, D5 low alert | pass: as of T0: D2 note, D5 low alert | pass: as of T0: D2 note, D5 low alert |
| S3 | GCK pathogenic, on metformin | Medium alert, D1 | pass: as of T0: D1 medium alert, D5 low alert | pass: as of T0: D1 medium alert, D5 low alert | pass: as of T0: D1 medium alert, D5 low alert |
| S4 | GCK pathogenic, no medication | Note only (no D1 alert) | pass: as of T0: D1 note, D5 low alert | pass: as of T0: D1 note, D5 low alert | pass: as of T0: D1 note, D5 low alert |
| S5 | HNF4A likely pathogenic, on metformin | Medium alert, D3 | pass: as of T0: D3 medium alert, D5 low alert | pass: as of T0: D3 medium alert, D5 low alert | pass: as of T0: D3 medium alert, D5 low alert |
| S6 | KCNJ11 pathogenic, diagnosed in infancy, on insulin | High alert, D4 | pass: as of T0: D4 high alert, D5 low alert | pass: as of T0: D4 high alert, D5 low alert | pass: as of T0: D4 high alert, D5 low alert |
| S7 | Any actionable variant | Low alert, D5 | pass: as of T0: D5 low alert | pass: as of T0: D5 low alert | pass: as of T0: D5 low alert |
| S8 | HNF1A VUS | Info, D6 | pass: as of T0: D6 info alert | pass: as of T0: D6 info alert | pass: as of T0: D6 info alert |
| S9 | Only benign variants | No alert | pass: as of T0: none | pass: as of T0: none | pass: as of T0: none |
| S10 | Malformed VCF | Rejected | pass: rejected: Line 12: POS must be a positive whole number, found '12O994300'. | pass: rejected: Line 12: POS must be a positive whole number, found '12O994300'. | pass: rejected: Line 12: POS must be a positive whole number, found '12O994300'. |
| S11 | FHIR export of S1 patient | Valid FHIR R4 Bundle | pass: valid Bundle with 5 entries | pass: valid Bundle with 5 entries | pass: valid Bundle with 5 entries |
| S12 | Lab technician opens recommendations | Access denied | pass: lab technician 403, clinician 500 (page error after the access check) | pass: lab technician 403, clinician 500 (page error after the access check) | pass: lab technician 403, clinician 500 (page error after the access check) |
| RS1 | HNF1A VUS (S8) upgraded to LP; patient on insulin | New high alert D2, reclassified | FAIL: current: D6 info alert; reclassified/high priority/notify: False; D6 superseded: False | pass: current: D2 high alert, D5 low alert; reclassified/high priority/notify: True; D6 superseded: True | pass: current: D2 high alert, D5 low alert; reclassified/high priority/notify: True; D6 superseded: True |
| RS2 | GCK LP downgraded to VUS | Alert superseded; review notice | FAIL: superseded [] (unexpected); review notices: 0; current: D1 medium alert, D5 low alert; FHIR status: ['final'] | pass: superseded ['D1', 'D5'] (downgrade); review notices: 1; current: D6 info alert; FHIR status: ['amended'] | pass: superseded ['D1', 'D5'] (downgrade); review notices: 1; current: D6 info alert; FHIR status: ['amended'] |
| RS3 | HNF4A LP changed to P | Provenance updated; no notice | FAIL: current: D3 medium alert, D5 low alert; same alerts: True; confirmed under Synthetic K_new (2026-01, controlled change set): False; notifications: False | pass: current: D3 medium alert, D5 low alert; same alerts: True; confirmed under Synthetic K_new (2026-01, controlled change set): True; notifications: False | pass: current: D3 medium alert, D5 low alert; same alerts: True; confirmed under Synthetic K_new (2026-01, controlled change set): True; notifications: False |
| RS4 | HNF1A VUS changed to LB | Provenance updated; no notice | FAIL: current: D6 info alert; D6 superseded: False; review notices: 0 | pass: current: none; D6 superseded: True; review notices: 0 | pass: current: none; D6 superseded: True; review notices: 0 |
| RS5 | D4 v1 (KCNJ11) replaced by v2 (adds ABCC8) | ABCC8 carriers on insulin newly alerted | FAIL: before revision: none; after: none | pass: before revision: none; after: D4 high alert (rule version 2, notify True) | pass: before revision: none; after: D4 high alert (rule version 2, notify True) |
| RS6 | Upgraded variant shared by a family of patients | Every carrier alerted | FAIL: 0 of 4 carriers alerted as expected | pass: 4 of 4 carriers alerted as expected | pass: 4 of 4 carriers alerted as expected |
| RS7 | Changed variant carried by no patient | No work performed | pass: carriers: 0; VariantChange rows: 0 | pass: carriers: 0; VariantChange rows: 0 | pass: carriers: 0; VariantChange rows: 0 |

S1–S9 are checked with `alerts_as_of(patient, T0)` after both knowledge changes, i.e. what the system recommended before them. RS1–RS6 describe re-evaluation, which the alert-once baseline does not perform, so it fails them by design.

## Cohorts

**Scale cohort:** 10,000 patients (120 families, 445 patients in families), 910 carriers of 585 selected variants, 17,309 patient-variant records over 633 distinct keys; 1,256 clinical overrides on 731 patients (listed in `results.json`). Loaded via bulk insert + first activation of K_old — import old release 549 ms, bulk load 1,232 ms, initial activation 15,520 ms, import new release 595 ms. Initial state equals the ground truth under K_old + rules v1: yes (883 findings).

## Knowledge changes

**Base release K_old** — Synthetic K_old (2024-01, base release): 9,000 synthetic ClinVar-format records (`##ge_ehr_synthetic=true`; sha256 `2bcaeda0da300c2a…`). **K_new** — Synthetic K_new (2026-01, controlled change set): 9,306 records (sha256 `623dae37129a0cf2…`).

| Gene | GCK | HNF1A | HNF4A | HNF1B | KCNJ11 | ABCC8 | INS |
|---|---|---|---|---|---|---|---|
| K_old records | 1,450 | 1,600 | 900 | 1,050 | 750 | 2,900 | 350 |
| Expert-panel records | 167 | 147 | 104 | 0 | 0 | 0 | 0 |

| Class | P | LP | VUS | LB | B | NC |
|---|---|---|---|---|---|---|
| K_old records | 775 | 872 | 4,246 | 2,146 | 709 | 252 |
| K_new records | 831 | 938 | 4,238 | 2,228 | 811 | 260 |

KCNJ11/ABCC8 classified records by inferred mechanism (K_old): activating 1,376, inactivating 1,334, unknown 843. Raw `Conflicting_*` significances (both wordings) are normalised to VUS.

**Controlled change set** (keys carried by at least one patient of either cohort vs. carried by nobody):

| Change | Category | Carried | Carried by nobody |
|---|---|---|---|
| VUS → LP | upgrade | 32 | 60 |
| VUS → P | upgrade | 11 | 21 |
| NC → LP (new record) | upgrade | 11 | 10 |
| LP → VUS | downgrade | 16 | 30 |
| P → VUS (conflicting) | downgrade | 7 | 10 |
| P → removed | downgrade | 5 | 10 |
| LP → P | within actionable | 16 | 30 |
| P → LP | within actionable | 6 | 10 |
| KATP P/LP: mechanism → activating | within actionable | 7 | 5 |
| VUS → LB | within non actionable | 21 | 80 |
| LB → B | within non actionable | 14 | 60 |
| VUS → Conflicting | within non actionable | 9 | 40 |
| new record (random class) | follows the new class | 0 | 300 |

The background polymorphism `11-17385325-CAGT-C` (likely benign → benign) is one of the carried within-non-actionable changes. **Rule revision:** D4 v1 (KCNJ11) → D4 v2 (KCNJ11 or ABCC8), rules v1 → v2.

## Timing runs (scale cohort)

| Strategy | Release runs (ms) | Rule revision runs (ms) | Outcomes identical across runs |
|---|---|---|---|
| Alert-once | 2.6, 3.3, 2.4 | 7.8, 7.5, 7.2 | yes |
| Full recomputation | 6,597.8, 6,755.8, 6,685.4 | 4,956.8, 4,953.2, 4,933.0 | yes |
| GE-EHR (change-driven) | 1,472.0, 1,425.5, 1,543.7 | 2,422.3, 2,516.1, 2,468.5 | yes |

## Real ClinVar release pair

Real ClinVar release pair: pending — NCBI not reachable from the build environment; run scripts/fetch_clinvar.py then python -m evaluation.run.

## Reproduce

```sh
.venv/bin/python -m evaluation.run --scale 10000 --correctness 30 --seed 2026
```
