"""Flask CLI command modules; each exposes ``register(app)``.

Owned by the Foundation work package (the individual modules belong to the work packages
named in their docstrings).
"""

from __future__ import annotations

from flask import Flask

from . import alert_cmds, clinvar_cmds, demo_cmds, patient_cmds, reeval_cmds, rules_cmds, user_cmds

# patient_cmds and alert_cmds (the terminal patient/alert workflow) belong to the CLI work package.
COMMAND_MODULES = (user_cmds, clinvar_cmds, rules_cmds, reeval_cmds, demo_cmds, patient_cmds, alert_cmds)


def register_commands(app: Flask) -> None:
    """Register the commands of every module in :data:`COMMAND_MODULES` on ``app.cli``."""
    for module in COMMAND_MODULES:
        module.register(app)
