"""Tests for the FHIR R4 Bundle export (SPEC §9, paper module F4)."""

from __future__ import annotations

import copy
import re
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Any

import pytest
from fhir.resources.R4B.bundle import Bundle
from fhir.resources.R4B.observation import Observation
from fhir.resources.R4B.patient import Patient as FhirPatient
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import event

from ge_ehr.extensions import db as _db
from ge_ehr.models import GenomicUpload, KbRelease, Patient, PatientVariant, utcnow
from ge_ehr.services.fhir_export import build_patient_bundle, fhir_instant, validate_bundle
from tests.factories import add_known_variant, add_patient_variant, make_patient, make_release

LOINC = "http://loinc.org"
HTEST = {"system": "http://terminology.hl7.org/CodeSystem/v3-ActReason", "code": "HTEST", "display": "test health data"}
VARIANT_PROFILE = "http://hl7.org/fhir/uv/genomics-reporting/StructureDefinition/variant"
IMPLICATION_PROFILE = "http://hl7.org/fhir/uv/genomics-reporting/StructureDefinition/diagnostic-implication"
INSTANT = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")

R1_LABEL = "R1 (2025-01, synthetic)"
R2_LABEL = "R2 (2026-01, synthetic)"
R1_ACTIVATED = datetime(2025, 1, 10, 8, 0, 0)
R2_ACTIVATED = datetime(2026, 1, 12, 7, 0, 0)
UPLOADED = datetime(2025, 2, 1, 9, 30, 15)
STORED = datetime(2025, 2, 1, 9, 30, 20)
RECLASSIFIED = datetime(2026, 1, 12, 7, 0, 5)


# ---------------------------------------------------------------------------
# Fixtures and helpers
# ---------------------------------------------------------------------------


@dataclass
class Record:
    """A patient with an expert-panel P variant, an amended (LP → VUS) variant and an NC variant."""

    patient: Patient
    r1: KbRelease
    r2: KbRelease
    pathogenic: PatientVariant
    amended: PatientVariant
    unclassified: PatientVariant
    upload: GenomicUpload


def add_upload(patient: Patient, uploaded_at: datetime = UPLOADED) -> GenomicUpload:
    upload = GenomicUpload(
        patient=patient, filename="sample.vcf", sha256="0" * 64, status="accepted", uploaded_at=uploaded_at
    )
    _db.session.add(upload)
    _db.session.flush()
    return upload


@pytest.fixture
def record(db: SQLAlchemy) -> Record:
    r1 = make_release(R1_LABEL, sequence=1, activated_at=R1_ACTIVATED)
    r2 = make_release(R2_LABEL, current=True, activated_at=R2_ACTIVATED)
    add_known_variant(
        r1, "12", 120978900, "G", "A", "P",
        hgvs="NC_000012.12:g.120978900G>A",
        condition="Maturity-onset diabetes of the young type 3",
        review_status="reviewed_by_expert_panel", review_stars=3, is_expert_panel=True,
    )
    add_known_variant(r1, "7", 44150500, "G", "A", "LP", hgvs="NC_000007.14:g.44150500G>A")
    add_known_variant(
        r2, "7", 44150500, "G", "A", "VUS",
        hgvs="NC_000007.14:g.44150500G>A", condition="not provided",
        review_status="criteria_provided,_single_submitter", review_stars=1,
    )
    patient = make_patient(mrn="FHIR-0001", given_name="Ana", family_name="Silva", sex="female",
                           birth_date=date(1988, 4, 2))
    upload = add_upload(patient)
    pathogenic = add_patient_variant(patient, "12", 120978900, "G", "A", classification="P", release=r1)
    amended = add_patient_variant(patient, "7", 44150500, "G", "A", classification="LP", release=r1)
    amended.current_classification = "VUS"
    amended.classification_release = r2
    amended.reclassified_count = 1
    amended.last_reclassified_at = RECLASSIFIED
    unclassified = add_patient_variant(
        patient, "17", 37700000, "G", "A", zygosity="homozygous", classification="NC", release=r1
    )
    for variant in (pathogenic, amended, unclassified):
        variant.upload = upload
        variant.created_at = STORED
    db.session.commit()
    return Record(patient, r1, r2, pathogenic, amended, unclassified, upload)


def resources(bundle: dict[str, Any]) -> list[dict[str, Any]]:
    return [entry["resource"] for entry in bundle["entry"]]


def full_url(bundle: dict[str, Any], resource: dict[str, Any]) -> str:
    return next(entry["fullUrl"] for entry in bundle["entry"] if entry["resource"] is resource)


def with_profile(bundle: dict[str, Any], profile: str) -> list[dict[str, Any]]:
    return [r for r in resources(bundle) if profile in r.get("meta", {}).get("profile", [])]


def variant_observation(bundle: dict[str, Any], ref: str, alt: str, pos: int) -> dict[str, Any]:
    for observation in with_profile(bundle, VARIANT_PROFILE):
        values = {c["code"]["coding"][0]["code"]: c for c in observation["component"]}
        start = values["81254-5"]["valueRange"]["low"]["value"]
        if (values["69547-8"]["valueString"], values["69551-0"]["valueString"], start) == (ref, alt, pos):
            return observation
    raise AssertionError(f"no variant Observation for {pos} {ref}>{alt}")


def implication_for(bundle: dict[str, Any], observation: dict[str, Any]) -> dict[str, Any] | None:
    url = full_url(bundle, observation)
    matches = [r for r in with_profile(bundle, IMPLICATION_PROFILE) if r["derivedFrom"] == [{"reference": url}]]
    assert len(matches) <= 1
    return matches[0] if matches else None


def component(observation: dict[str, Any], code: str) -> dict[str, Any] | None:
    found = [c for c in observation["component"] if c["code"]["coding"][0]["code"] == code]
    assert len(found) <= 1, f"component {code} repeated"
    if found:
        assert found[0]["code"]["coding"][0]["system"] == LOINC
    return found[0] if found else None


def loinc(code: str, display: str) -> dict[str, Any]:
    return {"coding": [{"system": LOINC, "code": code, "display": display}]}


@pytest.fixture
def bundle(record: Record) -> dict[str, Any]:
    return build_patient_bundle(record.patient)


# ---------------------------------------------------------------------------
# Bundle and Patient
# ---------------------------------------------------------------------------


def test_bundle_envelope(record: Record) -> None:
    before = utcnow().replace(microsecond=0)
    result = build_patient_bundle(record.patient)
    after = utcnow()
    assert result["resourceType"] == "Bundle"
    assert result["type"] == "collection"
    assert result["meta"] == {"tag": [HTEST]}
    assert INSTANT.match(result["timestamp"])
    assert before <= datetime.strptime(result["timestamp"], "%Y-%m-%dT%H:%M:%SZ") <= after
    assert "link" not in result


@pytest.mark.parametrize("base_url", ["http://ge-ehr.example.org/", "http://ge-ehr.example.org"])
def test_self_link_from_base_url(record: Record, base_url: str) -> None:
    result = build_patient_bundle(record.patient, base_url=base_url)
    assert result["link"] == [
        {"relation": "self", "url": f"http://ge-ehr.example.org/patients/{record.patient.id}/fhir"}
    ]


def test_patient_resource(bundle: dict[str, Any], record: Record) -> None:
    patient = resources(bundle)[0]
    assert patient["resourceType"] == "Patient"
    assert patient["meta"] == {"tag": [HTEST]}
    assert patient["identifier"] == [{"system": "urn:ge-ehr:mrn", "value": "FHIR-0001"}]
    assert patient["name"] == [{"use": "official", "family": "Silva", "given": ["Ana"]}]
    assert patient["gender"] == "female"
    assert patient["birthDate"] == "1988-04-02"
    assert bundle["entry"][0]["fullUrl"] == f"urn:uuid:{patient['id']}"
    FhirPatient.model_validate(patient)


@pytest.mark.parametrize("sex", ["female", "male", "other", "unknown"])
def test_patient_gender_codes(db: SQLAlchemy, sex: str) -> None:
    patient = make_patient(sex=sex)
    assert resources(build_patient_bundle(patient))[0]["gender"] == sex


@pytest.mark.parametrize(
    ("given_name", "family_name", "expected"),
    [
        ("  ", "Okafor", {"use": "official", "family": "Okafor"}),
        (" Ana ", "", {"use": "official", "given": ["Ana"]}),
    ],
)
def test_patient_name_omits_empty_parts(
    db: SQLAlchemy, given_name: str, family_name: str, expected: dict[str, Any]
) -> None:
    patient = make_patient(given_name=given_name, family_name=family_name)
    result = build_patient_bundle(patient)
    assert resources(result)[0]["name"] == [expected]
    validate_bundle(result)


def test_patient_without_variants(db: SQLAlchemy) -> None:
    patient = make_patient()
    result = build_patient_bundle(patient)
    assert [r["resourceType"] for r in resources(result)] == ["Patient"]
    validate_bundle(result)


def test_entry_order(bundle: dict[str, Any]) -> None:
    profiles = [r.get("meta", {}).get("profile", [r["resourceType"]])[0] for r in resources(bundle)]
    assert profiles == [
        "Patient",
        VARIANT_PROFILE, IMPLICATION_PROFILE,  # P variant
        VARIANT_PROFILE, IMPLICATION_PROFILE,  # amended VUS variant
        VARIANT_PROFILE,  # NC variant: no diagnostic implication
    ]


def test_every_resource_is_tagged_as_test_data(bundle: dict[str, Any]) -> None:
    assert all(resource["meta"]["tag"] == [HTEST] for resource in resources(bundle))


def test_full_urls_are_unique_uuid_urns_matching_ids(bundle: dict[str, Any]) -> None:
    urls = [entry["fullUrl"] for entry in bundle["entry"]]
    assert len(set(urls)) == len(urls)
    for entry in bundle["entry"]:
        identifier = uuid.UUID(entry["fullUrl"].removeprefix("urn:uuid:"))
        assert identifier.version == 5
        assert entry["fullUrl"] == f"urn:uuid:{entry['resource']['id']}"


def test_references_point_into_the_bundle(bundle: dict[str, Any]) -> None:
    patient_url = bundle["entry"][0]["fullUrl"]
    urls = {entry["fullUrl"] for entry in bundle["entry"]}
    for observation in resources(bundle)[1:]:
        assert observation["subject"] == {"reference": patient_url}
        for reference in observation.get("derivedFrom", []):
            assert reference["reference"] in urls


# ---------------------------------------------------------------------------
# Deterministic identifiers
# ---------------------------------------------------------------------------


def test_repeated_exports_are_identical_except_timestamp(record: Record) -> None:
    first, second = build_patient_bundle(record.patient), build_patient_bundle(record.patient)
    first.pop("timestamp")
    second.pop("timestamp")
    assert first == second


def test_ids_are_stable_across_reclassification(db: SQLAlchemy, record: Record) -> None:
    before = [entry["fullUrl"] for entry in build_patient_bundle(record.patient)["entry"]]
    record.pathogenic.current_classification = "LP"
    record.pathogenic.reclassified_count = 1
    record.pathogenic.last_reclassified_at = RECLASSIFIED
    db.session.flush()
    after = build_patient_bundle(record.patient)
    assert [entry["fullUrl"] for entry in after["entry"]] == before
    assert variant_observation(after, "G", "A", 120978900)["status"] == "amended"


def test_ids_differ_between_patients_sharing_a_variant(db: SQLAlchemy) -> None:
    first, second = make_patient(), make_patient()
    for patient in (first, second):
        add_patient_variant(patient, "20", 44410200, "G", "T")
    first_urls = {entry["fullUrl"] for entry in build_patient_bundle(first)["entry"]}
    second_urls = {entry["fullUrl"] for entry in build_patient_bundle(second)["entry"]}
    assert len(first_urls) == len(second_urls) == 2
    assert not first_urls & second_urls


# ---------------------------------------------------------------------------
# Variant Observation
# ---------------------------------------------------------------------------


def test_variant_observation_header(bundle: dict[str, Any]) -> None:
    observation = variant_observation(bundle, "G", "A", 120978900)
    assert observation["resourceType"] == "Observation"
    assert observation["meta"]["profile"] == [VARIANT_PROFILE]
    assert observation["status"] == "final"
    assert observation["category"] == [
        {"coding": [{"system": "http://terminology.hl7.org/CodeSystem/observation-category",
                     "code": "laboratory", "display": "Laboratory"}]},
        {"coding": [{"system": "http://terminology.hl7.org/CodeSystem/v2-0074", "code": "GE", "display": "Genetics"}]},
    ]
    assert observation["code"] == loinc("69548-6", "Genetic variant assessment")
    assert observation["valueCodeableConcept"] == loinc("LA9633-4", "Present")
    Observation.model_validate(observation)


@pytest.mark.parametrize(
    ("code", "value"),
    [
        ("48018-6", {"valueCodeableConcept": {"coding": [
            {"system": "http://www.genenames.org/geneId", "code": "HGNC:11621", "display": "HNF1A"}]}}),
        ("48013-7", {"valueCodeableConcept": {"coding": [
            {"system": "http://www.ncbi.nlm.nih.gov/refseq", "code": "NC_000012.12"}]}}),
        ("62374-4", {"valueCodeableConcept": loinc("LA26806-2", "GRCh38")}),
        ("92822-6", {"valueCodeableConcept": loinc("LA30102-0", "1-based character counting")}),
        ("81254-5", {"valueRange": {"low": {"value": 120978900}, "high": {"value": 120978900}}}),
        ("69547-8", {"valueString": "G"}),
        ("69551-0", {"valueString": "A"}),
        ("53034-5", {"valueCodeableConcept": loinc("LA6706-1", "Heterozygous")}),
        ("81290-9", {"valueCodeableConcept": {"text": "NC_000012.12:g.120978900G>A"}}),
    ],
)
def test_variant_components(bundle: dict[str, Any], code: str, value: dict[str, Any]) -> None:
    found = component(variant_observation(bundle, "G", "A", 120978900), code)
    assert found is not None, f"component {code} missing"
    assert {key: val for key, val in found.items() if key != "code"} == value


def test_variant_component_order(bundle: dict[str, Any]) -> None:
    observation = variant_observation(bundle, "G", "A", 120978900)
    assert [c["code"]["coding"][0]["code"] for c in observation["component"]] == [
        "48018-6", "48013-7", "62374-4", "92822-6", "81254-5", "69547-8", "69551-0", "53034-5", "81290-9",
    ]


@pytest.mark.parametrize(
    ("zygosity", "code", "display"),
    [("heterozygous", "LA6706-1", "Heterozygous"), ("homozygous", "LA6705-3", "Homozygous"),
     ("hemizygous", "LA6707-9", "Hemizygous")],
)
def test_allelic_state(db: SQLAlchemy, zygosity: str, code: str, display: str) -> None:
    patient = make_patient()
    add_patient_variant(patient, "11", 2160500, "G", "A", zygosity=zygosity)
    observation = resources(build_patient_bundle(patient))[1]
    assert component(observation, "53034-5")["valueCodeableConcept"] == loinc(code, display)


def test_allele_range_spans_the_reference_allele(db: SQLAlchemy) -> None:
    patient = make_patient()
    add_patient_variant(patient, "20", 44401300, "CTT", "C")
    observation = resources(build_patient_bundle(patient))[1]
    assert component(observation, "81254-5")["valueRange"] == {"low": {"value": 44401300}, "high": {"value": 44401302}}
    assert component(observation, "69547-8")["valueString"] == "CTT"
    assert component(observation, "69551-0")["valueString"] == "C"


def test_refseq_and_gene_per_panel_gene(bundle: dict[str, Any]) -> None:
    gck = variant_observation(bundle, "G", "A", 44150500)
    assert component(gck, "48018-6")["valueCodeableConcept"]["coding"][0] == {
        "system": "http://www.genenames.org/geneId", "code": "HGNC:4195", "display": "GCK"}
    assert component(gck, "48013-7")["valueCodeableConcept"]["coding"][0]["code"] == "NC_000007.14"


def test_hgvs_component_only_when_known(bundle: dict[str, Any]) -> None:
    assert component(variant_observation(bundle, "G", "A", 37700000), "81290-9") is None


def test_variant_outside_the_panel_has_no_gene_components(db: SQLAlchemy) -> None:
    patient = make_patient()
    add_patient_variant(patient, "1", 1000000, "A", "G")
    result = build_patient_bundle(patient)
    observation = resources(result)[1]
    assert component(observation, "48018-6") is None and component(observation, "48013-7") is None
    assert component(observation, "62374-4") is not None
    validate_bundle(result)


# ---------------------------------------------------------------------------
# Diagnostic-implication Observation
# ---------------------------------------------------------------------------


def test_implication_observation(bundle: dict[str, Any]) -> None:
    variant = variant_observation(bundle, "G", "A", 120978900)
    implication = implication_for(bundle, variant)
    assert implication is not None
    assert implication["meta"]["profile"] == [IMPLICATION_PROFILE]
    assert implication["status"] == "final"
    assert implication["code"] == {"coding": [{
        "system": "http://hl7.org/fhir/uv/genomics-reporting/CodeSystem/tbd-codes-cs",
        "code": "diagnostic-implication", "display": "Diagnostic Implication"}]}
    assert implication["category"] == variant["category"]
    assert implication["subject"] == variant["subject"]
    assert implication["derivedFrom"] == [{"reference": full_url(bundle, variant)}]
    assert [c["code"]["coding"][0]["code"] for c in implication["component"]] == ["53037-8", "81259-4"]
    assert component(implication, "53037-8")["valueCodeableConcept"] == loinc("LA6668-3", "Pathogenic")
    assert component(implication, "81259-4")["valueCodeableConcept"] == {
        "text": "Maturity-onset diabetes of the young type 3"}
    Observation.model_validate(implication)


@pytest.mark.parametrize(
    ("classification", "code", "display"),
    [("P", "LA6668-3", "Pathogenic"), ("LP", "LA26332-9", "Likely pathogenic"),
     ("VUS", "LA26333-7", "Uncertain significance"), ("LB", "LA26334-5", "Likely benign"),
     ("B", "LA6675-8", "Benign")],
)
def test_clinical_significance_codes(db: SQLAlchemy, classification: str, code: str, display: str) -> None:
    release = make_release(current=True)
    add_known_variant(release, "12", 120994300, "C", "T", classification)
    patient = make_patient()
    add_patient_variant(patient, "12", 120994300, "C", "T", classification=classification, release=release)
    result = build_patient_bundle(patient)
    implication = with_profile(result, IMPLICATION_PROFILE)[0]
    assert component(implication, "53037-8")["valueCodeableConcept"] == loinc(code, display)
    validate_bundle(result)


def test_not_classified_variant_has_no_implication(bundle: dict[str, Any]) -> None:
    variant = variant_observation(bundle, "G", "A", 37700000)
    assert variant["status"] == "final"
    assert implication_for(bundle, variant) is None
    assert len(with_profile(bundle, IMPLICATION_PROFILE)) == 2


@pytest.mark.parametrize("condition", [None, "", "not provided", "Not specified"])
def test_phenotype_omitted_when_condition_unstated(db: SQLAlchemy, condition: str | None) -> None:
    release = make_release(current=True)
    add_known_variant(release, "12", 120978900, "G", "A", "P", condition=condition)
    patient = make_patient()
    add_patient_variant(patient, "12", 120978900, "G", "A", classification="P", release=release)
    implication = with_profile(build_patient_bundle(patient), IMPLICATION_PROFILE)[0]
    assert component(implication, "81259-4") is None
    assert component(implication, "53037-8") is not None


def test_known_variant_is_taken_from_the_classification_release(db: SQLAlchemy) -> None:
    old, new = make_release("Old"), make_release("New", current=True)
    for release, hgvs in ((old, "old-hgvs"), (new, "new-hgvs")):
        condition = f"{release.label} condition"
        add_known_variant(release, "12", 120978900, "G", "A", "P", hgvs=hgvs, condition=condition)
        add_known_variant(release, "7", 44145200, "C", "T", "P", hgvs=f"{hgvs}-2", condition=condition)
    patient = make_patient()
    add_patient_variant(patient, "12", 120978900, "G", "A", classification="P", release=old)
    add_patient_variant(patient, "7", 44145200, "C", "T", classification="P", release=new)
    result = build_patient_bundle(patient)
    first, second = variant_observation(result, "G", "A", 120978900), variant_observation(result, "C", "T", 44145200)
    assert component(first, "81290-9")["valueCodeableConcept"]["text"] == "old-hgvs"
    assert component(second, "81290-9")["valueCodeableConcept"]["text"] == "new-hgvs-2"
    assert component(implication_for(result, first), "81259-4")["valueCodeableConcept"]["text"] == "Old condition"
    assert implication_for(result, first)["note"][0]["text"].startswith("Classified against Old,")
    assert implication_for(result, second)["note"][0]["text"].startswith("Classified against New,")


# ---------------------------------------------------------------------------
# Status, times and provenance
# ---------------------------------------------------------------------------


def test_amended_variant_exports_amended_observations(bundle: dict[str, Any]) -> None:
    variant = variant_observation(bundle, "G", "A", 44150500)
    implication = implication_for(bundle, variant)
    assert variant["status"] == implication["status"] == "amended"
    assert component(implication, "53037-8")["valueCodeableConcept"] == loinc("LA26333-7", "Uncertain significance")


def test_final_variant_exports_final_observations(bundle: dict[str, Any]) -> None:
    variant = variant_observation(bundle, "G", "A", 120978900)
    assert variant["status"] == implication_for(bundle, variant)["status"] == "final"


def test_times_of_a_final_variant(bundle: dict[str, Any]) -> None:
    variant = variant_observation(bundle, "G", "A", 120978900)
    implication = implication_for(bundle, variant)
    # Reported by the upload; classified when stored (R1 was already active).
    assert variant["effectiveDateTime"] == variant["issued"] == "2025-02-01T09:30:15Z"
    assert implication["effectiveDateTime"] == implication["issued"] == "2025-02-01T09:30:20Z"


def test_times_of_an_amended_variant(bundle: dict[str, Any]) -> None:
    variant = variant_observation(bundle, "G", "A", 44150500)
    implication = implication_for(bundle, variant)
    assert variant["effectiveDateTime"] == "2025-02-01T09:30:15Z"
    assert variant["issued"] == implication["issued"] == implication["effectiveDateTime"] == "2026-01-12T07:00:05Z"


def test_times_without_upload_or_activation(db: SQLAlchemy) -> None:
    release = make_release()  # imported, never activated
    patient = make_patient()
    variant = add_patient_variant(patient, "12", 120978900, "G", "A", classification="P", release=release)
    variant.created_at = datetime(2025, 3, 4, 5, 6, 7)
    result = build_patient_bundle(patient)
    observation, implication = resources(result)[1:]
    assert observation["effectiveDateTime"] == observation["issued"] == "2025-03-04T05:06:07Z"
    assert implication["effectiveDateTime"] == "2025-03-04T05:06:07Z"


def test_first_classification_at_release_activation(db: SQLAlchemy) -> None:
    # Ingested while no release was active, then classified when R1 was activated.
    release = make_release(current=True, activated_at=datetime(2025, 6, 1, 12, 0, 0))
    patient = make_patient()
    variant = add_patient_variant(patient, "12", 120978900, "G", "A", classification="P", release=release)
    variant.created_at = datetime(2025, 5, 1, 12, 0, 0)
    implication = with_profile(build_patient_bundle(patient), IMPLICATION_PROFILE)[0]
    assert implication["issued"] == "2025-06-01T12:00:00Z"


def test_amended_without_reclassification_time_falls_back(db: SQLAlchemy, record: Record) -> None:
    record.amended.last_reclassified_at = None
    result = build_patient_bundle(record.patient)
    variant = variant_observation(result, "G", "A", 44150500)
    assert variant["status"] == "amended"
    assert variant["effectiveDateTime"] == "2025-02-01T09:30:15Z"
    # The classification release's activation (R2) is the best remaining reclassification time.
    assert variant["issued"] == implication_for(result, variant)["issued"] == "2026-01-12T07:00:00Z"


def test_provenance_note_for_expert_panel_variant(bundle: dict[str, Any]) -> None:
    implication = implication_for(bundle, variant_observation(bundle, "G", "A", 120978900))
    assert implication["note"] == [{"text": (
        "Classified against R1 (2025-01, synthetic), review status reviewed by expert panel (3 stars), "
        "expert panel yes; reclassified 0 time(s)."
    )}]


def test_provenance_note_for_amended_variant(bundle: dict[str, Any]) -> None:
    implication = implication_for(bundle, variant_observation(bundle, "G", "A", 44150500))
    assert implication["note"] == [{"text": (
        "Classified against R2 (2026-01, synthetic), review status criteria provided, single submitter (1 star), "
        "expert panel no; reclassified 1 time(s); first reported as Likely pathogenic."
    )}]


def test_provenance_note_without_release_or_known_variant(db: SQLAlchemy) -> None:
    patient = make_patient()
    add_patient_variant(patient, "12", 120978900, "G", "A", classification="LP", release=None)
    result = build_patient_bundle(patient)
    implication = with_profile(result, IMPLICATION_PROFILE)[0]
    assert implication["note"][0]["text"] == (
        "Classified against an unrecorded knowledge-base release, review status not provided, "
        "expert panel no; reclassified 0 time(s)."
    )
    assert component(resources(result)[1], "81290-9") is None
    validate_bundle(result)


def test_provenance_note_for_overlay_expert_panel_without_review_status(db: SQLAlchemy) -> None:
    release = make_release("R-overlay", current=True)
    add_known_variant(release, "12", 120978900, "G", "A", "LP", is_expert_panel=True,
                      classification_source="expert_panel_overlay", review_status=None)
    patient = make_patient()
    add_patient_variant(patient, "12", 120978900, "G", "A", classification="LP", release=release)
    implication = with_profile(build_patient_bundle(patient), IMPLICATION_PROFILE)[0]
    assert implication["note"][0]["text"] == (
        "Classified against R-overlay, review status not provided, expert panel yes; reclassified 0 time(s)."
    )


# ---------------------------------------------------------------------------
# Queries
# ---------------------------------------------------------------------------


@pytest.fixture
def statements(db: SQLAlchemy) -> Iterator[list[str]]:
    executed: list[str] = []

    def record_statement(conn: Any, cursor: Any, statement: str, *args: Any) -> None:
        executed.append(statement)

    event.listen(db.engine, "before_cursor_execute", record_statement)
    try:
        yield executed
    finally:
        event.remove(db.engine, "before_cursor_execute", record_statement)


def _patient_with_variants(count: int, release: KbRelease, first_pos: int) -> Patient:
    patient = make_patient()
    upload = add_upload(patient)
    for pos in range(first_pos, first_pos + count):
        add_known_variant(release, "12", pos, "C", "T", "P", hgvs=f"hgvs-{pos}", condition="MODY")
        add_patient_variant(patient, "12", pos, "C", "T", classification="P", release=release).upload = upload
    return patient


def test_query_count_does_not_grow_with_variants(db: SQLAlchemy, statements: list[str]) -> None:
    release = make_release(current=True)
    small, large = _patient_with_variants(1, release, 120980000), _patient_with_variants(12, release, 120990000)
    db.session.commit()
    db.session.expire_all()

    statements.clear()
    build_patient_bundle(db.session.get(Patient, small.id))
    small_count = len(statements)
    db.session.expire_all()
    statements.clear()
    result = build_patient_bundle(db.session.get(Patient, large.id))
    assert len(statements) == small_count <= 3  # patient row + variants + known variants
    assert len(with_profile(result, VARIANT_PROFILE)) == 12
    assert all(component(obs, "81290-9") for obs in with_profile(result, VARIANT_PROFILE))


def test_export_is_read_only(db: SQLAlchemy, record: Record) -> None:
    build_patient_bundle(record.patient)
    assert not db.session.new and not db.session.dirty and not db.session.deleted


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def test_export_validates_with_r4b_models(bundle: dict[str, Any]) -> None:
    validate_bundle(bundle)
    parsed = Bundle.model_validate(bundle)
    assert isinstance(parsed.entry[0].resource, FhirPatient)
    assert all(isinstance(entry.resource, Observation) for entry in parsed.entry[1:])


def _first_observation(bundle: dict[str, Any]) -> dict[str, Any]:
    return bundle["entry"][1]["resource"]


@pytest.mark.parametrize(
    ("break_bundle", "message"),
    [
        (lambda b: _first_observation(b).pop("status"),
         "entry[1].resource.status: Value for the field 'status' is required"),
        (lambda b: b.update(timestamp="2026-01-01T00:00:00"),
         "timestamp: Instant value string does not match spec regex"),
        (lambda b: _first_observation(b).update(issued="yesterday"), "entry[1].resource.issued:"),
        (lambda b: _first_observation(b).update(colour="blue"),
         "entry[1].resource.colour: Extra inputs are not permitted"),
        (lambda b: _first_observation(b)["component"][4]["valueRange"]["low"].update(value="ten"),
         "entry[1].resource.component[4].valueRange.low.value:"),
        (lambda b: _first_observation(b).update(resourceType="GenomicVariant"),
         "entry[1].resource.resourceType: 'GenomicVariant' is not an R4B resource type"),
        (lambda b: _first_observation(b).pop("resourceType"), "entry[1].resource.resourceType: missing resource type"),
        (lambda b: b.update(resourceType="Patient"), "expects resource type ``Bundle``"),
    ],
    ids=["missing-status", "naive-timestamp", "bad-issued", "unknown-element", "bad-decimal", "unknown-type",
         "missing-type", "not-a-bundle"],
)
def test_validate_bundle_rejects_broken_bundles(bundle: dict[str, Any], break_bundle: Any, message: str) -> None:
    broken = copy.deepcopy(bundle)
    break_bundle(broken)
    with pytest.raises(ValueError, match=r"^Invalid FHIR R4B Bundle \(\d+ errors?\): ") as caught:
        validate_bundle(broken)
    assert message in str(caught.value)


def test_validate_bundle_rejects_non_objects() -> None:
    with pytest.raises(ValueError, match=r"^A FHIR Bundle must be a JSON object, not list\.$"):
        validate_bundle([])  # type: ignore[arg-type]


def test_validate_bundle_reports_error_count_and_truncates(bundle: dict[str, Any]) -> None:
    broken = copy.deepcopy(bundle)
    template = _first_observation(broken)
    broken["entry"] = [{"resource": {**template, "issued": "never", "effectiveDateTime": "never"}} for _ in range(15)]
    with pytest.raises(ValueError) as caught:
        validate_bundle(broken)
    message = str(caught.value)
    assert message.startswith("Invalid FHIR R4B Bundle (30 errors): ")
    assert message.endswith("; … and 10 more")
    assert message.count("entry[") == 20


def test_validate_bundle_tolerates_entries_without_resources(bundle: dict[str, Any]) -> None:
    trimmed = copy.deepcopy(bundle)
    trimmed["entry"].append({"fullUrl": "urn:uuid:7f1c0e6c-0d55-4a77-9f3c-111111111111"})
    validate_bundle(trimmed)
    trimmed["entry"] = "not a list"
    with pytest.raises(ValueError, match="entry"):
        validate_bundle(trimmed)


# ---------------------------------------------------------------------------
# Instant formatting
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (datetime(2026, 1, 15, 9, 30, 0, 123456), "2026-01-15T09:30:00Z"),
        (datetime(2026, 1, 15, 11, 30, 0, tzinfo=timezone(timedelta(hours=2))), "2026-01-15T09:30:00Z"),
        (datetime(2026, 1, 15, 9, 30, 0, tzinfo=timezone.utc), "2026-01-15T09:30:00Z"),
    ],
)
def test_fhir_instant(value: datetime, expected: str) -> None:
    assert fhir_instant(value) == expected
