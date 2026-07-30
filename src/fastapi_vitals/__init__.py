"""OpenTelemetry tracing + Prometheus RED metrics for FastAPI services.

Public API:
    tracing:  setup_tracing, shutdown_tracing, get_trace_context_ids,
              set_request_id_on_span, EXCLUDED_URLS, SUPPORTED_INSTRUMENTATIONS
              (optional instrument= / OTEL_INSTRUMENTATIONS for client auto-instr)
    metrics:  setup_metrics, metrics_response, mark_process_dead,
              observe_dependency, observe_llm, metrics_enabled, identity_labels,
              HTTP_REQUESTS, HTTP_REQUEST_DURATION, HTTP_REQUESTS_IN_FLIGHT,
              DEPENDENCY_REQUEST_DURATION, LLM_REQUEST_DURATION, LLM_TOKENS,
              RED_LABELS, IN_FLIGHT_LABELS, metrics_middleware

``observe_dependency`` / ``observe_llm`` are dual sync/async (``with`` /
``async with``) and emit Prometheus metrics plus an OTEL child span. Prefer
``observe_dependency`` *or* client auto-instrumentation per library (not both);
use ``observe_llm`` instead of nesting dependency for model calls.
``metrics_response`` is OpenMetrics so histogram exemplars are retained.
"""

__version__ = "0.1.0"
