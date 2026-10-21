"""Route tests of the main blueprint (UI-B): the role-aware dashboard and the about page.

The clinician/administrator dashboard is checked against the full demo (rules v1 → v2, releases
R1 → R2, SPEC §11): its KPIs, the "needs attention" list (reclassification alerts by severity,
open review notices), the current knowledge base and rule versions, recent runs and alerts per
rule. Lab technicians get a laboratory view without any alert information (SPEC §8.1).
"""

from __future__ import annotations

import re
from datetime import date

import pytest
from flask import Flask
from flask.testing import FlaskClient
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import distinct, func, select

from ge_ehr import constants as C
from ge_ehr.main import queries
from ge_ehr.models import Alert, GenomicUpload, KbRelease, Patient, PatientVariant, RuleVersion
from ge_ehr.services import patient_records
from ge_ehr.services.vcf_parser import VcfError
from tests.demo_fixtures import (  # noqa: F401  (fixtures)
    admin,
    clinician,
    count_queries,
    demo_admin,
    demo_app,
    demo_clinician,
    demo_lab,
    lab,
    seed_snapshot,
    seeded,
    text,
)
from tests.factories import make_alert, make_run


def main_content(page: str) -> str:
    """The page's <main> element (without the navbar and footer)."""
    return page.split('<main id="main-content"', 1)[1].split("</main>", 1)[0]


def patient(db: SQLAlchemy, mrn: str) -> Patient:
    return db.session.scalar(select(Patient).where(Patient.mrn == mrn))


# ---------------------------------------------------------------------------
# Access
# ---------------------------------------------------------------------------


def test_dashboard_requires_sign_in(client: FlaskClient) -> None:
    response = client.get("/")
    assert response.status_code == 302 and "/login" in response.headers["Location"]


def test_about_page_is_public(client: FlaskClient) -> None:
    response = client.get("/about")
    assert response.status_code == 200
    assert "GE-EHR for monogenic diabetes" in text(response)


@pytest.mark.parametrize("fixture", ["admin_client", "clinician_client", "lab_client"])
def test_every_role_has_a_dashboard_and_the_about_page(request: pytest.FixtureRequest, fixture: str) -> None:
    client: FlaskClient = request.getfixturevalue(fixture)
    assert client.get("/").status_code == 200
    assert client.get("/about").status_code == 200


# ---------------------------------------------------------------------------
# Clinical dashboard (clinicians and administrators)
# ---------------------------------------------------------------------------


def test_clinical_dashboard_kpis(demo_app: Flask, demo_clinician: FlaskClient) -> None:
    from ge_ehr.extensions import db

    data = queries.clinical_dashboard()
    carriers = db.session.scalar(
        select(func.count(distinct(PatientVariant.patient_id))).where(
            PatientVariant.current_classification.in_(sorted(C.ACTIONABLE))
        )
    )
    assert data.patients == 15
    assert data.actionable_carriers == carriers
    assert data.alerts_by_severity == {"high": 4, "medium": 4, "low": 12, "info": 1}
    assert data.current_alerts == 21
    assert data.open_notices == 1
    assert data.last_release_run.id == 3 and data.reclassified_in_last_run == 7
    assert data.release.label == "R2 (2026-01, synthetic)"
    assert [version.label for version in data.rule_versions] == ["D1 v1", "D2 v1", "D3 v1", "D4 v2", "D5 v1", "D6 v1"]

    page = main_content(text(demo_clinician.get("/")))
    for label in ("Patients", "Actionable carriers", "Current alerts", "Open review notices", "Reclassification alerts"):
        assert label in page
    assert "4 high" in page and "12 low" in page and "1 info" in page
    assert "raised by run #3, the latest release activation" in page


def test_needs_attention_lists_reclassification_alerts_by_severity(demo_app: Flask, demo_clinician: FlaskClient) -> None:
    data = queries.clinical_dashboard()
    severities = [alert.severity for alert in data.attention_alerts]
    assert severities == sorted(severities, key=lambda severity: -C.SEVERITY_ORDER[severity])
    first = data.attention_alerts[0]
    assert (first.rule_code, first.severity, first.patient.mrn) == ("D2", "high", "DEMO-0008")  # RS1
    assert all(alert.is_reclassified and alert.priority == "high" for alert in data.attention_alerts)
    assert data.attention_alerts_total == 7 and data.attention_alerts_hidden == 1
    assert [notice.patient.mrn for notice in data.attention_notices] == ["DEMO-0010"]  # RS2

    page = main_content(text(demo_clinician.get("/")))
    assert page.index("D2 · HNF1A diabetes on insulin or metformin") < page.index("D5 · Monogenic diabetes variant")
    assert f'href="/alerts/{first.id}"' in page
    assert f'href="/patients/{first.patient_id}/recommendations"' in page
    assert "Review notice · <span class=\"gene-symbol\">GCK</span> variant reclassified" in page
    assert "(1 more high priority)" in page


def test_needs_attention_lists_only_unreviewed_reclassification_alerts(seeded: Flask, db: SQLAlchemy) -> None:
    carrier = patient(db, "DEMO-0001")
    alert = db.session.scalar(select(Alert).where(Alert.patient_id == carrier.id, Alert.rule_code == "D2"))
    assert queries.clinical_dashboard().attention_alerts == []  # R1 only: nothing was reclassified

    alert.priority = "high"  # high priority alone is not a reclassification
    db.session.commit()
    assert queries.clinical_dashboard().attention_alerts == []

    alert.is_reclassified = True
    db.session.commit()
    assert queries.clinical_dashboard().attention_alerts == [alert]

    alert.status = "reviewed"  # a reviewed alert no longer needs attention
    db.session.commit()
    assert queries.clinical_dashboard().attention_alerts == []


def test_dashboard_knowledge_base_runs_and_rule_bars(demo_app: Flask, demo_clinician: FlaskClient) -> None:
    page = main_content(text(demo_clinician.get("/")))
    assert 'href="/knowledge/releases/2"' in page and "R2 (2026-01, synthetic)" in page
    assert "badge-current" in page and "badge-source-synthetic" in page
    assert "rule set v1 (2025), v2 (2026)" in page
    assert 'href="/knowledge/rules#rule-D4"' in page and ">D4 v2<" in page
    for run_id in (1, 2, 3):
        assert f'href="/knowledge/runs/{run_id}"' in page
    assert 'Initial classification · <span class="text-nowrap">R1 (2025-01, synthetic)</span>' in page
    assert '<span class="text-nowrap">R1 (2025-01, synthetic)</span> <span class="text-nowrap">→ R2 (2026-01, synthetic)</span>' in page
    assert "D4 v1 → v2" in page
    bars = {bar.code: bar for bar in queries.clinical_dashboard().rule_bars}
    assert list(bars) == ["D1", "D2", "D3", "D4", "D5", "D6"]
    assert bars["D5"].count == 12 and bars["D5"].percent == 100
    assert bars["D2"].count == 2 and bars["D2"].severity == "high"
    assert 'href="/alerts?rule=D5"' in page


def test_clinicians_and_administrators_get_their_own_actions(demo_clinician: FlaskClient, demo_admin: FlaskClient) -> None:
    clinician_page = main_content(text(demo_clinician.get("/")))
    admin_page = main_content(text(demo_admin.get("/")))
    assert 'href="/alerts"' in clinician_page and "/knowledge/releases/import" not in clinician_page
    assert 'href="/knowledge/releases/import"' in admin_page and 'href="/knowledge/rules/import"' in admin_page


def test_clinical_dashboard_queries_do_not_grow_with_the_data(seeded: Flask, db: SQLAlchemy) -> None:
    db.session.expire_all()
    with count_queries(db) as before:
        queries.clinical_dashboard()
    release = db.session.get(KbRelease, 1)
    rule = db.session.scalar(select(RuleVersion).where(RuleVersion.code == "D5", RuleVersion.is_current.is_(True)))
    background = db.session.scalars(select(PatientVariant).where(PatientVariant.current_classification.in_(("B", "LB"))))
    for variant in background:  # variants without findings, so the new alerts are the only current ones
        make_alert(variant, rule, release=release, priority="high", is_reclassified=True)
    for _ in range(4):
        make_run("full_recompute", status="success", strategy="full", old_release=release, new_release=release)
    db.session.commit()
    db.session.expire_all()
    with count_queries(db) as after:
        data = queries.clinical_dashboard()
    assert len(data.attention_alerts) >= 4 and len(data.recent_runs) == queries.RECENT_RUNS
    assert len(after) == len(before)


# ---------------------------------------------------------------------------
# Laboratory dashboard
# ---------------------------------------------------------------------------


def test_lab_dashboard_shows_no_alert_information(demo_lab: FlaskClient) -> None:
    page = text(demo_lab.get("/"))
    content = main_content(page)
    assert "Laboratory overview" in content
    for clinical in ("Current alerts", "Needs attention", "Reclassification alerts", "Open review notices", "/alerts",
                     "/notices", "/recommendations", "/knowledge/"):
        assert clinical not in page, clinical
    assert "Recommendations, alerts and review notices are visible to clinicians only." in content


def test_lab_dashboard_counts_and_recent_uploads(demo_app: Flask, demo_lab: FlaskClient) -> None:
    data = queries.lab_dashboard()
    assert (data.patients, data.awaiting_vcf, data.uploads_accepted, data.uploads_rejected) == (15, 0, 15, 0)
    assert len(data.recent_uploads) == queries.RECENT_UPLOADS
    page = main_content(text(demo_lab.get("/")))
    newest = data.recent_uploads[0]
    assert f'href="/patients/{newest.patient_id}"' in page and newest.filename in page
    assert "Every patient has an accepted VCF." in page


def test_lab_dashboard_lists_rejected_uploads_and_patients_awaiting_a_vcf(
    seeded: Flask, db: SQLAlchemy, lab: FlaskClient
) -> None:
    newcomer = patient_records.create_patient(
        mrn="NEW-0001", given_name="Noor", family_name="Haddad", sex="female", birth_date=date(1990, 3, 4)
    )
    db.session.commit()
    with pytest.raises(VcfError):
        patient_records.ingest_vcf(newcomer, b"##fileformat=VCFv4.2\nnot a header\n", "broken.vcf")
    db.session.commit()
    data = queries.lab_dashboard()
    assert (data.awaiting_vcf, data.uploads_rejected) == (1, 1)
    assert [item.mrn for item in data.awaiting_patients] == ["NEW-0001"]
    page = main_content(text(lab.get("/")))
    assert f'href="/patients/{newcomer.id}/vcf"' in page and "Upload VCF for Noor Haddad" in page
    upload = db.session.scalar(select(GenomicUpload).where(GenomicUpload.status == "rejected"))
    assert data.recent_uploads[0] == upload
    assert "broken.vcf" in page and "Line 2: A variant record appears before the" in page


def test_lab_dashboard_queries_do_not_grow_with_the_uploads(seeded: Flask, db: SQLAlchemy) -> None:
    db.session.expire_all()
    with count_queries(db) as before:
        queries.lab_dashboard()
    for number in range(3):
        newcomer = patient_records.create_patient(
            mrn=f"NEW-{number}", given_name="New", family_name=f"Patient {number}", sex="male", birth_date=date(1985, 1, 1)
        )
        db.session.add(GenomicUpload(patient=newcomer, filename="x.vcf", sha256="0" * 64, size_bytes=1, status="rejected",
                                     error="Line 1: not a VCF.", total_records=0, panel_variants=0, off_panel_skipped=0,
                                     non_pass_skipped=0, ref_calls_skipped=0))
    db.session.commit()
    db.session.expire_all()
    with count_queries(db) as after:
        queries.lab_dashboard()
    assert len(after) == len(before)


# ---------------------------------------------------------------------------
# Empty database
# ---------------------------------------------------------------------------


def test_clinical_dashboard_on_an_empty_database(admin_client: FlaskClient) -> None:
    page = main_content(text(admin_client.get("/")))
    assert "Nothing needs attention" in page
    assert "No release is active" in page and 'href="/knowledge/releases/import"' in page
    assert "No rules loaded" in page
    assert "No re-evaluation run yet" in page
    assert "no release activated yet" in page


def test_clinician_dashboard_on_an_empty_database_offers_no_imports(clinician_client: FlaskClient) -> None:
    page = main_content(text(clinician_client.get("/")))
    assert "No release is active" in page and "/knowledge/releases/import" not in page


def test_lab_dashboard_on_an_empty_database(lab_client: FlaskClient) -> None:
    page = main_content(text(lab_client.get("/")))
    assert "No VCF uploaded yet" in page and "Every patient has an accepted VCF." in page


# ---------------------------------------------------------------------------
# About
# ---------------------------------------------------------------------------


def test_about_describes_modules_genes_rules_and_references(client: FlaskClient) -> None:
    page = main_content(text(client.get("/about")))
    for module in ("F1", "F2", "F3", "F4", "F5"):
        assert f'<span class="module-code">{module}</span>' in page
    for info in C.GENE_INFO.values():
        assert info.symbol in page and info.hgnc_id in page and info.transcript in page
    for code in ("D1", "D2", "D3", "D4", "D5", "D6"):
        assert f'<th scope="row">{code}</th>' in page
    assert "Change-driven re-evaluation" in page and "only for variant keys stored for some patient" in page
    references = re.search(r'<ol class="reference-list">(.*?)</ol>', page, re.S).group(1)
    assert references.count("<li>") == 14
    assert "Monogenic diabetes mellitus and clinical implications of genetic diagnosis" in references
    assert "Synthea" in references


def test_about_states_the_disclaimer_and_limitations(client: FlaskClient) -> None:
    page = main_content(text(client.get("/about")))
    assert "Synthetic data only." in page
    assert "Advisory — for specialist review." in page
    assert "they must be reviewed by diabetes specialists before any clinical use" in page
    assert "GE-EHR never changes or reverses therapy itself." in page
    assert "Limitations" in page and "no left-normalisation" in page


@pytest.mark.parametrize(("fixture", "linked"), [("client", False), ("lab_client", False), ("clinician_client", True)])
def test_about_links_to_the_rules_only_for_clinical_roles(request: pytest.FixtureRequest, fixture: str, linked: bool) -> None:
    client: FlaskClient = request.getfixturevalue(fixture)
    page = main_content(text(client.get("/about")))
    assert ('href="/knowledge/rules"' in page) is linked
