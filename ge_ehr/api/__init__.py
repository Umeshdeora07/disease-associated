"""API blueprint: FHIR export and health check (JSON).

The blueprint object lives in :mod:`ge_ehr.api.routes` (owned by the FHIR work package);
this package module was created by the Foundation work package.
"""

from .routes import bp

__all__ = ["bp"]
