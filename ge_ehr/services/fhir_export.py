"""F4 — FHIR R4 Bundle export using the HL7 Genomics Reporting variant profiles (SPEC §9).

:func:`build_patient_bundle` turns a patient and their stored variants into a FHIR ``collection``
Bundle: one Patient, one *variant* Observation per PatientVariant and, for every variant whose
current classification is not ``NC``, one *diagnostic-implication* Observation derived from it.
Both Observations are exported with status ``amended`` once a release activation has changed the
variant's classification (paper §3, Table IV), so downstream systems can see that an earlier
report was revised. The structure is a simplified subset of the HL7 Genomics Reporting IG 2.0.0
profiles; the codes come from :mod:`ge_ehr.constants` and the module constants below.

:func:`validate_bundle` parses a bundle with the ``fhir.resources.R4B`` models. fhir.resources 8
ships R4B (4.3), which is structurally identical to R4 for the resources used here.

Every resource is tagged ``HTEST`` (test health data): GE-EHR stores synthetic data only.

Owned by the FHIR work package.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Iterable, Mapping, Sequence
from datetime import datetime, timezone
from itertools import batched
from typing import Any

from fhir.resources.R4B import get_fhir_model_class
from fhir.resources.R4B.bundle import Bundle
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.orm import joinedload

from .. import constants as C
from ..extensions import db
from ..models import KnownVariant, Patient, PatientVariant, utcnow
from .classification import IN_CLAUSE_CHUNK

logger = logging.getLogger(__name__)

FhirResource = dict[str, Any]
Code = tuple[str, str]  # (code, display)

# ---------------------------------------------------------------------------
# Code systems, profiles and codes
# ---------------------------------------------------------------------------

ACT_REASON_SYSTEM = "http://terminology.hl7.org/CodeSystem/v3-ActReason"
OBSERVATION_CATEGORY_SYSTEM = "http://terminology.hl7.org/CodeSystem/observation-category"
DIAGNOSTIC_SERVICE_SYSTEM = "http://terminology.hl7.org/CodeSystem/v2-0074"
HGNC_SYSTEM = "http://www.genenames.org/geneId"
REFSEQ_SYSTEM = "http://www.ncbi.nlm.nih.gov/refseq"
GENOMICS_TBD_CODES_SYSTEM = "http://hl7.org/fhir/uv/genomics-reporting/CodeSystem/tbd-codes-cs"
MRN_SYSTEM = "urn:ge-ehr:mrn"

VARIANT_PROFILE = "http://hl7.org/fhir/uv/genomics-reporting/StructureDefinition/variant"
DIAGNOSTIC_IMPLICATION_PROFILE = "http://hl7.org/fhir/uv/genomics-reporting/StructureDefinition/diagnostic-implication"

HTEST_TAG: dict[str, str] = {"system": ACT_REASON_SYSTEM, "code": "HTEST", "display": "test health data"}
LABORATORY_CATEGORY: Code = ("laboratory", "Laboratory")
GENETICS_CATEGORY: Code = ("GE", "Genetics")
DIAGNOSTIC_IMPLICATION_CODE: Code = ("diagnostic-implication", "Diagnostic Implication")

# LOINC observation and component codes.
GENETIC_VARIANT_ASSESSMENT: Code = ("69548-6", "Genetic variant assessment")
GENE_STUDIED: Code = ("48018-6", "Gene studied [ID]")
GENOMIC_REFERENCE_SEQUENCE: Code = ("48013-7", "Genomic reference sequence [ID]")
REFERENCE_ASSEMBLY: Code = ("62374-4", "Human reference sequence assembly version")
COORDINATE_SYSTEM: Code = ("92822-6", "Genomic coordinate system [Type]")
ALLELE_START_END: Code = ("81254-5", "Genomic allele start-end")
REF_ALLELE: Code = ("69547-8", "Genomic ref allele [ID]")
ALT_ALLELE: Code = ("69551-0", "Genomic alt allele [ID]")
ALLELIC_STATE: Code = ("53034-5", "Allelic state")
GENOMIC_HGVS: Code = ("81290-9", "Genomic DNA change (gHGVS)")
CLINICAL_SIGNIFICANCE: Code = ("53037-8", "Genetic variation clinical significance [Imp]")
ASSOCIATED_PHENOTYPE: Code = ("81259-4", "Associated phenotype")

# LOINC answer codes.
PRESENT: Code = ("LA9633-4", "Present")
GRCH38: Code = ("LA26806-2", "GRCh38")
ONE_BASED_CHARACTER_COUNTING: Code = ("LA30102-0", "1-based character counting")

# ClinVar CLNDN placeholders that mean "no condition stated".
_UNSTATED_CONDITIONS = frozenset({"not provided", "not specified"})

# Namespace of the deterministic resource UUIDs. Names are built from the MRN (the patient's
# stable business identifier) and the variant key, so exporting the same record again — even
# from a re-seeded database — yields the same fullUrls; an amended Observation keeps its id.
_UUID_NAMESPACE = uuid.uuid5(uuid.NAMESPACE_URL, "urn:ge-ehr:fhir")

# Upper bound on the validation errors quoted in a ValueError message.
_MAX_REPORTED_ERRORS = 20


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def build_patient_bundle(patient: Patient, *, base_url: str | None = None) -> dict[str, Any]:
    """Build the JSON-ready FHIR collection Bundle for ``patient``.

    Entries: the Patient, then per PatientVariant (in id order) its variant Observation followed
    by its diagnostic-implication Observation when the current class is not ``NC``. Provenance
    (hgvs, condition, review status, expert-panel flag) comes from the KnownVariant of each
    variant's ``classification_release``. ``base_url`` is the server root (e.g.
    ``https://ge-ehr.example.org/``); when given, the bundle carries a ``self`` link to its
    export endpoint. Two queries in total, whatever the number of variants. Read-only.
    """
    variants = _load_patient_variants(patient)
    known = _known_variants_for(variants)

    patient_resource = _patient_resource(patient)
    subject = {"reference": _full_url(patient_resource)}
    resources: list[FhirResource] = [patient_resource]
    for variant in variants:
        known_variant = known.get((variant.classification_release_id, variant.variant_key_id))
        observation = _variant_observation(patient, variant, known_variant, subject)
        resources.append(observation)
        if variant.current_classification != "NC":
            resources.append(_implication_observation(patient, variant, known_variant, subject, observation))

    bundle: dict[str, Any] = {
        "resourceType": "Bundle",
        "meta": {"tag": [dict(HTEST_TAG)]},
        "type": "collection",
        "timestamp": fhir_instant(utcnow()),
    }
    if base_url:
        bundle["link"] = [{"relation": "self", "url": f"{base_url.rstrip('/')}/patients/{patient.id}/fhir"}]
    bundle["entry"] = [{"fullUrl": _full_url(resource), "resource": resource} for resource in resources]
    logger.debug("Built FHIR bundle for patient %s: %d resource(s)", patient.mrn, len(resources))
    return bundle


def validate_bundle(bundle: dict[str, Any]) -> None:
    """Validate ``bundle`` with the ``fhir.resources.R4B`` models; raise ValueError if invalid.

    Every entry must name a known R4B ``resourceType``; the Bundle model then parses the bundle
    and each entry resource with that resource type's model. The ValueError message summarises
    the pydantic errors with their location, e.g.
    ``entry[1].resource.status: Value for the field 'status' is required.``
    """
    if not isinstance(bundle, Mapping):
        raise ValueError(f"A FHIR Bundle must be a JSON object, not {type(bundle).__name__}.")
    problems = _entry_resource_type_problems(bundle.get("entry"))
    if problems:
        raise ValueError(_validation_message(problems))
    try:
        Bundle.model_validate(bundle)
    except ValidationError as error:
        raise ValueError(_validation_message([_describe_error(item) for item in error.errors()])) from None


def fhir_instant(value: datetime) -> str:
    """Format a datetime as a FHIR ``instant`` (``2026-01-15T09:30:00Z``).

    Naive datetimes are taken to be UTC (the storage convention); aware ones are converted.
    """
    if value.tzinfo is not None:
        value = value.astimezone(timezone.utc)
    return value.strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------------------
# Queries
# ---------------------------------------------------------------------------


def _load_patient_variants(patient: Patient) -> list[PatientVariant]:
    """The patient's variants with key, upload and classification release (one query)."""
    statement = (
        select(PatientVariant)
        .where(PatientVariant.patient_id == patient.id)
        .options(
            joinedload(PatientVariant.variant_key),
            joinedload(PatientVariant.upload),
            joinedload(PatientVariant.classification_release),
        )
        .order_by(PatientVariant.id)
    )
    return list(db.session.execute(statement).scalars())


def _known_variants_for(variants: Sequence[PatientVariant]) -> dict[tuple[int | None, int], KnownVariant]:
    """KnownVariants keyed by (release id, variant key id) for each variant's classification release.

    One bulk query (chunked for very large variant sets) over the releases and keys involved;
    the few rows of unrelated (release, key) combinations it may return are dropped.
    """
    wanted = {
        (variant.classification_release_id, variant.variant_key_id)
        for variant in variants
        if variant.classification_release_id is not None
    }
    if not wanted:
        return {}
    release_ids = sorted({release_id for release_id, _ in wanted})
    key_ids = sorted({key_id for _, key_id in wanted})
    found: dict[tuple[int | None, int], KnownVariant] = {}
    for chunk in batched(key_ids, IN_CLAUSE_CHUNK, strict=False):
        statement = select(KnownVariant).where(
            KnownVariant.release_id.in_(release_ids), KnownVariant.variant_key_id.in_(chunk)
        )
        for known in db.session.execute(statement).scalars():
            pair = (known.release_id, known.variant_key_id)
            if pair in wanted:
                found[pair] = known
    return found


# ---------------------------------------------------------------------------
# Resources
# ---------------------------------------------------------------------------


def _patient_resource(patient: Patient) -> FhirResource:
    """Patient: MRN identifier, official name (empty parts omitted), gender and birth date."""
    name: dict[str, Any] = {"use": "official"}
    if patient.family_name.strip():
        name["family"] = patient.family_name.strip()
    if patient.given_name.strip():
        name["given"] = [patient.given_name.strip()]
    return {
        "resourceType": "Patient",
        "id": _resource_id("Patient", patient.mrn),
        "meta": {"tag": [dict(HTEST_TAG)]},
        "identifier": [{"system": MRN_SYSTEM, "value": patient.mrn}],
        "name": [name],
        # GE-EHR's SEXES are exactly FHIR's AdministrativeGender codes.
        "gender": patient.sex,
        "birthDate": patient.birth_date.isoformat(),
    }


def _variant_observation(
    patient: Patient, variant: PatientVariant, known: KnownVariant | None, subject: dict[str, str]
) -> FhirResource:
    """Variant Observation: gene, reference sequence/assembly, position, alleles, zygosity, HGVS.

    ``effectiveDateTime`` is when the variant was reported (its latest upload); ``issued`` is
    that time, or the time of the reclassification once the record is amended.
    """
    key = variant.variant_key
    observed_at = _observed_at(variant)
    issued_at = _classified_at(variant) if variant.is_amended else observed_at

    components: list[FhirResource] = []
    gene = C.GENE_INFO.get(variant.gene or key.gene or "")
    if gene is not None:
        components.append(
            _component(GENE_STUDIED, valueCodeableConcept=_concept(HGNC_SYSTEM, (gene.hgnc_id, gene.symbol)))
        )
        components.append(
            _component(
                GENOMIC_REFERENCE_SEQUENCE,
                valueCodeableConcept={"coding": [{"system": REFSEQ_SYSTEM, "code": gene.refseq_chrom}]},
            )
        )
    components.extend(
        [
            _component(REFERENCE_ASSEMBLY, valueCodeableConcept=_loinc_concept(GRCH38)),
            _component(COORDINATE_SYSTEM, valueCodeableConcept=_loinc_concept(ONE_BASED_CHARACTER_COUNTING)),
            # 1-based, inclusive span of the reference allele.
            _component(
                ALLELE_START_END,
                valueRange={"low": {"value": key.pos}, "high": {"value": key.pos + len(key.ref) - 1}},
            ),
            _component(REF_ALLELE, valueString=key.ref),
            _component(ALT_ALLELE, valueString=key.alt),
            _component(ALLELIC_STATE, valueCodeableConcept=_loinc_concept(C.ZYGOSITY_TO_LOINC[variant.zygosity])),
        ]
    )
    if known is not None and known.hgvs:
        components.append(_component(GENOMIC_HGVS, valueCodeableConcept={"text": known.hgvs}))

    return {
        "resourceType": "Observation",
        "id": _resource_id("Observation", "variant", patient.mrn, key.label),
        "meta": {"profile": [VARIANT_PROFILE], "tag": [dict(HTEST_TAG)]},
        "status": _status(variant),
        "category": _genetics_categories(),
        "code": _loinc_concept(GENETIC_VARIANT_ASSESSMENT),
        "subject": dict(subject),
        "effectiveDateTime": fhir_instant(observed_at),
        "issued": fhir_instant(issued_at),
        "valueCodeableConcept": _loinc_concept(PRESENT),
        "component": components,
    }


def _implication_observation(
    patient: Patient,
    variant: PatientVariant,
    known: KnownVariant | None,
    subject: dict[str, str],
    variant_observation: FhirResource,
) -> FhirResource:
    """Diagnostic-implication Observation: clinical significance, phenotype and provenance note.

    ``effectiveDateTime`` and ``issued`` are the time the current classification was assigned.
    """
    components: list[FhirResource] = [
        _component(
            CLINICAL_SIGNIFICANCE,
            valueCodeableConcept=_loinc_concept(C.CLASS_TO_LOINC[variant.current_classification]),
        )
    ]
    condition = (known.condition or "").strip() if known is not None else ""
    if condition and condition.lower() not in _UNSTATED_CONDITIONS:
        components.append(_component(ASSOCIATED_PHENOTYPE, valueCodeableConcept={"text": condition}))
    classified_at = fhir_instant(_classified_at(variant))
    return {
        "resourceType": "Observation",
        "id": _resource_id("Observation", "diagnostic-implication", patient.mrn, variant.variant_key.label),
        "meta": {"profile": [DIAGNOSTIC_IMPLICATION_PROFILE], "tag": [dict(HTEST_TAG)]},
        "status": _status(variant),
        "category": _genetics_categories(),
        "code": _concept(GENOMICS_TBD_CODES_SYSTEM, DIAGNOSTIC_IMPLICATION_CODE),
        "subject": dict(subject),
        "effectiveDateTime": classified_at,
        "issued": classified_at,
        "derivedFrom": [{"reference": _full_url(variant_observation)}],
        "component": components,
        "note": [{"text": _provenance_note(variant, known)}],
    }


def _provenance_note(variant: PatientVariant, known: KnownVariant | None) -> str:
    """Provenance text: release, review status (stars), expert-panel flag, reclassification count.

    E.g. ``Classified against R2 (2026-01, synthetic), review status reviewed by expert panel
    (3 stars), expert panel yes; reclassified 1 time(s); first reported as Likely pathogenic.``
    """
    release = variant.classification_release
    source = release.label if release is not None else "an unrecorded knowledge-base release"
    if known is not None and known.review_status:
        stars = known.review_stars
        review = f"{known.review_status.replace('_', ' ')} ({stars} star{'' if stars == 1 else 's'})"
    else:
        review = "not provided"
    expert_panel = "yes" if known is not None and known.is_expert_panel else "no"
    note = (
        f"Classified against {source}, review status {review}, expert panel {expert_panel}; "
        f"reclassified {variant.reclassified_count or 0} time(s)"
    )
    if variant.is_amended:
        note += f"; first reported as {C.CLASS_LABELS[variant.reported_classification]}"
    return note + "."


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _status(variant: PatientVariant) -> str:
    return "amended" if variant.is_amended else "final"


def _observed_at(variant: PatientVariant) -> datetime:
    """When the variant was reported: its latest upload, else when it was stored."""
    return variant.upload.uploaded_at if variant.upload is not None else variant.created_at


def _classified_at(variant: PatientVariant) -> datetime:
    """When the current classification was assigned.

    The last reclassification for amended variants; otherwise the first classification, i.e. the
    later of storing the variant and activating its classification release (a variant ingested
    before any release was active is first classified when a release is activated).
    """
    if variant.is_amended and variant.last_reclassified_at is not None:
        return variant.last_reclassified_at
    release = variant.classification_release
    if release is not None and release.activated_at is not None:
        return max(variant.created_at, release.activated_at)
    return variant.created_at


def _resource_id(*name_parts: str) -> str:
    """Deterministic UUID (version 5) for a resource; also used as its ``id``."""
    return str(uuid.uuid5(_UUID_NAMESPACE, "/".join(name_parts)))


def _full_url(resource: Mapping[str, Any]) -> str:
    return f"urn:uuid:{resource['id']}"


def _concept(system: str, code: Code) -> dict[str, Any]:
    return {"coding": [{"system": system, "code": code[0], "display": code[1]}]}


def _loinc_concept(code: Code) -> dict[str, Any]:
    return _concept(C.LOINC, code)


def _component(code: Code, **value: Any) -> FhirResource:
    """An Observation component with a LOINC code and one ``value[x]`` element."""
    return {"code": _loinc_concept(code), **value}


def _genetics_categories() -> list[dict[str, Any]]:
    return [
        _concept(OBSERVATION_CATEGORY_SYSTEM, LABORATORY_CATEGORY),
        _concept(DIAGNOSTIC_SERVICE_SYSTEM, GENETICS_CATEGORY),
    ]


def _entry_resource_type_problems(entries: Any) -> list[str]:
    """Problems with the ``resourceType`` of entry resources (unknown types break the Bundle model)."""
    if not isinstance(entries, list):
        return []  # absent, or reported by the Bundle model itself
    problems: list[str] = []
    for index, entry in enumerate(entries):
        resource = entry.get("resource") if isinstance(entry, Mapping) else None
        if not isinstance(resource, Mapping):
            continue  # an entry without a resource, or a non-object reported by the Bundle model
        resource_type = resource.get("resourceType")
        if not isinstance(resource_type, str) or not resource_type:
            problems.append(f"entry[{index}].resource.resourceType: missing resource type.")
            continue
        try:
            get_fhir_model_class(resource_type)
        except ValueError:
            problems.append(f"entry[{index}].resource.resourceType: {resource_type!r} is not an R4B resource type.")
    return problems


def _describe_error(error: Mapping[str, Any]) -> str:
    """``entry[0].resource.issued: Instant value string does not match spec regex.``"""
    location = ""
    for part in error.get("loc", ()):
        location += f"[{part}]" if isinstance(part, int) else (f".{part}" if location else str(part))
    message = str(error.get("msg", "invalid value")).removeprefix("Value error, ")
    return f"{location or 'Bundle'}: {message}"


def _validation_message(problems: Iterable[str]) -> str:
    """``Invalid FHIR R4B Bundle (2 errors): <problem>; <problem>`` (at most 20 problems quoted)."""
    problems = list(problems)
    shown = problems[:_MAX_REPORTED_ERRORS]
    message = f"Invalid FHIR R4B Bundle ({len(problems)} error{'' if len(problems) == 1 else 's'}): " + "; ".join(
        shown
    )
    if len(problems) > len(shown):
        message += f"; … and {len(problems) - len(shown)} more"
    return message
