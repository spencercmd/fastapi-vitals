"""observe_llm: duration, tokens, status, finish_reason, spans, exemplars, async."""

from __future__ import annotations

import asyncio
from typing import Optional

import pytest
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import SpanKind, StatusCode

from fastapi_vitals import metrics as m
from metrics_helpers import _scrape


def _finished_llm_spans(exporter: InMemorySpanExporter):
    """LLM child spans are named ``{operation} {model}`` (GenAI), not ``llm …``."""
    return [
        s
        for s in exporter.get_finished_spans()
        if (s.attributes or {}).get("gen_ai.provider.name") is not None
    ]


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
    assert spans[0].status.description == "error"
    assert spans[0].attributes["error.type"] == "error"


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


def _exception_events(span):
    return [e for e in span.events if e.name == "exception"]


class _CustomLlmError(Exception):
    """Non-builtin exception for error.type / exception.type alignment."""


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
    assert span.name == "chat_error_exc gpt-4o"
    assert span.kind == SpanKind.CLIENT
    assert span.status.status_code == StatusCode.ERROR
    assert span.status.description == "boom"
    assert span.attributes["error.type"] == "RuntimeError"
    # record_exception=False on start_as_current_span — we emit exactly one.
    events = _exception_events(span)
    assert len(events) == 1
    assert events[0].attributes["exception.type"] == "RuntimeError"
    assert span.attributes["error.type"] == events[0].attributes["exception.type"]


def test_observe_llm_error_type_matches_exception_type_for_non_builtin(
    monkeypatch, memory_tracer
):
    exporter, _provider = memory_tracer
    monkeypatch.setenv("SERVICE", "test-svc")
    with pytest.raises(_CustomLlmError, match="custom"):
        with m.observe_llm("openai", "gpt-4o", "chat_custom_exc"):
            raise _CustomLlmError("custom")

    spans = _finished_llm_spans(exporter)
    assert len(spans) == 1
    span = spans[0]
    events = _exception_events(span)
    assert len(events) == 1
    expected = f"{__name__}.{_CustomLlmError.__qualname__}"
    assert events[0].attributes["exception.type"] == expected
    assert span.attributes["error.type"] == expected
    assert span.attributes["error.type"] == events[0].attributes["exception.type"]


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
    span = spans[0]
    assert span.status.status_code == StatusCode.ERROR
    # Prom rate_limited wins for error.type; exception message kept on status.
    assert span.attributes["error.type"] == "rate_limited"
    assert span.status.description == "429"
    assert "status" not in (span.attributes or {})
    assert len(_exception_events(span)) == 1


def test_observe_llm_rate_limited_without_raise_marks_span_error(
    monkeypatch, memory_tracer
):
    exporter, _provider = memory_tracer
    monkeypatch.setenv("SERVICE", "test-svc")
    with m.observe_llm("openai", "gpt-4o", "rate_limited_no_raise") as obs:
        obs.set_status("rate_limited")
        obs.set_result(finish_reason="stop")

    body = _scrape()
    assert any(
        'operation="rate_limited_no_raise"' in line
        and 'status="rate_limited"' in line
        and "llm_request_duration_seconds_count" in line
        for line in body.splitlines()
    )
    spans = _finished_llm_spans(exporter)
    assert len(spans) == 1
    span = spans[0]
    assert span.status.status_code == StatusCode.ERROR
    assert span.status.description == "rate_limited"
    assert span.attributes["error.type"] == "rate_limited"
    assert _exception_events(span) == []


def test_observe_llm_reuse_clears_stale_error_state(monkeypatch, memory_tracer):
    """Re-entering the same instance must not leak prior error.type / status."""
    exporter, _provider = memory_tracer
    monkeypatch.setenv("SERVICE", "test-svc")
    obs = m.observe_llm("openai", "gpt-4o", "reuse_obs")

    with pytest.raises(RuntimeError, match="first"):
        with obs:
            raise RuntimeError("first")

    with obs as second:
        second.set_result(input_tokens=1, output_tokens=1, finish_reason="stop")

    body = _scrape()
    assert any(
        'operation="reuse_obs"' in line
        and 'status="ok"' in line
        and 'finish_reason="stop"' in line
        and "llm_request_duration_seconds_count" in line
        for line in body.splitlines()
    )
    spans = _finished_llm_spans(exporter)
    assert len(spans) == 2
    ok_span = spans[1]
    assert ok_span.status.status_code == StatusCode.UNSET
    assert "error.type" not in (ok_span.attributes or {})
    assert ok_span.attributes["gen_ai.usage.input_tokens"] == 1


def test_observe_llm_pre_enter_state_cleared_on_enter(monkeypatch, memory_tracer):
    """set_status / set_result before ``with`` must not survive enter reset."""
    monkeypatch.setenv("SERVICE", "test-svc")
    obs = m.observe_llm("openai", "gpt-4o", "pre_enter_reset")
    obs.set_status("rate_limited")
    obs.set_result(input_tokens=99, output_tokens=99, finish_reason="stop")

    with obs as entered:
        entered.set_result(input_tokens=1, output_tokens=2, finish_reason="length")

    body = _scrape()
    assert any(
        'operation="pre_enter_reset"' in line
        and 'status="ok"' in line
        and 'finish_reason="length"' in line
        and "llm_request_duration_seconds_count" in line
        for line in body.splitlines()
    )
    assert _llm_token_value(
        provider="openai",
        model="gpt-4o",
        operation="pre_enter_reset",
        token_type="input",
    ) == 1.0
    assert _llm_token_value(
        provider="openai",
        model="gpt-4o",
        operation="pre_enter_reset",
        token_type="output",
    ) == 2.0
    assert not any(
        'operation="pre_enter_reset"' in line and 'status="rate_limited"' in line
        for line in body.splitlines()
    )


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
    """Span uses GenAI inference attrs (no custom Prom-mirror / llm.* keys)."""
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
    assert span.name == "chat gpt-4o-mini"
    assert span.kind == SpanKind.CLIENT
    assert attrs["peer.service"] == "openai"
    assert attrs["gen_ai.provider.name"] == "openai"
    assert attrs["gen_ai.request.model"] == "gpt-4o-mini"
    assert attrs["gen_ai.operation.name"] == "chat"
    assert attrs["gen_ai.response.finish_reasons"] == ("stop",)
    assert attrs["gen_ai.usage.input_tokens"] == 12
    assert attrs["gen_ai.usage.output_tokens"] == 8
    # Custom / deprecated naming schemes must not appear.
    assert "provider" not in attrs
    assert "model" not in attrs
    assert "operation" not in attrs
    assert "status" not in attrs
    assert "finish_reason" not in attrs
    assert "llm.provider" not in attrs
    assert "llm.model" not in attrs
    assert "llm.operation" not in attrs
    assert "gen_ai.system" not in attrs
    assert "error.type" not in attrs
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
    assert spans[0].name == "async_chat gpt-4o"
    assert spans[0].kind == SpanKind.CLIENT
    assert spans[0].attributes["gen_ai.operation.name"] == "async_chat"


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
    span = spans[0]
    assert span.status.status_code == StatusCode.ERROR
    assert span.status.description == "llm down"
    assert span.attributes["error.type"] == "ValueError"
    assert len(_exception_events(span)) == 1


def test_aobserve_llm_rate_limited_status_preserved_on_raise(memory_tracer):
    exporter, _provider = memory_tracer

    async def _run():
        async with m.observe_llm("openai", "gpt-4o", "async_rate_limited") as obs:
            obs.set_status("rate_limited")
            raise RuntimeError("429")

    with pytest.raises(RuntimeError, match="429"):
        asyncio.run(_run())

    body = _scrape()
    assert any(
        'operation="async_rate_limited"' in line
        and 'status="rate_limited"' in line
        and "llm_request_duration_seconds_count" in line
        for line in body.splitlines()
    )
    spans = _finished_llm_spans(exporter)
    assert len(spans) == 1
    span = spans[0]
    assert span.status.status_code == StatusCode.ERROR
    assert span.attributes["error.type"] == "rate_limited"
    assert span.status.description == "429"
    assert len(_exception_events(span)) == 1


def test_aobserve_llm_records_cancelled_error(memory_tracer):
    """asyncio.CancelledError (BaseException) must mark Prom/span error."""
    exporter, _provider = memory_tracer

    async def _run():
        async with m.observe_llm("openai", "gpt-4o", "async_cancel"):
            raise asyncio.CancelledError()

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(_run())

    body = _scrape()
    assert any(
        'operation="async_cancel"' in line
        and 'status="error"' in line
        and "llm_request_duration_seconds_count" in line
        for line in body.splitlines()
    )
    spans = _finished_llm_spans(exporter)
    assert len(spans) == 1
    span = spans[0]
    assert span.status.status_code == StatusCode.ERROR
    events = _exception_events(span)
    assert len(events) == 1
    expected = "asyncio.exceptions.CancelledError"
    assert events[0].attributes["exception.type"] == expected
    assert span.attributes["error.type"] == expected


def test_aobserve_llm_rate_limited_preserved_on_cancelled_error(memory_tracer):
    """rate_limited + CancelledError: Prom status kept; span keeps str(exc)."""
    exporter, _provider = memory_tracer

    async def _run():
        async with m.observe_llm("openai", "gpt-4o", "async_rl_cancel") as obs:
            obs.set_status("rate_limited")
            raise asyncio.CancelledError("cancelled after 429")

    with pytest.raises(asyncio.CancelledError, match="cancelled after 429"):
        asyncio.run(_run())

    body = _scrape()
    assert any(
        'operation="async_rl_cancel"' in line
        and 'status="rate_limited"' in line
        and "llm_request_duration_seconds_count" in line
        for line in body.splitlines()
    )
    spans = _finished_llm_spans(exporter)
    assert len(spans) == 1
    span = spans[0]
    assert span.status.status_code == StatusCode.ERROR
    assert span.attributes["error.type"] == "rate_limited"
    assert span.status.description == "cancelled after 429"
    assert len(_exception_events(span)) == 1


def test_observe_llm_generator_exit_not_recorded_as_error(monkeypatch, memory_tracer):
    """GeneratorExit closes the CM without Prom/span LLM error or histogram."""
    exporter, _provider = memory_tracer
    monkeypatch.setenv("SERVICE", "test-svc")
    cm = m.observe_llm("openai", "gpt-4o", "gen_exit")
    cm.__enter__()
    assert cm.__exit__(GeneratorExit, GeneratorExit(), None) is False

    body = _scrape()
    assert not any(
        'operation="gen_exit"' in line
        and "llm_request_duration_seconds_count" in line
        for line in body.splitlines()
    )
    spans = _finished_llm_spans(exporter)
    assert len(spans) == 1
    span = spans[0]
    assert span.status.status_code == StatusCode.UNSET
    assert "error.type" not in (span.attributes or {})
    assert _exception_events(span) == []


def test_observe_llm_telemetry_failure_does_not_suppress_exception(
    monkeypatch, memory_tracer
):
    """Fail-open: metrics failure must not replace/suppress the call exception."""
    monkeypatch.setenv("SERVICE", "test-svc")

    def _boom(*_args, **_kwargs):
        raise RuntimeError("prom down")

    monkeypatch.setattr(m.LLM_REQUEST_DURATION, "labels", _boom)
    with pytest.raises(ValueError, match="llm boom"):
        with m.observe_llm("openai", "gpt-4o", "fail_open_exc"):
            raise ValueError("llm boom")


def test_observe_llm_telemetry_failure_on_success_is_swallowed(
    monkeypatch, memory_tracer
):
    """Fail-open: metrics failure must not fail a successful LLM call."""
    monkeypatch.setenv("SERVICE", "test-svc")

    def _boom(*_args, **_kwargs):
        raise RuntimeError("prom down")

    monkeypatch.setattr(m.LLM_REQUEST_DURATION, "labels", _boom)
    with m.observe_llm("openai", "gpt-4o", "fail_open_ok") as obs:
        obs.set_result(finish_reason="stop")


def test_observe_llm_record_exception_failure_still_sets_span_status(
    monkeypatch, memory_tracer
):
    """Fail-open: record_exception failure must not skip set_status or caller exc."""
    from opentelemetry.sdk.trace import Span as SdkSpan

    exporter, _provider = memory_tracer
    monkeypatch.setenv("SERVICE", "test-svc")

    def _boom(self, *_args, **_kwargs):
        raise RuntimeError("record_exception down")

    monkeypatch.setattr(SdkSpan, "record_exception", _boom)
    with pytest.raises(ValueError, match="still raised"):
        with m.observe_llm("openai", "gpt-4o", "record_exc_fail"):
            raise ValueError("still raised")

    spans = _finished_llm_spans(exporter)
    assert len(spans) == 1
    span = spans[0]
    assert span.status.status_code == StatusCode.ERROR
    assert span.status.description == "still raised"
    assert span.attributes["error.type"] == "ValueError"
    assert _exception_events(span) == []


def test_observe_llm_annotate_failure_still_records_metrics(
    monkeypatch, memory_tracer
):
    """Fail-open: span annotation failure must not skip the histogram."""
    monkeypatch.setenv("SERVICE", "test-svc")

    def _boom(self, _span):
        raise RuntimeError("annotate down")

    monkeypatch.setattr(m.observe_llm, "_annotate_exit", _boom)
    with m.observe_llm("openai", "gpt-4o", "annotate_fail_ok") as obs:
        obs.set_result(finish_reason="stop")

    body = _scrape()
    assert any(
        'operation="annotate_fail_ok"' in line
        and 'status="ok"' in line
        and "llm_request_duration_seconds_count" in line
        for line in body.splitlines()
    )
