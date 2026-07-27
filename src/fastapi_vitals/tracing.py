"""OpenTelemetry setup for FastAPI services."""

from __future__ import annotations

import logging
import os
from typing import Any, Callable, Optional, Sequence, Tuple

from fastapi import FastAPI
from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import SpanProcessor, TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor, SpanExporter

from . import _instrumentors, _resource
from ._identity import DEFAULT_SERVICE_NAME
from ._trace_ids import format_trace_span_ids
from .http_exclusions import excluded_urls_regex

ExporterFactory = Callable[[], SpanExporter]
ProcessorFactory = Callable[[SpanExporter], SpanProcessor]

__all__ = [
    "DEFAULT_SERVICE_NAME",
    "get_trace_context_ids",
    "set_request_id_on_span",
    "setup_tracing",
    "shutdown_tracing",
    "EXCLUDED_URLS",
    "SUPPORTED_INSTRUMENTATIONS",
]

logger = logging.getLogger(__name__)

EXCLUDED_URLS = excluded_urls_regex()
SUPPORTED_INSTRUMENTATIONS = _instrumentors.SUPPORTED_INSTRUMENTATIONS

_tracer_provider: Optional[TracerProvider] = None
_instrumented_app: Optional[FastAPI] = None


def get_trace_context_ids() -> Tuple[str, str]:
    ids = format_trace_span_ids(trace.get_current_span().get_span_context())
    return ids if ids is not None else ("-", "-")


def set_request_id_on_span(request_id: str) -> None:
    span = trace.get_current_span()
    if span.is_recording():
        span.set_attribute("http.request_id", request_id)


def _server_request_hook(span: trace.Span, scope: Any) -> None:
    if not span or not span.is_recording():
        return
    request_id = scope.get("request_id")
    if request_id:
        span.set_attribute("http.request_id", request_id)


def _default_exporter_factory() -> SpanExporter:
    # Default kwargs so OTEL_EXPORTER_OTLP_* env vars remain authoritative.
    return OTLPSpanExporter()


def _default_processor_factory(exporter: SpanExporter) -> SpanProcessor:
    # Default kwargs so OTEL_BSP_* env vars remain authoritative.
    return BatchSpanProcessor(exporter)


def _build_provider(
    service_name: str,
    *,
    exporter_factory: Optional[ExporterFactory] = None,
    processor_factory: Optional[ProcessorFactory] = None,
) -> TracerProvider:
    """Assemble TracerProvider + exporter/processor (pre-commit, may raise).

    Optional factories let tests inject in-memory exporters without monkeypatching
    ``OTLPSpanExporter`` / ``BatchSpanProcessor`` module attributes. Production
    callers leave them unset for default OTLP + batch processing.
    """
    resource = Resource.create(_resource.build_resource_attributes(service_name))
    # sampler=None → SDK reads OTEL_TRACES_SAMPLER / OTEL_TRACES_SAMPLER_ARG
    provider = TracerProvider(resource=resource)
    exp_factory = exporter_factory or _default_exporter_factory
    proc_factory = processor_factory or _default_processor_factory
    provider.add_span_processor(proc_factory(exp_factory()))
    return provider


def _instrument_fastapi(app: FastAPI, provider: TracerProvider) -> None:
    """Wire FastAPI request spans with shared exclusions and request-id hook."""
    FastAPIInstrumentor.instrument_app(
        app,
        tracer_provider=provider,
        excluded_urls=EXCLUDED_URLS,
        server_request_hook=_server_request_hook,
    )


def _log_tracing_enabled(
    endpoint: str, provider: TracerProvider, service_name: str
) -> None:
    """Post-commit info log; never raises into setup."""
    try:
        sampler = getattr(provider, "sampler", None)
        sampler_desc = (
            sampler.get_description()
            if sampler is not None and hasattr(sampler, "get_description")
            else repr(sampler)
        )
        logger.info(
            "OTEL tracing enabled endpoint=%s sampler=%s service=%s",
            endpoint,
            sampler_desc,
            service_name,
        )
    except Exception:  # pragma: no cover - defensive; commit already succeeded
        pass


def setup_tracing(
    app: FastAPI,
    instrument: Optional[Sequence[str]] = None,
    *,
    exporter_factory: Optional[ExporterFactory] = None,
    processor_factory: Optional[ProcessorFactory] = None,
) -> bool:
    """Configure OTLP tracing and FastAPI instrumentation.

    No-op (returns False) unless ``OTEL_EXPORTER_OTLP_ENDPOINT`` is set, or if
    tracing was already configured in this process.

    Sampling and batch export use standard OpenTelemetry env vars resolved by
    the SDK (``OTEL_TRACES_SAMPLER`` / ``OTEL_TRACES_SAMPLER_ARG``,
    ``OTEL_BSP_*``, ``OTEL_EXPORTER_OTLP_*``).

    Fail-open is pre-commit only: exporter/processor construction errors return
    False *before* ``set_tracer_provider`` (no dual-teardown). After commit,
    the module singleton matches the installed provider and stays set. A dead
    collector is handled on the batch worker thread and does not block request
    handling. Use ``shutdown_tracing`` only for true process shutdown cleanup.

    Resource attributes always include service identity and
    ``process.runtime.*``. Optional platform adapters (e.g. AWS ECS under
    ``fastapi_vitals.adapters.ecs``) may add ``cloud.*`` attributes
    and a preferred ``service.instance.id`` when their runtime is detected.
    This package never writes ``cloud.account.id``.

    Optional client auto-instrumentation fills dependency spans for libraries
    that do not call ``observe_dependency``. Request via::

        setup_tracing(app, instrument=["httpx", "requests"])

    or env ``OTEL_INSTRUMENTATIONS=httpx,requests``. Supported names are in
    ``SUPPORTED_INSTRUMENTATIONS`` (``httpx``, ``requests``, ``sqlalchemy``,
    ``redis``). Each requires the matching optional extra (e.g.
    ``fastapi-vitals[httpx]``). Missing packages and individual
    instrumentor failures are skipped with a warning; tracing still enables.

    Optional ``exporter_factory`` / ``processor_factory`` replace the default
    ``OTLPSpanExporter`` + ``BatchSpanProcessor`` (tests / custom pipelines).
    When omitted, construction uses default kwargs so SDK env vars win.

    **Do not double-wrap.** Prefer either ``observe_dependency`` *or* the
    matching client instrumentor for a given library — not both — or APM will
    show two child spans per outbound call.
    """
    global _instrumented_app, _tracer_provider

    endpoint = os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT")
    if not endpoint or _tracer_provider is not None:
        return False

    service_name = os.getenv(
        "OTEL_SERVICE_NAME",
        os.getenv("SERVICE", DEFAULT_SERVICE_NAME),
    )

    # --- Pre-commit: may return False without touching globals ---
    try:
        provider = _build_provider(
            service_name,
            exporter_factory=exporter_factory,
            processor_factory=processor_factory,
        )
    except Exception:
        logger.exception(
            "Failed to configure OTEL tracing; continuing without tracing"
        )
        return False

    # --- Commit: process-global provider + module singleton stay consistent ---
    # Do not return False or clear globals after this point.
    trace.set_tracer_provider(provider)
    _instrument_fastapi(app, provider)
    # Client instrumentors soft-fail per name; never roll back tracing.
    _instrumentors.apply(provider, instrument)
    _tracer_provider = provider
    _instrumented_app = app

    # Post-commit: never raise or roll back tracing for observability of setup.
    _log_tracing_enabled(endpoint, provider, service_name)
    return True


def shutdown_tracing() -> None:
    """Tear down tracing for process shutdown (tests / app lifespan)."""
    global _instrumented_app, _tracer_provider

    _instrumentors.shutdown()

    if _instrumented_app is not None:
        FastAPIInstrumentor.uninstrument_app(_instrumented_app)
        _instrumented_app = None
    if _tracer_provider is not None:
        _tracer_provider.shutdown()
        _tracer_provider = None
