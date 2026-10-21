"""``flask seed-demo [--full] [--reset] [--yes] [--password PW]`` — load the synthetic demo dataset (SPEC §11).

Without ``--full`` the database is left at rules v1 + demo release R1 (scenarios S1–S9), and the
command prints the two UI steps that show reclassification-aware re-evaluation live. ``--full``
applies rule set v2 and release R2 as well. ``--reset`` drops and recreates every table first
(after a confirmation prompt unless ``--yes``). The seed is committed only when it succeeds
completely.

Owned by the Scenario work package.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import click
from flask import Flask, current_app
from flask.cli import with_appcontext

from .. import constants as C
from ..cli import echo_table
from ..config import DEFAULT_DEMO_PASSWORD
from ..demo import RELEASE_2_LABEL, DemoSeedError, demo_password, seed_demo
from ..extensions import db

#: The two UI steps of a live demo after a plain ``flask seed-demo`` (what ``--full`` does instead).
LIVE_DEMO_STEPS: tuple[tuple[str, str], ...] = (
    (
        "Knowledge base → Rules → import data/rules/ruleset_v2.json",
        "D4 v2 adds ABCC8: DEMO-0012 gains a high-severity D4 alert (RS5).",
    ),
    (
        "Knowledge base → Releases → import data/demo/clinvar/demo_release_2.vcf.gz with 'activate and re-evaluate'",
        (
            f"Label it '{RELEASE_2_LABEL}'. Reclassifications RS1–RS4 and RS6; V11 is carried by nobody, "
            "so it is never examined (RS7)."
        ),
    ),
)


def _password_text(password_option: str | None) -> str:
    """How to refer to the demo password: the public default is shown, a configured one is not."""
    if password_option is not None:
        return "the password given with --password"
    if current_app.config["DEMO_PASSWORD"] == DEFAULT_DEMO_PASSWORD:
        return DEFAULT_DEMO_PASSWORD
    return "the value of DEMO_PASSWORD"


def _run_text(run: Mapping[str, Any]) -> str:
    """One line describing a re-evaluation run of the seed."""
    head = f"#{run['id']} {C.TRIGGER_LABELS[run['trigger']]} ({run['strategy']}, {run['status']})"
    if run["trigger"] == "rule_revision":
        changes = ", ".join(
            f"{change['code']} v{change['old_version']} → v{change['new_version']}" for change in run["rule_change"]
        )
        what = f"{changes or 'no rule changed'} under {run['release'] or 'no knowledge-base release'}"
    else:
        what = f"{run['release']} activated" + (" — initial classification" if run["initial"] else "")
        what += (
            f"; {run['stored_keys_compared']} stored key(s) compared, {run['changed_variants']} changed, "
            f"{run['carriers_affected']} carrier(s)"
        )
    return (
        f"{head}: {what}; {run['patients_reevaluated']} patient(s) re-evaluated, "
        f"{run['records_examined']} record(s) examined; {run['alerts_created']} alert(s) and "
        f"{run['notes_created']} note(s) created, {run['alerts_superseded']} superseded, "
        f"{run['alerts_confirmed']} confirmed; {run['review_notices']} review notice(s), "
        f"{run['notifications']} notification(s)."
    )


def _echo_summary(summary: Mapping[str, Any], *, full: bool, reset: bool, password_option: str | None) -> None:
    location = db.engine.url.render_as_string(hide_password=True)
    action = "Dropped and recreated all tables, then loaded" if reset else "Loaded"
    click.echo(f"{action} the GE-EHR demo dataset (synthetic data only) into {location}.")
    click.echo("")
    releases = "; ".join(
        f"{release['label']}{' [current]' if release['is_current'] else ''} — {release['record_count']:,} records"
        for release in summary["releases"]
    )
    uploads = summary["uploads"]
    click.echo(f"Rules:     {', '.join(summary['rules'])}")
    click.echo(f"Releases:  {releases}")
    click.echo(
        f"Patients:  {summary['patients']} ({uploads['accepted']} VCF upload(s) accepted, "
        f"{uploads['rejected']} rejected; {summary['patient_variants']} patient variant(s))"
    )
    click.echo(
        f"Alerts:    {', '.join(f'{count} {severity}' for severity, count in summary['alerts'].items())} (current); "
        f"{summary['notes']} note(s); {summary['superseded']} superseded; "
        f"{summary['open_review_notices']} open review notice(s)"
    )
    click.echo("Re-evaluation runs:")
    for run in summary["runs"]:
        click.echo(f"  {_run_text(run)}")
    click.echo("")

    click.echo(f"Demo logins (password: {_password_text(password_option)}):")
    echo_table(
        ("USERNAME", "ROLE", "NAME", "ACCOUNT"),
        (
            (
                user["username"],
                C.ROLE_LABELS[user["role"]],
                user["full_name"],
                "created" if user["created"] else "already existed (password unchanged)",
            )
            for user in summary["users"]
        ),
    )
    click.echo("")

    if full:
        click.echo(
            f"Rule set v2 and release {RELEASE_2_LABEL} are applied (Knowledge base → Re-evaluation runs). "
            "Sign in as dr.rao and open the Alerts inbox (filter 'Reclassified'), the Review notices and "
            "the recommendations of DEMO-0008 (RS1) and DEMO-0010 (RS2)."
        )
        return
    click.echo("Next steps for a live demo (sign in as admin):")
    for number, (step, effect) in enumerate(LIVE_DEMO_STEPS, start=1):
        click.echo(f"  {number}. {step}")
        click.echo(f"     {effect}")
    click.echo("Then sign in as dr.rao and review the Alerts inbox (filter 'Reclassified') and the Review notices.")


@click.command("seed-demo")
@click.option("--full", is_flag=True, help="Also apply rule set v2 and activate demo release R2 (reclassifications).")
@click.option("--reset", is_flag=True, help="Drop and recreate all tables before seeding. DESTROYS ALL DATA.")
@click.option("--yes", is_flag=True, help="Do not ask for confirmation before --reset drops the data.")
@click.option(
    "--password",
    default=None,
    help="Password of the demo accounts created (default: DEMO_PASSWORD from the environment, else Demo-Pass-2026!).",
)
@with_appcontext
def seed_demo_command(full: bool, reset: bool, yes: bool, password: str | None) -> None:
    """Seed demo users, rules, releases and the 15 synthetic demo patients into an empty database."""
    try:
        demo_password(password)
    except DemoSeedError as exc:
        raise click.BadParameter(str(exc), param_hint="'--password'") from None
    if reset and not yes:
        location = db.engine.url.render_as_string(hide_password=True)
        click.confirm(f"Drop ALL tables and data in {location} and load the demo dataset?", abort=True)
    try:
        summary = seed_demo(full=full, reset=reset, password=password)
    except DemoSeedError as exc:
        db.session.rollback()
        raise click.ClickException(str(exc)) from None
    db.session.commit()
    _echo_summary(summary, full=full, reset=reset, password_option=password)


def register(app: Flask) -> None:
    """Register the ``seed-demo`` command."""
    app.cli.add_command(seed_demo_command)
