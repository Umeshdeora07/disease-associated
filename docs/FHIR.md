# FHIR export (paper module F4)

`GET /patients/<id>/fhir` returns the patient's genomic record as a FHIR R4 `collection`
Bundle built from a simplified subset of the HL7 Genomics Reporting IG 2.0.0 profiles
(`ge_ehr/services/fhir_export.py`). After a release activation changes a variant's
classification, both of the variant's Observations are exported with status **`amended`**.

* **Access:** clinician or admin only. Every failure is a FHIR `OperationOutcome` sent as
  `application/fhir+json`: anonymous → 401 `login`, other roles → 403 `forbidden` (audited as
  `access_denied`), unknown patient → 404 `not-found`, export failed validation → 500 `exception`.
  A successful export is audited as `fhir_export`. `?download=1` sends the Bundle as the
  attachment `GE-EHR_<MRN>_fhir.json`.
* **Validation:** every response Bundle is parsed with the `fhir.resources.R4B` models (Bundle plus
  each entry resource) before it is sent (`validate_bundle`). fhir.resources 8 ships R4B (4.3),
  which is structurally identical to R4 for these resources.
* **Identifiers:** `fullUrl` is `urn:uuid:<uuid5>`, and the same UUID is the resource `id`. The
  UUID is derived from the MRN and the variant key, so exporting the same record again gives the
  same identifiers, and an amended Observation keeps its id.
* **Synthetic data:** the Bundle and every resource carry `meta.tag` `HTEST` (test health data).

## Mapping

Abbreviations: **LOINC** = `http://loinc.org`, **PV** = `PatientVariant`, **KV** = the
`KnownVariant` for the PV's key in the PV's `classification_release`.

| Resource · element | Source | Code system / code |
|---|---|---|
| **Bundle** `type` | constant `collection` | — |
| Bundle `timestamp` | time of export (UTC instant, `…Z`) | — |
| Bundle `meta.tag` (all resources) | constant | `http://terminology.hl7.org/CodeSystem/v3-ActReason` `HTEST` |
| Bundle `link[self]` | request URL `/patients/<id>/fhir` | — |
| **Patient** `identifier` | `Patient.mrn` | system `urn:ge-ehr:mrn` |
| Patient `name` (`official`) | `family_name`, `given_name` | — |
| Patient `gender` | `Patient.sex` | AdministrativeGender (same codes) |
| Patient `birthDate` | `Patient.birth_date` | — |
| **Variant Observation** `meta.profile` | constant | `http://hl7.org/fhir/uv/genomics-reporting/StructureDefinition/variant` |
| `status` | `PV.is_amended` (`reclassified_count > 0`) | `final` / `amended` |
| `category` | constant | `observation-category` `laboratory`; `v2-0074` `GE` |
| `code` | constant | LOINC `69548-6` Genetic variant assessment |
| `subject` | the Patient entry | `urn:uuid:` reference |
| `effectiveDateTime` | latest `GenomicUpload.uploaded_at` (else `PV.created_at`) | — |
| `issued` | as `effectiveDateTime`; time of the reclassification when amended | — |
| `valueCodeableConcept` | constant | LOINC `LA9633-4` Present |
| component `48018-6` Gene studied | `PV.gene` → `GENE_INFO.hgnc_id` / symbol | `http://www.genenames.org/geneId` |
| component `48013-7` Genomic ref seq | `GENE_INFO.refseq_chrom` (e.g. `NC_000012.12`) | `http://www.ncbi.nlm.nih.gov/refseq` |
| component `62374-4` Reference assembly | constant | LOINC `LA26806-2` GRCh38 |
| component `92822-6` Coordinate system | constant | LOINC `LA30102-0` 1-based character counting |
| component `81254-5` Allele start-end | `VariantKey.pos` … `pos + len(ref) − 1` (`valueRange`) | — |
| component `69547-8` Ref allele | `VariantKey.ref` (`valueString`) | — |
| component `69551-0` Alt allele | `VariantKey.alt` (`valueString`) | — |
| component `53034-5` Allelic state | `PV.zygosity` → `ZYGOSITY_TO_LOINC` | LOINC `LA6706-1` / `LA6705-3` / `LA6707-9` |
| component `81290-9` Genomic HGVS | `KV.hgvs` (ClinVar `CLNHGVS`), when known (`valueCodeableConcept.text`) | — |
| **Diagnostic implication Observation** (only when `PV.current_classification ≠ NC`) `meta.profile` | constant | `http://hl7.org/fhir/uv/genomics-reporting/StructureDefinition/diagnostic-implication` |
| `code` | constant | `http://hl7.org/fhir/uv/genomics-reporting/CodeSystem/tbd-codes-cs` `diagnostic-implication` |
| `status`, `category`, `subject` | as for the variant Observation | — |
| `effectiveDateTime`, `issued` | when the current class was assigned: `PV.last_reclassified_at` if amended, else the later of `PV.created_at` and the release's `activated_at` | — |
| `derivedFrom` | the variant Observation | `urn:uuid:` reference |
| component `53037-8` Clinical significance | `PV.current_classification` → `CLASS_TO_LOINC` | LOINC `LA6668-3` P, `LA26332-9` LP, `LA26333-7` VUS, `LA26334-5` LB, `LA6675-8` B |
| component `81259-4` Associated phenotype | `KV.condition` (ClinVar `CLNDN`), when stated (`valueCodeableConcept.text`) | — |
| `note` (provenance) | release `label`, `KV.review_status` + `review_stars`, `KV.is_expert_panel`, `PV.reclassified_count`, `PV.reported_classification` when amended | — |

Example provenance note:
*"Classified against R2 (2026-01, synthetic), review status criteria provided, single submitter
(1 star), expert panel no; reclassified 1 time(s); first reported as Likely pathogenic."*
