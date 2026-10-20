"""Flask CLI command modules; each exposes ``register(app)``.

Owned by the Foundation work package (the individual modules belong to the work packages
named in their docstrings).
"""

from __future__ import annotations

from flask import Flask

from . import clinvar_cmds, demo_cmds, reeval_cmds, rules_cmds, user_cmds

COMMAND_MODULES = (user_cmds, clinvar_cmds, rules_cmds, reeval_cmds, demo_cmds)


def register_commands(app: Flask) -> None:
    """Register the commands of every module in :data:`COMMAND_MODULES` on ``app.cli``."""
    for module in COMMAND_MODULES:
        module.register(app)
