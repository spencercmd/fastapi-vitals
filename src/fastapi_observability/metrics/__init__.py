"""Prometheus RED metrics, dependency/LLM observers, and HTTP middleware.

Public import path is ``fastapi_observability.metrics``. Internal modules
(``instruments``, ``middleware``, ``observe``) are an SRP split only —
consumers should import from this package root.

To rename series, call ``configure_metric_names`` (or set
``METRICS_NAME_PREFIX``) **before** importing this package.
"""

from __future__ import annotations

from fastapi_observability._identity import (
    DEFAULT_SERVICE_NAME,
    identity_labels,
)

from .instruments import (
    DEPENDENCY_REQUEST_DURATION,
    EXCLUDED_PATHS,
    HTTP_REQUEST_DURATION,
    HTTP_REQUESTS,
    HTTP_REQUESTS_IN_FLIGHT,
    IN_FLIGHT_LABELS,
    LLM_REQUEST_DURATION,
    LLM_TOKENS,
    RED_LABELS,
)
from .middleware import (
    metrics_enabled,
    metrics_middleware,
    metrics_response,
    setup_metrics,
)
from .names import configure_metric_names, metric_names
from .observe import observe_dependency, observe_llm

__all__ = [
    "RED_LABELS",
    "IN_FLIGHT_LABELS",
    "HTTP_REQUESTS",
    "HTTP_REQUEST_DURATION",
    "HTTP_REQUESTS_IN_FLIGHT",
    "DEPENDENCY_REQUEST_DURATION",
    "LLM_REQUEST_DURATION",
    "LLM_TOKENS",
    "EXCLUDED_PATHS",
    "identity_labels",
    "metrics_enabled",
    "metrics_middleware",
    "setup_metrics",
    "metrics_response",
    "observe_dependency",
    "observe_llm",
    "DEFAULT_SERVICE_NAME",
    "configure_metric_names",
    "metric_names",
]
