"""Forms of the administration blueprint.

Owned by the Foundation work package.
"""

from __future__ import annotations

from flask_wtf import FlaskForm
from sqlalchemy import select
from wtforms import BooleanField, Field, PasswordField, SelectField, StringField, SubmitField
from wtforms.validators import DataRequired, EqualTo, Length, Optional, Regexp, ValidationError

from .. import constants as C
from ..auth.forms import PASSWORD_HELP, PasswordPolicy
from ..extensions import db
from ..models import User
from ..security import EMAIL_PATTERN, USERNAME_PATTERN, USERNAME_RULES


def _normalise(value: str | None) -> str | None:
    return value.strip().lower() if value else value


def _strip(value: str | None) -> str | None:
    return value.strip() if value else value


class NewUserForm(FlaskForm):
    username = StringField(
        "Username",
        filters=[_normalise],
        validators=[DataRequired(message="Enter a username."), Regexp(USERNAME_PATTERN, message=USERNAME_RULES)],
        description=USERNAME_RULES,
        render_kw={"autocomplete": "off", "autocapitalize": "none", "spellcheck": "false"},
    )
    full_name = StringField(
        "Full name",
        filters=[_strip],
        validators=[DataRequired(message="Enter the user's full name."), Length(max=128)],
        description='As shown in the audit log and on reviews, e.g. "Dr. A. Rao".',
    )
    email = StringField(
        "Email",
        filters=[_strip],
        validators=[
            Optional(),
            Length(max=255),
            Regexp(EMAIL_PATTERN, message="Enter a valid email address, e.g. name@example.org."),
        ],
        description="Optional.",
        render_kw={"autocomplete": "off", "type": "email"},
    )
    role = SelectField(
        "Role",
        choices=[(role, C.ROLE_LABELS[role]) for role in C.ROLES],
        default="clinician",
        description="Lab technicians can upload VCFs but cannot see recommendations or alerts.",
    )
    password = PasswordField(
        "Initial password",
        validators=[DataRequired(message="Enter an initial password."), PasswordPolicy()],
        description=PASSWORD_HELP,
        render_kw={"autocomplete": "new-password"},
    )
    confirm_password = PasswordField(
        "Confirm initial password",
        validators=[
            DataRequired(message="Repeat the initial password."),
            EqualTo("password", message="The two passwords do not match."),
        ],
        render_kw={"autocomplete": "new-password"},
    )
    must_change_password = BooleanField("Require a password change at first sign-in", default=True)
    submit = SubmitField("Create user")

    def validate_username(self, field: Field) -> None:
        if db.session.execute(select(User.id).filter_by(username=field.data)).first() is not None:
            raise ValidationError("A user with this username already exists.")
