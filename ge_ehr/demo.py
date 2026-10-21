"""Synthetic demo dataset loader used by ``flask seed-demo``, the scenario tests and the UI (SPEC §11).

:func:`seed_demo` builds the demo database through the regular services, so the result looks
exactly like a database filled through the UI (audit trail, provenance, runs):

1. the demo users ``admin`` (admin), ``dr.rao`` (clinician) and ``lab.kim`` (lab technician);
2. rule set v1 (``data/rules/ruleset_v1.json``), loaded with
   :func:`~ge_ehr.services.alerting.import_rule_versions` — a bootstrap, since no patient exists yet;
3. demo release R1 (``data/demo/clinvar/demo_release_1.vcf.gz``), imported and activated (the
   initial classification; nothing to re-evaluate yet);
4. the 15 demo patients of ``data/demo/patients.json``: dr.rao records each patient with diagnosis
   and current medications, then lab.kim uploads the patient's VCF, which classifies the variants
   under R1 and evaluates the patient (trigger ``vcf_upload``). This is the S1–S9 state of §11.

``full=True`` continues the story the live demo shows in the UI: rule set v2 is applied through the
re-evaluation engine (D4 v2 adds ABCC8 — RS5) and demo release R2 is imported and activated
change-driven (reclassifications RS1–RS4, RS6; RS7). ``activate_r2=True`` does only the latter.

Seeding requires an empty database (no patients, knowledge-base releases or rule versions) so the
scenario expectations hold; ``reset=True`` drops and recreates every table first. Demo accounts
that already exist (e.g. an ``admin`` created with ``flask create-user``) are kept unchanged.
Problems with the demo inputs are reported as :class:`DemoSeedError` naming the file or patient.
Everything is synthetic; the demo files carry the synthetic markers of SPEC §0.

Owned by the Scenario work package.
"""

from __future__ import annotations

import json
import logging
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, TypeGuard

from flask import Flask, current_app, has_app_context
from sqlalchemy import func, select

from . import constants as C
from .extensions import db
from .models import (
    RUN_COUNTERS,
    Alert,
    GenomicUpload,
    KbRelease,
    Patient,
    PatientVariant,
    ReevaluationRun,
    ReviewNotice,
    RuleVersion,
    User,
)
from .security import audit, password_policy_problems
from .services import alerting, clinvar, patient_records, reevaluation
from .services.rules import RULES_DATA_DIR, RuleDefinitionError, load_rule_set_file, rule_code_sort_key

logger = logging.getLogger(__name__)

#: Directory of the demo dataset (patients, patient VCFs, demo ClinVar-format releases).
DEMO_DATA_DIR = Path(__file__).resolve().parents[1] / "data" / "demo"
DEMO_PATIENTS_FILE = DEMO_DATA_DIR / "patients.json"
DEMO_VCF_DIR = DEMO_DATA_DIR / "vcfs"
DEMO_RELEASE_1_FILE = DEMO_DATA_DIR / "clinvar" / "demo_release_1.vcf.gz"
DEMO_RELEASE_2_FILE = DEMO_DATA_DIR / "clinvar" / "demo_release_2.vcf.gz"
RELEASE_1_LABEL = "R1 (2025-01, synthetic)"
RELEASE_2_LABEL = "R2 (2026-01, synthetic)"
#: Rule sets that can bootstrap the demo (``rules=`` argument of :func:`seed_demo`).
DEMO_RULE_SETS: dict[str, Path] = {
    "v1": RULES_DATA_DIR / "ruleset_v1.json",
    "v2": RULES_DATA_DIR / "ruleset_v2.json",
}
#: The rule set ``full=True`` applies through the re-evaluation engine (RS5).
REVISED_RULE_SET = "v2"
#: Text fields every patients.json record needs; their values are validated by patient_records.
REQUIRED_PATIENT_FIELDS: tuple[str, ...] = ("mrn", "given_name", "family_name", "sex", "birth_date", "vcf")
REQUIRED_MEDICATION_FIELDS: tuple[str, ...] = ("drug_name", "drug_class")


@dataclass(frozen=True)
class DemoUser:
    """A demo account created by :func:`seed_demo`."""

    username: str
    role: str
    full_name: str


DEMO_ADMIN = DemoUser("admin", "admin", "Demo Administrator")
DEMO_CLINICIAN = DemoUser("dr.rao", "clinician", "Dr. A. Rao")
DEMO_LAB_TECHNICIAN = DemoUser("lab.kim", "lab_technician", "J. Kim")
DEMO_USERS: tuple[DemoUser, ...] = (DEMO_ADMIN, DEMO_CLINICIAN, DEMO_LAB_TECHNICIAN)


class DemoSeedError(ValueError):
    """The demo dataset cannot be loaded as requested; the message is user-facing."""


def demo_password(password: str | None = None) -> str:
    """The password for new demo accounts: ``password`` if given, else the app's ``DEMO_PASSWORD``.

    Raises :class:`DemoSeedError` when it does not meet the password policy.
    """
    secret = password if password is not None else current_app.config["DEMO_PASSWORD"]
    problems = password_policy_problems(secret)
    if problems:
        raise DemoSeedError(f"The demo password does not meet the password policy: {' '.join(problems)}")
    return secret


def load_demo_patients(path: Path = DEMO_PATIENTS_FILE) -> list[dict[str, Any]]:
    """The demo patient records of ``patients.json`` (a list of objects, SPEC §11).

    Only the structure is checked here: every record needs the text fields of
    :data:`REQUIRED_PATIENT_FIELDS` (``vcf`` is a file name in :data:`DEMO_VCF_DIR`), a
    ``diagnosis`` object with a ``diabetes_type`` and, optionally, a ``medications`` list of
    objects with ``drug_name`` and ``drug_class``. The values themselves are validated by
    :mod:`~ge_ehr.services.patient_records` when the patients are created.

    Raises :class:`DemoSeedError` when the file is missing, is not valid JSON or a record does
    not have that structure.
    """
    try:
        records = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise DemoSeedError(f"Demo data file not found: {path}.") from None
    except json.JSONDecodeError as exc:
        raise DemoSeedError(f"{path} is not valid JSON: {exc.msg} (line {exc.lineno}).") from None
    if not isinstance(records, list) or not all(isinstance(record, dict) for record in records):
        raise DemoSeedError(f"{path} must contain a JSON list of patient objects.")
    for number, record in enumerate(records, start=1):
        problems = _record_problems(record)
        if problems:
            mrn = record["mrn"] if _is_text(record.get("mrn")) else "no MRN"
            raise DemoSeedError(f"{path}: patient #{number} ({mrn}): {'; '.join(problems)}.")
    return records


def _is_text(value: Any) -> TypeGuard[str]:
    """Whether ``value`` is a non-blank string."""
    return isinstance(value, str) and bool(value.strip())


def _record_problems(record: Mapping[str, Any]) -> list[str]:
    """The structural problems of one patients.json record (empty when it can be loaded)."""
    problems = []
    missing = [field for field in REQUIRED_PATIENT_FIELDS if not _is_text(record.get(field))]
    if missing:
        problems.append(f"missing or empty field(s) {', '.join(missing)}")
    vcf = record.get("vcf")
    if _is_text(vcf) and Path(vcf).name != vcf:
        problems.append(f"'vcf' must be a file name in {DEMO_VCF_DIR}, got {vcf!r}")
    diagnosis = record.get("diagnosis")
    if not isinstance(diagnosis, dict) or not _is_text(diagnosis.get("diabetes_type")):
        problems.append("'diagnosis' must be an object with a 'diabetes_type'")
    medications = record.get("medications", [])
    if not isinstance(medications, list) or not all(
        isinstance(medication, dict) and all(_is_text(medication.get(field)) for field in REQUIRED_MEDICATION_FIELDS)
        for medication in medications
    ):
        problems.append("'medications' must be a list of objects with a 'drug_name' and a 'drug_class'")
    return problems


def seed_demo(
    app_or_none: Flask | None = None,
    *,
    rules: str = "v1",
    full: bool = False,
    activate_r2: bool = False,
    reset: bool = False,
    password: str | None = None,
) -> dict[str, Any]:
    """Load the demo dataset (see the module docstring) and return a summary of the database.

    ``rules`` names the rule set loaded before the patients (``"v1"``, the default the scenarios
    assume, or ``"v2"``). ``full`` then applies rule set v2 through the re-evaluation engine
    (skipped when v2 was loaded already) and activates release R2; ``activate_r2`` activates R2
    only. ``reset`` drops and recreates every table first — **all data is deleted**; otherwise
    missing tables are created (as ``flask init-db`` does). ``password`` is used for the demo
    accounts created (default: the app's ``DEMO_PASSWORD``).

    ``app_or_none`` selects the application whose database is seeded. With None (or the current
    app) the work joins the current session and the caller commits, like any service call; the
    ``flask seed-demo`` command does. Given another app without an active context, its context
    is pushed for the duration and the seed is committed there, because that session ends with
    the context.

    The options, the demo files and the rule sets are checked before anything is changed.
    Raises :class:`DemoSeedError` for an unknown rule set, a password that violates the policy,
    a missing or malformed demo file or — without ``reset`` — a database that already holds
    patients, releases or rule versions; also, while loading, for a demo release or patient
    record (values or VCF) that the services reject — the caller should then roll back. Any
    other failure propagates from the service that raised it (a failed re-evaluation is
    recorded as a failed run, SPEC §7.6).

    The summary holds ``users`` (username, full_name, role, created), ``rules`` (current rule
    versions), ``releases``, ``current_release``, ``patients``, ``uploads`` (by status),
    ``patient_variants``, ``alerts`` (current alerts by severity), ``notes`` (current notes),
    ``superseded``, ``open_review_notices`` and ``runs`` (the re-evaluation runs of this seed with
    their counters).
    """
    # ``current_app == app`` compares the proxied application itself (Flask has identity equality).
    if app_or_none is None or (has_app_context() and current_app == app_or_none):
        return _seed(rules=rules, full=full, activate_r2=activate_r2, reset=reset, password=password)
    with app_or_none.app_context():
        summary = _seed(rules=rules, full=full, activate_r2=activate_r2, reset=reset, password=password)
        db.session.commit()
    return summary


# ---------------------------------------------------------------------------
# Seeding steps
# ---------------------------------------------------------------------------


def _seed(*, rules: str, full: bool, activate_r2: bool, reset: bool, password: str | None) -> dict[str, Any]:
    """Check every input, then load the demo dataset into the current session (see :func:`seed_demo`)."""
    if rules not in DEMO_RULE_SETS:
        raise DemoSeedError(f"Unknown demo rule set {rules!r}; expected one of: {', '.join(DEMO_RULE_SETS)}.")
    secret = demo_password(password)
    with_r2 = full or activate_r2
    apply_revision = full and rules != REVISED_RULE_SET
    records = load_demo_patients(DEMO_PATIENTS_FILE)
    _check_files(records, rules=rules, with_r2=with_r2, apply_revision=apply_revision)
    rule_set = _load_rule_set(rules)
    revised_rule_set = _load_rule_set(REVISED_RULE_SET) if apply_revision else None

    if reset:
        _reset_database()
    else:
        # Create any missing table (as `flask init-db` does), then refuse to mix with existing data.
        db.metadata.create_all(bind=db.session.connection())
        _check_empty()

    users, created_users = _ensure_users(secret)
    admin, clinician, lab = (users[demo.username] for demo in DEMO_USERS)

    alerting.import_rule_versions(rule_set, user=admin)
    release_1 = _import_demo_release(DEMO_RELEASE_1_FILE, RELEASE_1_LABEL, admin)
    runs = [reevaluation.activate_release(release_1, user=admin)]
    for record in records:
        _add_demo_patient(record, clinician=clinician, lab=lab)
    if revised_rule_set is not None:
        runs.append(reevaluation.apply_rule_set(revised_rule_set, user=admin))
    if with_r2:
        release_2 = _import_demo_release(DEMO_RELEASE_2_FILE, RELEASE_2_LABEL, admin)
        runs.append(reevaluation.activate_release(release_2, user=admin))

    audit(
        "demo_seeded",
        detail={
            "rules": rules,
            "full": full,
            "activate_r2": activate_r2,
            "reset": reset,
            "patients": len(records),
            "users_created": sorted(created_users),
            "run_ids": [run.id for run in runs],
        },
        user=admin,
    )
    summary = _summary(users, created_users, runs)
    logger.info(
        "Seeded the demo dataset: %d patient(s), current release %s, %d run(s)%s",
        summary["patients"],
        summary["current_release"],
        len(runs),
        " (database reset first)" if reset else "",
    )
    return summary


def _check_files(records: Sequence[Mapping[str, Any]], *, rules: str, with_r2: bool, apply_revision: bool) -> None:
    """Fail before changing anything when a demo file needed for this seed is missing."""
    needed = [DEMO_RULE_SETS[rules], DEMO_RELEASE_1_FILE]
    if apply_revision:
        needed.append(DEMO_RULE_SETS[REVISED_RULE_SET])
    if with_r2:
        needed.append(DEMO_RELEASE_2_FILE)
    needed.extend(DEMO_VCF_DIR / record["vcf"] for record in records)
    missing = [str(path) for path in needed if not path.is_file()]
    if missing:
        raise DemoSeedError(f"Demo data file(s) not found: {', '.join(missing)}.")


def _load_rule_set(name: str) -> dict[str, Any]:
    """The validated document of demo rule set ``name`` (read before anything is changed)."""
    path = DEMO_RULE_SETS[name]
    try:
        return load_rule_set_file(path)
    except (OSError, RuleDefinitionError) as exc:
        raise DemoSeedError(f"Demo rule set {path} cannot be loaded: {exc}") from exc


def _import_demo_release(path: Path, label: str, admin: User) -> KbRelease:
    """Import a demo ClinVar-format release (not yet current) imported by ``admin``."""
    try:
        return clinvar.import_release(path, label=label, user=admin)
    except ValueError as exc:  # a file import_release refuses (validated before anything is written)
        raise DemoSeedError(f"Demo release {path} could not be imported: {exc}") from exc


def _reset_database() -> None:
    """Drop and recreate every table on the session's connection — **all data is deleted**.

    Pending changes are flushed first (and dropped with everything else) and the identity map is
    cleared, so no object of the old database survives in the session. The DDL runs in the
    caller's transaction: on PostgreSQL a rollback restores the old database; SQLite commits DDL
    immediately.
    """
    db.session.flush()
    connection = db.session.connection()
    db.metadata.drop_all(bind=connection)
    db.metadata.create_all(bind=connection)
    db.session.expunge_all()
    logger.warning("Dropped and recreated all tables before seeding the demo dataset")


def _check_empty() -> None:
    """Refuse to seed a database that already holds patients, releases or rule versions."""
    counts = {
        "patient(s)": db.session.scalar(select(func.count(Patient.id))),
        "knowledge-base release(s)": db.session.scalar(select(func.count(KbRelease.id))),
        "rule version(s)": db.session.scalar(select(func.count(RuleVersion.id))),
    }
    present = [f"{count} {what}" for what, count in counts.items() if count]
    if present:
        raise DemoSeedError(
            f"The database is not empty ({', '.join(present)}); the demo dataset is only loaded into an "
            "empty database. Reset it first with 'flask seed-demo --reset' (reset=True), which drops "
            "and recreates all tables and DELETES ALL DATA."
        )


def _ensure_users(password: str) -> tuple[dict[str, User], set[str]]:
    """The demo accounts by username, creating missing ones; also returns the usernames created.

    An existing account with a demo username is kept as it is (role and password unchanged).
    """
    names = [demo.username for demo in DEMO_USERS]
    users = {user.username: user for user in db.session.scalars(select(User).where(User.username.in_(names)))}
    created: set[str] = set()
    for demo in DEMO_USERS:
        if demo.username in users:
            continue
        user = User(username=demo.username, full_name=demo.full_name, role=demo.role, must_change_password=False)
        user.set_password(password)
        db.session.add(user)
        db.session.flush()
        audit(
            "user_created", "user", user.id, detail={"username": user.username, "role": user.role, "via": "seed-demo"}
        )
        users[demo.username] = user
        created.add(demo.username)
    return users, created


def _add_demo_patient(record: Mapping[str, Any], *, clinician: User, lab: User) -> None:
    """Record one demo patient (clinician: demographics, diagnosis, medications), then upload the VCF (lab).

    Medications come first, so the upload evaluates the patient against the current therapy and
    the history starts with the final alerts instead of notes superseded a moment later. The
    ISO date strings of patients.json are parsed and validated by the patient record service.
    Raises :class:`DemoSeedError` naming the patient when the service rejects a value or the VCF.
    """
    diagnosis = record["diagnosis"]
    try:
        patient = patient_records.create_patient(
            mrn=record["mrn"],
            given_name=record["given_name"],
            family_name=record["family_name"],
            sex=record["sex"],
            birth_date=record["birth_date"],
            family_id=record.get("family_id"),
            notes=record.get("notes"),
            diabetes_type=diagnosis["diabetes_type"],
            age_at_diagnosis_months=diagnosis.get("age_at_diagnosis_months"),
            diagnosed_on=diagnosis.get("diagnosed_on"),
            user=clinician,
        )
        for medication in record.get("medications", []):
            patient_records.add_medication(
                patient,
                drug_name=medication["drug_name"],
                drug_class=medication["drug_class"],
                dose=medication.get("dose"),
                start_date=medication.get("start_date"),
                user=clinician,
            )
        vcf = DEMO_VCF_DIR / record["vcf"]
        patient_records.ingest_vcf(patient, vcf.read_bytes(), vcf.name, user=lab)
    except ValueError as exc:  # the services' validation errors, including VcfError for the VCF
        raise DemoSeedError(f"Demo patient {record['mrn']} could not be loaded: {exc}") from exc


# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------


def _run_summary(run: ReevaluationRun) -> dict[str, Any]:
    return {
        "id": run.id,
        "trigger": run.trigger,
        "strategy": run.strategy,
        "status": run.status,
        # The activated release, or (rule revision) the release the rules were evaluated under.
        "release": run.new_release.label if run.new_release is not None else None,
        "rule_change": run.rule_change or [],
        "initial": bool((run.summary or {}).get("initial", False)),
        **{name: getattr(run, name) for name in RUN_COUNTERS},
    }


def _summary(users: Mapping[str, User], created_users: set[str], runs: Sequence[ReevaluationRun]) -> dict[str, Any]:
    """Counts describing the seeded database (a fixed number of aggregate queries)."""
    current_findings = Counter(
        {
            (kind, severity): count
            for kind, severity, count in db.session.execute(
                select(Alert.kind, Alert.severity, func.count(Alert.id))
                .where(Alert.status.in_(C.CURRENT_ALERT_STATUSES))
                .group_by(Alert.kind, Alert.severity)
            )
        }
    )
    uploads = {
        status: count
        for status, count in db.session.execute(
            select(GenomicUpload.status, func.count(GenomicUpload.id)).group_by(GenomicUpload.status)
        )
    }
    releases = list(db.session.scalars(select(KbRelease).order_by(KbRelease.id)))
    current_release = next((release for release in releases if release.is_current), None)
    return {
        "users": [
            {
                "username": users[demo.username].username,
                "full_name": users[demo.username].full_name,
                "role": users[demo.username].role,
                "created": demo.username in created_users,
            }
            for demo in DEMO_USERS
        ],
        "rules": [
            version.label
            for version in sorted(RuleVersion.current_versions(), key=lambda version: rule_code_sort_key(version.code))
        ],
        "releases": [
            {
                "id": release.id,
                "label": release.label,
                "record_count": release.record_count,
                "is_current": release.is_current,
                "sequence": release.sequence,
            }
            for release in releases
        ],
        "current_release": current_release.label if current_release is not None else None,
        "patients": db.session.scalar(select(func.count(Patient.id))),
        "uploads": {status: uploads.get(status, 0) for status in C.UPLOAD_STATUSES},
        "patient_variants": db.session.scalar(select(func.count(PatientVariant.id))),
        "alerts": {severity: current_findings[("alert", severity)] for severity in C.SEVERITIES},
        "notes": current_findings[("note", None)],
        "superseded": db.session.scalar(select(func.count(Alert.id)).where(Alert.status == "superseded")),
        "open_review_notices": db.session.scalar(
            select(func.count(ReviewNotice.id)).where(ReviewNotice.status == "open")
        ),
        "runs": [_run_summary(run) for run in runs],
    }
