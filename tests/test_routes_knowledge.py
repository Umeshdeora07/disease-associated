"""Route tests of the knowledge blueprint (UI-B): ClinVar releases (list, import, detail,
activation, comparison), decision rules (list, import) and re-evaluation runs (list, detail,
full recomputation).

Read-only pages are checked against one fully seeded demo database (``flask seed-demo --full``:
rules v1 → v2, releases R1 → R2) shared by the module; every test that changes data gets its own
database seeded with rules v1 and R1, and imports ``data/demo`` files through the routes, so the
demo scenarios of SPEC §11 run end to end through the web interface.
"""

from __future__ import annotations

import io
import json
import re
import sqlite3
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from flask import Flask
from flask.testing import FlaskClient
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import delete, func, select
from werkzeug.test import TestResponse

from ge_ehr.demo import DEMO_RELEASE_1_FILE, DEMO_RELEASE_2_FILE
from ge_ehr.extensions import db as _db
from ge_ehr.knowledge import queries
from ge_ehr.models import Alert, AuditLog, KbRelease, Patient, ReevaluationRun, RuleVersion
from ge_ehr.services import clinvar, reevaluation
from tests.conftest import build_app
from tests.demo_fixtures import (  # noqa: F401  (fixtures)
    DEMO_PASSWORD,
    admin,
    clinician,
    count_queries,
    demo_admin,
    demo_app,
    demo_clinician,
    demo_lab,
    flashes,
    restore,
    seed_snapshot,
    seeded,
    text,
)
from tests.factories import make_release, make_run
from tests.ui_data import csrf_token

REPO = Path(__file__).resolve().parents[1]
RULESET_V1 = REPO / "data" / "rules" / "ruleset_v1.json"
RULESET_V2 = REPO / "data" / "rules" / "ruleset_v2.json"
OVERLAY_EXAMPLE = REPO / "data" / "demo" / "expert_panel_overlay_example.csv"
#: Demo variant V11 (GCK VUS → P in R2) is carried by nobody (RS7).
V11_LABEL = "7-44160100-T-C"

VIEW_PAGES = (
    "/knowledge/releases",
    "/knowledge/releases/1",
    "/knowledge/releases/compare",
    "/knowledge/rules",
    "/knowledge/runs",
    "/knowledge/runs/3",
)
ADMIN_PAGES = ("/knowledge/releases/import", "/knowledge/rules/import")
ADMIN_POSTS = ("/knowledge/releases/1/activate", "/knowledge/recompute", "/knowledge/releases/import", "/knowledge/rules/import")


def patient(mrn: str) -> Patient:
    return _db.session.scalar(select(Patient).where(Patient.mrn == mrn))


def current_findings(mrn: str) -> set[tuple[str, str, str | None]]:
    """``(rule code, kind, severity)`` of a patient's current alerts and notes."""
    rows = _db.session.execute(
        select(Alert.rule_code, Alert.kind, Alert.severity)
        .join(Patient, Patient.id == Alert.patient_id)
        .where(Patient.mrn == mrn, Alert.status.in_(("active", "reviewed")))
    )
    return {tuple(row) for row in rows}


def audit_count(action: str) -> int:
    return _db.session.scalar(select(func.count(AuditLog.id)).where(AuditLog.action == action)) or 0


def file_field(path: Path, name: str | None = None) -> tuple[io.BytesIO, str]:
    return io.BytesIO(path.read_bytes()), name or path.name


def _post_form(client: FlaskClient, url: str, data: dict[str, Any]) -> TestResponse:
    """POST multipart form data; fields set to None are left out."""
    fields = {name: value for name, value in data.items() if value is not None}
    return client.post(url, data=fields, content_type="multipart/form-data")


def import_release(client: FlaskClient, path: Path = DEMO_RELEASE_2_FILE, **fields: Any) -> TestResponse:
    return _post_form(
        client, "/knowledge/releases/import", {"vcf_file": file_field(path), "strategy": "change_driven", **fields}
    )


def import_rules(client: FlaskClient, **fields: Any) -> TestResponse:
    return _post_form(
        client, "/knowledge/rules/import", {"strategy": "change_driven", "submit": "Import and re-evaluate", **fields}
    )


# ---------------------------------------------------------------------------
# Roles (SPEC §8.1)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("path", VIEW_PAGES + ADMIN_PAGES)
def test_anonymous_users_are_sent_to_login(demo_app: Flask, path: str) -> None:
    response = demo_app.test_client().get(path)
    assert response.status_code == 302
    assert "/login" in response.headers["Location"]


@pytest.mark.parametrize("path", VIEW_PAGES + ADMIN_PAGES)
def test_lab_technicians_cannot_open_knowledge_pages(demo_lab: FlaskClient, path: str) -> None:
    assert demo_lab.get(path).status_code == 403


@pytest.mark.parametrize("path", ADMIN_POSTS)
def test_lab_technicians_and_clinicians_cannot_change_the_knowledge_base(
    demo_lab: FlaskClient, demo_clinician: FlaskClient, path: str
) -> None:
    before = _db.session.scalar(select(func.count(ReevaluationRun.id)))
    assert demo_lab.post(path).status_code == 403
    assert demo_clinician.post(path).status_code == 403
    assert _db.session.scalar(select(func.count(ReevaluationRun.id))) == before


def test_denied_access_is_audited(demo_lab: FlaskClient) -> None:
    before = audit_count("access_denied")
    demo_lab.get("/knowledge/runs")
    entry = _db.session.scalars(select(AuditLog).where(AuditLog.action == "access_denied").order_by(AuditLog.id.desc())).first()
    assert audit_count("access_denied") == before + 1
    assert entry.detail["path"] == "/knowledge/runs" and entry.username == "lab.kim"


@pytest.mark.parametrize("path", VIEW_PAGES)
def test_clinicians_can_view_every_knowledge_page(demo_clinician: FlaskClient, path: str) -> None:
    assert demo_clinician.get(path).status_code == 200


@pytest.mark.parametrize("path", ADMIN_PAGES)
def test_clinicians_cannot_open_import_pages(demo_clinician: FlaskClient, path: str) -> None:
    assert demo_clinician.get(path).status_code == 403


def test_clinicians_do_not_see_administrator_actions(demo_clinician: FlaskClient) -> None:
    releases = text(demo_clinician.get("/knowledge/releases"))
    old_release = text(demo_clinician.get("/knowledge/releases/1"))
    rules = text(demo_clinician.get("/knowledge/rules"))
    runs = text(demo_clinician.get("/knowledge/runs"))
    assert "/knowledge/releases/import" not in releases
    assert "/knowledge/releases/1/activate" not in old_release and "Activate this release" not in old_release
    assert "/knowledge/rules/import" not in rules
    assert "/knowledge/recompute" not in runs


def test_administrators_see_their_actions(demo_admin: FlaskClient) -> None:
    assert "/knowledge/releases/import" in text(demo_admin.get("/knowledge/releases"))
    old_release = text(demo_admin.get("/knowledge/releases/1"))
    assert 'action="/knowledge/releases/1/activate"' in old_release
    assert "data-confirm=" in old_release
    assert "/knowledge/rules/import" in text(demo_admin.get("/knowledge/rules"))
    runs = text(demo_admin.get("/knowledge/runs"))
    assert 'action="/knowledge/recompute"' in runs and "data-confirm=" in runs


def test_import_pages_render_for_administrators(demo_admin: FlaskClient) -> None:
    release_page = text(demo_admin.get("/knowledge/releases/import"))
    assert 'enctype="multipart/form-data"' in release_page
    for field in ('name="vcf_file"', 'name="label"', 'name="release_date"', 'name="expert_panel_csv"',
                  'name="activate"', 'name="strategy"'):
        assert field in release_page
    assert "R2 (2026-01, synthetic)" in release_page  # the current release is explained
    rules_page = text(demo_admin.get("/knowledge/rules/import"))
    for field in ('name="rules_file"', 'name="rules_json"', 'name="strategy"', 'name="validate_only"'):
        assert field in rules_page


# ---------------------------------------------------------------------------
# Releases (read-only)
# ---------------------------------------------------------------------------


def test_release_list_shows_both_releases_with_their_activation(demo_clinician: FlaskClient) -> None:
    page = text(demo_clinician.get("/knowledge/releases"))
    assert "R1 (2025-01, synthetic)" in page and "R2 (2026-01, synthetic)" in page
    assert page.count("badge-current") == 1
    assert "Synthetic" in page and "demo_release_2.vcf.gz" in page
    r2 = _db.session.scalar(select(KbRelease).where(KbRelease.is_current.is_(True)))
    assert f"{r2.record_count:,}" in page
    assert 'href="/knowledge/runs/3"' in page and 'href="/knowledge/runs/1"' in page  # activation runs
    assert "14 distinct patient variant keys" in page


def test_release_list_queries_do_not_grow_with_the_number_of_releases(demo_app: Flask, demo_clinician: FlaskClient) -> None:
    _db.session.expire_all()
    with count_queries(_db) as before:
        demo_clinician.get("/knowledge/releases")
    for number in range(3):
        make_release(f"Extra release {number}", file_sha256=f"{number:064d}")
    _db.session.commit()
    try:
        _db.session.expire_all()
        with count_queries(_db) as after:
            response = demo_clinician.get("/knowledge/releases")
        assert response.status_code == 200 and "Extra release 2" in text(response)
        assert len(after) == len(before)
    finally:
        _db.session.execute(delete(KbRelease).where(KbRelease.label.like("Extra release%")))
        _db.session.commit()


def test_release_detail_shows_gene_by_class_counts(demo_clinician: FlaskClient) -> None:
    release = _db.session.get(KbRelease, 2)
    response = demo_clinician.get("/knowledge/releases/2")
    page = text(response)
    assert response.status_code == 200
    detail = queries.release_detail(release)
    assert {row.gene for row in detail.rows} == {"GCK", "HNF1A", "HNF4A", "HNF1B", "KCNJ11", "ABCC8", "INS"}
    assert detail.class_totals == {cls: release.class_counts.get(cls, 0) for cls in detail.class_totals}
    assert sum(row.total for row in detail.rows) == release.record_count
    for cls, count in release.class_counts.items():
        assert f"{count:,}" in page, cls
    assert detail.expert_panel > 0 and "Expert panel" in page
    assert detail.stored_keys == 14 and detail.stored_keys_listed == 14
    assert "Activation history" in page and 'href="/knowledge/runs/3"' in page
    # The current release offers the comparison with its predecessor and no activation.
    assert "/knowledge/releases/compare?old=1&amp;new=2" in page
    assert "Activate this release" not in page


def test_unknown_release_is_404(demo_clinician: FlaskClient) -> None:
    response = demo_clinician.get("/knowledge/releases/999")
    assert response.status_code == 404
    assert "Knowledge-base release 999 does not exist." in text(response)


# ---------------------------------------------------------------------------
# Comparison
# ---------------------------------------------------------------------------


def test_compare_defaults_to_the_last_two_activations_and_stored_keys(demo_clinician: FlaskClient) -> None:
    response = demo_clinician.get("/knowledge/releases/compare")
    page = text(response)
    assert response.status_code == 200
    assert re.search(r"<strong>5</strong> changed keys\s+among 14 stored patient variant keys", page)
    assert re.search(r"<strong>7</strong> patients carry a changed variant", page)
    for label in ("12-120994300-C-T", "20-44410200-G-T", "7-44150500-G-A", "20-44401300-C-T", "12-120999100-A-G"):
        assert label in page
    assert V11_LABEL not in page  # carried by nobody: never compared by default (RS7)


def test_compare_summary_by_category_and_gene(demo_app: Flask) -> None:
    r1, r2 = _db.session.get(KbRelease, 1), _db.session.get(KbRelease, 2)
    comparison = queries.compare_releases(r1, r2, all_keys=False, category=None)
    by_category = {summary.category: (summary.changes, summary.carriers) for summary in comparison.categories}
    assert by_category == {
        "upgrade": (2, 4),
        "downgrade": (1, 1),
        "within_actionable": (1, 1),
        "within_non_actionable": (1, 1),
    }
    genes = {summary.gene: (summary.total, summary.carriers) for summary in comparison.genes}
    assert genes == {"GCK": (1, 1), "HNF1A": (2, 2), "HNF4A": (2, 4)}
    assert comparison.carriers_affected == 7 and not comparison.activation_preview
    # Changes carried by a patient come first, upgrades before downgrades.
    assert [change.category for change in comparison.changes][:2] == ["upgrade", "upgrade"]


def test_compare_all_keys_includes_variants_nobody_carries(demo_clinician: FlaskClient) -> None:
    page = text(demo_clinician.get("/knowledge/releases/compare?old=1&new=2&all=1"))
    r1, r2 = _db.session.get(KbRelease, 1), _db.session.get(KbRelease, 2)
    comparison = queries.compare_releases(r1, r2, all_keys=True, category=None)
    assert comparison.total == len(clinvar.diff_releases(r1, r2)) > 5
    assert f"<strong>{comparison.total:,}</strong> changed keys" in page and "across both releases" in page
    v11 = next(row for row in queries.change_rows(comparison.changes, comparison) if row.variant_key.label == V11_LABEL)
    assert (v11.change.category, v11.carriers) == ("upgrade", 0)  # RS7: changed, but carried by nobody
    # Changes carried by a patient are listed first, so page 1 starts with the five stored ones.
    assert [comparison.carriers_by_key.get(change.variant_key_id, 0) > 0 for change in comparison.changes[:6]] == [
        True, True, True, True, True, False
    ]


def test_compare_category_filter_lists_only_that_category(demo_clinician: FlaskClient) -> None:
    page = text(demo_clinician.get("/knowledge/releases/compare?old=1&new=2&category=downgrade"))
    assert "7-44150500-G-A" in page
    assert "12-120994300-C-T" not in page
    assert "Downgrade out of actionable band" in page  # heading of the filtered list


def test_compare_can_be_reversed(demo_clinician: FlaskClient) -> None:
    comparison = queries.compare_releases(
        _db.session.get(KbRelease, 2), _db.session.get(KbRelease, 1), all_keys=False, category=None
    )
    assert {summary.category: summary.changes for summary in comparison.categories}["downgrade"] == 2
    assert demo_clinician.get("/knowledge/releases/compare?old=2&new=1").status_code == 200


def test_compare_same_release_is_rejected(demo_clinician: FlaskClient) -> None:
    response = demo_clinician.get("/knowledge/releases/compare?old=2&new=2")
    assert response.status_code == 400
    assert "Choose two different releases to compare." in text(response)


@pytest.mark.parametrize("query", ["old=999&new=2", "old=abc&new=2", "old=1&new=-1"])
def test_compare_unknown_release_is_404(demo_clinician: FlaskClient, query: str) -> None:
    assert demo_clinician.get(f"/knowledge/releases/compare?{query}").status_code == 404


def test_compare_with_fewer_than_two_releases_explains_why(clinician: FlaskClient) -> None:
    response = clinician.get("/knowledge/releases/compare")
    assert response.status_code == 200
    assert "At least two releases are needed" in text(response)


# ---------------------------------------------------------------------------
# Rules and runs (read-only)
# ---------------------------------------------------------------------------


def test_rules_page_shows_the_current_rule_set_with_history(demo_clinician: FlaskClient) -> None:
    page = text(demo_clinician.get("/knowledge/rules"))
    for code in ("D1", "D2", "D3", "D4", "D5", "D6"):
        assert f'id="rule-{code}"' in page
    assert "On insulin · diagnosed before 12 months of age" in page  # describe_clinical_condition (D4)
    assert "On insulin or metformin · not on a sulfonylurea" in page  # D2
    assert "No therapy condition" in page  # D5, D6
    assert "KCNJ11 or ABCC8 · P/LP · activating" in page  # D4 v2 genetic criterion in the history
    assert "Version history (2)" in page  # D4 v1 and v2
    assert "Advisory — for specialist review" in page
    d4 = queries.rule_cards()[3]
    assert d4.rule.code == "D4" and d4.current.version.version == 2
    assert [view.version.version for view in d4.history] == [2, 1]


def test_runs_page_lists_every_run_newest_first(demo_clinician: FlaskClient) -> None:
    page = text(demo_clinician.get("/knowledge/runs"))
    positions = [page.index(f'href="/knowledge/runs/{run_id}"') for run_id in (3, 2, 1)]
    assert positions == sorted(positions)
    assert "Rule revision" in page and "ClinVar release" in page
    assert "No release (initial)" in page  # run #1 is the first activation


@pytest.mark.parametrize(
    ("query", "listed", "absent"),
    [
        ("trigger=rule_revision", [2], [1, 3]),
        ("trigger=clinvar_release", [1, 3], [2]),
        ("status=success", [1, 2, 3], []),
        ("trigger=bogus&status=bogus", [1, 2, 3], []),
    ],
)
def test_runs_page_filters(demo_clinician: FlaskClient, query: str, listed: list[int], absent: list[int]) -> None:
    page = text(demo_clinician.get(f"/knowledge/runs?{query}"))
    for run_id in listed:
        assert f'href="/knowledge/runs/{run_id}"' in page
    for run_id in absent:
        assert f'href="/knowledge/runs/{run_id}"' not in page


def test_runs_page_filter_without_matches_shows_empty_state(demo_clinician: FlaskClient) -> None:
    page = text(demo_clinician.get("/knowledge/runs?status=failed"))
    assert "No run matches these filters" in page


def test_release_run_detail_shows_counters_changes_and_carriers(demo_clinician: FlaskClient) -> None:
    run = _db.session.get(ReevaluationRun, 3)
    page = text(demo_clinician.get("/knowledge/runs/3"))
    assert "Run #3 · ClinVar release" in page
    assert "R1 (2025-01, synthetic) → R2 (2026-01, synthetic)" in page
    assert run.changed_variants == 5 and run.carriers_affected == 7
    for label in ("Stored keys compared", "Changed variants", "Carriers affected", "Records examined",
                  "Alerts created", "Superseded", "Provenance confirmed", "Review notices", "Notifications"):
        assert label in page
    assert "By change category" in page and "Superseded by reason" in page
    # Carriers link to their recommendations (RS6: the whole family is listed).
    for mrn in ("DEMO-0013", "DEMO-0014", "DEMO-0015"):
        assert f'href="/patients/{patient(mrn).id}/recommendations"' in page
    assert "Affected patients" in page and "7 patients with changed findings" in page
    assert '/knowledge/releases/compare?old=1&amp;new=2' in page


def test_run_summary_matches_the_run(demo_app: Flask) -> None:
    run = _db.session.get(ReevaluationRun, 3)
    summary = queries.run_summary(run)
    assert [name for name, _ in summary.categories] == ["upgrade", "downgrade", "within_actionable", "within_non_actionable"]
    assert dict(summary.categories)["downgrade"]["review_notices"] == 1
    affected, total = queries.affected_patients(run)
    assert total == 7
    by_mrn = {item.patient.mrn: item for item in affected}
    assert by_mrn["DEMO-0010"].notices == 1 and by_mrn["DEMO-0010"].superseded == 2  # RS2
    assert by_mrn["DEMO-0005"].confirmed == 2 and by_mrn["DEMO-0005"].notifications == 0  # RS3
    assert by_mrn["DEMO-0008"].alerts_created == 2 and by_mrn["DEMO-0008"].notifications == 2  # RS1


def test_rule_revision_run_detail(demo_clinician: FlaskClient) -> None:
    page = text(demo_clinician.get("/knowledge/runs/2"))
    assert "Run #2 · Rule revision" in page
    assert "By rule" in page and 'href="/knowledge/rules#rule-D4"' in page
    assert f'href="/patients/{patient("DEMO-0012").id}/recommendations"' in page  # RS5 gains D4
    assert "Variant changes" not in page


def test_first_activation_is_labelled_initial_classification(demo_clinician: FlaskClient) -> None:
    page = text(demo_clinician.get("/knowledge/runs/1"))
    assert "initial classification, not a reclassification" in page


def test_unknown_run_is_404(demo_clinician: FlaskClient) -> None:
    response = demo_clinician.get("/knowledge/runs/999")
    assert response.status_code == 404
    assert "Re-evaluation run 999 does not exist." in text(response)


def test_failed_run_shows_its_error(clinician: FlaskClient, db: SQLAlchemy) -> None:
    run = make_run("clinvar_release", status="failed", strategy="change_driven", error="RuntimeError: disk full")
    db.session.commit()
    page = text(clinician.get(f"/knowledge/runs/{run.id}"))
    assert "The run failed and was rolled back." in page and "RuntimeError: disk full" in page
    runs_page = text(clinician.get("/knowledge/runs?status=failed"))
    assert f'href="/knowledge/runs/{run.id}"' in runs_page and "No change applied" in runs_page


def test_run_pages_queries_do_not_grow_with_the_number_of_runs(clinician: FlaskClient, db: SQLAlchemy) -> None:
    db.session.expire_all()
    with count_queries(db) as before:
        clinician.get("/knowledge/runs")
    release = db.session.get(KbRelease, 1)
    for _ in range(5):
        make_run("full_recompute", status="success", strategy="full", old_release=release, new_release=release)
    db.session.commit()
    db.session.expire_all()
    with count_queries(db) as after:
        clinician.get("/knowledge/runs")
    assert len(after) == len(before)


# ---------------------------------------------------------------------------
# Release import and activation
# ---------------------------------------------------------------------------


def test_import_release_without_activation(admin: FlaskClient, db: SQLAlchemy) -> None:
    response = import_release(admin, label="R2 (2026-01, synthetic)", release_date="2026-01-05", notes="Demo file")
    release = db.session.scalar(select(KbRelease).where(KbRelease.label == "R2 (2026-01, synthetic)"))
    assert response.status_code == 302
    assert response.headers["Location"].endswith(f"/knowledge/releases/{release.id}")
    assert not release.is_current and release.sequence is None
    assert release.file_name == "demo_release_2.vcf.gz" and release.is_synthetic
    assert release.release_date.isoformat() == "2026-01-05" and release.notes == "Demo file"
    assert audit_count("release_imported") == 2  # R1 by the seed, R2 here — audited once by the service
    assert db.session.scalar(select(func.count(ReevaluationRun.id))) == 1  # only R1's activation
    category, message = flashes(admin)[-1]
    assert category == "success" and "It is not active yet" in message
    page = text(admin.get(response.headers["Location"]))
    assert "Activate this release" in page  # the admin can activate it from its page


def test_import_release_defaults_label_and_date_from_the_file(admin: FlaskClient, db: SQLAlchemy) -> None:
    import_release(admin)
    release = db.session.scalar(select(KbRelease).order_by(KbRelease.id.desc()))
    assert release.label == f"ClinVar {release.file_date}"
    assert release.release_date.isoformat() == release.file_date


def test_import_and_activate_runs_the_change_driven_reevaluation(admin: FlaskClient, db: SQLAlchemy) -> None:
    response = import_release(admin, label="R2 (2026-01, synthetic)", activate="y")
    release = db.session.scalar(select(KbRelease).where(KbRelease.label == "R2 (2026-01, synthetic)"))
    run = db.session.scalar(select(ReevaluationRun).where(ReevaluationRun.new_release_id == release.id))
    assert response.status_code == 302
    assert response.headers["Location"].endswith(f"/knowledge/runs/{run.id}")
    assert release.is_current and release.sequence == 2
    assert run.strategy == "change_driven" and run.status == "success"
    assert run.changed_variants == 5 and run.carriers_affected == 7 and run.review_notices == 1
    # RS1: the upgraded HNF1A VUS now raises D2 + D5; the D6 monitoring alert is superseded.
    assert current_findings("DEMO-0008") >= {("D2", "alert", "high"), ("D5", "alert", "low")}
    assert ("D6", "alert", "info") not in current_findings("DEMO-0008")
    # RS2: the downgraded GCK LP loses D1/D5 and gains the D6 monitoring alert.
    assert current_findings("DEMO-0010") == {("D6", "alert", "info")}
    category, message = flashes(admin)[-1]
    assert category == "success" and "is now the current release" in message and f"run #{run.id}" in message
    assert "Run #" in text(admin.get(response.headers["Location"]))


def test_import_and_activate_with_full_recomputation(admin: FlaskClient, db: SQLAlchemy) -> None:
    response = import_release(admin, activate="y", strategy="full")
    run = db.session.scalar(select(ReevaluationRun).order_by(ReevaluationRun.id.desc()))
    assert response.status_code == 302 and run.strategy == "full" and run.trigger == "clinvar_release"
    assert run.patients_reevaluated == 15


def test_import_release_with_expert_panel_overlay(admin: FlaskClient, db: SQLAlchemy) -> None:
    response = import_release(admin, expert_panel_csv=file_field(OVERLAY_EXAMPLE))
    release = db.session.scalar(select(KbRelease).order_by(KbRelease.id.desc()))
    assert response.status_code == 302
    detail = queries.release_detail(release)
    assert detail.overlay_overrides > 0
    assert "from an overlay CSV" in text(admin.get(f"/knowledge/releases/{release.id}"))


def test_import_release_sanitises_the_file_name(admin: FlaskClient, db: SQLAlchemy) -> None:
    import_release(admin, vcf_file=(io.BytesIO(DEMO_RELEASE_2_FILE.read_bytes()), "../../my release.vcf.gz"))
    release = db.session.scalar(select(KbRelease).order_by(KbRelease.id.desc()))
    assert release.file_name == "my_release.vcf.gz"


def test_duplicate_release_file_is_refused(admin: FlaskClient, db: SQLAlchemy) -> None:
    before = db.session.scalar(select(func.count(KbRelease.id)))
    response = import_release(admin, path=DEMO_RELEASE_1_FILE)
    page = text(response)
    assert response.status_code == 400
    assert "This exact file is already stored as" in page
    assert 'href="/knowledge/releases/1"' in page and "(the current release)" in page
    assert db.session.scalar(select(func.count(KbRelease.id))) == before


def test_invalid_vcf_is_refused_with_the_service_message(admin: FlaskClient, db: SQLAlchemy) -> None:
    before = db.session.scalar(select(func.count(KbRelease.id)))
    response = import_release(admin, vcf_file=(io.BytesIO(b"this is not a VCF\n"), "broken.vcf"))
    assert response.status_code == 400
    assert "broken.vcf is not a VCF file" in text(response)
    assert db.session.scalar(select(func.count(KbRelease.id))) == before
    assert audit_count("release_imported") == 1


@pytest.mark.parametrize(
    ("fields", "message"),
    [
        ({"vcf_file": None}, "Choose a ClinVar VCF file to import."),
        ({"vcf_file": (io.BytesIO(b"x"), "release.txt")}, "Choose a ClinVar VCF whose name ends in .vcf or .vcf.gz."),
        ({"release_date": "05/01/2026"}, "Enter a date in YYYY-MM-DD format."),
        ({"expert_panel_csv": (io.BytesIO(b"x"), "overlay.xlsx")}, "Choose an overlay file whose name ends in .csv."),
        ({"label": "x" * 129}, "Field cannot be longer than 128 characters."),
        ({"strategy": "fastest"}, "Not a valid choice."),
    ],
)
def test_import_release_form_validation(admin: FlaskClient, db: SQLAlchemy, fields: dict[str, Any], message: str) -> None:
    response = import_release(admin, **fields)
    assert response.status_code == 400
    assert message in text(response)
    assert db.session.scalar(select(func.count(KbRelease.id))) == 1


def test_invalid_overlay_is_shown_at_the_overlay_field(admin: FlaskClient, db: SQLAlchemy) -> None:
    overlay = (io.BytesIO(b"variation,classification\n1,P\n"), "overlay.csv")
    response = import_release(admin, expert_panel_csv=overlay)
    page = text(response)
    assert response.status_code == 400
    assert "Expert-panel overlay overlay.csv: missing column(s)" in page
    assert re.search(r'id="expert_panel_csv-error">Expert-panel overlay', page)
    assert db.session.scalar(select(func.count(KbRelease.id))) == 1


def test_activate_a_stored_release(admin: FlaskClient, db: SQLAlchemy) -> None:
    import_release(admin, label="R2")
    release = db.session.scalar(select(KbRelease).where(KbRelease.label == "R2"))
    response = admin.post(f"/knowledge/releases/{release.id}/activate", data={"strategy": "change_driven"})
    run = db.session.scalar(select(ReevaluationRun).where(ReevaluationRun.new_release_id == release.id))
    assert response.status_code == 302 and response.headers["Location"].endswith(f"/knowledge/runs/{run.id}")
    assert release.is_current and run.changed_variants == 5
    assert audit_count("release_activated") == 2


def test_activating_the_current_release_does_nothing(admin: FlaskClient, db: SQLAlchemy) -> None:
    response = admin.post("/knowledge/releases/1/activate", data={"strategy": "change_driven"})
    assert response.status_code == 302 and response.headers["Location"].endswith("/knowledge/releases/1")
    assert db.session.scalar(select(func.count(ReevaluationRun.id))) == 1
    assert flashes(admin)[-1] == ("info", "“R1 (2025-01, synthetic)” is already the current release; nothing to do.")


def test_activate_rejects_an_unknown_strategy(admin: FlaskClient, db: SQLAlchemy) -> None:
    import_release(admin, label="R2")
    release = db.session.scalar(select(KbRelease).where(KbRelease.label == "R2"))
    response = admin.post(f"/knowledge/releases/{release.id}/activate", data={"strategy": "fastest"})
    assert response.status_code == 302
    assert not release.is_current
    category, message = flashes(admin)[-1]
    assert category == "danger" and "Not a valid choice" in message


def test_activate_unknown_release_is_404(admin: FlaskClient) -> None:
    assert admin.post("/knowledge/releases/999/activate", data={"strategy": "change_driven"}).status_code == 404


def test_failed_activation_keeps_the_release_inactive(
    admin: FlaskClient, db: SQLAlchemy, monkeypatch: pytest.MonkeyPatch
) -> None:
    import_release(admin, label="R2")
    release = db.session.scalar(select(KbRelease).where(KbRelease.label == "R2"))

    def explode(*args: Any, **kwargs: Any) -> ReevaluationRun:
        raise RuntimeError("database went away")

    monkeypatch.setattr(reevaluation, "activate_release", explode)
    response = admin.post(f"/knowledge/releases/{release.id}/activate", data={"strategy": "change_driven"})
    assert response.status_code == 302 and response.headers["Location"].endswith(f"/knowledge/releases/{release.id}")
    db.session.expire_all()
    assert not db.session.get(KbRelease, release.id).is_current
    category, message = flashes(admin)[-1]
    assert category == "danger" and "Activation of “R2” failed: database went away." in message


def test_compare_previews_the_activation(admin: FlaskClient, db: SQLAlchemy) -> None:
    import_release(admin, label="R2")
    release = db.session.scalar(select(KbRelease).where(KbRelease.label == "R2"))
    page = text(admin.get("/knowledge/releases/compare"))  # default: current → newest pending
    assert "what activating <strong>R2</strong> would apply" in page
    assert f'href="/knowledge/releases/{release.id}#activate"' in page


# ---------------------------------------------------------------------------
# Rule import
# ---------------------------------------------------------------------------


def test_import_rule_set_file_reevaluates_carriers(admin: FlaskClient, db: SQLAlchemy) -> None:
    response = import_rules(admin, rules_file=file_field(RULESET_V2))
    run = db.session.scalar(select(ReevaluationRun).where(ReevaluationRun.trigger == "rule_revision"))
    assert response.status_code == 302 and response.headers["Location"].endswith(f"/knowledge/runs/{run.id}")
    assert [(change["code"], change["old_version"], change["new_version"]) for change in run.rule_change] == [
        ("D4", 1, 2)
    ]
    assert ("D4", "alert", "high") in current_findings("DEMO-0012")  # RS5
    current = {version.code: version.version for version in RuleVersion.current_versions()}
    assert current["D4"] == 2
    category, message = flashes(admin)[-1]
    assert category == "success" and "D4 v1 → v2" in message


def test_import_pasted_rule_set_identical_to_the_current_one(admin: FlaskClient, db: SQLAlchemy) -> None:
    response = import_rules(admin, rules_json=RULESET_V1.read_text())
    run = db.session.scalar(select(ReevaluationRun).where(ReevaluationRun.trigger == "rule_revision"))
    assert response.status_code == 302 and run.changed_variants == 0 and not run.rule_change
    category, message = flashes(admin)[-1]
    assert category == "info" and "no current rule changed" in message


def test_validate_only_previews_without_storing(admin: FlaskClient, db: SQLAlchemy) -> None:
    before = db.session.scalar(select(func.count(RuleVersion.id)))
    response = import_rules(admin, rules_file=file_field(RULESET_V2), submit=None, validate_only="Validate only")
    page = text(response)
    assert response.status_code == 200
    assert "Valid rule set “v2 (2026)”" in page
    assert "Becomes current" in page and "Already current" in page
    assert "1 would become current" in page
    assert '"rule_set": "GE-EHR monogenic diabetes rules"' in page.replace("&#34;", '"')  # moved to the text box
    assert db.session.scalar(select(func.count(RuleVersion.id))) == before
    assert db.session.scalar(select(func.count(ReevaluationRun.id))) == 1


def test_invalid_json_is_reported_with_its_position(admin: FlaskClient, db: SQLAlchemy) -> None:
    response = import_rules(admin, rules_json='{"rules": [}')
    assert response.status_code == 400
    assert re.search(r"pasted JSON: Rule set is not valid JSON: .* \(line 1, column \d+\)\.", text(response))
    assert db.session.scalar(select(func.count(ReevaluationRun.id))) == 1


def test_invalid_rule_definition_is_reported_precisely(admin: FlaskClient, db: SQLAlchemy) -> None:
    doc = json.loads(RULESET_V2.read_text())
    del doc["rules"][1]["severity"]
    response = import_rules(admin, rules_file=(io.BytesIO(json.dumps(doc).encode()), "broken.json"))
    page = text(response)
    assert response.status_code == 400
    assert "broken.json:" in page and "severity" in page
    assert 'id="rules_file-error"' in page


def test_changed_definition_of_a_stored_version_is_refused(admin: FlaskClient, db: SQLAlchemy) -> None:
    doc = json.loads(RULESET_V1.read_text())
    doc["rules"][0]["recommendation"] = "Something else entirely."
    response = import_rules(admin, rules_json=json.dumps(doc))
    assert response.status_code == 400
    assert "D1 v1" in text(response)
    assert db.session.scalar(select(func.count(ReevaluationRun.id))) == 1  # input error: no failed run


@pytest.mark.parametrize(
    ("fields", "message"),
    [
        ({}, "Choose a rule-set JSON file or paste the JSON below."),
        ({"rules_file": (io.BytesIO(b"{}"), "rules.json"), "rules_json": "{}"}, "Provide either a file or pasted JSON, not both."),
        ({"rules_file": (io.BytesIO(b"{}"), "rules.txt")}, "Choose a rule-set file whose name ends in .json."),
        ({"rules_file": (io.BytesIO("{}".encode("utf-16")), "rules.json")}, "Rule set file is not UTF-8 encoded text."),
    ],
)
def test_rule_import_form_validation(admin: FlaskClient, fields: dict[str, Any], message: str) -> None:
    response = import_rules(admin, **fields)
    assert response.status_code == 400
    assert message in text(response)


def test_failed_rule_import_is_reported(admin: FlaskClient, db: SQLAlchemy, monkeypatch: pytest.MonkeyPatch) -> None:
    def explode(*args: Any, **kwargs: Any) -> ReevaluationRun:
        raise RuntimeError("worker crashed")

    monkeypatch.setattr(reevaluation, "apply_rule_set", explode)
    response = import_rules(admin, rules_file=file_field(RULESET_V2))
    assert response.status_code == 302 and response.headers["Location"].endswith("/knowledge/runs")
    category, message = flashes(admin)[-1]
    assert category == "danger" and "Importing the rule set failed: worker crashed." in message
    assert {version.code: version.version for version in RuleVersion.current_versions()}["D4"] == 1


# ---------------------------------------------------------------------------
# Full recomputation
# ---------------------------------------------------------------------------


def test_full_recompute_changes_nothing_in_a_consistent_database(admin: FlaskClient, db: SQLAlchemy) -> None:
    alerts_before = db.session.scalar(select(func.count(Alert.id)))
    response = admin.post("/knowledge/recompute")
    run = db.session.scalar(select(ReevaluationRun).where(ReevaluationRun.trigger == "full_recompute"))
    assert response.status_code == 302 and response.headers["Location"].endswith(f"/knowledge/runs/{run.id}")
    assert run.strategy == "full" and run.status == "success" and run.patients_reevaluated == 15
    assert run.alerts_created == 0 and run.alerts_superseded == 0
    assert db.session.scalar(select(func.count(Alert.id))) == alerts_before
    category, message = flashes(admin)[-1]
    assert category == "success" and message.startswith("Full recomputation complete.")
    page = text(admin.get(response.headers["Location"]))
    assert "Every patient re-evaluated under R1 (2025-01, synthetic)" in page
    assert "No patient&#39;s findings changed" in page and "By rule" not in page


def test_failed_full_recompute_is_reported(admin: FlaskClient, monkeypatch: pytest.MonkeyPatch) -> None:
    def explode(*args: Any, **kwargs: Any) -> ReevaluationRun:
        raise RuntimeError("out of memory")

    monkeypatch.setattr(reevaluation, "full_recompute", explode)
    response = admin.post("/knowledge/recompute")
    assert response.status_code == 302 and response.headers["Location"].endswith("/knowledge/runs")
    category, message = flashes(admin)[-1]
    assert category == "danger" and "Full recomputation failed: out of memory." in message


def test_recompute_is_post_only(admin: FlaskClient) -> None:
    assert admin.get("/knowledge/recompute").status_code == 405


# ---------------------------------------------------------------------------
# CSRF (the testing configuration disables it; this app enforces it)
# ---------------------------------------------------------------------------


@pytest.fixture
def csrf_admin(seed_snapshot: sqlite3.Connection) -> Iterator[FlaskClient]:
    """The administrator signed in to a copy of the plain demo seed served by an app enforcing CSRF."""
    application = build_app({"WTF_CSRF_ENABLED": True})
    with application.app_context():
        restore(seed_snapshot)
        try:
            client = application.test_client()
            token = csrf_token(client.get("/login"))
            response = client.post("/login", data={"username": "admin", "password": DEMO_PASSWORD, "csrf_token": token})
            assert response.status_code == 302
            yield client
        finally:
            _db.session.remove()
            _db.drop_all()
            _db.engine.dispose()


def test_knowledge_posts_require_a_csrf_token(csrf_admin: FlaskClient) -> None:
    assert csrf_admin.post("/knowledge/recompute").status_code == 400
    response = csrf_admin.post(
        "/knowledge/rules/import",
        data={"rules_json": RULESET_V2.read_text(), "strategy": "change_driven", "submit": "Import"},
    )
    assert response.status_code == 400
    assert _db.session.scalar(select(func.count(ReevaluationRun.id))) == 1


def test_knowledge_forms_work_with_their_csrf_token(csrf_admin: FlaskClient) -> None:
    token = csrf_token(csrf_admin.get("/knowledge/runs"))
    assert csrf_admin.post("/knowledge/recompute", data={"csrf_token": token}).status_code == 302
    token = csrf_token(csrf_admin.get("/knowledge/rules/import"))
    response = csrf_admin.post(
        "/knowledge/rules/import",
        data={"csrf_token": token, "rules_json": RULESET_V2.read_text(), "strategy": "change_driven", "submit": "Import"},
    )
    assert response.status_code == 302
    assert {version.code: version.version for version in RuleVersion.current_versions()}["D4"] == 2


# ---------------------------------------------------------------------------
# Empty database
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("/knowledge/releases", "No knowledge-base release yet"),
        ("/knowledge/releases/compare", "At least two releases are needed"),
        ("/knowledge/rules", "No rules loaded"),
        ("/knowledge/runs", "No re-evaluation run yet"),
        ("/knowledge/releases/import", "None — the first activation classifies every stored variant"),
        ("/knowledge/rules/import", "Import a rule set"),
    ],
)
def test_pages_render_on_an_empty_database(admin_client: FlaskClient, path: str, expected: str) -> None:
    response = admin_client.get(path)
    assert response.status_code == 200
    assert expected in text(response)


def test_empty_state_for_clinicians_does_not_offer_imports(clinician_client: FlaskClient) -> None:
    page = text(clinician_client.get("/knowledge/releases"))
    assert "An administrator imports ClinVar releases." in page
    assert "/knowledge/releases/import" not in page.split("<main", 1)[1].split("</main>", 1)[0]
