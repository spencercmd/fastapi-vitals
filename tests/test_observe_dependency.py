"""observe_dependency: histogram, child spans, exemplars, async, DualContext parity."""

from __future__ import annotations

import asyncio

import pytest
from opentelemetry.trace import StatusCode

from fastapi_vitals import metrics as m
from metrics_helpers import _finished_spans, _scrape


def test_observe_dependency_records_a_sample(monkeypatch, memory_tracer):
    monkeypatch.setenv("SERVICE", "test-svc")
    with m.observe_dependency("openai", "chat"):
        pass
    body = _scrape()
    assert "dependency_request_duration_seconds" in body
    assert 'dependency="openai"' in body
    assert 'operation="chat"' in body
    assert 'status="ok"' in body


def test_observe_dependency_exemplar_when_span_active(monkeypatch, memory_tracer):
    exporter, _provider = memory_tracer
    monkeypatch.setenv("SERVICE", "test-svc")
    with m.observe_dependency("openai", "chat"):
        pass
    body = _scrape()
    bucket_lines = [
        line
        for line in body.splitlines()
        if line.startswith("dependency_request_duration_seconds_bucket")
        and 'dependency="openai"' in line
        and "trace_id=" in line
    ]
    assert bucket_lines, "expected OpenMetrics exemplar on dependency histogram"
    assert "span_id=" in bucket_lines[0]

    spans = _finished_spans(exporter)
    assert len(spans) == 1
    expected_trace = f"{spans[0].context.trace_id:032x}"
    assert f'trace_id="{expected_trace}"' in bucket_lines[0]


def test_observe_dependency_emits_child_span_with_attributes(memory_tracer):
    exporter, provider = memory_tracer
    tracer = provider.get_tracer("test")
    with tracer.start_as_current_span("http_request") as parent:
        parent_ctx = parent.get_span_context()
        with m.observe_dependency("openai", "chat"):
            pass

    spans = _finished_spans(exporter)
    assert len(spans) == 1
    span = spans[0]
    assert span.name == "dependency openai"
    assert span.attributes["peer.service"] == "openai"
    assert span.attributes["dependency"] == "openai"
    assert span.attributes["operation"] == "chat"
    assert "rpc.method" not in (span.attributes or {})
    assert span.status.status_code == StatusCode.UNSET
    assert span.parent is not None
    assert span.parent.span_id == parent_ctx.span_id


def test_observe_dependency_records_error_status_on_exception(monkeypatch, memory_tracer):
    exporter, _provider = memory_tracer
    monkeypatch.setenv("SERVICE", "test-svc")
    with pytest.raises(RuntimeError, match="boom"):
        with m.observe_dependency("openai", "chat"):
            raise RuntimeError("boom")

    body = _scrape()
    assert 'dependency="openai"' in body
    assert 'status="error"' in body

    spans = _finished_spans(exporter)
    assert len(spans) == 1
    span = spans[0]
    assert span.status.status_code == StatusCode.ERROR
    assert any(e.name == "exception" for e in span.events)


def test_aobserve_dependency_records_metric_and_span(memory_tracer):
    exporter, _provider = memory_tracer

    async def _run():
        async with m.observe_dependency("sql", "query"):
            pass

    asyncio.run(_run())

    body = _scrape()
    assert 'dependency="sql"' in body
    assert 'operation="query"' in body
    assert 'status="ok"' in body

    spans = _finished_spans(exporter)
    assert len(spans) == 1
    assert spans[0].name == "dependency sql"
    assert spans[0].attributes["peer.service"] == "sql"
    assert spans[0].attributes["operation"] == "query"
    assert "rpc.method" not in (spans[0].attributes or {})


def test_aobserve_dependency_records_error_on_exception(memory_tracer):
    exporter, _provider = memory_tracer

    async def _run():
        async with m.observe_dependency("sql", "query"):
            raise ValueError("db down")

    with pytest.raises(ValueError, match="db down"):
        asyncio.run(_run())

    body = _scrape()
    assert 'dependency="sql"' in body
    assert 'status="error"' in body

    spans = _finished_spans(exporter)
    assert len(spans) == 1
    assert spans[0].status.status_code == StatusCode.ERROR
    assert any(e.name == "exception" for e in spans[0].events)


def test_observe_dependency_records_without_active_span(monkeypatch):
    """Histogram path works when exemplar labels are None (no active span)."""
    monkeypatch.setenv("SERVICE", "test-svc")
    with m.observe_dependency("http", "get"):
        pass
    body = _scrape()
    assert "dependency_request_duration_seconds" in body
    assert 'dependency="http"' in body
    assert 'operation="get"' in body
    assert 'status="ok"' in body


# --- DualContext / observer parity (shared shell used by both observers) ---


def test_dependency_and_llm_both_mark_span_error_and_exception_event(memory_tracer):
    """Parity seam: both observers surface ERROR + exception event on raise."""
    exporter, _provider = memory_tracer
    with pytest.raises(RuntimeError):
        with m.observe_dependency("sql", "parity_dep_err"):
            raise RuntimeError("dep fail")
    with pytest.raises(RuntimeError):
        with m.observe_llm("openai", "gpt-4o", "parity_llm_err"):
            raise RuntimeError("llm fail")

    dep = _finished_spans(exporter)
    llm = _finished_spans(exporter, name_prefix="llm ")
    assert len(dep) == 1 and len(llm) == 1
    for span in (dep[0], llm[0]):
        assert span.status.status_code == StatusCode.ERROR
        assert any(e.name == "exception" for e in span.events)


def test_dependency_and_llm_both_attach_exemplars(monkeypatch, memory_tracer):
    """Parity seam: both histograms carry OpenMetrics exemplars when span is active."""
    exporter, _provider = memory_tracer
    monkeypatch.setenv("SERVICE", "test-svc")
    with m.observe_dependency("sql", "parity_dep_ex"):
        pass
    with m.observe_llm("openai", "gpt-4o", "parity_llm_ex") as obs:
        obs.set_result(finish_reason="stop")

    body = _scrape()
    dep_spans = [
        s for s in exporter.get_finished_spans() if s.name == "dependency sql"
    ]
    llm_spans = [
        s for s in exporter.get_finished_spans() if s.name == "llm openai"
    ]
    assert len(dep_spans) == 1 and len(llm_spans) == 1
    dep_trace = f"{dep_spans[0].context.trace_id:032x}"
    llm_trace = f"{llm_spans[0].context.trace_id:032x}"
    assert any(
        line.startswith("dependency_request_duration_seconds_bucket")
        and 'operation="parity_dep_ex"' in line
        and f'trace_id="{dep_trace}"' in line
        for line in body.splitlines()
    )
    assert any(
        line.startswith("llm_request_duration_seconds_bucket")
        and 'operation="parity_llm_ex"' in line
        and f'trace_id="{llm_trace}"' in line
        for line in body.splitlines()
    )


def test_observe_exit_without_enter_is_safe():
    """Dual-CM shell must not assert if __exit__/__aexit__ run without enter."""
    dep = m.observe_dependency("sql", "no_enter")
    assert dep.__exit__(None, None, None) is False

    llm = m.observe_llm("openai", "gpt-4o", "no_enter")
    assert llm.__exit__(None, None, None) is False
