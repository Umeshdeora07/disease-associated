"""F3 — rule definitions and the pure rule evaluation, Algorithm 1 (SPEC §6.1, §6.2). No DB access.

A rule set is a JSON document (``data/rules/*.json``)::

    {"rule_set": "...", "label": "v2 (2026)", "rules": [{"code": "D2", "version": 1, ...}, ...]}

:func:`validate_rule_set` checks it strictly and :meth:`RuleSpec.from_definition` turns one rule
definition into an executable spec. :func:`evaluate` is Algorithm 1 of the paper: for every rule
whose genetic criterion a classified variant meets, the rule's clinical (therapy/diagnosis)
condition decides between an **alert** (condition absent or holding) and a **note** explaining
which part of the condition failed. Findings are produced per (rule, variant).

Owned by the Rules work package.
"""

from __future__ import annotations

import copy
import json
import logging
import os
import re
import sys
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any

from ..constants import CLASS_LABELS, CLASSES, DRUG_CLASSES, GENES, MECHANISMS, RULE_KINDS, SEVERITIES, ZYGOSITIES

logger = logging.getLogger(__name__)

#: Directory holding the bundled rule sets ``ruleset_v1.json`` and ``ruleset_v2.json``.
RULES_DATA_DIR = Path(__file__).resolve().parents[2] / "data" / "rules"

RULE_CODE_PATTERN = re.compile(r"D[0-9]+")
MAX_CODE_LENGTH = 8  # Rule.code / RuleVersion.code column width
MAX_TITLE_LENGTH = 255  # RuleVersion.title / Alert.title column width
MAX_LABEL_LENGTH = 128  # RuleVersion.rule_set_label column width

RULE_SET_FIELDS: tuple[str, ...] = ("rule_set", "label", "rules")
RULE_REQUIRED_FIELDS: tuple[str, ...] = (
    "code",
    "version",
    "kind",
    "title",
    "genetic",
    "clinical",
    "severity",
    "recommendation",
    "source",
)
RULE_OPTIONAL_FIELDS: tuple[str, ...] = ("change_note",)
RULE_FIELDS: tuple[str, ...] = RULE_REQUIRED_FIELDS + RULE_OPTIONAL_FIELDS
GENETIC_FIELDS: tuple[str, ...] = ("genes", "classes", "mechanism", "zygosity")

ANY_OF_DRUG_CLASSES = "any_of_drug_classes"
NONE_OF_DRUG_CLASSES = "none_of_drug_classes"
MAX_AGE_AT_DIAGNOSIS_MONTHS = "max_age_at_diagnosis_months"
CLINICAL_FIELDS: tuple[str, ...] = (ANY_OF_DRUG_CLASSES, NONE_OF_DRUG_CLASSES, MAX_AGE_AT_DIAGNOSIS_MONTHS)

# How each drug class reads inside a sentence ("not receiving insulin or a sulfonylurea").
_DRUG_PHRASES: dict[str, str] = {
    "insulin": "insulin",
    "metformin": "metformin",
    "sulfonylurea": "a sulfonylurea",
    "other": "another glucose-lowering drug",
}


class RuleDefinitionError(ValueError):
    """A rule set or rule definition is invalid; the message says precisely what is wrong."""


# ---------------------------------------------------------------------------
# Executable rules and evaluation inputs/outputs
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RuleSpec:
    """An executable rule version.

    ``clinical`` is the normalised clinical condition: a read-only mapping holding only the
    conditions that apply (drug-class lists as tuples in :data:`~ge_ehr.constants.DRUG_CLASSES`
    order), or None when the rule has no therapy condition. ``zygosity`` restricts the rule to
    carriers with one of these zygosities (None: any zygosity).
    """

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
    zygosity: frozenset[str] | None = None

    @property
    def label(self) -> str:
        """``"D4 v2"``."""
        return f"{self.code} v{self.version}"

    @classmethod
    def from_definition(cls, defn: Mapping[str, Any], rule_version_id: int | None = None) -> RuleSpec:
        """Build a spec from one rule definition dict (SPEC §6.1), validating it strictly.

        Raises :class:`RuleDefinitionError` naming the rule and the offending field.
        """
        return _parse_rule(defn, position=None, rule_version_id=rule_version_id)


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


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def _fail(context: str, message: str) -> RuleDefinitionError:
    return RuleDefinitionError(f"{context}: {message}")


def _quoted(values: Iterable[Any]) -> str:
    return ", ".join(repr(value) for value in values)


def _is_positive_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def _type_name(value: Any) -> str:
    """JSON-flavoured type name for error messages."""
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, (int, float)):
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, Mapping):
        return "object"
    if isinstance(value, (list, tuple, set, frozenset)):
        return "list"
    return type(value).__name__


def _check_fields(value: Mapping[str, Any], allowed: Sequence[str], context: str, where: str) -> None:
    """Reject keys of ``value`` that are not in ``allowed``."""
    unknown = sorted(str(key) for key in value if key not in allowed)
    if unknown:
        raise _fail(context, f"unknown field(s) in {where}: {_quoted(unknown)}; allowed: {', '.join(allowed)}.")


def _required_text(defn: Mapping[str, Any], field: str, context: str, max_length: int | None = None) -> str:
    value = defn[field]
    if not isinstance(value, str) or not value.strip():
        raise _fail(context, f"{field!r} must be a non-empty string, got {_type_name(value)} {value!r}.")
    if max_length is not None and len(value) > max_length:
        raise _fail(context, f"{field!r} must be at most {max_length} characters (got {len(value)}).")
    return value


def _choice(value: Any, allowed: Sequence[str], field: str, context: str) -> str:
    if not isinstance(value, str) or value not in allowed:
        raise _fail(context, f"{field!r} must be one of {', '.join(allowed)}; got {value!r}.")
    return value


def _subset_list(
    value: Any,
    allowed: Sequence[str],
    field: str,
    nouns: tuple[str, str],
    context: str,
    *,
    nullable: bool = False,
) -> tuple[str, ...]:
    """Validate a non-empty list of distinct values from ``allowed``; returned in ``allowed`` order.

    ``nouns`` is the (singular, plural) name of the values for error messages; ``nullable`` only
    changes the wording of the error (the caller handles null itself).
    """
    singular, plural = nouns
    requirement = f"{'null or ' if nullable else ''}a non-empty list of {plural}"
    if not isinstance(value, (list, tuple, set, frozenset)) or not value:
        raise _fail(context, f"{field!r} must be {requirement}, got {_type_name(value)} {value!r}.")
    items = list(value)
    unknown = [item for item in items if not isinstance(item, str) or item not in allowed]
    if unknown:
        what = singular if len(unknown) == 1 else plural
        raise _fail(context, f"{field!r} contains unknown {what} {_quoted(unknown)}; allowed: {', '.join(allowed)}.")
    duplicates = sorted({item for item in items if items.count(item) > 1}, key=allowed.index)
    if duplicates:
        raise _fail(context, f"{field!r} lists {_quoted(duplicates)} more than once.")
    return tuple(item for item in allowed if item in items)


def _rule_context(defn: Mapping[str, Any], position: int | None) -> str:
    """Name a rule in error messages: ``Rule D2 v1``, ``Rule D2`` or ``Rule #3``."""
    code, version = defn.get("code"), defn.get("version")
    if isinstance(code, str) and RULE_CODE_PATTERN.fullmatch(code) and len(code) <= MAX_CODE_LENGTH:
        return f"Rule {code} v{version}" if _is_positive_int(version) else f"Rule {code}"
    return f"Rule #{position}" if position is not None else "Rule definition"


def _normalize_clinical(value: Any, context: str) -> Mapping[str, Any] | None:
    """Validate a clinical condition; return only the conditions that apply (None if none do)."""
    if value is None:
        return None
    if not isinstance(value, Mapping):
        raise _fail(context, f"'clinical' must be an object or null, got {_type_name(value)} {value!r}.")
    _check_fields(value, CLINICAL_FIELDS, context, "'clinical'")
    conditions: dict[str, Any] = {}
    for name in (ANY_OF_DRUG_CLASSES, NONE_OF_DRUG_CLASSES):
        if value.get(name) is not None:
            conditions[name] = _subset_list(
                value[name], DRUG_CLASSES, f"clinical.{name}", ("drug class", "drug classes"), context, nullable=True
            )
    overlap = set(conditions.get(ANY_OF_DRUG_CLASSES, ())) & set(conditions.get(NONE_OF_DRUG_CLASSES, ()))
    if overlap:
        ordered = [drug_class for drug_class in DRUG_CLASSES if drug_class in overlap]
        raise _fail(
            context,
            f"{'drug class' if len(ordered) == 1 else 'drug classes'} {_quoted(ordered)} cannot be in both "
            f"'clinical.{ANY_OF_DRUG_CLASSES}' and 'clinical.{NONE_OF_DRUG_CLASSES}'.",
        )
    max_age = value.get(MAX_AGE_AT_DIAGNOSIS_MONTHS)
    if max_age is not None:
        if not _is_positive_int(max_age):
            raise _fail(
                context,
                f"'clinical.{MAX_AGE_AT_DIAGNOSIS_MONTHS}' must be null or a positive integer number of months, "
                f"got {_type_name(max_age)} {max_age!r}.",
            )
        conditions[MAX_AGE_AT_DIAGNOSIS_MONTHS] = max_age
    return MappingProxyType(conditions) if conditions else None


def _parse_genetic(
    value: Any, context: str
) -> tuple[frozenset[str], frozenset[str], str | None, frozenset[str] | None]:
    if not isinstance(value, Mapping):
        raise _fail(context, f"'genetic' must be an object, got {_type_name(value)} {value!r}.")
    _check_fields(value, GENETIC_FIELDS, context, "'genetic'")
    missing = [name for name in ("genes", "classes") if name not in value]
    if missing:
        raise _fail(context, f"missing required field(s) in 'genetic': {_quoted(missing)}.")
    genes = _subset_list(value["genes"], GENES, "genetic.genes", ("gene", "genes"), context)
    classes = _subset_list(value["classes"], CLASSES, "genetic.classes", ("class", "classes"), context)
    mechanism = value.get("mechanism")
    if mechanism is not None and (not isinstance(mechanism, str) or mechanism not in MECHANISMS):
        raise _fail(context, f"'genetic.mechanism' must be null or one of {', '.join(MECHANISMS)}; got {mechanism!r}.")
    zygosity = value.get("zygosity")
    if zygosity is not None:
        zygosity = frozenset(
            _subset_list(zygosity, ZYGOSITIES, "genetic.zygosity", ("zygosity", "zygosities"), context, nullable=True)
        )
    return frozenset(genes), frozenset(classes), mechanism, zygosity


def _parse_rule(defn: Any, *, position: int | None, rule_version_id: int | None) -> RuleSpec:
    """Validate one rule definition and build its :class:`RuleSpec`."""
    if not isinstance(defn, Mapping):
        where = f"Rule #{position}" if position is not None else "Rule definition"
        raise RuleDefinitionError(f"{where}: must be an object, got {_type_name(defn)}.")
    context = _rule_context(defn, position)

    if "code" in defn:
        code = defn["code"]
        if not isinstance(code, str) or not RULE_CODE_PATTERN.fullmatch(code) or len(code) > MAX_CODE_LENGTH:
            raise _fail(
                context,
                f"'code' must be 'D' followed by digits (at most {MAX_CODE_LENGTH} characters, e.g. 'D2'), "
                f"got {code!r}.",
            )
    if "version" in defn and not _is_positive_int(defn["version"]):
        version = defn["version"]
        raise _fail(context, f"'version' must be a positive integer, got {_type_name(version)} {version!r}.")
    _check_fields(defn, RULE_FIELDS, context, "the rule")
    missing = [name for name in RULE_REQUIRED_FIELDS if name not in defn]
    if missing:
        hint = " (use null for no therapy condition)" if "clinical" in missing else ""
        raise _fail(context, f"missing required field(s) {_quoted(missing)}{hint}.")

    kind = _choice(defn["kind"], RULE_KINDS, "kind", context)
    title = _required_text(defn, "title", context, MAX_TITLE_LENGTH)
    genes, classes, mechanism, zygosity = _parse_genetic(defn["genetic"], context)
    clinical = _normalize_clinical(defn["clinical"], context)
    severity = _choice(defn["severity"], SEVERITIES, "severity", context)
    recommendation = _required_text(defn, "recommendation", context)
    source = _required_text(defn, "source", context)
    change_note = defn.get("change_note")
    if change_note is not None and not isinstance(change_note, str):
        raise _fail(context, f"'change_note' must be a string or null, got {_type_name(change_note)} {change_note!r}.")

    return RuleSpec(
        rule_version_id=rule_version_id,
        code=defn["code"],
        version=defn["version"],
        title=title,
        kind=kind,
        genes=genes,
        classes=classes,
        mechanism=mechanism,
        clinical=clinical,
        severity=severity,
        recommendation=recommendation,
        source=source,
        zygosity=zygosity,
    )


def validate_rule_set(doc: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Validate a rule-set document and return (deep copies of) its rule definitions.

    The document must be an object with a non-empty ``rules`` list and optional non-empty
    ``rule_set`` / ``label`` strings; no other keys are allowed. Every rule is checked with
    :meth:`RuleSpec.from_definition`, and a (code, version) pair may appear only once.

    Raises :class:`RuleDefinitionError` with a precise message.
    """
    if not isinstance(doc, Mapping):
        raise RuleDefinitionError(f"Rule set must be an object with a 'rules' list, got {_type_name(doc)}.")
    _check_fields(doc, RULE_SET_FIELDS, "Rule set", "the document")
    for name in ("rule_set", "label"):
        value = doc.get(name)
        if value is not None and (not isinstance(value, str) or not value.strip()):
            raise RuleDefinitionError(f"Rule set: {name!r} must be a non-empty string when given, got {value!r}.")
    label = doc.get("label")
    if label is not None and len(label) > MAX_LABEL_LENGTH:
        raise RuleDefinitionError(
            f"Rule set: 'label' must be at most {MAX_LABEL_LENGTH} characters (got {len(label)})."
        )
    rules = doc.get("rules")
    if not isinstance(rules, list) or not rules:
        raise RuleDefinitionError(
            f"Rule set: 'rules' must be a non-empty list of rule definitions, got {_type_name(rules)}."
        )

    seen: dict[tuple[str, int], int] = {}
    for position, defn in enumerate(rules, start=1):
        spec = _parse_rule(defn, position=position, rule_version_id=None)
        identity = (spec.code, spec.version)
        if identity in seen:
            raise RuleDefinitionError(
                f"Rule set lists {spec.label} more than once (rules #{seen[identity]} and #{position})."
            )
        seen[identity] = position
    return [copy.deepcopy(dict(defn)) for defn in rules]


def parse_rule_set_json(data: str | bytes) -> dict[str, Any]:
    """Parse and validate a rule-set JSON document (an uploaded or pasted file).

    Raises :class:`RuleDefinitionError` when the text is not UTF-8, not valid JSON, or not a
    valid rule set.
    """
    if isinstance(data, bytes):
        try:
            data = data.decode("utf-8-sig")
        except UnicodeDecodeError:
            raise RuleDefinitionError("Rule set file is not UTF-8 encoded text.") from None
    try:
        doc = json.loads(data)
    except json.JSONDecodeError as exc:
        raise RuleDefinitionError(
            f"Rule set is not valid JSON: {exc.msg} (line {exc.lineno}, column {exc.colno})."
        ) from None
    validate_rule_set(doc)
    return doc


def load_rule_set_file(path: str | os.PathLike[str]) -> dict[str, Any]:
    """Read, parse and validate a rule-set JSON file (see :func:`parse_rule_set_json`).

    Raises :class:`OSError` when the file cannot be read and :class:`RuleDefinitionError` when
    its content is invalid.
    """
    return parse_rule_set_json(Path(path).read_bytes())


# ---------------------------------------------------------------------------
# Clinical conditions
# ---------------------------------------------------------------------------


def format_age_months(months: int) -> str:
    """Human-readable age from months: ``"2 months"``, ``"12 months"``, ``"30 years"``, ``"2 years 6 months"``.

    Ages below two years are given in months, older ages in years (and remaining months).
    """
    if months < 24:
        return f"{months} month{'' if months == 1 else 's'}"
    years, rest = divmod(months, 12)
    text = f"{years} years"
    return f"{text} {rest} month{'' if rest == 1 else 's'}" if rest else text


def _drug_list(classes: Iterable[str], conjunction: str) -> str:
    """``"insulin"``, ``"insulin or metformin"``, ``"insulin, metformin or a sulfonylurea"``."""
    selected = set(classes)
    phrases = [_DRUG_PHRASES[drug_class] for drug_class in DRUG_CLASSES if drug_class in selected]
    if len(phrases) == 1:
        return phrases[0]
    return f"{', '.join(phrases[:-1])} {conjunction} {phrases[-1]}"


def _condition_holds(conditions: Mapping[str, Any] | None, ctx: ClinicalContext) -> tuple[bool, str]:
    """:func:`clinical_condition_holds` for an already normalised condition."""
    if not conditions:
        return True, "no therapy condition"
    met: list[str] = []
    failed: list[str] = []

    any_of = conditions.get(ANY_OF_DRUG_CLASSES)
    if any_of:
        receiving = ctx.drug_classes & set(any_of)
        if receiving:
            met.append(f"receiving {_drug_list(receiving, 'and')}")
        elif not ctx.drug_classes:
            failed.append("no current glucose-lowering medication")
        else:
            failed.append(f"not receiving {_drug_list(any_of, 'or')}")

    none_of = conditions.get(NONE_OF_DRUG_CLASSES)
    if none_of:
        excluded = ctx.drug_classes & set(none_of)
        if excluded:
            # An exclusion explains the note on its own: the patient already receives the therapy
            # the rule would recommend, so a missing required therapy is not reported as well.
            failed = [f"already receiving {_drug_list(excluded, 'and')}"]
        else:
            met.append(f"not receiving {_drug_list(none_of, 'or')}")

    max_age = conditions.get(MAX_AGE_AT_DIAGNOSIS_MONTHS)
    if max_age is not None:
        age = ctx.age_at_diagnosis_months
        if age is None:
            failed.append("age at diagnosis unknown")
        elif age < max_age:
            met.append(f"diagnosed at {format_age_months(age)} (before {format_age_months(max_age)})")
        else:
            failed.append(
                f"diagnosed at {format_age_months(age)} (rule requires diagnosis before {format_age_months(max_age)})"
            )

    return (False, "; ".join(failed)) if failed else (True, "; ".join(met))


def clinical_condition_holds(clinical: Mapping[str, Any] | None, ctx: ClinicalContext) -> tuple[bool, str]:
    """Return (holds, human-readable reason) for a rule's therapy/diagnosis condition.

    All present conditions must hold (SPEC §6.1): ``any_of_drug_classes`` — a current drug
    class is in the list; ``none_of_drug_classes`` — no current drug class is in the list;
    ``max_age_at_diagnosis_months`` — the age at diagnosis is known and below the value.

    When the condition fails, the reason names the failed parts joined by ``"; "``, e.g.
    ``"already receiving a sulfonylurea"``, ``"no current glucose-lowering medication"`` or
    ``"not receiving insulin; age at diagnosis unknown"``. An exclusion (``none_of``) failure
    takes precedence over a missing required therapy (``any_of``); an unmet diagnosis-age
    condition is always reported. When it holds, the reason says why (``"receiving insulin;
    not receiving a sulfonylurea"``).

    Raises :class:`RuleDefinitionError` for a malformed condition.
    """
    return _condition_holds(_normalize_clinical(clinical, "Clinical condition"), ctx)


def describe_clinical_condition(clinical: Mapping[str, Any] | None) -> str:
    """Short human-readable description of a clinical condition (used by the UI).

    Examples: ``"On insulin or metformin · not on a sulfonylurea"``, ``"Any glucose-lowering
    medication"``, ``"On insulin · diagnosed before 12 months of age"``, ``"No therapy condition"``.
    """
    conditions = _normalize_clinical(clinical, "Clinical condition")
    if not conditions:
        return "No therapy condition"
    parts: list[str] = []
    any_of = conditions.get(ANY_OF_DRUG_CLASSES)
    if any_of:
        parts.append(
            "Any glucose-lowering medication" if set(any_of) == set(DRUG_CLASSES) else f"On {_drug_list(any_of, 'or')}"
        )
    none_of = conditions.get(NONE_OF_DRUG_CLASSES)
    if none_of:
        parts.append(
            "not on any glucose-lowering medication"
            if set(none_of) == set(DRUG_CLASSES)
            else f"not on {_drug_list(none_of, 'or')}"
        )
    max_age = conditions.get(MAX_AGE_AT_DIAGNOSIS_MONTHS)
    if max_age is not None:
        parts.append(f"diagnosed before {format_age_months(max_age)} of age")
    text = " · ".join(parts)
    return text[0].upper() + text[1:]


def describe_genetic_criterion(rule: RuleSpec) -> str:
    """Short description of a rule's genetic criterion (used by the UI and CLI).

    Examples: ``"HNF1A · P/LP · heterozygous"``, ``"KCNJ11 or ABCC8 · P/LP · activating"``,
    ``"Any of the 7 panel genes · P/LP · heterozygous"``.
    """
    genes = [gene for gene in GENES if gene in rule.genes]
    if len(genes) == len(GENES):
        gene_text = f"Any of the {len(GENES)} panel genes"
    elif len(genes) == 1:
        gene_text = genes[0]
    else:
        gene_text = f"{', '.join(genes[:-1])} or {genes[-1]}"
    parts = [gene_text, "/".join(cls for cls in CLASSES if cls in rule.classes)]
    if rule.mechanism is not None:
        parts.append(rule.mechanism)
    if rule.zygosity is not None:
        parts.append(_zygosity_text(rule.zygosity))
    return " · ".join(parts)


# ---------------------------------------------------------------------------
# Algorithm 1
# ---------------------------------------------------------------------------


def rule_code_sort_key(code: str) -> tuple[int, str]:
    """Natural order of rule codes (D1 < D2 < … < D10); codes not of the D<n> form sort last."""
    return (int(code[1:]), code) if RULE_CODE_PATTERN.fullmatch(code) else (sys.maxsize, code)


def _zygosity_text(zygosities: Iterable[str]) -> str:
    """``"heterozygous"``, ``"heterozygous or hemizygous"`` (in :data:`ZYGOSITIES` order)."""
    selected = set(zygosities)
    names = [zygosity for zygosity in ZYGOSITIES if zygosity in selected]
    return names[0] if len(names) == 1 else f"{', '.join(names[:-1])} or {names[-1]}"


def _matches_genetic(rule: RuleSpec, variant: ClassifiedVariant) -> bool:
    """Gene, class and (when the rule names one) mechanism match; zygosity is checked separately."""
    return (
        variant.gene in rule.genes
        and variant.classification in rule.classes
        and (rule.mechanism is None or variant.mechanism == rule.mechanism)
    )


def _zygosity_matches(rule: RuleSpec, variant: ClassifiedVariant) -> bool:
    return rule.zygosity is None or variant.zygosity in rule.zygosity


def _note_message(rule: RuleSpec, variant: ClassifiedVariant, reason: str) -> str:
    criterion = f"{variant.gene} {CLASS_LABELS[variant.classification].lower()}"
    if rule.mechanism is not None:
        criterion += f", {rule.mechanism}"
    return (
        f"Genetic criterion of {rule.code} met ({criterion}) but therapy condition not met: {reason}. "
        "No alert raised."
    )


def _zygosity_note_message(rule: RuleSpec, variant: ClassifiedVariant) -> str:
    """Why a rule whose gene/class/mechanism criterion is met does not apply to this genotype."""
    return (
        f"Rule {rule.code} applies to {_zygosity_text(rule.zygosity or ())} carriers; this {variant.gene} "
        f"{CLASS_LABELS[variant.classification].lower()} variant is {variant.zygosity} — possible "
        "recessive/biallelic form, refer to a specialist. No alert raised."
    )


def evaluate(
    variants: Iterable[ClassifiedVariant], ctx: ClinicalContext, rules: Iterable[RuleSpec]
) -> list[Finding]:
    """Algorithm 1: alert and note findings in deterministic (rule code, variant) order.

    For each rule and each variant whose gene, class and (when the rule names one) mechanism
    meet the rule's genetic criterion: if the rule restricts zygosity and the variant's is not
    listed (e.g. a homozygous variant for a rule of heterozygous carriers), a **note** finding
    explaining that the rule does not apply to this genotype (possible recessive/biallelic form);
    otherwise, if the rule has no clinical condition or it holds, an **alert** finding (rule
    severity, recommendation, source), else a **note** finding (severity None) whose message
    explains which clinical condition failed. Rules are ordered by natural code order, variants
    by ``variant_key_id``.

    Raises :class:`RuleDefinitionError` when ``rules`` holds more than one version of a code and
    :class:`ValueError` when a variant key appears more than once — either would produce two
    findings for the same (rule, variant).
    """
    rule_list = sorted(rules, key=lambda rule: (rule_code_sort_key(rule.code), rule.version))
    for previous, rule in zip(rule_list, rule_list[1:]):
        if previous.code == rule.code:
            raise RuleDefinitionError(
                f"Cannot evaluate {previous.label} and {rule.label} together; pass one version per rule code."
            )
    variant_list = sorted(variants, key=lambda variant: variant.variant_key_id)
    for previous, variant in zip(variant_list, variant_list[1:]):
        if previous.variant_key_id == variant.variant_key_id:
            raise ValueError(f"Variant key {variant.variant_key_id} is listed more than once.")

    findings: list[Finding] = []
    for rule in rule_list:
        matching = [variant for variant in variant_list if _matches_genetic(rule, variant)]
        if not matching:
            continue
        holds, reason = _condition_holds(rule.clinical, ctx)
        for variant in matching:
            if not _zygosity_matches(rule, variant):
                alert, message = False, _zygosity_note_message(rule, variant)
            else:
                alert = holds
                message = rule.recommendation if holds else _note_message(rule, variant, reason)
            findings.append(
                Finding(
                    rule_code=rule.code,
                    rule_version_id=rule.rule_version_id,
                    variant_key_id=variant.variant_key_id,
                    patient_variant_id=variant.patient_variant_id,
                    kind="alert" if alert else "note",
                    severity=rule.severity if alert else None,
                    title=rule.title,
                    message=message,
                    source=rule.source,
                    classification=variant.classification,
                )
            )
    return findings
