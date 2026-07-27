"""Shared OpenTelemetry trace/span ID hex formatting.

Single convention for ``get_trace_context_ids`` (logging / response headers)
and Prometheus histogram exemplars so scrape↔trace joins stay stable.
"""

from __future__ import annotations

from typing import Optional, Tuple

from opentelemetry.trace import SpanContext


def format_trace_span_ids(span_context: SpanContext) -> Optional[Tuple[str, str]]:
    """Return ``(trace_id, span_id)`` as lowercase OTEL hex, or ``None`` if invalid.

    Uses the OpenTelemetry ↔ Prometheus convention: ``trace_id`` is 32 hex
    chars (``032x``), ``span_id`` is 16 hex chars (``016x``).
    """
    if not span_context.is_valid:
        return None
    return f"{span_context.trace_id:032x}", f"{span_context.span_id:016x}"
