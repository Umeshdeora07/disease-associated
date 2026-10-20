"""F3 — reconciliation of findings into alerts/notes, and rule-version import (SPEC §6.3).

Owned by the Rules work package. Interface stub created by the Foundation work package: the
dataclass is final; the function bodies are implemented by the Rules package.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from ..models import Alert, KbRelease, Patient, ReevaluationRun, User
    from .rules import ClassifiedVariant, ClinicalContext, RuleSpec

logger = logging.getLogger(__name__)


@dataclass
class ReconcileResult:
    """Outcome of :func:`reconcile_patient`."""

    created: list[Alert] = field(default_factory=list)
    superseded: list[Alert] = field(default_factory=list)
    confirmed: list[Alert] = field(default_factory=list)
    unchanged: list[Alert] = field(default_factory=list)
    records_examined: int = 0


def load_current_rules() -> list[RuleSpec]:
    """The current rule versions (``RuleVersion.is_current``) as executable specs."""
    raise NotImplementedError


def clinical_context(patient: Patient) -> ClinicalContext:
    """Current drug classes and diagnosis data of ``patient``."""
    raise NotImplementedError


def reconcile_patient(
    patient: Patient,
    release: KbRelease | None,
    rules: Iterable[RuleSpec],
    *,
    trigger: str,
    variant_key_ids: Iterable[int] | None = None,
    rule_codes: Iterable[str] | None = None,
    run: ReevaluationRun | None = None,
    superseded_reason: str = "therapy_changed",
    user: User | None = None,
    now: datetime | None = None,
    classified: Iterable[ClassifiedVariant] | None = None,
) -> ReconcileResult:
    """Create, supersede or confirm the patient's alerts/notes so they match the desired findings."""
    raise NotImplementedError


def evaluate_patient_now(
    patient: Patient, *, trigger: str, user: User | None = None, superseded_reason: str = "therapy_changed"
) -> ReconcileResult:
    """Reconcile one patient against the current release and rules (full scope)."""
    raise NotImplementedError


def import_rule_versions(doc: Mapping[str, Any], *, user: User | None = None) -> list[dict[str, Any]]:
    """Store unseen rule versions and make the highest version per code current.

    Returns ``{"code", "old_version", "new_version", "old_rule_version_id",
    "new_rule_version_id"}`` for every code whose current version changed. Raises
    :class:`~ge_ehr.services.rules.RuleDefinitionError` for an existing (code, version) whose
    definition differs.
    """
    raise NotImplementedError
