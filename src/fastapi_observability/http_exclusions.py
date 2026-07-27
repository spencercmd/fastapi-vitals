"""HTTP paths excluded from RED metrics and OTEL FastAPI instrumentation.

Single source of truth so metrics ``EXCLUDED_PATHS`` and tracing
``EXCLUDED_URLS`` cannot drift (e.g. health live/ready probes).
"""

from __future__ import annotations

import re
from typing import Sequence, Tuple

__all__ = [
    "EXCLUDED_HTTP_PATHS",
    "excluded_urls_regex",
]

# Exact request paths skipped by metrics middleware and by FastAPI OTEL
# instrumentation (via ``excluded_urls_regex``). Keep in sync with consumer
# access-log filters for probe noise.
EXCLUDED_HTTP_PATHS: Tuple[str, ...] = (
    "/health",
    "/health/live",
    "/health/ready",
    "/metrics",
    "/docs",
    "/docs/oauth2-redirect",
    "/redoc",
    "/openapi.json",
)


def excluded_urls_regex(paths: Sequence[str] = EXCLUDED_HTTP_PATHS) -> str:
    """Build an ``excluded_urls`` regex for FastAPIInstrumentor.

    Matches any URL whose path ends with one of ``paths`` (same semantics as
    the historical single-regex constant).
    """
    if not paths:
        raise ValueError("paths must not be empty")
    alternation = "|".join(re.escape(p) for p in paths)
    return rf".*(?:{alternation})$"
