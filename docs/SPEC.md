# GE-EHR for Monogenic Diabetes — Implementation Specification

This document is the binding contract for the implementation of the system described in the paper
*"GE-EHR for monogenic diabetes: a Genomic-Enhanced EHR with reclassification-aware re-evaluation"*
(the uploaded design paper). Every module, file name, function signature, field name and route
below is normative. If something is not specified, choose the simplest correct option that is
consistent with this document and the paper, and document the decision in a code comment.

> **Scope reminder.** The system is a research prototype that stores **synthetic data only**.
> Recommendations are **advisory** and phrased for specialist review; the system never changes
> therapy itself. The UI must say so clearly.

---

## 0. Ground rules for every implementer

* Python 3.13, the shared virtualenv at `/home/user/disease-associated/.venv` (already created —
  run tools as `.venv/bin/python`, `.venv/bin/pytest`, `.venv/bin/flask`). Do **not** pip-install
  new packages without listing them in `requirements.txt`; prefer the stdlib and the installed set:
  Flask 3.1, Flask-SQLAlchemy 3.1 / SQLAlchemy 2.1, Flask-Login, Flask-WTF/WTForms, Flask-Migrate,
  cyvcf2 0.34, fhir.resources 8.3 (use the `fhir.resources.R4B` sub-package for R4-compatible
  validation), gunicorn, psycopg2-binary, python-dotenv, pytest, pytest-cov, playwright.
* Package name: `ge_ehr`. App factory: `ge_ehr.create_app(config: str | dict | None = None)`.
* SQLAlchemy 2.0 style models (`Mapped[...]`, `mapped_column`) on Flask-SQLAlchemy `db.Model`.
  Portable types only (must work on SQLite **and** PostgreSQL). Store datetimes as **naive UTC**
  produced by `ge_ehr.models.utcnow()`.
* **Transactions:** service functions `flush()` but never `commit()`; the caller (route, CLI
  command, seeding, evaluation harness, test) commits. Single exception: the re-evaluation entry
  points in `services/reevaluation.py` record a *failed* run in a fresh transaction after rolling
  back (see §7.6).
* Type hints and docstrings on public functions. Module-level `logger = logging.getLogger(__name__)`;
  no `print` in library code (CLI commands use `click.echo`).
* Never commit to git (the coordinator commits). Never touch files owned by another work package
  unless this spec says so; if you must, keep the change minimal and mention it in your report.
* Network: PyPI, npm registry and github.com are reachable; NCBI/ClinVar FTP is **not** reachable
  from this sandbox. Never fabricate "real" ClinVar data — every generated knowledge-base file
  must carry the header line `##ge_ehr_synthetic=true` and the source line
  `##source=GE-EHR synthetic ClinVar-format release (NOT real ClinVar data)`.
* Tests: `pytest` from the repo root must pass; tests use an in-memory SQLite DB via the
  `TestingConfig`. Keep the whole suite fast (< ~60 s, excluding tests marked `@pytest.mark.slow`).


### 0.1 Work packages and file ownership

| WP | Owns |
|---|---|
| **Foundation** | `requirements.txt`, `pyproject.toml`, `.env.example`, `wsgi.py`, `ge_ehr/__init__.py`, `config.py`, `extensions.py`, `constants.py`, `models.py`, `security.py`, `cli.py`, `ge_ehr/commands/__init__.py`, `commands/user_cmds.py`, **interface stubs** of every other module (all dataclasses/exceptions fully defined as in this spec + function signatures raising `NotImplementedError`), auth + admin blueprints, `templates/base.html`, `templates/_macros.html`, auth/admin/error templates, `static/` (vendored Bootstrap, `css/app.css`, `js/app.js`), `tests/conftest.py`, `tests/factories.py`, foundation tests. |
| **KB** | `services/vcf_parser.py`, `services/clinvar.py`, `services/classification.py`, `commands/clinvar_cmds.py`, `scripts/fetch_clinvar.py`, `scripts/build_demo_releases.py`, `data/demo/clinvar/*`, `data/demo/expert_panel_overlay_example.csv`, their tests + `tests/data/*.vcf*`. |
| **Rules** | `services/rules.py`, `services/alerting.py`, `data/rules/*.json`, `commands/rules_cmds.py`, their tests. |
| **Reeval** | `services/reevaluation.py`, `services/patient_records.py`, `commands/reeval_cmds.py`, their tests. |
| **FHIR** | `services/fhir_export.py`, `ge_ehr/api/routes.py`, their tests. |
| **Cohort** | `evaluation/__init__.py`, `evaluation/cohort.py`, `scripts/run_synthea.sh`, `scripts/build_cohorts.py`, `data/cohorts/*`, `data/demo/patients.json`, `data/demo/vcfs/*`, their tests. |
| **UI-A** | `patients/`, `alerts/` blueprints + `templates/patients/*`, `templates/alerts/*`, their route tests; may append patient/alert-specific CSS to `static/css/app.css` in a clearly delimited section. |
| **UI-B** | `main/`, `knowledge/` blueprints + `templates/main/*`, `templates/knowledge/*`, audit-page polish, their route tests; may append a delimited CSS section. |
| **Eval** | `evaluation/knowledge.py`, `evaluation/strategies.py`, `evaluation/metrics.py`, `evaluation/run.py`, `evaluation/results/*`, `docs/EVALUATION.md`, their tests. |
| **Scenario** | `ge_ehr/demo.py`, `commands/demo_cmds.py`, `tests/test_scenarios.py`. |
| **Docs/Deploy** | `README.md`, `docs/ARCHITECTURE.md`, `docs/USER_GUIDE.md`, `docs/DEPLOYMENT.md`, `render.yaml`, `Dockerfile`, `Procfile`, `migrations/`. |

---

## 1. Repository layout

```
README.md                      project overview, quick start            (Docs WP)
requirements.txt               pinned runtime + dev deps                (Foundation)
pyproject.toml                 metadata + pytest config (markers: slow) (Foundation)
.env.example                   SECRET_KEY, DATABASE_URL, ...            (Foundation)
wsgi.py                        app = create_app()                       (Foundation)
render.yaml, Dockerfile, Procfile                                      (Docs/Deploy WP)
ge_ehr/
  __init__.py                  create_app, blueprints, error handlers, security headers
  config.py                    Config / DevelopmentConfig / TestingConfig / ProductionConfig
  extensions.py                db, migrate, login_manager, csrf
  constants.py                 genes, classes, bands, drug classes, LOINC/HGNC codes, helpers
  models.py                    ALL ORM models (§3)
  security.py                  roles_required, audit(), password policy
  cli.py                       registers command groups from ge_ehr/commands/*
  commands/                    clinvar_cmds.py, rules_cmds.py, reeval_cmds.py, demo_cmds.py, user_cmds.py
  services/
    vcf_parser.py              F1  patient VCF validation + parsing (cyvcf2)          (KB WP)
    clinvar.py                 F2  ClinVar VCF import / writer / diff helpers          (KB WP)
    classification.py          F2  significance normalisation, classification lookup   (KB WP)
    rules.py                   F3  rule definitions, pure Algorithm 1                  (Rules WP)
    alerting.py                F3  DB reconciliation of findings → alerts/notes, rule import (Rules WP)
    patient_records.py         F1  patient / diagnosis / medication / VCF-ingest service (Reeval WP)
    reevaluation.py            F5  change-driven + full re-evaluation, point-in-time   (Reeval WP)
    fhir_export.py             F4  FHIR R4 Bundle                                       (FHIR WP)
  auth/        routes.py       login, logout, change password                (Foundation)
  admin/       routes.py       users, audit log                              (Foundation; UI-B may polish the audit page)
  main/        routes.py       dashboard, about                              (UI-B WP)
  patients/    routes.py       F1 pages, VCF upload, recommendations         (UI-A WP)
  alerts/      routes.py       alerts inbox, alert detail/review, notices    (UI-A WP)
  knowledge/   routes.py       releases, rules, re-evaluation runs           (UI-B WP)
  api/         routes.py       GET /patients/<id>/fhir, /health              (FHIR WP)
  demo.py                      seed_demo()                                   (Scenario WP)
  templates/  static/
data/
  rules/ruleset_v1.json  ruleset_v2.json                                    (Rules WP)
  demo/clinvar/demo_release_1.vcf.gz demo_release_2.vcf.gz                  (KB WP)
  demo/expert_panel_overlay_example.csv                                     (KB WP)
  demo/patients.json  demo/vcfs/*.vcf                                       (Cohort WP)
  cohorts/                     compact Synthea-derived cohorts               (Cohort WP)
evaluation/                    evaluation harness (§10)                      (Cohort WP + Eval WP)
scripts/                       fetch_clinvar.py, build_demo_releases.py, run_synthea.sh, ...
tests/                         pytest suite
docs/                          SPEC.md (this), ARCHITECTURE.md, USER_GUIDE.md, DEPLOYMENT.md, EVALUATION.md
```

---

## 2. Constants (`ge_ehr/constants.py`)

```python
GENES = ("GCK", "HNF1A", "HNF4A", "HNF1B", "KCNJ11", "ABCC8", "INS")

@dataclass(frozen=True)
class GeneInfo:
    symbol: str; hgnc_id: str; chrom: str; start: int; end: int  # padded GRCh38 panel region, 1-based inclusive
    transcript: str; refseq_chrom: str

GENE_INFO = {
  "GCK":    GeneInfo("GCK",    "HGNC:4195",  "7",  44_140_000,  44_200_000,  "NM_000162.5", "NC_000007.14"),
  "HNF1A":  GeneInfo("HNF1A",  "HGNC:11621", "12", 120_970_000, 121_010_000, "NM_000545.6", "NC_000012.12"),
  "HNF4A":  GeneInfo("HNF4A",  "HGNC:5024",  "20", 44_350_000,  44_440_000,  "NM_175914.4", "NC_000020.11"),
  "HNF1B":  GeneInfo("HNF1B",  "HGNC:11630", "17", 37_680_000,  37_750_000,  "NM_000458.4", "NC_000017.11"),
  "KCNJ11": GeneInfo("KCNJ11", "HGNC:6257",  "11", 17_380_000,  17_390_999,  "NM_000525.4", "NC_000011.10"),
  "ABCC8":  GeneInfo("ABCC8",  "HGNC:59",    "11", 17_391_000,  17_480_000,  "NM_000352.6", "NC_000011.10"),
  "INS":    GeneInfo("INS",    "HGNC:6081",  "11", 2_155_000,   2_163_500,   "NM_000207.3", "NC_000011.10"),
}
def gene_for_position(chrom: str, pos: int) -> str | None   # normalises "chr" prefix

# GRCh38 vs GRCh37 contig lengths used to reject GRCh37 VCFs
GRCH38_CONTIG_LENGTHS = {"7": 159_345_973, "11": 135_086_622, "12": 133_275_309, "17": 83_257_441, "20": 64_444_167}
GRCH37_CONTIG_LENGTHS = {"7": 159_138_663, "11": 135_006_516, "12": 133_851_895, "17": 81_195_210, "20": 63_025_520}

CLASSES = ("P", "LP", "VUS", "LB", "B", "NC")
CLASS_LABELS = {"P": "Pathogenic", "LP": "Likely pathogenic", "VUS": "Uncertain significance",
                "LB": "Likely benign", "B": "Benign", "NC": "Not classified"}
CLASS_ORDER = {"P": 5, "LP": 4, "VUS": 3, "LB": 2, "B": 1, "NC": 0}
ACTIONABLE = frozenset({"P", "LP"})
def band(cls: str) -> str        # "actionable" | "non_actionable"
def is_actionable(cls: str) -> bool

CHANGE_CATEGORIES = ("upgrade", "downgrade", "within_actionable", "within_non_actionable")
def categorize_change(old_cls: str, new_cls: str) -> str | None
    # None if old == new; upgrade: non_actionable -> actionable; downgrade: actionable -> non_actionable;
    # within_actionable: P<->LP; within_non_actionable: any change among VUS/LB/B/NC

MECHANISMS = ("activating", "inactivating", "unknown")
DRUG_CLASSES = ("insulin", "metformin", "sulfonylurea", "other")      # all are glucose-lowering
DRUG_CLASS_LABELS = {...}
SEVERITIES = ("high", "medium", "low", "info");  SEVERITY_ORDER = {"high": 3, "medium": 2, "low": 1, "info": 0}
ROLES = ("admin", "clinician", "lab_technician");  ROLE_LABELS = {...}
ALERT_STATUSES = ("active", "reviewed", "superseded");  CURRENT_ALERT_STATUSES = ("active", "reviewed")
FINDING_KINDS = ("alert", "note")
TRIGGERS = ("vcf_upload", "medication_change", "diagnosis_change", "clinvar_release",
            "rule_revision", "full_recompute", "manual")
DIABETES_TYPES = ("type1", "type2", "monogenic", "neonatal", "gestational", "other", "none", "unknown")
ZYGOSITIES = ("heterozygous", "homozygous", "hemizygous")
SEXES = ("female", "male", "other", "unknown")

# FHIR / LOINC codes (see §9)
LOINC = "http://loinc.org"
CLASS_TO_LOINC = {"P": ("LA6668-3", "Pathogenic"), "LP": ("LA26332-9", "Likely pathogenic"),
                  "VUS": ("LA26333-7", "Uncertain significance"), "LB": ("LA26334-5", "Likely benign"),
                  "B": ("LA6675-8", "Benign")}
ZYGOSITY_TO_LOINC = {"heterozygous": ("LA6706-1", "Heterozygous"), "homozygous": ("LA6705-3", "Homozygous"),
                     "hemizygous": ("LA6707-9", "Hemizygous")}
```

---

## 3. Data model (`ge_ehr/models.py`)

All tables have an integer primary key `id`. `utcnow()` returns naive UTC. Relationship names are
normative. Add sensible indexes on all FKs and on the lookup columns named below.

| Model | Fields (beyond `id`) |
|---|---|
| **User** (`users`) | `username` (unique, ≤64), `full_name`, `email` (nullable), `password_hash`, `role` (ROLES), `active` (bool, default True; expose `is_active` property for Flask-Login), `must_change_password` (bool), `failed_logins` (int), `locked_until` (datetime nullable), `created_at`, `last_login_at`. Methods: `set_password(pw)`, `check_password(pw)`, `has_role(*roles)`. |
| **AuditLog** (`audit_log`) | `at`, `user_id` (nullable FK), `username` (snapshot), `action` (str, e.g. `login`, `login_failed`, `vcf_upload_accepted`, `vcf_upload_rejected`, `medication_added`, `medication_stopped`, `diagnosis_updated`, `alert_reviewed`, `notice_acknowledged`, `release_imported`, `release_activated`, `rules_imported`, `reevaluation_run`, `access_denied`, `fhir_export`, `user_created`, ...), `entity_type`, `entity_id`, `detail` (JSON), `ip_address`. |
| **Patient** (`patients`) | `mrn` (unique), `given_name`, `family_name`, `sex` (SEXES), `birth_date` (date), `family_id` (nullable, indexed — relatives share it), `external_id` (nullable — e.g. Synthea UUID), `notes` (text), `is_synthetic` (bool, default True), `created_at`, `updated_at`. Relationships: `diagnosis` (one-to-one, `uselist=False`), `medications`, `variants` (PatientVariant), `uploads` (GenomicUpload), `alerts`, `review_notices`. Properties: `display_name`, `age_years` (int, from birth_date, today UTC), `active_medications`, `current_drug_classes` (frozenset). |
| **Diagnosis** (`diagnoses`) | `patient_id` (unique FK), `diabetes_type` (DIABETES_TYPES), `age_at_diagnosis_months` (int nullable), `diagnosed_on` (date nullable), `notes`, `updated_at`. |
| **Medication** (`medications`) | `patient_id`, `drug_name`, `drug_class` (DRUG_CLASSES), `dose` (nullable), `start_date` (date nullable), `end_date` (date nullable), `created_at`, `created_by_id` (nullable FK users). Property `is_active` = `end_date is None`. Only diabetes therapy is recorded. |
| **GenomicUpload** (`genomic_uploads`) | `patient_id`, `filename`, `sha256`, `size_bytes`, `sample_id` (nullable), `uploaded_by_id` (nullable), `uploaded_at`, `status` (`accepted`/`rejected`), `error` (text nullable), `total_records`, `panel_variants`, `off_panel_skipped`, `non_pass_skipped`, `ref_calls_skipped`. Rejected uploads are stored (no variants) for audit. |
| **VariantKey** (`variant_keys`) | `chrom` (str, no `chr` prefix, `MT` for mito), `pos` (int, 1-based), `ref`, `alt` (upper-case), `gene` (nullable). `UniqueConstraint(chrom,pos,ref,alt)`. Property `label` → `"7-44145200-C-T"`. Classmethod `get_or_create(chrom,pos,ref,alt,gene=None)` (normalises; flushes). |
| **PatientVariant** (`patient_variants`) | `patient_id`, `variant_key_id`, `upload_id` (latest upload that reported it), `zygosity` (ZYGOSITIES), `gene`, `current_classification` (CLASSES), `classification_release_id` (nullable FK kb_releases — release behind `current_classification`), `reported_classification` (class at first evaluation), `reclassified_count` (int, default 0), `last_reclassified_at` (nullable), `last_reclassified_run_id` (nullable FK), `created_at`. `UniqueConstraint(patient_id, variant_key_id)`. Relationships `patient`, `variant_key`, `upload`. Property `is_amended` = `reclassified_count > 0`. |
| **KbRelease** (`kb_releases`) | `label`, `source` (`clinvar` / `synthetic`), `release_date` (date), `file_name`, `file_sha256`, `file_date` (str from `##fileDate`), `imported_at`, `imported_by_id`, `record_count`, `gene_counts` (JSON), `class_counts` (JSON), `is_current` (bool), `activated_at` (nullable), `sequence` (int nullable — assigned 1,2,3,… when activated; total order of activation), `is_synthetic` (bool), `notes`. Classmethod `current()`. Each ClinVar import is one KbRelease. |
| **KnownVariant** (`known_variants`) | `release_id`, `variant_key_id`, `clinvar_variation_id` (int nullable — VCF ID column), `allele_id` (int nullable), `gene`, `classification` (CLASSES), `clinvar_significance` (raw CLNSIG), `review_status` (raw CLNREVSTAT), `review_stars` (0–4), `is_expert_panel` (bool), `classification_source` (`clinvar` / `expert_panel_overlay`), `condition` (CLNDN, text), `mechanism` (MECHANISMS), `hgvs` (CLNHGVS, text), `molecular_consequence` (MC, text). `UniqueConstraint(release_id, variant_key_id)`. |
| **Rule** (`rules`) | `code` (unique, `D1`…`D6`), `title`, `description`. Relationship `versions`. |
| **RuleVersion** (`rule_versions`) | `rule_id`, `code` (denormalised), `version` (int), `definition` (JSON — §6.1), `genes` (JSON list), `severity`, `title`, `recommendation`, `source`, `kind` (`treatment`/`cascade`/`monitoring`), `is_current` (bool), `rule_set_label`, `change_note`, `created_at`, `imported_by_id`. `UniqueConstraint(rule_id, version)`. Classmethod `current_versions()` → list ordered by code. |
| **Alert** (`alerts`) | `patient_id`, `patient_variant_id` (nullable), `variant_key_id`, `rule_code`, `created_rule_version_id`, `confirmed_rule_version_id`, `kind` (`alert`/`note`), `severity` (SEVERITIES; NULL for notes), `title`, `message` (recommendation or note text), `source`, `classification` (class at creation), `status` (ALERT_STATUSES, default `active`), `trigger` (TRIGGERS), `is_reclassified` (bool), `priority` (`normal`/`high`), `notify` (bool — counts as a clinician notification), `created_release_id` (nullable), `confirmed_release_id` (nullable), `created_run_id` (nullable), `superseded_run_id` (nullable), `created_at`, `superseded_at`, `superseded_reason` (`downgrade`/`reclassified`/`therapy_changed`/`diagnosis_changed`/`rule_revised`/`variant_removed`), `reviewed_at`, `reviewed_by_id`, `review_comment`, `review_notice_id` (nullable FK — the notice that reported its supersession). Relationships `patient`, `variant_key`, `patient_variant`, `created_rule_version`, `confirmed_rule_version`, `created_release`, `confirmed_release`, `events`, `review_notice`, `reviewed_by`. **Alerts are never deleted.** Invariant: at most one *current* (status ∈ active/reviewed) alert per `(patient_id, rule_code, variant_key_id, kind)`. |
| **AlertEvent** (`alert_events`) — provenance | `alert_id`, `event` (`created`/`confirmed`/`reviewed`/`superseded`), `release_id`, `rule_version_id`, `classification`, `run_id`, `user_id`, `at`, `detail` (text). |
| **ReviewNotice** (`review_notices`) | `patient_id`, `variant_key_id`, `run_id`, `old_classification`, `new_classification`, `message`, `status` (`open`/`acknowledged`), `created_at`, `acknowledged_at`, `acknowledged_by_id`, `acknowledgement_comment`. Relationship `alerts` (back-populates `Alert.review_notice`). |
| **ReevaluationRun** (`reevaluation_runs`) | `trigger` (TRIGGERS), `strategy` (`change_driven`/`full`/`patient`), `old_release_id`, `new_release_id`, `rule_change` (JSON list of `{code, old_version, new_version}`), `started_at`, `finished_at`, `duration_ms` (float), `status` (`running`/`success`/`failed`), `error`, `initiated_by_id`, counters (int, default 0): `stored_keys_compared`, `changed_variants`, `carriers_affected`, `patients_reevaluated`, `records_examined`, `alerts_created`, `alerts_superseded`, `alerts_confirmed`, `notes_created`, `review_notices`, `notifications`; `summary` (JSON — per-category counts etc.). Relationship `changes`. |
| **VariantChange** (`variant_changes`) | `run_id`, `variant_key_id`, `gene`, `old_classification`, `new_classification`, `old_mechanism`, `new_mechanism`, `category` (CHANGE_CATEGORIES), `carrier_count`. |

---

## 4. Patient VCF parsing — F1 (`services/vcf_parser.py`)

```python
class VcfError(ValueError):            # message is user-facing; .line (int|None)
@dataclass(frozen=True)
class ParsedVariant: chrom: str; pos: int; ref: str; alt: str; zygosity: str; gene: str; filter: str; genotype: str
@dataclass
class ParsedVcf:
    sample_id: str | None; fileformat: str; variants: list[ParsedVariant]
    total_records: int; off_panel_skipped: int; non_pass_skipped: int; ref_calls_skipped: int
    warnings: list[str]
def parse_patient_vcf(data: bytes, filename: str = "upload.vcf") -> ParsedVcf
```

* Accept plain text or gzip/bgzip (`.vcf`, `.vcf.gz`); max size enforced by the route (20 MB).
* **Validate first in pure Python** (htslib can crash or silently truncate on bad input), then
  parse with **cyvcf2** (write to a temp file). Reject (raise `VcfError` with line number) when:
  first line is not `##fileformat=VCFv4.1|4.2|4.3`; no `#CHROM` header; header has fewer than
  10 columns (needs FORMAT + ≥1 sample); a data line has the wrong column count; POS not a positive
  integer; REF not `^[ACGTN]+$`; ALT not a comma list of `^[ACGTN]+$` / `*` / `.` (symbolic alleles
  like `<DEL>` are skipped with a warning, not rejected); no `GT` in FORMAT; file has zero data
  lines is **accepted** (0 variants) with a warning; assembly is GRCh37/hg19 (via `##reference=`
  containing `GRCh37`/`hg19`/`b37`, or `##contig` lengths matching `GRCH37_CONTIG_LENGTHS`) →
  reject with "GRCh38 required"; undecodable bytes → reject.
* First sample only (record `sample_id`; warn if more samples). Split multi-allelic records.
  Zygosity from GT: one copy of alt → `heterozygous`, two → `homozygous`, haploid alt →
  `hemizygous`; no alt allele (0/0, ./., 0) → skipped, counted in `ref_calls_skipped`.
* Keep FILTER `PASS` or `.` only (others counted in `non_pass_skipped`).
* Keep only variants inside the 7-gene panel (`gene_for_position`); others counted in
  `off_panel_skipped`. Chromosome names normalised (`chr7` → `7`, `chrM`/`M` → `MT`).
* Variant key = (chrom, pos, ref, alt) upper-case, GRCh38. No left-normalisation (exact matching —
  a documented limitation in the paper).

---

## 5. Knowledge base — F2 (`services/clinvar.py`, `services/classification.py`)

### 5.1 Significance normalisation (`classification.py`)

```python
def normalize_clinvar_significance(clnsig: str | None) -> str      # → one of CLASSES
def review_stars(clnrevstat: str | None) -> int
def is_expert_panel_status(clnrevstat: str | None) -> bool          # reviewed_by_expert_panel | practice_guideline
def infer_mechanism(gene: str, condition: str | None) -> str        # MECHANISMS
```

* CLNSIG mapping (case-insensitive; take the primary term before `|` — secondary terms such as
  `risk_factor` are ignored; `_` treated as space):
  `Pathogenic` → P; `Likely_pathogenic` → LP; `Pathogenic/Likely_pathogenic` → **LP** (conservative,
  still actionable); `Pathogenic,_low_penetrance` → P; `Likely_pathogenic,_low_penetrance` → LP;
  `Uncertain_significance` → VUS; any `Conflicting_*` (both the old *interpretations* and the new
  *classifications* wording) → **VUS**; `Likely_benign` → LB; `Benign` → B; `Benign/Likely_benign` → LB;
  everything else (`not_provided`, `drug_response`, `risk_factor`, `association`, `protective`,
  `Affects`, `other`, `no_classification_for_the_single_variant`, `Uncertain_risk_allele`, missing…) → NC.
* Review stars: `practice_guideline` 4; `reviewed_by_expert_panel` 3;
  `criteria_provided,_multiple_submitters,_no_conflicts` 2; `criteria_provided,_single_submitter`
  and `criteria_provided,_conflicting_*` 1; anything else 0.
* Mechanism (only meaningful for KCNJ11/ABCC8; other genes → `unknown`): condition mentions
  neonatal diabetes / diabetes (regex `neonatal|diabetes`) and not hyperinsulinism → `activating`;
  mentions `hyperinsulin|hypoglyc` and not diabetes → `inactivating`; otherwise `unknown`.
  Rationale: activating KATP-channel variants cause neonatal diabetes, inactivating ones cause
  congenital hyperinsulinism; rule D4 requires activating variants.

### 5.2 Classification lookup (`classification.py`)

```python
@dataclass(frozen=True)
class KnownClass: classification: str; mechanism: str; known_variant_id: int | None
                  is_expert_panel: bool; review_status: str | None; review_stars: int; condition: str | None
NOT_CLASSIFIED = KnownClass("NC", "unknown", None, False, None, 0, None)
def classify_keys(release: KbRelease | None, variant_key_ids: Iterable[int]) -> dict[int, KnownClass]
    # one bulk query; keys absent from the release → NOT_CLASSIFIED ("not classified", NOT VUS)
def classified_variants_for_patient(patient: Patient, release: KbRelease | None,
                                    variant_key_ids: Iterable[int] | None = None) -> list[ClassifiedVariant]
    # ClassifiedVariant is defined in services/rules.py (§6.2)
```

Expert-panel precedence: in ClinVar's aggregate VCF the CLNSIG of an expert-panel-reviewed
variant already reflects the panel; we flag `is_expert_panel`. Additionally an optional **expert
panel overlay CSV** (columns `clinvar_variation_id,classification,panel,date`; classification as
code `P`/`LP`/… or text `Pathogenic`/…) can be applied at import: matching KnownVariants get
`classification` overridden, `is_expert_panel=True`, `classification_source="expert_panel_overlay"`.

### 5.3 ClinVar import (`clinvar.py`)

```python
@dataclass
class ClinVarRecord: chrom, pos, ref, alt, variation_id, allele_id, gene, clnsig, clnrevstat,
                     clndn, clnhgvs, mc   # raw-ish fields
def iter_clinvar_records(path: str | os.PathLike) -> Iterator[ClinVarRecord]
    # cyvcf2; keeps only records whose GENEINFO lists a panel gene OR whose position is in a panel
    # region; gene = the panel gene from GENEINFO (if several, the one whose region contains pos);
    # skips ALT "."/symbolic; splits multi-allelic
def read_vcf_header_meta(path) -> dict   # fileDate, source, ge_ehr_synthetic flag
def import_release(path, *, label: str | None = None, release_date: date | None = None,
                   expert_panel_csv: str | os.PathLike | None = None, user: User | None = None,
                   notes: str | None = None) -> KbRelease
    # creates KbRelease (is_current=False) + KnownVariants (bulk insert, batch 1000), VariantKeys
    # get_or_create in bulk; computes record_count/gene_counts/class_counts; sha256; source/is_synthetic
    # from header; label default "ClinVar <fileDate>"; release_date default from fileDate.
    # Refuses a duplicate file (same sha256 already imported) with ValueError.
def write_clinvar_vcf(records: Iterable[dict | ClinVarRecord], path, *, file_date: str,
                      synthetic: bool = True) -> None
    # writes a ClinVar-format VCF 4.1 (bgzip/gzip if path ends .gz) with the INFO fields above,
    # sorted by chrom/pos; synthetic=True adds the two mandatory synthetic header lines (§0)
def diff_releases(old: KbRelease | None, new: KbRelease, variant_key_ids: Iterable[int] | None = None)
        -> list[KeyChange]
    # KeyChange(variant_key_id, gene, old_class, new_class, old_mechanism, new_mechanism, category)
    # restricted to variant_key_ids if given; includes keys whose class OR mechanism changed;
    # category from categorize_change(old,new); class unchanged but mechanism changed →
    # "within_actionable" if actionable else "within_non_actionable"
```

`scripts/fetch_clinvar.py` downloads real ClinVar GRCh38 VCFs (current:
`https://ftp.ncbi.nlm.nih.gov/pub/clinvar/vcf_GRCh38/clinvar.vcf.gz`; archived:
`.../vcf_GRCh38/archive_2.0/<YEAR>/clinvar_<YYYYMMDD>.vcf.gz`), streams them and writes 7-gene
subsets to `data/clinvar/real/clinvar_<date>_7genes.vcf.gz` (git-ignored). Used by the scheduled job
and by the real-release-pair evaluation when NCBI is reachable.

CLI (`commands/clinvar_cmds.py`, group `clinvar`):
`flask clinvar import PATH [--label] [--date YYYY-MM-DD] [--expert-panel CSV] [--activate/--no-activate] [--strategy change_driven|full]`
(activation calls `reevaluation.activate_release`), `flask clinvar activate RELEASE_ID [--strategy]`,
`flask clinvar list`, `flask clinvar sync [--url URL] [--strategy]` (download latest, skip if
`fileDate` ≤ current release's, subset, import, activate — this is the scheduled job).

---

## 6. Decision support — F3 (`services/rules.py`, `services/alerting.py`)

### 6.1 Rule definition JSON (stored in `RuleVersion.definition`, files `data/rules/*.json`)

```json
{
  "rule_set": "GE-EHR monogenic diabetes rules",
  "label": "v2 (2026)",
  "rules": [
    {
      "code": "D2", "version": 1, "kind": "treatment",
      "title": "HNF1A diabetes on insulin or metformin",
      "genetic": {"genes": ["HNF1A"], "classes": ["P", "LP"], "mechanism": null},
      "clinical": {"any_of_drug_classes": ["insulin", "metformin"], "none_of_drug_classes": ["sulfonylurea"],
                   "max_age_at_diagnosis_months": null},
      "severity": "high",
      "recommendation": "Consider low-dose sulfonylurea as first-line treatment.",
      "source": "Kang et al. 2021; Naylor et al. 2024 (ADA/EASD PMDI)",
      "change_note": "Initial version"
    }
  ]
}
```

`clinical: null` means "no therapy condition". Semantics of `clinical` (all present keys must hold):
`any_of_drug_classes` — patient's current drug classes intersect the list; `none_of_drug_classes` —
no current drug class in the list; `max_age_at_diagnosis_months` — diagnosis age known and
`< value`. `genetic.mechanism` (nullable) — variant mechanism must equal it.

**Current rules (Table III).** `ruleset_v2.json` holds the current versions; `ruleset_v1.json`
is identical except D4 version 1 covers KCNJ11 only.

| Code | Genetic condition | Clinical condition | Severity | Recommendation (verbatim intent) | Source |
|---|---|---|---|---|---|
| D1 v1 | GCK P/LP | any_of all four drug classes | medium | "Medication usually not required outside pregnancy; review whether glucose-lowering treatment can be stopped." | Kang 2021 [1]; De Sousa 2025 [4] |
| D2 v1 | HNF1A P/LP | any_of insulin, metformin; none_of sulfonylurea | high | "Consider low-dose sulfonylurea as first-line treatment." | Kang 2021 [1]; Naylor 2024 [7] |
| D3 v1 | HNF4A P/LP | same as D2 | medium | "Consider low-dose sulfonylurea (supporting evidence is more limited than for HNF1A)." | [1], [7] |
| D4 v1 | KCNJ11 P/LP, mechanism activating | any_of insulin; max_age_at_diagnosis_months 12 (diagnosed in infancy) | high | "Refer to specialist to consider transfer to high-dose sulfonylurea." | [1] |
| D4 v2 | KCNJ11 **or ABCC8** P/LP, activating | same | high | same | [1] |
| D5 v1 | any of the 7 genes P/LP | null | low | "Offer genetic counselling and cascade testing of first-degree relatives (50% risk, autosomal dominant inheritance)." | Autosomal dominant inheritance |
| D6 v1 | GCK, HNF1A or HNF4A VUS | null | info | "No treatment change; variant flagged and monitored for reclassification." | ClinGen MDEP [5]; ACMG/AMP [8] |

All recommendations end up displayed with the disclaimer "Advisory — for specialist review".

### 6.2 Pure evaluation — Algorithm 1 (`services/rules.py`, no DB access)

```python
class RuleDefinitionError(ValueError)
@dataclass(frozen=True)
class RuleSpec: rule_version_id: int | None; code; version; title; kind; genes: frozenset[str]
                classes: frozenset[str]; mechanism: str | None; clinical: Mapping | None
                severity; recommendation; source
    @classmethod from_definition(defn: dict, rule_version_id=None) -> RuleSpec   # validates strictly
@dataclass(frozen=True)
class ClassifiedVariant: patient_variant_id: int | None; variant_key_id: int; gene; classification
                         mechanism; zygosity; label: str = ""; is_expert_panel: bool = False
@dataclass(frozen=True)
class ClinicalContext: drug_classes: frozenset[str]; age_at_diagnosis_months: int | None; diabetes_type: str | None
@dataclass(frozen=True)
class Finding: rule_code; rule_version_id; variant_key_id; patient_variant_id; kind ("alert"|"note")
               severity (str | None); title; message; source; classification
    key -> tuple (rule_code, variant_key_id, kind, severity)
def validate_rule_set(doc: dict) -> list[dict]          # raises RuleDefinitionError with a precise message
def clinical_condition_holds(clinical, ctx) -> tuple[bool, str]   # (holds, human-readable reason)
def evaluate(variants, ctx, rules) -> list[Finding]     # Algorithm 1, deterministic order (code, variant)
def describe_clinical_condition(clinical) -> str        # human readable, used by UI
```

Algorithm 1 for patient *p*, release *K*, rule set *R*: for each rule *r* and each classified
variant *v* of *p* with `v.gene ∈ r.genes`, `v.classification ∈ r.classes` and mechanism match:
if `r.clinical` is null or holds → **alert** finding (severity, recommendation, source);
otherwise → **note** finding (severity `None`) whose message explains which clinical condition
failed, e.g. *"Genetic criterion of D2 met (HNF1A likely pathogenic) but therapy condition not met:
already receiving a sulfonylurea. No alert raised."* Alerts are per (patient, rule, variant).

### 6.3 Reconciliation (`services/alerting.py`)

```python
def load_current_rules() -> list[RuleSpec]          # from RuleVersion.is_current
def clinical_context(patient) -> ClinicalContext
@dataclass
class ReconcileResult: created: list[Alert]; superseded: list[Alert]; confirmed: list[Alert]
                       unchanged: list[Alert]; records_examined: int
def reconcile_patient(patient, release, rules, *, trigger: str, variant_key_ids=None, rule_codes=None,
                      run=None, superseded_reason: str = "therapy_changed", user=None,
                      now=None, classified=None) -> ReconcileResult
def evaluate_patient_now(patient, *, trigger: str, user=None, superseded_reason: str = "therapy_changed") -> ReconcileResult
    # current release + current rules, full scope for that patient (used after VCF upload,
    # medication change, diagnosis change)
def import_rule_versions(doc: dict, *, user=None) -> list[dict]
    # validate_rule_set(doc); create Rule rows as needed and RuleVersion rows for unseen
    # (code, version); an existing (code, version) whose definition differs → RuleDefinitionError;
    # the highest version per code becomes is_current (others False); returns the list of
    # {"code", "old_version", "new_version", "old_rule_version_id", "new_rule_version_id"} for
    # codes whose current version changed (old_* None for brand-new rules). No re-evaluation here.
```

`reconcile_patient` computes desired findings (restricted to `variant_key_ids` / `rule_codes`
when given) and compares with the patient's *current* alerts/notes in the same scope, matching on
`Finding.key` = `(rule_code, variant_key_id, kind, severity)`:
* desired & not current → create Alert (+ AlertEvent `created`), `created_release_id =
  confirmed_release_id = release.id`, `created_rule_version_id = confirmed_rule_version_id`,
  `trigger`, `created_run_id = run.id`; `notify = (kind == "alert" and severity != "info")`.
* current & not desired → status `superseded`, `superseded_at`, `superseded_reason`,
  `superseded_run_id`, AlertEvent `superseded`. Never deleted.
* in both → if release or rule version differ from confirmed → update `confirmed_*` + AlertEvent
  `confirmed` (counts as provenance update); else unchanged. A `reviewed` alert stays `reviewed`.
* `records_examined` = number of (patient-variant × in-scope) records evaluated (= number of
  classified variants passed to `evaluate`).

### 6.4 Patient record service (`services/patient_records.py`)

```python
def create_patient(*, mrn, given_name, family_name, sex, birth_date, family_id=None, notes=None,
                   diabetes_type="unknown", age_at_diagnosis_months=None, diagnosed_on=None,
                   external_id=None, user=None) -> Patient
def update_patient(patient, *, user=None, **fields) -> Patient
def update_diagnosis(patient, *, diabetes_type, age_at_diagnosis_months, diagnosed_on=None, notes=None,
                     user=None) -> ReconcileResult    # triggers evaluate_patient_now(trigger="diagnosis_change", superseded_reason="diagnosis_changed")
def add_medication(patient, *, drug_name, drug_class, dose=None, start_date=None, user=None) -> tuple[Medication, ReconcileResult]
def stop_medication(medication, *, end_date=None, user=None) -> ReconcileResult
def ingest_vcf(patient, data: bytes, filename: str, *, user=None) -> tuple[GenomicUpload, ReconcileResult | None]
    # parse (VcfError → rejected GenomicUpload stored, audit 'vcf_upload_rejected', re-raise VcfError);
    # upsert PatientVariants (union across uploads; latest upload wins zygosity); classify NEW
    # PatientVariants under the current release (current_/reported_classification,
    # classification_release_id); then evaluate_patient_now(trigger="vcf_upload")
```
`suggest_drug_class(drug_name) -> str` lives in `constants.py` (Foundation) — keyword mapping incl.
common generic and brand names (insulin*, metformin, glipizide, glyburide/glibenclamide,
glimepiride, gliclazide, tolbutamide, chlorpropamide → sulfonylurea; everything else → other).

Medication / diagnosis changes run F1–F3 for that patient only (paper §3.1). Superseded alerts
from therapy changes do **not** raise review notices.

---

## 7. Re-evaluation engine — F5 (`services/reevaluation.py`) — main contribution

```python
def activate_release(new_release, *, strategy="change_driven", user=None) -> ReevaluationRun
def apply_rule_set(doc: dict, *, user=None, strategy="change_driven") -> ReevaluationRun
    # alerting.import_rule_versions(doc) (validation; existing (code,version) must be identical);
    # then re-evaluate the carriers of every revised rule (§7.3)
def full_recompute(*, user=None, trigger="full_recompute") -> ReevaluationRun
def alerts_as_of(patient, at: datetime) -> list[Alert]          # point-in-time view (§7.5)
def release_history() -> list[KbRelease]                         # activated releases by sequence
```

### 7.1 Algorithm 2 — change-driven re-evaluation on a new release
1. `K_old = KbRelease.current()` (may be None ⇒ everything NC); `V_obs` = distinct
   `variant_key_id` of all PatientVariants. `stored_keys_compared = |V_obs|`.
2. `Δ = diff_releases(K_old, K_new, V_obs)` — only stored keys are compared (work scales with
   changed variants and their carriers, not with the size of ClinVar). Variants carried by no
   patient are never examined (RS7). Store one VariantChange per element of Δ.
3. For each changed key *v* with category *c*, for each carrier *p*: update the PatientVariant
   (`current_classification`, `classification_release_id`, `reclassified_count += 1` when the class
   changed, `last_reclassified_*`), then `reconcile_patient(p, K_new, rules, variant_key_ids={v},
   trigger="clinvar_release", run=run, superseded_reason=…)` and apply category semantics:
   * **upgrade** (into actionable band): created alerts get `is_reclassified=True`,
     `priority="high"`, `notify=True` unless severity `info`; superseded ones (e.g. the D6 VUS
     monitoring alert) get reason `reclassified`, no notice.
   * **downgrade** (out of actionable band): superseded alerts get reason `downgrade`; if any
     superseded alert has severity ≠ info, create **one ReviewNotice** per (patient, variant, run)
     linking them ("…downgraded from LP to VUS in <release>; alert(s) D1, D5 superseded. Review any
     treatment change made on the basis of this alert; GE-EHR never reverses therapy
     automatically."). Created alerts (e.g. D6 monitoring) are silent (`notify=False`). The FHIR
     export now reports the variant as `amended`.
   * **within actionable / within non-actionable**: still-firing alerts → provenance updated
     (`confirmed_release_id`, AlertEvent `confirmed`); no notification. Findings that stop
     applying (e.g. D6 after VUS→LB) are superseded silently (reason `reclassified`); findings that
     newly apply because only the mechanism changed are created with `notify=True` (outcome changed).
4. Set `K_new.is_current=True`, `sequence = max+1`, `activated_at`; previous current → False. Log
   the run with all counters and `duration_ms`.
5. **First activation** (no previous current release): this is an *initial classification*, not a
   reclassification. All carriers of stored keys whose class ≠ NC are reconciled normally; created
   alerts are **not** flagged `is_reclassified` (priority normal, `notify` as in §6.3); PatientVariants
   with `classification_release_id IS NULL` get `reported_classification = current_classification =
   new class`, `classification_release_id = K_new.id` and `reclassified_count` is **not** incremented.
   VariantChange rows are still recorded (category from NC → class) and `summary.initial = true`.

**PatientVariant classification bookkeeping (all code paths).** When a PatientVariant is first
classified against a release (`classification_release_id IS NULL`) set `reported_classification`
and do not increment `reclassified_count`. Afterwards, every change of `current_classification`
caused by a release activation increments `reclassified_count` and sets `last_reclassified_at` /
`last_reclassified_run_id` (this drives FHIR `amended`). Variants ingested while no release is
current get `current_classification = reported_classification = "NC"` and
`classification_release_id = NULL`.

### 7.2 Full recomputation (`strategy="full"`) — evaluation strategy 2
Same final state and the same notification semantics as 7.1, but every patient is reconciled
against `K_new` with full scope; the category used for flags/notices is computed per key from
`categorize_change(class_old, class_new)`. `records_examined` = all patient-variant records.

### 7.3 Rule revision (`apply_rule_set`)
For each code whose current version changed: affected genes = old.genes ∪ new.genes; carriers =
patients with a PatientVariant in those genes; `reconcile_patient(..., rule_codes={code},
trigger="rule_revision", superseded_reason="rule_revised")` under the current release. Outcome per
(patient, variant): still fires with same severity → provenance update (`confirmed_rule_version_id`)
without notification; newly fires → new alert `notify=True` (RS5); stops firing or severity changes →
supersede (+ ReviewNotice if old severity ≠ info) and create the new one if any. `strategy="full"`
reconciles all patients instead of carriers only. Importing a rule set identical to the current one
is a no-op run (changed 0).

### 7.4 Run bookkeeping
Every entry point creates a `ReevaluationRun` (status running → success), fills counters:
`changed_variants=|Δ|`, `carriers_affected` = distinct patients carrying a changed key,
`patients_reevaluated`, `records_examined` (sum from reconcile), `alerts_created`/`notes_created`,
`alerts_superseded`, `alerts_confirmed`, `review_notices`, `notifications` = created alerts with
`notify=True` + review notices; `summary` JSON = counts per category and per gene. Audit
`reevaluation_run`.

### 7.5 Point-in-time view
`alerts_as_of(patient, at)` returns the alerts/notes that were current at time `at`:
`created_at ≤ at` and (`superseded_at` is null or `superseded_at > at`). Each carries its release
and rule-version provenance, so "what did the system recommend on date X and on what evidence" is
reproducible.

### 7.6 Failure handling
On exception: `db.session.rollback()`, then record a ReevaluationRun with `status="failed"` and
the error text in a fresh transaction (commit), then re-raise. Release stays non-current.

---

## 8. Web application (roles, routes, UI)

### 8.1 Roles (enforced server-side with `@roles_required`, 403 page otherwise + audit `access_denied`)

| Capability | admin | clinician | lab_technician |
|---|:-:|:-:|:-:|
| Dashboard | ✔ | ✔ | ✔ (lab view: no alerts) |
| Patient list / view demographics & genomic profile | ✔ | ✔ | ✔ |
| Create/edit patient, diagnosis, medications | ✔ | ✔ | ✗ |
| Upload VCF | ✔ | ✔ | ✔ |
| **Recommendations / alerts / notes / review notices** | ✔ | ✔ | **✗ (403, S12)** |
| Review alert, acknowledge notice | ✔ | ✔ | ✗ |
| FHIR export | ✔ | ✔ | ✗ |
| Knowledge base & rules & runs — view | ✔ | ✔ | ✗ |
| Import/activate release, import rules, full recompute | ✔ | ✗ | ✗ |
| Users & audit log | ✔ | ✗ | ✗ |

### 8.2 Routes (blueprint → URL). HTML unless stated.

* auth: `GET/POST /login`, `POST /logout`, `GET/POST /account/password`.
  Login lockout: 5 failed attempts → locked 15 min. Generic error messages. `next` param validated
  (same-host relative URLs only).
* main: `GET /` dashboard; `GET /about` (system description, disclaimer, paper references).
* patients: `GET /patients` (search `q` on name/MRN, filters `alerts=1`, `family=<id>`, pagination);
  `GET/POST /patients/new`; `GET /patients/<id>` (overview + genomic profile + uploads; alert
  summary only for clinician/admin); `GET/POST /patients/<id>/edit`; `POST /patients/<id>/diagnosis`;
  `POST /patients/<id>/medications`; `POST /patients/<id>/medications/<mid>/stop`;
  `GET/POST /patients/<id>/vcf` (upload form; on `VcfError` re-render with the message and
  **HTTP 400**, nothing stored except the rejected upload record — S10);
  `GET /patients/<id>/recommendations` (alerts, notes, history with provenance, review notices,
  point-in-time selector `?at=ISO-datetime`) — **lab technician → 403** (S12).
* alerts: `GET /alerts` (inbox; filters `severity`, `status` (default current), `rule`,
  `reclassified=1`, pagination); `GET /alerts/<id>` (detail + provenance events);
  `POST /alerts/<id>/review` (comment; status → reviewed); `GET /notices`;
  `POST /notices/<id>/acknowledge`.
* knowledge: `GET /knowledge/releases`; `GET/POST /knowledge/releases/import` (admin; VCF upload,
  label, date, optional overlay CSV, checkbox "activate and re-evaluate now", strategy);
  `GET /knowledge/releases/<id>`; `POST /knowledge/releases/<id>/activate` (admin);
  `GET /knowledge/releases/compare?old=<id>&new=<id>` (diff limited to stored keys by default,
  `all=1` for all keys); `GET /knowledge/rules`; `GET/POST /knowledge/rules/import` (admin, JSON
  upload or paste); `GET /knowledge/runs`; `GET /knowledge/runs/<id>`;
  `POST /knowledge/recompute` (admin, full recompute).
* admin: `GET /admin/users`, `GET/POST /admin/users/new`, `POST /admin/users/<id>/toggle`,
  `POST /admin/users/<id>/reset-password`, `GET /admin/audit` (filters, pagination).
* api: `GET /patients/<id>/fhir` → `application/fhir+json` Bundle (`?download=1` → attachment);
  `GET /health` → `{"status":"ok","db":"ok","current_release":…}` (no auth).

**Endpoint names are normative** (the base layout links to them; Foundation creates placeholders
for every one so `url_for` always works, and the UI packages replace them keeping the names):
`auth.login`, `auth.logout`, `auth.change_password`, `main.dashboard`, `main.about`,
`patients.list_patients`, `patients.new_patient`, `patients.view_patient`, `patients.edit_patient`,
`patients.update_diagnosis`, `patients.add_medication`, `patients.stop_medication`,
`patients.upload_vcf`, `patients.recommendations`, `alerts.inbox`, `alerts.view_alert`,
`alerts.review_alert`, `alerts.notices`, `alerts.acknowledge_notice`, `knowledge.releases`,
`knowledge.import_release`, `knowledge.view_release`, `knowledge.activate_release`,
`knowledge.compare_releases`, `knowledge.rules`, `knowledge.import_rules`, `knowledge.runs`,
`knowledge.view_run`, `knowledge.recompute`, `admin.users`, `admin.new_user`, `admin.toggle_user`,
`admin.reset_password`, `admin.audit`, `api.patient_fhir`, `api.health`.

### 8.3 UI requirements
Server-rendered Jinja2 + **Bootstrap 5.3 and Bootstrap Icons vendored locally** under
`ge_ehr/static/vendor/` (no CDN — the CSP is `default-src 'self'`). Professional clinical look:
fixed top navbar (brand "GE-EHR · Monogenic Diabetes"), role-aware nav with badge counts (current
high/medium alerts, open notices), user menu with role badge; a persistent amber banner
**"Synthetic data only · Research prototype · Not for clinical use"**; flash messages; consistent
badges — classification (P dark red, LP red-orange, VUS amber, LB teal, B green, NC grey), severity
(high red, medium orange, low blue, info grey), status, "Reclassified" (purple), "Expert panel"
(shield icon), "Amended"; tables with empty states; forms with labels, help text and validation
messages; confirm dialogs on destructive/major actions; responsive down to 375 px; WCAG AA
contrast; error pages 400/403/404/413/500 in the layout. Recommendations always show severity,
rule code+version, recommendation, source, classification, release label, and the advisory
disclaimer. Dates displayed as `YYYY-MM-DD HH:MM UTC`.

### 8.4 Security
CSRF on every POST (Flask-WTF `CSRFProtect`); passwords hashed with Werkzeug (scrypt); session
cookie `HttpOnly`, `SameSite=Lax`, `Secure` in production; `SECRET_KEY` required in production
(refuse to start without it); security headers (CSP `default-src 'self'; img-src 'self' data:;
style-src 'self'; script-src 'self'; frame-ancestors 'none'`, `X-Content-Type-Options: nosniff`,
`Referrer-Policy: same-origin`, `X-Frame-Options: DENY`); `MAX_CONTENT_LENGTH` 256 MB globally,
20 MB enforced for patient VCFs; uploads processed in memory/tempfiles, never served back;
audit log for security-relevant actions; no PHI (synthetic flag everywhere).

---

## 9. FHIR export — F4 (`services/fhir_export.py`)

```python
def build_patient_bundle(patient, *, base_url: str | None = None) -> dict   # JSON-ready dict
def validate_bundle(bundle: dict) -> None   # parse with fhir.resources.R4B Bundle (+ each resource); raise ValueError
```

Bundle `type: "collection"`, `timestamp`, `meta.tag` HTEST (`http://terminology.hl7.org/CodeSystem/v3-ActReason`).
Resources (`fullUrl: urn:uuid:<deterministic uuid5>`):
* **Patient** — identifier MRN (`system: urn:ge-ehr:mrn`), name, gender, birthDate, `meta.tag` HTEST.
* Per PatientVariant a **variant Observation** — `meta.profile`
  `http://hl7.org/fhir/uv/genomics-reporting/StructureDefinition/variant`; `status` `final`, or
  **`amended`** when `PatientVariant.is_amended`; category `laboratory`
  (`http://terminology.hl7.org/CodeSystem/observation-category`) + `GE` (`http://terminology.hl7.org/CodeSystem/v2-0074`);
  code LOINC `69548-6` "Genetic variant assessment"; `valueCodeableConcept` LOINC `LA9633-4` "Present";
  components: `48018-6` gene studied (HGNC system `http://www.genenames.org/geneId`, code HGNC id,
  display symbol); `48013-7` genomic ref seq (`http://www.ncbi.nlm.nih.gov/refseq`, e.g. `NC_000012.12`);
  `62374-4` reference assembly `LA26806-2` GRCh38; `92822-6` coordinate system `LA30102-0`
  "1-based character counting"; `81254-5` exact start-end (`valueRange.low.value = pos`);
  `69547-8` ref allele (`valueString`); `69551-0` alt allele; `53034-5` allelic state (zygosity LA code);
  `81290-9` genomic HGVS when known (from KnownVariant.hgvs, `valueCodeableConcept.text`).
* Per variant whose current class ≠ NC a **diagnostic-implication Observation** — profile
  `http://hl7.org/fhir/uv/genomics-reporting/StructureDefinition/diagnostic-implication`; code
  `{system: http://hl7.org/fhir/uv/genomics-reporting/CodeSystem/tbd-codes-cs, code: diagnostic-implication}`;
  `derivedFrom` → the variant Observation; component `53037-8` clinical significance (CLASS_TO_LOINC);
  component `81259-4` associated phenotype (`valueCodeableConcept.text` = condition) when known;
  `status` final/amended as above; `note` with provenance ("Classified against <release label>,
  review status …, expert panel yes/no; reclassified N time(s)").
Endpoint requires login (clinician/admin); audit `fhir_export`. The R4B models are used for
validation because fhir.resources 8 ships R4B (4.3) — structurally identical for these resources.

---

## 10. Synthetic data & evaluation (paper §5)

* **Cohorts.** Correctness cohort of 30 and scale cohort of 10,000 synthetic patients generated with
  **Synthea** (Java 21 is available; jar from GitHub releases into a git-ignored dir) using its
  diabetes-related modules; demographics, diabetes diagnoses and medications come from Synthea
  (`patients.csv`, `conditions.csv`, `medications.csv`). Converted to compact cohort files in
  `data/cohorts/` (committed, small) by `evaluation/cohort.py`, which also has a deterministic pure
  Python generator fallback (`generate_cohort(n, seed)`) used by tests. Medications mapped to drug
  classes via `suggest_drug_class`-compatible keyword rules (insulin; metformin; sulfonylureas:
  glipizide, glyburide/glibenclamide, glimepiride, gliclazide, tolbutamide, chlorpropamide;
  everything else glucose-lowering → other). Family groups created so one variant is shared by
  several relatives.
* **Knowledge changes.** (a) *Real ClinVar release pair* (several years apart, 7-gene subsets via
  `scripts/fetch_clinvar.py`) — runs when the files exist (`data/clinvar/real/`), otherwise skipped
  and reported as "pending: NCBI not reachable from the build environment". (b) *Controlled change
  set* on a synthetic ClinVar-format base release (~9k records over the 7 genes with a realistic
  class mix): injected known upgrades, downgrades, within-actionable, within-non-actionable
  changes, changes on keys carried by nobody, plus the rule revision D4 v1 → v2.
* **Strategies.** (1) alert-once baseline (no re-evaluation after the initial upload); (2) full
  recomputation; (3) change-driven (GE-EHR). Each runs on its own fresh database with identical
  initial state.
* **Metrics.** re-alert sensitivity (carriers of upgraded variants that have a new current alert
  from that variant / all carriers of upgraded variants); stale alerts after release (current
  alerts not in the ground-truth state for K_new) and missed alerts (ground-truth alerts absent);
  notification precision (correct notifications / notifications; correct = new alert expected by
  ground truth or a review notice for a ground-truth-removed actionable alert) and recall; records
  examined; time per release (ms) on the 10,000 cohort; point-in-time correctness on S1–S12 (and
  `alerts_as_of` reproducing pre-release state). Ground truth = pure `rules.evaluate` on K_new.
* Outputs: `evaluation/results/results.json` and `evaluation/results/RESULTS.md` (Table VI with
  measured values) + `docs/EVALUATION.md`. Command: `.venv/bin/python -m evaluation.run`.

---

## 11. Demo dataset (used by `flask seed-demo`, the scenario tests and the UI)

Synthetic demo variants (coordinates inside the padded panel regions; alleles are synthetic and
**not** checked against the reference genome). Demo release 1 = "R1 (2025-01, synthetic)", demo
release 2 = "R2 (2026-01, synthetic)"; both also contain ~1,500 deterministic filler records
(seed 7) so pages look realistic.

| Id | Gene | Key (chrom-pos-ref-alt) | R1 | R2 | Notes |
|---|---|---|---|---|---|
| V01 | HNF1A | 12-120978900-G-A | P | P | |
| V02 | GCK | 7-44145200-C-T | P | P | |
| V03 | HNF4A | 20-44401300-C-T | LP | P | within actionable (RS3) |
| V04 | KCNJ11 | 11-17387100-C-T | P | P | condition "Permanent neonatal diabetes mellitus" → activating |
| V05 | INS | 11-2160500-G-A | LP | LP | |
| V06 | HNF1A | 12-120994300-C-T | VUS | LP | upgrade (RS1) |
| V07 | GCK | 7-44150500-G-A | LP | VUS | downgrade (RS2) |
| V08 | HNF1A | 12-120999100-A-G | VUS | LB | within non-actionable (RS4) |
| V09 | ABCC8 | 11-17410300-G-A | P | P | condition "Permanent neonatal diabetes mellitus" → activating (RS5) |
| V10 | HNF4A | 20-44410200-G-T | VUS | LP | shared by a family (RS6) |
| V11 | GCK | 7-44160100-T-C | VUS | P | carried by nobody (RS7) |
| B01 | HNF1A | 12-121001000-C-T | B | B | background |
| B02 | GCK | 7-44180000-A-G | LB | LB | background |
| B03 | HNF1B | 17-37700000-G-A | B | B | background |
| B04 | ABCC8 | 11-17450000-C-T | LB | LB | background |
| X01 | (off panel) | 1-1000000-A-G | — | — | in VCFs only, filtered out |

V-expert: V01 and V02 have `CLNREVSTAT=reviewed_by_expert_panel` in both releases.

Demo patients (`data/demo/patients.json`; MRNs `DEMO-0001`…; all `is_synthetic`):

| MRN | Scenario | Variants | Diagnosis (type, age at dx) | Current meds | Expected under R1 + rules v1 |
|---|---|---|---|---|---|
| DEMO-0001 | S1 | V01 het, B01 | type1, 180 m | insulin | D2 high, D5 low |
| DEMO-0002 | S2 | V01 | type2, 300 m | sulfonylurea (gliclazide) | D2 note, D5 low |
| DEMO-0003 | S3 | V02 | type2, 360 m | metformin | D1 medium, D5 low |
| DEMO-0004 | S4 | V02, B02 | other, 240 m | none | D1 note, D5 low |
| DEMO-0005 | S5 / RS3 | V03 | type2, 264 m | metformin | D3 medium, D5 low |
| DEMO-0006 | S6 | V04 | neonatal, 2 m | insulin | D4 high, D5 low |
| DEMO-0007 | S7 | V05 | type1, 96 m | insulin | D5 low |
| DEMO-0008 | S8 / RS1 | V06, B03 | type1, 192 m | insulin | D6 info |
| DEMO-0009 | S9 | B01, B02, B04, X01 | type2, 480 m | metformin | none |
| DEMO-0010 | RS2 | V07 | type2, 420 m | metformin | D1 medium, D5 low |
| DEMO-0011 | RS4 | V08 | type1, 150 m | insulin | D6 info |
| DEMO-0012 | RS5 | V09 | neonatal, 3 m | insulin | D5 low (D4 v1 is KCNJ11-only) |
| DEMO-0013 | RS6 (family FAM-RS6, mother) | V10 | type2, 324 m | metformin | D6 info |
| DEMO-0014 | RS6 (child) | V10 | type1, 168 m | insulin | D6 info |
| DEMO-0015 | RS6 (child, no diabetes) | V10 | none, — | none | D6 info |

Expected after importing rules v2 (RS5): DEMO-0012 gains **D4 high** (notify). Expected after
activating R2 (change-driven): DEMO-0008 → D2 high + D5 low, both `is_reclassified`, high priority;
D6 superseded (RS1). DEMO-0010 → D1 and D5 superseded, one ReviewNotice, D6 info created, FHIR
`amended` (RS2). DEMO-0005 → D3/D5 provenance updated to R2, no notification (RS3). DEMO-0011 →
D6 superseded silently, no notice, no notification (RS4). DEMO-0013/14/15 → every carrier alerted:
D3 medium + D5 low for 0013 and 0014; D3 note + D5 low for 0015 (RS6). V11 → not examined (RS7).
S10 = `data/demo/vcfs/malformed.vcf` rejected; S11 = FHIR export of DEMO-0001 valid; S12 = lab
technician GET `/patients/<DEMO-0001 id>/recommendations` → 403.

CLI: `flask seed-demo [--full] [--reset]` (defined in `commands/demo_cmds.py`). Demo users created by `flask seed-demo` (passwords from env `DEMO_PASSWORD`, default
`Demo-Pass-2026!`, `must_change_password=False`): `admin` (admin), `dr.rao` (clinician, "Dr. A. Rao"),
`lab.kim` (lab_technician, "J. Kim"). `seed_demo(app_or_none, *, rules="v1", activate_r2=False)`
loads rules v1 and R1, creates the patients, ingests their VCFs (as `lab.kim`) and adds
medications; leaving rules v2 and R2 for the user to import in the UI to watch re-evaluation
(`--full` flag applies both). It is idempotent-safe: refuses to run on a non-empty patients table
unless `--reset`.

---

## 12. Testing (Table V)

Unit tests per module plus `tests/test_scenarios.py` (Scenario WP) implementing **S1–S9, S11 and
RS1–RS7 exactly as in §11** using `seed_demo` on an in-memory DB, and `tests/test_scenarios_ui.py`
(UI-A WP) implementing **S10 and S12** through the routes; route tests for every blueprint (status codes,
role enforcement, CSRF-protected forms work with the test client). Coverage target ≥ 85 % for
`ge_ehr/services`.
