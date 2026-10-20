"""``flask clinvar ...`` — import, activate, list and sync knowledge-base releases (SPEC §5.3).

Owned by the KB work package. Interface stub created by the Foundation work package: the group
is registered so ``flask clinvar --help`` works; the KB package adds the subcommands.
"""

from __future__ import annotations

from flask import Flask
from flask.cli import AppGroup

clinvar_cli = AppGroup("clinvar", help="Import, activate, list and sync ClinVar knowledge-base releases.")


def register(app: Flask) -> None:
    """Register the ``clinvar`` command group."""
    app.cli.add_command(clinvar_cli)
