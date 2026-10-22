"""Routes of the patients blueprint (``/patients…``): F1 pages, VCF upload and recommendations.

Every change goes through :mod:`ge_ehr.services.patient_records`, which validates, audits and
re-evaluates decision support for the patient; routes only translate between HTTP and the
service, commit, flash a summary and redirect (POST-redirect-GET). A rejected submission is
re-rendered with its messages and HTTP 400 — for a rejected VCF (S10) only the rejected-upload
record and its audit row are kept.

Roles (SPEC §8.1): every role may list and view patients and upload VCFs; only clinicians and
administrators may change records or see recommendations (lab technicians get 403 and an
``access_denied`` audit row, S12). Templates hide what a role cannot use.

Owned by the UI-A work package.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from flask import Blueprint, abort, current_app, flash, redirect, render_template, request, url_for
from flask_login import current_user
from werkzeug.wrappers import Response

from .. import constants as C
from ..alerts import queries as alert_queries
from ..alerts.forms import acknowledge_form
from ..constants import CLINICAL_ROLES
from ..extensions import db
from ..models import GenomicUpload, Medication, Patient, utc_today
from ..security import roles_required
from ..services import patient_records
from ..services.alerting import ReconcileResult
from ..services.vcf_parser import VcfError
from . import queries
from .forms import DiagnosisForm, MedicationForm, PatientForm, VcfUploadForm, drug_suggestions

logger = logging.getLogger(__name__)

bp = Blueprint("patients", __name__, url_prefix="/patients")

#: Uploads listed beside the VCF upload form.
RECENT_UPLOADS = 5


def _per_page() -> int:
    return int(current_app.config["ITEMS_PER_PAGE"])


def _can_see_alerts() -> bool:
    return current_user.has_role(*CLINICAL_ROLES)


def _current_path() -> str:
    """Path and query of this request, used as the ``next`` target of forms posted from the page."""
    return request.full_path.rstrip("?")


def _today() -> str:
    """Today (UTC) as the ``max`` of date inputs that cannot be in the future."""
    return utc_today().isoformat()


def _plural(count: int, singular: str, plural: str | None = None) -> str:
    return f"{count} {singular if count == 1 else (plural or singular + 's')}"


def describe_outcome(result: ReconcileResult | None) -> str:
    """One sentence summarising how a change affected the patient's alerts and notes."""
    if result is None:
        return "No panel variant to evaluate."
    created_alerts = sum(alert.kind == "alert" for alert in result.created)
    created_notes = len(result.created) - created_alerts
    parts = [
        _plural(created_alerts, "new alert") if created_alerts else "",
        _plural(created_notes, "new note") if created_notes else "",
        f"{len(result.superseded)} superseded" if result.superseded else "",
        _plural(len(result.confirmed), "provenance update") if result.confirmed else "",
    ]
    changes = [part for part in parts if part]
    if not changes:
        return "Decision support re-evaluated: no change to alerts or notes."
    return "Decision support re-evaluated: " + ", ".join(changes) + "."


# ---------------------------------------------------------------------------
# Patient list, creation and editing
# ---------------------------------------------------------------------------


@bp.route("")
@roles_required()
def list_patients() -> str:
    """Patients with search, filters and per-row aggregates (alerts only for clinical roles)."""
    show_alerts = _can_see_alerts()
    filters = queries.PatientListFilters.from_args(request.args, allow_alert_filter=show_alerts)
    pagination = db.paginate(
        queries.patient_list_statement(filters),
        page=request.args.get("page", 1, type=int),
        per_page=_per_page(),
        error_out=False,
    )
    return render_template(
        "patients/list.html",
        pagination=pagination,
        rows=queries.patient_list_rows(pagination.items, include_alerts=show_alerts),
        filters=filters,
        family_size=queries.family_size(filters.family) if filters.family else None,
        show_alerts=show_alerts,
    )


@bp.route("/new", methods=["GET", "POST"])
@roles_required(*CLINICAL_ROLES)
def new_patient() -> Response | tuple[str, int]:
    """Create a patient with demographics and diagnosis."""
    form = PatientForm()
    if form.validate_on_submit():
        try:
            patient = patient_records.create_patient(**form.demographics(), **form.diagnosis(), user=current_user)
        except ValueError as exc:
            db.session.rollback()
            form.attach_service_error(str(exc))
        else:
            db.session.commit()
            flash(f"Patient {patient.display_name} ({patient.mrn}) created. Upload a VCF to add genomic data.", "success")
            return redirect(url_for("patients.view_patient", patient_id=patient.id))
    status = 400 if form.is_submitted() else 200
    return render_template("patients/form.html", form=form, patient=None, today=_today()), status


@bp.route("/<int:patient_id>/edit", methods=["GET", "POST"])
@roles_required(*CLINICAL_ROLES)
def edit_patient(patient_id: int) -> Response | tuple[str, int]:
    """Edit demographics and diagnosis; a changed diagnosis re-evaluates decision support."""
    patient = queries.load_patient(patient_id)
    diagnosis = patient.diagnosis
    form = PatientForm(
        obj=patient,
        diabetes_type=diagnosis.diabetes_type if diagnosis else "unknown",
        age_at_diagnosis_months=diagnosis.age_at_diagnosis_months if diagnosis else None,
        diagnosed_on=diagnosis.diagnosed_on if diagnosis else None,
    )
    if form.validate_on_submit():
        try:
            outcome = _save_patient(patient, form)
        except ValueError as exc:
            db.session.rollback()
            form.attach_service_error(str(exc))
        else:
            db.session.commit()
            flash(" ".join(filter(None, [f"Patient {patient.mrn} updated.", outcome])), "success")
            return redirect(url_for("patients.view_patient", patient_id=patient.id))
    status = 400 if form.is_submitted() else 200
    return render_template("patients/form.html", form=form, patient=patient, today=_today()), status


def _save_patient(patient: Patient, form: PatientForm) -> str | None:
    """Apply the edit form; returns the re-evaluation summary when the diagnosis changed."""
    patient_records.update_patient(patient, **form.demographics(), user=current_user)
    diagnosis = patient.diagnosis
    values = form.diagnosis()
    current = {name: getattr(diagnosis, name) if diagnosis else None for name in values}
    if values == current:
        return None
    result = patient_records.update_diagnosis(
        patient, **values, notes=diagnosis.notes if diagnosis else None, user=current_user
    )
    return describe_outcome(result)


# ---------------------------------------------------------------------------
# Patient page and its inline forms
# ---------------------------------------------------------------------------


def _render_patient(
    patient: Patient,
    *,
    diagnosis_form: DiagnosisForm | None = None,
    medication_form: MedicationForm | None = None,
    status: int = 200,
) -> tuple[str, int]:
    """The patient page; a rejected inline form is passed in to show its messages (expanded)."""
    show_alerts = _can_see_alerts()
    current_medications, stopped_medications = queries.medications_for_display(patient)
    context: dict[str, Any] = {
        "patient": patient,
        "age": queries.age_text(patient.birth_date),
        "relatives": queries.relatives(patient),
        "profile": queries.genomic_profile(patient),
        "uploads": sorted(patient.uploads, key=lambda upload: upload.id, reverse=True),
        "current_medications": current_medications,
        "stopped_medications": stopped_medications,
        "summary": queries.alert_summary(patient) if show_alerts else None,
        "show_alerts": show_alerts,
        "diagnosis_form": diagnosis_form or DiagnosisForm.for_diagnosis(patient.diagnosis, formdata=None),
        "medication_form": medication_form or MedicationForm(prefix="med", formdata=None),
        "diagnosis_form_open": diagnosis_form is not None,
        "medication_form_open": medication_form is not None,
        "drug_suggestions": drug_suggestions(),
        "today": _today(),
        "return_url": url_for("patients.view_patient", patient_id=patient.id),
    }
    return render_template("patients/view.html", **context), status


@bp.route("/<int:patient_id>")
@roles_required()
def view_patient(patient_id: int) -> tuple[str, int]:
    """Overview, diagnosis, medications, genomic profile and uploads (alert summary for clinical roles)."""
    return _render_patient(queries.load_patient(patient_id))


@bp.route("/<int:patient_id>/diagnosis", methods=["POST"])
@roles_required(*CLINICAL_ROLES)
def update_diagnosis(patient_id: int) -> Response | tuple[str, int]:
    """Replace the diagnosis and re-evaluate the patient (trigger ``diagnosis_change``)."""
    patient = queries.load_patient(patient_id)
    form = DiagnosisForm(prefix="dx")
    if form.validate_on_submit():
        try:
            result = patient_records.update_diagnosis(patient, **form.values(), user=current_user)
        except ValueError as exc:
            db.session.rollback()
            form.attach_service_error(str(exc))
        else:
            db.session.commit()
            flash(f"Diagnosis updated. {describe_outcome(result)}", "success")
            return redirect(url_for("patients.view_patient", patient_id=patient.id, _anchor="diagnosis"))
    return _render_patient(patient, diagnosis_form=form, status=400)


@bp.route("/<int:patient_id>/medications", methods=["POST"])
@roles_required(*CLINICAL_ROLES)
def add_medication(patient_id: int) -> Response | tuple[str, int]:
    """Record a current diabetes medication and re-evaluate (trigger ``medication_change``)."""
    patient = queries.load_patient(patient_id)
    form = MedicationForm(prefix="med")
    if form.validate_on_submit():
        drug_class, suggested = form.resolved_drug_class()
        try:
            medication, result = patient_records.add_medication(
                patient,
                drug_name=form.drug_name.data,
                drug_class=drug_class,
                dose=form.dose.data,
                start_date=form.start_date.data,
                user=current_user,
            )
        except ValueError as exc:
            db.session.rollback()
            form.attach_service_error(str(exc))
        else:
            db.session.commit()
            how = " (suggested from the drug name)" if suggested else ""
            flash(
                f"{medication.drug_name} added as {C.DRUG_CLASS_LABELS[medication.drug_class]}{how}. "
                f"{describe_outcome(result)}",
                "success",
            )
            return redirect(url_for("patients.view_patient", patient_id=patient.id, _anchor="medications"))
    return _render_patient(patient, medication_form=form, status=400)


@bp.route("/<int:patient_id>/medications/<int:medication_id>/stop", methods=["POST"])
@roles_required(*CLINICAL_ROLES)
def stop_medication(patient_id: int, medication_id: int) -> Response:
    """Stop a medication today and re-evaluate (trigger ``medication_change``)."""
    medication = db.get_or_404(Medication, medication_id, description=f"Medication {medication_id} does not exist.")
    if medication.patient_id != patient_id:
        abort(404, description=f"Medication {medication_id} does not belong to patient {patient_id}.")
    try:
        result = patient_records.stop_medication(medication, user=current_user)
    except ValueError as exc:
        db.session.rollback()
        flash(str(exc), "warning")
    else:
        db.session.commit()
        flash(f"{medication.drug_name} stopped. {describe_outcome(result)}", "success")
    return redirect(url_for("patients.view_patient", patient_id=patient_id, _anchor="medications"))


# ---------------------------------------------------------------------------
# VCF upload
# ---------------------------------------------------------------------------


def _upload_summary(upload: GenomicUpload, result: ReconcileResult | None) -> str:
    """Flash message of an accepted upload: record counts and, for clinical roles, the findings raised."""
    parts = [
        f"VCF {upload.filename} accepted: {_plural(upload.panel_variants, 'panel variant')}",
        f"{_plural(upload.off_panel_skipped, 'off-panel record')} skipped",
    ]
    if upload.non_pass_skipped:
        parts.append(f"{_plural(upload.non_pass_skipped, 'non-PASS record')} skipped")
    if upload.ref_calls_skipped:
        parts.append(f"{_plural(upload.ref_calls_skipped, 'reference call')} skipped")
    message = ", ".join(parts) + "."
    if result is None:
        return message + " No panel variant was found, so there was nothing to evaluate."
    if not _can_see_alerts():
        # Lab technicians may not see recommendations, so not even how many were raised.
        return message + " Decision support has been updated for the care team."
    created_alerts = sum(alert.kind == "alert" for alert in result.created)
    created_notes = len(result.created) - created_alerts
    return (
        f"{message} {_plural(created_alerts, 'alert')} and {_plural(created_notes, 'note')} created"
        + (f", {len(result.superseded)} superseded." if result.superseded else ".")
    )


def _render_upload(
    patient: Patient, form: VcfUploadForm, *, error: VcfError | None = None, status: int = 200
) -> tuple[str, int]:
    uploads = sorted(patient.uploads, key=lambda upload: upload.id, reverse=True)[:RECENT_UPLOADS]
    return (
        render_template(
            "patients/upload_vcf.html",
            patient=patient,
            form=form,
            error=error,
            uploads=uploads,
            max_bytes=current_app.config["PATIENT_VCF_MAX_BYTES"],
            genes=list(C.GENE_INFO.values()),
        ),
        status,
    )


@bp.route("/<int:patient_id>/vcf", methods=["GET", "POST"])
@roles_required()
def upload_vcf(patient_id: int) -> Response | tuple[str, int]:
    """Upload a patient VCF; a rejected file is re-rendered with its error and HTTP 400 (S10)."""
    patient = queries.load_patient(patient_id)
    form = VcfUploadForm()
    if not form.validate_on_submit():
        return _render_upload(patient, form, status=400 if form.is_submitted() else 200)
    storage = form.vcf_file.data
    try:
        upload, result = patient_records.ingest_vcf(patient, storage.read(), storage.filename or "", user=current_user)
    except VcfError as exc:
        db.session.commit()  # keep the rejected-upload record and its audit row
        return _render_upload(patient, form, error=exc, status=400)
    db.session.commit()
    flash(_upload_summary(upload, result), "success")
    return redirect(url_for("patients.view_patient", patient_id=patient.id, _anchor="genomic-profile"))


# ---------------------------------------------------------------------------
# Recommendations
# ---------------------------------------------------------------------------


@bp.route("/<int:patient_id>/recommendations")
@roles_required(*CLINICAL_ROLES)
def recommendations(patient_id: int) -> tuple[str, int]:
    """Current alerts and notes, review notices and history; ``?at=`` shows a past state (§7.5)."""
    patient = db.get_or_404(Patient, patient_id, description=f"Patient {patient_id} does not exist.")
    at_value = request.args.get("at", "").strip()
    at: datetime | None = None
    at_error: str | None = None
    if at_value:
        try:
            at = queries.parse_point_in_time(at_value)
        except ValueError as exc:
            at_error = str(exc)
    view = queries.recommendations_as_of(patient, at) if at is not None else queries.recommendations_view(patient)
    return_url = _current_path()
    return (
        render_template(
            "patients/recommendations.html",
            patient=patient,
            view=view,
            at_error=at_error,
            return_url=return_url,
            notice_forms={
                notice.id: acknowledge_form(notice.id, formdata=None, next=return_url) for notice in view.open_notices
            },
            notice_views={
                item.notice.id: item
                for item in alert_queries.notice_views(view.open_notices + view.acknowledged_notices)
            },
        ),
        400 if at_error else 200,
    )
