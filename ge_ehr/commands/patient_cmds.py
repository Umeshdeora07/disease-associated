"""``flask patients …`` — the patient workflow in the terminal (F1, with F3/F4/F5 views).

Commands (MRN is the patient identifier; ``--as USERNAME`` names the account an action is
recorded under in the audit log):

* ``list`` — search and filter patients; ``show MRN`` — demographics, diagnosis, medications,
  genomic profile, uploads and (not for lab technicians) current alerts, notes and open review
  notices; ``history MRN [--at WHEN]`` — the recommendation timeline or the point-in-time view
  (SPEC §7.5); ``fhir MRN [--output FILE]`` — the validated FHIR R4 Bundle (F4).
* ``create``, ``set-diagnosis``, ``add-medication``, ``stop-medication`` (clinicians and
  administrators) and ``upload-vcf`` (every role) change the record through
  :mod:`ge_ehr.services.patient_records`, which validates, audits and re-evaluates decision
  support for that patient; the command prints what changed.

Roles follow SPEC §8.1: ``--as`` is required for changes and must name an active account with
the right role; read commands accept it to view as that account (a lab technician sees no
recommendations). A refused attempt is audited as ``access_denied``. Every change is committed
on success; on error it is rolled back and the command exits with status 1 and a clear message
(a rejected VCF keeps its rejected-upload record, as in the web app — S10).

Owned by the CLI work package. Read-side view models come from :mod:`ge_ehr.patients.queries`
(shared with the web pages), so the terminal and the browser show the same data.
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Callable, Mapping, Sequence
from datetime import date, datetime
from pathlib import Path
from typing import Any

import click
from flask import Flask, current_app
from sqlalchemy import select

from .. import constants as C
from ..extensions import db
from ..models import Alert, AlertEvent, AuditLog, GenomicUpload, Medication, Patient, User
from ..patients import queries
from ..patients.routes import describe_outcome
from ..security import audit
from ..services import patient_records, reevaluation
from ..services.alerting import ReconcileResult
from ..services.fhir_export import build_patient_bundle, validate_bundle
from ..services.rules import rule_code_sort_key
from ..services.vcf_parser import VcfError
from ..templating import months_to_age
from . import _format as F

logger = logging.getLogger(__name__)

DEFAULT_LIST_LIMIT = 50


class IsoDate(click.ParamType):
    """A calendar date written ``YYYY-MM-DD`` (converted to :class:`datetime.date`)."""

    name = "YYYY-MM-DD"

    def convert(self, value: Any, param: click.Parameter | None, ctx: click.Context | None) -> date:
        if isinstance(value, date):
            return value
        try:
            return date.fromisoformat(str(value).strip())
        except ValueError:
            self.fail(f"{value!r} is not a date; write it as YYYY-MM-DD, e.g. 2026-01-15.", param, ctx)


DATE = IsoDate()

patients_cli = F.command_group(
    "patients",
    """Find, view and update patient records (synthetic data only).

    \b
    Examples:
      flask patients list --search okonkwo
      flask patients show DEMO-0001
      flask patients add-medication DEMO-0001 --drug "Gliclazide 80 mg" --as dr.rao
      flask patients upload-vcf DEMO-0001 data/demo/vcfs/DEMO-0001.vcf --as lab.kim
      flask patients history DEMO-0010 --at "2026-01-15 09:30"
      flask patients fhir DEMO-0001 --output DEMO-0001.json

    Changes need --as USERNAME (the account recorded in the audit log). Recommendations are
    advisory, for specialist review.
    """,
)


def as_option(*, required: bool, help_text: str | None = None) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """The ``--as USERNAME`` option (stored as ``as_user``)."""
    default_help = F.AS_HELP if required else "View as this account (a lab technician sees no recommendations)."
    return click.option("--as", "as_user", metavar="USERNAME", required=required, help=help_text or default_help)


# ---------------------------------------------------------------------------
# Re-evaluation outcome and findings
# ---------------------------------------------------------------------------


def _finding_order(alert: Alert) -> tuple[Any, ...]:
    return (alert.kind != "alert", -C.SEVERITY_ORDER.get(alert.severity or "", -1), rule_code_sort_key(alert.rule_code), alert.id)


def _finding_row(alert: Alert, action: str) -> list[str]:
    """``+ new alert | #21 | D2 v1 | HIGH | HNF1A 12-… (P) | HNF1A diabetes on insulin or metformin``."""
    rule = alert.confirmed_rule_version.label if alert.confirmed_rule_version is not None else alert.rule_code
    return [
        f"{action} {alert.kind}",
        f"#{alert.id}",
        rule,
        F.severity_label(alert.severity),
        f"{F.variant_text(alert.variant_key)} ({F.class_label(alert.classification)})",
        alert.title,
    ]


def echo_outcome(result: ReconcileResult | None, user: User | None) -> None:
    """How a change affected decision support: a summary line plus created/superseded findings.

    Lab technicians get no detail (they may not see recommendations, SPEC §8.1).
    """
    if result is not None and not F.may_see_recommendations(user):
        F.echo("Decision support has been updated for the care team.")
        return
    F.echo(describe_outcome(result))
    if result is None:
        return
    rows = [_finding_row(alert, "+ new") for alert in sorted(result.created, key=_finding_order)]
    rows += [_finding_row(alert, "- superseded") for alert in sorted(result.superseded, key=_finding_order)]
    for line in F.table_lines(None, rows, flex=5):
        F.echo(line)


def _evidence_text(finding: queries.FindingView) -> str:
    alert = finding.alert
    release = finding.release.label if finding.release is not None else "no knowledge-base release"
    created = f"created {F.when(alert.created_at)} ({C.TRIGGER_LABELS.get(alert.trigger, alert.trigger)}"
    created += f", run #{alert.created_run_id})" if alert.created_run_id is not None else ")"
    return f"{release}; rule {finding.rule_label}; {created}"


def _now_text(alert: Alert) -> str:
    """The finding's status today, for the point-in-time view."""
    if alert.status == "superseded":
        reason = C.SUPERSEDED_REASON_LABELS.get(alert.superseded_reason or "", alert.superseded_reason)
        return f"superseded {F.when(alert.superseded_at)} ({reason})"
    return f"still current ({alert.status})"


def _echo_finding(finding: queries.FindingView, *, indent: int = 4, at: datetime | None = None) -> None:
    """One alert or note: header, variant, recommendation, source and evidence.

    ``at``: the finding as it stood at that past point in time — the title, text and source shown
    then (not today's), its status today added, and no review hint offered.
    """
    alert = finding.alert
    as_of = at is not None
    snapshot = reevaluation.alert_snapshot_as_of(alert, at) if at is not None else None
    shown = snapshot if snapshot is not None else alert
    title, message, source = shown.title, shown.message, shown.source
    margin = " " * indent
    names = []
    if alert.is_reclassified:
        names.append("Reclassified")
    if alert.priority == "high":
        names.append("High priority")
    markers = F.flags(names)
    header = margin + "  ".join([f"#{alert.id}", finding.rule_label, F.status_label(finding.status), title])
    budget = F.WIDTH - (F.visible_length(markers) + 2 if markers else 0)
    F.echo(F.shorten(header, budget) + (f"  {markers}" if markers else ""))
    options = {"label_width": 14, "indent": indent + 4}
    F.field("Variant", f"{F.variant_text(alert.variant_key)} - {F.class_label(finding.classification, long=True)}", **options)
    F.field("Recommendation" if alert.kind == "alert" else "Note", message, **options)
    F.field("Source", source, **options)
    F.field("Evidence", _evidence_text(finding), **options)
    if finding.status == "reviewed" and alert.reviewed_at is not None:
        who = alert.reviewed_by.username if alert.reviewed_by is not None else "unknown user"
        review = f"{F.when(alert.reviewed_at)} by {who}" + (f": {alert.review_comment}" if alert.review_comment else "")
        F.field("Reviewed", review, **options)
    elif finding.status == "active" and not as_of:
        F.field("Review", f"flask alerts review {alert.id} --as USERNAME [--comment TEXT]", **options)
    if as_of:
        F.field("Now", _now_text(alert), **options)


def _echo_findings(
    groups: Sequence[tuple[str, Sequence[queries.FindingView]]],
    notes: Sequence[queries.FindingView],
    *,
    at: datetime | None = None,
) -> None:
    if not groups and not notes:
        F.echo("  No alerts or notes.")
        return
    for severity, findings in groups:
        F.echo(f"  {F.severity_label(severity)} ({len(findings)})")
        for finding in findings:
            _echo_finding(finding, at=at)
    if notes:
        F.echo(f"  NOTES ({len(notes)}) - genetic criterion met, clinical condition not met: no alert raised")
        for finding in notes:
            _echo_finding(finding, at=at)


# ---------------------------------------------------------------------------
# list
# ---------------------------------------------------------------------------


def _alert_counts_text(counts: Mapping[str, int]) -> str:
    """``1 high, 2 low`` (severities coloured)."""
    return ", ".join(
        f"{count} {F.style(severity, **F.SEVERITY_STYLES.get(severity, {}))}" for severity, count in counts.items()
    )


_AGE_UNITS = {"years": "y", "year": "y", "months": "mo", "month": "mo"}


def _age_sex(patient: Patient, age: str) -> str:
    """``32 y, F`` — compact age and sex for the patient list (``8 mo, M`` below two years)."""
    number, _space, unit = age.partition(" ")
    sex = C.SEX_LABELS.get(patient.sex, patient.sex or "?")[:1]
    return f"{number} {_AGE_UNITS.get(unit, unit)}, {sex}"


@patients_cli.command("list")
@click.option("--search", "-s", default="", metavar="TEXT", help="Part of the MRN, given name or family name.")
@click.option("--with-alerts", is_flag=True, help="Only patients with current alerts (clinicians and administrators).")
@click.option("--family", "family_id", metavar="ID", default="", help="Only the members of this family (family ID).")
@click.option(
    "--diabetes-type", type=click.Choice(C.DIABETES_TYPES, case_sensitive=False), help="Only this diagnosis type."
)
@click.option(
    "--limit", type=click.IntRange(min=1), default=DEFAULT_LIST_LIMIT, show_default=True, help="Rows to show."
)
@as_option(required=False)
def list_command(
    search: str, with_alerts: bool, family_id: str, diabetes_type: str | None, limit: int, as_user: str | None
) -> None:
    """List patients by MRN with diagnosis, actionable variants, last VCF and current alerts.

    \b
    Examples:
      flask patients list
      flask patients list --search okonkwo
      flask patients list --with-alerts --limit 10
      flask patients list --family FAM-RS6
      flask patients list --as lab.kim          (lab view: no alert counts)
    """
    user = F.acting_user(as_user, action="list patients")
    show_alerts = F.may_see_recommendations(user)
    if with_alerts and not show_alerts:
        F.deny(user, roles=C.CLINICAL_ROLES, action="filter patients by alerts")
    filters = queries.PatientListFilters(
        q=search.strip(), family=family_id.strip(), diabetes_type=(diabetes_type or "").lower(), with_alerts=with_alerts
    )
    page = db.paginate(queries.patient_list_statement(filters), page=1, per_page=limit, max_per_page=None, error_out=False)
    if not page.items:
        if filters.active:
            F.echo("No patients match. Try a shorter --search text or drop a filter.")
        else:
            F.echo("No patients yet. Create one:    flask patients create --help")
            F.echo("or load the demo dataset:       flask seed-demo")
        return
    rows = queries.patient_list_rows(page.items, include_alerts=show_alerts)
    headers = ["MRN", "NAME", "AGE/SEX", "DIABETES", "FAMILY", "P/LP", "LAST VCF"]
    if show_alerts:
        headers.append("CURRENT ALERTS")
    body = []
    for row in rows:
        patient = row.patient
        diagnosis = patient.diagnosis
        values: list[Any] = [
            patient.mrn,
            patient.display_name,
            _age_sex(patient, row.age),
            C.DIABETES_TYPE_LABELS.get(diagnosis.diabetes_type, diagnosis.diabetes_type) if diagnosis else None,
            patient.family_id,
            row.actionable_variants,
            F.status_label(row.last_upload.status) if row.last_upload is not None else None,
        ]
        if show_alerts:
            values.append(_alert_counts_text(row.alert_counts) or "none")
        body.append(values)
    F.table(headers, body, flex=1, indent=0)
    shown = len(page.items)
    if page.total and page.total > shown:
        F.echo(f"\nShowing {shown} of {page.total} patients; narrow with --search or --family, or raise --limit.")
    else:
        F.echo(f"\n{F.plural(shown, 'patient')}. Details: flask patients show MRN")
    F.echo("P/LP = actionable (pathogenic or likely pathogenic) variants." + ("  Alerts: flask alerts list" if show_alerts else ""))


# ---------------------------------------------------------------------------
# show
# ---------------------------------------------------------------------------


def _diagnosis_text(patient: Patient) -> str:
    diagnosis = patient.diagnosis
    if diagnosis is None:
        return "not recorded"
    parts = [C.DIABETES_TYPE_LABELS.get(diagnosis.diabetes_type, diagnosis.diabetes_type)]
    if diagnosis.age_at_diagnosis_months is not None:
        months = diagnosis.age_at_diagnosis_months
        parts.append(f"diagnosed at {F.plural(months, 'month')} ({months_to_age(months)})")
    if diagnosis.diagnosed_on is not None:
        parts.append(f"on {F.day(diagnosis.diagnosed_on)}")
    return ", ".join(parts)


def _echo_demographics(patient: Patient) -> None:
    F.echo(F.style(f"{patient.mrn}  {patient.display_name}", bold=True) + ("  (synthetic record)" if patient.is_synthetic else ""))
    F.field("Sex", C.SEX_LABELS.get(patient.sex, patient.sex))
    F.field("Born", f"{F.day(patient.birth_date)} ({queries.age_text(patient.birth_date)})")
    if patient.family_id:
        relatives = queries.relatives(patient)
        names = ", ".join(relative.mrn for relative in relatives) or "no other recorded members"
        F.field("Family", f"{patient.family_id} (relatives: {names})")
    if patient.external_id:
        F.field("External ID", patient.external_id)
    F.field("Diagnosis", _diagnosis_text(patient))
    if patient.diagnosis is not None and patient.diagnosis.notes:
        F.field("Diagnosis notes", patient.diagnosis.notes)
    if patient.notes:
        F.field("Notes", patient.notes)


def _echo_medications(patient: Patient) -> None:
    current, stopped = queries.medications_for_display(patient)
    F.heading(f"Current diabetes medications ({len(current)})")
    if current:
        F.table(
            ["DRUG", "CLASS", "DOSE", "SINCE"],
            ((m.drug_name, C.DRUG_CLASS_LABELS.get(m.drug_class, m.drug_class), m.dose, m.start_date) for m in current),
            flex=2,
        )
    else:
        F.echo("  None recorded.")
    if stopped:
        F.paragraph("Stopped: " + "; ".join(f"{m.drug_name} (until {F.day(m.end_date)})" for m in stopped))


def _review_text(row: queries.ProfileVariant) -> str:
    if row.known.known_variant_id is None:
        return "not in release"
    return f"{row.known.review_stars}/4 stars"


def _variant_flags(row: queries.ProfileVariant) -> list[str]:
    variant = row.variant
    names = []
    if row.known.is_expert_panel:
        names.append("Expert panel")
    if variant.is_amended:
        names.append("Amended")
    if row.changed_across_releases:
        names.append("Reclassified")
    return names


def _echo_profile(patient: Patient) -> None:
    profile = queries.genomic_profile(patient)
    release = profile.release.label if profile.release is not None else None
    F.heading(f"Genomic profile ({profile.actionable_count} actionable of {F.plural(len(profile.variants), 'panel variant')})")
    if release:
        F.echo(f"  Classified against {release}" + (f"; history across {len(profile.releases)} activated releases." if len(profile.releases) > 1 else "."))
    else:
        F.echo("  No knowledge-base release is active, so the variants are not classified yet.")
    if not profile.variants:
        F.echo("  No genomic data yet. Upload a GRCh38 VCF with:")
        F.echo(f"    flask patients upload-vcf {patient.mrn} PATH --as USERNAME")
        return
    F.table(
        ["GENE", "VARIANT", "ZYGOSITY", "CLASS", "REVIEW", "HISTORY", "FLAGS"],
        (
            (
                row.variant.gene,
                row.variant.variant_key.label,
                row.variant.zygosity,
                F.class_label(row.variant.current_classification),
                _review_text(row),
                " > ".join(F.class_label(step.classification) for step in row.history) or None,
                F.flags(_variant_flags(row)) or None,
            )
            for row in profile.variants
        ),
    )
    legends = []
    if profile.any_expert_panel:
        legends.append("Expert panel = ClinGen expert-panel classification")
    if profile.any_amended:
        legends.append("Amended = class changed since first report (FHIR 'amended')")
    if profile.any_reclassified:
        legends.append("Reclassified = class differs between activated releases")
    for row in profile.variants:
        if row.variant.reported_classification != row.variant.current_classification:
            legends.append(
                f"{row.variant.variant_key.label} was first reported as "
                f"{C.CLASS_LABELS.get(row.variant.reported_classification, row.variant.reported_classification).lower()}"
            )
    for legend in legends:
        F.echo(f"  {legend}.")


def _echo_uploads(patient: Patient) -> None:
    uploads = sorted(patient.uploads, key=lambda upload: upload.id, reverse=True)
    F.heading(f"VCF uploads ({len(uploads)})")
    if not uploads:
        F.echo("  None yet.")
        return
    F.table(
        ["ID", "UPLOADED (UTC)", "FILE", "STATUS", "PANEL", "SKIPPED (OFF-PANEL/NON-PASS/REF)", "BY"],
        (
            (
                upload.id,
                upload.uploaded_at.strftime("%Y-%m-%d %H:%M"),
                upload.filename,
                F.status_label(upload.status),
                upload.panel_variants if upload.status == "accepted" else None,
                (
                    f"{upload.off_panel_skipped}/{upload.non_pass_skipped}/{upload.ref_calls_skipped}"
                    if upload.status == "accepted"
                    else None
                ),
                upload.uploaded_by.username if upload.uploaded_by is not None else None,
            )
            for upload in uploads
        ),
        flex=2,
    )
    for upload in uploads:
        if upload.status == "rejected" and upload.error:
            F.field(f"#{upload.id} rejected", upload.error, label_width=12)


def _echo_recommendations(patient: Patient) -> None:
    view = queries.recommendations_view(patient)
    F.heading(f"Current alerts ({view.alert_count}) and notes ({len(view.notes)})")
    _echo_findings(view.groups, view.notes)
    F.heading(f"Open review notices ({len(view.open_notices)})")
    if not view.open_notices:
        F.echo("  None.")
    for notice in view.open_notices:
        F.notice_block(notice, indent=2, with_patient=False)
    if view.history:
        F.echo(f"\n  {F.plural(len(view.history), 'superseded finding')}: flask patients history {patient.mrn}")


@patients_cli.command("show")
@click.argument("mrn")
@as_option(required=False)
def show_command(mrn: str, as_user: str | None) -> None:
    """Show one patient: record, medications, genomic profile, uploads and recommendations.

    With --as naming a lab technician account, everything except alerts, notes and review
    notices is shown (lab technicians do not see recommendations).

    \b
    Examples:
      flask patients show DEMO-0001
      flask patients show demo-0008 --as dr.rao
    """
    user = F.acting_user(as_user, action="view patients")
    patient = F.find_patient(mrn)
    _echo_demographics(patient)
    _echo_medications(patient)
    _echo_profile(patient)
    _echo_uploads(patient)
    if F.may_see_recommendations(user):
        _echo_recommendations(patient)
    else:
        F.heading("Recommendations")
        F.echo("  Alerts, notes and review notices are shown to clinicians and administrators only.")
    F.advisory_footer()


# ---------------------------------------------------------------------------
# create / set-diagnosis
# ---------------------------------------------------------------------------


@patients_cli.command("create")
@as_option(required=True)
@click.option("--mrn", required=True, help="Medical record number, e.g. DEMO-0016 (letters, digits, . _ / -).")
@click.option("--given", "given_name", required=True, metavar="NAME", help="Given name.")
@click.option("--family", "family_name", required=True, metavar="NAME", help="Family name (surname).")
@click.option("--sex", type=click.Choice(C.SEXES, case_sensitive=False), required=True, help="Administrative sex.")
@click.option("--birth-date", type=DATE, required=True, help="Birth date, YYYY-MM-DD.")
@click.option(
    "--diabetes-type",
    type=click.Choice(C.DIABETES_TYPES, case_sensitive=False),
    default="unknown",
    show_default=True,
    help="Diagnosis type.",
)
@click.option("--age-at-diagnosis-months", type=click.IntRange(min=0), help="Age at diagnosis in whole months.")
@click.option("--diagnosed-on", type=DATE, help="Date of diagnosis, YYYY-MM-DD.")
@click.option("--family-id", metavar="ID", help="Family ID shared by relatives, e.g. FAM-RS6.")
@click.option("--notes", metavar="TEXT", help="Free-text notes.")
def create_command(
    as_user: str,
    mrn: str,
    given_name: str,
    family_name: str,
    sex: str,
    birth_date: date,
    diabetes_type: str,
    age_at_diagnosis_months: int | None,
    diagnosed_on: date | None,
    family_id: str | None,
    notes: str | None,
) -> None:
    """Create a (synthetic) patient with demographics and diagnosis.

    \b
    Example:
      flask patients create --as dr.rao --mrn DEMO-0016 --given Ana --family Silva \\
          --sex female --birth-date 1990-04-01 --diabetes-type type2 --age-at-diagnosis-months 300
    """
    user = F.acting_user(as_user, roles=C.CLINICAL_ROLES, action="create patients")
    with F.saving("Could not create the patient"):
        patient = patient_records.create_patient(
            mrn=mrn,
            given_name=given_name,
            family_name=family_name,
            sex=sex.lower(),
            birth_date=birth_date,
            family_id=family_id,
            notes=notes,
            diabetes_type=diabetes_type.lower(),
            age_at_diagnosis_months=age_at_diagnosis_months,
            diagnosed_on=diagnosed_on,
            user=user,
        )
    F.say(
        f"Created patient {F.patient_text(patient)} ({C.SEX_LABELS[patient.sex]}, born {F.day(patient.birth_date)}; "
        f"diagnosis: {_diagnosis_text(patient)})."
    )
    F.echo("Next steps:")
    F.echo(f"  flask patients upload-vcf {patient.mrn} PATH --as {user.username}")
    F.echo(f'  flask patients add-medication {patient.mrn} --drug "NAME" --as {user.username}')


def _diagnosis_values(patient: Patient) -> dict[str, Any]:
    diagnosis = patient.diagnosis
    return {
        "diabetes_type": diagnosis.diabetes_type if diagnosis else None,
        "age_at_diagnosis_months": diagnosis.age_at_diagnosis_months if diagnosis else None,
        "diagnosed_on": diagnosis.diagnosed_on if diagnosis else None,
    }


def _diagnosis_value_text(name: str, value: Any) -> str:
    if name == "diabetes_type" and value:
        return C.DIABETES_TYPE_LABELS.get(value, value)
    return F.text(value)


_DIAGNOSIS_FIELD_LABELS = {
    "diabetes_type": "type",
    "age_at_diagnosis_months": "age at diagnosis (months)",
    "diagnosed_on": "diagnosis date",
}


@patients_cli.command("set-diagnosis")
@click.argument("mrn")
@as_option(required=True)
@click.option(
    "--type", "diabetes_type", type=click.Choice(C.DIABETES_TYPES, case_sensitive=False), required=True,
    help="Diagnosis type.",
)
@click.option(
    "--age-at-diagnosis-months", type=click.IntRange(min=0),
    help="Age at diagnosis in whole months (omit to keep the recorded value).",
)
@click.option("--unknown-age", is_flag=True, help="Clear the recorded age at diagnosis.")
@click.option("--diagnosed-on", type=DATE, help="Date of diagnosis, YYYY-MM-DD (omit to keep the recorded date).")
def set_diagnosis_command(
    mrn: str,
    as_user: str,
    diabetes_type: str,
    age_at_diagnosis_months: int | None,
    unknown_age: bool,
    diagnosed_on: date | None,
) -> None:
    """Set the diabetes diagnosis and re-evaluate the patient's decision support.

    Values not given keep their recorded value (type "none" clears the age and date).

    \b
    Examples:
      flask patients set-diagnosis DEMO-0006 --type neonatal --age-at-diagnosis-months 4 --as dr.rao
      flask patients set-diagnosis DEMO-0001 --type monogenic --as dr.rao
    """
    if unknown_age and age_at_diagnosis_months is not None:
        raise click.UsageError("Give either --age-at-diagnosis-months or --unknown-age, not both.")
    user = F.acting_user(as_user, roles=C.CLINICAL_ROLES, action="change diagnoses")
    patient = F.find_patient(mrn)
    before = _diagnosis_values(patient)
    kind = diabetes_type.lower()
    no_diabetes = kind == "none"
    age = age_at_diagnosis_months
    if age is None and not unknown_age and not no_diabetes:
        age = before["age_at_diagnosis_months"]
    on = diagnosed_on
    if on is None and not no_diabetes:
        on = before["diagnosed_on"]
    notes = patient.diagnosis.notes if patient.diagnosis is not None else None
    with F.saving("Could not update the diagnosis"):
        result = patient_records.update_diagnosis(
            patient, diabetes_type=kind, age_at_diagnosis_months=age, diagnosed_on=on, notes=notes, user=user
        )
    after = _diagnosis_values(patient)
    changes = [
        f"{_DIAGNOSIS_FIELD_LABELS[name]} {_diagnosis_value_text(name, before[name])} -> "
        f"{_diagnosis_value_text(name, after[name])}"
        for name in after
        if before[name] != after[name]
    ]
    F.say(f"Diagnosis of {F.patient_text(patient)}: {_diagnosis_text(patient)}.")
    F.say(("Changed: " + "; ".join(changes) + ".") if changes else "No recorded value changed.")
    echo_outcome(result, user)


# ---------------------------------------------------------------------------
# Medications
# ---------------------------------------------------------------------------


@patients_cli.command("add-medication")
@click.argument("mrn")
@as_option(required=True)
@click.option("--drug", "drug_name", required=True, metavar="NAME", help='Drug name, e.g. "Gliclazide 80 mg".')
@click.option(
    "--class",
    "drug_class",
    type=click.Choice(("suggested", *C.DRUG_CLASSES), case_sensitive=False),
    default="suggested",
    show_default=True,
    help="Drug class; 'suggested' derives it from the drug name.",
)
@click.option("--dose", metavar="TEXT", help='Dose, e.g. "80 mg twice daily".')
@click.option("--start", "start_date", type=DATE, help="Start date, YYYY-MM-DD.")
def add_medication_command(
    mrn: str, as_user: str, drug_name: str, drug_class: str, dose: str | None, start_date: date | None
) -> None:
    """Record a current diabetes medication and re-evaluate the patient's decision support.

    \b
    Examples:
      flask patients add-medication DEMO-0001 --drug "Gliclazide 80 mg" --as dr.rao
      flask patients add-medication DEMO-0003 --drug "Sitagliptin" --class other --dose "100 mg daily" \\
          --start 2026-01-10 --as dr.rao
    """
    user = F.acting_user(as_user, roles=C.CLINICAL_ROLES, action="change medications")
    patient = F.find_patient(mrn)
    suggested = drug_class.lower() == "suggested"
    therapy = C.suggest_drug_class(drug_name) if suggested else drug_class.lower()
    with F.saving("Could not add the medication"):
        medication, result = patient_records.add_medication(
            patient, drug_name=drug_name, drug_class=therapy, dose=dose, start_date=start_date, user=user
        )
    how = " (suggested from the drug name; override with --class)" if suggested else ""
    F.say(
        f"Added {medication.drug_name} to {F.patient_text(patient)} as "
        f"{C.DRUG_CLASS_LABELS[medication.drug_class]}{how}."
    )
    echo_outcome(result, user)


def _normalised(name: str) -> str:
    return re.sub(r"\s+", " ", name).strip().casefold()


def _matching_medications(patient: Patient, drug_name: str) -> list[Medication]:
    """Current medications named ``drug_name`` (case-insensitive), else the one whose name contains it."""
    wanted = _normalised(drug_name)
    current = patient.active_medications
    exact = [medication for medication in current if _normalised(medication.drug_name) == wanted]
    if exact:
        return exact
    partial = [medication for medication in current if wanted and wanted in _normalised(medication.drug_name)]
    if len(partial) > 1:
        names = "; ".join(medication.drug_name for medication in partial)
        raise click.ClickException(f"{drug_name!r} matches several current medications ({names}); give the full name.")
    if not partial:
        names = "; ".join(medication.drug_name for medication in current) or "none"
        raise click.ClickException(
            f"{patient.mrn} has no current medication named {drug_name!r}. Current medications: {names}."
        )
    return partial


@patients_cli.command("stop-medication")
@click.argument("mrn")
@as_option(required=True)
@click.option(
    "--drug", "drug_name", required=True, metavar="NAME",
    help="Name of the current medication (case-insensitive; a unique part of the name is enough).",
)
@click.option("--end", "end_date", type=DATE, help="Last day of the medication, YYYY-MM-DD (default: today, UTC).")
def stop_medication_command(mrn: str, as_user: str, drug_name: str, end_date: date | None) -> None:
    """Stop a current medication and re-evaluate the patient's decision support.

    \b
    Examples:
      flask patients stop-medication DEMO-0001 --drug "Insulin glargine 100 units/mL (Lantus)" --as dr.rao
      flask patients stop-medication DEMO-0001 --drug lantus --end 2026-10-01 --as dr.rao
    """
    user = F.acting_user(as_user, roles=C.CLINICAL_ROLES, action="change medications")
    patient = F.find_patient(mrn)
    medications = _matching_medications(patient, drug_name)
    results: list[ReconcileResult] = []
    with F.saving("Could not stop the medication"):
        for medication in medications:
            results.append(patient_records.stop_medication(medication, end_date=end_date, user=user))
    for medication in medications:
        F.say(f"Stopped {medication.drug_name} for {F.patient_text(patient)} (last day {F.day(medication.end_date)}).")
    echo_outcome(_merged(results), user)


def _merged(results: Sequence[ReconcileResult]) -> ReconcileResult:
    """One result for several consecutive re-evaluations (a finding created then superseded drops out)."""
    if len(results) == 1:
        return results[0]
    created: dict[int, Alert] = {}
    superseded: list[Alert] = []
    confirmed: list[Alert] = []
    for result in results:
        for alert in result.superseded:
            if alert.id in created:
                del created[alert.id]
            else:
                superseded.append(alert)
        created.update({alert.id: alert for alert in result.created})
        confirmed.extend(result.confirmed)
    return ReconcileResult(
        created=list(created.values()),
        superseded=superseded,
        confirmed=confirmed,
        unchanged=list(results[-1].unchanged),
        records_examined=sum(result.records_examined for result in results),
    )


# ---------------------------------------------------------------------------
# VCF upload
# ---------------------------------------------------------------------------


def _accepted_detail(upload: GenomicUpload) -> dict[str, Any]:
    """The audit detail recorded for an accepted upload (new/updated variants, parser warnings)."""
    entry = db.session.scalar(
        select(AuditLog)
        .where(AuditLog.action == "vcf_upload_accepted", AuditLog.entity_id == upload.id)
        .order_by(AuditLog.id.desc())
    )
    return dict(entry.detail or {}) if entry is not None else {}


def _echo_accepted(upload: GenomicUpload, patient: Patient) -> None:
    detail = _accepted_detail(upload)
    sample = f", sample {upload.sample_id}" if upload.sample_id else ""
    F.say(f"Accepted {upload.filename} for {F.patient_text(patient)} (upload #{upload.id}{sample}).")
    new, updated = detail.get("new_variants"), detail.get("updated_variants")
    variants = F.plural(upload.panel_variants, "panel variant")
    if new is not None and updated is not None:
        variants += f" ({new} new, {updated} already recorded)"
    F.field("Records", f"{F.plural(upload.total_records, 'record')} in the file; {variants}", label_width=8)
    F.field(
        "Skipped",
        f"{upload.off_panel_skipped} off-panel, {upload.non_pass_skipped} non-PASS, "
        f"{F.plural(upload.ref_calls_skipped, 'reference call')}",
        label_width=8,
    )
    for warning in detail.get("warnings") or []:
        F.field("Warning", warning, label_width=8)


@patients_cli.command("upload-vcf")
@click.argument("mrn")
@click.argument("path", type=click.Path(exists=True, dir_okay=False, readable=True))
@as_option(required=True)
def upload_vcf_command(mrn: str, path: str, as_user: str) -> None:
    """Upload a patient VCF (GRCh38, plain or gzipped) and re-evaluate the patient.

    A rejected file prints the line-numbered reason and exits with status 1; the rejected
    upload is kept for the audit trail and nothing else is stored.

    \b
    Examples:
      flask patients upload-vcf DEMO-0001 data/demo/vcfs/DEMO-0001.vcf --as lab.kim
      flask patients upload-vcf DEMO-0016 C:\\Users\\me\\sample.vcf.gz --as dr.rao
    """
    user = F.acting_user(as_user, action="upload VCFs")
    patient = F.find_patient(mrn)
    file_path = Path(path)
    limit = current_app.config.get("MAX_CONTENT_LENGTH")
    if limit and file_path.stat().st_size > limit:
        # Like the web upload (HTTP 413): far too large to read; nothing is stored.
        raise click.ClickException(
            f"{file_path.name} is {file_path.stat().st_size / (1024 * 1024):,.0f} MB; patient VCFs may be at most "
            f"{current_app.config['PATIENT_VCF_MAX_BYTES'] / (1024 * 1024):.0f} MB. "
            "Restrict the VCF to the gene panel (or compress it with bgzip) first."
        )
    try:
        data = file_path.read_bytes()
    except OSError as exc:
        raise click.ClickException(f"Could not read {path}: {exc.strerror or exc}.") from None
    rejected: VcfError | None = None
    with F.saving("Could not import the VCF"):
        try:
            upload, result = patient_records.ingest_vcf(patient, data, file_path.name, user=user)
        except VcfError as exc:
            rejected = exc  # the rejected-upload record and its audit row are committed (S10)
    if rejected is not None:
        record = db.session.scalar(
            select(GenomicUpload)
            .where(GenomicUpload.patient_id == patient.id, GenomicUpload.status == "rejected")
            .order_by(GenomicUpload.id.desc())
        )
        kept = f" (rejected upload #{record.id})" if record is not None else ""
        raise click.ClickException(
            f"Rejected {file_path.name} for {patient.mrn}: {rejected}\n"
            f"Nothing was imported; the rejection is recorded in the audit trail{kept}."
        )
    _echo_accepted(upload, patient)
    echo_outcome(result, user)
    F.echo(f"Genomic profile: flask patients show {patient.mrn}")


# ---------------------------------------------------------------------------
# history
# ---------------------------------------------------------------------------


_EVENT_ORDER = {"created": 0, "confirmed": 1, "reviewed": 2, "superseded": 3}
_EVENT_VERBS = {"created": "+ new", "confirmed": "= confirmed", "reviewed": "* reviewed", "superseded": "- superseded"}


def _moment_value(moment: datetime) -> str:
    """The ``--at`` value selecting the state right after ``moment`` (rounded up to the millisecond)."""
    return queries.ChangePoint(at=moment, label="", run_id=None, created=0, superseded=0).value.replace("T", " ")


def _moment_label(events: Sequence[AlertEvent]) -> str:
    """What happened at one moment and the evidence then in use.

    The run's trigger (``ClinVar release R2 … (run #3)``), else the trigger of a created finding
    or the reason of a supersession, followed by the release the findings rested on.
    """
    label = ""
    for item in events:
        run = item.run
        if run is not None:
            label = C.TRIGGER_LABELS.get(run.trigger, run.trigger)
            if run.trigger == "clinvar_release" and run.new_release is not None:
                return f"{label} {run.new_release.label} (run #{run.id})"
            label += f" (run #{run.id})"
            break
    if not label:
        created = [item for item in events if item.event == "created"]
        superseded = [item for item in events if item.event == "superseded"]
        if created:
            label = C.TRIGGER_LABELS.get(created[0].alert.trigger, created[0].alert.trigger)
        elif superseded:
            reason = superseded[0].alert.superseded_reason or ""
            label = C.SUPERSEDED_REASON_LABELS.get(reason, reason) or "Re-evaluation"
        else:
            return "Clinician review"
    releases = {item.release.label for item in events if item.release is not None and item.event != "reviewed"}
    return label + (f"; evidence: {', '.join(sorted(releases))}" if releases else "")


def _event_row(item: AlertEvent) -> list[str]:
    """``+ new alert | #23 | D2 v1 | HIGH | HNF1A 12-120994300-C-T (LP) | Reclassified``."""
    alert = item.alert
    rule = item.rule_version.label if item.rule_version is not None else alert.rule_code
    cls = item.classification or alert.classification
    extra = " "  # an empty last cell (a space is trimmed; None would print "-")
    if item.event == "created" and alert.is_reclassified:
        extra = F.flag("Reclassified")
    elif item.event == "confirmed":
        extra = "provenance updated"
    elif item.event == "superseded":
        reason = alert.superseded_reason or ""
        extra = C.SUPERSEDED_REASON_LABELS.get(reason, reason).lower()
    elif item.event == "reviewed" and item.user is not None:
        extra = f"by {item.user.username}"
    return [
        f"{_EVENT_VERBS.get(item.event, item.event)} {alert.kind}",
        f"#{alert.id}",
        rule,
        F.severity_label(alert.severity),
        f"{F.variant_text(alert.variant_key)} ({F.class_label(cls)})",
        extra,
    ]


def _echo_timeline(patient: Patient) -> None:
    """Every provenance event of the patient's alerts and notes, grouped by moment, newest first."""
    view = queries.recommendations_view(patient)
    current = [finding.alert for _severity, findings in view.groups for finding in findings]
    current += [finding.alert for finding in view.notes]
    F.echo(F.style(f"Recommendation history of {F.patient_text(patient)}", bold=True))
    F.paragraph(
        f"{F.plural(len(current), 'current finding')}, {F.plural(len(view.history), 'superseded finding')}. "
        "Alerts are never deleted; every change is listed with the evidence behind it (newest first)."
    )
    moments: dict[datetime, list[AlertEvent]] = {}
    for alert in [*current, *view.history]:
        for item in alert.events:
            moments.setdefault(item.at, []).append(item)
    if not moments:
        F.echo("")
        F.echo("  No alert or note has been raised for this patient.")
        return
    ordered = [
        (moment, sorted(moments[moment], key=lambda item: (_EVENT_ORDER.get(item.event, 9), item.alert_id)))
        for moment in sorted(moments, reverse=True)
    ]
    # One table for all moments, so the columns line up across the whole timeline.
    lines = iter(F.table_lines(None, [_event_row(item) for _moment, events in ordered for item in events], flex=4))
    for moment, events in ordered:
        F.echo("")
        F.echo(F.style(f"{_moment_value(moment)} UTC", bold=True) + f"  {_moment_label(events)}")
        for _item in events:
            F.echo(next(lines))
    F.echo("")
    F.echo("State right after a change: pass its time to --at, e.g.")
    F.echo(f'  flask patients history {patient.mrn} --at "{_moment_value(ordered[0][0])}"')


def _echo_as_of(patient: Patient, at: datetime) -> None:
    view = queries.recommendations_as_of(patient, at)
    F.echo(F.style(f"Recommendations of {F.patient_text(patient)} as of {view.at_display}", bold=True))
    F.echo("  The alerts and notes that were current then, with the evidence they rested on at that time.")
    F.heading(f"Alerts ({view.alert_count}) and notes ({len(view.notes)})")
    if not view.groups and not view.notes:
        F.echo("  No alerts or notes were current at that time.")
    else:
        _echo_findings(view.groups, view.notes, at=at)


def _point_in_time(_ctx: click.Context, _param: click.Parameter, value: str | None) -> datetime | None:
    if value is None:
        return None
    try:
        return queries.parse_point_in_time(value)
    except ValueError:
        raise click.BadParameter(
            f'{value!r} is not a date and time; use "YYYY-MM-DD HH:MM" (UTC), e.g. "2026-01-15 09:30".'
        ) from None


@patients_cli.command("history")
@click.argument("mrn")
@click.option(
    "--at", "at", metavar='"YYYY-MM-DD HH:MM"', callback=_point_in_time,
    help="Show the alerts and notes current at this time (UTC; seconds and milliseconds allowed).",
)
@as_option(required=False, help_text="View as this account (must be a clinician or administrator).")
def history_command(mrn: str, at: datetime | None, as_user: str | None) -> None:
    """Show the recommendation history of a patient, or its state at a point in time (--at).

    Without --at: a timeline, newest first, of every change to the patient's alerts and notes
    (created, confirmed under a new release, reviewed, superseded) with its trigger and evidence.
    With --at: what the system recommended at that time and on what evidence (release, rule
    version and classification then) — reproducible because alerts are never deleted.

    \b
    Examples:
      flask patients history DEMO-0010
      flask patients history DEMO-0010 --at "2026-01-15 09:30"
      flask patients history DEMO-0010 --at 2026-01-15
    """
    F.acting_user(as_user, roles=C.CLINICAL_ROLES, action="view recommendations")
    patient = F.find_patient(mrn)
    if at is None:
        _echo_timeline(patient)
    else:
        _echo_as_of(patient, at)
    F.advisory_footer()


# ---------------------------------------------------------------------------
# FHIR export
# ---------------------------------------------------------------------------


@patients_cli.command("fhir")
@click.argument("mrn")
@click.option(
    "--output", "-o", type=click.Path(dir_okay=False), help="Write the Bundle to this file instead of the terminal."
)
@click.option("--force", is_flag=True, help="Overwrite the --output file if it exists.")
@as_option(required=False, help_text="Account recorded with the export (must be a clinician or administrator).")
def fhir_command(mrn: str, output: str | None, force: bool, as_user: str | None) -> None:
    """Export the patient's genomic data as a validated FHIR R4 Bundle (JSON).

    The Bundle is validated before it is written; the export is recorded in the audit log.

    \b
    Examples:
      flask patients fhir DEMO-0001
      flask patients fhir DEMO-0001 --output DEMO-0001_fhir.json --as dr.rao
    """
    user = F.acting_user(as_user, roles=C.CLINICAL_ROLES, action="export FHIR bundles")
    patient = F.find_patient(mrn)
    target = Path(output) if output else None
    if target is not None and target.exists() and not force:
        raise click.ClickException(f"{output} already exists; add --force to overwrite it.")
    bundle = build_patient_bundle(patient)
    try:
        validate_bundle(bundle)
    except ValueError as exc:
        logger.exception("FHIR export of patient %s produced an invalid Bundle", patient.mrn)
        raise click.ClickException(f"The FHIR Bundle of {patient.mrn} failed validation, so nothing was exported: {exc}") from None
    payload = json.dumps(bundle, indent=2) + "\n"
    resources = len(bundle["entry"])
    if target is not None:
        try:
            target.write_text(payload, encoding="utf-8")
        except OSError as exc:
            raise click.ClickException(f"Could not write {output}: {exc.strerror or exc}.") from None
    with F.saving("Could not record the FHIR export"):
        audit(
            "fhir_export",
            "patient",
            patient.id,
            detail={"mrn": patient.mrn, "resources": resources, "output": "file" if target else "stdout", "via": "cli"},
            user=user,
        )
    if target is None:
        click.echo(payload, nl=False)
    else:
        F.say(f"Wrote the validated FHIR R4 Bundle of {patient.mrn} ({F.plural(resources, 'resource')}) to {output}.")


def register(app: Flask) -> None:
    """Register the ``patients`` command group."""
    app.cli.add_command(patients_cli)
