"""Run the GE-EHR evaluation (SPEC §10, paper §5): ``python -m evaluation.run``.

Steps: generate the synthetic base release K_old; prepare the correctness cohort (Table V
scenarios) and the scale cohort from the Synthea files in ``data/cohorts/``; derive K_new with a
controlled change set covering every change category plus changes on keys carried by nobody;
compute the ground truth with the pure rule engine; build one baseline database per cohort and
run the three strategies (alert-once, full recomputation, GE-EHR change-driven) on identical
copies for the release K_old → K_new and the rule revision D4 v1 → v2. The scale cohort is run
``--repeats`` times (default 3) to report median, minimum and maximum time per change. When two
real ClinVar 7-gene subsets exist in ``--real-dir`` the scale evaluation is repeated on that
natural release pair; otherwise the report says it is pending.

Outputs: ``<out>/results.json`` (every raw number, the clinical overrides and the environment)
and ``<out>/RESULTS.md`` (Table VI with measured values and the breakdowns). ``--quick`` runs a
small configuration (30 + 300 patients, 1,500-record knowledge base, one repeat) in seconds; it
writes to ``evaluation/results/quick`` unless ``--out`` is given.

Owned by the Eval work package.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import platform
import sqlite3
import statistics
import sys
import tempfile
from collections import Counter
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, date, datetime
from importlib import metadata
from pathlib import Path
from typing import Any

from ge_ehr import constants as C
from ge_ehr.services import rules as rule_service
from ge_ehr.services.clinvar import file_sha256, write_clinvar_vcf

from . import knowledge, metrics, strategies
from .cohort import CohortPatient, load_compact_cohort
from .knowledge import InjectedChange, Key, KnownRecord
from .strategies import STRATEGIES, STRATEGY_LABELS, CohortPlan, ReleaseFiles, StrategyOutcome

logger = logging.getLogger("evaluation")

REPO_ROOT = Path(__file__).resolve().parents[1]
COHORT_DIR = REPO_ROOT / "data" / "cohorts"
CORRECTNESS_FILE = "synthea_correctness_30.csv.gz"
SCALE_FILE = "synthea_scale_10000.csv.gz"
DEFAULT_OUT = REPO_ROOT / "evaluation" / "results"
QUICK_OUT = DEFAULT_OUT / "quick"
DEFAULT_REAL_DIR = REPO_ROOT / "data" / "clinvar" / "real"
DEFAULT_SEED = 2026
DEFAULT_REPEATS = 3
QUICK_SCALE, QUICK_KB_SIZE = 300, 1_500

BASE_FILE_DATE, NEW_FILE_DATE = date(2024, 1, 7), date(2026, 1, 4)
BASE_LABEL = "Synthetic K_old (2024-01, base release)"
NEW_LABEL = "Synthetic K_new (2026-01, controlled change set)"

# Random changes on keys carried by nobody, per 9,000 base records (scaled with the release size).
UNCARRIED_CHANGES: dict[str, int] = {
    "vus_to_lp": 60,
    "vus_to_p": 20,
    "nc_to_lp": 10,
    "lp_to_vus": 30,
    "p_to_vus": 10,
    "p_removed": 10,
    "lp_to_p": 30,
    "p_to_lp": 10,
    "katp_activating": 5,
    "vus_to_lb": 80,
    "lb_to_b": 60,
    "vus_to_conflicting": 40,
}
ADDED_UNCARRIED = 300  # new ClinVar submissions on keys nobody carries, per 9,000 base records
REAL_PAIR_PENDING = (
    "pending — NCBI not reachable from the build environment; run scripts/fetch_clinvar.py then "
    "python -m evaluation.run"
)


@dataclass(frozen=True)
class Options:
    """Evaluation settings (see :func:`parse_args`)."""

    scale: int = 10_000
    correctness: int = 30
    seed: int = DEFAULT_SEED
    out: Path = DEFAULT_OUT
    real_dir: Path = DEFAULT_REAL_DIR
    kb_size: int = knowledge.BASE_RELEASE_SIZE
    repeats: int = DEFAULT_REPEATS
    work_dir: Path | None = None
    quick: bool = False


def _positive(text: str) -> int:
    value = int(text)
    if value < 1:
        raise argparse.ArgumentTypeError(f"must be a positive whole number, got {text}")
    return value


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m evaluation.run",
        description="Evaluate alert-once, full recomputation and GE-EHR change-driven re-evaluation (synthetic data).",
    )
    parser.add_argument("--scale", type=_positive, help="scale cohort size (first N Synthea patients; default 10000)")
    parser.add_argument("--correctness", type=_positive, help="correctness cohort size (default 30)")
    parser.add_argument("--seed", type=int, default=DEFAULT_SEED, help=f"random seed (default {DEFAULT_SEED})")
    parser.add_argument("--out", type=Path, help=f"output directory (default {DEFAULT_OUT})")
    parser.add_argument("--real-dir", type=Path, default=DEFAULT_REAL_DIR, help="directory with real ClinVar subsets")
    parser.add_argument("--kb-size", type=_positive, help="records in the synthetic base release (default 9000)")
    parser.add_argument("--repeats", type=_positive, help="timing repeats of the scale cohort (default 3)")
    parser.add_argument("--work-dir", type=Path, help="keep databases, releases and VCFs here (default: temporary)")
    parser.add_argument("--quick", action="store_true", help="small, fast configuration (30 + 300 patients)")
    return parser


def parse_args(argv: Sequence[str] | None = None) -> Options:
    """Options from the command line; ``--quick`` changes the defaults of the size options."""
    args = build_parser().parse_args(argv)
    quick = bool(args.quick)
    return Options(
        scale=args.scale or (QUICK_SCALE if quick else 10_000),
        correctness=args.correctness or 30,
        seed=args.seed,
        out=args.out or (QUICK_OUT if quick else DEFAULT_OUT),
        real_dir=args.real_dir,
        kb_size=args.kb_size or (QUICK_KB_SIZE if quick else knowledge.BASE_RELEASE_SIZE),
        repeats=args.repeats or (1 if quick else DEFAULT_REPEATS),
        work_dir=args.work_dir,
        quick=quick,
    )


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------


def environment() -> dict[str, Any]:
    """Interpreter, library and machine information recorded with the results."""
    return {
        "python": platform.python_version(),
        "sqlite": sqlite3.sqlite_version,
        "sqlalchemy": metadata.version("sqlalchemy"),
        "flask": metadata.version("flask"),
        "platform": platform.platform(),
        "machine": platform.machine(),
        "cpu_count": os.cpu_count(),
        "date": datetime.now(UTC).isoformat(timespec="seconds"),
    }


@contextmanager
def _work_directory(path: Path | None) -> Iterator[Path]:
    if path is not None:
        path.mkdir(parents=True, exist_ok=True)
        yield path
        return
    with tempfile.TemporaryDirectory(prefix="ge-ehr-eval-") as temporary:
        yield Path(temporary)


def load_cohort(file_name: str, size: int) -> list[CohortPatient]:
    """The first ``size`` patients (by Synthea id) of a cohort file in ``data/cohorts/``."""
    patients = load_compact_cohort(COHORT_DIR / file_name)
    if size > len(patients):
        raise ValueError(f"{file_name} holds {len(patients):,} patients; {size:,} were requested.")
    return patients[:size]


def _rule_documents() -> tuple[dict[str, Any], dict[str, Any]]:
    return (
        rule_service.load_rule_set_file(rule_service.RULES_DATA_DIR / "ruleset_v1.json"),
        rule_service.load_rule_set_file(rule_service.RULES_DATA_DIR / "ruleset_v2.json"),
    )


def _activating(*releases: Mapping[Key, KnownRecord]) -> frozenset[Key]:
    return frozenset(key for release in releases for key, known in release.items() if known.mechanism == "activating")


def _timing(values: Sequence[float]) -> dict[str, Any]:
    return {
        "runs": [round(value, 3) for value in values],
        "median": round(statistics.median(values), 3),
        "min": round(min(values), 3),
        "max": round(max(values), 3),
    }


def _signature(outcome: StrategyOutcome) -> tuple[Any, ...]:
    """Everything a repeat must reproduce exactly (states, notifications, work)."""
    return tuple(
        (change.state, change.notifications, change.records_examined)
        for change in (outcome.release, outcome.rule_revision)
    )


def _as_of_dict(outcome: StrategyOutcome) -> dict[str, Any]:
    return {
        name: {
            "at": check.at.isoformat(),
            "patients_checked": check.patients_checked,
            "patients_matching": check.patients_matching,
            "matches": check.matches,
            "mismatches": list(check.mismatches),
        }
        for name, check in outcome.as_of.items()
    }


def _run_counters(outcome: StrategyOutcome) -> dict[str, Any]:
    return {
        change.change: {
            "duration_ms": round(change.duration_ms, 3),
            "commit_ms": round(change.commit_ms, 3),
            "records_examined": change.records_examined,
            "run": dict(change.run) if change.run else None,
        }
        for change in (outcome.release, outcome.rule_revision)
    }


def evaluate_cohort(
    plan: CohortPlan,
    files: ReleaseFiles,
    old: Mapping[Key, KnownRecord],
    new: Mapping[Key, KnownRecord],
    changes: Sequence[InjectedChange],
    work: Path,
    *,
    ingest: str,
    repeats: int,
    as_of_scope: str,
    scenarios: bool,
) -> dict[str, Any]:
    """Ground truth, baseline and all strategies (``repeats`` times) for one cohort; JSON-ready."""
    doc_v1, doc_v2 = _rule_documents()
    rules_v1, rules_v2 = metrics.rule_specs(doc_v1), metrics.rule_specs(doc_v2)
    truth_initial = metrics.ground_truth(plan, old, rules_v1)
    truth_release = metrics.ground_truth(plan, new, rules_v1)
    truth_rules = metrics.ground_truth(plan, new, rules_v2)
    logger.info("%s cohort: building the baseline database (%s)", plan.name, ingest)
    baseline = strategies.build_baseline(
        plan,
        files,
        doc_v1,
        work / f"{plan.name}-baseline.db",
        ingest=ingest,
        vcf_dir=work / f"{plan.name}-vcfs" if ingest == "vcf" else None,
    )
    t0 = baseline.state.at

    def inspect(app: Any, outcome: StrategyOutcome) -> list[metrics.ScenarioCheck]:
        return metrics.scenario_checks(app, plan, outcome, t0=t0)

    runs: dict[str, list[StrategyOutcome]] = {strategy: [] for strategy in STRATEGIES}
    for repeat in range(repeats):
        order = STRATEGIES[repeat % len(STRATEGIES) :] + STRATEGIES[: repeat % len(STRATEGIES)]
        for strategy in order:
            logger.info("%s cohort, run %d/%d: %s", plan.name, repeat + 1, repeats, STRATEGY_LABELS[strategy])
            runs[strategy].append(
                strategies.run_strategy(
                    strategy,
                    baseline,
                    files,
                    doc_v2,
                    work / f"{plan.name}-{strategy}-{repeat + 1}.db",
                    as_of_scope=as_of_scope if repeat == 0 else None,
                    inspect=inspect if scenarios and repeat == 0 else None,
                )
            )

    release_pairs = metrics.release_realert_pairs(plan, changes)
    revision_pairs = metrics.revision_realert_pairs(truth_release, truth_rules)
    results: dict[str, Any] = {}
    for strategy, outcomes in runs.items():
        first = outcomes[0]
        entry: dict[str, Any] = {
            "label": STRATEGY_LABELS[strategy],
            "release": {
                **metrics.change_metrics(
                    first.release, before=truth_initial, after=truth_release, realert_pairs=release_pairs
                ),
                "delta": metrics.delta_check(first.release, plan, changes),
                "timing_ms": _timing([outcome.release.duration_ms for outcome in outcomes]),
            },
            "rule_revision": {
                **metrics.change_metrics(
                    first.rule_revision, before=truth_release, after=truth_rules, realert_pairs=revision_pairs
                ),
                "timing_ms": _timing([outcome.rule_revision.duration_ms for outcome in outcomes]),
            },
            "as_of": _as_of_dict(first),
            "outcomes_identical_across_repeats": all(_signature(outcome) == _signature(first) for outcome in outcomes),
            "runs": [_run_counters(outcome) for outcome in outcomes],
        }
        if scenarios:
            checks = first.inspection
            entry["scenarios"] = [check.as_dict() for check in checks]
            entry["point_in_time_correctness"] = metrics.point_in_time_correctness(checks).as_dict()
        results[strategy] = entry
    return {
        "cohort": plan.summary(),
        "baseline": {
            "ingest": ingest,
            "timings_ms": {name: round(value, 3) for name, value in baseline.timings_ms.items()},
            "counts": dict(baseline.counts),
            "initial_state_vs_ground_truth": metrics.state_check(baseline.state.findings, truth_initial),
        },
        "ground_truth": {
            "initial": _truth_counts(truth_initial),
            "after_release": _truth_counts(truth_release),
            "after_rule_revision": _truth_counts(truth_rules),
            "release_re_alert_pairs": len(release_pairs),
            "rule_revision_re_alert_pairs": len(revision_pairs),
        },
        "change_kinds": metrics.kind_breakdown(plan, changes),
        "categories": metrics.category_breakdown(
            {strategy: outcomes[0].release for strategy, outcomes in runs.items()},
            plan=plan,
            changes=changes,
            before=truth_initial,
            after=truth_release,
        ),
        "strategies": results,
        "overrides": [override.as_dict() for override in plan.overrides],
    }


def _truth_counts(findings: frozenset[strategies.FindingId]) -> dict[str, Any]:
    alerts = [finding for finding in findings if finding.kind == "alert"]
    return {
        "findings": len(findings),
        "alerts": len(alerts),
        "notes": len(findings) - len(alerts),
        "alerts_by_rule": dict(sorted(Counter(finding.rule_code for finding in alerts).items())),
    }


def _scaled(counts: Mapping[str, int], kb_size: int) -> dict[str, int]:
    factor = kb_size / knowledge.BASE_RELEASE_SIZE
    return {kind: max(1, round(count * factor)) for kind, count in counts.items()}


def evaluate_synthetic(
    options: Options, correctness: Sequence[CohortPatient], scale: Sequence[CohortPatient], work: Path
) -> dict[str, Any]:
    """The controlled-change-set evaluation on both cohorts."""
    seed = options.seed
    base = knowledge.generate_base_release(seed, total=options.kb_size)
    old = knowledge.classify_records(base)
    background = strategies.choose_background_pool(old, seed)
    correctness_plan = strategies.plan_correctness_cohort(correctness, base, background, seed=seed)
    pools = strategies.synthetic_role_pools(base, correctness_plan.selected_keys | set(background.keys))
    scale_plan = strategies.plan_scale_cohort(
        scale,
        pools,
        background,
        seed=seed,
        role_counts=strategies.scaled_role_counts(strategies.SCALE_ROLE_COUNTS, len(scale)),
        activating_keys=_activating(old),
        taken=old.keys() | correctness_plan.carried_keys | correctness_plan.selected_keys,
    )
    spec = strategies.change_set_spec(
        [correctness_plan, scale_plan],
        background,
        uncarried=_scaled(UNCARRIED_CHANGES, options.kb_size),
        added_uncarried=_scaled({"added": ADDED_UNCARRIED}, options.kb_size)["added"],
    )
    new_records, changes = knowledge.apply_change_set(base, spec, seed)
    new = knowledge.classify_records(new_records)
    release_dir = work / "releases"
    files = ReleaseFiles(
        old_path=release_dir / f"ge_ehr_synthetic_k_old_seed{seed}.vcf.gz",
        new_path=release_dir / f"ge_ehr_synthetic_k_new_seed{seed}.vcf.gz",
        old_label=BASE_LABEL,
        new_label=NEW_LABEL,
        old_date=BASE_FILE_DATE,
        new_date=NEW_FILE_DATE,
    )
    write_clinvar_vcf(base, files.old_path, file_date=BASE_FILE_DATE.isoformat(), synthetic=True)
    write_clinvar_vcf(new_records, files.new_path, file_date=NEW_FILE_DATE.isoformat(), synthetic=True)

    cohorts = {}
    for plan, ingest, repeats, scope, scenarios in (
        (correctness_plan, "vcf", 1, "all", True),
        (scale_plan, "bulk", options.repeats, "alerted", False),
    ):
        cohorts[plan.name] = evaluate_cohort(
            plan, files, old, new, changes, work, ingest=ingest, repeats=repeats, as_of_scope=scope, scenarios=scenarios
        )
    return {
        "knowledge": {
            "base_release": {
                "label": BASE_LABEL,
                "file_date": BASE_FILE_DATE.isoformat(),
                "sha256": file_sha256(files.old_path),
                **knowledge.release_summary(base),
            },
            "new_release": {
                "label": NEW_LABEL,
                "file_date": NEW_FILE_DATE.isoformat(),
                "sha256": file_sha256(files.new_path),
                **knowledge.release_summary(new_records),
            },
            "background_pool": {
                "keys": len(background.keys),
                "changed_key": knowledge.key_label(background.changed_key) if background.changed_key else None,
            },
            "change_set": knowledge.summarize_changes(changes),
            "carried_changes": [change.as_dict() for change in changes if change.carried],
            "rule_revision": "D4 v1 (KCNJ11) → D4 v2 (KCNJ11 or ABCC8), rules v1 → v2",
        },
        "cohorts": cohorts,
        "table_vi_expectations": table_vi_expectations(cohorts["scale"], cohorts["correctness"]),
    }


def evaluate_real_pair(
    pair: knowledge.RealReleasePair, options: Options, scale: Sequence[CohortPatient], work: Path
) -> dict[str, Any]:
    """The natural changes of a real ClinVar release pair on the scale cohort."""
    old_records, new_records = knowledge.read_release(pair.old_path), knowledge.read_release(pair.new_path)
    old, new = knowledge.classify_records(old_records), knowledge.classify_records(new_records)
    changed = knowledge.transitions(old, new)
    background = strategies.choose_background_pool(
        old, options.seed, eligible=lambda key: key not in changed, with_changed_key=False
    )
    plan = strategies.plan_scale_cohort(
        scale,
        strategies.transition_role_pools(old, new, background.keys),
        background,
        seed=options.seed,
        role_counts=strategies.scaled_role_counts(strategies.REAL_PAIR_ROLE_COUNTS, len(scale)),
        activating_keys=_activating(old, new),
        taken=old.keys() | new.keys(),
        name="real_pair",
        mrn_prefix="EVAL-R",
        strict=False,
    )
    changes = knowledge.natural_changes(old, new, plan.carried_keys)
    files = ReleaseFiles(
        old_path=pair.old_path,
        new_path=pair.new_path,
        old_label=f"ClinVar {pair.old_date.isoformat()} (7-gene subset)",
        new_label=f"ClinVar {pair.new_date.isoformat()} (7-gene subset)",
        old_date=pair.old_date,
        new_date=pair.new_date,
    )
    result = evaluate_cohort(
        plan,
        files,
        old,
        new,
        changes,
        work,
        ingest="bulk",
        repeats=options.repeats,
        as_of_scope="alerted",
        scenarios=False,
    )
    return {
        "status": "evaluated",
        "releases": {
            "old": {
                "file": pair.old_path.name,
                "sha256": file_sha256(pair.old_path),
                **knowledge.release_summary(old_records),
            },
            "new": {
                "file": pair.new_path.name,
                "sha256": file_sha256(pair.new_path),
                **knowledge.release_summary(new_records),
            },
        },
        "changes": knowledge.summarize_changes(changes),
        "cohort": result,
    }


def run_evaluation(options: Options) -> dict[str, Any]:
    """Run everything and return the JSON-ready results (see the module docstring)."""
    started = datetime.now(UTC)
    correctness = load_cohort(CORRECTNESS_FILE, options.correctness)
    scale = load_cohort(SCALE_FILE, options.scale)
    pair = knowledge.load_real_pair(options.real_dir)  # validated before the long synthetic run
    with _work_directory(options.work_dir) as work:
        synthetic = evaluate_synthetic(options, correctness, scale, work)
        if pair is None:
            real: dict[str, Any] = {
                "status": "pending",
                "reason": REAL_PAIR_PENDING,
                "directory": _display_path(options.real_dir),
            }
        else:
            real = evaluate_real_pair(pair, options, scale, work)
    return {
        "generated_at": started.isoformat(timespec="seconds"),
        "duration_s": round((datetime.now(UTC) - started).total_seconds(), 1),
        "command": "python -m evaluation.run " + " ".join(_command_arguments(options)),
        "synthetic_data_only": True,
        "options": {
            "scale": options.scale,
            "correctness": options.correctness,
            "seed": options.seed,
            "kb_size": options.kb_size,
            "repeats": options.repeats,
            "quick": options.quick,
        },
        "environment": environment(),
        "cohort_sources": {
            "correctness": {"file": f"data/cohorts/{CORRECTNESS_FILE}", "patients_used": len(correctness)},
            "scale": {"file": f"data/cohorts/{SCALE_FILE}", "patients_used": len(scale)},
            "provenance": "Synthea (see data/cohorts/README.md)",
        },
        "synthetic": synthetic,
        "real_pair": real,
    }


def _display_path(path: Path) -> str:
    try:
        return str(path.resolve().relative_to(REPO_ROOT))
    except ValueError:
        return str(path)


def _command_arguments(options: Options) -> list[str]:
    arguments = ["--scale", str(options.scale), "--correctness", str(options.correctness), "--seed", str(options.seed)]
    if options.kb_size != knowledge.BASE_RELEASE_SIZE:
        arguments += ["--kb-size", str(options.kb_size)]
    if options.repeats != DEFAULT_REPEATS:
        arguments += ["--repeats", str(options.repeats)]
    return arguments


# ---------------------------------------------------------------------------
# RESULTS.md
# ---------------------------------------------------------------------------


def _number(value: float | int | None) -> str:
    if value is None:
        return "n/a"
    return f"{value:,.0f}" if isinstance(value, int) or float(value).is_integer() else f"{value:,.1f}"


def _ratio(ratio: Mapping[str, Any]) -> str:
    if ratio["value"] is None:
        return "n/a (0 / 0)" if ratio["denominator"] == 0 else "n/a"
    return f"{ratio['value'] * 100:.1f} % ({ratio['numerator']:,} / {ratio['denominator']:,})"


def _precision(metric: Mapping[str, Any]) -> str:
    ratio = metric["notification_precision"]
    return "n/a (no notifications)" if ratio["denominator"] == 0 else _ratio(ratio)


def _ms(timing: Mapping[str, Any], repeats: int) -> str:
    if repeats == 1:
        return f"{timing['median']:,.1f} ms"
    return f"{timing['median']:,.1f} ms [{timing['min']:,.1f}–{timing['max']:,.1f}]"


def _stale(metric: Mapping[str, Any]) -> str:
    stale, affected = metric["stale_alerts"], metric["affected_alerts"]
    if stale == 0:
        return "0"
    return (
        f"{stale:,} ({stale:,} of {affected:,} affected)" if stale == affected else f"{stale:,} ({affected:,} affected)"
    )


def _records(metric: Mapping[str, Any], strategy: str, total_records: int, scope: str = "changed only") -> str:
    """Records examined; ``scope`` describes what the change-driven strategy restricts itself to."""
    records = metric["records_examined"]
    if strategy == "full":
        return f"{records:,} (all {total_records:,})" if records == total_records else f"{records:,}"
    if strategy == "change_driven":
        return f"{records:,} ({scope})"
    return f"{records:,}"


def _as_of(entry: Mapping[str, Any], name: str) -> str:
    check = entry["as_of"].get(name)
    if check is None:
        return "n/a"
    ratio = {"numerator": check["patients_matching"], "denominator": check["patients_checked"]}
    ratio["value"] = ratio["numerator"] / ratio["denominator"] if ratio["denominator"] else None
    return _ratio(ratio) + " patients"


def _yes(value: bool | None) -> str:
    return "n/a" if value is None else ("yes" if value else "no")


def _table(header: Sequence[str], rows: Sequence[Sequence[str]]) -> list[str]:
    lines = ["| " + " | ".join(header) + " |", "|" + "|".join("---" for _ in header) + "|"]
    lines.extend("| " + " | ".join(row) + " |" for row in rows)
    return lines


def _table_vi(cohort: Mapping[str, Any], correctness: Mapping[str, Any], repeats: int) -> list[str]:
    entries = cohort["strategies"]
    total = cohort["baseline"]["counts"]["patient_variants"]
    patients = cohort["baseline"]["counts"]["patients"]

    def row(label: str, render: Any) -> list[str]:
        return [label, *(render(strategy, entries[strategy]) for strategy in STRATEGIES)]

    rows = [
        row("Re-alert sensitivity", lambda s, e: _ratio(e["release"]["re_alert_sensitivity"])),
        row("Stale alerts after release", lambda s, e: _stale(e["release"])),
        row("Missed alerts after release", lambda s, e: _number(e["release"]["missed_alerts"])),
        row("Notification precision", lambda s, e: _precision(e["release"])),
        row("Notification recall", lambda s, e: _ratio(e["release"]["notification_recall"])),
        row("Records examined", lambda s, e: _records(e["release"], s, total)),
        row("Patients re-evaluated", lambda s, e: f"{e['release']['patients_reevaluated']:,} of {patients:,}"),
        row(
            f"Time per release ({patients:,} patients)" + (f", median [min–max] of {repeats}" if repeats > 1 else ""),
            lambda s, e: _ms(e["release"]["timing_ms"], repeats),
        ),
        [
            "Point-in-time correctness (S1–S12, correctness cohort)",
            *(_ratio(correctness["strategies"][strategy]["point_in_time_correctness"]) for strategy in STRATEGIES),
        ],
        row("`alerts_as_of` reproduces the pre-release state", lambda s, e: _as_of(e, "before_release")),
        row("Final state equals ground truth", lambda s, e: _yes(e["release"]["final_state_matches_ground_truth"])),
    ]
    return _table(["Metric", *(STRATEGY_LABELS[strategy] for strategy in STRATEGIES)], rows)


def _rule_table(cohort: Mapping[str, Any], repeats: int) -> list[str]:
    entries = cohort["strategies"]
    total = cohort["baseline"]["counts"]["patient_variants"]
    rows = [
        [
            "Newly indicated alerts raised",
            *(_ratio(entries[s]["rule_revision"]["re_alert_sensitivity"]) for s in STRATEGIES),
        ],
        ["Stale alerts after revision", *(_number(entries[s]["rule_revision"]["stale_alerts"]) for s in STRATEGIES)],
        ["Missed alerts after revision", *(_number(entries[s]["rule_revision"]["missed_alerts"]) for s in STRATEGIES)],
        ["Notification precision", *(_precision(entries[s]["rule_revision"]) for s in STRATEGIES)],
        ["Notification recall", *(_ratio(entries[s]["rule_revision"]["notification_recall"]) for s in STRATEGIES)],
        [
            "Records examined",
            *(
                _records(entries[s]["rule_revision"], s, total, "carriers of the revised rule's genes")
                for s in STRATEGIES
            ),
        ],
        [
            "Time per revision" + (f", median [min–max] of {repeats}" if repeats > 1 else ""),
            *(_ms(entries[s]["rule_revision"]["timing_ms"], repeats) for s in STRATEGIES),
        ],
        [
            "`alerts_as_of` reproduces the pre-revision state",
            *(_as_of(entries[s], "before_rule_revision") for s in STRATEGIES),
        ],
        [
            "Final state equals ground truth",
            *(_yes(entries[s]["rule_revision"]["final_state_matches_ground_truth"]) for s in STRATEGIES),
        ],
    ]
    return _table(["Metric", *(STRATEGY_LABELS[strategy] for strategy in STRATEGIES)], rows)


def _category_table(cohort: Mapping[str, Any]) -> list[str]:
    rows = []
    for row in cohort["categories"].values():
        cells = [
            row["label"],
            _number(row["carried_keys"]),
            _number(row["carrier_pairs"]),
            _number(row["expected_new_alerts"]),
            _number(row["expected_removed_alerts"]),
            _number(row["expected_notifications"]),
        ]
        for strategy in STRATEGIES:
            observed = row["strategies"][strategy]
            cells.append(
                f"{observed['alerts_created']} new / {observed['alerts_superseded']} superseded; "
                f"stale {observed['stale_alerts']}, missed {observed['missed_alerts']}; "
                f"notifications {observed['correct_notifications']}/{observed['notifications']} correct"
            )
        rows.append(cells)
    header = [
        "Change category",
        "Carried keys",
        "Carrier pairs",
        "Expected new alerts",
        "Expected removed alerts",
        "Expected notifications",
        *(STRATEGY_LABELS[strategy] for strategy in STRATEGIES),
    ]
    return _table(header, rows)


def _scenario_table(correctness: Mapping[str, Any]) -> list[str]:
    checks = {
        strategy: {check["code"]: check for check in correctness["strategies"][strategy]["scenarios"]}
        for strategy in STRATEGIES
    }
    rows = []
    for code, (scenario, expected) in metrics.TABLE_V.items():
        cells = [code, scenario, expected]
        for strategy in STRATEGIES:
            check = checks[strategy].get(code)
            cells.append("n/a" if check is None else f"{'pass' if check['passed'] else 'FAIL'}: {check['observed']}")
        rows.append(cells)
    return _table(["ID", "Scenario", "Expected", *(STRATEGY_LABELS[strategy] for strategy in STRATEGIES)], rows)


def _knowledge_section(synthetic: Mapping[str, Any]) -> list[str]:
    kb = synthetic["knowledge"]
    base, new = kb["base_release"], kb["new_release"]
    lines = [
        "## Knowledge changes",
        "",
        f"**Base release K_old** — {base['label']}: {base['records']:,} synthetic ClinVar-format records "
        f"(`##ge_ehr_synthetic=true`; sha256 `{base['sha256'][:16]}…`). "
        f"**K_new** — {new['label']}: {new['records']:,} records (sha256 `{new['sha256'][:16]}…`).",
        "",
        *_table(
            ["Gene", *C.GENES],
            [
                ["K_old records", *(_number(base["genes"][gene]) for gene in C.GENES)],
                ["Expert-panel records", *(_number(base["expert_panel_by_gene"][gene]) for gene in C.GENES)],
            ],
        ),
        "",
        *_table(
            ["Class", *C.CLASSES],
            [
                ["K_old records", *(_number(base["classes"][cls]) for cls in C.CLASSES)],
                ["K_new records", *(_number(new["classes"][cls]) for cls in C.CLASSES)],
            ],
        ),
        "",
        "KCNJ11/ABCC8 classified records by inferred mechanism (K_old): "
        + ", ".join(f"{name} {count:,}" for name, count in base["katp_mechanisms"].items())
        + ". Raw `Conflicting_*` significances (both wordings) are normalised to VUS.",
        "",
        "**Controlled change set** (keys carried by at least one patient of either cohort vs. carried by nobody):",
        "",
    ]
    kinds = kb["change_set"]["kinds"]
    lines += _table(
        ["Change", "Category", "Carried", "Carried by nobody"],
        [
            [kind["label"], kind["group"].replace("_", " "), _number(kind["carried"]), _number(kind["uncarried"])]
            for kind in kinds.values()
        ],
    )
    lines += [
        "",
        f"The background polymorphism `{kb['background_pool']['changed_key']}` (likely benign → benign) is one of the "
        f"carried within-non-actionable changes. **Rule revision:** {kb['rule_revision']}.",
        "",
    ]
    return lines


def _cohort_section(name: str, cohort: Mapping[str, Any]) -> list[str]:
    summary, baseline = cohort["cohort"], cohort["baseline"]
    initial = baseline["initial_state_vs_ground_truth"]
    timings = ", ".join(f"{key.replace('_', ' ')} {value:,.0f} ms" for key, value in baseline["timings_ms"].items())
    loaded = "VCF upload (F1)" if baseline["ingest"] == "vcf" else "bulk insert + first activation of K_old"
    return [
        f"**{name}:** {summary['patients']:,} patients ({summary['families']} families, "
        f"{summary['patients_in_families']} patients in families), "
        f"{summary['carriers_of_selected_variants']:,} carriers of {summary['selected_keys']:,} selected variants, "
        f"{summary['patient_variant_records']:,} patient-variant records over {summary['distinct_panel_keys']:,} "
        f"distinct keys; {summary['overrides']:,} clinical overrides on {summary['patients_with_overrides']:,} "
        f"patients (listed in `results.json`). Loaded via {loaded} — {timings}. "
        f"Initial state equals the ground truth under K_old + rules v1: {_yes(initial['matches'])} "
        f"({initial['findings']:,} findings).",
        "",
    ]


def table_vi_expectations(cohort: Mapping[str, Any], correctness: Mapping[str, Any]) -> list[dict[str, Any]]:
    """The expected outcomes of paper Table VI per strategy, with the measured value and whether it is met.

    ``cohort`` is the scale cohort's results (release change), ``correctness`` the correctness
    cohort's (point-in-time scenarios). "Changed only" means change-driven re-evaluation examined
    exactly the patient-variant records of the changed stored keys (and fewer than full).
    """
    release = {strategy: cohort["strategies"][strategy]["release"] for strategy in STRATEGIES}
    alert_once, full, change = release["alert_once"], release["full"], release["change_driven"]
    total = cohort["baseline"]["counts"]["patient_variants"]
    point = {strategy: correctness["strategies"][strategy]["point_in_time_correctness"] for strategy in STRATEGIES}
    sensitivity = {strategy: metric["re_alert_sensitivity"] for strategy, metric in release.items()}
    precision = {strategy: metric["notification_precision"] for strategy, metric in release.items()}
    changed = change["delta"]
    rows: list[tuple[str, str, str, str, bool]] = [
        (
            "Re-alert sensitivity",
            "alert_once",
            "0 %",
            _ratio(sensitivity["alert_once"]),
            sensitivity["alert_once"]["numerator"] == 0 < sensitivity["alert_once"]["denominator"],
        ),
        *(
            (
                "Re-alert sensitivity",
                strategy,
                "100 %",
                _ratio(sensitivity[strategy]),
                sensitivity[strategy]["value"] == 1.0,
            )
            for strategy in ("full", "change_driven")
        ),
        (
            "Stale alerts after release",
            "alert_once",
            "all affected",
            _stale(alert_once),
            alert_once["stale_alerts"] == alert_once["affected_alerts"] > 0,
        ),
        *(
            (
                "Stale alerts after release",
                strategy,
                "0",
                _stale(release[strategy]),
                release[strategy]["stale_alerts"] == 0,
            )
            for strategy in ("full", "change_driven")
        ),
        (
            "Notification precision",
            "alert_once",
            "n/a",
            _precision(alert_once),
            precision["alert_once"]["denominator"] == 0,
        ),
        *(
            (
                "Notification precision",
                strategy,
                "100 %",
                _precision(release[strategy]),
                precision[strategy]["value"] == 1.0,
            )
            for strategy in ("full", "change_driven")
        ),
        (
            "Records examined",
            "alert_once",
            "0",
            _number(alert_once["records_examined"]),
            alert_once["records_examined"] == 0,
        ),
        (
            "Records examined",
            "full",
            "all",
            f"{full['records_examined']:,} of {total:,}",
            full["records_examined"] == total,
        ),
        (
            "Records examined",
            "change_driven",
            "changed only",
            f"{change['records_examined']:,} = records of the {changed['recorded']:,} changed stored keys",
            change["records_examined"] == changed["carrier_records"] < full["records_examined"],
        ),
        (
            "Time per release",
            "change_driven",
            "lower than full",
            f"median {change['timing_ms']['median']:,.1f} ms vs {full['timing_ms']['median']:,.1f} ms",
            change["timing_ms"]["median"] < full["timing_ms"]["median"],
        ),
        *(
            ("Point-in-time correctness", strategy, "100 %", _ratio(point[strategy]), point[strategy]["value"] == 1.0)
            for strategy in STRATEGIES
        ),
    ]
    return [
        {"metric": metric, "strategy": strategy, "expected": expected, "measured": measured, "met": met}
        for metric, strategy, expected, measured, met in rows
    ]


def _expectations(expectations: Sequence[Mapping[str, Any]]) -> list[str]:
    """The comparison of the measurements with paper Table VI as Markdown."""
    met = sum(row["met"] for row in expectations)
    lines = [f"Paper Table VI expectations met: **{met} of {len(expectations)}**.", ""]
    lines += _table(
        ["Metric", "Strategy", "Expected (paper)", "Measured", "Met"],
        [
            [
                row["metric"],
                STRATEGY_LABELS[row["strategy"]],
                row["expected"],
                row["measured"],
                "yes" if row["met"] else "**no**",
            ]
            for row in expectations
        ],
    )
    return lines + [""]


def _real_pair_section(real: Mapping[str, Any], repeats: int) -> list[str]:
    lines = ["## Real ClinVar release pair", ""]
    if real["status"] != "evaluated":
        return lines + [f"Real ClinVar release pair: {real['reason']}.", ""]
    old, new = real["releases"]["old"], real["releases"]["new"]
    cohort = real["cohort"]
    lines += [
        f"{old['file']} ({old['records']:,} records) → {new['file']} ({new['records']:,} records); "
        f"natural changes per category: "
        + ", ".join(
            f"{name.replace('_', ' ')} {value['carried'] + value['uncarried']:,}"
            for name, value in real["changes"]["categories"].items()
        )
        + ".",
        "",
        *_cohort_section("Real-pair cohort", cohort),
    ]
    entries = cohort["strategies"]
    total = cohort["baseline"]["counts"]["patient_variants"]
    rows = [
        ["Re-alert sensitivity", *(_ratio(entries[s]["release"]["re_alert_sensitivity"]) for s in STRATEGIES)],
        ["Stale alerts after release", *(_stale(entries[s]["release"]) for s in STRATEGIES)],
        ["Missed alerts", *(_number(entries[s]["release"]["missed_alerts"]) for s in STRATEGIES)],
        ["Notification precision", *(_precision(entries[s]["release"]) for s in STRATEGIES)],
        ["Records examined", *(_records(entries[s]["release"], s, total) for s in STRATEGIES)],
        ["Time per release", *(_ms(entries[s]["release"]["timing_ms"], repeats) for s in STRATEGIES)],
    ]
    return lines + _table(["Metric", *(STRATEGY_LABELS[s] for s in STRATEGIES)], rows) + [""]


def render_markdown(results: Mapping[str, Any]) -> str:
    """The human-readable report ``RESULTS.md``."""
    env = results["environment"]
    synthetic = results["synthetic"]
    scale, correctness = synthetic["cohorts"]["scale"], synthetic["cohorts"]["correctness"]
    repeats = results["options"]["repeats"]
    patients = scale["baseline"]["counts"]["patients"]
    lines = [
        "# GE-EHR evaluation results",
        "",
        "> **Synthetic data only.** Synthea patients with documented clinical overrides and a synthetic "
        "ClinVar-format knowledge base — not real ClinVar data, not for clinical use.",
        "",
        f"Generated {results['generated_at']} by `{results['command']}` in {results['duration_s']:,.0f} s "
        f"(Python {env['python']}, SQLite {env['sqlite']}, SQLAlchemy {env['sqlalchemy']}, {env['cpu_count']} CPUs, "
        f"{env['platform']}). Raw numbers: [`results.json`](results.json); methodology: "
        "[`docs/EVALUATION.md`](../../docs/EVALUATION.md).",
        "",
        f"## Table VI — measured (controlled change set, scale cohort of {patients:,} patients)",
        "",
        *_table_vi(scale, correctness, repeats),
        "",
        "Re-alert sensitivity counts (patient, variant) pairs of carriers of upgraded variants with a new current "
        "alert from that variant. Stale = current alerts not in the ground truth for K_new; affected = alerts the "
        "change made obsolete. Times are wall-clock milliseconds of the activation call (excluding the commit).",
        "",
        *_expectations(synthetic["table_vi_expectations"]),
        "## Rule revision (D4 v1 → v2 adds ABCC8), scale cohort",
        "",
        *_rule_table(scale, repeats),
        "",
        "The rule revision follows the release, so its ground truth is K_new + rules v2; the alert-once baseline "
        "still shows the stale alerts it kept from the release.",
        "",
        "## Per-category breakdown of the release (scale cohort)",
        "",
        *_category_table(scale),
        "",
        "## Correctness cohort (Table V scenarios)",
        "",
        *_cohort_section("Correctness cohort", correctness),
        *_table_vi(correctness, correctness, 1),
        "",
        *_scenario_table(correctness),
        "",
        "S1–S9 are checked with `alerts_as_of(patient, T0)` after both knowledge changes, i.e. what the system "
        "recommended before them. RS1–RS6 describe re-evaluation, which the alert-once baseline does not perform, "
        "so it fails them by design.",
        "",
        "## Cohorts",
        "",
        *_cohort_section("Scale cohort", scale),
        *_knowledge_section(synthetic),
        "## Timing runs (scale cohort)",
        "",
        *_table(
            ["Strategy", "Release runs (ms)", "Rule revision runs (ms)", "Outcomes identical across runs"],
            [
                [
                    STRATEGY_LABELS[s],
                    ", ".join(f"{value:,.1f}" for value in scale["strategies"][s]["release"]["timing_ms"]["runs"]),
                    ", ".join(
                        f"{value:,.1f}" for value in scale["strategies"][s]["rule_revision"]["timing_ms"]["runs"]
                    ),
                    _yes(scale["strategies"][s]["outcomes_identical_across_repeats"]),
                ]
                for s in STRATEGIES
            ],
        ),
        "",
        *_real_pair_section(results["real_pair"], repeats),
        "## Reproduce",
        "",
        "```sh",
        f".venv/bin/python -m evaluation.run {' '.join(_command_arguments_from(results['options']))}",
        "```",
        "",
    ]
    return "\n".join(lines)


def _command_arguments_from(options: Mapping[str, Any]) -> list[str]:
    return _command_arguments(
        Options(
            scale=options["scale"],
            correctness=options["correctness"],
            seed=options["seed"],
            kb_size=options["kb_size"],
            repeats=options["repeats"],
        )
    )


def write_results(results: Mapping[str, Any], out: Path) -> tuple[Path, Path]:
    """Write ``results.json`` and ``RESULTS.md`` to ``out``; returns both paths."""
    out.mkdir(parents=True, exist_ok=True)
    json_path, markdown_path = out / "results.json", out / "RESULTS.md"
    json_path.write_text(json.dumps(results, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    markdown_path.write_text(render_markdown(results), encoding="utf-8")
    return json_path, markdown_path


def main(argv: Sequence[str] | None = None) -> int:
    """Command-line entry point; returns the process exit status."""
    options = parse_args(argv)
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter("%(asctime)s %(message)s", "%H:%M:%S"))
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    logger.propagate = False
    try:
        results = run_evaluation(options)
        json_path, markdown_path = write_results(results, options.out)
        logger.info("Wrote %s and %s", _display_path(json_path), _display_path(markdown_path))
    except (ValueError, OSError) as exc:
        logger.error("Evaluation failed: %s", exc)
        return 1
    finally:
        logger.removeHandler(handler)
    return 0


if __name__ == "__main__":
    sys.exit(main())
