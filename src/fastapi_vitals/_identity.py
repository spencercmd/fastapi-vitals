"""Shared service identity defaults for metrics labels and OTEL resources.

Single source for ``SERVICE`` / ``ENV`` / ``APP_VERSION`` fallbacks so RED
series and trace resource attributes cannot drift.

Defaults are intentionally generic. Production deployments should set
``SERVICE`` (and usually ``ENV`` / ``APP_VERSION``) explicitly.
"""

from __future__ import annotations

import os
from typing import Tuple

from fastapi_vitals._process_cache import ProcessCache

# Neutral defaults — not org- or product-specific. Prefer setting SERVICE.
DEFAULT_SERVICE_NAME = "app"
DEFAULT_ENV = "prod"
DEFAULT_VERSION = "unknown"


def _load_identity_labels() -> Tuple[str, str, str]:
    return (
        os.getenv("SERVICE", DEFAULT_SERVICE_NAME),
        os.getenv("ENV", DEFAULT_ENV),
        os.getenv("APP_VERSION", DEFAULT_VERSION),
    )


_identity = ProcessCache(_load_identity_labels)


def identity_labels() -> Tuple[str, str, str]:
    """Return ``(service, env, version)`` for Prometheus identity labels.

    Reads ``SERVICE``, ``ENV``, and ``APP_VERSION`` (defaults: ``app``,
    ``prod``, ``unknown``) on first call and freezes the result for the
    process. Use with star-unpack for custom metrics so series stay aligned
    with RED / dependency series::

        MY_COUNTER.labels(*identity_labels(), "dim").inc()

    Call ``reset_identity_labels()`` after changing env in tests or rare
    reconfiguration.
    """
    return _identity.get()


def reset_identity_labels() -> None:
    """Drop cached identity labels (tests / rare reconfig)."""
    _identity.reset()
