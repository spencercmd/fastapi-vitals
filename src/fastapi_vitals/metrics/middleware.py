"""HTTP metrics middleware and scrape helpers."""

from __future__ import annotations

import os
import time
from typing import Awaitable, Callable

from fastapi import FastAPI, Request
from prometheus_client import REGISTRY
from prometheus_client.openmetrics.exposition import (
    CONTENT_TYPE_LATEST as OPENMETRICS_CONTENT_TYPE,
)
from prometheus_client.openmetrics.exposition import (
    generate_latest as generate_latest_openmetrics,
)
from starlette.responses import Response

from fastapi_vitals._identity import identity_labels
from fastapi_vitals._process_cache import ProcessCache

from ._exemplars import exemplar_labels
from .instruments import (
    ENABLED_VALUES,
    EXCLUDED_PATHS,
    HTTP_REQUEST_DURATION,
    HTTP_REQUESTS,
    HTTP_REQUESTS_IN_FLIGHT,
)
from .route_templates import RouteTemplateCache, route_template

CallNext = Callable[[Request], Awaitable[Response]]


def _load_metrics_enabled() -> bool:
    return os.getenv("METRICS_ENABLED", "true").strip().lower() in ENABLED_VALUES


_metrics_enabled = ProcessCache(_load_metrics_enabled)


def metrics_enabled() -> bool:
    """Return whether RED/metrics scrape are enabled (``METRICS_ENABLED``).

    Freezes the env value after the first call for the process. Use
    ``reset_metrics_enabled()`` in tests after monkeypatching env.
    """
    return _metrics_enabled.get()


def reset_metrics_enabled() -> None:
    """Drop cached metrics-enabled flag (tests / rare reconfig)."""
    _metrics_enabled.reset()


def _status_class(status_code: int) -> str:
    return f"{status_code // 100}xx"


async def metrics_middleware(request: Request, call_next: CallNext) -> Response:
    if not metrics_enabled() or request.url.path in EXCLUDED_PATHS:
        return await call_next(request)

    # Snapshot identity + route once so in-flight and RED share labels and
    # gauge inc/dec cannot leak across label sets.
    service, env, version = identity_labels()
    method = request.method
    route = route_template(request)
    in_flight_labels = (service, env, method, route)
    HTTP_REQUESTS_IN_FLIGHT.labels(*in_flight_labels).inc()
    started_at = time.perf_counter()
    status_code = 500
    try:
        response = await call_next(request)
        status_code = response.status_code
        return response
    finally:
        HTTP_REQUESTS_IN_FLIGHT.labels(*in_flight_labels).dec()
        labels = (service, env, version, method, route, _status_class(status_code))
        HTTP_REQUESTS.labels(*labels).inc()
        HTTP_REQUEST_DURATION.labels(*labels).observe(
            time.perf_counter() - started_at,
            exemplar=exemplar_labels(),
        )


def setup_metrics(app: FastAPI, *, cache_unmatched: bool = False) -> None:
    """Install RED middleware and a bounded route-template cache.

    ``cache_unmatched``: when True, also memoize ``unmatched`` results (opt-in
    negative cache). Use only after routes are stable so late registration
    cannot stick permanent misses. Default False preserves prior behavior.
    """
    if getattr(app.state, "metrics_configured", False):
        return
    app.middleware("http")(metrics_middleware)
    app.state.route_template_cache = RouteTemplateCache(cache_unmatched=cache_unmatched)
    app.state.metrics_configured = True


def metrics_response() -> Response:
    if not metrics_enabled():
        return Response(status_code=404)
    # OpenMetrics is required for exemplars (classic Prometheus text drops them).
    return Response(
        generate_latest_openmetrics(REGISTRY), media_type=OPENMETRICS_CONTENT_TYPE
    )
