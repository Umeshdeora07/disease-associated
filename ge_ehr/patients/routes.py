"""Placeholder routes of the patients blueprint (``/patients...``).

Owned by the UI-A work package, which replaces this file keeping the endpoint names, URL rules
and role restrictions (SPEC §8.1, §8.2). Created by the Foundation work package.
"""

from __future__ import annotations

from flask import Blueprint, render_template

from ..constants import CLINICAL_ROLES
from ..security import roles_required

bp = Blueprint("patients", __name__, url_prefix="/patients")


def _placeholder(title: str) -> str:
    return render_template("placeholder.html", title=title, owner="UI-A")


@bp.route("")
@roles_required()
def list_patients() -> str:
    return _placeholder("Patients")


@bp.route("/new", methods=["GET", "POST"])
@roles_required(*CLINICAL_ROLES)
def new_patient() -> str:
    return _placeholder("New patient")


@bp.route("/<int:patient_id>")
@roles_required()
def view_patient(patient_id: int) -> str:
    return _placeholder("Patient record")


@bp.route("/<int:patient_id>/edit", methods=["GET", "POST"])
@roles_required(*CLINICAL_ROLES)
def edit_patient(patient_id: int) -> str:
    return _placeholder("Edit patient")


@bp.route("/<int:patient_id>/diagnosis", methods=["POST"])
@roles_required(*CLINICAL_ROLES)
def update_diagnosis(patient_id: int) -> str:
    return _placeholder("Update diagnosis")


@bp.route("/<int:patient_id>/medications", methods=["POST"])
@roles_required(*CLINICAL_ROLES)
def add_medication(patient_id: int) -> str:
    return _placeholder("Add medication")


@bp.route("/<int:patient_id>/medications/<int:medication_id>/stop", methods=["POST"])
@roles_required(*CLINICAL_ROLES)
def stop_medication(patient_id: int, medication_id: int) -> str:
    return _placeholder("Stop medication")


@bp.route("/<int:patient_id>/vcf", methods=["GET", "POST"])
@roles_required()
def upload_vcf(patient_id: int) -> str:
    return _placeholder("Upload VCF")


@bp.route("/<int:patient_id>/recommendations")
@roles_required(*CLINICAL_ROLES)
def recommendations(patient_id: int) -> str:
    return _placeholder("Recommendations")
