# GE-EHR user guide

How each role uses GE-EHR, in the browser (http://localhost:5000 after `python run.py`) and in the
terminal (`flask --app wsgi ...` inside the activated environment). Installation and the demo
data are described in the [README](../README.md).

> Synthetic data only. Recommendations are advisory and phrased for specialist review; GE-EHR
> never changes therapy itself.

## Lab technician (`lab.kim`)

Uploads genetic results. Cannot see recommendations, alerts or review notices (those pages
answer *403 Access denied*).

**Browser**
1. *Patients* → search by name or MRN → open the patient.
2. *Upload VCF* → choose a `.vcf` or `.vcf.gz` file (VCF 4.1–4.3, **GRCh38**, one sample).
3. Accepted: a summary shows how many variants fell in the 7 panel genes and how many were
   skipped (off-panel, reference calls, non-PASS). Rejected: the exact line and reason are shown
   (e.g. *"Line 12: POS must be a positive whole number"*); nothing is stored except the rejected
   upload record.

**Terminal**
```bash
flask --app wsgi patients list --search Okonkwo
flask --app wsgi patients upload-vcf DEMO-0001 data/demo/vcfs/DEMO-0001.vcf --as lab.kim
flask --app wsgi patients show DEMO-0001 --as lab.kim      # lab view: no recommendations
```

## Clinician (`dr.rao`)

Sees and acts on the decision support.

**Browser**
- *Dashboard*: current alerts by severity, open review notices, "Needs attention" (high-priority
  reclassification alerts), current ClinVar release and rule versions, recent re-evaluation runs.
- *Patients* → patient page: diagnosis (edit), current diabetes medicines (add / stop), genomic
  profile (gene, variant, zygosity, classification badge, review stars, expert-panel and
  *Amended* / *Reclassified* flags, classification history per release), uploads,
  **FHIR export** button.
- *Recommendations* (patient): current alerts grouped by severity with rule code and version,
  recommendation, evidence source, classification and ClinVar release; information notes (rule
  matched genetically but the therapy condition did not, e.g. already on a sulfonylurea);
  history of withdrawn alerts with the reason; full provenance per alert. **View as of** shows
  exactly what the system recommended at an earlier moment, with the evidence it used then.
- *Alerts* inbox: filter by severity, status, rule, *Reclassified*; open an alert to see its
  provenance timeline and **mark it reviewed** with a comment.
- *Review notices*: created when an alert is withdrawn (e.g. a variant downgraded from likely
  pathogenic to uncertain). Check whether a treatment change was based on it, then
  **acknowledge** with a comment.

Changing a medicine or the diagnosis re-checks that patient immediately (for example, starting
a sulfonylurea turns the D2 alert into an information note).

**Terminal**
```bash
flask --app wsgi patients show DEMO-0001
flask --app wsgi patients add-medication DEMO-0001 --as dr.rao --drug gliclazide
flask --app wsgi patients stop-medication DEMO-0001 --as dr.rao --drug gliclazide
flask --app wsgi patients set-diagnosis DEMO-0006 --as dr.rao --type neonatal --age-at-diagnosis-months 2
flask --app wsgi patients history DEMO-0008
flask --app wsgi patients history DEMO-0008 --at "2026-01-01 10:00"
flask --app wsgi patients fhir DEMO-0001 --output DEMO-0001_fhir.json
flask --app wsgi alerts list --severity high
flask --app wsgi alerts show 1
flask --app wsgi alerts review 1 --as dr.rao --comment "Discussed at clinic"
flask --app wsgi alerts notices
flask --app wsgi alerts acknowledge 1 --as dr.rao --comment "No therapy change was made"
```

## Administrator (`admin`)

Everything a clinician can do, plus:

- *Knowledge base → Releases*: import a ClinVar(-format) GRCh38 VCF (optionally an expert-panel
  overlay CSV), and **activate** it. Activation runs the change-driven re-evaluation: only
  stored variants whose classification changed are compared, and only their carriers are
  re-checked. *Compare* shows the differences between two releases.
- *Knowledge base → Rules*: current rule set (D1–D6), version history, **import** a new rule-set
  JSON (validated first; carriers of the revised rules' genes are re-checked).
- *Re-evaluation runs*: every run with its counters (changed variants, carriers, records
  examined, alerts created/withdrawn/confirmed, notices, duration) and the list of changes.
  *Full recompute* re-checks every patient.
- *Admin → Users*: create users, deactivate, reset passwords. *Admin → Audit log*: every
  login, upload, change, review, import and denied access.

**Terminal**
```bash
flask --app wsgi clinvar import data/demo/clinvar/demo_release_2.vcf.gz --label "R2 (2026-01, synthetic)" --activate
flask --app wsgi clinvar list
flask --app wsgi clinvar sync                    # download the latest real ClinVar (needs internet)
flask --app wsgi rules validate data/rules/ruleset_v2.json
flask --app wsgi rules import data/rules/ruleset_v2.json
flask --app wsgi reeval runs
flask --app wsgi reeval show 3
flask --app wsgi reeval full --yes
flask --app wsgi create-user asha --role clinician --full-name "Dr. Asha Verma"
flask --app wsgi list-users
```

## What the alerts mean (rules D1–D6)

| Rule | When | Severity | Recommendation (advisory) |
|---|---|---|---|
| D1 | GCK P/LP (heterozygous) and on any glucose-lowering medicine | medium | Medication usually not required outside pregnancy; review whether it can be stopped. |
| D2 | HNF1A P/LP (heterozygous), on insulin or metformin, not on a sulfonylurea | high | Consider low-dose sulfonylurea as first-line treatment. |
| D3 | HNF4A P/LP (heterozygous), same therapy condition | medium | Consider low-dose sulfonylurea (evidence more limited than for HNF1A). |
| D4 | KCNJ11 or ABCC8 activating P/LP, diagnosed before 12 months, on insulin | high | Refer to specialist to consider transfer to high-dose sulfonylurea. |
| D5 | Any of the 7 genes P/LP (heterozygous) | low | Offer genetic counselling and cascade testing of first-degree relatives. |
| D6 | GCK, HNF1A or HNF4A uncertain (VUS) | info | No treatment change; variant monitored for reclassification. |

When the genetic part matches but the therapy part does not, an **information note** explains
why no alert was raised. Homozygous variants get a note recommending specialist referral instead
of D1–D3/D5.

## What happens when ClinVar or the rules change

| Change | What GE-EHR does |
|---|---|
| Upgrade into pathogenic/likely pathogenic | New alerts, marked **Reclassified**, high priority |
| Downgrade out of pathogenic/likely pathogenic | Alerts withdrawn, **review notice** for the clinician, FHIR result marked *amended*; therapy is never reversed automatically |
| Change within the same band (e.g. likely pathogenic → pathogenic) | Evidence trail updated silently |
| Rule revision | Revised rule re-run for carriers of its genes; notified only if the outcome changes |
