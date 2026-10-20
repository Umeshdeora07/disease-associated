"""API routes (JSON): ``GET /patients/<id>/fhir`` (FHIR export, F4) and ``GET /health``.

The FHIR endpoint answers in ``application/fhir+json``: the patient's Bundle on success and a
FHIR ``OperationOutcome`` for every failure (401 anonymous, 403 role without access, 404 unknown
patient, 500 export failure), so FHIR clients never receive an HTML page or a non-FHIR body.
Both routes are GET-only, so CSRF protection does not apply.

Owned by the FHIR work package (the health check was created by the Foundation work package).
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Callable
from functools import wraps
from typing import Any, ParamSpec

from flask import Blueprint, Response, jsonify, request
from flask_login import current_user
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from werkzeug.exceptions import Forbidden

from ..constants import CLINICAL_ROLES
from ..extensions import db
from ..models import KbRelease, Patient
from ..security import audit, roles_required
from ..services.fhir_export import build_patient_bundle, validate_bundle

logger = logging.getLogger(__name__)

bp = Blueprint("api", __name__)

P = ParamSpec("P")

FHIR_JSON_MIMETYPE = "application/fhir+json"
_TRUE_VALUES = frozenset({"1", "true", "yes"})
_UNSAFE_FILENAME_CHARACTERS = re.compile(r"[^A-Za-z0-9._-]")

UNAUTHENTICATED_MESSAGE = "Authentication is required: sign in to GE-EHR as a clinician or administrator."
FORBIDDEN_MESSAGE = (
    "Your role does not permit FHIR export; only clinicians and administrators may export patient "
    "records. The attempt has been recorded in the audit log."
)
EXPORT_FAILED_MESSAGE = (
    "The FHIR export could not be produced because the generated Bundle failed validation. "
    "The error has been logged; please contact the system administrator."
)


def operation_outcome(status: int, issue_code: str, message: str) -> Response:
    """A FHIR ``OperationOutcome`` response with one error issue (``issue_code`` from FHIR IssueType)."""
    outcome = {
        "resourceType": "OperationOutcome",
        "issue": [{"severity": "error", "code": issue_code, "diagnostics": message}],
    }
    return _fhir_response(outcome, status)


def fhir_access_errors(view: Callable[P, Response]) -> Callable[P, Response]:
    """Report authentication and authorisation failures of a FHIR view as OperationOutcomes.

    Wraps a view protected by :func:`ge_ehr.security.roles_required`: anonymous requests get a
    401 ``login`` outcome, and the 403 raised for a role without access (after ``roles_required``
    has committed its ``access_denied`` audit row) becomes a 403 ``forbidden`` outcome.
    """

    @wraps(view)
    def wrapped(*args: P.args, **kwargs: P.kwargs) -> Response:
        if not current_user.is_authenticated:
            return operation_outcome(401, "login", UNAUTHENTICATED_MESSAGE)
        try:
            return view(*args, **kwargs)
        except Forbidden:
            return operation_outcome(403, "forbidden", FORBIDDEN_MESSAGE)

    return wrapped


@bp.route("/patients/<int:patient_id>/fhir")
@fhir_access_errors
@roles_required(*CLINICAL_ROLES)
def patient_fhir(patient_id: int) -> Response:
    """The patient's validated FHIR R4 Bundle; ``?download=1`` serves it as a file attachment."""
    patient = db.session.get(Patient, patient_id)
    if patient is None:
        return operation_outcome(404, "not-found", f"Patient {patient_id} does not exist.")

    bundle = build_patient_bundle(patient, base_url=request.url_root)
    try:
        validate_bundle(bundle)
    except ValueError:
        logger.exception("FHIR export of patient %s produced an invalid Bundle", patient.mrn)
        return operation_outcome(500, "exception", EXPORT_FAILED_MESSAGE)

    download = request.args.get("download", "").strip().lower() in _TRUE_VALUES
    audit(
        "fhir_export",
        "patient",
        patient.id,
        detail={"mrn": patient.mrn, "resources": len(bundle["entry"]), "download": download},
    )
    db.session.commit()

    response = _fhir_response(bundle, 200)
    if download:
        response.headers.set("Content-Disposition", "attachment", filename=download_filename(patient.mrn))
    return response


def download_filename(mrn: str) -> str:
    """``GE-EHR_<MRN>_fhir.json`` with characters outside ``[A-Za-z0-9._-]`` replaced by ``_``."""
    return f"GE-EHR_{_UNSAFE_FILENAME_CHARACTERS.sub('_', mrn)}_fhir.json"


def _fhir_response(resource: dict[str, Any], status: int) -> Response:
    return Response(json.dumps(resource, indent=2), status=status, mimetype=FHIR_JSON_MIMETYPE)


@bp.route("/health")
def health() -> tuple[Response, int]:
    """Liveness/readiness probe (no authentication): database check and current release."""
    try:
        db.session.execute(text("SELECT 1"))
        release = KbRelease.current()
    except SQLAlchemyError:
        logger.exception("Health check: database unavailable")
        db.session.rollback()
        return jsonify(status="error", db="error", current_release=None), 503
    return jsonify(status="ok", db="ok", current_release=release.label if release else None), 200
