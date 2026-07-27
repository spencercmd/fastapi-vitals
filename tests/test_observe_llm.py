"""observe_llm: duration, tokens, status, finish_reason, spans, exemplars, async."""

from __future__ import annotations

import asyncio
from typing import Optional

import pytest
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import StatusCode

from fastapi_observability import metrics as m
from metrics_helpers import _finished_spans, _scrape


def _finished_llm_spans(exporter: InMemorySpanExporter):
    return _finished_spans(exporter, name_prefix="llm ")


def _llm_token_value(*, provider, model, operation, token_type) -> Optional[float]:
    for metric in m.LLM_TOKENS.collect():
        for sample in metric.samples:
            if (
                sample.name == "llm_tokens_total"
                and sample.labels.get("provider") == provider
                and sample.labels.get("model") == model
                and sample.labels.get("operation") == operation
                and sample.labels.get("token_type") == token_type
            ):
                return sample.value
    return None


def test_observe_llm_records_duration_and_tokens(monkeypatch, memory_tracer):
    monkeypatch.setenv("SERVICE", "test-svc")
    with m.observe_llm("openai", "gpt-4o-mini", "chat") as obs:
        obs.set_result(
            input_tokens=100,
            output_tokens=40,
            finish_reason="stop",
        )
    body = _scrape()
    assert "llm_request_duration_seconds" in body
    assert 'provider="openai"' in body
    assert 'model="gpt-4o-mini"' in body
    assert 'operation="chat"' in body
    assert 'status="ok"' in body
    assert 'finish_reason="stop"' in body
    assert _llm_token_value(
        provider="openai",
        model="gpt-4o-mini",
        operation="chat",
        token_type="input",
    ) == 100.0
    assert _llm_token_value(
        provider="openai",
        model="gpt-4o-mini",
        operation="chat",
        token_type="output",
    ) == 40.0


def test_observe_llm_maps_unknown_finish_reason_to_other(monkeypatch, memory_tracer):
    """Free-form finish_reason collapses to finish_reason=other (cardinality bound)."""
    monkeypatch.setenv("SERVICE", "test-svc")
    with m.observe_llm("openai", "gpt-4o", "finish_allowlist") as obs:
        obs.set_result(finish_reason="model_custom_stop_xyz")
    body = _scrape()
    assert any(
        line.startswith("llm_request_duration_seconds_count")
        and 'operation="finish_allowlist"' in line
        and 'finish_reason="other"' in line
        for line in body.splitlines()
    )


def test_observe_llm_set_status_error_without_raise_marks_span_error(
    monkeypatch, memory_tracer
):
    """Non-ok status without exception still sets span status ERROR (invariant)."""
    exporter, _provider = memory_tracer
    monkeypatch.setenv("SERVICE", "test-svc")
    with m.observe_llm("openai", "gpt-4o", "status_no_raise") as obs:
        obs.set_status("error")
        obs.set_result(finish_reason="stop")

    body = _scrape()
    assert any(
        'operation="status_no_raise"' in line
        and 'status="error"' in line
        and "llm_request_duration_seconds_count" in line
        for line in body.splitlines()
    )
    spans = _finished_llm_spans(exporter)
    assert len(spans) == 1
    assert spans[0].status.status_code == StatusCode.ERROR


def test_observe_llm_omitted_set_result_uses_unknown_finish_reason(
    monkeypatch, memory_tracer
):
    monkeypatch.setenv("SERVICE", "test-svc")
    with m.observe_llm("transformers", "local-classifier", "predict"):
        pass
    body = _scrape()
    assert 'provider="transformers"' in body
    assert 'model="local-classifier"' in body
    assert 'finish_reason="unknown"' in body
    assert 'status="ok"' in body
    assert (
        _llm_token_value(
            provider="transformers",
            model="local-classifier",
            operation="predict",
            token_type="input",
        )
        is None
    )


def test_observe_llm_zero_tokens_do_not_increment_counter(monkeypatch, memory_tracer):
    monkeypatch.setenv("SERVICE", "test-svc")
    with m.observe_llm("openai", "gpt-test", "noop") as obs:
        obs.set_result(input_tokens=0, output_tokens=0, finish_reason="stop")
    assert (
        _llm_token_value(
            provider="openai",
            model="gpt-test",
            operation="noop",
            token_type="input",
        )
        is None
    )
    assert (
        _llm_token_value(
            provider="openai",
            model="gpt-test",
            operation="noop",
            token_type="output",
        )
        is None
    )


def test_observe_llm_error_status_on_exception(monkeypatch, memory_tracer):
    exporter, _provider = memory_tracer
    monkeypatch.setenv("SERVICE", "test-svc")
    with pytest.raises(RuntimeError, match="boom"):
        with m.observe_llm("openai", "gpt-4o", "chat_error_exc"):
            raise RuntimeError("boom")

    body = _scrape()
    assert any(
        'operation="chat_error_exc"' in line
        and 'status="error"' in line
        and 'finish_reason="unknown"' in line
        and "llm_request_duration_seconds_count" in line
        for line in body.splitlines()
    )

    spans = _finished_llm_spans(exporter)
    assert len(spans) == 1
    span = spans[0]
    assert span.name == "llm openai"
    assert span.status.status_code == StatusCode.ERROR
    assert any(e.name == "exception" for e in span.events)


def test_observe_llm_rate_limited_status_preserved_on_raise(monkeypatch, memory_tracer):
    exporter, _provider = memory_tracer
    monkeypatch.setenv("SERVICE", "test-svc")
    with pytest.raises(RuntimeError, match="429"):
        with m.observe_llm("openai", "gpt-4o", "chat_rate_limited") as obs:
            obs.set_status("rate_limited")
            raise RuntimeError("429")

    body = _scrape()
    assert any(
        'operation="chat_rate_limited"' in line
        and 'status="rate_limited"' in line
        and "llm_request_duration_seconds_count" in line
        for line in body.splitlines()
    )

    spans = _finished_llm_spans(exporter)
    assert len(spans) == 1
    assert spans[0].status.status_code == StatusCode.ERROR
    assert spans[0].attributes["status"] == "rate_limited"


def test_observe_llm_unknown_status_coerces_to_error(monkeypatch, memory_tracer):
    monkeypatch.setenv("SERVICE", "test-svc")
    with m.observe_llm("openai", "gpt-4o", "chat_bad_status") as obs:
        obs.set_status("not-a-real-status")
        obs.set_result(finish_reason="stop")
    body = _scrape()
    assert any(
        'status="error"' in line
        and "llm_request_duration_seconds_count" in line
        and 'operation="chat_bad_status"' in line
        for line in body.splitlines()
    )


def test_observe_llm_emits_child_span_with_attributes(memory_tracer):
    """Span uses Prom-aligned attrs + token usage only (no llm.* / gen_ai name triples)."""
    exporter, provider = memory_tracer
    tracer = provider.get_tracer("test")
    with tracer.start_as_current_span("http_request") as parent:
        parent_ctx = parent.get_span_context()
        with m.observe_llm("openai", "gpt-4o-mini", "chat") as obs:
            obs.set_result(
                input_tokens=12,
                output_tokens=8,
                finish_reason="stop",
            )

    spans = _finished_llm_spans(exporter)
    assert len(spans) == 1
    span = spans[0]
    attrs = dict(span.attributes or {})
    assert span.name == "llm openai"
    assert attrs["peer.service"] == "openai"
    assert attrs["provider"] == "openai"
    assert attrs["model"] == "gpt-4o-mini"
    assert attrs["operation"] == "chat"
    assert attrs["status"] == "ok"
    assert attrs["finish_reason"] == "stop"
    assert attrs["gen_ai.usage.input_tokens"] == 12
    assert attrs["gen_ai.usage.output_tokens"] == 8
    # Single naming scheme — no triple mirrors.
    assert "llm.provider" not in attrs
    assert "llm.model" not in attrs
    assert "llm.operation" not in attrs
    assert "gen_ai.system" not in attrs
    assert "gen_ai.request.model" not in attrs
    assert "gen_ai.operation.name" not in attrs
    assert "gen_ai.response.finish_reasons" not in attrs
    assert span.status.status_code == StatusCode.UNSET
    assert span.parent is not None
    assert span.parent.span_id == parent_ctx.span_id


def test_observe_llm_exemplar_when_span_active(monkeypatch, memory_tracer):
    exporter, _provider = memory_tracer
    monkeypatch.setenv("SERVICE", "test-svc")
    with m.observe_llm("openai", "gpt-4o", "chat_exemplar") as obs:
        obs.set_result(finish_reason="stop")
    body = _scrape()
    spans = _finished_llm_spans(exporter)
    assert len(spans) == 1
    expected_trace = f"{spans[0].context.trace_id:032x}"
    # Registry is process-global; match this observation's operation + trace.
    bucket_lines = [
        line
        for line in body.splitlines()
        if line.startswith("llm_request_duration_seconds_bucket")
        and 'operation="chat_exemplar"' in line
        and f'trace_id="{expected_trace}"' in line
    ]
    assert bucket_lines, "expected OpenMetrics exemplar on llm histogram"
    assert "span_id=" in bucket_lines[0]


def test_aobserve_llm_records_metric_and_span(memory_tracer):
    exporter, _provider = memory_tracer

    async def _run():
        async with m.observe_llm("openai", "gpt-4o", "async_chat") as obs:
            obs.set_result(input_tokens=5, output_tokens=3, finish_reason="stop")

    asyncio.run(_run())

    body = _scrape()
    assert 'operation="async_chat"' in body
    assert 'status="ok"' in body
    assert _llm_token_value(
        provider="openai",
        model="gpt-4o",
        operation="async_chat",
        token_type="input",
    ) == 5.0

    spans = _finished_llm_spans(exporter)
    assert len(spans) == 1
    assert spans[0].name == "llm openai"
    assert spans[0].attributes["operation"] == "async_chat"


def test_aobserve_llm_records_error_on_exception(memory_tracer):
    exporter, _provider = memory_tracer

    async def _run():
        async with m.observe_llm("openai", "gpt-4o", "async_fail"):
            raise ValueError("llm down")

    with pytest.raises(ValueError, match="llm down"):
        asyncio.run(_run())

    body = _scrape()
    assert 'operation="async_fail"' in body
    assert 'status="error"' in body

    spans = _finished_llm_spans(exporter)
    assert len(spans) == 1
    assert spans[0].status.status_code == StatusCode.ERROR
    assert any(e.name == "exception" for e in spans[0].events)
