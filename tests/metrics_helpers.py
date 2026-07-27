"""Shared helpers for metrics middleware / observer tests."""

from __future__ import annotations

from typing import Optional

from fastapi import FastAPI
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from prometheus_client import REGISTRY
from prometheus_client.openmetrics.exposition import (
    generate_latest as generate_latest_openmetrics,
)

from fastapi_observability import metrics as m


def _scrape() -> str:
    return generate_latest_openmetrics(REGISTRY).decode()


def _finished_spans(exporter: InMemorySpanExporter, name_prefix: str = "dependency "):
    return [
        s
        for s in exporter.get_finished_spans()
        if s.name.startswith(name_prefix)
    ]


def _app(*, fail=False):
    app = FastAPI()
    m.setup_metrics(app)

    @app.get("/ping")
    def ping():
        return {"ok": True}

    @app.get("/items/{item_id}")
    def get_item(item_id: int):
        return {"id": item_id}

    @app.get("/boom")
    def boom():
        if fail:
            raise RuntimeError("boom")
        return {"ok": True}

    @app.get("/health")
    def health():
        return {"status": "ok"}

    @app.get("/health/live")
    def health_live():
        return {"status": "ok"}

    @app.get("/health/ready")
    def health_ready():
        return {"status": "ok"}

    @app.get("/metrics")
    def metrics_ep():
        return m.metrics_response()

    return app


def _in_flight_value(*, route: str, method: str = "GET") -> Optional[float]:
    """Read the process-local gauge sample (avoids scraping text / label order)."""
    for metric in m.HTTP_REQUESTS_IN_FLIGHT.collect():
        for sample in metric.samples:
            if (
                sample.name == "http_requests_in_flight"
                and sample.labels.get("route") == route
                and sample.labels.get("method") == method
            ):
                return sample.value
    return None
