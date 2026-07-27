#!/usr/bin/env python3
"""Micro-benchmark for route template resolution (cold / warm / unmatched).

Usage (from repo root, with the package installed editable)::

    .venv/bin/python scripts/bench_route_template.py
    .venv/bin/python scripts/bench_route_template.py --iterations 5000

Not a CI gate — use for local regressions when changing the route walker.
"""

from __future__ import annotations

import argparse
import statistics
import time
from typing import Callable, List

from fastapi import FastAPI
from starlette.testclient import TestClient

from fastapi_observability.metrics import setup_metrics
from fastapi_observability.metrics.route_templates import route_template


def _build_app(*, cache_unmatched: bool = False) -> FastAPI:
    app = FastAPI()
    setup_metrics(app, cache_unmatched=cache_unmatched)

    @app.get("/ping")
    def ping():
        return {"ok": True}

    @app.get("/items/{item_id}")
    def item(item_id: int):
        return {"id": item_id}

    return app


def _time_calls(fn: Callable[[], None], iterations: int) -> List[float]:
    samples: List[float] = []
    for _ in range(iterations):
        started = time.perf_counter()
        fn()
        samples.append(time.perf_counter() - started)
    return samples


def _report(label: str, samples: List[float]) -> None:
    us = [s * 1_000_000 for s in samples]
    print(
        f"{label:28s}  n={len(us):5d}  "
        f"mean={statistics.mean(us):8.2f} µs  "
        f"p50={statistics.median(us):8.2f} µs  "
        f"p95={statistics.quantiles(us, n=20)[18]:8.2f} µs"
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--iterations", type=int, default=2000)
    args = parser.parse_args()
    n = args.iterations

    app = _build_app()
    # Warm ASGI app construction once outside timed sections.
    client = TestClient(app)
    assert client.get("/ping").status_code == 200

    # Build request scopes without full middleware for pure walker cost.
    from starlette.requests import Request

    def make_request(path: str, method: str = "GET") -> Request:
        scope = {
            "type": "http",
            "asgi": {"version": "3.0"},
            "http_version": "1.1",
            "method": method,
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

    cold_app = _build_app()
    # Empty cache: first resolution of each path is a full walk.
    cold_req = make_request("/items/42")
    cold_req.scope["app"] = cold_app

    def cold() -> None:
        # Clear cache between samples so every call is a miss.
        cache = cold_app.state.route_template_cache
        cache.clear()
        cold_req.scope["app"] = cold_app
        assert route_template(cold_req) == "/items/{item_id}"

    warm_req = make_request("/items/99")
    # Seed warm path once.
    assert route_template(warm_req) == "/items/{item_id}"

    def warm() -> None:
        assert route_template(warm_req) == "/items/{item_id}"

    unmatched_req = make_request("/no-such-route")

    def unmatched() -> None:
        # Default: unmatched not cached → full walk each time.
        assert route_template(unmatched_req) == "unmatched"

    neg_app = _build_app(cache_unmatched=True)
    neg_req = make_request("/also-missing")
    neg_req.scope["app"] = neg_app
    # Seed negative cache.
    assert route_template(neg_req) == "unmatched"

    def unmatched_cached() -> None:
        assert route_template(neg_req) == "unmatched"

    print(f"route_template micro-bench (iterations={n})")
    _report("cold (cache miss)", _time_calls(cold, n))
    _report("warm (cache hit)", _time_calls(warm, n))
    _report("unmatched (no neg cache)", _time_calls(unmatched, n))
    _report("unmatched (neg cache hit)", _time_calls(unmatched_cached, n))


if __name__ == "__main__":
    main()
