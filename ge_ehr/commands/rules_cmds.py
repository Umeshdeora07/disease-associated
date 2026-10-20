"""``flask rules ...`` — import and list decision-support rule sets (SPEC §6).

Owned by the Rules work package. Interface stub created by the Foundation work package: the
group is registered so ``flask rules --help`` works; the Rules package adds the subcommands.
"""

from __future__ import annotations

from flask import Flask
from flask.cli import AppGroup

rules_cli = AppGroup("rules", help="Import and list versioned decision-support rules.")


def register(app: Flask) -> None:
    """Register the ``rules`` command group."""
    app.cli.add_command(rules_cli)
