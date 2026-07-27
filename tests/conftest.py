"""Shared pytest fixtures for fastapi-vitals tests."""

from __future__ import annotations

from typing import Iterator, Tuple

import pytest
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from fastapi_vitals._identity import reset_identity_labels
from fastapi_vitals.metrics import _exemplars as metrics_exemplars
from fastapi_vitals.metrics.middleware import reset_metrics_enabled


@pytest.fixture(autouse=True)
def _reset_process_caches() -> Iterator[None]:
    """Drop identity / metrics_enabled caches so monkeypatched env is visible.

    Production freezes these at first use; tests reconfigure ``SERVICE`` /
    ``METRICS_ENABLED`` per case and need a clean slate.
    """
    reset_identity_labels()
    reset_metrics_enabled()
    yield
    reset_identity_labels()
    reset_metrics_enabled()


@pytest.fixture
def memory_tracer(
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[Tuple[InMemorySpanExporter, TracerProvider]]:
    """Provide an in-memory tracer without claiming the process-global provider.

    OTEL ``set_tracer_provider`` is once-only; patch ``get_tracer`` on
    ``metrics._exemplars`` (where observers resolve the tracer) so suite order
    stays independent of ``setup_tracing`` tests.
    """
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    monkeypatch.setattr(
        metrics_exemplars.trace,
        "get_tracer",
        lambda name, *a, **k: provider.get_tracer(name),
    )
    # Drop process-local cache so observers pick up the patched get_tracer.
    metrics_exemplars.reset_metrics_tracer()
    yield exporter, provider
    exporter.clear()
    metrics_exemplars.reset_metrics_tracer()
