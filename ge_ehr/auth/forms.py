"""Forms of the authentication blueprint (and the shared password-policy validator).

Owned by the Foundation work package.
"""

from __future__ import annotations

from flask_wtf import FlaskForm
from wtforms import Field, PasswordField, StringField, SubmitField
from wtforms.validators import DataRequired, EqualTo, Length, ValidationError

from ..security import PASSWORD_MAX_LENGTH, PASSWORD_MIN_LENGTH, password_policy_problems

PASSWORD_HELP = f"At least {PASSWORD_MIN_LENGTH} characters, including letters and digits."


class PasswordPolicy:
    """WTForms validator enforcing :func:`ge_ehr.security.password_policy_problems`."""

    def __call__(self, form: FlaskForm, field: Field) -> None:
        problems = password_policy_problems(field.data or "")
        if problems:
            raise ValidationError(" ".join(problems))


class LoginForm(FlaskForm):
    username = StringField(
        "Username",
        validators=[DataRequired(message="Enter your username."), Length(max=64)],
        render_kw={"autocomplete": "username", "autocapitalize": "none", "spellcheck": "false", "autofocus": True},
    )
    password = PasswordField(
        "Password",
        validators=[DataRequired(message="Enter your password."), Length(max=PASSWORD_MAX_LENGTH)],
        render_kw={"autocomplete": "current-password"},
    )
    submit = SubmitField("Sign in")


class ChangePasswordForm(FlaskForm):
    current_password = PasswordField(
        "Current password",
        validators=[DataRequired(message="Enter your current password.")],
        render_kw={"autocomplete": "current-password"},
    )
    new_password = PasswordField(
        "New password",
        validators=[DataRequired(message="Enter a new password."), PasswordPolicy()],
        description=PASSWORD_HELP,
        render_kw={"autocomplete": "new-password"},
    )
    confirm_password = PasswordField(
        "Confirm new password",
        validators=[
            DataRequired(message="Repeat the new password."),
            EqualTo("new_password", message="The two new passwords do not match."),
        ],
        render_kw={"autocomplete": "new-password"},
    )
    submit = SubmitField("Change password")

    def validate_new_password(self, field: Field) -> None:
        if field.data and field.data == self.current_password.data:
            raise ValidationError("The new password must be different from the current password.")
