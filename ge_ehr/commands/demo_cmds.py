"""``flask seed-demo [--full] [--reset]`` — load the synthetic demo dataset (SPEC §11).

Owned by the Scenario work package. Interface stub created by the Foundation work package.
"""

from __future__ import annotations

import click
from flask import Flask
from flask.cli import with_appcontext


@click.command("seed-demo")
@click.option("--full", is_flag=True, help="Also import rules v2 and activate release R2.")
@click.option("--reset", is_flag=True, help="Drop and recreate all tables before seeding.")
@with_appcontext
def seed_demo_command(full: bool, reset: bool) -> None:
    """Seed demo users, releases, rules and patients."""
    raise click.ClickException("not implemented yet")


def register(app: Flask) -> None:
    """Register the ``seed-demo`` command."""
    app.cli.add_command(seed_demo_command)
