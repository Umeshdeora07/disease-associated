"""Forms of the knowledge blueprint (Flask-WTF, CSRF-protected).

The forms check what a browser can get wrong (a file is chosen, its name has the right ending,
lengths, dates, choices). The content of an uploaded file is validated by the services that
consume it (:func:`ge_ehr.services.clinvar.import_release`,
:func:`ge_ehr.services.rules.parse_rule_set_json`), whose messages are shown next to the field.

Owned by the UI-B work package.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from flask_wtf import FlaskForm
from flask_wtf.file import FileField, FileRequired
from werkzeug.datastructures import FileStorage
from wtforms import BooleanField, DateField, Field, SelectField, StringField, SubmitField, TextAreaField
from wtforms.validators import Length, Optional, ValidationError

from ..models import KbRelease

#: File name endings accepted for a ClinVar(-format) release (plain text or gzip/bgzip compressed).
CLINVAR_SUFFIXES: tuple[str, ...] = (".vcf", ".vcf.gz")
OVERLAY_SUFFIXES: tuple[str, ...] = (".csv",)
RULE_SET_SUFFIXES: tuple[str, ...] = (".json",)
#: How :func:`ge_ehr.services.clinvar.load_expert_panel_overlay` starts every error message.
OVERLAY_ERROR_PREFIX = "Expert-panel overlay"
#: Largest pasted rule set accepted (the bundled rule sets are about 5 KB).
MAX_RULE_SET_CHARS = 200_000
NOTES_MAX_LENGTH = 2_000

#: Re-evaluation strategies offered for an activation or a rule-set import (SPEC §7.1–§7.3).
STRATEGY_CHOICES: list[tuple[str, str]] = [
    ("change_driven", "Change-driven (GE-EHR) — re-evaluate only the carriers of what changed"),
    ("full", "Full recomputation — re-evaluate every patient"),
]
STRATEGY_HELP = (
    "Both strategies reach the same alerts. Change-driven examines far fewer records; full recomputation is the "
    "reference strategy of the evaluation."
)


def _strip(value: Any) -> Any:
    return value.strip() if isinstance(value, str) else value


def _max_length(column: Any) -> int:
    """Declared length of a String column, so form limits always match the database."""
    return int(column.property.columns[0].type.length)


class FileNameEndsWith:
    """Accept only file names with one of ``suffixes`` (case-insensitive); no file passes.

    Combine with :class:`~flask_wtf.file.FileRequired` when a file is mandatory. The file's
    content is validated by the service that reads it.
    """

    def __init__(self, suffixes: Sequence[str], message: str) -> None:
        self.suffixes = tuple(suffix.lower() for suffix in suffixes)
        self.message = message

    def __call__(self, form: FlaskForm, field: Field) -> None:
        storage = field.data
        if not isinstance(storage, FileStorage) or not storage.filename:
            return
        if not storage.filename.lower().endswith(self.suffixes):
            raise ValidationError(self.message)


class IsoDateField(DateField):
    """``<input type="date">`` whose parse error names the expected format."""

    def process_formdata(self, valuelist: list[Any]) -> None:
        try:
            super().process_formdata(valuelist)
        except ValueError:
            raise ValueError("Enter a date in YYYY-MM-DD format.") from None


def _strategy_field() -> SelectField:
    return SelectField(
        "Re-evaluation strategy", choices=STRATEGY_CHOICES, default="change_driven", description=STRATEGY_HELP
    )


class ImportReleaseForm(FlaskForm):
    """Upload a ClinVar(-format) GRCh38 VCF as a new knowledge-base release."""

    vcf_file = FileField(
        "ClinVar VCF (GRCh38)",
        validators=[
            FileRequired(message="Choose a ClinVar VCF file to import."),
            FileNameEndsWith(CLINVAR_SUFFIXES, "Choose a ClinVar VCF whose name ends in .vcf or .vcf.gz."),
        ],
        description=(
            "A ClinVar GRCh38 VCF (vcf_GRCh38) or a 7-gene subset of it, plain or gzip/bgzip-compressed. "
            "Records outside the seven panel genes are skipped."
        ),
        render_kw={"accept": ".vcf,.gz"},
    )
    label = StringField(
        "Release label",
        filters=[_strip],
        validators=[Optional(), Length(max=_max_length(KbRelease.label))],
        description='Shown wherever the release is cited, e.g. "ClinVar 2026-01". Default: "ClinVar <fileDate>".',
        render_kw={"autocomplete": "off", "placeholder": "ClinVar <fileDate>"},
    )
    release_date = IsoDateField(
        "Release date",
        validators=[Optional()],
        description="Default: the file's ##fileDate header.",
    )
    expert_panel_csv = FileField(
        "Expert-panel overlay (optional)",
        validators=[FileNameEndsWith(OVERLAY_SUFFIXES, "Choose an overlay file whose name ends in .csv.")],
        description=(
            "CSV with the columns clinvar_variation_id, classification, panel, date. Matching variants take the "
            "expert-panel classification and are flagged as expert-panel reviewed."
        ),
        render_kw={"accept": ".csv,text/csv"},
    )
    notes = TextAreaField(
        "Notes",
        filters=[_strip],
        validators=[Optional(), Length(max=NOTES_MAX_LENGTH)],
        description="Optional, e.g. where the file came from.",
        render_kw={"rows": 2},
    )
    activate = BooleanField(
        "Activate and re-evaluate now",
        default=False,
        description=(
            "Makes the release current and re-evaluates patients right away. Leave it unticked to compare the "
            "release with the current one first and activate it from its page."
        ),
    )
    strategy = _strategy_field()
    submit = SubmitField("Import release")

    def attach_import_error(self, message: str) -> None:
        """Show an error of the import service: overlay problems at the overlay, others at the VCF."""
        field = self.expert_panel_csv if message.startswith(OVERLAY_ERROR_PREFIX) else self.vcf_file
        field.errors.append(message)


class ActivateReleaseForm(FlaskForm):
    """Make a stored release current (re-evaluating carriers with the chosen strategy)."""

    strategy = _strategy_field()
    submit = SubmitField("Activate and re-evaluate")


class ImportRulesForm(FlaskForm):
    """Upload or paste a rule-set JSON document (SPEC §6.1)."""

    rules_file = FileField(
        "Rule-set file",
        validators=[FileNameEndsWith(RULE_SET_SUFFIXES, "Choose a rule-set file whose name ends in .json.")],
        description="A JSON rule set such as data/rules/ruleset_v2.json.",
        render_kw={"accept": ".json,application/json"},
    )
    rules_json = TextAreaField(
        "Or paste the rule-set JSON",
        validators=[
            Optional(),
            Length(max=MAX_RULE_SET_CHARS, message=f"Paste at most {MAX_RULE_SET_CHARS:,} characters."),
        ],
        render_kw={"rows": 12, "spellcheck": "false", "autocomplete": "off"},
    )
    strategy = _strategy_field()
    validate_only = SubmitField("Validate only")
    submit = SubmitField("Import and re-evaluate")

    def validate(self, extra_validators: Any = None) -> bool:
        """Standard validation plus: exactly one of the file and the pasted text is given."""
        valid = super().validate(extra_validators)
        has_file = isinstance(self.rules_file.data, FileStorage) and bool(self.rules_file.data.filename)
        has_text = bool((self.rules_json.data or "").strip())
        if not has_file and not has_text:
            self.rules_file.errors.append("Choose a rule-set JSON file or paste the JSON below.")
            return False
        if has_file and has_text:
            self.rules_json.errors.append("Provide either a file or pasted JSON, not both.")
            return False
        return valid

    @property
    def source_name(self) -> str:
        """Where the document came from, for messages: the file name or "pasted JSON"."""
        storage = self.rules_file.data
        if isinstance(storage, FileStorage) and storage.filename:
            return storage.filename
        return "pasted JSON"

    def document(self) -> bytes | str:
        """The rule-set text: the uploaded file's bytes or the pasted JSON."""
        storage = self.rules_file.data
        if isinstance(storage, FileStorage) and storage.filename:
            return storage.read()
        return self.rules_json.data or ""

    def attach_rule_error(self, message: str) -> None:
        """Show a rule-set validation message next to the input it concerns."""
        storage = self.rules_file.data
        field = self.rules_file if isinstance(storage, FileStorage) and storage.filename else self.rules_json
        field.errors.append(message)
