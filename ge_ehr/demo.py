"""Synthetic demo dataset loader used by ``flask seed-demo``, the scenario tests and the UI (SPEC §11).

Owned by the Scenario work package. Interface stub created by the Foundation work package.
"""

from __future__ import annotations

import logging
from typing import Any

from flask import Flask

logger = logging.getLogger(__name__)


def seed_demo(app_or_none: Flask | None, *, rules: str = "v1", activate_r2: bool = False) -> dict[str, Any]:
    """Create demo users, rules, releases and patients; return a summary of what was created."""
    raise NotImplementedError
