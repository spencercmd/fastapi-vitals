"""Configurable Prometheus metric series names.

Default base names are standard RED-style series (see README). Override via:

1. Environment (before process import)::

       METRICS_NAME_PREFIX=myapp_

2. Explicit configure **before** first instrument access::

       from fastapi_vitals.metrics.names import configure_metric_names
       configure_metric_names(prefix="myapp")
       from fastapi_vitals.metrics import HTTP_REQUESTS  # uses prefix

Once any instrument is constructed, names are locked for the process.
"""

from __future__ import annotations

import os
from typing import Dict, Mapping, Optional

# Canonical base names (no prefix). Public contract for scrapers/alerts.
BASE_NAMES: Dict[str, str] = {
    "http_requests": "http_requests_total",
    "http_request_duration": "http_request_duration_seconds",
    "http_requests_in_flight": "http_requests_in_flight",
    "dependency_request_duration": "dependency_request_duration_seconds",
    "llm_request_duration": "llm_request_duration_seconds",
    "llm_tokens": "llm_tokens_total",
}

_metric_names: Dict[str, str] = dict(BASE_NAMES)
_names_locked = False


def _apply_prefix(prefix: str) -> None:
    p = prefix.strip()
    if p and not p.endswith("_"):
        p = p + "_"
    for key, base in BASE_NAMES.items():
        _metric_names[key] = f"{p}{base}" if p else base


def configure_metric_names(
    *,
    prefix: Optional[str] = None,
    names: Optional[Mapping[str, str]] = None,
) -> None:
    """Configure metric series names before instruments are first used.

    Parameters
    ----------
    prefix:
        Prepended to every default base name (a trailing ``_`` is added if
        missing). Combined with ``names`` overrides (overrides win per key).
    names:
        Optional full overrides keyed by logical name
        (``http_requests``, ``http_request_duration``, …). Unknown keys
        are ignored.

    Raises
    ------
    RuntimeError
        If instruments were already constructed (names are locked).
    """
    if _names_locked:
        raise RuntimeError(
            "Metric names are locked after instruments are constructed; "
            "call configure_metric_names() before importing instruments "
            "or set METRICS_NAME_PREFIX before process start."
        )
    if prefix is not None:
        _apply_prefix(prefix)
    if names:
        for key, value in names.items():
            if key in _metric_names and value:
                _metric_names[key] = value


def _bootstrap_from_env() -> None:
    prefix = os.getenv("METRICS_NAME_PREFIX", "").strip()
    if prefix:
        _apply_prefix(prefix)


_bootstrap_from_env()


def resolve(key: str) -> str:
    """Return the configured series name for a logical key and lock names."""
    global _names_locked
    _names_locked = True
    return _metric_names[key]


def metric_names() -> Dict[str, str]:
    """Return a copy of the current name map (may still be unlocked)."""
    return dict(_metric_names)


def reset_metric_names() -> None:
    """Restore defaults and unlock (tests only). Does not rebuild instruments."""
    global _names_locked
    _metric_names.clear()
    _metric_names.update(BASE_NAMES)
    _names_locked = False
    _bootstrap_from_env()
