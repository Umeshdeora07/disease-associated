"""F3 — rule definitions and the pure rule evaluation, Algorithm 1 (SPEC §6.1, §6.2). No DB access.

Owned by the Rules work package. Interface stub created by the Foundation work package: the
exception and dataclasses are final (``RuleSpec.from_definition`` is implemented by the Rules
package, as are the module functions).
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)


class RuleDefinitionError(ValueError):
    """A rule set or rule definition is invalid; the message says precisely what is wrong."""


@dataclass(frozen=True)
class RuleSpec:
    """An executable rule version."""

    rule_version_id: int | None
    code: str
    version: int
    title: str
    kind: str
    genes: frozenset[str]
    classes: frozenset[str]
    mechanism: str | None
    clinical: Mapping[str, Any] | None
    severity: str
    recommendation: str
    source: str

    @classmethod
    def from_definition(cls, defn: Mapping[str, Any], rule_version_id: int | None = None) -> RuleSpec:
        """Build a spec from one rule definition dict (SPEC §6.1), validating it strictly.

        Raises :class:`RuleDefinitionError`.
        """
        raise NotImplementedError


@dataclass(frozen=True)
class ClassifiedVariant:
    """A patient variant with its classification under some release."""

    patient_variant_id: int | None
    variant_key_id: int
    gene: str
    classification: str
    mechanism: str
    zygosity: str
    label: str = ""
    is_expert_panel: bool = False


@dataclass(frozen=True)
class ClinicalContext:
    """The patient data the therapy conditions are evaluated against."""

    drug_classes: frozenset[str]
    age_at_diagnosis_months: int | None
    diabetes_type: str | None


@dataclass(frozen=True)
class Finding:
    """A desired alert (``kind="alert"``) or note (``kind="note"``, severity None)."""

    rule_code: str
    rule_version_id: int | None
    variant_key_id: int
    patient_variant_id: int | None
    kind: str
    severity: str | None
    title: str
    message: str
    source: str | None
    classification: str

    @property
    def key(self) -> tuple[str, int, str, str | None]:
        """Reconciliation key: (rule_code, variant_key_id, kind, severity)."""
        return (self.rule_code, self.variant_key_id, self.kind, self.severity)


def validate_rule_set(doc: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Validate a rule-set document and return its rule definitions.

    Raises :class:`RuleDefinitionError` with a precise message.
    """
    raise NotImplementedError


def clinical_condition_holds(clinical: Mapping[str, Any] | None, ctx: ClinicalContext) -> tuple[bool, str]:
    """Return (holds, human-readable reason) for a rule's therapy/diagnosis condition."""
    raise NotImplementedError


def evaluate(
    variants: Iterable[ClassifiedVariant], ctx: ClinicalContext, rules: Iterable[RuleSpec]
) -> list[Finding]:
    """Algorithm 1: alert and note findings in deterministic (rule code, variant) order."""
    raise NotImplementedError


def describe_clinical_condition(clinical: Mapping[str, Any] | None) -> str:
    """Human-readable description of a clinical condition (used by the UI)."""
    raise NotImplementedError
