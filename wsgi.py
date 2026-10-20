"""WSGI entry point: ``gunicorn wsgi:app`` and ``flask --app wsgi ...``.

The configuration is chosen by the ``FLASK_CONFIG`` environment variable (default
``development``); a ``.env`` file in the project root is loaded when present.
"""

from ge_ehr import create_app

app = create_app()
