"""Database and user administration commands: ``init-db``, ``create-user``, ``list-users``.

Owned by the Foundation work package.
"""

from __future__ import annotations

import click
from flask import Flask
from flask.cli import with_appcontext
from sqlalchemy import select

from .. import constants as C
from ..cli import echo_table
from ..extensions import db
from ..models import User
from ..security import (
    EMAIL_PATTERN,
    USERNAME_RULES,
    audit,
    is_valid_username,
    normalize_username,
    password_policy_problems,
)


def _policy_checked(password: str, param_hint: str | None = None) -> str:
    """Return ``password`` or raise :class:`click.BadParameter` listing the policy violations.

    Also used as the ``click.prompt`` value processor, which re-prompts on the error.
    """
    problems = password_policy_problems(password)
    if problems:
        raise click.BadParameter(" ".join(problems), param_hint=param_hint)
    return password


@click.command("init-db")
@click.option("--drop", is_flag=True, help="Drop all tables first. DESTROYS ALL DATA.")
@click.option("--yes", is_flag=True, help="Do not ask for confirmation before dropping.")
@with_appcontext
def init_db_command(drop: bool, yes: bool) -> None:
    """Create all database tables (optionally dropping existing ones first)."""
    location = db.engine.url.render_as_string(hide_password=True)
    if drop:
        if not yes:
            click.confirm(f"Drop ALL tables and data in {location}?", abort=True)
        db.drop_all()
        click.echo("Dropped all tables.")
    db.create_all()
    click.echo(f"Database initialised: {len(db.metadata.tables)} tables in {location}.")


@click.command("create-user")
@click.argument("username")
@click.option("--role", type=click.Choice(C.ROLES), required=True, help="Role of the new account.")
@click.option("--full-name", required=True, help='Display name, e.g. "Dr. A. Rao".')
@click.option("--email", default=None, help="Optional email address.")
@click.option("--password", default=None, help="Initial password (prompted with hidden input when omitted).")
@click.option(
    "--must-change-password/--no-must-change-password",
    default=False,
    show_default=True,
    help="Force a password change at first sign-in.",
)
@with_appcontext
def create_user_command(
    username: str, role: str, full_name: str, email: str | None, password: str | None, must_change_password: bool
) -> None:
    """Create a user account."""
    username = normalize_username(username)
    if not is_valid_username(username):
        raise click.BadParameter(USERNAME_RULES, param_hint="'USERNAME'")
    if email is not None and not EMAIL_PATTERN.fullmatch(email):
        raise click.BadParameter(f"{email!r} is not a valid email address.", param_hint="'--email'")
    if db.session.execute(select(User.id).filter_by(username=username)).first() is not None:
        raise click.ClickException(f"A user named {username!r} already exists.")
    if password is None:
        password = click.prompt("Password", hide_input=True, confirmation_prompt=True, value_proc=_policy_checked)
    else:
        _policy_checked(password, param_hint="'--password'")

    user = User(
        username=username,
        full_name=full_name.strip(),
        email=email,
        role=role,
        must_change_password=must_change_password,
    )
    user.set_password(password)
    db.session.add(user)
    db.session.flush()
    audit("user_created", "user", user.id, detail={"username": username, "role": role, "via": "cli"})
    db.session.commit()
    click.echo(f"Created {C.ROLE_LABELS[role].lower()} account {username!r}.")


@click.command("list-users")
@with_appcontext
def list_users_command() -> None:
    """List all user accounts."""
    users = db.session.execute(select(User).order_by(User.username)).scalars().all()
    if not users:
        click.echo("No users. Create one with: flask create-user USERNAME --role admin --full-name NAME")
        return
    echo_table(
        ("USERNAME", "FULL NAME", "ROLE", "ACTIVE", "MUST CHANGE PW", "LAST LOGIN (UTC)"),
        (
            (
                user.username,
                user.full_name,
                user.role,
                "yes" if user.active else "no",
                "yes" if user.must_change_password else "no",
                user.last_login_at.strftime("%Y-%m-%d %H:%M") if user.last_login_at else None,
            )
            for user in users
        ),
    )


def register(app: Flask) -> None:
    """Register the commands of this module."""
    for command in (init_db_command, create_user_command, list_users_command):
        app.cli.add_command(command)
