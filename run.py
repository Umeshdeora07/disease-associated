#!/usr/bin/env python3
"""Start GE-EHR on this computer; then open http://127.0.0.1:5000 in a web browser.

    python run.py                    # waitress (production-grade, works on Windows, macOS, Linux)
    python run.py --port 8080        # another port
    python run.py --dev              # Flask development server: auto-reload and debugger
    python run.py --host 0.0.0.0     # also reachable from other computers (not recommended)

Run it from the repository root inside the virtual environment (``python`` is ``.venv\\Scripts\\python``
on Windows and ``.venv/bin/python`` on macOS/Linux). On start it

* creates the instance folder and the database tables when they are missing (SQLite file
  ``instance/ge_ehr.db`` unless ``DATABASE_URL`` names another database);
* uses the development configuration unless ``FLASK_CONFIG`` says otherwise, but with a random
  SECRET_KEY generated once into ``instance/secret_key`` (when ``SECRET_KEY`` is not set), so
  logins survive restarts and no shared key is used;
* binds to 127.0.0.1 only, unless ``--host`` says otherwise, and prints the address to open.

The settings in ``.env`` (copy ``.env.example``) apply; the file is optional. Load demo data with
``flask --app wsgi seed-demo`` (users admin / dr.rao / lab.kim). Stop the server with Ctrl+C.
Synthetic data only; recommendations are advisory.
"""

from __future__ import annotations

import argparse
import ipaddress
import os
import sys
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import TYPE_CHECKING

REPO_ROOT = Path(__file__).resolve().parent
if str(REPO_ROOT) not in sys.path:  # `python path/to/run.py` from another folder
    sys.path.insert(0, str(REPO_ROOT))

if TYPE_CHECKING:
    from flask import Flask

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 5000
DEFAULT_THREADS = 8
LOOPBACK_NAMES = frozenset({"localhost", "localhost.localdomain"})


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    """Command-line options of ``run.py``."""
    parser = argparse.ArgumentParser(
        prog="run.py",
        description="Start the GE-EHR web application locally (synthetic data only; advisory recommendations).",
        epilog="Example: python run.py --port 8080. Demo data: flask --app wsgi seed-demo",
    )
    parser.add_argument(
        "--host",
        default=DEFAULT_HOST,
        help=f"address to listen on (default {DEFAULT_HOST}: this computer only; 0.0.0.0 = every network interface)",
    )
    parser.add_argument("--port", type=_port, default=DEFAULT_PORT, help=f"TCP port (default {DEFAULT_PORT})")
    parser.add_argument(
        "--dev",
        action="store_true",
        help="use the Flask development server (auto-reload and interactive debugger; this computer only)",
    )
    parser.add_argument(
        "--threads", type=_positive_int, default=DEFAULT_THREADS, help=f"waitress worker threads (default {DEFAULT_THREADS})"
    )
    return parser.parse_args(argv)


def _port(text: str) -> int:
    value = _positive_int(text)
    if value > 65535:
        raise argparse.ArgumentTypeError(f"{text!r} is not a TCP port (1-65535)")
    return value


def _positive_int(text: str) -> int:
    try:
        value = int(text)
    except ValueError:
        value = 0
    if value < 1:
        raise argparse.ArgumentTypeError(f"{text!r} is not a positive whole number")
    return value


def is_loopback(host: str) -> bool:
    """True for addresses only this computer can reach (127.0.0.0/8, ::1, localhost)."""
    if host.lower() in LOOPBACK_NAMES:
        return True
    try:
        return ipaddress.ip_address(host.strip("[]")).is_loopback
    except ValueError:
        return False


def browser_url(host: str, port: int) -> str:
    """The address to open in a browser for a server listening on ``host``."""
    if host in ("0.0.0.0", "::", ""):
        host = DEFAULT_HOST
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    return f"http://{host}:{port}"


def build_app(config: str | dict[str, object] | None = None) -> Flask:
    """Create the app, its instance folder, a persistent SECRET_KEY (if none is set) and the tables."""
    from ge_ehr import create_app
    from ge_ehr.config import DEVELOPMENT_SECRET_KEY, instance_secret_key
    from ge_ehr.extensions import db

    app = create_app(config)
    os.makedirs(app.instance_path, exist_ok=True)
    if app.config["SECRET_KEY"] == DEVELOPMENT_SECRET_KEY:
        app.config["SECRET_KEY"] = instance_secret_key(app.instance_path)
    with app.app_context():
        db.create_all()
    return app


def describe(app: Flask, host: str, port: int, *, dev: bool) -> list[str]:
    """The start-up banner: address, database, VCF reader and (on an empty database) a demo hint."""
    from sqlalchemy import func, select

    from ge_ehr.extensions import db
    from ge_ehr.models import User
    from ge_ehr.services.vcf_backend import describe_vcf_backend

    with app.app_context():
        database = db.engine.url.render_as_string(hide_password=True)
        users = db.session.scalar(select(func.count(User.id))) or 0
    lines = [
        "GE-EHR - Monogenic Diabetes (research prototype: synthetic data only, advisory recommendations)",
        f"  Open {browser_url(host, port)} in your browser. Press Ctrl+C to stop.",
        f"  Server:     {'Flask development server (auto-reload, debugger)' if dev else 'waitress'}"
        f", configuration '{app.config['CONFIG_NAME']}'",
        f"  Database:   {database}",
        f"  VCF reader: {describe_vcf_backend()}",
    ]
    if not users:
        password = app.config.get("DEMO_PASSWORD", "Demo-Pass-2026!")
        lines += [
            "  The database has no users yet. Load the demo data (in another terminal, same folder):",
            "      flask --app wsgi seed-demo",
            f"  then sign in as admin, dr.rao or lab.kim with the password {password}",
            "  (or create your own account: flask --app wsgi create-user --help).",
        ]
    if not is_loopback(host):
        lines.append(
            f"  WARNING: listening on {host}, so other computers on your network can reach GE-EHR. "
            "Use the default 127.0.0.1 unless you need that."
        )
    return lines


def serve_with_waitress(app: Flask, host: str, port: int, threads: int) -> None:
    """Serve ``app`` with waitress until Ctrl+C."""
    from waitress import serve

    serve(app, host=host, port=port, threads=threads, ident="GE-EHR")


def serve_with_flask(app: Flask, host: str, port: int, threads: int) -> None:
    """Serve ``app`` with the Flask development server (auto-reload and debugger) until Ctrl+C."""
    app.run(host=host, port=port, debug=True, use_reloader=True)


def main(
    argv: Sequence[str] | None = None,
    *,
    serve: Callable[[Flask, str, int, int], None] | None = None,
) -> int:
    """Entry point; ``serve`` replaces the server (tests). Returns the process exit code."""
    args = parse_args(argv)
    if args.dev and not is_loopback(args.host):
        print(
            "run.py: --dev serves the interactive debugger, which can run code on this computer; it is "
            f"only allowed on 127.0.0.1 (got --host {args.host}).",
            file=sys.stderr,
        )
        return 2
    try:
        app = build_app()
    except ModuleNotFoundError as exc:
        if exc.name and exc.name.startswith("psycopg"):
            print(
                "run.py: DATABASE_URL names a PostgreSQL database, but its driver is not installed. "
                "Install it with: python -m pip install psycopg2-binary",
                file=sys.stderr,
            )
            return 1
        raise
    except (RuntimeError, ValueError) as exc:  # invalid settings, e.g. SECRET_KEY or GE_EHR_VCF_BACKEND
        print(f"run.py: {exc}", file=sys.stderr)
        return 1
    reloader_child = os.environ.get("WERKZEUG_RUN_MAIN") == "true"  # --dev: say everything only once
    if not reloader_child:
        print("\n".join(describe(app, args.host, args.port, dev=args.dev)), flush=True)
    server = serve or (serve_with_flask if args.dev else serve_with_waitress)
    try:
        server(app, args.host, args.port, args.threads)
    except KeyboardInterrupt:  # the Flask development server; waitress returns on Ctrl+C
        pass
    except OSError as exc:  # most often: the port is taken (EADDRINUSE / WinError 10048)
        print(
            f"run.py: cannot listen on {args.host}:{args.port} ({exc}). Is GE-EHR already running? "
            f"Choose another port, e.g. python run.py --port {args.port + 1}",
            file=sys.stderr,
        )
        return 1
    if not reloader_child:
        print("GE-EHR stopped.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
