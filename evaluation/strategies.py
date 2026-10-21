"""Cohort preparation, isolated databases and the three evaluated strategies (SPEC §10, paper §5).

**Cohort preparation** (pure, deterministic per seed). :func:`plan_correctness_cohort` and
:func:`plan_scale_cohort` turn Synthea cohorts (:mod:`evaluation.cohort`) into a
:class:`CohortPlan`: every patient gets 1–3 common benign background variants
(:class:`BackgroundPool`); a share of patients carry a *selected* knowledge-base variant whose
*role* says what the controlled change set will do with it (a :data:`~evaluation.knowledge.CHANGE_KINDS`
change or one of the :data:`STABLE_ROLES`); families formed with
:func:`~evaluation.cohort.make_families` share their variant. Synthea's cohorts hold almost no
monogenic phenotypes (no sulfonylurea users at all, one diabetic patient among the 30), so
carriers get documented **clinical overrides** (:class:`Override`, all reported in
``results.json``): MODY-gene carriers a diabetes diagnosis when they had none and a therapy drawn
from :data:`GENE_PROFILE_MIX` (HNF1A/HNF4A carriers on insulin, metformin or already on a
sulfonylurea), activating KCNJ11/ABCC8 carriers a neonatal diagnosis (< 12 months) with insulin or
glibenclamide. Relatives are unaffected (left as Synthea made them) with probability
:data:`UNAFFECTED_RELATIVE_SHARE`. The correctness cohort additionally maps patients to the
scenarios of paper Table V (S1–S9, RS1–RS7) exactly as SPEC §11 describes the demo patients.

**Databases.** :func:`build_baseline` creates an isolated app (:func:`evaluation_app`) on a fresh
SQLite *file*, imports rules v1 and K_old and loads the cohort: the correctness cohort through
the real F1 path (``patient_records.create_patient`` / ``add_medication`` / ``ingest_vcf`` of a
per-patient VCF, evaluated at upload under K_old), the scale cohort with bulk inserts of
patients, diagnoses, medications and PatientVariants (no VCF parsing and no GenomicUpload rows,
for speed) followed by the first activation of K_old (initial classification, SPEC §7.1 step 5).
K_new is imported (not activated) last, so every strategy starts from a byte-identical copy and
the import cost is excluded from the timings.

**Strategies** (:func:`run_strategy`, each on its own copy): ``alert_once`` marks K_new current and
switches the rule versions without any re-evaluation (an EHR that evaluated once); ``full``
calls ``activate_release(strategy="full")`` and ``apply_rule_set(strategy="full")``;
``change_driven`` (GE-EHR) calls the same entry points with ``strategy="change_driven"``. The
knowledge changes are K_old → K_new, then rules v1 → v2 (D4 adds ABCC8). Each change is timed
(wall clock of the call) and its outcome captured (:class:`ChangeOutcome`); the point-in-time
view is checked against snapshots taken before each change.

Owned by the Eval work package.
"""

from __future__ import annotations

import logging
import random
import secrets
import shutil
import time
from collections import Counter, defaultdict
from collections.abc import Callable, Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import date, datetime, timedelta
from functools import cached_property
from itertools import batched
from pathlib import Path
from typing import Any, NamedTuple

from flask import Flask
from sqlalchemy import func, insert, or_, select, update

from ge_ehr import constants as C
from ge_ehr import create_app
from ge_ehr.extensions import db
from ge_ehr.models import (
    RUN_COUNTERS,
    Alert,
    Diagnosis,
    KbRelease,
    Medication,
    Patient,
    PatientVariant,
    ReevaluationRun,
    ReviewNotice,
    VariantChange,
    VariantKey,
    utcnow,
)
from ge_ehr.services import alerting, clinvar, patient_records, reevaluation
from ge_ehr.services.clinvar import ClinVarRecord

from . import knowledge
from .cohort import REFERENCE_DATE, CohortMedication, CohortPatient, VcfVariant, format_patient_vcf, make_families, months_between
from .knowledge import CHANGE_KINDS, KATP_GENES, Key, KnownRecord, key_label

logger = logging.getLogger(__name__)

STRATEGIES: tuple[str, ...] = ("alert_once", "full", "change_driven")
STRATEGY_LABELS: dict[str, str] = {
    "alert_once": "Alert-once",
    "full": "Full recomputation",
    "change_driven": "GE-EHR (change-driven)",
}
CHANGES: tuple[str, ...] = ("release", "rule_revision")

# ---------------------------------------------------------------------------
# Roles of selected variants
# ---------------------------------------------------------------------------

STABLE_ROLES: dict[str, str] = {
    "stable_p": "pathogenic, unchanged",
    "stable_lp": "likely pathogenic, unchanged",
    "stable_vus": "VUS, unchanged",
    "stable_katp_activating": "activating KCNJ11/ABCC8 P/LP, unchanged",
    "stable_novel": "private variant absent from ClinVar (NC), unchanged",
}
NOVEL_ROLES: frozenset[str] = frozenset({"nc_to_lp", "stable_novel"})
CATEGORY_ROLE_LABELS: dict[str, str] = {category: label for category, label in C.CHANGE_CATEGORY_LABELS.items()}

# Gene of a selected variant (monogenic-diabetes carriers: GCK and HNF1A dominate).
CARRIER_GENE_WEIGHTS: dict[str, float] = {
    "GCK": 0.30,
    "HNF1A": 0.30,
    "HNF4A": 0.12,
    "HNF1B": 0.08,
    "KCNJ11": 0.07,
    "ABCC8": 0.08,
    "INS": 0.05,
}

# Selected keys per role for a cohort of 10,000 patients (scaled linearly, at least 1 each).
SCALE_ROLE_COUNTS: dict[str, int] = {
    "vus_to_lp": 30,
    "vus_to_p": 10,
    "nc_to_lp": 10,
    "lp_to_vus": 15,
    "p_to_vus": 6,
    "p_removed": 4,
    "lp_to_p": 15,
    "p_to_lp": 5,
    "katp_activating": 6,
    "vus_to_lb": 20,
    "lb_to_b": 12,
    "vus_to_conflicting": 8,
    "stable_p": 70,
    "stable_lp": 60,
    "stable_vus": 250,
    "stable_katp_activating": 24,
    "stable_novel": 40,
}
# For a real release pair the changes are natural, so carried keys are chosen by change category.
REAL_PAIR_ROLE_COUNTS: dict[str, int] = {
    "upgrade": 40,
    "downgrade": 20,
    "within_actionable": 20,
    "within_non_actionable": 40,
    "stable_p": 70,
    "stable_lp": 60,
    "stable_vus": 250,
    "stable_katp_activating": 24,
    "stable_novel": 40,
}
SCALE_REFERENCE_SIZE = 10_000
FAMILIES_PER_PATIENT = 0.012  # 120 families of 2–5 relatives in 10,000 patients
CORRECTNESS_FAMILIES = 2  # the RS6 family and one more
# One generic correctness patient (or family) per role, so every change kind occurs at least once.
CORRECTNESS_COVERAGE_ROLES: tuple[str, ...] = (
    "vus_to_p",
    "nc_to_lp",
    "p_to_vus",
    "p_removed",
    "p_to_lp",
    "katp_activating",
    "lb_to_b",
    "vus_to_conflicting",
    "stable_vus",
    "stable_novel",
    "stable_lp",
    "stable_katp_activating",
    "stable_p",
)

# ---------------------------------------------------------------------------
# Clinical profiles and overrides
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Profile:
    """Target clinical state of a carrier: one current drug class (None: untreated), neonatal onset."""

    name: str
    drug_class: str | None
    neonatal: bool = False


PROFILES: dict[str, Profile] = {
    profile.name: profile
    for profile in (
        Profile("untreated", None),
        Profile("metformin", "metformin"),
        Profile("insulin", "insulin"),
        Profile("sulfonylurea", "sulfonylurea"),
        Profile("neonatal_insulin", "insulin", neonatal=True),
        Profile("neonatal_sulfonylurea", "sulfonylurea", neonatal=True),
    )
}
GENE_PROFILE_MIX: dict[str, tuple[tuple[str, float], ...]] = {
    "GCK": (("untreated", 0.45), ("metformin", 0.30), ("insulin", 0.25)),
    "HNF1A": (("insulin", 0.40), ("metformin", 0.30), ("sulfonylurea", 0.15), ("untreated", 0.15)),
    "HNF4A": (("insulin", 0.40), ("metformin", 0.30), ("sulfonylurea", 0.15), ("untreated", 0.15)),
    "HNF1B": (("insulin", 0.55), ("metformin", 0.25), ("untreated", 0.20)),
    "INS": (("insulin", 0.80), ("untreated", 0.20)),
}
KATP_ACTIVATING_PROFILE_MIX: tuple[tuple[str, float], ...] = (("neonatal_insulin", 0.75), ("neonatal_sulfonylurea", 0.25))
UNAFFECTED_RELATIVE_SHARE = 0.35
NEONATAL_ONSET_MAX_MONTHS = 9
MODY_ONSET_MONTHS = (120, 420)  # typical onset 10–35 years
CHILDHOOD_LIMIT_MONTHS = 18 * 12

# Synthea's own product names for insulin and metformin; sulfonylureas never occur in Synthea.
DRUG_NAMES: dict[str, str] = {
    "insulin": "insulin isophane human 70 UNT/ML / insulin regular human 30 UNT/ML Injectable Suspension [Humulin]",
    "metformin": "24 HR Metformin hydrochloride 500 MG Extended Release Oral Tablet",
    "sulfonylurea": "Gliclazide 80 MG Oral Tablet",
}
NEONATAL_SULFONYLUREA = "Glibenclamide 2.5 MG Oral Tablet"


@dataclass(frozen=True)
class Override:
    """A documented change of Synthea's clinical data for an evaluation scenario."""

    mrn: str
    synthetic_id: str
    field: str  # "diagnosis" | "medications"
    old: Any
    new: Any
    reason: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "mrn": self.mrn,
            "synthetic_id": self.synthetic_id,
            "field": self.field,
            "old": self.old,
            "new": self.new,
            "reason": self.reason,
        }


# ---------------------------------------------------------------------------
# Correctness scenarios (paper Table V, SPEC §11)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ScenarioKey:
    """A knowledge-base key a scenario needs (picked from the base release by these criteria)."""

    name: str
    gene: str
    cls: str
    change: str | None = None  # change kind injected into K_new
    mechanism: str | None = None
    raw: str | None = None  # required raw CLNSIG
    prefer_expert: bool = False
    carried: bool = True


SCENARIO_KEYS: tuple[ScenarioKey, ...] = (
    ScenarioKey("V01", "HNF1A", "P", raw="Pathogenic", prefer_expert=True),
    ScenarioKey("V02", "GCK", "P", raw="Pathogenic", prefer_expert=True),
    ScenarioKey("V03", "HNF4A", "LP", change="lp_to_p"),
    ScenarioKey("V04", "KCNJ11", "P", mechanism="activating"),
    ScenarioKey("V05", "INS", "LP"),
    ScenarioKey("V06", "HNF1A", "VUS", change="vus_to_lp", raw="Uncertain_significance"),
    ScenarioKey("V07", "GCK", "LP", change="lp_to_vus"),
    ScenarioKey("V08", "HNF1A", "VUS", change="vus_to_lb", raw="Uncertain_significance"),
    ScenarioKey("V09", "ABCC8", "P", mechanism="activating"),
    ScenarioKey("V10", "HNF4A", "VUS", change="vus_to_lp", raw="Uncertain_significance"),
    ScenarioKey("V11", "GCK", "VUS", change="vus_to_p", raw="Uncertain_significance", carried=False),
)
# Background keys the scenarios name (taken from the background pool) and the off-panel X01.
REQUIRED_BACKGROUND: tuple[tuple[str, str, str], ...] = (
    ("B01", "HNF1A", "B"),
    ("B02", "GCK", "LB"),
    ("B03", "HNF1B", "B"),
    ("B04", "ABCC8", "LB"),
)
OFF_PANEL_KEY: Key = ("1", 1_000_000, "A", "G")


@dataclass(frozen=True)
class Scenario:
    """A correctness-cohort patient of SPEC §11: variants and exact clinical state."""

    code: str
    keys: tuple[str, ...]
    diabetes_type: str
    age_at_diagnosis_months: int | None
    drug_class: str | None
    description: str


SCENARIOS: tuple[Scenario, ...] = (
    Scenario("S1", ("V01", "B01"), "type1", 180, "insulin", "HNF1A pathogenic, on insulin"),
    Scenario("S2", ("V01",), "type2", 300, "sulfonylurea", "HNF1A pathogenic, already on a sulfonylurea"),
    Scenario("S3", ("V02",), "type2", 360, "metformin", "GCK pathogenic, on metformin"),
    Scenario("S4", ("V02", "B02"), "other", 240, None, "GCK pathogenic, no medication"),
    Scenario("S5", ("V03",), "type2", 264, "metformin", "HNF4A likely pathogenic, on metformin (RS3: LP → P)"),
    Scenario("S6", ("V04",), "neonatal", 2, "insulin", "KCNJ11 pathogenic, diagnosed in infancy, on insulin"),
    Scenario("S7", ("V05",), "type1", 96, "insulin", "actionable INS variant"),
    Scenario("S8", ("V06", "B03"), "type1", 192, "insulin", "HNF1A VUS on insulin (RS1: VUS → LP)"),
    Scenario("S9", ("B01", "B02", "B04", "X01"), "type2", 480, "metformin", "only benign variants (+ one off panel)"),
    Scenario("RS2", ("V07",), "type2", 420, "metformin", "GCK likely pathogenic downgraded to VUS"),
    Scenario("RS4", ("V08",), "type1", 150, "insulin", "HNF1A VUS reclassified as likely benign"),
    Scenario("RS5", ("V09",), "neonatal", 3, "insulin", "ABCC8 pathogenic, neonatal, on insulin (D4 v2)"),
)
RS6_KEY = "V10"
RS6_MEMBER_STATES: tuple[tuple[str, int | None, str | None], ...] = (
    ("type2", 324, "metformin"),
    ("type1", 168, "insulin"),
    ("none", None, None),
)


# ---------------------------------------------------------------------------
# Plans
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PlannedPatient:
    """A patient of an evaluation cohort: clinical data (after overrides) and VCF variants."""

    mrn: str
    patient: CohortPatient
    variants: tuple[VcfVariant, ...]
    scenario: str | None = None

    @property
    def panel_variants(self) -> tuple[VcfVariant, ...]:
        """The variants inside the 7-gene panel (what VCF parsing keeps)."""
        return tuple(variant for variant in self.variants if C.gene_for_position(variant[0], variant[1]))

    @property
    def panel_keys(self) -> frozenset[Key]:
        return frozenset(variant[:4] for variant in self.panel_variants)


@dataclass(frozen=True)
class CohortPlan:
    """A prepared evaluation cohort."""

    name: str
    patients: tuple[PlannedPatient, ...]
    roles: Mapping[Key, str]  # selected (non-background) carried key → role
    overrides: tuple[Override, ...]
    scenario_keys: Mapping[str, Key] = field(default_factory=dict)
    uncarried_changes: Mapping[Key, str] = field(default_factory=dict)  # scenario changes nobody carries (RS7)
    families: int = 0

    @cached_property
    def carriers(self) -> dict[Key, frozenset[str]]:
        """Panel key → MRNs of its carriers."""
        carriers: defaultdict[Key, set[str]] = defaultdict(set)
        for planned in self.patients:
            for key in planned.panel_keys:
                carriers[key].add(planned.mrn)
        return {key: frozenset(mrns) for key, mrns in carriers.items()}

    @property
    def carried_keys(self) -> frozenset[Key]:
        return frozenset(self.carriers)

    @property
    def selected_keys(self) -> frozenset[Key]:
        """Keys this plan assigned a role or a scenario to (excluded when planning another cohort)."""
        return frozenset(self.roles) | frozenset(self.scenario_keys.values())

    @property
    def forced_changes(self) -> dict[Key, str]:
        """Changes the knowledge change set must inject for this cohort."""
        forced = {key: role for key, role in self.roles.items() if role in CHANGE_KINDS}
        forced.update(self.uncarried_changes)
        return forced

    @cached_property
    def by_mrn(self) -> dict[str, PlannedPatient]:
        return {planned.mrn: planned for planned in self.patients}

    def scenario_patients(self, code: str) -> list[PlannedPatient]:
        return [planned for planned in self.patients if planned.scenario == code]

    def summary(self) -> dict[str, Any]:
        """Counts describing the cohort for ``results.json``."""
        diabetes = Counter(planned.patient.diabetes_type for planned in self.patients)
        drugs = Counter(m.drug_class for planned in self.patients for m in planned.patient.medications)
        selected = [planned for planned in self.patients if planned.panel_keys & self.roles.keys()]
        return {
            "patients": len(self.patients),
            "families": self.families,
            "patients_in_families": sum(planned.patient.family_id is not None for planned in self.patients),
            "carriers_of_selected_variants": len(selected),
            "selected_keys": len(self.roles),
            "roles": dict(sorted(Counter(self.roles.values()).items())),
            "patient_variant_records": sum(len(planned.panel_variants) for planned in self.patients),
            "distinct_panel_keys": len(self.carriers),
            "diabetes_types_after_overrides": dict(sorted(diabetes.items())),
            "current_medications_after_overrides": dict(sorted(drugs.items())),
            "overrides": len(self.overrides),
            "overrides_by_field": dict(sorted(Counter(item.field for item in self.overrides).items())),
            "patients_with_overrides": len({item.mrn for item in self.overrides}),
            "scenarios": {
                planned.mrn: planned.scenario for planned in self.patients if planned.scenario is not None
            },
        }


@dataclass(frozen=True)
class BackgroundPool:
    """Common benign polymorphisms everybody draws background variants from (Zipf-like frequencies)."""

    keys: tuple[Key, ...]
    weights: tuple[float, ...]
    changed_key: Key | None  # an LB background variant reclassified LB → B in K_new
    scenario_keys: Mapping[str, Key]  # B01–B04

    def draw(self, rng: random.Random) -> list[Key]:
        count = _weighted(rng, ((1, 0.50), (2, 0.35), (3, 0.15)))
        keys, weights = list(self.keys), list(self.weights)
        chosen: list[Key] = []
        for _ in range(count):
            index = _weighted(rng, list(zip(range(len(keys)), weights)))
            chosen.append(keys.pop(index))
            weights.pop(index)
        return chosen


BACKGROUND_POOL_SIZE = 48
BACKGROUND_CHANGED_RANK = 10  # the changed LB key is a mid-frequency polymorphism (~3 % of patients)
BACKGROUND_HOMOZYGOUS_SHARE = 0.2


def _weighted[T](rng: random.Random, options: Sequence[tuple[T, float]]) -> T:
    total = sum(weight for _option, weight in options)
    point = rng.random() * total
    for option, weight in options:
        point -= weight
        if point < 0:
            return option
    return options[-1][0]


def choose_background_pool(
    classes: Mapping[Key, KnownRecord],
    seed: int,
    *,
    eligible: Callable[[Key], bool] | None = None,
    with_changed_key: bool = True,
    size: int = BACKGROUND_POOL_SIZE,
) -> BackgroundPool:
    """Pick ``size`` benign (B/LB) keys, including the scenario keys B01–B04 (the most frequent).

    ``eligible`` restricts the candidates (e.g. to keys unchanged in a real release pair). With
    ``with_changed_key`` the first LB key at rank ≥ :data:`BACKGROUND_CHANGED_RANK` (not B01–B04)
    is designated to change LB → B in K_new. Raises ValueError when a required key is missing.
    """
    rng = random.Random(f"ge-ehr-background/{seed}")
    benign = sorted(
        key
        for key, known in classes.items()
        if known.classification in ("B", "LB") and (eligible is None or eligible(key))
    )
    chosen: list[Key] = []
    scenario_keys: dict[str, Key] = {}
    for name, gene, cls in REQUIRED_BACKGROUND:
        candidates = [key for key in benign if classes[key].gene == gene and classes[key].classification == cls]
        if not candidates:
            raise ValueError(f"The base release has no {gene} {cls} record for background variant {name}.")
        key = rng.choice(candidates)
        chosen.append(key)
        scenario_keys[name] = key
    remaining = [key for key in benign if key not in scenario_keys.values()]
    if len(remaining) < size - len(chosen):
        raise ValueError(f"The base release has only {len(benign)} benign keys; the background pool needs {size}.")
    chosen.extend(rng.sample(remaining, size - len(chosen)))
    changed = None
    if with_changed_key:
        changed = next(
            (key for key in chosen[BACKGROUND_CHANGED_RANK:] if knowledge.is_eligible("lb_to_b", classes[key])), None
        )
        if changed is None:
            raise ValueError("The background pool has no likely benign key to reclassify as benign.")
    weights = tuple(1.0 / (rank + 1) for rank in range(len(chosen)))
    return BackgroundPool(tuple(chosen), weights, changed, scenario_keys)


RolePools = dict[str, dict[str, list[Key]]]
"""role → gene → candidate keys (sorted)."""


def _stable_roles(known: KnownRecord) -> list[str]:
    roles = {"P": ["stable_p"], "LP": ["stable_lp"], "VUS": ["stable_vus"]}.get(known.classification, [])
    if known.gene in KATP_GENES and known.classification in C.ACTIONABLE and known.mechanism == "activating":
        roles.append("stable_katp_activating")
    return roles


def synthetic_role_pools(base: Sequence[ClinVarRecord], exclude: Collection[Key]) -> RolePools:
    """Candidate keys of the base release per role (change kinds by eligibility, stable roles by class)."""
    pools: defaultdict[str, defaultdict[str, list[Key]]] = defaultdict(lambda: defaultdict(list))
    excluded = set(exclude)
    for key, known in sorted(knowledge.classify_records(base).items()):
        if key in excluded:
            continue
        for kind in CHANGE_KINDS:
            if kind not in NOVEL_ROLES and knowledge.is_eligible(kind, known):
                pools[kind][known.gene].append(key)
        for role in _stable_roles(known):
            pools[role][known.gene].append(key)
    return {role: dict(genes) for role, genes in pools.items()}


def transition_role_pools(
    old: Mapping[Key, KnownRecord], new: Mapping[Key, KnownRecord], exclude: Collection[Key]
) -> RolePools:
    """Candidate keys of a real release pair: changed keys by category, unchanged keys by stable role."""
    pools: defaultdict[str, defaultdict[str, list[Key]]] = defaultdict(lambda: defaultdict(list))
    excluded = set(exclude)
    changed = knowledge.transitions(old, new)
    for key, (before, after) in sorted(changed.items()):
        if key in excluded:
            continue
        category = knowledge.change_category(
            before.classification if before else "NC",
            after.classification if after else "NC",
            before.mechanism if before else "unknown",
            after.mechanism if after else "unknown",
        )
        present = after if after is not None else before
        if category is not None and present is not None:
            pools[category][present.gene].append(key)
    for key, known in sorted(old.items()):
        if key not in changed and key not in excluded:
            for role in _stable_roles(known):
                pools[role][known.gene].append(key)
    return {role: dict(genes) for role, genes in pools.items()}


def role_label(role: str) -> str:
    """Human-readable role: ``"VUS → LP"``, ``"VUS, unchanged"``, ``"Upgrade into actionable band"``."""
    if role in CHANGE_KINDS:
        return CHANGE_KINDS[role].label
    return STABLE_ROLES.get(role) or CATEGORY_ROLE_LABELS.get(role, role)


def scaled_role_counts(counts: Mapping[str, int], patients: int) -> dict[str, int]:
    """Role counts for a cohort of ``patients`` (linear in :data:`SCALE_REFERENCE_SIZE`, at least 1)."""
    factor = patients / SCALE_REFERENCE_SIZE
    return {role: max(1, round(count * factor)) for role, count in counts.items()}


class _Planner:
    """Mutable state while a :class:`CohortPlan` is assembled."""

    def __init__(
        self,
        name: str,
        cohort: Sequence[CohortPatient],
        *,
        seed: int,
        mrn_prefix: str,
        n_families: int,
        background: BackgroundPool,
        taken: Iterable[Key],
    ) -> None:
        self.name = name
        self.rng = random.Random(f"ge-ehr-plan/{name}/{seed}")
        self.patients: list[CohortPatient] = make_families(cohort, n_families, seed)
        self.n_families = n_families
        self.mrns = tuple(f"{mrn_prefix}-{index:05d}" for index in range(1, len(self.patients) + 1))
        self.variants: list[dict[Key, str]] = [{} for _ in self.patients]
        self.scenarios: list[str | None] = [None] * len(self.patients)
        self.overrides: list[Override] = []
        self.roles: dict[Key, str] = {}
        self.scenario_keys: dict[str, Key] = {}
        self.uncarried: dict[Key, str] = {}
        self.background = background
        self.taken: set[Key] = set(taken) | set(background.keys)
        self.used: set[Key] = set(background.keys)

    # -- groups -------------------------------------------------------------

    def family_groups(self) -> list[list[int]]:
        """Indices of each family's members (in cohort order), families ordered by id."""
        groups: defaultdict[str, list[int]] = defaultdict(list)
        for index, patient in enumerate(self.patients):
            if patient.family_id is not None:
                groups[patient.family_id].append(index)
        return [groups[family_id] for family_id in sorted(groups)]

    def single_indices(self) -> list[int]:
        return [index for index, patient in enumerate(self.patients) if patient.family_id is None]

    # -- keys ---------------------------------------------------------------

    def gene(self, genes: Collection[str] | None = None) -> str:
        options = [(gene, weight) for gene, weight in CARRIER_GENE_WEIGHTS.items() if genes is None or gene in genes]
        return _weighted(self.rng, options)

    def pick_key(self, pools: RolePools, role: str) -> Key | None:
        """A not yet used candidate key for ``role`` (gene by :data:`CARRIER_GENE_WEIGHTS`), or None."""
        if role in NOVEL_ROLES:
            key = knowledge.novel_keys(self.gene(), 1, self.rng, self.taken)[0]
            self.used.add(key)
            return key
        genes = pools.get(role, {})
        available = {gene: [key for key in keys if key not in self.used] for gene, keys in genes.items()}
        available = {gene: keys for gene, keys in available.items() if keys}
        if not available:
            return None
        key = self.rng.choice(available[self.gene(available)])
        self.used.add(key)
        return key

    def pick_scenario_key(self, classes: Mapping[Key, KnownRecord], spec: ScenarioKey) -> Key:
        candidates = [
            key
            for key, known in sorted(classes.items())
            if key not in self.used
            and known.gene == spec.gene
            and known.classification == spec.cls
            and (spec.mechanism is None or known.mechanism == spec.mechanism)
            and (spec.raw is None or known.clnsig == spec.raw)
            and (spec.change is None or knowledge.is_eligible(spec.change, known))
        ]
        if spec.prefer_expert:
            candidates = [key for key in candidates if classes[key].is_expert_panel] or candidates
        if not candidates:
            raise ValueError(
                f"The base release has no {spec.gene} {spec.cls} record usable as scenario variant {spec.name}."
            )
        key = self.rng.choice(candidates)
        self.used.add(key)
        return key

    # -- patients -----------------------------------------------------------

    def carry(self, index: int, key: Key, zygosity: str = "heterozygous") -> None:
        self.variants[index][key] = zygosity

    def give_background(self, index: int) -> None:
        for key in self.background.draw(self.rng):
            if key not in self.variants[index]:
                homozygous = self.rng.random() < BACKGROUND_HOMOZYGOUS_SHARE
                self.carry(index, key, "homozygous" if homozygous else "heterozygous")

    def carrier_profile(self, gene: str, activating: bool, *, relative: bool) -> Profile | None:
        """The clinical profile of a carrier; None leaves Synthea's data unchanged."""
        if relative and self.rng.random() < UNAFFECTED_RELATIVE_SHARE:
            return None
        if gene in KATP_GENES:
            return PROFILES[_weighted(self.rng, KATP_ACTIVATING_PROFILE_MIX)] if activating else None
        return PROFILES[_weighted(self.rng, GENE_PROFILE_MIX[gene])]

    def apply_profile(self, index: int, profile: Profile, reason: str) -> None:
        patient = self.patients[index]
        age_now = months_between(patient.birth_date, REFERENCE_DATE)
        if profile.neonatal:
            self.set_diagnosis(index, "neonatal", min(self.rng.randint(0, NEONATAL_ONSET_MAX_MONTHS), age_now), reason)
        elif patient.diabetes_type in ("none", "unknown"):
            low, high = MODY_ONSET_MONTHS
            onset = self.rng.randint(low, high)
            if onset > age_now:
                onset = self.rng.randint(age_now // 2, age_now)
            kind = "type1" if profile.drug_class == "insulin" and onset < CHILDHOOD_LIMIT_MONTHS else "type2"
            self.set_diagnosis(index, kind, onset, reason)
        name = None
        if profile.drug_class is not None:
            neonatal_su = profile.neonatal and profile.drug_class == "sulfonylurea"
            name = NEONATAL_SULFONYLUREA if neonatal_su else DRUG_NAMES[profile.drug_class]
        self.set_therapy(index, profile.drug_class, name, reason)

    def set_diagnosis(self, index: int, diabetes_type: str, age: int | None, reason: str) -> None:
        patient = self.patients[index]
        if diabetes_type == "none":
            age = None
        elif age is not None:
            age = min(age, months_between(patient.birth_date, REFERENCE_DATE))
        old = {"diabetes_type": patient.diabetes_type, "age_at_diagnosis_months": patient.age_at_diagnosis_months}
        new = {"diabetes_type": diabetes_type, "age_at_diagnosis_months": age}
        if old != new:
            self.patients[index] = replace(patient, diabetes_type=diabetes_type, age_at_diagnosis_months=age)
            self.overrides.append(Override(self.mrns[index], patient.synthetic_id, "diagnosis", old, new, reason))

    def set_therapy(self, index: int, drug_class: str | None, drug_name: str | None, reason: str) -> None:
        """Make ``drug_class`` the only current glucose-lowering therapy (None: no medication)."""
        patient = self.patients[index]
        target = frozenset({drug_class}) if drug_class else frozenset()
        if frozenset(medication.drug_class for medication in patient.medications) == target:
            return
        medications: list[CohortMedication] = []
        if drug_class is not None and drug_name is not None:
            if C.suggest_drug_class(drug_name) != drug_class:
                raise ValueError(f"Drug name {drug_name!r} does not map to the drug class {drug_class!r}.")
            medications.append(CohortMedication(drug_name, drug_class, self._start_date(patient)))
        self.patients[index] = replace(patient, medications=medications)
        self.overrides.append(
            Override(
                self.mrns[index],
                patient.synthetic_id,
                "medications",
                [_medication_dict(medication) for medication in patient.medications],
                [_medication_dict(medication) for medication in medications],
                reason,
            )
        )

    def _start_date(self, patient: CohortPatient) -> date:
        diagnosed = patient.birth_date + timedelta(days=round((patient.age_at_diagnosis_months or 0) * 30.44))
        start = diagnosed + timedelta(days=self.rng.randint(0, 365))
        return max(patient.birth_date, min(start, REFERENCE_DATE))

    def set_clinical(self, index: int, diabetes_type: str, age: int | None, drug_class: str | None, reason: str) -> None:
        """Exact clinical state of a scenario patient."""
        self.set_diagnosis(index, diabetes_type, age, reason)
        self.set_therapy(index, drug_class, DRUG_NAMES[drug_class] if drug_class else None, reason)

    def assign(
        self,
        selections: Sequence[tuple[str, Key]],
        activating_keys: Collection[Key],
        groups: Sequence[Sequence[int]] | None = None,
    ) -> list[int]:
        """Give each selected key to a carrier group and apply the carriers' profiles.

        Without ``groups`` the families of the cohort take random keys and the other keys go to
        single patients drawn at random; with ``groups`` the i-th selection goes to the i-th group.
        Returns the indices of all carriers. Raises ValueError when the cohort is too small.
        """
        if groups is None:
            families = self.family_groups()
            singles = self.single_indices()
            self.rng.shuffle(singles)
            if len(families) > len(selections):
                raise ValueError(f"{len(families)} families but only {len(selections)} selected variants to share.")
            family_slots = set(self.rng.sample(range(len(selections)), len(families)))
            needed = len(selections) - len(families)
            if needed > len(singles):
                raise ValueError(
                    f"The {self.name} cohort has {len(singles)} patients outside families but {needed} selected "
                    "variants need single carriers; use a larger cohort or fewer selected variants."
                )
            family_iter, single_iter = iter(families), iter(singles)
            groups = [next(family_iter) if slot in family_slots else [next(single_iter)] for slot in range(len(selections))]
        elif len(selections) > len(groups):
            raise ValueError(f"{len(selections)} selected variants but only {len(groups)} carrier groups.")
        carriers: list[int] = []
        for (role, key), group in zip(selections, groups):
            self.roles[key] = role
            gene = C.gene_for_position(key[0], key[1]) or ""
            activating = key in activating_keys or role == "katp_activating"
            for position, index in enumerate(group):
                self.carry(index, key)
                carriers.append(index)
                profile = self.carrier_profile(gene, activating, relative=position > 0)
                if profile is not None:
                    relation = "relative carrying" if position else "carrier of"
                    self.apply_profile(
                        index, profile, f"{relation} {gene} {key_label(key)} ({role_label(role)}): profile {profile.name}"
                    )
        return carriers

    def build(self) -> CohortPlan:
        patients = tuple(
            PlannedPatient(
                mrn=mrn,
                patient=patient,
                variants=tuple((*key, zygosity) for key, zygosity in sorted(variants.items())),
                scenario=scenario,
            )
            for mrn, patient, variants, scenario in zip(self.mrns, self.patients, self.variants, self.scenarios)
        )
        return CohortPlan(
            name=self.name,
            patients=patients,
            roles=dict(self.roles),
            overrides=tuple(self.overrides),
            scenario_keys=dict(self.scenario_keys),
            uncarried_changes=dict(self.uncarried),
            families=self.n_families,
        )


def _medication_dict(medication: CohortMedication) -> dict[str, Any]:
    return {
        "drug_name": medication.drug_name,
        "drug_class": medication.drug_class,
        "start_date": medication.start_date.isoformat() if medication.start_date else None,
    }


def _activating_keys(classes: Mapping[Key, KnownRecord]) -> frozenset[Key]:
    return frozenset(key for key, known in classes.items() if known.mechanism == "activating")


def plan_correctness_cohort(
    cohort: Sequence[CohortPatient],
    base: Sequence[ClinVarRecord],
    background: BackgroundPool,
    *,
    seed: int,
    mrn_prefix: str = "EVAL-C",
) -> CohortPlan:
    """Map a small cohort onto the Table V scenarios (SPEC §11) plus generic carriers of every change kind.

    The first 12 patients outside families become S1–S9, RS2, RS4 and RS5 with the exact
    variants and clinical state of the demo patients; the largest family becomes RS6 (all members
    carry the upgraded V10); V11 (RS7) changes in K_new but is carried by nobody. The remaining
    patients and families carry one key each for :data:`CORRECTNESS_COVERAGE_ROLES` (in that
    order, as far as patients last) with carrier profiles, plus background variants.
    """
    classes = knowledge.classify_records(base)
    planner = _Planner(
        "correctness",
        cohort,
        seed=seed,
        mrn_prefix=mrn_prefix,
        n_families=CORRECTNESS_FAMILIES,
        background=background,
        taken=classes,
    )
    planner.scenario_keys.update(background.scenario_keys)
    planner.scenario_keys["X01"] = OFF_PANEL_KEY
    stable_role = {"P": "stable_p", "LP": "stable_lp", "VUS": "stable_vus"}
    for spec in SCENARIO_KEYS:
        key = planner.pick_scenario_key(classes, spec)
        planner.scenario_keys[spec.name] = key
        if not spec.carried:
            planner.uncarried[key] = spec.change or ""
        else:
            planner.roles[key] = spec.change or stable_role[spec.cls]

    families = sorted(planner.family_groups(), key=lambda group: (-len(group), group[0]))
    if not families:
        raise ValueError("The correctness cohort needs at least one family for scenario RS6.")
    singles = planner.single_indices()
    if len(singles) < len(SCENARIOS):
        raise ValueError(
            f"The correctness cohort needs at least {len(SCENARIOS)} patients outside families for the "
            f"scenarios; it has {len(singles)}."
        )
    for scenario, index in zip(SCENARIOS, singles):
        planner.scenarios[index] = scenario.code
        for name in scenario.keys:
            planner.carry(index, planner.scenario_keys[name])
        planner.set_clinical(
            index,
            scenario.diabetes_type,
            scenario.age_at_diagnosis_months,
            scenario.drug_class,
            f"scenario {scenario.code}: {scenario.description}",
        )
    for position, index in enumerate(families[0]):
        diabetes_type, age, drug_class = RS6_MEMBER_STATES[position % len(RS6_MEMBER_STATES)]
        planner.scenarios[index] = "RS6"
        planner.carry(index, planner.scenario_keys[RS6_KEY])
        planner.set_clinical(index, diabetes_type, age, drug_class, "scenario RS6: family sharing an upgraded HNF4A VUS")

    groups = [*families[1:], *([index] for index in singles[len(SCENARIOS):])]
    pools = synthetic_role_pools(base, planner.used)
    selections: list[tuple[str, Key]] = []
    for role in CORRECTNESS_COVERAGE_ROLES[: len(groups)]:
        key = planner.pick_key(pools, role)
        if key is None:
            raise ValueError(f"The base release has no key for the correctness role {role} ({role_label(role)}).")
        selections.append((role, key))
    planner.assign(selections, _activating_keys(classes), groups)
    for group in groups:
        for index in group:
            planner.give_background(index)
    return planner.build()


def plan_scale_cohort(
    cohort: Sequence[CohortPatient],
    pools: RolePools,
    background: BackgroundPool,
    *,
    seed: int,
    role_counts: Mapping[str, int],
    activating_keys: Collection[Key],
    taken: Iterable[Key],
    n_families: int | None = None,
    name: str = "scale",
    mrn_prefix: str = "EVAL-S",
    strict: bool = True,
) -> CohortPlan:
    """Assign selected variants (``role_counts`` keys per role from ``pools``) and background variants.

    ``n_families`` defaults to :data:`FAMILIES_PER_PATIENT` × cohort size; families take random
    selected keys. ``taken`` holds every key of the releases (new private variants avoid them).
    With ``strict`` a role whose pool runs dry raises ValueError; otherwise the role gets fewer keys
    (real release pairs have few changes of some categories).
    """
    families = round(len(cohort) * FAMILIES_PER_PATIENT) if n_families is None else n_families
    planner = _Planner(
        name, cohort, seed=seed, mrn_prefix=mrn_prefix, n_families=families, background=background, taken=taken
    )
    selections: list[tuple[str, Key]] = []
    for role, count in role_counts.items():
        for picked in range(count):
            key = planner.pick_key(pools, role)
            if key is None:
                if strict:
                    raise ValueError(f"Only {picked} candidate key(s) for role {role} ({role_label(role)}); {count} needed.")
                logger.warning("Role %s: only %d of %d requested keys available", role, picked, count)
                break
            selections.append((role, key))
    planner.rng.shuffle(selections)
    planner.assign(selections, activating_keys)
    for index in range(len(planner.patients)):
        planner.give_background(index)
    return planner.build()


def change_set_spec(
    plans: Sequence[CohortPlan],
    background: BackgroundPool,
    *,
    uncarried: Mapping[str, int],
    added_uncarried: int,
) -> knowledge.ChangeSetSpec:
    """The controlled change set for all cohorts: their forced changes, the background LB → B change
    and random changes on keys carried by nobody in any cohort.

    Raises ValueError when two plans force different changes on the same key.
    """
    forced: dict[Key, str] = {}
    if background.changed_key is not None:
        forced[background.changed_key] = "lb_to_b"
    for plan in plans:
        for key, kind in plan.forced_changes.items():
            if forced.setdefault(key, kind) != kind:
                raise ValueError(f"{key_label(key)} is forced to change as both {forced[key]} and {kind}.")
    carried = frozenset(background.keys).union(*(plan.carried_keys | plan.selected_keys for plan in plans))
    return knowledge.ChangeSetSpec(
        forced=forced,
        uncarried=dict(uncarried),
        carried_keys=carried - {key for plan in plans for key in plan.uncarried_changes},
        added_uncarried=added_uncarried,
    )


# ---------------------------------------------------------------------------
# Isolated databases
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ReleaseFiles:
    """The two knowledge-base releases of an evaluation and their labels."""

    old_path: Path
    new_path: Path
    old_label: str
    new_label: str
    old_date: date
    new_date: date


def evaluation_app(db_path: Path) -> Flask:
    """An isolated GE-EHR app on the SQLite file ``db_path`` (independent of the environment's settings).

    SQLite runs in its default rollback-journal mode (no WAL); CSRF is off so the test client can
    sign in (scenario S12); password hashing is cheap because only throw-away users are created;
    GE-EHR logs only errors (the expected "access denied" warning of S12 would be noise).
    """
    return create_app(
        {
            "SQLALCHEMY_DATABASE_URI": f"sqlite:///{db_path.resolve()}",
            "SQLALCHEMY_ENGINE_OPTIONS": {},
            "SECRET_KEY": secrets.token_hex(32),
            "TESTING": True,
            "WTF_CSRF_ENABLED": False,
            "SESSION_COOKIE_SECURE": False,
            "REMEMBER_COOKIE_SECURE": False,
            "PASSWORD_HASH_METHOD": "scrypt:1024:8:1",
            "LOG_LEVEL": "ERROR",
            "TRUSTED_PROXIES": 0,
        }
    )



INSERT_BATCH = 1_000
AS_OF_SCOPES: tuple[str, ...] = ("all", "alerted")


class FindingId(NamedTuple):
    """Identity of a current alert or note — what reconciliation matches on — for one patient."""

    mrn: str
    rule_code: str
    key: Key
    kind: str
    severity: str | None


class Notification(NamedTuple):
    """A clinician notification: a created alert with ``notify`` set, or a review notice."""

    channel: str  # "alert" | "review_notice"
    mrn: str
    key: Key
    rule_code: str | None
    severity: str | None


@dataclass(frozen=True)
class StateCapture:
    """The current alerts and notes at one moment."""

    at: datetime
    alert_ids: Mapping[int, frozenset[int]]  # patient id → ids of its current alerts and notes
    findings: frozenset[FindingId]


@dataclass(frozen=True)
class Baseline:
    """The initial state shared by every strategy: a SQLite file and its T0 snapshot."""

    db_path: Path
    state: StateCapture  # after the initial evaluation under K_old + rules v1 (T0)
    timings_ms: Mapping[str, float]
    counts: Mapping[str, int]


class _Lookup:
    """Patient MRNs and variant keys by id (one query each), to express alerts as :class:`FindingId`."""

    def __init__(self) -> None:
        self.mrns: dict[int, str] = dict(db.session.execute(select(Patient.id, Patient.mrn)).tuples().all())
        columns = (VariantKey.id, VariantKey.chrom, VariantKey.pos, VariantKey.ref, VariantKey.alt)
        self.keys: dict[int, Key] = {
            row.id: (row.chrom, row.pos, row.ref, row.alt) for row in db.session.execute(select(*columns))
        }

    def finding(self, row: Any) -> FindingId:
        return FindingId(self.mrns[row.patient_id], row.rule_code, self.keys[row.variant_key_id], row.kind, row.severity)


def _ms(clock: float) -> float:
    return (time.perf_counter() - clock) * 1000


def capture_state(lookup: _Lookup, at: datetime) -> StateCapture:
    """The current alerts and notes (one query), stamped with ``at``."""
    statement = select(
        Alert.id, Alert.patient_id, Alert.rule_code, Alert.variant_key_id, Alert.kind, Alert.severity
    ).where(Alert.status.in_(C.CURRENT_ALERT_STATUSES))
    ids: defaultdict[int, set[int]] = defaultdict(set)
    findings: set[FindingId] = set()
    for row in db.session.execute(statement):
        ids[row.patient_id].add(row.id)
        findings.add(lookup.finding(row))
    return StateCapture(at, {patient_id: frozenset(alert_ids) for patient_id, alert_ids in ids.items()}, frozenset(findings))


def build_baseline(
    plan: CohortPlan,
    files: ReleaseFiles,
    rules_v1: Mapping[str, Any],
    db_path: Path,
    *,
    ingest: str,
    vcf_dir: Path | None = None,
) -> Baseline:
    """Create the initial-state database of ``plan`` at ``db_path`` (an existing file is replaced).

    Rules v1 and K_old are loaded first. ``ingest="vcf"`` activates K_old, then creates every
    patient and medication through :mod:`ge_ehr.services.patient_records` and ingests a VCF per
    patient (written to ``vcf_dir`` when given), so each patient is evaluated at upload.
    ``ingest="bulk"`` inserts patients, diagnoses, medications and PatientVariants in bulk and
    then activates K_old (first activation = initial classification of every stored key). K_new
    is imported (not activated) last; T0 is captured afterwards.
    """
    if ingest not in ("vcf", "bulk"):
        raise ValueError(f"Unknown ingest mode {ingest!r}; expected 'vcf' or 'bulk'.")
    db_path.unlink(missing_ok=True)
    app = evaluation_app(db_path)
    timings: dict[str, float] = {}
    with app.app_context():
        try:
            db.create_all()
            alerting.import_rule_versions(dict(rules_v1))
            clock = time.perf_counter()
            old_release = clinvar.import_release(files.old_path, label=files.old_label, release_date=files.old_date)
            db.session.commit()
            timings["import_old_release"] = _ms(clock)
            if ingest == "vcf":
                reevaluation.activate_release(old_release)
                db.session.commit()
                clock = time.perf_counter()
                _ingest_vcfs(plan, vcf_dir)
                db.session.commit()
                timings["create_patients_and_ingest_vcfs"] = _ms(clock)
            else:
                clock = time.perf_counter()
                _bulk_load(plan)
                db.session.commit()
                timings["bulk_load"] = _ms(clock)
                clock = time.perf_counter()
                reevaluation.activate_release(old_release)
                db.session.commit()
                timings["initial_activation"] = _ms(clock)
            clock = time.perf_counter()
            clinvar.import_release(files.new_path, label=files.new_label, release_date=files.new_date)
            db.session.commit()
            timings["import_new_release"] = _ms(clock)
            state = capture_state(_Lookup(), utcnow())
            counts = {
                "patients": db.session.scalar(select(func.count(Patient.id))),
                "patient_variants": db.session.scalar(select(func.count(PatientVariant.id))),
                "stored_keys": db.session.scalar(select(func.count(func.distinct(PatientVariant.variant_key_id)))),
                "current_alerts_and_notes": len(state.findings),
            }
        finally:
            db.session.remove()
            db.engine.dispose()
    logger.info(
        "Baseline %s: %d patients, %d patient variants, %d current findings",
        db_path.name,
        counts["patients"],
        counts["patient_variants"],
        counts["current_alerts_and_notes"],
    )
    return Baseline(db_path, state, timings, counts)


def _ingest_vcfs(plan: CohortPlan, vcf_dir: Path | None) -> None:
    """Create each patient with the F1 services and ingest its VCF (evaluated at upload)."""
    if vcf_dir is not None:
        vcf_dir.mkdir(parents=True, exist_ok=True)
    for planned in plan.patients:
        source = planned.patient
        patient = patient_records.create_patient(
            mrn=planned.mrn,
            given_name=source.given_name,
            family_name=source.family_name,
            sex=source.sex,
            birth_date=source.birth_date,
            family_id=source.family_id,
            diabetes_type=source.diabetes_type,
            age_at_diagnosis_months=source.age_at_diagnosis_months,
            external_id=source.synthetic_id,
        )
        for medication in source.medications:
            patient_records.add_medication(
                patient, drug_name=medication.drug_name, drug_class=medication.drug_class, start_date=medication.start_date
            )
        text = format_patient_vcf(planned.mrn, planned.variants)
        if vcf_dir is not None:
            (vcf_dir / f"{planned.mrn}.vcf").write_text(text, encoding="utf-8", newline="\n")
        patient_records.ingest_vcf(patient, text.encode("utf-8"), f"{planned.mrn}.vcf")


def _insert(model: type[Any], rows: Sequence[dict[str, Any]]) -> None:
    for batch in batched(rows, INSERT_BATCH):
        db.session.execute(insert(model), list(batch))


def _bulk_load(plan: CohortPlan) -> None:
    """Insert patients (ids 1…n), diagnoses, medications and unclassified PatientVariants in bulk."""
    patients: list[dict[str, Any]] = []
    diagnoses: list[dict[str, Any]] = []
    medications: list[dict[str, Any]] = []
    for patient_id, planned in enumerate(plan.patients, start=1):
        source = planned.patient
        patients.append(
            {
                "id": patient_id,
                "mrn": planned.mrn,
                "given_name": source.given_name,
                "family_name": source.family_name,
                "sex": source.sex,
                "birth_date": source.birth_date,
                "family_id": source.family_id,
                "external_id": source.synthetic_id,
                "is_synthetic": True,
            }
        )
        diagnoses.append(
            {
                "patient_id": patient_id,
                "diabetes_type": source.diabetes_type,
                "age_at_diagnosis_months": source.age_at_diagnosis_months,
            }
        )
        medications.extend(
            {
                "patient_id": patient_id,
                "drug_name": medication.drug_name,
                "drug_class": medication.drug_class,
                "start_date": medication.start_date,
            }
            for medication in source.medications
        )
    _insert(Patient, patients)
    _insert(Diagnosis, diagnoses)
    _insert(Medication, medications)
    key_ids = _variant_key_ids({key for planned in plan.patients for key in planned.panel_keys})
    _insert(
        PatientVariant,
        [
            {
                "patient_id": patient_id,
                "variant_key_id": key_ids[(chrom, pos, ref, alt)],
                "zygosity": zygosity,
                "gene": C.gene_for_position(chrom, pos),
                "current_classification": "NC",
                "reported_classification": "NC",
            }
            for patient_id, planned in enumerate(plan.patients, start=1)
            for chrom, pos, ref, alt, zygosity in planned.panel_variants
        ],
    )


def _variant_key_ids(keys: Collection[Key]) -> dict[Key, int]:
    """Ids of ``keys``; keys not stored yet (private variants) are inserted with new ids."""
    columns = (VariantKey.id, VariantKey.chrom, VariantKey.pos, VariantKey.ref, VariantKey.alt)
    stored = {(row.chrom, row.pos, row.ref, row.alt): row.id for row in db.session.execute(select(*columns))}
    next_id = max(stored.values(), default=0) + 1
    missing = sorted(set(keys) - stored.keys())
    rows = []
    for offset, (chrom, pos, ref, alt) in enumerate(missing):
        stored[(chrom, pos, ref, alt)] = next_id + offset
        rows.append(
            {"id": next_id + offset, "chrom": chrom, "pos": pos, "ref": ref, "alt": alt, "gene": C.gene_for_position(chrom, pos)}
        )
    _insert(VariantKey, rows)
    return {key: stored[key] for key in keys}


# ---------------------------------------------------------------------------
# Strategies
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ChangeOutcome:
    """What one knowledge change did in one strategy's database."""

    change: str  # "release" | "rule_revision"
    strategy: str
    started_at: datetime
    duration_ms: float  # wall clock of the activation / rule-revision call
    commit_ms: float
    state: frozenset[FindingId]  # current alerts and notes afterwards
    new_findings: frozenset[FindingId]  # current findings created by this change
    superseded: frozenset[FindingId]  # findings superseded by this change
    notifications: tuple[Notification, ...]
    run: Mapping[str, Any] | None  # ReevaluationRun counters (None: no run — alert-once)
    changed_keys: frozenset[Key] | None  # Δ recorded as VariantChange rows (None: no run)

    @property
    def records_examined(self) -> int:
        return int(self.run["records_examined"]) if self.run else 0


@dataclass(frozen=True)
class AsOfCheck:
    """Whether ``alerts_as_of(patient, at)`` returns exactly the alerts captured at ``at``."""

    at: datetime
    patients_checked: int
    patients_matching: int
    mismatches: tuple[str, ...]  # MRNs (first 20)

    @property
    def matches(self) -> bool:
        return self.patients_checked == self.patients_matching


@dataclass(frozen=True)
class StrategyOutcome:
    """Both knowledge changes of one strategy, the point-in-time checks and the inspection result."""

    strategy: str
    release: ChangeOutcome
    rule_revision: ChangeOutcome
    as_of: Mapping[str, AsOfCheck]
    inspection: Any = None


Inspector = Callable[[Flask, StrategyOutcome], Any]
"""Called with the strategy's app (no context pushed) after both changes, e.g. for scenario checks."""


def mark_release_current(release: KbRelease) -> None:
    """Alert-once baseline: make ``release`` current with no re-evaluation (no run, no alert change)."""
    db.session.execute(update(KbRelease).where(KbRelease.is_current.is_(True)).values(is_current=False))
    release.sequence = (db.session.scalar(select(func.max(KbRelease.sequence))) or 0) + 1
    release.is_current = True
    release.activated_at = utcnow()
    db.session.flush()


def _activate(strategy: str, release: KbRelease) -> ReevaluationRun | None:
    if strategy == "alert_once":
        mark_release_current(release)
        return None
    return reevaluation.activate_release(release, strategy=strategy)


def _revise_rules(strategy: str, doc: Mapping[str, Any]) -> ReevaluationRun | None:
    if strategy == "alert_once":
        alerting.import_rule_versions(dict(doc))
        return None
    return reevaluation.apply_rule_set(dict(doc), strategy=strategy)


def _run_info(run: ReevaluationRun | None) -> dict[str, Any] | None:
    if run is None:
        return None
    info: dict[str, Any] = {name: getattr(run, name) for name in RUN_COUNTERS}
    info.update(id=run.id, strategy=run.strategy, trigger=run.trigger, status=run.status, engine_duration_ms=run.duration_ms)
    summary = run.summary or {}
    for name in ("categories", "rules", "superseded_by_reason"):
        if name in summary:
            info[name] = summary[name]
    return info


def _apply_change(
    change: str, strategy: str, lookup: _Lookup, action: Callable[[], ReevaluationRun | None]
) -> ChangeOutcome:
    """Run ``action`` (timed), commit, and capture its effect on alerts, notes and notifications."""
    started = utcnow()
    clock = time.perf_counter()
    run = action()
    duration = _ms(clock)
    clock = time.perf_counter()
    db.session.commit()
    commit_ms = _ms(clock)

    statement = select(
        Alert.patient_id,
        Alert.rule_code,
        Alert.variant_key_id,
        Alert.kind,
        Alert.severity,
        Alert.status,
        Alert.created_at,
        Alert.notify,
    ).where(or_(Alert.status.in_(C.CURRENT_ALERT_STATUSES), Alert.superseded_at >= started))
    state: set[FindingId] = set()
    new: set[FindingId] = set()
    superseded: set[FindingId] = set()
    notifications: list[Notification] = []
    for row in db.session.execute(statement):
        finding = lookup.finding(row)
        if row.status in C.CURRENT_ALERT_STATUSES:
            state.add(finding)
            if row.created_at >= started:
                new.add(finding)
                if row.notify:
                    notifications.append(Notification("alert", finding.mrn, finding.key, finding.rule_code, finding.severity))
        else:
            superseded.add(finding)
    notices = select(ReviewNotice.patient_id, ReviewNotice.variant_key_id).where(ReviewNotice.created_at >= started)
    for row in db.session.execute(notices):
        notifications.append(Notification("review_notice", lookup.mrns[row.patient_id], lookup.keys[row.variant_key_id], None, None))
    changed_keys = None
    if run is not None:
        key_ids = db.session.scalars(select(VariantChange.variant_key_id).where(VariantChange.run_id == run.id))
        changed_keys = frozenset(lookup.keys[key_id] for key_id in key_ids)
    logger.info("%s / %s: %.1f ms (+%.1f ms commit)", STRATEGY_LABELS[strategy], change, duration, commit_ms)
    return ChangeOutcome(
        change=change,
        strategy=strategy,
        started_at=started,
        duration_ms=duration,
        commit_ms=commit_ms,
        state=frozenset(state),
        new_findings=frozenset(new),
        superseded=frozenset(superseded),
        notifications=tuple(sorted(notifications, key=lambda item: (item.channel, item.mrn, item.key, item.rule_code or ""))),
        run=_run_info(run),
        changed_keys=changed_keys,
    )


def check_as_of(capture: StateCapture, patient_ids: Sequence[int]) -> AsOfCheck:
    """Compare ``alerts_as_of(patient, capture.at)`` with the captured alert ids, patient by patient."""
    checked = 0
    mismatches: list[str] = []
    for chunk in batched(patient_ids, 500):
        for patient in db.session.scalars(select(Patient).where(Patient.id.in_(chunk)).order_by(Patient.id)):
            checked += 1
            shown = frozenset(alert.id for alert in reevaluation.alerts_as_of(patient, capture.at))
            if shown != capture.alert_ids.get(patient.id, frozenset()):
                mismatches.append(patient.mrn)
        db.session.expunge_all()  # bound the identity map on large cohorts
    return AsOfCheck(capture.at, checked, checked - len(mismatches), tuple(mismatches[:20]))


def _as_of_patients(scope: str) -> list[int]:
    if scope == "all":
        return list(db.session.scalars(select(Patient.id).order_by(Patient.id)))
    return list(db.session.scalars(select(Alert.patient_id).distinct().order_by(Alert.patient_id)))


def run_strategy(
    strategy: str,
    baseline: Baseline,
    files: ReleaseFiles,
    rules_v2: Mapping[str, Any],
    db_path: Path,
    *,
    as_of_scope: str | None = "all",
    inspect: Inspector | None = None,
    keep_database: bool = False,
) -> StrategyOutcome:
    """Copy the baseline to ``db_path`` and apply both knowledge changes with ``strategy``.

    The release change activates K_new (``alert_once``: marked current without re-evaluation);
    the rule revision applies ``rules_v2`` (``alert_once``: versions switched without
    re-evaluation). Afterwards ``alerts_as_of`` is checked against the T0 snapshot and the
    snapshot taken between the two changes, for every patient (``as_of_scope="all"``), every
    patient with an alert history (``"alerted"``) or not at all (None — e.g. for timing repeats).
    ``inspect`` runs last; its result is stored in
    :attr:`StrategyOutcome.inspection`. The copy is deleted unless ``keep_database``.
    """
    if strategy not in STRATEGIES:
        raise ValueError(f"Unknown strategy {strategy!r}; expected one of {', '.join(STRATEGIES)}.")
    if as_of_scope is not None and as_of_scope not in AS_OF_SCOPES:
        raise ValueError(f"Unknown as-of scope {as_of_scope!r}; expected one of {', '.join(AS_OF_SCOPES)}.")
    shutil.copyfile(baseline.db_path, db_path)
    app = evaluation_app(db_path)
    try:
        with app.app_context():
            lookup = _Lookup()
            new_release = db.session.scalar(
                select(KbRelease).where(KbRelease.label == files.new_label, KbRelease.is_current.is_(False))
            )
            if new_release is None:
                raise ValueError(f"Release {files.new_label!r} is not an inactive release of the baseline database.")
            release = _apply_change("release", strategy, lookup, lambda: _activate(strategy, new_release))
            before_rules = capture_state(lookup, utcnow())
            rule_revision = _apply_change("rule_revision", strategy, lookup, lambda: _revise_rules(strategy, rules_v2))
            as_of: dict[str, AsOfCheck] = {}
            if as_of_scope is not None:
                patient_ids = _as_of_patients(as_of_scope)
                as_of["before_release"] = check_as_of(baseline.state, patient_ids)
                as_of["before_rule_revision"] = check_as_of(before_rules, patient_ids)
        outcome = StrategyOutcome(strategy, release, rule_revision, as_of)
        if inspect is not None:
            outcome = replace(outcome, inspection=inspect(app, outcome))
        return outcome
    finally:
        with app.app_context():
            db.session.remove()
            db.engine.dispose()
        if not keep_database:
            db_path.unlink(missing_ok=True)
