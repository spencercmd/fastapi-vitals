"""HTTP RED middleware, exclusions, OpenMetrics scrape, in-flight, route templates."""

from __future__ import annotations

import threading

from fastapi import APIRouter, FastAPI
from fastapi.testclient import TestClient
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor

from fastapi_vitals import metrics as m
from metrics_helpers import _app, _in_flight_value


def test_metrics_enabled_default(monkeypatch):
    monkeypatch.delenv("METRICS_ENABLED", raising=False)
    assert m.metrics_enabled() is True


def test_metrics_disabled_by_env(monkeypatch):
    monkeypatch.setenv("METRICS_ENABLED", "false")
    assert m.metrics_enabled() is False


def test_metrics_enabled_cache_freezes_until_reset(monkeypatch):
    """METRICS_ENABLED freezes after first read; reset re-evaluates env."""
    from fastapi_vitals.metrics.middleware import reset_metrics_enabled

    monkeypatch.setenv("METRICS_ENABLED", "true")
    assert m.metrics_enabled() is True

    monkeypatch.setenv("METRICS_ENABLED", "false")
    assert m.metrics_enabled() is True  # frozen

    reset_metrics_enabled()
    assert m.metrics_enabled() is False


def test_metrics_response_is_openmetrics(monkeypatch):
    monkeypatch.setenv("METRICS_ENABLED", "true")
    resp = m.metrics_response()
    assert resp.status_code == 200
    assert "openmetrics-text" in resp.media_type
    assert "# EOF" in bytes(resp.body).decode()


def test_metrics_response_404_when_disabled(monkeypatch):
    monkeypatch.setenv("METRICS_ENABLED", "false")
    assert m.metrics_response().status_code == 404


def test_red_metrics_use_identity_labels_and_route_template(monkeypatch):
    monkeypatch.setenv("SERVICE", "test-svc")
    monkeypatch.setenv("METRICS_ENABLED", "true")
    client = TestClient(_app())
    client.get("/ping")
    body = client.get("/metrics").text
    assert 'service="test-svc"' in body
    assert 'route="/ping"' in body
    assert 'status_class="2xx"' in body
    assert "http_request_duration_seconds_bucket" in body


def test_metrics_scrape_does_not_self_count(monkeypatch):
    monkeypatch.setenv("METRICS_ENABLED", "true")
    client = TestClient(_app())
    body = client.get("/metrics").text
    assert 'route="/metrics"' not in body


def test_health_probes_are_excluded_from_red_metrics(monkeypatch):
    monkeypatch.setenv("METRICS_ENABLED", "true")
    client = TestClient(_app())
    for path in ("/health", "/health/live", "/health/ready"):
        assert client.get(path).status_code == 200
    body = client.get("/metrics").text
    assert 'route="/health"' not in body
    assert 'route="/health/live"' not in body
    assert 'route="/health/ready"' not in body


def test_http_histogram_exemplar_when_span_active(monkeypatch, memory_tracer):
    exporter, provider = memory_tracer
    monkeypatch.setenv("SERVICE", "test-svc")
    monkeypatch.setenv("METRICS_ENABLED", "true")
    # Instrument with our provider so request spans attach via contextvars;
    # _exemplar_labels reads get_current_span() without needing set_tracer_provider.
    app = _app()
    FastAPIInstrumentor.instrument_app(app, tracer_provider=provider)
    try:
        client = TestClient(app)
        assert client.get("/ping").status_code == 200
        body = client.get("/metrics").text
    finally:
        FastAPIInstrumentor.uninstrument_app(app)

    server_spans = [
        s
        for s in exporter.get_finished_spans()
        if s.attributes and s.attributes.get("http.route") == "/ping"
    ] or [
        s
        for s in exporter.get_finished_spans()
        if "/ping" in (s.name or "")
    ]
    assert server_spans, "expected a finished HTTP server span for /ping"
    expected_trace = f"{server_spans[0].context.trace_id:032x}"

    bucket_lines = [
        line
        for line in body.splitlines()
        if line.startswith("http_request_duration_seconds_bucket")
        and 'route="/ping"' in line
        and f'trace_id="{expected_trace}"' in line
    ]
    assert bucket_lines, (
        "expected OpenMetrics exemplar on http_request_duration_seconds "
        f"with trace_id={expected_trace}"
    )
    assert "span_id=" in bucket_lines[0]
    assert "# {" in bucket_lines[0]


def test_in_flight_returns_to_zero_with_expected_labels(monkeypatch):
    monkeypatch.setenv("SERVICE", "test-svc")
    monkeypatch.setenv("ENV", "dev")
    monkeypatch.setenv("METRICS_ENABLED", "true")
    client = TestClient(_app())
    client.get("/ping")
    assert _in_flight_value(route="/ping") == 0.0
    # Process-global registry may retain older series; match this request's labels.
    matched = [
        s
        for metric in m.HTTP_REQUESTS_IN_FLIGHT.collect()
        for s in metric.samples
        if s.name == "http_requests_in_flight"
        and s.labels.get("service") == "test-svc"
        and s.labels.get("env") == "dev"
        and s.labels.get("route") == "/ping"
        and s.labels.get("method") == "GET"
    ]
    assert matched and matched[0].value == 0.0
    assert "status_class" not in matched[0].labels
    assert "version" not in matched[0].labels


def test_in_flight_uses_route_template(monkeypatch):
    monkeypatch.setenv("SERVICE", "test-svc")
    monkeypatch.setenv("METRICS_ENABLED", "true")
    client = TestClient(_app())
    client.get("/items/42")
    assert _in_flight_value(route="/items/{item_id}") == 0.0
    assert _in_flight_value(route="/items/42") is None


def test_red_and_in_flight_share_route_snapshot(monkeypatch):
    """Middleware snapshots route once so RED and in-flight use the same template."""
    monkeypatch.setenv("SERVICE", "snap-svc")
    monkeypatch.setenv("METRICS_ENABLED", "true")
    client = TestClient(_app())
    client.get("/items/7")
    body = client.get("/metrics").text
    red = [
        line
        for line in body.splitlines()
        if line.startswith("http_requests_total{")
        and 'service="snap-svc"' in line
        and 'route="/items/{item_id}"' in line
    ]
    inflight = [
        line
        for line in body.splitlines()
        if line.startswith("http_requests_in_flight{")
        and 'service="snap-svc"' in line
        and 'route="/items/{item_id}"' in line
    ]
    assert red and inflight
    assert not any('route="/items/7"' in line for line in red + inflight)


def test_unmatched_route_label(monkeypatch):
    """Paths with no matching route still record under route=unmatched."""
    monkeypatch.setenv("SERVICE", "unmatch-svc")
    monkeypatch.setenv("METRICS_ENABLED", "true")
    app = FastAPI()
    m.setup_metrics(app)

    @app.get("/metrics")
    def metrics_ep():
        return m.metrics_response()

    client = TestClient(app)
    assert client.get("/does-not-exist").status_code == 404
    body = client.get("/metrics").text
    assert any(
        line.startswith("http_requests_total{")
        and 'service="unmatch-svc"' in line
        and 'route="unmatched"' in line
        and 'status_class="4xx"' in line
        for line in body.splitlines()
    )
    assert _in_flight_value(route="unmatched") == 0.0


def test_in_flight_include_router_uses_prefixed_template(monkeypatch):
    """include_router prefixes must appear on both in-flight and RED series."""
    monkeypatch.setenv("SERVICE", "test-svc")
    monkeypatch.setenv("METRICS_ENABLED", "true")
    app = FastAPI()
    m.setup_metrics(app)
    sub = APIRouter()

    @sub.get("/nested/{x}")
    def nested(x: int):
        return {"x": x}

    app.include_router(sub, prefix="/api")

    deep = APIRouter()

    @deep.get("/leaf/{id}")
    def leaf(id: int):
        return {"id": id}

    mid = APIRouter()
    mid.include_router(deep, prefix="/v1")
    app.include_router(mid, prefix="/svc")

    @app.get("/metrics")
    def metrics_ep():
        return m.metrics_response()

    client = TestClient(app)
    assert client.get("/api/nested/2").status_code == 200
    assert client.get("/svc/v1/leaf/9").status_code == 200

    assert _in_flight_value(route="/api/nested/{x}") == 0.0
    assert _in_flight_value(route="/svc/v1/leaf/{id}") == 0.0
    # Not the un-prefixed APIRoute.path that scope["route"] alone would give.
    assert _in_flight_value(route="/nested/{x}") is None
    assert _in_flight_value(route="/leaf/{id}") is None

    body = client.get("/metrics").text
    assert 'route="/api/nested/{x}"' in body
    assert 'route="/svc/v1/leaf/{id}"' in body
    assert "http_requests_total" in body


def test_in_flight_excludes_health_and_metrics(monkeypatch):
    monkeypatch.setenv("METRICS_ENABLED", "true")
    client = TestClient(_app())
    for path in ("/health", "/health/live", "/health/ready", "/metrics"):
        assert client.get(path).status_code in (200, 404)
    assert _in_flight_value(route="/health") is None
    assert _in_flight_value(route="/health/live") is None
    assert _in_flight_value(route="/health/ready") is None
    assert _in_flight_value(route="/metrics") is None


def test_in_flight_holds_while_request_running(monkeypatch):
    """Gauge is 1 while the handler blocks, 0 after it finishes.

    Own TestClient per thread — shared TestClient is not concurrent-safe.
    ``entered`` is set inside the handler, so middleware has already incremented.
    """
    monkeypatch.setenv("SERVICE", "test-svc")
    monkeypatch.setenv("METRICS_ENABLED", "true")
    hold_event = threading.Event()
    entered = threading.Event()
    app = FastAPI()
    m.setup_metrics(app)

    @app.get("/hold")
    def hold():
        entered.set()
        hold_event.wait(timeout=5)
        return {"ok": True}

    errors = []

    def _request():
        try:
            TestClient(app).get("/hold")
        except Exception as exc:  # pragma: no cover
            errors.append(exc)

    thread = threading.Thread(target=_request)
    thread.start()
    assert entered.wait(timeout=2.0), "handler never entered"
    assert _in_flight_value(route="/hold") == 1.0
    hold_event.set()
    thread.join(timeout=5)
    assert not errors
    assert _in_flight_value(route="/hold") == 0.0


def test_in_flight_decrements_on_exception(monkeypatch):
    monkeypatch.setenv("SERVICE", "test-svc")
    monkeypatch.setenv("METRICS_ENABLED", "true")
    client = TestClient(_app(fail=True), raise_server_exceptions=False)
    resp = client.get("/boom")
    assert resp.status_code == 500
    assert _in_flight_value(route="/boom") == 0.0


def test_mount_route_template_for_red_and_in_flight(monkeypatch):
    """Starlette Mount paths resolve to mount-prefix + child template."""
    monkeypatch.setenv("SERVICE", "mount-svc")
    monkeypatch.setenv("METRICS_ENABLED", "true")

    sub = FastAPI()

    @sub.get("/items/{item_id}")
    def get_item(item_id: int):
        return {"id": item_id}

    app = FastAPI()
    m.setup_metrics(app)
    app.mount("/v1", sub)

    @app.get("/metrics")
    def metrics_ep():
        return m.metrics_response()

    client = TestClient(app)
    assert client.get("/v1/items/42").status_code == 200

    body = client.get("/metrics").text
    assert 'service="mount-svc"' in body
    assert 'route="/v1/items/{item_id}"' in body
    assert 'route="/v1/items/42"' not in body
    assert 'route="/v1/{path}"' not in body
    assert _in_flight_value(route="/v1/items/{item_id}") == 0.0


def test_setup_metrics_installs_route_template_cache():
    """setup_metrics attaches a bounded route-template cache (idempotent)."""
    app = FastAPI()
    assert not hasattr(app.state, "route_template_cache")
    m.setup_metrics(app)
    cache = app.state.route_template_cache
    assert cache is not None
    m.setup_metrics(app)
    assert app.state.route_template_cache is cache


def test_repeated_static_and_template_routes_keep_labels(monkeypatch):
    """Cache hits must not corrupt low-cardinality route labels."""
    monkeypatch.setenv("SERVICE", "cache-svc")
    monkeypatch.setenv("METRICS_ENABLED", "true")
    client = TestClient(_app())
    for _ in range(3):
        assert client.get("/ping").status_code == 200
        assert client.get("/items/1").status_code == 200
        assert client.get("/items/99").status_code == 200

    body = client.get("/metrics").text
    assert 'service="cache-svc"' in body
    assert 'route="/ping"' in body
    assert 'route="/items/{item_id}"' in body
    assert 'route="/items/1"' not in body
    assert 'route="/items/99"' not in body


def test_route_template_cache_memoizes_successful_walk(monkeypatch):
    """Successful (path, method) → template is stored; unmatched is not."""
    from fastapi_vitals.metrics.route_templates import RouteTemplateCache

    monkeypatch.setenv("SERVICE", "memo-svc")
    monkeypatch.setenv("METRICS_ENABLED", "true")
    app = _app()
    cache = app.state.route_template_cache
    assert isinstance(cache, RouteTemplateCache)

    client = TestClient(app)
    assert client.get("/ping").status_code == 200
    assert cache.get(("/ping", "GET")) == "/ping"

    assert client.get("/does-not-exist").status_code == 404
    assert cache.get(("/does-not-exist", "GET")) is None


def test_negative_cache_memoizes_unmatched_when_opted_in(monkeypatch):
    """cache_unmatched=True stores unmatched after routes are stable."""
    from fastapi_vitals.metrics.route_templates import RouteTemplateCache

    monkeypatch.setenv("SERVICE", "neg-cache-svc")
    monkeypatch.setenv("METRICS_ENABLED", "true")

    app = FastAPI()
    m.setup_metrics(app, cache_unmatched=True)
    cache = app.state.route_template_cache
    assert isinstance(cache, RouteTemplateCache)
    assert cache.cache_unmatched is True

    @app.get("/ping")
    def ping():
        return {"ok": True}

    @app.get("/metrics")
    def metrics_ep():
        return m.metrics_response()

    client = TestClient(app)
    assert client.get("/missing").status_code == 404
    assert cache.get(("/missing", "GET")) == "unmatched"

    # Second hit must still label unmatched (cache hit path).
    assert client.get("/missing").status_code == 404
    body = client.get("/metrics").text
    assert 'route="unmatched"' in body
    assert 'service="neg-cache-svc"' in body


def test_default_setup_metrics_does_not_cache_unmatched(monkeypatch):
    """Default remains safe for late route registration (no sticky misses)."""
    monkeypatch.setenv("METRICS_ENABLED", "true")
    app = _app()
    assert app.state.route_template_cache.cache_unmatched is False


def test_method_mismatch_uses_path_template(monkeypatch):
    """PARTIAL method mismatch (405) labels with the path-matched template."""
    monkeypatch.setenv("SERVICE", "method-svc")
    monkeypatch.setenv("METRICS_ENABLED", "true")
    app = FastAPI()
    m.setup_metrics(app)

    @app.get("/only-get")
    def only_get():
        return {"ok": True}

    @app.get("/metrics")
    def metrics_ep():
        return m.metrics_response()

    client = TestClient(app)
    assert client.post("/only-get").status_code == 405
    assert client.get("/only-get").status_code == 200

    body = client.get("/metrics").text
    assert any(
        line.startswith("http_requests_total{")
        and 'service="method-svc"' in line
        and 'route="/only-get"' in line
        and 'method="POST"' in line
        and 'status_class="4xx"' in line
        for line in body.splitlines()
    )
    assert _in_flight_value(route="/only-get", method="POST") == 0.0
    cache = app.state.route_template_cache
    assert cache.get(("/only-get", "POST")) == "/only-get"
    assert cache.get(("/only-get", "GET")) == "/only-get"


def test_public_metrics_api_reexports():
    """Package split must keep stable ``from fastapi_vitals.metrics import …``."""
    from fastapi_vitals.metrics import (
        DEPENDENCY_REQUEST_DURATION,
        EXCLUDED_PATHS,
        HTTP_REQUEST_DURATION,
        HTTP_REQUESTS,
        HTTP_REQUESTS_IN_FLIGHT,
        IN_FLIGHT_LABELS,
        LLM_REQUEST_DURATION,
        LLM_TOKENS,
        RED_LABELS,
        identity_labels,
        mark_process_dead,
        metrics_enabled,
        metrics_middleware,
        metrics_response,
        observe_dependency,
        observe_llm,
        setup_metrics,
    )

    assert callable(identity_labels)
    assert callable(setup_metrics)
    assert callable(metrics_response)
    assert callable(mark_process_dead)
    assert callable(metrics_enabled)
    assert callable(metrics_middleware)
    assert observe_dependency is m.observe_dependency
    assert observe_llm is m.observe_llm
    assert HTTP_REQUESTS is m.HTTP_REQUESTS
    assert HTTP_REQUEST_DURATION is m.HTTP_REQUEST_DURATION
    assert HTTP_REQUESTS_IN_FLIGHT is m.HTTP_REQUESTS_IN_FLIGHT
    assert DEPENDENCY_REQUEST_DURATION is m.DEPENDENCY_REQUEST_DURATION
    assert LLM_REQUEST_DURATION is m.LLM_REQUEST_DURATION
    assert LLM_TOKENS is m.LLM_TOKENS
    assert RED_LABELS == m.RED_LABELS
    assert IN_FLIGHT_LABELS == m.IN_FLIGHT_LABELS
    assert EXCLUDED_PATHS == m.EXCLUDED_PATHS
