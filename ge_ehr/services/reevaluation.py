"""F5 — change-driven and full re-evaluation, rule revision and point-in-time view (SPEC §7).

Owned by the Reeval work package. Interface stub created by the Foundation work package; the
function bodies are implemented by the Reeval package.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from ..models import Alert, KbRelease, Patient, ReevaluationRun, User

logger = logging.getLogger(__name__)


def activate_release(
    new_release: KbRelease, *, strategy: str = "change_driven", user: User | None = None
) -> ReevaluationRun:
    """Make ``new_release`` current and re-evaluate affected carriers (Algorithm 2)."""
    raise NotImplementedError


def apply_rule_set(doc: dict[str, Any], *, user: User | None = None, strategy: str = "change_driven") -> ReevaluationRun:
    """Import rule versions and re-evaluate the carriers of every revised rule."""
    raise NotImplementedError


def full_recompute(*, user: User | None = None, trigger: str = "full_recompute") -> ReevaluationRun:
    """Reconcile every patient against the current release and rules."""
    raise NotImplementedError


def alerts_as_of(patient: Patient, at: datetime) -> list[Alert]:
    """Alerts and notes that were current for ``patient`` at time ``at``."""
    raise NotImplementedError


def release_history() -> list[KbRelease]:
    """Activated releases ordered by activation sequence."""
    raise NotImplementedError
