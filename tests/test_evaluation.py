"""Tests of the evaluation harness (Eval WP, SPEC §10).

Unit tests cover the synthetic knowledge base and change sets (``evaluation.knowledge``), the
cohort plans and databases (``evaluation.strategies``), the metrics (``evaluation.metrics``) and
the runner (``evaluation.run``). One module-scoped quick evaluation (30 + 300 patients, a
1,500-record knowledge base, about 7 s) checks the qualitative outcomes the paper expects in
Table VI: alert-once never re-alerts and keeps every affected alert; full recomputation and
change-driven re-evaluation reach the ground truth with precise notifications, and the
change-driven strategy examines fewer records.
"""

from __future__ import annotations

import gzip
import json
from dataclasses import replace
from datetime import date, datetime
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import func, select

from evaluation import knowledge as K
from evaluation import metrics as M
from evaluation import run as R
from evaluation import strategies as S
from evaluation.cohort import CohortMedication, CohortPatient, generate_cohort, load_compact_cohort
from ge_ehr import constants as C
from ge_ehr.extensions import db
from ge_ehr.models import KbRelease, ReevaluationRun
from ge_ehr.services import clinvar
from ge_ehr.services.clinvar import ClinVarRecord, write_clinvar_vcf

SEED = 2026
SMALL_KB = 1_500
COHORT_DIR = Path(__file__).resolve().parents[1] / "data" / "cohorts"


@pytest.fixture(scope="module")
def base() -> list[ClinVarRecord]:
    return K.generate_base_release(SEED, total=SMALL_KB)


@pytest.fixture(scope="module")
def classes(base: list[ClinVarRecord]) -> dict[K.Key, K.KnownRecord]:
    return K.classify_records(base)


@pytest.fixture(scope="module")
def background(classes: dict[K.Key, K.KnownRecord]) -> S.BackgroundPool:
    return S.choose_background_pool(classes, SEED)


@pytest.fixture(scope="module")
def correctness_cohort() -> list[CohortPatient]:
    return load_compact_cohort(COHORT_DIR / R.CORRECTNESS_FILE)


@pytest.fixture(scope="module")
def correctness_plan(correctness_cohort, base, background) -> S.CohortPlan:
    return S.plan_correctness_cohort(correctness_cohort, base, background, seed=SEED)


def _scale_plan(cohort, base, classes, background, *, exclude=frozenset(), seed=SEED) -> S.CohortPlan:
    pools = S.synthetic_role_pools(base, set(exclude) | set(background.keys))
    return S.plan_scale_cohort(
        cohort,
        pools,
        background,
        seed=seed,
        role_counts=S.scaled_role_counts(S.SCALE_ROLE_COUNTS, len(cohort)),
        activating_keys=frozenset(key for key, known in classes.items() if known.mechanism == "activating"),
        taken=set(classes) | set(exclude),
    )


@pytest.fixture(scope="module")
def scale_plan(base, classes, background, correctness_plan) -> S.CohortPlan:
    cohort = generate_cohort(1_000, 11)
    return _scale_plan(cohort, base, classes, background, exclude=correctness_plan.selected_keys)


# ---------------------------------------------------------------------------
# Synthetic base release
# ---------------------------------------------------------------------------


def test_base_release_is_deterministic_with_realistic_gene_sizes_and_classes() -> None:
    records = K.generate_base_release(SEED)
    assert records == K.generate_base_release(SEED)
    assert records != K.generate_base_release(SEED + 1)
    summary = K.release_summary(records)
    assert summary["records"] == len(records) == 9_000
    genes = summary["genes"]
    assert max(genes, key=genes.get) == "ABCC8" and min(genes, key=genes.get) == "INS"
    classes = summary["classes"]
    assert max(classes, key=classes.get) == "VUS"
    assert all(classes[cls] > 0 for cls in C.CLASSES)
    raw = summary["raw_significance"]
    assert (
        raw["Conflicting_classifications_of_pathogenicity"] > 0
        and raw["Conflicting_interpretations_of_pathogenicity"] > 0
    )
    expert = summary["expert_panel_by_gene"]
    assert all(expert[gene] > 0 for gene in K.EXPERT_PANEL_GENES)
    assert all(expert[gene] == 0 for gene in set(C.GENES) - K.EXPERT_PANEL_GENES)
    assert all(count > 0 for count in summary["katp_mechanisms"].values())


def test_base_release_records_are_unique_panel_variants_with_synthetic_ids(base: list[ClinVarRecord]) -> None:
    keys = [K.record_key(record) for record in base]
    assert len(set(keys)) == len(keys)
    for record in base:
        assert C.gene_for_position(record.chrom, record.pos) == record.gene
        assert record.variation_id >= K.FIRST_VARIATION_ID and record.allele_id >= K.FIRST_ALLELE_ID
        assert record.clnhgvs.startswith(C.GENE_INFO[record.gene].refseq_chrom + ":g.")
    katp_conditions = {record.clndn for record in base if record.gene in K.KATP_GENES}
    assert any("neonatal_diabetes" in text for text in katp_conditions)
    assert any("Hyperinsulinemic" in text for text in katp_conditions)


def test_gene_sizes_scale_and_reject_tiny_releases() -> None:
    sizes = K.gene_sizes(100)
    assert sum(sizes.values()) == 100 and all(size >= 1 for size in sizes.values())
    assert K.gene_sizes(9_000) == {gene: K.GENE_RECORD_SHARES[gene] for gene in C.GENES}
    with pytest.raises(ValueError, match="at least 7 records"):
        K.gene_sizes(6)


def test_written_release_has_synthetic_headers_and_round_trips(tmp_path: Path, base: list[ClinVarRecord]) -> None:
    path = tmp_path / "k_old.vcf.gz"
    write_clinvar_vcf(base, path, file_date="2024-01-07", synthetic=True)
    with gzip.open(path, "rt", encoding="utf-8") as handle:
        header = [line.rstrip("\n") for line in handle if line.startswith("##")]
    assert all(line in header for line in C.SYNTHETIC_HEADER_LINES)
    meta = clinvar.read_vcf_header_meta(path)
    assert meta["ge_ehr_synthetic"] is True and meta["fileDate"] == "2024-01-07"
    assert K.classify_records(K.read_release(path)) == K.classify_records(base)


def test_pure_classification_matches_the_database_import(app, tmp_path: Path, base: list[ClinVarRecord]) -> None:
    path = tmp_path / "k_old.vcf.gz"
    write_clinvar_vcf(base, path, file_date="2024-01-07")
    release = clinvar.import_release(path)
    summary = K.release_summary(base)
    assert release.is_synthetic and release.source == "synthetic"
    assert release.class_counts == summary["classes"]
    assert release.gene_counts == summary["genes"]


# ---------------------------------------------------------------------------
# Change sets
# ---------------------------------------------------------------------------


def _first_eligible(base: list[ClinVarRecord], kind: str, skip: set[K.Key] = frozenset()) -> K.Key:
    return next(
        K.record_key(record) for record in base if K.is_eligible(kind, record) and K.record_key(record) not in skip
    )


def test_forced_changes_cover_every_kind_with_the_expected_category(base: list[ClinVarRecord]) -> None:
    forced: dict[K.Key, str] = {}
    for kind in K.CHANGE_KINDS:
        if kind == "nc_to_lp":
            forced[K.novel_keys("HNF1A", 1, __import__("random").Random(1), {K.record_key(r) for r in base})[0]] = kind
        else:
            forced[_first_eligible(base, kind, set(forced))] = kind
    spec = K.ChangeSetSpec(forced=forced, carried_keys=frozenset(forced))
    new, truth = K.apply_change_set(base, spec, SEED)
    by_kind = {change.kind: change for change in truth}
    assert set(by_kind) == set(K.CHANGE_KINDS) and all(change.carried for change in truth)
    for kind, change in by_kind.items():
        expected = None if kind == "vus_to_conflicting" else K.CHANGE_KINDS[kind].group
        assert change.category == expected, kind
    assert by_kind["katp_activating"].new_mechanism == "activating" != by_kind["katp_activating"].old_mechanism
    assert by_kind["vus_to_conflicting"].new_class == "VUS" and by_kind["vus_to_conflicting"].new_clnsig.startswith(
        "Conflicting"
    )
    new_classes = K.classify_records(new)
    assert by_kind["p_removed"].key not in new_classes
    assert new_classes[by_kind["nc_to_lp"].key].classification == "LP"
    assert new_classes[by_kind["lp_to_p"].key].classification == "P"
    assert len(new) == len(base)  # one removed, one added


def test_uncarried_changes_and_additions_avoid_carried_keys(base: list[ClinVarRecord]) -> None:
    carried = frozenset(K.record_key(record) for record in base[::3])
    snapshot = list(base)
    spec = K.ChangeSetSpec(
        uncarried={"vus_to_lp": 5, "lb_to_b": 4, "nc_to_lp": 2}, carried_keys=carried, added_uncarried=6
    )
    new, truth = K.apply_change_set(base, spec, SEED)
    assert base == snapshot  # input untouched
    assert all(not change.carried and change.key not in carried for change in truth)
    counts = K.summarize_changes(truth)["kinds"]
    assert counts["vus_to_lp"] == {"label": "VUS → LP", "group": "upgrade", "carried": 0, "uncarried": 5}
    assert counts["added"]["uncarried"] == 6 and counts["nc_to_lp"]["uncarried"] == 2
    assert len(new) == len(base) + 8
    assert K.apply_change_set(base, spec, SEED) == (new, truth)


@pytest.mark.parametrize(
    ("spec_args", "message"),
    [
        ({"uncarried": {"bogus": 1}}, "Unknown change kind"),
        ({"uncarried": {"vus_to_lp": -1}}, "must be >= 0"),
        ({"added_uncarried": -2}, "must be >= 0"),
        ({"uncarried": {"katp_activating": 100_000}}, "only"),
        ({"forced": {("1", 1_000_000, "A", "G"): "vus_to_lp"}}, "outside the gene panel"),
    ],
)
def test_invalid_change_sets_are_rejected(base: list[ClinVarRecord], spec_args: dict[str, Any], message: str) -> None:
    with pytest.raises(ValueError, match=message):
        K.apply_change_set(base, K.ChangeSetSpec(**spec_args), SEED)


def test_ineligible_forced_changes_are_rejected(base: list[ClinVarRecord]) -> None:
    vus = _first_eligible(base, "vus_to_lp")
    with pytest.raises(ValueError, match="cannot be applied.*classified VUS"):
        K.apply_change_set(base, K.ChangeSetSpec(forced={vus: "lp_to_p"}), SEED)
    with pytest.raises(ValueError, match="cannot be applied"):
        K.apply_change_set(base, K.ChangeSetSpec(forced={vus: "nc_to_lp"}), SEED)
    with pytest.raises(ValueError, match="more than once"):
        K.apply_change_set([*base, base[0]], K.ChangeSetSpec(), SEED)


def test_change_category_matches_the_engine_rules() -> None:
    assert K.change_category("VUS", "LP", "unknown", "unknown") == "upgrade"
    assert K.change_category("P", "NC", "unknown", "unknown") == "downgrade"
    assert K.change_category("LP", "LP", "inactivating", "activating") == "within_actionable"
    assert K.change_category("VUS", "VUS", "unknown", "activating") == "within_non_actionable"
    assert K.change_category("VUS", "VUS", "unknown", "unknown") is None


def test_transitions_and_natural_changes(classes: dict[K.Key, K.KnownRecord]) -> None:
    keys = sorted(classes)
    removed, changed = keys[0], keys[1]
    new = dict(classes)
    del new[removed]
    new[changed] = replace(classes[changed], classification="P" if classes[changed].classification != "P" else "B")
    assert set(K.transitions(classes, new)) == {removed, changed}
    natural = {change.key: change for change in K.natural_changes(classes, new, {changed})}
    assert natural[changed].carried and not natural[removed].carried
    assert natural[removed].new_class == "NC"
    assert natural[changed].kind == natural[changed].category


# ---------------------------------------------------------------------------
# Real release pairs
# ---------------------------------------------------------------------------


def _write_subset(directory: Path, day: str, records: list[ClinVarRecord]) -> Path:
    path = directory / f"clinvar_{day.replace('-', '')}_7genes.vcf.gz"
    write_clinvar_vcf(records, path, file_date=day, synthetic=True)
    return path


def test_load_real_pair_needs_two_subsets(tmp_path: Path, base: list[ClinVarRecord]) -> None:
    assert K.load_real_pair(tmp_path / "missing") is None
    assert K.load_real_pair(tmp_path) is None
    _write_subset(tmp_path, "2023-01-07", base[:50])
    (tmp_path / "notes.txt").write_text("not a subset", encoding="utf-8")
    assert K.load_real_pair(tmp_path, allow_synthetic=True) is None


def test_load_real_pair_refuses_synthetic_files_and_picks_oldest_and_newest(tmp_path: Path, base) -> None:
    oldest = _write_subset(tmp_path, "2021-03-01", base[:50])
    _write_subset(tmp_path, "2023-01-07", base[:60])
    newest = _write_subset(tmp_path, "2025-06-02", base[:70])
    with pytest.raises(ValueError, match="synthetic file"):
        K.load_real_pair(tmp_path)
    pair = K.load_real_pair(tmp_path, allow_synthetic=True)
    assert (pair.old_path, pair.new_path) == (oldest, newest)
    assert (pair.old_date, pair.new_date) == (date(2021, 3, 1), date(2025, 6, 2))


def test_load_real_pair_rejects_duplicate_dates(tmp_path: Path, base) -> None:
    _write_subset(tmp_path, "2023-01-07", base[:50])
    write_clinvar_vcf(base[:40], tmp_path / "clinvar_20230108_7genes.vcf.gz", file_date="2023-01-07")
    with pytest.raises(ValueError, match="same fileDate"):
        K.load_real_pair(tmp_path, allow_synthetic=True)


# ---------------------------------------------------------------------------
# Cohort plans
# ---------------------------------------------------------------------------


def test_background_pool_holds_the_scenario_keys(classes, background: S.BackgroundPool) -> None:
    assert len(background.keys) == S.BACKGROUND_POOL_SIZE == len(set(background.keys))
    for name, gene, cls in S.REQUIRED_BACKGROUND:
        known = classes[background.scenario_keys[name]]
        assert (known.gene, known.classification) == (gene, cls)
    assert classes[background.changed_key].classification == "LB"
    assert background.changed_key not in background.scenario_keys.values()
    assert S.choose_background_pool(classes, SEED) == background
    with pytest.raises(ValueError, match="no HNF1A B record"):
        S.choose_background_pool(classes, SEED, eligible=lambda key: classes[key].gene != "HNF1A")


def test_correctness_plan_maps_every_table_v_scenario(correctness_plan: S.CohortPlan, classes) -> None:
    scenarios = [planned.scenario for planned in correctness_plan.patients if planned.scenario]
    assert sorted(set(scenarios) - {"RS6"}) == sorted(scenario.code for scenario in S.SCENARIOS)
    assert scenarios.count("RS6") >= 2
    keys = correctness_plan.scenario_keys
    for spec in S.SCENARIO_KEYS:
        known = classes[keys[spec.name]]
        assert (known.gene, known.classification) == (spec.gene, spec.cls), spec.name
        if spec.mechanism:
            assert known.mechanism == spec.mechanism
    assert keys["V11"] not in correctness_plan.carried_keys  # RS7: carried by nobody
    assert correctness_plan.forced_changes[keys["V11"]] == "vus_to_p"
    assert correctness_plan.forced_changes[keys["V03"]] == "lp_to_p"
    s9 = correctness_plan.scenario_patients("S9")[0]
    assert S.OFF_PANEL_KEY in {variant[:4] for variant in s9.variants}
    assert S.OFF_PANEL_KEY not in s9.panel_keys
    s6 = correctness_plan.scenario_patients("S6")[0].patient
    assert (s6.diabetes_type, s6.age_at_diagnosis_months) == ("neonatal", 2)
    assert {medication.drug_class for medication in s6.medications} == {"insulin"}
    s2 = correctness_plan.scenario_patients("S2")[0].patient
    assert {medication.drug_class for medication in s2.medications} == {"sulfonylurea"}
    family = {planned.patient.family_id for planned in correctness_plan.scenario_patients("RS6")}
    assert len(family) == 1 and None not in family


def test_correctness_plan_needs_enough_patients(correctness_cohort, base, background) -> None:
    with pytest.raises(ValueError, match="at least 12 patients outside families"):
        S.plan_correctness_cohort(correctness_cohort[:14], base, background, seed=SEED)


def test_scale_plan_is_deterministic_and_shares_variants_in_families(
    scale_plan: S.CohortPlan, base, classes, background, correctness_plan
) -> None:
    again = _scale_plan(generate_cohort(1_000, 11), base, classes, background, exclude=correctness_plan.selected_keys)
    assert again == scale_plan
    assert not scale_plan.selected_keys & correctness_plan.selected_keys
    expected = S.scaled_role_counts(S.SCALE_ROLE_COUNTS, 1_000)
    assert dict(scale_plan.summary()["roles"]) == dict(sorted(expected.items()))
    families: dict[str, set[frozenset[K.Key]]] = {}
    for planned in scale_plan.patients:
        assert planned.panel_keys & set(background.keys) or planned.panel_keys  # everybody has variants
        if planned.patient.family_id:
            families.setdefault(planned.patient.family_id, set()).add(
                frozenset(planned.panel_keys & scale_plan.roles.keys())
            )
    assert len(families) == scale_plan.families
    assert all(len(selected) == 1 and len(next(iter(selected))) == 1 for selected in families.values())


def test_scale_plan_overrides_give_carriers_monogenic_phenotypes(scale_plan: S.CohortPlan, classes) -> None:
    by_mrn = scale_plan.by_mrn
    assert scale_plan.overrides
    for override in scale_plan.overrides:
        assert override.old != override.new and override.reason
        patient = by_mrn[override.mrn].patient
        if patient.age_at_diagnosis_months is not None:
            assert patient.age_at_diagnosis_months <= (date(2026, 10, 1) - patient.birth_date).days // 28
    neonatal = [planned.patient for planned in scale_plan.patients if planned.patient.diabetes_type == "neonatal"]
    assert neonatal and all(patient.age_at_diagnosis_months < 12 for patient in neonatal)
    hnf_carriers = [
        planned.patient
        for planned in scale_plan.patients
        if any(
            C.gene_for_position(*key[:2]) in ("HNF1A", "HNF4A") for key in planned.panel_keys & scale_plan.roles.keys()
        )
    ]
    classes_on = {medication.drug_class for patient in hnf_carriers for medication in patient.medications}
    assert {"insulin", "metformin", "sulfonylurea"} <= classes_on


def test_scale_plan_errors_for_too_small_cohorts(base, classes, background) -> None:
    with pytest.raises(ValueError, match="patients outside families"):
        S.plan_scale_cohort(
            generate_cohort(20, 3),
            S.synthetic_role_pools(base, background.keys),
            background,
            seed=SEED,
            role_counts={"stable_vus": 30},
            activating_keys=frozenset(),
            taken=classes,
            n_families=0,
        )
    with pytest.raises(ValueError, match="candidate key"):
        S.plan_scale_cohort(
            generate_cohort(50, 3),
            {},
            background,
            seed=SEED,
            role_counts={"stable_p": 1},
            activating_keys=frozenset(),
            taken=classes,
        )


def test_scaled_role_counts_keep_every_role() -> None:
    counts = S.scaled_role_counts(S.SCALE_ROLE_COUNTS, 30)
    assert set(counts) == set(S.SCALE_ROLE_COUNTS) and min(counts.values()) == 1
    assert S.scaled_role_counts(S.SCALE_ROLE_COUNTS, 10_000) == S.SCALE_ROLE_COUNTS


def test_change_set_spec_merges_cohorts(correctness_plan, scale_plan, background) -> None:
    spec = S.change_set_spec([correctness_plan, scale_plan], background, uncarried={"vus_to_lb": 3}, added_uncarried=2)
    assert spec.forced[background.changed_key] == "lb_to_b"
    assert spec.forced.items() >= correctness_plan.forced_changes.items()
    assert spec.forced.items() >= scale_plan.forced_changes.items()
    assert correctness_plan.scenario_keys["V11"] not in spec.carried_keys
    assert scale_plan.carried_keys <= spec.carried_keys
    clash = replace(scale_plan, roles={**scale_plan.roles, correctness_plan.scenario_keys["V03"]: "p_to_lp"})
    with pytest.raises(ValueError, match="forced to change as both"):
        S.change_set_spec([correctness_plan, clash], background, uncarried={}, added_uncarried=0)


# ---------------------------------------------------------------------------
# Strategies and metrics (units)
# ---------------------------------------------------------------------------


def test_mark_release_current_switches_without_reevaluation(app) -> None:
    first = KbRelease(label="A", is_current=True, sequence=1)
    second = KbRelease(label="B")
    db.session.add_all([first, second])
    db.session.flush()
    S.mark_release_current(second)
    db.session.flush()
    assert KbRelease.current() is second and second.sequence == 2 and second.activated_at is not None
    assert not first.is_current
    assert db.session.scalar(select(func.count(ReevaluationRun.id))) == 0


def test_strategy_inputs_are_validated(tmp_path: Path, correctness_plan: S.CohortPlan) -> None:
    files = S.ReleaseFiles(tmp_path / "a", tmp_path / "b", "a", "b", date(2024, 1, 1), date(2025, 1, 1))
    baseline = S.Baseline(tmp_path / "missing.db", S.StateCapture(datetime(2026, 1, 1), {}, frozenset()), {}, {})
    with pytest.raises(ValueError, match="Unknown strategy"):
        S.run_strategy("sometimes", baseline, files, {}, tmp_path / "x.db")
    with pytest.raises(ValueError, match="Unknown as-of scope"):
        S.run_strategy("full", baseline, files, {}, tmp_path / "x.db", as_of_scope="some")
    with pytest.raises(ValueError, match="Unknown ingest mode"):
        S.build_baseline(correctness_plan, files, {}, tmp_path / "x.db", ingest="ftp")


def _finding(mrn: str, rule: str, key: K.Key, kind: str = "alert", severity: str | None = "high") -> S.FindingId:
    return S.FindingId(mrn, rule, key, kind, severity)


# Keys inside the HNF1A, GCK and HNF4A panel regions (synthetic alleles).
KEY_A, KEY_B, KEY_C = ("12", 120_978_901, "A", "G"), ("7", 44_145_201, "C", "T"), ("20", 44_401_301, "G", "A")


def _outcome(**changes: Any) -> S.ChangeOutcome:
    defaults: dict[str, Any] = {
        "change": "release",
        "strategy": "change_driven",
        "started_at": datetime(2026, 1, 1),
        "duration_ms": 12.5,
        "commit_ms": 1.0,
        "state": frozenset(),
        "new_findings": frozenset(),
        "superseded": frozenset(),
        "notifications": (),
        "run": {
            "records_examined": 4,
            "patients_reevaluated": 2,
            "stored_keys_compared": 9,
            "changed_variants": 3,
            "engine_duration_ms": 11.0,
        },
        "changed_keys": frozenset(),
    }
    return S.ChangeOutcome(**{**defaults, **changes})


def test_ratio_is_none_without_denominator() -> None:
    assert M.Ratio(0, 0).value is None and M.Ratio(3, 4).value == 0.75
    assert M.Ratio(1, 2).as_dict() == {"numerator": 1, "denominator": 2, "value": 0.5}


def test_change_metrics_count_stale_missed_and_notification_quality() -> None:
    old_alert = _finding("P1", "D1", KEY_A, severity="medium")
    old_info = _finding("P2", "D6", KEY_B, severity="info")
    new_alert = _finding("P2", "D2", KEY_B)
    new_low = _finding("P2", "D5", KEY_B, severity="low")
    kept = _finding("P3", "D5", KEY_C, severity="low")
    before = frozenset({old_alert, old_info, kept})
    after = frozenset({new_alert, new_low, kept})
    notifications = (
        S.Notification("alert", "P2", KEY_B, "D2", "high"),
        S.Notification("alert", "P2", KEY_B, "D2", "high"),  # repeated: counted once
        S.Notification("review_notice", "P1", KEY_A, None, None),
        S.Notification("review_notice", "P3", KEY_C, None, None),  # nothing actionable was removed
    )
    outcome = _outcome(
        state=frozenset({new_alert, kept, old_info}),
        new_findings=frozenset({new_alert}),
        notifications=notifications,
    )
    result = M.change_metrics(outcome, before=before, after=after, realert_pairs={("P2", KEY_B), ("P9", KEY_A)})
    assert result["re_alert_sensitivity"] == {"numerator": 1, "denominator": 2, "value": 0.5}
    assert (result["stale_alerts"], result["missed_alerts"], result["affected_alerts"]) == (1, 1, 2)
    assert result["expected_notifications"] == 3  # D2 high, D5 low, notice for P1
    assert result["correct_notifications"] == 2
    assert result["notification_precision"]["value"] == 0.5
    assert result["notification_recall"]["value"] == pytest.approx(2 / 3)
    assert not result["final_state_matches_ground_truth"]
    assert result["records_examined"] == 4 and result["patients_reevaluated"] == 2


def test_change_metrics_without_notifications_report_na_precision() -> None:
    finding = _finding("P1", "D5", KEY_A, severity="low")
    outcome = _outcome(strategy="alert_once", state=frozenset({finding}), run=None, changed_keys=None)
    result = M.change_metrics(outcome, before=frozenset({finding}), after=frozenset({finding}), realert_pairs=set())
    assert result["notification_precision"]["value"] is None
    assert result["notification_recall"]["denominator"] == 0
    assert result["final_state_matches_ground_truth"] and result["records_examined"] == 0
    assert M.delta_check(outcome, _tiny_plan(), []) is None


def _tiny_plan() -> S.CohortPlan:
    patient = CohortPatient(
        "GEN-1",
        "Ada",
        "Test",
        "female",
        date(1990, 1, 1),
        "type1",
        120,
        [CohortMedication("Humulin", "insulin", date(2001, 1, 1))],
    )
    variants = ((*KEY_A, "heterozygous"), (*KEY_B, "heterozygous"), ("1", 1_000_000, "A", "G", "heterozygous"))
    return S.CohortPlan("tiny", (S.PlannedPatient("P1", patient, variants),), roles={}, overrides=())


def test_ground_truth_uses_the_pure_rule_engine() -> None:
    plan = replace(
        _tiny_plan(),
        patients=(
            S.PlannedPatient(
                "P1",
                _tiny_plan().patients[0].patient,
                ((*("12", 120_978_900, "G", "A"), "heterozygous"), ("1", 1_000_000, "A", "G", "heterozygous")),
            ),
        ),
    )
    key = ("12", 120_978_900, "G", "A")
    release = {key: K.KnownRecord(key, "HNF1A", "P", "unknown", False, 3, "Pathogenic")}
    rules = M.rule_specs(R._rule_documents()[0])
    assert M.ground_truth(plan, release, rules) == {
        _finding("P1", "D2", key, severity="high"),
        _finding("P1", "D5", key, severity="low"),
    }
    assert M.ground_truth(plan, {}, rules) == frozenset()  # absent from the release: not classified


def test_delta_check_compares_recorded_and_expected_keys() -> None:
    plan = _tiny_plan()
    change = K.InjectedChange(
        KEY_A,
        "HNF1A",
        "vus_to_lp",
        True,
        "Uncertain_significance",
        "Likely_pathogenic",
        "VUS",
        "LP",
        "unknown",
        "unknown",
    )
    raw_only = replace(change, key=KEY_B, kind="vus_to_conflicting", new_class="VUS")
    good = M.delta_check(_outcome(changed_keys=frozenset({KEY_A})), plan, [change, raw_only])
    assert good["matches"] and good["expected"] == 1
    bad = M.delta_check(_outcome(changed_keys=frozenset({KEY_B})), plan, [change, raw_only])
    assert not bad["matches"] and bad["missing"] == [K.key_label(KEY_A)] and bad["unexpected"] == [K.key_label(KEY_B)]


# ---------------------------------------------------------------------------
# End to end: the quick evaluation
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def quick(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Any]:
    work = tmp_path_factory.mktemp("evaluation")
    options = R.Options(
        scale=R.QUICK_SCALE,
        kb_size=R.QUICK_KB_SIZE,
        repeats=1,
        work_dir=work,
        quick=True,
        real_dir=work / "no-real-releases",
        out=work / "out",
    )
    return R.run_evaluation(options)


def _strategies(quick: dict[str, Any], cohort: str) -> dict[str, Any]:
    return quick["synthetic"]["cohorts"][cohort]["strategies"]


@pytest.mark.parametrize("cohort", ["correctness", "scale"])
def test_alert_once_never_realerts_and_keeps_affected_alerts(quick: dict[str, Any], cohort: str) -> None:
    release = _strategies(quick, cohort)["alert_once"]["release"]
    assert release["re_alert_sensitivity"]["numerator"] == 0 < release["re_alert_sensitivity"]["denominator"]
    assert release["stale_alerts"] == release["affected_alerts"] > 0
    assert release["notifications"] == 0 and release["records_examined"] == 0
    assert release["notification_precision"]["value"] is None
    assert not release["final_state_matches_ground_truth"]


@pytest.mark.parametrize("cohort", ["correctness", "scale"])
@pytest.mark.parametrize("strategy", ["full", "change_driven"])
def test_reevaluation_reaches_ground_truth_with_precise_notifications(quick, cohort: str, strategy: str) -> None:
    entry = _strategies(quick, cohort)[strategy]
    for change in ("release", "rule_revision"):
        metric = entry[change]
        assert metric["final_state_matches_ground_truth"], (change, metric["stale_examples"], metric["missed_examples"])
        assert metric["stale_alerts"] == metric["missed_alerts"] == 0
        assert metric["notification_recall"]["value"] in (1.0, None)
        assert metric["notification_precision"]["value"] in (1.0, None)
    release = entry["release"]
    assert release["re_alert_sensitivity"]["value"] == 1.0
    assert release["notification_precision"]["value"] == 1.0 and release["notification_recall"]["value"] == 1.0
    assert release["delta"]["matches"]
    assert all(check["matches"] for check in entry["as_of"].values())


@pytest.mark.parametrize("cohort", ["correctness", "scale"])
def test_change_driven_examines_fewer_records_than_full(quick: dict[str, Any], cohort: str) -> None:
    entries = _strategies(quick, cohort)
    full, change = entries["full"], entries["change_driven"]
    total = quick["synthetic"]["cohorts"][cohort]["baseline"]["counts"]["patient_variants"]
    assert full["release"]["records_examined"] == total
    assert 0 < change["release"]["records_examined"] < full["release"]["records_examined"]
    assert change["release"]["patients_reevaluated"] < full["release"]["patients_reevaluated"]
    assert change["rule_revision"]["records_examined"] < full["rule_revision"]["records_examined"]


def test_initial_state_equals_ground_truth(quick: dict[str, Any]) -> None:
    for cohort in quick["synthetic"]["cohorts"].values():
        assert cohort["baseline"]["initial_state_vs_ground_truth"]["matches"]


def test_table_v_scenarios(quick: dict[str, Any]) -> None:
    entries = _strategies(quick, "correctness")
    for strategy, entry in entries.items():
        checks = {check["code"]: check for check in entry["scenarios"]}
        assert entry["point_in_time_correctness"]["value"] == 1.0, strategy
        assert checks["S12"]["passed"] and checks["S12"]["observed"].startswith("lab technician 403, clinician ")
        assert checks["RS7"]["passed"]
        reclassification = [checks[f"RS{number}"]["passed"] for number in range(1, 7)]
        if strategy == "alert_once":
            assert not any(reclassification)  # no re-evaluation, by design
        else:
            assert all(reclassification), [checks[f"RS{number}"] for number in range(1, 7)]


def test_results_record_environment_overrides_and_pending_real_pair(quick: dict[str, Any]) -> None:
    assert {"python", "sqlite", "cpu_count", "date"} <= quick["environment"].keys()
    correctness = quick["synthetic"]["cohorts"]["correctness"]
    assert len(correctness["overrides"]) == correctness["cohort"]["overrides"] > 0
    assert quick["real_pair"]["status"] == "pending"
    change_set = quick["synthetic"]["knowledge"]["change_set"]
    assert {kind for kind, row in change_set["kinds"].items() if row["carried"]} >= set(K.CHANGE_KINDS)
    json.dumps(quick)  # JSON-ready


def test_write_results_renders_table_vi(tmp_path: Path, quick: dict[str, Any]) -> None:
    json_path, markdown_path = R.write_results(quick, tmp_path / "out")
    assert json.loads(json_path.read_text(encoding="utf-8"))["options"] == quick["options"]
    text = markdown_path.read_text(encoding="utf-8")
    expectations = quick["synthetic"]["table_vi_expectations"]
    assert "## Table VI — measured" in text
    assert f"Paper Table VI expectations met: **{len(expectations)} of {len(expectations)}**" in text
    assert "Real ClinVar release pair: pending — NCBI not reachable from the build environment" in text
    for label in S.STRATEGY_LABELS.values():
        assert label in text


def test_table_vi_expectations_are_all_met(quick: dict[str, Any]) -> None:
    expectations = quick["synthetic"]["table_vi_expectations"]
    assert [row for row in expectations if not row["met"]] == []
    assert {row["strategy"] for row in expectations} == set(S.STRATEGIES)
    scale = quick["synthetic"]["cohorts"]["scale"]["strategies"]["change_driven"]["release"]
    assert scale["records_examined"] == scale["delta"]["carrier_records"] > 0  # "changed only"


def test_table_vi_expectations_flag_deviations(quick: dict[str, Any]) -> None:
    cohorts = json.loads(json.dumps(quick["synthetic"]["cohorts"]))  # deep copy
    scale = cohorts["scale"]["strategies"]
    scale["change_driven"]["release"]["stale_alerts"] = 3
    scale["full"]["release"]["records_examined"] -= 1
    scale["alert_once"]["release"]["re_alert_sensitivity"]["numerator"] = 1
    failed = {
        (row["metric"], row["strategy"])
        for row in R.table_vi_expectations(cohorts["scale"], cohorts["correctness"])
        if not row["met"]
    }
    assert failed == {
        ("Stale alerts after release", "change_driven"),
        ("Records examined", "full"),
        ("Re-alert sensitivity", "alert_once"),
    }
    text = "\n".join(R._expectations(R.table_vi_expectations(cohorts["scale"], cohorts["correctness"])))
    assert "expectations met: **13 of 16**" in text and "| **no** |" in text


def test_real_pair_path_runs_on_a_release_pair(tmp_path: Path, base: list[ClinVarRecord], classes) -> None:
    carried = frozenset(sorted(classes)[::2])
    new, _truth = K.apply_change_set(
        base,
        K.ChangeSetSpec(
            uncarried={"vus_to_lp": 40, "lp_to_vus": 20, "lp_to_p": 20, "vus_to_lb": 40}, carried_keys=carried
        ),
        SEED,
    )
    pair = K.RealReleasePair(
        _write_subset(tmp_path, "2023-01-07", base),
        _write_subset(tmp_path, "2025-01-05", new),
        date(2023, 1, 7),
        date(2025, 1, 5),
    )
    options = R.Options(scale=200, repeats=1, work_dir=tmp_path)
    result = R.evaluate_real_pair(pair, options, generate_cohort(200, 5), tmp_path)
    assert result["status"] == "evaluated"
    entries = result["cohort"]["strategies"]
    assert entries["change_driven"]["release"]["final_state_matches_ground_truth"]
    assert entries["full"]["release"]["final_state_matches_ground_truth"]
    assert entries["alert_once"]["release"]["re_alert_sensitivity"]["numerator"] == 0
    assert entries["change_driven"]["release"]["records_examined"] < entries["full"]["release"]["records_examined"]
    assert result["cohort"]["cohort"]["roles"]["upgrade"] >= 1


# ---------------------------------------------------------------------------
# Command line
# ---------------------------------------------------------------------------


def test_parse_args_defaults_and_quick_mode() -> None:
    options = R.parse_args([])
    assert (options.scale, options.correctness, options.repeats, options.kb_size) == (10_000, 30, 3, 9_000)
    assert options.out == R.DEFAULT_OUT and options.real_dir == R.DEFAULT_REAL_DIR
    quick = R.parse_args(["--quick"])
    assert (quick.scale, quick.repeats, quick.kb_size, quick.out) == (300, 1, 1_500, R.QUICK_OUT)
    explicit = R.parse_args(["--quick", "--scale", "500", "--repeats", "2", "--seed", "9", "--out", "x"])
    assert (explicit.scale, explicit.repeats, explicit.seed, explicit.out) == (500, 2, 9, Path("x"))
    with pytest.raises(SystemExit):
        R.parse_args(["--scale", "0"])


def test_load_cohort_rejects_oversized_requests() -> None:
    assert len(R.load_cohort(R.CORRECTNESS_FILE, 5)) == 5
    with pytest.raises(ValueError, match="holds 30 patients; 31 were requested"):
        R.load_cohort(R.CORRECTNESS_FILE, 31)


def test_main_writes_results(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, quick: dict[str, Any]) -> None:
    seen: list[R.Options] = []

    def fake_run(options: R.Options) -> dict[str, Any]:
        seen.append(options)
        return quick

    monkeypatch.setattr(R, "run_evaluation", fake_run)
    assert R.main(["--quick", "--out", str(tmp_path)]) == 0
    assert seen[0].quick and (tmp_path / "results.json").is_file() and (tmp_path / "RESULTS.md").is_file()


def test_main_reports_errors(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    def failing(_options: R.Options) -> dict[str, Any]:
        raise ValueError("synthea_scale_10000.csv.gz holds 10,000 patients; 20,000 were requested.")

    monkeypatch.setattr(R, "run_evaluation", failing)
    assert R.main(["--scale", "20000", "--out", str(tmp_path)]) == 1
    assert not (tmp_path / "results.json").exists()
    assert "Evaluation failed: synthea_scale_10000.csv.gz holds 10,000 patients" in capsys.readouterr().err


def test_committed_results_meet_the_paper_expectations() -> None:
    """The results kept in the repository (``python -m evaluation.run``) are complete and consistent."""
    path = R.DEFAULT_OUT / "results.json"
    if not path.is_file():
        pytest.skip("evaluation/results/results.json has not been generated (run python -m evaluation.run)")
    results = json.loads(path.read_text(encoding="utf-8"))
    assert results["synthetic_data_only"] is True and not results["options"]["quick"]
    assert (results["options"]["scale"], results["options"]["correctness"], results["options"]["repeats"]) == (
        10_000,
        30,
        3,
    )
    assert [row for row in results["synthetic"]["table_vi_expectations"] if not row["met"]] == []
    assert results["real_pair"]["status"] in ("pending", "evaluated")
    scale = results["synthetic"]["cohorts"]["scale"]
    assert scale["baseline"]["counts"]["patients"] == 10_000
    assert all(len(scale["strategies"][strategy]["release"]["timing_ms"]["runs"]) == 3 for strategy in S.STRATEGIES)
    markdown = (R.DEFAULT_OUT / "RESULTS.md").read_text(encoding="utf-8")
    assert results["generated_at"] in markdown and "## Table VI — measured" in markdown
