"""Optional pytest-benchmark cases for route template resolution.

Run with::

    .venv/bin/pytest tests/test_bench_route_template.py --benchmark-only -q

Skipped when pytest-benchmark is not installed (not required for default CI).
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from starlette.requests import Request

from fastapi_observability.metrics import setup_metrics
from fastapi_observability.metrics.route_templates import route_template

pytest.importorskip("pytest_benchmark")


def _app(*, cache_unmatched: bool = False) -> FastAPI:
    app = FastAPI()
    setup_metrics(app, cache_unmatched=cache_unmatched)

    @app.get("/ping")
    def ping():
        return {"ok": True}

    @app.get("/items/{item_id}")
    def item(item_id: int):
        return {"id": item_id}

    return app


def _request(app: FastAPI, path: str) -> Request:
    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": path,
        "raw_path": path.encode(),
        "query_string": b"",
        "headers": [],
        "client": ("test", 50000),
        "server": ("test", 80),
        "app": app,
    }
    return Request(scope)


def test_bench_route_template_cold(benchmark):
    app = _app()
    req = _request(app, "/items/42")

    def run():
        app.state.route_template_cache.clear()
        return route_template(req)

    assert benchmark(run) == "/items/{item_id}"


def test_bench_route_template_warm(benchmark):
    app = _app()
    req = _request(app, "/items/99")
    assert route_template(req) == "/items/{item_id}"

    def run():
        return route_template(req)

    assert benchmark(run) == "/items/{item_id}"


def test_bench_route_template_unmatched(benchmark):
    app = _app()
    req = _request(app, "/missing")

    def run():
        return route_template(req)

    assert benchmark(run) == "unmatched"


def test_bench_route_template_unmatched_negative_cache(benchmark):
    app = _app(cache_unmatched=True)
    req = _request(app, "/missing")
    assert route_template(req) == "unmatched"

    def run():
        return route_template(req)

    assert benchmark(run) == "unmatched"
