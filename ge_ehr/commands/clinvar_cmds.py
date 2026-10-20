"""``flask clinvar ...`` — import, activate, list and sync knowledge-base releases (SPEC §5.3).

* ``flask clinvar import PATH [--label] [--date] [--expert-panel CSV] [--activate/--no-activate] [--strategy]``
* ``flask clinvar activate RELEASE_ID [--strategy]``
* ``flask clinvar list``
* ``flask clinvar sync [--url URL] [--strategy] [--output-dir DIR]`` — the scheduled job: download
  the latest ClinVar GRCh38 VCF, skip it unless its fileDate is newer than the current release's,
  write the 7-gene subset, import it (unless a failed earlier run already did) and activate it.

Each command commits its own work. On an error the session is rolled back and a
:class:`click.ClickException` with a clear message ends the command (exit status 1). An import
is committed before activation starts, so a failed re-evaluation leaves the imported release
in place (non-current) for a later ``flask clinvar activate``.

Owned by the KB work package.
"""

from __future__ import annotations

import logging
from datetime import date, datetime
from pathlib import Path

import click
from flask import Flask, current_app
from flask.cli import AppGroup
from sqlalchemy import func, select

from .. import constants as C
from ..cli import echo_table
from ..extensions import db
from ..models import KbRelease, KnownVariant, ReevaluationRun
from ..services import clinvar

logger = logging.getLogger(__name__)

ACTIVATION_STRATEGIES: tuple[str, ...] = ("change_driven", "full")

clinvar_cli = AppGroup("clinvar", help="Import, activate, list and sync ClinVar knowledge-base releases.")

strategy_option = click.option(
    "--strategy",
    type=click.Choice(ACTIVATION_STRATEGIES),
    default="change_driven",
    show_default=True,
    help="Re-evaluation strategy used when the release is activated.",
)


def default_subset_dir() -> Path:
    """``data/clinvar/real`` in the repository (git-ignored), where real ClinVar subsets go."""
    return Path(current_app.root_path).parent / "data" / "clinvar" / "real"


@clinvar_cli.command("import")
@click.argument("path", type=click.Path(exists=True, dir_okay=False, path_type=Path))
@click.option("--label", default=None, help='Release label (default: "ClinVar <fileDate>").')
@click.option(
    "--date",
    "release_date",
    type=click.DateTime(formats=["%Y-%m-%d"]),
    default=None,
    help="Release date, YYYY-MM-DD (default: the file's ##fileDate).",
)
@click.option(
    "--expert-panel",
    "expert_panel_csv",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    default=None,
    help="Expert-panel overlay CSV (clinvar_variation_id,classification,panel,date) applied during import.",
)
@click.option(
    "--activate/--no-activate",
    default=False,
    show_default=True,
    help="Activate the release and re-evaluate patients right after the import.",
)
@strategy_option
def import_command(
    path: Path,
    label: str | None,
    release_date: datetime | None,
    expert_panel_csv: Path | None,
    activate: bool,
    strategy: str,
) -> None:
    """Import a ClinVar(-format) GRCh38 VCF at PATH as a new knowledge-base release."""
    release = _import_release(
        path,
        label=label.strip() if label else None,
        release_date=release_date.date() if release_date else None,
        expert_panel_csv=expert_panel_csv,
    )
    if activate:
        _activate_release(release, strategy)
    else:
        click.echo(f"Not activated. Activate it with: flask clinvar activate {release.id}")


@clinvar_cli.command("activate")
@click.argument("release_id", type=int)
@strategy_option
def activate_command(release_id: int, strategy: str) -> None:
    """Make release RELEASE_ID current and re-evaluate the affected patients."""
    release = db.session.get(KbRelease, release_id)
    if release is None:
        raise click.ClickException(
            f"There is no knowledge-base release with id {release_id}; see 'flask clinvar list'."
        )
    if release.is_current:
        click.echo(f"Release {release.label!r} (id {release.id}) is already the current release; nothing to do.")
        return
    _activate_release(release, strategy)


@clinvar_cli.command("list")
def list_command() -> None:
    """List the imported knowledge-base releases."""
    releases = db.session.execute(select(KbRelease).order_by(KbRelease.id)).scalars().all()
    if not releases:
        click.echo("No knowledge-base releases. Import one with: flask clinvar import PATH")
        return
    echo_table(
        ("ID", "LABEL", "SOURCE", "FILE DATE", "RELEASE DATE", "RECORDS", "CURRENT", "SEQ", "IMPORTED (UTC)"),
        (
            (
                release.id,
                release.label,
                release.source,
                release.file_date,
                release.release_date,
                release.record_count,
                "yes" if release.is_current else "",
                release.sequence,
                release.imported_at.strftime("%Y-%m-%d %H:%M"),
            )
            for release in releases
        ),
    )


@clinvar_cli.command("sync")
@click.option(
    "--url",
    default=None,
    help="ClinVar GRCh38 VCF to fetch: URL or local path (default: the CLINVAR_SYNC_URL setting).",
)
@strategy_option
@click.option(
    "--output-dir",
    type=click.Path(file_okay=False, path_type=Path),
    default=None,
    help="Folder for the 7-gene subset (default: data/clinvar/real).",
)
def sync_command(url: str | None, strategy: str, output_dir: Path | None) -> None:
    """Fetch the latest ClinVar release, import its 7-gene subset and activate it.

    Nothing is imported when the release's fileDate is not newer than the current release's.
    Intended for a scheduled job.
    """
    source = url or current_app.config["CLINVAR_SYNC_URL"]
    current_day = _file_day(KbRelease.current())
    click.echo(f"Fetching {source} ...")
    try:
        result = clinvar.fetch_clinvar_subset(source, output_dir or default_subset_dir(), newer_than=current_day)
    except (clinvar.ClinVarDownloadError, ValueError) as exc:
        raise click.ClickException(str(exc)) from None
    if result is None:
        click.echo(
            f"The ClinVar release at {source} is not newer than the current release (data of {current_day}); "
            "nothing to do."
        )
        return
    click.echo(
        f"Wrote {result.path}: {result.kept_records:,} of {result.total_records:,} ClinVar records are in the "
        "7-gene panel."
    )
    # A previous run may have imported this very subset and then failed to activate it.
    release = clinvar.release_for_file(result.path)
    if release is None:
        release = _import_release(result.path)
    else:
        click.echo(f"{result.path.name} was already imported as release {release.label!r} (id {release.id}).")
        if release.is_current:
            click.echo("It is already the current release; nothing to do.")
            return
    _activate_release(release, strategy)


def _file_day(release: KbRelease | None) -> date | None:
    """The day a release's data was produced (its fileDate, else its release date)."""
    if release is None:
        return None
    return clinvar.parse_file_date(release.file_date) or release.release_date


def _import_release(
    path: Path,
    *,
    label: str | None = None,
    release_date: date | None = None,
    expert_panel_csv: Path | None = None,
) -> KbRelease:
    """Import and commit a release, then print its summary."""
    try:
        release = clinvar.import_release(
            path, label=label, release_date=release_date, expert_panel_csv=expert_panel_csv
        )
        db.session.commit()
    except (ValueError, OSError) as exc:
        db.session.rollback()
        raise click.ClickException(str(exc)) from None
    overrides = db.session.scalar(
        select(func.count(KnownVariant.id)).where(
            KnownVariant.release_id == release.id, KnownVariant.classification_source == "expert_panel_overlay"
        )
    )
    kind = "synthetic ClinVar-format" if release.is_synthetic else "ClinVar"
    click.echo(
        f"Imported {kind} release {release.label!r} (id {release.id}, fileDate {release.file_date or '-'}): "
        f"{release.record_count:,} panel records."
    )
    click.echo("  Classes: " + ", ".join(f"{cls} {release.class_counts.get(cls, 0):,}" for cls in C.CLASSES))
    click.echo("  Genes:   " + ", ".join(f"{gene} {release.gene_counts.get(gene, 0):,}" for gene in C.GENES))
    if overrides:
        click.echo(f"  Expert-panel overlay applied to {overrides:,} variant(s).")
    return release


def _activate_release(release: KbRelease, strategy: str) -> None:
    """Activate a committed release (re-evaluating carriers), commit and print the run summary."""
    # Imported on use: the re-evaluation engine builds on the services of every other package.
    from ..services.reevaluation import activate_release

    label, release_id = release.label, release.id
    try:
        run = activate_release(release, strategy=strategy)
        db.session.commit()
    except Exception as exc:
        db.session.rollback()
        logger.exception("Activation of knowledge-base release %s failed", release_id)
        raise click.ClickException(
            f"Activation of release {label!r} (id {release_id}) failed: {str(exc) or type(exc).__name__}. "
            "The release was not made current."
        ) from None
    _echo_run(release, run)


def _echo_run(release: KbRelease, run: ReevaluationRun) -> None:
    duration = f" in {run.duration_ms:,.0f} ms" if run.duration_ms is not None else ""
    strategy = C.RUN_STRATEGY_LABELS.get(run.strategy, run.strategy)
    click.echo(
        f"Activated release {release.label!r} (id {release.id}, sequence {release.sequence}). "
        f"Re-evaluation run #{run.id} ({strategy}, {run.status}){duration}:"
    )
    click.echo(
        f"  {run.stored_keys_compared:,} stored keys compared, {run.changed_variants:,} changed, "
        f"{run.carriers_affected:,} carriers affected, {run.patients_reevaluated:,} patients re-evaluated, "
        f"{run.records_examined:,} records examined"
    )
    click.echo(
        f"  Alerts: {run.alerts_created:,} created, {run.alerts_superseded:,} superseded, "
        f"{run.alerts_confirmed:,} confirmed; notes {run.notes_created:,}; review notices {run.review_notices:,}; "
        f"notifications {run.notifications:,}"
    )


def register(app: Flask) -> None:
    """Register the ``clinvar`` command group."""
    app.cli.add_command(clinvar_cli)
