"""Forms of the patients blueprint (Flask-WTF, CSRF-protected).

The forms check what a browser can get wrong (required values, lengths, number and date formats,
choices); the patient-record service (:mod:`ge_ehr.services.patient_records`) remains the single
source of truth for the domain rules (MRN format, dates relative to the birth date, …). Its
:class:`ValueError` messages start with the field label they concern, so
:meth:`ServiceErrorsMixin.attach_service_error` can show them next to the right field.

Owned by the UI-A work package.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any, ClassVar

from flask_wtf import FlaskForm
from flask_wtf.file import FileField, FileRequired
from sqlalchemy.orm import InstrumentedAttribute
from wtforms import DateField, Field, IntegerField, SelectField, StringField, TextAreaField
from wtforms.validators import DataRequired, Length, NumberRange, Optional, ValidationError

from .. import constants as C
from ..models import Diagnosis, Medication, Patient
from ..services.patient_records import MAX_AGE_AT_DIAGNOSIS_MONTHS, NOTES_MAX_LENGTH

#: File name endings accepted by the VCF upload form (plain text or gzip/bgzip compressed).
VCF_SUFFIXES: tuple[str, ...] = (".vcf", ".vcf.gz")
#: Value of the drug-class choice that lets the server suggest the class from the drug name.
SUGGEST_DRUG_CLASS = ""
#: Common glucose-lowering drugs offered as suggestions in the medication form (any name is accepted).
COMMON_DIABETES_DRUGS: tuple[str, ...] = (
    "Insulin glargine",
    "Insulin degludec",
    "Insulin detemir",
    "Insulin lispro",
    "Insulin aspart",
    "Human insulin (isophane)",
    "Metformin",
    "Metformin extended-release",
    "Gliclazide",
    "Glimepiride",
    "Glipizide",
    "Glibenclamide",
    "Tolbutamide",
    "Sitagliptin",
    "Linagliptin",
    "Empagliflozin",
    "Dapagliflozin",
    "Liraglutide",
    "Semaglutide",
    "Dulaglutide",
    "Pioglitazone",
    "Acarbose",
)


def _strip(value: Any) -> Any:
    return value.strip() if isinstance(value, str) else value


def _max_length(column: InstrumentedAttribute[Any]) -> int:
    """Declared length of a String column, so form limits always match the database."""
    return column.property.columns[0].type.length


def _choices(values: Sequence[str], labels: Mapping[str, str]) -> list[tuple[str, str]]:
    return [(value, labels[value]) for value in values]


class IsoDateField(DateField):
    """``<input type="date">`` whose parse error names the expected format."""

    def process_formdata(self, valuelist: list[Any]) -> None:
        try:
            super().process_formdata(valuelist)
        except ValueError:
            raise ValueError("Enter a date in YYYY-MM-DD format.") from None


class MonthsField(IntegerField):
    """A whole number of months (``<input type="number">``) with a precise parse error."""

    def process_formdata(self, valuelist: list[Any]) -> None:
        try:
            super().process_formdata(valuelist)
        except ValueError:
            raise ValueError("Enter a whole number of months, e.g. 2 or 180.") from None


class ServiceErrorsMixin:
    """Shows a service :class:`ValueError` message next to the field it concerns."""

    #: Leading text of a service message → name of the field it belongs to.
    SERVICE_FIELDS: ClassVar[Mapping[str, str]] = {}

    def attach_service_error(self, message: str) -> None:
        """Append ``message`` to the field whose label starts it (longest match wins).

        Call after validation, when the field error lists exist. A message that names no known
        field becomes a form-level error, shown by the ``render_form_errors`` macro.
        """
        for prefix in sorted(self.SERVICE_FIELDS, key=len, reverse=True):
            if message.startswith(prefix):
                field: Field = getattr(self, self.SERVICE_FIELDS[prefix])
                field.errors.append(message)
                return
        self.form_errors.append(message)  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# Patient demographics and diagnosis
# ---------------------------------------------------------------------------

_DIAGNOSIS_SERVICE_FIELDS: dict[str, str] = {
    "Diabetes type": "diabetes_type",
    "Age at diagnosis": "age_at_diagnosis_months",
    "Diagnosis date": "diagnosed_on",
}
_AGE_AT_DIAGNOSIS_HELP = (
    "Whole months, e.g. 2 for neonatal diabetes or 180 for diagnosis at 15 years. "
    "Rule D4 needs diagnosis before 12 months of age."
)


def _diabetes_type_field() -> SelectField:
    return SelectField(
        "Diabetes type",
        choices=_choices(C.DIABETES_TYPES, C.DIABETES_TYPE_LABELS),
        default="unknown",
        description='Choose "No diabetes" for relatives tested for cascade screening.',
    )


def _age_at_diagnosis_field() -> MonthsField:
    return MonthsField(
        "Age at diagnosis (months)",
        validators=[
            Optional(),
            NumberRange(
                min=0,
                max=MAX_AGE_AT_DIAGNOSIS_MONTHS,
                message=f"Enter a number of months between 0 and {MAX_AGE_AT_DIAGNOSIS_MONTHS:,}.",
            ),
        ],
        description=_AGE_AT_DIAGNOSIS_HELP,
    )


def _diagnosed_on_field() -> IsoDateField:
    return IsoDateField("Diagnosis date", validators=[Optional()], description="Optional.")


class PatientForm(ServiceErrorsMixin, FlaskForm):
    """Demographics plus the diabetes diagnosis (new-patient and edit-patient pages)."""

    SERVICE_FIELDS: ClassVar[Mapping[str, str]] = {
        "MRN": "mrn",
        "A patient with MRN": "mrn",
        "Given name": "given_name",
        "Family name": "family_name",
        "Sex": "sex",
        "Birth date": "birth_date",
        "Family ID": "family_id",
        "External ID": "external_id",
        "Notes": "notes",
        **_DIAGNOSIS_SERVICE_FIELDS,
    }

    mrn = StringField(
        "MRN",
        filters=[_strip],
        validators=[DataRequired(message="Enter the medical record number."), Length(max=_max_length(Patient.mrn))],
        description="Unique medical record number: letters and digits, optionally separated by . _ / -, e.g. DEMO-0016.",
        render_kw={"autocomplete": "off", "spellcheck": "false"},
    )
    given_name = StringField(
        "Given name",
        filters=[_strip],
        validators=[DataRequired(message="Enter the given name."), Length(max=_max_length(Patient.given_name))],
        render_kw={"autocomplete": "off"},
    )
    family_name = StringField(
        "Family name",
        filters=[_strip],
        validators=[DataRequired(message="Enter the family name."), Length(max=_max_length(Patient.family_name))],
        render_kw={"autocomplete": "off"},
    )
    sex = SelectField("Sex", choices=_choices(C.SEXES, C.SEX_LABELS), default="unknown")
    birth_date = IsoDateField("Birth date", validators=[DataRequired(message="Enter the birth date.")])
    family_id = StringField(
        "Family ID",
        filters=[_strip],
        validators=[Optional(), Length(max=_max_length(Patient.family_id))],
        description="Optional. Relatives share a family ID (e.g. FAM-RS6), which links them for cascade testing.",
        render_kw={"autocomplete": "off", "spellcheck": "false"},
    )
    external_id = StringField(
        "External ID",
        filters=[_strip],
        validators=[Optional(), Length(max=_max_length(Patient.external_id))],
        description="Optional identifier in a source system, e.g. a Synthea patient UUID.",
        render_kw={"autocomplete": "off", "spellcheck": "false"},
    )
    notes = TextAreaField(
        "Notes",
        filters=[_strip],
        validators=[Optional(), Length(max=NOTES_MAX_LENGTH)],
        description="Optional clinical context. Synthetic data only — never enter real patient information.",
        render_kw={"rows": 3},
    )
    diabetes_type = _diabetes_type_field()
    age_at_diagnosis_months = _age_at_diagnosis_field()
    diagnosed_on = _diagnosed_on_field()

    #: Fields passed to the patient-record service as demographics.
    DEMOGRAPHIC_FIELDS: ClassVar[tuple[str, ...]] = (
        "mrn",
        "given_name",
        "family_name",
        "sex",
        "birth_date",
        "family_id",
        "external_id",
        "notes",
    )
    #: Fields passed to the patient-record service as the diagnosis.
    DIAGNOSIS_FIELDS: ClassVar[tuple[str, ...]] = ("diabetes_type", "age_at_diagnosis_months", "diagnosed_on")

    def demographics(self) -> dict[str, Any]:
        """The submitted demographic values (the service turns blank optional text into None)."""
        return {name: getattr(self, name).data for name in self.DEMOGRAPHIC_FIELDS}

    def diagnosis(self) -> dict[str, Any]:
        """The submitted diagnosis values."""
        return {name: getattr(self, name).data for name in self.DIAGNOSIS_FIELDS}


class DiagnosisForm(ServiceErrorsMixin, FlaskForm):
    """Inline diagnosis editor of the patient page (field names prefixed ``dx-``)."""

    SERVICE_FIELDS: ClassVar[Mapping[str, str]] = {**_DIAGNOSIS_SERVICE_FIELDS, "Diagnosis notes": "notes"}

    diabetes_type = _diabetes_type_field()
    age_at_diagnosis_months = _age_at_diagnosis_field()
    diagnosed_on = _diagnosed_on_field()
    notes = TextAreaField(
        "Diagnosis notes",
        filters=[_strip],
        validators=[Optional(), Length(max=NOTES_MAX_LENGTH)],
        description="Optional, e.g. antibody status or C-peptide.",
        render_kw={"rows": 2},
    )

    def values(self) -> dict[str, Any]:
        """Keyword arguments for :func:`~ge_ehr.services.patient_records.update_diagnosis`."""
        return {
            "diabetes_type": self.diabetes_type.data,
            "age_at_diagnosis_months": self.age_at_diagnosis_months.data,
            "diagnosed_on": self.diagnosed_on.data,
            "notes": self.notes.data or None,
        }

    @classmethod
    def for_diagnosis(cls, diagnosis: Diagnosis | None, **kwargs: Any) -> DiagnosisForm:
        """The form pre-filled with ``diagnosis`` (posted data, when present, takes precedence)."""
        return cls(prefix="dx", obj=diagnosis, **kwargs)


# ---------------------------------------------------------------------------
# Medications
# ---------------------------------------------------------------------------


class MedicationForm(ServiceErrorsMixin, FlaskForm):
    """Add a current diabetes medication (field names prefixed ``med-``)."""

    SERVICE_FIELDS: ClassVar[Mapping[str, str]] = {
        "Drug name": "drug_name",
        "Drug class": "drug_class",
        "Dose": "dose",
        "Start date": "start_date",
    }

    drug_name = StringField(
        "Drug name",
        filters=[_strip],
        validators=[DataRequired(message="Enter the drug name."), Length(max=_max_length(Medication.drug_name))],
        description="Generic or brand name, e.g. Gliclazide 80 mg or Lantus. Record diabetes therapy only.",
        render_kw={"autocomplete": "off"},
    )
    drug_class = SelectField(
        "Drug class",
        choices=[(SUGGEST_DRUG_CLASS, "Suggest from the drug name")] + _choices(C.DRUG_CLASSES, C.DRUG_CLASS_LABELS),
        default=SUGGEST_DRUG_CLASS,
        description="The rules use the class, e.g. D2 does not fire for patients already on a sulfonylurea.",
    )
    dose = StringField(
        "Dose",
        filters=[_strip],
        validators=[Optional(), Length(max=_max_length(Medication.dose))],
        description="Optional, e.g. 40 mg twice daily.",
        render_kw={"autocomplete": "off"},
    )
    start_date = IsoDateField("Start date", validators=[Optional()], description="Optional.")

    def resolved_drug_class(self) -> tuple[str, bool]:
        """The chosen drug class, or the one suggested from the drug name; True when suggested."""
        if self.drug_class.data == SUGGEST_DRUG_CLASS:
            return C.suggest_drug_class(self.drug_name.data or ""), True
        return self.drug_class.data, False


def drug_suggestions() -> list[tuple[str, str]]:
    """``(drug name, suggested class label)`` pairs for the drug-name ``<datalist>``."""
    return [(name, C.DRUG_CLASS_LABELS[C.suggest_drug_class(name)]) for name in COMMON_DIABETES_DRUGS]


# ---------------------------------------------------------------------------
# VCF upload
# ---------------------------------------------------------------------------


class VcfFileName:
    """Accept only ``.vcf`` / ``.vcf.gz`` file names (the content is validated by the parser)."""

    def __call__(self, form: FlaskForm, field: Field) -> None:
        name = (getattr(field.data, "filename", None) or "").lower()
        if not name.endswith(VCF_SUFFIXES):
            raise ValidationError("Choose a VCF file whose name ends in .vcf or .vcf.gz.")


class VcfUploadForm(FlaskForm):
    """Patient VCF upload."""

    vcf_file = FileField(
        "Patient VCF",
        validators=[FileRequired(message="Choose a VCF file to upload."), VcfFileName()],
        render_kw={"accept": ".vcf,.gz"},
    )
