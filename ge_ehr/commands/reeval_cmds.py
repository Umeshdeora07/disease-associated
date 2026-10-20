"""``flask reeval ...`` — run and inspect re-evaluations (SPEC §7).

Owned by the Reeval work package. Interface stub created by the Foundation work package: the
group is registered so ``flask reeval --help`` works; the Reeval package adds the subcommands.
"""

from __future__ import annotations

from flask import Flask
from flask.cli import AppGroup

reeval_cli = AppGroup("reeval", help="Run full recomputations and inspect re-evaluation runs.")


def register(app: Flask) -> None:
    """Register the ``reeval`` command group."""
    app.cli.add_command(reeval_cli)
