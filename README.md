# GE-EHR for Monogenic Diabetes

A **Genomic-Enhanced Electronic Health Record** that keeps genetic results up to date.

Monogenic diabetes (MODY, neonatal diabetes) is caused by a single gene, and the gene decides the
treatment: GCK diabetes usually needs no medicine, HNF1A/HNF4A diabetes responds to low-dose
sulfonylurea tablets, and KCNJ11/ABCC8 neonatal diabetes responds to high-dose sulfonylurea.
Many variants are first reported as "uncertain" (VUS) and are **reclassified later** in ClinVar.
Most record systems check a patient's variants only once, so the clinician never hears about the
reclassification. GE-EHR stores the variants, checks them against a versioned copy of ClinVar,
applies six medication-aware rules, and **re-checks the stored patients every time ClinVar or the
rules change**, alerting the clinician only when something clinically relevant changed.

> **Important — research prototype.** Synthetic (made-up) patients only. Every recommendation is
> advisory and must be reviewed by a diabetes specialist. GE-EHR never changes treatment itself.
> Not for clinical use.

---

## What it does

| Module | In plain words |
|---|---|
| **F1 Patient records** | Patients, diabetes diagnosis, current diabetes medicines, and upload of a lab variant file (VCF). |
| **F2 Variant classification** | Finds variants in 7 diabetes genes (GCK, HNF1A, HNF4A, HNF1B, KCNJ11, ABCC8, INS) and labels them Pathogenic / Likely pathogenic / Uncertain / Likely benign / Benign using the current ClinVar release (expert-panel classifications preferred). |
| **F3 Decision support** | Six rules (D1–D6) compare the genetic result with the patient's current medicines and raise an alert, e.g. *"HNF1A pathogenic, patient on insulin → consider low-dose sulfonylurea"*. |
| **F4 FHIR export** | Exports the patient's genetic results as a standard HL7 FHIR R4 file (marked *amended* after a reclassification). |
| **F5 Re-evaluation engine** | When a new ClinVar release or rule version is loaded, re-checks **only** the patients whose variants changed: upgrades create new high-priority alerts, downgrades withdraw alerts and ask the clinician to review, small changes just update the evidence trail. |

Three user roles: **admin** (manages ClinVar releases, rules, users), **clinician** (sees and
reviews recommendations), **lab technician** (uploads VCF files; cannot see recommendations).

Everything can be done in the **browser** (http://localhost:5000) or in the **terminal**.

---

## 1. Install (one time)

You need **Python 3.13** (3.11 or 3.12 should also work) and **Git**.

### Windows (PowerShell)

```powershell
git clone https://github.com/Umeshdeora07/disease-associated.git
cd disease-associated
git checkout claude/project-assessment-pricing-785c20
py -3.13 -m venv .venv
.venv\Scripts\activate
python -m pip install -r requirements.txt
```

If PowerShell says *"running scripts is disabled"*, run this once and try `activate` again:
`Set-ExecutionPolicy -Scope CurrentUser RemoteSigned`

### macOS / Linux

```bash
git clone https://github.com/Umeshdeora07/disease-associated.git
cd disease-associated
git checkout claude/project-assessment-pricing-785c20
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
```

> Every time you open a new terminal, go to the folder and activate the environment again
> (`.venv\Scripts\activate` on Windows, `source .venv/bin/activate` on macOS/Linux).

## 2. Load the demo data

```bash
flask --app wsgi seed-demo
```

This creates 15 synthetic demo patients, the rules and a first ClinVar release. Demo logins
(password for all: `Demo-Pass-2026!`):

| Username | Role | Can do |
|---|---|---|
| `admin` | Administrator | Everything: ClinVar releases, rules, users, audit log |
| `dr.rao` | Clinician | Patients, recommendations, alerts, review notices, FHIR export |
| `lab.kim` | Lab technician | Patients and VCF upload only |

To start again from a clean demo: `flask --app wsgi seed-demo --reset --yes`

## 3. Start the app

```bash
python run.py
```

Open **http://localhost:5000** in your browser and sign in. Stop the app with `Ctrl+C`.
(Options: `python run.py --port 8080`, `python run.py --dev` for the auto-reloading development server.)

## 4. Live demo in 5 steps

1. Sign in as **dr.rao** → *Patients* → **DEMO-0001**: HNF1A pathogenic, on insulin →
   high alert *"Consider low-dose sulfonylurea"* (D2). Open **DEMO-0008**: an HNF1A *uncertain*
   variant → only "monitored for reclassification" (D6).
2. Sign out, sign in as **admin** → *Knowledge base → Rules → Import* →
   `data/rules/ruleset_v2.json`. Rule D4 now also covers ABCC8, so **DEMO-0012** gets a new
   high-priority alert.
3. *Knowledge base → Releases → Import* → `data/demo/clinvar/demo_release_2.vcf.gz`, label
   `R2 (2026-01, synthetic)`, tick **Activate and re-evaluate now**. You land on the
   re-evaluation run: 5 changed variants, only 7 patients re-checked.
4. Sign in as **dr.rao** → *Alerts* → filter **Reclassified**: DEMO-0008's variant was upgraded to
   *likely pathogenic*, so it now gets the high-priority D2 alert; the HNF4A family
   (DEMO-0013/14/15) is alerted together.
5. *Review notices*: DEMO-0010's GCK variant was **downgraded**, its alerts were withdrawn and the
   clinician is asked to review — acknowledge it. On any patient's *Recommendations* page, use
   **View as of** to see exactly what the system said before the update.

## 5. Use it from the terminal

All commands start with `flask --app wsgi` (run them inside the activated environment, in the
project folder). Add `--help` to any command for all options. Changes need `--as USERNAME`
(the action is checked against that user's role and written to the audit log).

```bash
# Patients
flask --app wsgi patients list
flask --app wsgi patients show DEMO-0001
flask --app wsgi patients history DEMO-0008
flask --app wsgi patients history DEMO-0008 --at "2026-01-01 10:00"
flask --app wsgi patients create --as dr.rao --mrn P-0100 --given Asha --family Verma --sex female --birth-date 1990-05-14 --diabetes-type type1 --age-at-diagnosis-months 180
flask --app wsgi patients set-diagnosis P-0100 --as dr.rao --type type1 --age-at-diagnosis-months 180
flask --app wsgi patients add-medication P-0100 --as dr.rao --drug "insulin glargine"
flask --app wsgi patients stop-medication P-0100 --as dr.rao --drug "insulin glargine"
flask --app wsgi patients upload-vcf P-0100 data/demo/vcfs/DEMO-0001.vcf --as lab.kim
flask --app wsgi patients fhir DEMO-0001 --output DEMO-0001_fhir.json

# Alerts and review notices
flask --app wsgi alerts list
flask --app wsgi alerts list --severity high --reclassified
flask --app wsgi alerts show 1
flask --app wsgi alerts review 1 --as dr.rao --comment "Discussed with patient"
flask --app wsgi alerts notices
flask --app wsgi alerts acknowledge 1 --as dr.rao --comment "No therapy change was made"

# Knowledge base, rules and re-evaluation (admin jobs)
flask --app wsgi clinvar list
flask --app wsgi clinvar import data/demo/clinvar/demo_release_2.vcf.gz --label "R2 (2026-01, synthetic)" --activate
flask --app wsgi rules validate data/rules/ruleset_v2.json
flask --app wsgi rules import data/rules/ruleset_v2.json
flask --app wsgi rules list --current
flask --app wsgi reeval runs
flask --app wsgi reeval show 3
flask --app wsgi reeval full --yes

# Users
flask --app wsgi list-users
flask --app wsgi create-user asha --role clinician --full-name "Dr. Asha Verma"   # asks for a password
```

## 6. Use real ClinVar data

The demo ClinVar files are **synthetic** (made up, clearly marked). With internet access you can
download the real ClinVar release and keep only the 7 genes:

```bash
python scripts/fetch_clinvar.py                    # latest release → data/clinvar/real/
python scripts/fetch_clinvar.py --date 20200106    # an archived release
flask --app wsgi clinvar import data/clinvar/real/<file>.vcf.gz --activate
flask --app wsgi clinvar sync                      # download + import + activate if newer
```

`flask --app wsgi clinvar sync` can be scheduled (Windows Task Scheduler / cron) to keep the
knowledge base current; each run re-checks only the affected patients.

## 7. Run the evaluation (paper §5)

```bash
python -m evaluation.run            # about 2 minutes; 10,000 + 30 Synthea patients
python -m evaluation.run --quick    # small and fast check
```

Results go to `evaluation/results/RESULTS.md` (and `results.json`); the method is described in
[docs/EVALUATION.md](docs/EVALUATION.md). Measured on the 10,000-patient Synthea cohort:

| Metric | Alert-once | Full recomputation | GE-EHR (change-driven) |
|---|---|---|---|
| Re-alert sensitivity | 0 % | 100 % | 100 % |
| Stale alerts after a release | all affected | 0 | 0 |
| Notification precision | — | 100 % | 100 % |
| Records examined | 0 | all (17,309) | changed only (573) |
| Time per release | — | ≈ 6.7 s | ≈ 1.5 s |
| Point-in-time correctness (S1–S12) | 100 % | 100 % | 100 % |

All 16 expectations of the paper's Table VI are met. The knowledge-base changes are synthetic
because the build environment could not reach NCBI; with internet access, `scripts/fetch_clinvar.py`
and `python -m evaluation.run --real-dir data/clinvar/real` evaluate a real ClinVar release pair.

## 8. Run the tests

```bash
python -m pip install -r requirements-dev.txt
python -m pytest -q
```

About 2,200 automated tests, including every scenario of the paper's Table V (S1–S12, RS1–RS7).

## 9. Where is the data stored?

Everything stays **on your computer** in the `instance/` folder:

- `instance/ge_ehr.db` — the SQLite database (patients, variants, alerts, releases, audit log).
- `instance/secret_key` — a random key for login sessions, created on first start.

Delete `instance/ge_ehr.db` (or run `flask --app wsgi seed-demo --reset --yes`) to start over.
To use PostgreSQL instead, `pip install psycopg2-binary` and set `DATABASE_URL` in a `.env` file
(see `.env.example`).

## 10. Troubleshooting

| Problem | Fix |
|---|---|
| `flask` / `python` not found | Activate the environment first (step 1). On Windows use `py -3.13`. |
| "Address already in use" | Another program uses port 5000: `python run.py --port 8080`. |
| `cyvcf2` will not install | Not needed: on Windows it is skipped automatically, and GE-EHR uses its built-in Python VCF reader (same results). On other systems delete the `cyvcf2` line in `requirements.txt` and install again. |
| Forgot a password | `flask --app wsgi create-user NEWNAME --role admin`, then reset others under *Admin → Users*. |
| "Refusing to seed a non-empty database" | `flask --app wsgi seed-demo --reset --yes` |
| Want to see what happened | *Admin → Audit log* in the browser. |

## 11. Limitations

- Synthetic patients and a synthetic ClinVar-format knowledge base; clinical accuracy and
  clinician response were not evaluated.
- Seven genes and six simplified rules; rarer and syndromic forms, pregnancy, and copy-number
  changes (e.g. whole-gene HNF1B deletions) are not covered. The rules summarise published
  guidance whose evidence is limited — they must be reviewed by diabetes specialists before any
  clinical use.
- Exact variant matching (no left-normalisation); rules D1–D3 and D5 apply to heterozygous
  carriers, homozygous variants get a "refer to a specialist" note instead.
- SQLite is meant for a single local user; use PostgreSQL for heavier use.

## Project structure

```
ge_ehr/              the application
  services/          F1–F5 logic: vcf_parser, clinvar, classification, rules, alerting,
                     patient_records, reevaluation, fhir_export
  commands/          terminal commands (patients, alerts, clinvar, rules, reeval, users, demo)
  patients/ alerts/ knowledge/ main/ admin/ auth/ api/   web pages
  templates/ static/ web layout and styles (Bootstrap, works offline)
data/                rules (v1, v2), synthetic demo ClinVar releases, demo patients and VCFs,
                     Synthea cohorts
evaluation/          evaluation harness and results
scripts/             fetch_clinvar.py, demo/cohort builders
tests/               automated tests
docs/                SPEC, USER_GUIDE, ARCHITECTURE, EVALUATION, FHIR
run.py               start the app locally
```

More: [User guide](docs/USER_GUIDE.md) · [Architecture](docs/ARCHITECTURE.md) ·
[Evaluation](docs/EVALUATION.md) · [FHIR mapping](docs/FHIR.md) · [Specification](docs/SPEC.md)
