"""Shared OTEL helpers for metrics observers (tracer cache + exemplars)."""

from __future__ import annotations

from typing import Dict, Optional

from opentelemetry import trace
from opentelemetry.trace import Tracer

from fastapi_vitals._process_cache import ProcessCache
from fastapi_vitals._trace_ids import format_trace_span_ids

_TRACER_NAME = "fastapi_vitals.metrics"  # stable OTEL tracer name
_tracer = ProcessCache(lambda: trace.get_tracer(_TRACER_NAME))


def get_metrics_tracer() -> Tracer:
    """Return a process-cached tracer for metrics observer spans."""
    return _tracer.get()


def reset_metrics_tracer() -> None:
    """Drop the cached tracer (tests that swap providers)."""
    _tracer.reset()


def exemplar_labels() -> Optional[Dict[str, str]]:
    """Return ``trace_id`` / ``span_id`` exemplar labels when a span is active.

    Uses the OpenTelemetry ↔ Prometheus convention (lowercase hex). Returns
    ``None`` when tracing is off or no valid span is current so observations
    still record without exemplars. ``Histogram.observe`` accepts ``exemplar=None``.
    """
    ids = format_trace_span_ids(trace.get_current_span().get_span_context())
    if ids is None:
        return None
    return {"trace_id": ids[0], "span_id": ids[1]}
