#!/usr/bin/env python3
"""Micro-benchmark for route template resolution (cold / warm / unmatched).

Usage (from repo root, with the package installed editable)::

    .venv/bin/python scripts/bench_route_template.py
    .venv/bin/python scripts/bench_route_template.py --iterations 5000
    .venv/bin/python scripts/bench_route_template.py --route-counts 10,100,500

Not a CI gate — use for local regressions when changing the route walker.
"""

from __future__ import annotations

import argparse
import statistics
import time
from typing import Callable, List, Sequence

from fastapi import FastAPI
from starlette.requests import Request
from starlette.testclient import TestClient

from fastapi_vitals.metrics import setup_metrics
from fastapi_vitals.metrics.route_templates import route_template


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


def _add_route(app: FastAPI, path: str) -> None:
    @app.get(path)
    def _handler() -> dict:
        return {"ok": True}


def _build_flat_app(n_routes: int) -> FastAPI:
    """Flat table of N distinct GET /r{i} routes (i = 0..N-1)."""
    # Disable docs/OpenAPI so len(app.routes) == n_routes (O(R) walk size).
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    setup_metrics(app)
    for i in range(n_routes):
        _add_route(app, f"/r{i}")
    return app


def _make_request(app: FastAPI, path: str, method: str = "GET") -> Request:
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


def _time_calls(fn: Callable[[], None], iterations: int) -> List[float]:
    samples: List[float] = []
    for _ in range(iterations):
        started = time.perf_counter()
        fn()
        samples.append(time.perf_counter() - started)
    return samples


def _mean_us(samples: List[float]) -> float:
    return statistics.mean(s * 1_000_000 for s in samples)


def _report(label: str, samples: List[float]) -> None:
    us = [s * 1_000_000 for s in samples]
    print(
        f"{label:28s}  n={len(us):5d}  "
        f"mean={statistics.mean(us):8.2f} µs  "
        f"p50={statistics.median(us):8.2f} µs  "
        f"p95={statistics.quantiles(us, n=20)[18]:8.2f} µs"
    )


def _parse_route_counts(raw: str) -> List[int]:
    try:
        counts = [int(part.strip()) for part in raw.split(",") if part.strip()]
    except ValueError as exc:
        raise argparse.ArgumentTypeError(
            "expected a comma-separated list of positive integers "
            f"(got {raw!r})"
        ) from exc
    if not counts or any(n < 1 for n in counts):
        raise argparse.ArgumentTypeError(
            "expected a comma-separated list of positive integers "
            f"(got {raw!r})"
        )
    return counts


def _cold_last_route(app: FastAPI, req: Request, expected: str) -> Callable[[], None]:
    def cold() -> None:
        app.state.route_template_cache.clear()
        assert route_template(req) == expected

    return cold


def _warm_hit(req: Request, expected: str) -> Callable[[], None]:
    def warm() -> None:
        assert route_template(req) == expected

    return warm


def _run_scaling_study(route_counts: Sequence[int], iterations: int) -> None:
    print()
    print("Scaling study (flat table, cold last-route):")
    print(f"{'Routes':>6}  {'Cold mean':>12}  {'Warm mean':>12}")
    for n_routes in route_counts:
        app = _build_flat_app(n_routes)
        last_path = f"/r{n_routes - 1}"
        req = _make_request(app, last_path)
        assert route_template(req) == last_path

        cold_mean = _mean_us(_time_calls(_cold_last_route(app, req, last_path), iterations))
        warm_mean = _mean_us(_time_calls(_warm_hit(req, last_path), iterations))
        print(f"{n_routes:6d}  {cold_mean:9.2f} µs  {warm_mean:9.2f} µs")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--iterations", type=int, default=2000)
    parser.add_argument(
        "--route-counts",
        type=_parse_route_counts,
        default=[10, 100, 500],
        help="comma-separated route table sizes for the scaling study "
        "(default: 10,100,500)",
    )
    args = parser.parse_args()
    n = args.iterations

    app = _build_app()
    # Warm ASGI app construction once outside timed sections.
    client = TestClient(app)
    assert client.get("/ping").status_code == 200

    cold_app = _build_app()
    # Empty cache: first resolution of each path is a full walk.
    cold_req = _make_request(cold_app, "/items/42")

    def cold() -> None:
        # Clear cache between samples so every call is a miss.
        cache = cold_app.state.route_template_cache
        cache.clear()
        assert route_template(cold_req) == "/items/{item_id}"

    warm_req = _make_request(app, "/items/99")
    # Seed warm path once.
    assert route_template(warm_req) == "/items/{item_id}"

    def warm() -> None:
        assert route_template(warm_req) == "/items/{item_id}"

    unmatched_req = _make_request(app, "/no-such-route")

    def unmatched() -> None:
        # Default: unmatched not cached → full walk each time.
        assert route_template(unmatched_req) == "unmatched"

    neg_app = _build_app(cache_unmatched=True)
    neg_req = _make_request(neg_app, "/also-missing")
    # Seed negative cache.
    assert route_template(neg_req) == "unmatched"

    def unmatched_cached() -> None:
        assert route_template(neg_req) == "unmatched"

    print(f"route_template micro-bench (iterations={n})")
    _report("cold (cache miss)", _time_calls(cold, n))
    _report("warm (cache hit)", _time_calls(warm, n))
    _report("unmatched (no neg cache)", _time_calls(unmatched, n))
    _report("unmatched (neg cache hit)", _time_calls(unmatched_cached, n))

    _run_scaling_study(args.route_counts, n)


if __name__ == "__main__":
    main()
