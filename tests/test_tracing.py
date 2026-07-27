import importlib
from unittest.mock import MagicMock

import pytest
from fastapi import FastAPI

from fastapi_observability import tracing as t


@pytest.fixture(autouse=True)
def _reset_tracing_state():
    """Ensure process-global tracer state does not leak across tests."""
    yield
    t.shutdown_tracing()


def test_get_trace_context_ids_returns_dashes_without_active_span():
    assert t.get_trace_context_ids() == ("-", "-")


def test_get_trace_context_ids_and_exemplar_labels_share_hex_format(memory_tracer):
    """Trace IDs and Prometheus exemplars must use the same OTEL hex convention."""
    from fastapi_observability.metrics import _exemplars as metrics_tracing

    _exporter, provider = memory_tracer
    tracer = provider.get_tracer("test-join")
    with tracer.start_as_current_span("join"):
        tid, sid = t.get_trace_context_ids()
        labels = metrics_tracing.exemplar_labels()

    assert labels is not None
    assert labels["trace_id"] == tid
    assert labels["span_id"] == sid
    assert len(tid) == 32 and len(sid) == 16
    assert tid == tid.lower() and sid == sid.lower()
    assert all(c in "0123456789abcdef" for c in tid + sid)


def test_format_trace_span_ids_invalid_and_valid():
    """Shared formatter: invalid → None; valid → 032x / 016x hex pair."""
    from opentelemetry.trace import INVALID_SPAN_CONTEXT, SpanContext, TraceFlags

    from fastapi_observability._trace_ids import format_trace_span_ids

    assert format_trace_span_ids(INVALID_SPAN_CONTEXT) is None

    ctx = SpanContext(
        trace_id=0xABC,
        span_id=0xDEF,
        is_remote=False,
        trace_flags=TraceFlags(0x01),
    )
    assert format_trace_span_ids(ctx) == (
        "00000000000000000000000000000abc",
        "0000000000000def",
    )


def test_setup_tracing_is_noop_without_endpoint(monkeypatch):
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_ENDPOINT", raising=False)
    app = FastAPI()
    assert t.setup_tracing(app) is False


def test_set_request_id_on_span_is_safe_without_active_span():
    # No recording span in this context: must be a silent no-op.
    t.set_request_id_on_span("req-123")


def test_excluded_urls_cover_health_probes_and_docs():
    """Tracing exclusions must cover the same health/docs paths as metrics."""
    import re

    pattern = re.compile(t.EXCLUDED_URLS)
    for path in (
        "/health",
        "/health/live",
        "/health/ready",
        "/metrics",
        "/docs",
        "/docs/oauth2-redirect",
        "/redoc",
        "/openapi.json",
    ):
        assert pattern.match(f"http://x{path}"), path
    assert not pattern.match("http://x/v2/ai-billing-api")
    assert not pattern.match("http://x/api/healthcheck")


def test_metrics_and_tracing_exclusions_share_one_source():
    """Single path list drives metrics frozenset and tracing regex."""
    import re

    from fastapi_observability import metrics as m
    from fastapi_observability.http_exclusions import EXCLUDED_HTTP_PATHS

    assert m.EXCLUDED_PATHS == frozenset(EXCLUDED_HTTP_PATHS)
    pattern = re.compile(t.EXCLUDED_URLS)
    for path in EXCLUDED_HTTP_PATHS:
        assert path in m.EXCLUDED_PATHS
        assert pattern.match(f"http://host{path}"), path


def _enable_tracing_endpoint(monkeypatch):
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://localhost:4318")
    # Avoid real OTLP export / flush during tests.
    monkeypatch.setattr(t, "OTLPSpanExporter", lambda *a, **k: MagicMock())


def test_setup_tracing_accepts_instrument_kwarg_when_extra_missing(monkeypatch):
    """Requesting an optional client instrumentation must not fail if the
    corresponding package is not installed — tracing still enables."""
    _enable_tracing_endpoint(monkeypatch)
    # Simulate missing optional extra regardless of local install state.
    real_import = importlib.import_module

    def fake_import(name, package=None):
        if name == "opentelemetry.instrumentation.httpx":
            raise ImportError("simulated missing extra")
        return real_import(name, package)

    monkeypatch.setattr(importlib, "import_module", fake_import)
    app = FastAPI()
    assert t.setup_tracing(app, instrument=["httpx"]) is True


def test_setup_tracing_instruments_httpx_when_requested(monkeypatch):
    pytest.importorskip("opentelemetry.instrumentation.httpx")
    from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor

    _enable_tracing_endpoint(monkeypatch)
    app = FastAPI()
    assert t.setup_tracing(app, instrument=["httpx"]) is True
    assert HTTPXClientInstrumentor().is_instrumented_by_opentelemetry is True


def test_setup_tracing_instruments_requests_when_requested(monkeypatch):
    pytest.importorskip("opentelemetry.instrumentation.requests")
    from opentelemetry.instrumentation.requests import RequestsInstrumentor

    _enable_tracing_endpoint(monkeypatch)
    app = FastAPI()
    assert t.setup_tracing(app, instrument=["requests"]) is True
    assert RequestsInstrumentor().is_instrumented_by_opentelemetry is True


def test_setup_tracing_skips_unknown_instrumentation_name(monkeypatch):
    _enable_tracing_endpoint(monkeypatch)
    app = FastAPI()
    assert t.setup_tracing(app, instrument=["not-a-real-client"]) is True


def test_setup_tracing_reads_otel_instrumentations_env(monkeypatch):
    pytest.importorskip("opentelemetry.instrumentation.httpx")
    from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor

    _enable_tracing_endpoint(monkeypatch)
    monkeypatch.setenv("OTEL_INSTRUMENTATIONS", "httpx")
    app = FastAPI()
    assert t.setup_tracing(app) is True
    assert HTTPXClientInstrumentor().is_instrumented_by_opentelemetry is True


def test_setup_tracing_instrument_kwarg_overrides_env(monkeypatch):
    pytest.importorskip("opentelemetry.instrumentation.httpx")
    pytest.importorskip("opentelemetry.instrumentation.requests")
    from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
    from opentelemetry.instrumentation.requests import RequestsInstrumentor

    _enable_tracing_endpoint(monkeypatch)
    monkeypatch.setenv("OTEL_INSTRUMENTATIONS", "httpx")
    app = FastAPI()
    # Explicit empty list: do not fall back to env.
    assert t.setup_tracing(app, instrument=[]) is True
    assert HTTPXClientInstrumentor().is_instrumented_by_opentelemetry is False
    assert RequestsInstrumentor().is_instrumented_by_opentelemetry is False


def test_default_setup_tracing_does_not_instrument_clients(monkeypatch):
    """Consumers that never opt in stay unchanged (no client auto-instrument)."""
    pytest.importorskip("opentelemetry.instrumentation.httpx")
    from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor

    _enable_tracing_endpoint(monkeypatch)
    monkeypatch.delenv("OTEL_INSTRUMENTATIONS", raising=False)
    app = FastAPI()
    assert t.setup_tracing(app) is True
    assert HTTPXClientInstrumentor().is_instrumented_by_opentelemetry is False


def test_shutdown_tracing_uninstruments_clients(monkeypatch):
    pytest.importorskip("opentelemetry.instrumentation.httpx")
    from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor

    _enable_tracing_endpoint(monkeypatch)
    app = FastAPI()
    assert t.setup_tracing(app, instrument=["httpx"]) is True
    assert HTTPXClientInstrumentor().is_instrumented_by_opentelemetry is True
    t.shutdown_tracing()
    assert HTTPXClientInstrumentor().is_instrumented_by_opentelemetry is False


def test_setup_tracing_soft_fails_when_instrumentor_raises(monkeypatch):
    """Client instrumentor errors are soft: tracing stays enabled."""
    pytest.importorskip("opentelemetry.instrumentation.httpx")
    from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor

    _enable_tracing_endpoint(monkeypatch)

    def boom(self, *a, **k):
        raise RuntimeError("instrument failed")

    monkeypatch.setattr(HTTPXClientInstrumentor, "instrument", boom)
    app = FastAPI()
    assert t.setup_tracing(app, instrument=["httpx"]) is True
    assert t._tracer_provider is not None
    assert HTTPXClientInstrumentor().is_instrumented_by_opentelemetry is False


def test_fail_open_before_commit_does_not_instrument_clients(monkeypatch):
    """Exporter failure is pre-commit: no FastAPI/client patches, return False."""
    pytest.importorskip("opentelemetry.instrumentation.httpx")
    from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor

    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://localhost:4318")

    def boom(*a, **k):
        raise RuntimeError("exporter construction failed")

    monkeypatch.setattr(t, "OTLPSpanExporter", boom)
    app = FastAPI()
    assert t.setup_tracing(app, instrument=["httpx"]) is False
    assert t._tracer_provider is None
    assert HTTPXClientInstrumentor().is_instrumented_by_opentelemetry is False


def test_supported_instrumentations_is_name_frozenset():
    """Public surface is the set of accepted client names (not import specs)."""
    assert isinstance(t.SUPPORTED_INSTRUMENTATIONS, frozenset)
    assert t.SUPPORTED_INSTRUMENTATIONS == frozenset(
        {"httpx", "requests", "sqlalchemy", "redis"}
    )


def test_setup_tracing_dedupes_instrument_names(monkeypatch):
    pytest.importorskip("opentelemetry.instrumentation.httpx")
    from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor

    _enable_tracing_endpoint(monkeypatch)
    calls = {"n": 0}
    real_instrument = HTTPXClientInstrumentor.instrument

    def counting(self, *a, **k):
        calls["n"] += 1
        return real_instrument(self, *a, **k)

    monkeypatch.setattr(HTTPXClientInstrumentor, "instrument", counting)
    app = FastAPI()
    assert t.setup_tracing(app, instrument=["httpx", "HTTPX", "httpx"]) is True
    assert calls["n"] == 1
    assert HTTPXClientInstrumentor().is_instrumented_by_opentelemetry is True


def _provider_sampler(provider):
    return getattr(provider, "sampler", None)


def test_setup_tracing_default_sampler_is_parentbased_always_on(monkeypatch):
    from opentelemetry.sdk.trace.sampling import ALWAYS_ON, ParentBased

    _enable_tracing_endpoint(monkeypatch)
    monkeypatch.delenv("OTEL_TRACES_SAMPLER", raising=False)
    monkeypatch.delenv("OTEL_TRACES_SAMPLER_ARG", raising=False)
    app = FastAPI()
    assert t.setup_tracing(app) is True
    sampler = _provider_sampler(t._tracer_provider)
    assert isinstance(sampler, ParentBased)
    # ParentBased wraps the root decision; default root is ALWAYS_ON.
    assert sampler._root is ALWAYS_ON or type(sampler._root).__name__ == "AlwaysOnSampler"


def test_setup_tracing_honors_traceidratio_sampler(monkeypatch):
    from opentelemetry.sdk.trace.sampling import ParentBased, TraceIdRatioBased

    _enable_tracing_endpoint(monkeypatch)
    monkeypatch.setenv("OTEL_TRACES_SAMPLER", "parentbased_traceidratio")
    monkeypatch.setenv("OTEL_TRACES_SAMPLER_ARG", "0.25")
    app = FastAPI()
    assert t.setup_tracing(app) is True
    sampler = _provider_sampler(t._tracer_provider)
    assert isinstance(sampler, ParentBased)
    root = sampler._root
    assert isinstance(root, TraceIdRatioBased)
    assert root.rate == pytest.approx(0.25)


def test_setup_tracing_fail_open_when_exporter_raises(monkeypatch):
    """Pre-commit failure: never install module singleton / return False."""
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://localhost:4318")

    def boom(*a, **k):
        raise RuntimeError("exporter construction failed")

    monkeypatch.setattr(t, "OTLPSpanExporter", boom)
    app = FastAPI()
    assert t.setup_tracing(app) is False
    assert t._tracer_provider is None
    assert t._instrumented_app is None


def test_setup_tracing_fail_open_when_batch_processor_raises(monkeypatch):
    _enable_tracing_endpoint(monkeypatch)

    def boom(*a, **k):
        raise RuntimeError("batch processor construction failed")

    monkeypatch.setattr(t, "BatchSpanProcessor", boom)
    app = FastAPI()
    assert t.setup_tracing(app) is False
    assert t._tracer_provider is None
    assert t._instrumented_app is None


def test_setup_tracing_commits_singleton_after_provider_built(monkeypatch):
    """Once exporter+processor succeed, setup returns True and sets singleton."""
    _enable_tracing_endpoint(monkeypatch)
    app = FastAPI()
    assert t.setup_tracing(app) is True
    assert t._tracer_provider is not None
    assert t._instrumented_app is app
    # Second call is a no-op singleton gate (module state, not dual-teardown).
    assert t.setup_tracing(app) is False


def test_batch_processor_constructed_without_size_overrides(monkeypatch):
    """Leave BSP kwargs unset so OTEL_BSP_* env vars remain authoritative."""
    _enable_tracing_endpoint(monkeypatch)
    calls = []
    real_bsp = t.BatchSpanProcessor

    def capture(exporter, *args, **kwargs):
        calls.append((args, kwargs))
        return real_bsp(exporter, *args, **kwargs)

    monkeypatch.setattr(t, "BatchSpanProcessor", capture)
    app = FastAPI()
    assert t.setup_tracing(app) is True
    assert len(calls) == 1
    args, kwargs = calls[0]
    assert args == ()
    for key in (
        "max_queue_size",
        "schedule_delay_millis",
        "max_export_batch_size",
        "export_timeout_millis",
    ):
        assert key not in kwargs


def test_setup_tracing_accepts_exporter_and_processor_factories(monkeypatch):
    """Injectable factories let tests avoid monkeypatching OTLP classes."""
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
        InMemorySpanExporter,
    )

    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://localhost:4318")
    exporter = InMemorySpanExporter()
    factories = {"exporter": 0, "processor": 0}

    def exporter_factory():
        factories["exporter"] += 1
        return exporter

    def processor_factory(exp):
        factories["processor"] += 1
        return SimpleSpanProcessor(exp)

    app = FastAPI()
    assert (
        t.setup_tracing(
            app,
            exporter_factory=exporter_factory,
            processor_factory=processor_factory,
        )
        is True
    )
    assert factories == {"exporter": 1, "processor": 1}
    assert t._tracer_provider is not None
