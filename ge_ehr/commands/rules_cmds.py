"""``flask rules ...`` — validate, import, bootstrap and list decision-support rule sets (SPEC §6).

Which command to use:

* ``flask rules validate PATH`` — check a rule-set JSON file; the database is not touched.
* ``flask rules bootstrap PATH`` — load the initial rules into a **fresh** database (before any
  patient variants are stored, e.g. right after ``flask init-db``). It stores the rule versions
  and makes the highest version per code current, but does **not** re-evaluate patients. It
  refuses to change a current rule version once patient variants exist, because their alerts
  would no longer match the rules — use ``import`` then.
* ``flask rules import PATH [--strategy change_driven|full]`` — apply a rule set to a live system
  (e.g. D4 v1 → v2): stores the new versions and re-evaluates the carriers of every revised rule
  (or every patient with ``--strategy full``) through
  :func:`ge_ehr.services.reevaluation.apply_rule_set`, logging a re-evaluation run.
* ``flask rules list [--current]`` — show the stored rule versions.

Owned by the Rules work package.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import click
from flask import Flask
from flask.cli import AppGroup
from sqlalchemy import func, select

from ..cli import echo_table
from ..extensions import db
from ..models import PatientVariant, ReevaluationRun, RuleVersion
from ..services.alerting import import_rule_versions
from ..services.rules import (
    RuleDefinitionError,
    RuleSpec,
    describe_clinical_condition,
    describe_genetic_criterion,
    load_rule_set_file,
    rule_code_sort_key,
)

rules_cli = AppGroup("rules", help="Validate, import, bootstrap and list versioned decision-support rules.")

_PATH = click.Path(exists=True, dir_okay=False)
_REEVALUATION_STRATEGIES = ("change_driven", "full")


def _read_rule_set(path: str) -> dict[str, Any]:
    """Load and validate a rule-set file, turning problems into a CLI error."""
    try:
        return load_rule_set_file(path)
    except (OSError, RuleDefinitionError) as exc:
        raise click.ClickException(f"{path}: {exc}") from None


def _rule_set_name(doc: Mapping[str, Any]) -> str:
    label = doc.get("label")
    return f"rule set {label!r}" if label else "rule set (no label)"


def _version_list(versions: list[tuple[str, int]]) -> str:
    return ", ".join(f"{code} v{number}" for code, number in versions)


def _change_text(change: Mapping[str, Any]) -> str:
    """``"D4 v1 → v2"`` or ``"D7 (new) v1"``."""
    old = change.get("old_version")
    if old is None:
        return f"{change['code']} (new) v{change['new_version']}"
    return f"{change['code']} v{old} → v{change['new_version']}"


def _echo_run(run: ReevaluationRun) -> None:
    changes = run.rule_change or []
    if changes:
        click.echo(f"Revised rules: {', '.join(_change_text(change) for change in changes)}.")
    else:
        click.echo("No current rule version changed.")
    duration = f"; {run.duration_ms:.1f} ms" if run.duration_ms is not None else ""
    click.echo(
        f"Re-evaluation run #{run.id} ({run.trigger}, {run.strategy}, {run.status}): "
        f"{run.patients_reevaluated} patient(s) re-evaluated, {run.records_examined} record(s) examined; "
        f"{run.alerts_created} alert(s) and {run.notes_created} note(s) created, "
        f"{run.alerts_superseded} superseded, {run.alerts_confirmed} confirmed; "
        f"{run.review_notices} review notice(s), {run.notifications} notification(s){duration}."
    )


@rules_cli.command("validate")
@click.argument("path", type=_PATH)
def validate_command(path: str) -> None:
    """Check a rule-set JSON file (the database is not changed)."""
    doc = _read_rule_set(path)
    versions = [(rule["code"], rule["version"]) for rule in doc["rules"]]
    click.echo(
        f"{path}: valid {_rule_set_name(doc)} with {len(versions)} rule definition(s): {_version_list(versions)}."
    )


@rules_cli.command("import")
@click.argument("path", type=_PATH)
@click.option(
    "--strategy",
    type=click.Choice(_REEVALUATION_STRATEGIES),
    default="change_driven",
    show_default=True,
    help="change_driven re-evaluates only carriers of the revised rules' genes; full re-evaluates every patient.",
)
def import_command(path: str, strategy: str) -> None:
    """Import a rule set and re-evaluate the carriers of every revised rule."""
    # Imported when the command runs, so registering the CLI does not load the re-evaluation engine.
    from ..services import reevaluation

    doc = _read_rule_set(path)
    try:
        run = reevaluation.apply_rule_set(doc, strategy=strategy)
    except RuleDefinitionError as exc:
        db.session.rollback()
        raise click.ClickException(f"{path}: {exc}") from None
    db.session.commit()
    click.echo(f"Imported {_rule_set_name(doc)} from {path}.")
    _echo_run(run)


@rules_cli.command("bootstrap")
@click.argument("path", type=_PATH)
def bootstrap_command(path: str) -> None:
    """Load rule versions into a fresh database without re-evaluating patients.

    Use this once, before patient variants are stored; on a live system use 'flask rules import'.
    """
    doc = _read_rule_set(path)
    stored_before = db.session.scalar(select(func.count(RuleVersion.id)))
    try:
        changes = import_rule_versions(doc)
    except RuleDefinitionError as exc:
        db.session.rollback()
        raise click.ClickException(f"{path}: {exc}") from None
    variant_count = db.session.scalar(select(func.count(PatientVariant.id))) if changes else 0
    if variant_count:
        db.session.rollback()
        raise click.ClickException(
            f"Refusing to bootstrap: {_rule_set_name(doc)} would change the current rules "
            f"({', '.join(_change_text(change) for change in changes)}) while {variant_count} patient variant(s) "
            f"are stored, so their alerts would no longer match. Use 'flask rules import {path}' to import "
            "and re-evaluate the affected patients."
        )
    db.session.commit()

    created = db.session.scalar(select(func.count(RuleVersion.id))) - stored_before
    if not created and not changes:
        click.echo(f"The {_rule_set_name(doc)} is already imported; nothing changed.")
        return
    current = [
        (version.code, version.version)
        for version in sorted(RuleVersion.current_versions(), key=lambda version: rule_code_sort_key(version.code))
    ]
    click.echo(
        f"Bootstrapped {_rule_set_name(doc)}: {created} new rule version(s) stored, "
        f"{len(changes)} current version change(s). Current rules: {_version_list(current)}."
    )


@rules_cli.command("list")
@click.option("--current", "current_only", is_flag=True, help="Show only the current version of each rule.")
def list_command(current_only: bool) -> None:
    """List the stored rule versions."""
    statement = select(RuleVersion)
    if current_only:
        statement = statement.where(RuleVersion.is_current.is_(True))
    versions = sorted(
        db.session.scalars(statement), key=lambda version: (rule_code_sort_key(version.code), version.version)
    )
    if not versions:
        click.echo(
            "No rule versions stored. Load the initial rules with: flask rules bootstrap data/rules/ruleset_v1.json"
        )
        return
    rows = []
    for version in versions:
        spec = RuleSpec.from_definition(version.definition, rule_version_id=version.id)
        rows.append(
            (
                version.code,
                version.version,
                "yes" if version.is_current else "no",
                version.kind,
                version.severity,
                describe_genetic_criterion(spec),
                describe_clinical_condition(spec.clinical),
                version.rule_set_label,
            )
        )
    echo_table(
        ("CODE", "VERSION", "CURRENT", "KIND", "SEVERITY", "GENETIC CRITERION", "CLINICAL CONDITION", "RULE SET"), rows
    )


def register(app: Flask) -> None:
    """Register the ``rules`` command group."""
    app.cli.add_command(rules_cli)
