"""API routes: ``GET /patients/<id>/fhir`` (placeholder) and ``GET /health``.

Owned by the FHIR work package, which replaces the FHIR placeholder keeping the endpoint names.
Created by the Foundation work package; ``health`` is complete.
"""

from __future__ import annotations

import logging

from flask import Blueprint, Response, jsonify
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from ..constants import CLINICAL_ROLES
from ..extensions import db
from ..models import KbRelease
from ..security import roles_required

logger = logging.getLogger(__name__)

bp = Blueprint("api", __name__)


@bp.route("/patients/<int:patient_id>/fhir")
@roles_required(*CLINICAL_ROLES)
def patient_fhir(patient_id: int) -> tuple[Response, int]:
    return jsonify(error="Not implemented", message="FHIR export is not available yet.", status=501), 501


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
