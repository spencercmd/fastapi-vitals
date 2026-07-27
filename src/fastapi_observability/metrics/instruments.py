"""Process-global Prometheus instruments for RED / dependency / LLM metrics.

Series names come from ``names.resolve`` (see ``configure_metric_names`` /
``METRICS_NAME_PREFIX``). Instruments are constructed at import of this
module — configure names first if you need a non-default prefix.
"""

from __future__ import annotations

from prometheus_client import Counter, Gauge, Histogram

from fastapi_observability.http_exclusions import EXCLUDED_HTTP_PATHS

from .names import resolve as _resolve_name

RED_LABELS = ("service", "env", "version", "method", "route", "status_class")
# No status_class (unknown mid-request) or version (static per process).
IN_FLIGHT_LABELS = ("service", "env", "method", "route")
# Derived from http_exclusions.EXCLUDED_HTTP_PATHS (shared with tracing).
EXCLUDED_PATHS = frozenset(EXCLUDED_HTTP_PATHS)
ENABLED_VALUES = frozenset({"1", "true", "yes", "on"})
# Shared across HTTP / dependency / LLM histograms. Upper buckets to 300s so
# LLM generate calls are not all +Inf; shorter HTTP/dep calls still fill lower buckets.
DURATION_BUCKETS = (
    0.01,
    0.025,
    0.05,
    0.1,
    0.25,
    0.5,
    1.0,
    2.5,
    5.0,
    10.0,
    15.0,
    30.0,
    60.0,
    90.0,
    120.0,
    180.0,
    300.0,
)
# status values for observe_llm; unknown strings coerce to "error" (fail-open).
LLM_STATUS_VALUES = frozenset({"ok", "error", "rate_limited"})
# Bound finish_reason label cardinality; free-form values map to "other".
LLM_FINISH_REASONS = frozenset(
    {"stop", "length", "content_filter", "tool_calls", "unknown"}
)

HTTP_REQUESTS = Counter(
    _resolve_name("http_requests"), "Total HTTP requests.", RED_LABELS
)
HTTP_REQUEST_DURATION = Histogram(
    _resolve_name("http_request_duration"),
    "HTTP request duration in seconds.",
    RED_LABELS,
    buckets=DURATION_BUCKETS,
)
# Single-process Gauge (correct for single-worker deployments). When using
# multi-worker Gunicorn/Uvicorn with PROMETHEUS_MULTIPROC_DIR, you need
# multiprocess_mode="livesum" and MultiProcessCollector in metrics_response
# (not implemented here — env must be set before importing metrics).
HTTP_REQUESTS_IN_FLIGHT = Gauge(
    _resolve_name("http_requests_in_flight"),
    "HTTP requests currently being processed.",
    IN_FLIGHT_LABELS,
)
DEPENDENCY_REQUEST_DURATION = Histogram(
    _resolve_name("dependency_request_duration"),
    "Outbound dependency request duration in seconds.",
    ("service", "env", "version", "dependency", "operation", "status"),
    buckets=DURATION_BUCKETS,
)
LLM_REQUEST_DURATION = Histogram(
    _resolve_name("llm_request_duration"),
    "LLM request duration in seconds.",
    (
        "service",
        "env",
        "version",
        "provider",
        "model",
        "operation",
        "status",
        "finish_reason",
    ),
    buckets=DURATION_BUCKETS,
)
LLM_TOKENS = Counter(
    _resolve_name("llm_tokens"),
    "LLM tokens consumed (input and output).",
    (
        "service",
        "env",
        "version",
        "provider",
        "model",
        "operation",
        "token_type",
    ),
)
