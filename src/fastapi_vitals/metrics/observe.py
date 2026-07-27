"""Dependency and LLM dual sync/async observers (Prometheus + OTEL child spans).

Class names are function-style (``observe_dependency``, ``observe_llm``) for
context-manager call-site UX (``with observe_dependency(...)``); this is not a
style regression and must not be CapWords-renamed (breaking public API).

Bodies intentionally specialize (LLM tokens / finish_reason / status
allowlists). Shared pieces: ``DualContext`` + ``_record_span_exception``. Do
not extract a generic observer until a third sibling appears; then prefer a
small ``_timed_span_body`` helper over a mega-template.
"""

from __future__ import annotations

import time
from contextlib import contextmanager
from typing import Iterator

from opentelemetry.trace import Span, Status, StatusCode

from fastapi_vitals._identity import identity_labels

from ._dual_context import DualContext
from ._exemplars import exemplar_labels, get_metrics_tracer
from .instruments import (
    DEPENDENCY_REQUEST_DURATION,
    LLM_FINISH_REASONS,
    LLM_REQUEST_DURATION,
    LLM_STATUS_VALUES,
    LLM_TOKENS,
)


def _record_span_exception(span: Span, exc: BaseException) -> None:
    """Mark span ERROR + record_exception when the span is recording."""
    if not span.is_recording():
        return
    span.record_exception(exc)
    span.set_status(Status(StatusCode.ERROR, str(exc)))


class observe_dependency(DualContext["observe_dependency"]):
    """Time an outbound dependency call (Prometheus + OTEL child span).

    Works with both ``with`` and ``async with`` so FastAPI services can wrap
    sync or async clients without a separate helper::

        with observe_dependency("openai", "chat"):
            ...

        async with observe_dependency("sql", "query"):
            ...

    Span attributes: ``peer.service``, plus ``dependency`` / ``operation``
    aligned with the histogram labels (not ``rpc.*`` — those names are too
    narrow for LLM/SQL/HTTP deps). Exceptions set span status ERROR and
    ``record_exception``; histogram ``status`` is ``ok`` or ``error``.

    **Ownership:** this helper owns explicit dependency **metrics** (primary)
    and an OTEL child span (secondary enrichment). Client auto-instrumentation
    lives in ``setup_tracing``. Prefer one or the other per library —
    combining both for the same call yields two child spans in APM.
    Prefer this helper for clients without an instrumentor (e.g. OpenAI) and
    for critical SQL/Redis; prefer instrumentors for httpx/requests blanket
    coverage.

    Histogram ``status`` remains ``ok`` / ``error`` today; richer values
    (timeout, http_429, …) can be added later without changing the signature.
    """

    def __init__(self, dependency: str, operation: str) -> None:
        self.dependency = dependency
        self.operation = operation

    @contextmanager
    def _body(self) -> Iterator["observe_dependency"]:
        started_at = time.perf_counter()
        status = "ok"
        with get_metrics_tracer().start_as_current_span(
            f"dependency {self.dependency}"
        ) as span:
            if span.is_recording():
                span.set_attribute("peer.service", self.dependency)
                # Align with Prometheus histogram dimensions (not rpc.* —
                # dependency/operation cover LLM, SQL, HTTP, etc.).
                span.set_attribute("dependency", self.dependency)
                span.set_attribute("operation", self.operation)
            try:
                yield self
            except Exception as exc:
                status = "error"
                _record_span_exception(span, exc)
                raise
            finally:
                # Still inside the child span — exemplar can read it directly.
                duration = time.perf_counter() - started_at
                labels = (
                    *identity_labels(),
                    self.dependency,
                    self.operation,
                    status,
                )
                DEPENDENCY_REQUEST_DURATION.labels(*labels).observe(
                    duration, exemplar=exemplar_labels()
                )


class observe_llm(DualContext["observe_llm"]):
    """Time an LLM / local-model call (Prometheus + OTEL child span).

    Additive helper for OpenAI-class and transformers call sites. Prefer this
    **instead of** nesting ``observe_dependency`` around the same call (double
    duration series). Non-LLM services can ignore it entirely.

    Works with both ``with`` and ``async with``::

        with observe_llm("openai", "gpt-4o-mini", "chat") as obs:
            response = client.chat.completions.create(...)
            usage = response.usage
            obs.set_result(
                input_tokens=usage.prompt_tokens if usage else 0,
                output_tokens=usage.completion_tokens if usage else 0,
                finish_reason=response.choices[0].finish_reason or "unknown",
            )

        with observe_llm("openai", model, "chat") as obs:
            try:
                ...
            except RateLimitError:
                obs.set_status("rate_limited")
                raise

    Metrics:

    * ``llm_request_duration_seconds`` — labels provider, model, operation,
      status (``ok`` / ``error`` / ``rate_limited``), finish_reason
    * ``llm_tokens_total`` — ``token_type`` is ``input`` or ``output`` (values
      on the counter, not as high-cardinality label values)

    Span name ``llm {provider}``; attributes are Prom-aligned
    (``provider`` / ``model`` / ``operation`` / ``status`` / ``finish_reason``)
    plus ``peer.service`` and optional ``gen_ai.usage.*_tokens``.
    """

    def __init__(self, provider: str, model: str, operation: str) -> None:
        self.provider = provider
        self.model = model
        self.operation = operation
        self._status = "ok"
        self._finish_reason = "unknown"
        self._input_tokens = 0
        self._output_tokens = 0

    def set_result(
        self,
        *,
        input_tokens: int = 0,
        output_tokens: int = 0,
        finish_reason: str = "unknown",
    ) -> None:
        """Record token usage and finish reason for exit-time metrics/span."""
        self._input_tokens = max(0, int(input_tokens or 0))
        self._output_tokens = max(0, int(output_tokens or 0))
        reason = (finish_reason or "").strip() if finish_reason is not None else ""
        reason = reason or "unknown"
        self._finish_reason = (
            reason if reason in LLM_FINISH_REASONS else "other"
        )

    def set_status(self, status: str) -> None:
        """Set histogram ``status`` (``ok``, ``error``, ``rate_limited``).

        Unknown values coerce to ``error`` (fail-open). Call before re-raise
        in ``except`` so rate limits are not collapsed into generic error.
        """
        if status in LLM_STATUS_VALUES:
            self._status = status
        else:
            self._status = "error"

    def _annotate_enter(self, span: Span) -> None:
        if not span.is_recording():
            return
        # Prom-aligned names only (+ peer.service for dependency-style filters).
        span.set_attribute("peer.service", self.provider)
        span.set_attribute("provider", self.provider)
        span.set_attribute("model", self.model)
        span.set_attribute("operation", self.operation)

    def _annotate_exit(self, span: Span) -> None:
        if not span.is_recording():
            return
        span.set_attribute("status", self._status)
        span.set_attribute("finish_reason", self._finish_reason)
        if self._input_tokens:
            span.set_attribute("gen_ai.usage.input_tokens", self._input_tokens)
        if self._output_tokens:
            span.set_attribute("gen_ai.usage.output_tokens", self._output_tokens)
        # Non-ok Prom status and span status stay aligned even without an exception.
        if self._status != "ok":
            span.set_status(Status(StatusCode.ERROR, self._status))

    def _record_metrics(self, duration: float) -> None:
        identity = identity_labels()
        labels = (
            *identity,
            self.provider,
            self.model,
            self.operation,
            self._status,
            self._finish_reason,
        )
        LLM_REQUEST_DURATION.labels(*labels).observe(
            duration, exemplar=exemplar_labels()
        )
        if self._input_tokens:
            LLM_TOKENS.labels(
                *identity, self.provider, self.model, self.operation, "input"
            ).inc(self._input_tokens)
        if self._output_tokens:
            LLM_TOKENS.labels(
                *identity, self.provider, self.model, self.operation, "output"
            ).inc(self._output_tokens)

    @contextmanager
    def _body(self) -> Iterator["observe_llm"]:
        started_at = time.perf_counter()
        with get_metrics_tracer().start_as_current_span(
            f"llm {self.provider}"
        ) as span:
            self._annotate_enter(span)
            try:
                yield self
            except Exception as exc:
                if self._status == "ok":
                    self._status = "error"
                _record_span_exception(span, exc)
                raise
            finally:
                self._annotate_exit(span)
                # Still inside the child span — exemplar can read it directly.
                self._record_metrics(time.perf_counter() - started_at)
