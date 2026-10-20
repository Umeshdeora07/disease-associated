"""Flask CLI wiring: command registration, ``flask shell`` context and output helpers.

Owned by the Foundation work package. Commands live in :mod:`ge_ehr.commands`.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from typing import Any

import click
from flask import Flask

from . import models
from .extensions import db


def register_cli(app: Flask) -> None:
    """Register every command group and the ``flask shell`` context."""
    from .commands import register_commands  # the command modules import this module

    register_commands(app)

    @app.shell_context_processor
    def shell_context() -> dict[str, Any]:
        return {"db": db, "models": models}


def echo_table(headers: Sequence[str], rows: Iterable[Sequence[Any]]) -> None:
    """Print rows as a left-aligned plain-text table (None shown as ``-``)."""
    cells = [[("-" if value is None else str(value)) for value in row] for row in rows]
    widths = [len(header) for header in headers]
    for row in cells:
        widths = [max(width, len(cell)) for width, cell in zip(widths, row, strict=True)]
    click.echo("  ".join(header.ljust(width) for header, width in zip(headers, widths, strict=True)).rstrip())
    click.echo("  ".join("-" * width for width in widths))
    for row in cells:
        click.echo("  ".join(cell.ljust(width) for cell, width in zip(row, widths, strict=True)).rstrip())
