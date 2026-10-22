"""Shared terminal helpers of ``flask patients …`` and ``flask alerts …``.

Formatting (output stays readable at :data:`WIDTH` = 100 columns):

* :func:`style` and the label helpers (:func:`severity_label`, :func:`class_label`,
  :func:`status_label`, :func:`flag`) colour severities, classifications and statuses with
  :func:`click.style`. Colour only supplements the text — every label reads the same without it.
  Styles are dropped when ``NO_COLOR`` is set (https://no-color.org), when ``TERM=dumb``, and on
  a Windows console that cannot show ANSI colours; :func:`click.echo` also strips them whenever
  the output is not a terminal (piped or redirected to a file).
* :func:`table` / :func:`table_lines` print aligned text tables, shortening one flexible column
  to fit the width; :func:`field`, :func:`paragraph`, :func:`say` and :func:`heading` print
  label/value lines, wrapped text, wrapped messages and section titles.
* :func:`echo` is :func:`click.echo` that never fails on a console code page lacking a
  character (e.g. Windows output redirected to a cp1252 file): such characters become ``?``.

Command plumbing shared by both groups (no business logic — that lives in the services):

* :func:`command_group` creates a command group whose commands hide the services' routine INFO
  log lines (the command prints its own summary; ``LOG_LEVEL`` in the environment or ``.env``
  still wins).
* :func:`acting_user` resolves ``--as USERNAME`` and enforces the role matrix of SPEC §8.1; a
  refused attempt is audited as ``access_denied`` (like a 403 in the web app).
* :func:`find_patient` looks a patient up by MRN.
* :func:`saving` commits a change on success; on error it rolls back and exits with status 1.

Owned by the CLI work package.
"""

from __future__ import annotations

import functools
import logging
import os
import sys
import textwrap
from collections.abc import Iterable, Iterator, Sequence
from contextlib import contextmanager
from datetime import date, datetime
from typing import Any, NoReturn

import click
from flask.cli import AppGroup
from sqlalchemy import func, select

from .. import constants as C
from ..extensions import db
from ..models import Patient, ReviewNotice, User, VariantKey
from ..security import audit, normalize_username
from ..templating import format_date, format_datetime

logger = logging.getLogger(__name__)

#: Output width the commands keep to.
WIDTH = 100
#: Gap between table columns.
GAP = "  "
#: Shown for a missing value (ASCII, so it survives any console code page).
EMPTY = "-"
#: Marks shortened table cells.
ELLIPSIS = "..."
#: Narrowest a flexible table column is shortened to.
MIN_FLEX_WIDTH = 12
#: Footer of every view that shows recommendations (SPEC §6.1).
ADVISORY = C.ADVISORY_DISCLAIMER

SEVERITY_STYLES: dict[str, dict[str, Any]] = {
    "high": {"fg": "red", "bold": True},
    "medium": {"fg": "yellow", "bold": True},
    "low": {"fg": "bright_blue"},
    "info": {"fg": "bright_black"},
}
CLASS_STYLES: dict[str, dict[str, Any]] = {
    "P": {"fg": "red", "bold": True},
    "LP": {"fg": "bright_red"},
    "VUS": {"fg": "yellow"},
    "LB": {"fg": "cyan"},
    "B": {"fg": "green"},
    "NC": {"fg": "bright_black"},
}
STATUS_STYLES: dict[str, dict[str, Any]] = {
    "active": {"fg": "yellow"},
    "reviewed": {"fg": "green"},
    "superseded": {"fg": "bright_black"},
    "open": {"fg": "yellow", "bold": True},
    "acknowledged": {"fg": "green"},
    "accepted": {"fg": "green"},
    "rejected": {"fg": "red"},
}
FLAG_STYLES: dict[str, dict[str, Any]] = {
    "Reclassified": {"fg": "magenta", "bold": True},
    "Amended": {"fg": "magenta"},
    "Expert panel": {"fg": "cyan"},
    "High priority": {"fg": "red"},
}


# ---------------------------------------------------------------------------
# Colour
# ---------------------------------------------------------------------------


@functools.cache
def _windows_console_supports_ansi() -> bool:
    """Enable ANSI escape processing on the Windows console (Windows 10+); False if impossible.

    False as well when standard output is not a console — click strips styles then anyway.
    """
    try:
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
        handle = kernel32.GetStdHandle(-11)  # STD_OUTPUT_HANDLE
        mode = wintypes.DWORD()
        if not kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
            return False
        enable_virtual_terminal_processing = 0x0004
        if mode.value & enable_virtual_terminal_processing:
            return True
        return bool(kernel32.SetConsoleMode(handle, mode.value | enable_virtual_terminal_processing))
    except Exception:  # noqa: BLE001 - any failure simply means "no colour"
        return False


def color_enabled() -> bool:
    """False when ``NO_COLOR`` is set, ``TERM`` is ``dumb`` or a Windows console lacks ANSI support."""
    if os.environ.get("NO_COLOR"):
        return False
    if os.environ.get("TERM") == "dumb":
        return False
    if sys.platform == "win32":
        return _windows_console_supports_ansi()
    return True


def style(text: str, **styles: Any) -> str:
    """``click.style(text, **styles)``, or ``text`` unchanged when colour is disabled."""
    if not styles or not color_enabled():
        return text
    return click.style(text, **styles)


def visible_length(text: str) -> int:
    """Length of ``text`` as displayed (ANSI style codes excluded)."""
    return len(click.unstyle(text))


def severity_label(severity: str | None) -> str:
    """``HIGH`` / ``MEDIUM`` / ``LOW`` / ``INFO`` coloured; ``note`` for a note (no severity)."""
    if not severity:
        return "note"
    return style(severity.upper(), **SEVERITY_STYLES.get(severity, {}))


def class_label(cls: str | None, *, long: bool = False) -> str:
    """``LP`` coloured, or ``Likely pathogenic (LP)`` with ``long=True``."""
    if not cls:
        return EMPTY
    text = f"{C.CLASS_LABELS.get(cls, cls)} ({cls})" if long else cls
    return style(text, **CLASS_STYLES.get(cls, {}))


def status_label(status: str | None) -> str:
    """An alert, notice or upload status, coloured (``superseded`` dimmed)."""
    if not status:
        return EMPTY
    return style(status, **STATUS_STYLES.get(status, {}))


def flag(text: str) -> str:
    """A flag such as ``Reclassified`` or ``Amended`` (colour from :data:`FLAG_STYLES`)."""
    return style(text, **FLAG_STYLES.get(text, {}))


def flags(names: Iterable[str]) -> str:
    """Several flags separated by commas (empty string when none)."""
    return ", ".join(flag(name) for name in names)


def heading(text: str) -> None:
    """A section title preceded by a blank line."""
    echo("")
    echo(style(text, bold=True))


# ---------------------------------------------------------------------------
# Text output
# ---------------------------------------------------------------------------


def echo(message: str = "", *, err: bool = False) -> None:
    """:func:`click.echo`, replacing characters the output encoding cannot represent."""
    try:
        click.echo(message, err=err)
    except UnicodeEncodeError:
        stream = sys.stderr if err else sys.stdout
        encoding = getattr(stream, "encoding", None) or "ascii"
        click.echo(message.encode(encoding, errors="replace").decode(encoding), err=err)


def text(value: Any) -> str:
    """A cell value as text: None → ``-``, dates as ``YYYY-MM-DD``, datetimes in UTC."""
    if value is None or value == "":
        return EMPTY
    if isinstance(value, datetime):
        return when(value)
    if isinstance(value, date):
        return format_date(value)
    return str(value)


def when(moment: datetime | None) -> str:
    """``2026-01-15 09:30 UTC`` (SPEC §8.3); ``-`` for None."""
    return EMPTY if moment is None else format_datetime(moment)


def day(value: date | datetime | None) -> str:
    """``2026-01-15``; ``-`` for None."""
    return EMPTY if value is None else format_date(value)


def plural(count: int, singular: str, plural_form: str | None = None) -> str:
    """``1 alert`` / ``2 alerts``."""
    return f"{count} {singular if count == 1 else (plural_form or singular + 's')}"


def shorten(value: str, width: int) -> str:
    """``value`` cut to ``width`` visible characters with ``...`` (styles are dropped when cut)."""
    if visible_length(value) <= width:
        return value
    plain = click.unstyle(value)
    if width <= len(ELLIPSIS):
        return plain[:width]
    return plain[: width - len(ELLIPSIS)].rstrip() + ELLIPSIS


def _pad(value: str, width: int) -> str:
    return value + " " * max(width - visible_length(value), 0)


def table_lines(
    headers: Sequence[str] | None,
    rows: Iterable[Sequence[Any]],
    *,
    flex: int | None = None,
    indent: int = 2,
    width: int = WIDTH,
) -> list[str]:
    """The lines of an aligned table (a bold header line first unless ``headers`` is None).

    Column ``flex`` is shortened (with ``...``) so that lines fit ``width``. Cells may contain
    styled text; alignment uses the visible length. None shows as ``-``.
    """
    cells = [[text(value) for value in row] for row in rows]
    columns = len(headers) if headers is not None else max((len(row) for row in cells), default=0)
    widths = [len(header) for header in headers] if headers is not None else [0] * columns
    for row in cells:
        widths = [max(current, visible_length(cell)) for current, cell in zip(widths, row, strict=True)]
    total = indent + sum(widths) + len(GAP) * (len(widths) - 1)
    if flex is not None and total > width:
        minimum = max(MIN_FLEX_WIDTH, len(headers[flex]) if headers is not None else 0)
        widths[flex] = min(widths[flex], max(minimum, widths[flex] - (total - width)))
    margin = " " * indent

    def line(values: Sequence[str]) -> str:
        shortened = [shorten(value, column) for value, column in zip(values, widths, strict=True)]
        padded = [_pad(value, column) for value, column in zip(shortened[:-1], widths[:-1], strict=True)]
        return (margin + GAP.join([*padded, shortened[-1]])).rstrip()

    lines = [line([style(header, bold=True) for header in headers])] if headers is not None else []
    lines.extend(line(row) for row in cells)
    return lines


def table(
    headers: Sequence[str],
    rows: Iterable[Sequence[Any]],
    *,
    flex: int | None = None,
    indent: int = 2,
    width: int = WIDTH,
) -> None:
    """Print an aligned table with a bold header line (see :func:`table_lines`)."""
    for line in table_lines(headers, rows, flex=flex, indent=indent, width=width):
        echo(line)


def paragraph(value: str, *, indent: int = 2, width: int = WIDTH) -> None:
    """Print ``value`` wrapped to ``width`` columns (blank lines in it are kept)."""
    margin = " " * indent
    for block in value.splitlines() or [""]:
        if not block.strip():
            echo("")
            continue
        echo(textwrap.fill(block.strip(), width=width, initial_indent=margin, subsequent_indent=margin))


def say(message: str, *, width: int = WIDTH) -> None:
    """Print a one-paragraph message wrapped to ``width`` (continuation lines indented by two)."""
    echo(
        textwrap.fill(
            message, width=width, subsequent_indent="  ", break_on_hyphens=False, break_long_words=False
        )
    )


def field(label: str, value: Any, *, label_width: int = 16, indent: int = 2, width: int = WIDTH) -> None:
    """Print ``  Label:   value`` with the value wrapped under itself when it is long."""
    prefix = " " * indent + f"{label}:".ljust(label_width + 1) + " "
    value_text = text(value)
    if visible_length(prefix + value_text) <= width or value_text != click.unstyle(value_text):
        echo(prefix + value_text)
        return
    echo(
        textwrap.fill(
            value_text,
            width=width,
            initial_indent=prefix,
            subsequent_indent=" " * len(prefix),
            break_on_hyphens=False,
        )
    )


# ---------------------------------------------------------------------------
# Domain fragments shared by both groups
# ---------------------------------------------------------------------------


def variant_text(variant_key: VariantKey | None, gene: str | None = None) -> str:
    """``HNF1A 12-120978900-G-A`` (gene from ``gene``, else the key's own gene)."""
    if variant_key is None:
        return EMPTY
    symbol = gene or variant_key.gene
    return f"{symbol} {variant_key.label}" if symbol else variant_key.label


def patient_text(patient: Patient) -> str:
    """``DEMO-0001 Maya Okonkwo``."""
    return f"{patient.mrn} {patient.display_name}"


def advisory_footer() -> None:
    """The disclaimer every recommendation view ends with."""
    echo("")
    echo(style(ADVISORY, bold=True))


def class_change(old: str | None, new: str | None) -> str:
    """``LP -> VUS`` with both classes coloured."""
    return f"{class_label(old)} -> {class_label(new)}"


def notice_block(notice: ReviewNotice, *, indent: int = 2, with_patient: bool = True) -> None:
    """One review notice: header line, message, superseded alerts and the next step."""
    margin = " " * indent
    release = notice.run.new_release.label if notice.run is not None and notice.run.new_release else None
    parts = [
        f"#{notice.id}",
        status_label(notice.status),
        when(notice.created_at),
        *([patient_text(notice.patient)] if with_patient else []),
    ]
    echo(margin + "  ".join(parts))
    detail = f"{variant_text(notice.variant_key)}: {class_change(notice.old_classification, notice.new_classification)}"
    if release:
        detail += f" in {release}"
    if notice.run_id is not None:
        detail += f" (run #{notice.run_id})"
    echo(margin + "    " + detail)
    paragraph(notice.message, indent=indent + 4)
    if notice.alerts:
        superseded = ", ".join(f"#{alert.id} {alert.rule_code} {severity_label(alert.severity)}" for alert in notice.alerts)
        echo(margin + f"    Superseded: {superseded}")
    if notice.status == "open":
        echo(margin + f"    Acknowledge: flask alerts acknowledge {notice.id} --as USERNAME [--comment TEXT]")
    else:
        who = notice.acknowledged_by.username if notice.acknowledged_by is not None else "unknown user"
        line = f"    Acknowledged {when(notice.acknowledged_at)} by {who}"
        echo(margin + line + (f": {notice.acknowledgement_comment}" if notice.acknowledgement_comment else "."))


# ---------------------------------------------------------------------------
# Command plumbing
# ---------------------------------------------------------------------------

AS_HELP = "Account the action is recorded under in the audit log (must exist and be active)."
#: Package whose routine INFO log lines the terminal commands hide.
PACKAGE_LOGGER = "ge_ehr"


def quiet_service_logs(ctx: click.Context) -> None:
    """Raise the ``ge_ehr`` log level to WARNING until ``ctx`` closes.

    The services log every upload, evaluation and review at INFO level for the server log; in
    the terminal those timestamped lines would interleave with the command's own output. An
    explicit ``LOG_LEVEL`` (environment or ``.env``) is respected, so ``LOG_LEVEL=INFO flask
    patients ...`` shows them again. Warnings and errors are always shown.
    """
    if os.environ.get("LOG_LEVEL"):
        return
    package_logger = logging.getLogger(PACKAGE_LOGGER)
    previous = package_logger.level
    if package_logger.getEffectiveLevel() >= logging.WARNING:
        return
    package_logger.setLevel(logging.WARNING)
    ctx.call_on_close(lambda: package_logger.setLevel(previous))


def command_group(name: str, help_text: str) -> AppGroup:
    """A Flask command group (commands run in the app context) that hides routine INFO logs."""

    @click.pass_context
    def callback(ctx: click.Context) -> None:
        quiet_service_logs(ctx)

    return AppGroup(name, help=help_text, callback=callback)


def _roles_text(roles: Sequence[str]) -> str:
    labels = [C.ROLE_LABELS[role].lower() for role in roles]
    joined = " or ".join(labels)
    article = "an" if joined[:1] in "aeiou" else "a"
    return f"{article} {joined}"


def deny(user: User, *, roles: Sequence[str], action: str) -> NoReturn:
    """Audit ``access_denied`` (committed) and exit with status 1 explaining which role is needed."""
    ctx = click.get_current_context(silent=True)
    # "flask patients list" whatever the program name (``python -m flask``, the test runner, ...).
    command = " ".join(["flask", *ctx.command_path.split()[1:]]) if ctx is not None else None
    db.session.rollback()
    audit(
        "access_denied",
        "user",
        user.id,
        detail={
            "command": command,
            "required_roles": list(roles),
            "role": user.role,
            "via": "cli",
        },
        user=user,
    )
    db.session.commit()
    logger.info("Access denied: user %s (%s) may not %s", user.username, user.role, action)
    raise click.ClickException(
        f"{user.username} ({C.ROLE_LABELS[user.role]}) may not {action}; that needs "
        f"{_roles_text(roles)} account. The attempt has been recorded in the audit log."
    )


def acting_user(username: str | None, *, roles: Sequence[str] = (), action: str = "do this") -> User | None:
    """The active account named by ``--as`` (None when the option was not given).

    Raises :class:`click.ClickException` for an unknown or deactivated account, and — after
    auditing ``access_denied`` — when the account's role is not in ``roles`` (any role when empty).
    The terminal has direct access to the database, so no password is asked: ``--as`` names the
    person the action is recorded under, and the role matrix of SPEC §8.1 applies to that person.
    """
    if username is None:
        return None
    name = normalize_username(username)
    user = db.session.scalar(select(User).where(User.username == name))
    if user is None:
        raise click.ClickException(f"No user account named {name!r}. List the accounts with: flask list-users")
    if not user.active:
        raise click.ClickException(
            f"The account {name!r} is deactivated; an administrator can reactivate it under Users."
        )
    if roles and not user.has_role(*roles):
        deny(user, roles=roles, action=action)
    return user


def may_see_recommendations(user: User | None) -> bool:
    """True unless ``user`` is an account whose role may not see alerts (lab technicians)."""
    return user is None or user.has_role(*C.CLINICAL_ROLES)


def find_patient(mrn: str) -> Patient:
    """The patient with this MRN (exact match, else a unique case-insensitive match)."""
    value = mrn.strip()
    patient = db.session.scalar(select(Patient).where(Patient.mrn == value))
    if patient is None and value:
        matches = db.session.scalars(select(Patient).where(func.lower(Patient.mrn) == value.lower())).all()
        if len(matches) == 1:
            patient = matches[0]
    if patient is None:
        raise click.ClickException(
            f"No patient with MRN {value!r}. Find patients with: flask patients list --search TEXT"
        )
    return patient


def _sentence(message: str) -> str:
    message = message.strip()
    return message if message.endswith((".", "!", "?")) else message + "."


@contextmanager
def saving(failure: str) -> Iterator[None]:
    """Commit the block's changes; on any error roll back and exit with status 1.

    ``failure`` starts the error message, e.g. ``"Could not add the medication"``. A
    :class:`ValueError` from a service carries a user-facing message; anything else is logged
    with its traceback and reported by type.
    """
    try:
        yield
        db.session.commit()
    except (click.ClickException, click.Abort, click.exceptions.Exit):
        db.session.rollback()
        raise
    except ValueError as exc:
        db.session.rollback()
        raise click.ClickException(f"{failure}: {_sentence(str(exc))} Nothing was changed.") from None
    except Exception as exc:
        db.session.rollback()
        logger.exception("%s", failure)
        detail = f"{type(exc).__name__}: {exc}" if str(exc) else type(exc).__name__
        raise click.ClickException(f"{failure}: unexpected error ({detail}). Nothing was changed.") from None
