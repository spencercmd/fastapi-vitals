"""HTTP metrics middleware and scrape helpers."""

from __future__ import annotations

import logging
import os
import time
from typing import Awaitable, Callable, Optional, Tuple

from fastapi import FastAPI, Request
from prometheus_client import REGISTRY, CollectorRegistry, multiprocess
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

logger = logging.getLogger(__name__)

CallNext = Callable[[Request], Awaitable[Response]]

# Empty OpenMetrics body for fail-soft multiproc misconfiguration.
_EMPTY_OPENMETRICS = b"# EOF\n"


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


def _multiproc_dir() -> Optional[Tuple[str, str]]:
    """Return ``(env_key, path)`` when a multiproc env key is present, else ``None``.

    Presence matches prometheus_client (``in os.environ``), not truthiness of
    the value: empty/whitespace still means multiproc mode is on and scrape
    must not fall back to ``REGISTRY``. Uppercase preferred; deprecated
    lowercase accepted. Returns the raw env string (may be ``""`` or
    whitespace-only) so ``isdir`` / ``MultiProcessCollector`` see the same
    path as prometheus_client writers — do not ``.strip()`` contentful values.
    ``env_key`` is the name actually present (for warnings). Does not freeze —
    scrape may re-read env; request middleware stays untouched.
    """
    if "PROMETHEUS_MULTIPROC_DIR" in os.environ:
        return ("PROMETHEUS_MULTIPROC_DIR", os.environ["PROMETHEUS_MULTIPROC_DIR"])
    if "prometheus_multiproc_dir" in os.environ:
        return ("prometheus_multiproc_dir", os.environ["prometheus_multiproc_dir"])
    return None


def mark_process_dead(pid: Optional[int] = None) -> None:
    """Remove live* gauge mmap files for a dead worker.

    Call from Gunicorn ``worker_exit`` (or equivalent) so in-flight gauges do
    not linger after a worker dies. Defaults to the current PID. Fail-open:
    errors are logged and swallowed so an exit hook cannot break shutdown.
    No-ops when the multiproc env key is absent, blank, or not a directory
    (upstream would ``TypeError`` on unset or glob the CWD on ``\"\"``).
    """
    multiproc = _multiproc_dir()
    if multiproc is None:
        return
    _, multiproc_path = multiproc
    if not multiproc_path.strip() or not os.path.isdir(multiproc_path):
        return
    try:
        multiprocess.mark_process_dead(
            os.getpid() if pid is None else pid, path=multiproc_path
        )
    except Exception:
        logger.warning("mark_process_dead failed; continuing", exc_info=True)


def metrics_response() -> Response:
    if not metrics_enabled():
        return Response(status_code=404)
    # OpenMetrics is required for exemplars (classic Prometheus text drops them).
    # Under multiproc, exemplars are a no-op in client_python mmap values; the
    # scrape still uses OpenMetrics so single-process and multiproc share a
    # Content-Type and framing.
    multiproc = _multiproc_dir()
    if multiproc is not None:
        env_key, multiproc_path = multiproc
        # Blank/whitespace → empty scrape; otherwise use the raw env string for
        # isdir and MultiProcessCollector (writers read the same unstripped value).
        if not multiproc_path.strip() or not os.path.isdir(multiproc_path):
            logger.warning(
                "%s=%r is not a directory; returning empty OpenMetrics scrape",
                env_key,
                multiproc_path,
            )
            return Response(
                _EMPTY_OPENMETRICS, media_type=OPENMETRICS_CONTENT_TYPE
            )
        try:
            registry = CollectorRegistry()
            multiprocess.MultiProcessCollector(registry, path=multiproc_path)
            return Response(
                generate_latest_openmetrics(registry),
                media_type=OPENMETRICS_CONTENT_TYPE,
            )
        except Exception:
            logger.warning(
                "multiproc metrics scrape failed; returning empty OpenMetrics",
                exc_info=True,
            )
            return Response(
                _EMPTY_OPENMETRICS, media_type=OPENMETRICS_CONTENT_TYPE
            )
    return Response(
        generate_latest_openmetrics(REGISTRY), media_type=OPENMETRICS_CONTENT_TYPE
    )
