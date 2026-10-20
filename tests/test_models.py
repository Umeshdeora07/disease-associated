"""Tests for ge_ehr.models (schema, constraints, properties, classmethods)."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from ge_ehr.models import (
    RUN_COUNTERS,
    Alert,
    AuditLog,
    Diagnosis,
    KbRelease,
    KnownVariant,
    Medication,
    Patient,
    PatientVariant,
    ReevaluationRun,
    Rule,
    RuleVersion,
    User,
    VariantChange,
    VariantKey,
    utc_today,
    utcnow,
)
from tests.factories import (
    TEST_PASSWORD,
    add_known_variant,
    add_medication,
    add_patient_variant,
    make_alert,
    make_patient,
    make_release,
    make_review_notice,
    make_rule_version,
    make_run,
    make_user,
    rule_definition,
)


def test_utcnow_is_naive_utc() -> None:
    now = utcnow()
    assert now.tzinfo is None
    assert abs(now - datetime.now(timezone.utc).replace(tzinfo=None)) < timedelta(seconds=5)
    assert utc_today() == now.date()


def test_all_tables_created(db: SQLAlchemy) -> None:
    assert set(db.metadata.tables) == {
        "users", "audit_log", "patients", "diagnoses", "medications", "genomic_uploads", "variant_keys",
        "patient_variants", "kb_releases", "known_variants", "rules", "rule_versions", "alerts",
        "alert_events", "review_notices", "reevaluation_runs", "variant_changes",
    }


# ---------------------------------------------------------------------------
# User
# ---------------------------------------------------------------------------


def test_user_password_hashing(db: SQLAlchemy) -> None:
    user = make_user("hash.me")
    assert user.password_hash.startswith("scrypt:")
    assert TEST_PASSWORD not in user.password_hash
    assert user.check_password(TEST_PASSWORD)
    assert not user.check_password("wrong-password-1")
    user.password_hash = ""
    assert not user.check_password(TEST_PASSWORD)


def test_user_defaults_and_normalised_username(db: SQLAlchemy) -> None:
    user = User(username="  Dr.Rao ", full_name="Dr. A. Rao", role="clinician")
    assert user.username == "dr.rao"
    assert user.active is True and user.is_active is True
    assert user.must_change_password is False
    assert user.failed_logins == 0
    assert user.role_label == "Clinician"
    user.active = False
    assert user.is_active is False


def test_user_rejects_unknown_role() -> None:
    with pytest.raises(ValueError, match="Unknown role 'nurse'"):
        User(username="x1", full_name="X", role="nurse")


def test_user_has_role(db: SQLAlchemy) -> None:
    user = make_user(role="lab_technician")
    assert user.has_role("lab_technician")
    assert user.has_role("admin", "lab_technician")
    assert not user.has_role("admin", "clinician")
    assert not user.has_role()


def test_user_is_locked() -> None:
    user = User(username="lock.me", full_name="L", role="admin")
    assert not user.is_locked()
    user.locked_until = utcnow() + timedelta(minutes=1)
    assert user.is_locked()
    assert not user.is_locked(at=utcnow() + timedelta(minutes=2))


def test_username_is_unique(db: SQLAlchemy) -> None:
    make_user("twin")
    with pytest.raises(IntegrityError):
        make_user("TWIN")


def test_session_id_round_trip_and_invalidation(db: SQLAlchemy) -> None:
    user = make_user("session.user")
    session_id = user.get_id()
    assert session_id.startswith(f"{user.id}:")
    assert User.from_session_id(session_id) is user
    user.set_password("Another-Pass-456")
    assert User.from_session_id(session_id) is None, "a password change invalidates old sessions"
    assert User.from_session_id(user.get_id()) is user
    user.active = False
    assert User.from_session_id(user.get_id()) is None, "inactive users are not loaded"


@pytest.mark.parametrize("session_id", ["", "abc", "999:deadbeef", ":", "1"])
def test_from_session_id_rejects_malformed_or_unknown_ids(db: SQLAlchemy, session_id: str) -> None:
    make_user()
    assert User.from_session_id(session_id) is None


# ---------------------------------------------------------------------------
# Constructor defaults, audit log
# ---------------------------------------------------------------------------


def test_scalar_defaults_apply_before_flush() -> None:
    run = ReevaluationRun(trigger="manual")
    assert all(getattr(run, counter) == 0 for counter in RUN_COUNTERS)
    assert (run.status, run.strategy) == ("running", "change_driven")
    run.alerts_created += 1
    assert run.alerts_created == 1
    alert = Alert(rule_code="D2")
    assert (alert.status, alert.priority, alert.notify, alert.is_reclassified) == ("active", "normal", False, False)
    assert PatientVariant().reclassified_count == 0
    assert ReevaluationRun(trigger="manual", status="failed").status == "failed"


def test_audit_log_stores_json_detail(db: SQLAlchemy) -> None:
    user = make_user()
    entry = AuditLog(action="release_imported", user=user, username=user.username, detail={"records": 12, "genes": ["GCK"]})
    db.session.add(entry)
    db.session.commit()
    db.session.expire_all()
    stored = db.session.get(AuditLog, entry.id)
    assert stored.detail == {"records": 12, "genes": ["GCK"]}
    assert stored.user is user and stored.at is not None


# ---------------------------------------------------------------------------
# Patients, diagnosis, medications
# ---------------------------------------------------------------------------


def test_patient_properties(db: SQLAlchemy) -> None:
    patient = make_patient(given_name="Maya", family_name="Okafor", diagnosis={"diabetes_type": "type1", "age_at_diagnosis_months": 180})
    assert patient.display_name == "Maya Okafor"
    assert patient.is_synthetic is True
    assert patient.diagnosis.diabetes_type == "type1"
    assert patient.diagnosis.patient is patient
    assert patient.current_drug_classes == frozenset()
    insulin = add_medication(patient, "Insulin glargine")
    add_medication(patient, "Metformin", end_date=date(2024, 1, 1))
    add_medication(patient, "Liraglutide")
    assert insulin.is_active
    assert [m.drug_name for m in patient.active_medications] == ["Insulin glargine", "Liraglutide"]
    assert patient.current_drug_classes == frozenset({"insulin", "other"})
    assert isinstance(patient.current_drug_classes, frozenset)


def _years_before(day: date, years: int) -> date:
    try:
        return day.replace(year=day.year - years)
    except ValueError:  # 29 February
        return day.replace(year=day.year - years, day=28)


def test_patient_age_years_counts_completed_years(db: SQLAlchemy) -> None:
    today = utc_today()
    patient = make_patient(birth_date=_years_before(today, 30))
    assert patient.age_years == 30, "birthday today"
    patient.birth_date = _years_before(today + timedelta(days=1), 30)
    assert patient.age_years == 29, "birthday tomorrow"


def test_diagnosis_is_one_to_one(db: SQLAlchemy) -> None:
    patient = make_patient(diagnosis="neonatal")
    assert patient.diagnosis.diabetes_type == "neonatal"
    db.session.commit()
    db.session.add(Diagnosis(patient_id=patient.id, diabetes_type="type2"))
    with pytest.raises(IntegrityError):
        db.session.flush()


def test_patient_mrn_unique(db: SQLAlchemy) -> None:
    make_patient(mrn="DUP-1")
    with pytest.raises(IntegrityError):
        make_patient(mrn="DUP-1")


@pytest.mark.parametrize(
    ("factory", "message"),
    [
        (lambda: make_patient(sex="M"), "sex"),
        (lambda: make_patient(diagnosis="type3"), "diabetes_type"),
        (lambda: add_medication(make_patient(), "Insulin", drug_class="biguanide"), "drug_class"),
    ],
)
def test_check_constraints_reject_unknown_codes(db: SQLAlchemy, factory, message: str) -> None:
    with pytest.raises(IntegrityError, match=message):
        factory()


def test_foreign_keys_are_enforced_on_sqlite(db: SQLAlchemy) -> None:
    db.session.add(Medication(patient_id=9999, drug_name="Insulin", drug_class="insulin"))
    with pytest.raises(IntegrityError, match="FOREIGN KEY"):
        db.session.flush()


# ---------------------------------------------------------------------------
# Variant keys and patient variants
# ---------------------------------------------------------------------------


def test_variant_key_get_or_create_normalises_and_is_idempotent(db: SQLAlchemy) -> None:
    key = VariantKey.get_or_create("chr7", "44145200", "c", "t")
    assert (key.chrom, key.pos, key.ref, key.alt) == ("7", 44_145_200, "C", "T")
    assert key.label == "7-44145200-C-T"
    assert key.gene == "GCK", "gene defaults to the panel gene at the position"
    assert key.id is not None
    assert VariantKey.get_or_create("7", 44_145_200, "C", "T") is key
    assert VariantKey.get_or_create("CHR7", 44_145_200, " C ", "t") is key
    assert VariantKey.find("chr7", 44_145_200, "C", "T") is key
    assert VariantKey.find("7", 44_145_201, "C", "T") is None
    assert db.session.scalar(select(VariantKey).where(VariantKey.pos == 44_145_200)) is key


def test_variant_key_gene_handling(db: SQLAlchemy) -> None:
    off_panel = VariantKey.get_or_create("1", 1_000_000, "A", "G")
    assert off_panel.gene is None
    assert VariantKey.get_or_create("1", 1_000_000, "A", "G", gene="TEST1").gene == "TEST1"
    explicit = VariantKey.get_or_create("chrM", 100, "A", "G", gene="MT-X")
    assert explicit.chrom == "MT" and explicit.gene == "MT-X"


@pytest.mark.parametrize(
    ("pos", "ref", "alt", "message"),
    [(0, "A", "G", "positive integer"), ("x", "A", "G", "positive integer"), (5, "", "G", "must not be empty"), (5, "A", " ", "must not be empty")],
)
def test_variant_key_rejects_invalid_values(db: SQLAlchemy, pos, ref: str, alt: str, message: str) -> None:
    with pytest.raises(ValueError, match=message):
        VariantKey.get_or_create("7", pos, ref, alt)


def test_patient_variant_properties_and_uniqueness(db: SQLAlchemy) -> None:
    release = make_release("R1", current=True)
    patient = make_patient()
    variant = add_patient_variant(patient, "12", 120_978_900, "G", "A", classification="P", release=release)
    assert variant.gene == "HNF1A"
    assert variant.label == "12-120978900-G-A"
    assert variant.classification_release is release
    assert variant.reported_classification == "P"
    assert not variant.is_amended
    variant.reclassified_count = 1
    assert variant.is_amended
    assert patient.variants == [variant]
    with pytest.raises(IntegrityError):
        add_patient_variant(patient, "chr12", 120_978_900, "G", "A")


def test_patient_variant_rejects_unknown_classification(db: SQLAlchemy) -> None:
    with pytest.raises(IntegrityError, match="current_classification"):
        add_patient_variant(make_patient(), "7", 44_145_200, "C", "T", classification="Pathogenic")


# ---------------------------------------------------------------------------
# Knowledge base and rules
# ---------------------------------------------------------------------------


def test_kb_release_current(db: SQLAlchemy) -> None:
    assert KbRelease.current() is None
    first = make_release("R1", current=True)
    assert KbRelease.current() is first and first.sequence == 1
    make_release("R2 (not activated)")
    assert KbRelease.current() is first
    second = make_release("R2", current=True)
    assert KbRelease.current() is second and second.sequence == 2
    assert first.is_current is False


def test_known_variants_write_only_collection(db: SQLAlchemy) -> None:
    release = make_release("R1")
    known = add_known_variant(release, "7", 44_145_200, "C", "T", "P", review_status="reviewed_by_expert_panel", review_stars=3, is_expert_panel=True)
    assert known.gene == "GCK" and known.classification_source == "clinvar" and known.mechanism == "unknown"
    assert db.session.scalars(release.known_variants.select()).all() == [known]
    with pytest.raises(IntegrityError):
        add_known_variant(release, "chr7", 44_145_200, "c", "t", "LP")


def test_known_variant_rejects_unknown_mechanism(db: SQLAlchemy) -> None:
    with pytest.raises(IntegrityError, match="mechanism"):
        add_known_variant(make_release(), "11", 17_387_100, "C", "T", "P", mechanism="gain")


def test_release_file_hash_is_unique(db: SQLAlchemy) -> None:
    make_release("A", file_sha256="ab" * 32)
    with pytest.raises(IntegrityError):
        make_release("B", file_sha256="ab" * 32)


def test_rule_versions(db: SQLAlchemy) -> None:
    d4_v1 = make_rule_version(rule_definition("D4", 1, genes=["KCNJ11"], mechanism="activating"))
    make_rule_version(rule_definition("D2", 1))
    d4_v2 = make_rule_version(rule_definition("D4", 2, genes=["KCNJ11", "ABCC8"], mechanism="activating"))
    assert [version.label for version in RuleVersion.current_versions()] == ["D2 v1", "D4 v2"]
    assert d4_v1.is_current is False
    rule = db.session.execute(select(Rule).filter_by(code="D4")).scalar_one()
    assert [version.version for version in rule.versions] == [1, 2]
    assert rule.current_version is d4_v2
    assert d4_v2.genes == ["KCNJ11", "ABCC8"]
    assert d4_v2.definition["genetic"]["mechanism"] == "activating"
    with pytest.raises(IntegrityError):
        make_rule_version(rule_definition("D4", 2))


# ---------------------------------------------------------------------------
# Alerts, events, notices, runs
# ---------------------------------------------------------------------------


@pytest.fixture
def alert_setup(db: SQLAlchemy) -> tuple[PatientVariant, RuleVersion, KbRelease]:
    release = make_release("R1", current=True)
    patient = make_patient()
    variant = add_patient_variant(patient, "12", 120_978_900, "G", "A", classification="P", release=release)
    rule_version = make_rule_version(rule_definition("D2", 1))
    return variant, rule_version, release


def test_alert_relationships_and_provenance(db: SQLAlchemy, alert_setup) -> None:
    variant, rule_version, release = alert_setup
    alert = make_alert(variant, rule_version, release=release)
    assert alert.patient is variant.patient
    assert alert.variant_key is variant.variant_key
    assert alert.created_rule_version is rule_version and alert.confirmed_rule_version is rule_version
    assert alert.created_release is release and alert.confirmed_release is release
    assert alert.is_current and not alert.is_note
    assert [event.event for event in alert.events] == ["created"]
    assert variant.patient.alerts == [alert]
    note = make_alert(variant, rule_version, kind="note")
    assert note.is_note and note.severity is None


def test_at_most_one_current_alert_per_finding(db: SQLAlchemy, alert_setup) -> None:
    variant, rule_version, release = alert_setup
    make_alert(variant, rule_version, release=release)
    with pytest.raises(IntegrityError, match="UNIQUE"):
        make_alert(variant, rule_version, release=release)


def test_superseded_alerts_do_not_count_as_current(db: SQLAlchemy, alert_setup) -> None:
    variant, rule_version, release = alert_setup
    old = make_alert(variant, rule_version, release=release, severity="medium")
    old.status = "superseded"
    old.superseded_at = utcnow()
    old.superseded_reason = "rule_revised"
    replacement = make_alert(variant, rule_version, release=release)
    replacement.status = "reviewed"
    db.session.flush()
    current = db.session.scalars(select(Alert).where(Alert.status.in_(("active", "reviewed")))).all()
    assert current == [replacement]


def test_supersede_and_create_in_one_flush(db: SQLAlchemy, alert_setup) -> None:
    variant, rule_version, release = alert_setup
    old = make_alert(variant, rule_version, release=release)
    old.status = "superseded"
    db.session.add(
        Alert(
            patient_id=old.patient_id, variant_key_id=old.variant_key_id, rule_code=old.rule_code,
            created_rule_version=rule_version, confirmed_rule_version=rule_version, kind="alert", severity="low",
            title="t", message="m", classification="P", trigger="rule_revision",
        )
    )
    db.session.flush()  # the UPDATE is emitted before the INSERT, so the partial index holds


@pytest.mark.parametrize(
    ("field", "value"),
    [("kind", "warning"), ("status", "deleted"), ("severity", "critical"), ("trigger", "cron"), ("superseded_reason", "because")],
)
def test_alert_check_constraints(db: SQLAlchemy, alert_setup, field: str, value: str) -> None:
    variant, rule_version, release = alert_setup
    with pytest.raises(IntegrityError, match=field):
        make_alert(variant, rule_version, release=release, **{field: value})


def test_review_notice_links_alerts(db: SQLAlchemy, alert_setup) -> None:
    variant, rule_version, release = alert_setup
    run = make_run("clinvar_release", strategy="change_driven", new_release=release)
    alert = make_alert(variant, rule_version, release=release, status="superseded", superseded_run=run, superseded_reason="downgrade")
    notice = make_review_notice(variant, run=run, alerts=[alert])
    assert notice.status == "open"
    assert alert.review_notice is notice
    assert notice.alerts == [alert]
    assert variant.patient.review_notices == [notice]
    assert notice.run is run and run.new_release is release


def test_reevaluation_run_changes(db: SQLAlchemy) -> None:
    run = make_run("clinvar_release", summary={"initial": True})
    key = VariantKey.get_or_create("12", 120_994_300, "C", "T")
    change = VariantChange(run=run, variant_key=key, gene="HNF1A", old_classification="VUS", new_classification="LP", category="upgrade", carrier_count=1)
    db.session.add(change)
    db.session.commit()
    assert run.changes == [change]
    assert run.summary == {"initial": True}
    with pytest.raises(IntegrityError, match="category"):
        db.session.add(VariantChange(run=run, variant_key=key, old_classification="VUS", new_classification="LB", category="sideways"))
        db.session.flush()


def test_known_variant_model_has_spec_fields() -> None:
    expected = {
        "release_id", "variant_key_id", "clinvar_variation_id", "allele_id", "gene", "classification",
        "clinvar_significance", "review_status", "review_stars", "is_expert_panel", "classification_source",
        "condition", "mechanism", "hgvs", "molecular_consequence",
    }
    assert expected <= set(KnownVariant.__table__.columns.keys())


def test_alert_model_has_spec_fields() -> None:
    expected = {
        "patient_id", "patient_variant_id", "variant_key_id", "rule_code", "created_rule_version_id",
        "confirmed_rule_version_id", "kind", "severity", "title", "message", "source", "classification", "status",
        "trigger", "is_reclassified", "priority", "notify", "created_release_id", "confirmed_release_id",
        "created_run_id", "superseded_run_id", "created_at", "superseded_at", "superseded_reason", "reviewed_at",
        "reviewed_by_id", "review_comment", "review_notice_id",
    }
    assert expected <= set(Alert.__table__.columns.keys())


def test_patient_model_relationship_names() -> None:
    relationships = set(Patient.__mapper__.relationships.keys())
    assert {"diagnosis", "medications", "variants", "uploads", "alerts", "review_notices"} <= relationships
    assert {
        "patient", "variant_key", "patient_variant", "created_rule_version", "confirmed_rule_version",
        "created_release", "confirmed_release", "events", "review_notice", "reviewed_by",
    } <= set(Alert.__mapper__.relationships.keys())
    assert {"patient", "variant_key", "upload"} <= set(PatientVariant.__mapper__.relationships.keys())
    assert "changes" in ReevaluationRun.__mapper__.relationships.keys()
    assert "versions" in Rule.__mapper__.relationships.keys()
