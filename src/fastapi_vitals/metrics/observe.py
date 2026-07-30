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

import logging
import time
from contextlib import contextmanager
from typing import Iterator, Optional

from opentelemetry.trace import Span, SpanKind, Status, StatusCode

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

logger = logging.getLogger(__name__)


def _exception_type_name(exc: BaseException) -> str:
    """OTEL ``exception.type`` / ``error.type`` string for ``exc``.

    Matches ``Span.record_exception``: ``module.QualName``, except builtins
    which are unqualified ``QualName``.
    """
    module = type(exc).__module__
    qualname = type(exc).__qualname__
    if module and module != "builtins":
        return f"{module}.{qualname}"
    return qualname


def _record_span_exception(span: Span, exc: BaseException) -> bool:
    """Mark span ERROR + record_exception when the span is recording.

    Fail-open: each span API call is isolated so a ``record_exception``
    failure cannot skip ``set_status``, and neither can replace the
    caller's exception. Returns whether ``set_status`` succeeded (so
    callers can avoid overwriting ``str(exc)`` or retry a fallback).
    """
    if not span.is_recording():
        return False
    try:
        span.record_exception(exc)
    except Exception:
        logger.debug("span.record_exception failed; continuing", exc_info=True)
    try:
        span.set_status(Status(StatusCode.ERROR, str(exc)))
        return True
    except Exception:
        logger.debug("span.set_status failed; continuing", exc_info=True)
        return False


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
    ``BaseException`` subclasses (including ``asyncio.CancelledError``) are
    recorded the same way and re-raised. Exit telemetry is fail-open.

    **Ownership:** this helper owns explicit dependency **metrics** (primary)
    and an OTEL child span (secondary enrichment). Client auto-instrumentation
    lives in ``setup_tracing``. Prefer one or the other per library —
    combining both for the same call yields two child spans in APM.
    Prefer this helper for clients without an instrumentor (e.g. OpenAI) and
    for critical SQL/Redis; prefer instrumentors for httpx/requests blanket
    coverage.

    Histogram ``status`` remains ``ok`` / ``error`` today; richer values
    (timeout, http_429, …) can be added later without changing the signature.
    ``GeneratorExit`` is re-raised without marking an error or recording the
    histogram (abnormal CM close is not a dependency outcome).
    """

    def __init__(self, dependency: str, operation: str) -> None:
        super().__init__()
        self.dependency = dependency
        self.operation = operation

    @contextmanager
    def _body(self) -> Iterator["observe_dependency"]:
        started_at = time.perf_counter()
        status = "ok"
        emit_metrics = True
        # Disable SDK auto exception/status — _record_span_exception owns both
        # (avoids a duplicate exception event).
        with get_metrics_tracer().start_as_current_span(
            f"dependency {self.dependency}",
            record_exception=False,
            set_status_on_exception=False,
        ) as span:
            if span.is_recording():
                span.set_attribute("peer.service", self.dependency)
                # Align with Prometheus histogram dimensions (not rpc.* —
                # dependency/operation cover LLM, SQL, HTTP, etc.).
                span.set_attribute("dependency", self.dependency)
                span.set_attribute("operation", self.operation)
            try:
                yield self
            except GeneratorExit:
                # Abnormal CM/generator close — not a dependency failure.
                emit_metrics = False
                raise
            except BaseException as exc:
                status = "error"
                _record_span_exception(span, exc)
                raise
            finally:
                # Do not return from finally — that would discard GeneratorExit.
                if emit_metrics:
                    try:
                        # Still inside the child span — exemplar can read it.
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
                    except Exception:
                        logger.debug(
                            "observe_dependency exit telemetry failed; continuing",
                            exc_info=True,
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

    Child span follows OpenTelemetry GenAI inference conventions (Development
    stability): name ``{operation} {model}``, kind CLIENT, attributes
    ``gen_ai.provider.name``, ``gen_ai.request.model``,
    ``gen_ai.operation.name`` (set at span creation for attribute-based
    samplers), ``gen_ai.response.finish_reasons``, optional
    ``gen_ai.usage.*_tokens``, plus ``peer.service``. ``error.type`` is
    ``rate_limited`` when that Prom status was set, else the OpenTelemetry
    exception type string (``module.QualName``, builtins unqualified —
    same as ``exception.type`` on the event) when one was recorded, else
    the non-ok Prom status. Exception paths keep the status description
    from ``record_exception`` (``str(exc)``). ``BaseException`` subclasses
    (including ``asyncio.CancelledError``) are recorded the same way and
    re-raised; ``GeneratorExit`` is re-raised without marking an LLM error
    or recording the histogram (abnormal CM close is not an LLM outcome).
    Each ``with`` / ``async with`` enter resets status, finish_reason, tokens,
    and exception state — call ``set_status`` / ``set_result`` inside the block.
    Exit telemetry is fail-open: a metrics/span annotation failure is logged
    and does not suppress an in-flight exception or fail the call.
    """

    def __init__(self, provider: str, model: str, operation: str) -> None:
        super().__init__()
        self.provider = provider
        self.model = model
        self.operation = operation
        self._status = "ok"
        self._finish_reason = "unknown"
        self._input_tokens = 0
        self._output_tokens = 0
        self._exception_type: Optional[str] = None
        self._span_error_status_set = False

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

    def _annotate_exit(self, span: Span) -> None:
        if not span.is_recording():
            return
        span.set_attribute(
            "gen_ai.response.finish_reasons", (self._finish_reason,)
        )
        if self._input_tokens:
            span.set_attribute("gen_ai.usage.input_tokens", self._input_tokens)
        if self._output_tokens:
            span.set_attribute("gen_ai.usage.output_tokens", self._output_tokens)
        # Prefer Prom rate_limited for alerting parity; else exception class;
        # else non-ok Prom status. Never overwrite exception status description
        # (str(exc) from _record_span_exception).
        if self._status == "rate_limited":
            span.set_attribute("error.type", "rate_limited")
        elif self._exception_type is not None:
            span.set_attribute("error.type", self._exception_type)
        elif self._status != "ok":
            span.set_attribute("error.type", self._status)
        if (
            not self._span_error_status_set
            and (self._exception_type is not None or self._status != "ok")
        ):
            description = (
                self._status if self._status != "ok" else self._exception_type or "error"
            )
            span.set_status(Status(StatusCode.ERROR, description))

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
        # Reset so a reused instance cannot leak status/tokens/error.type.
        self._status = "ok"
        self._finish_reason = "unknown"
        self._input_tokens = 0
        self._output_tokens = 0
        self._exception_type = None
        self._span_error_status_set = False
        started_at = time.perf_counter()
        emit_metrics = True
        # Identity attrs at creation so attribute-based samplers can see them.
        # Disable SDK auto exception/status — _record_span_exception owns both
        # (keeps description as str(exc); avoids a duplicate exception event).
        with get_metrics_tracer().start_as_current_span(
            f"{self.operation} {self.model}",
            kind=SpanKind.CLIENT,
            attributes={
                "gen_ai.operation.name": self.operation,
                "gen_ai.provider.name": self.provider,
                "gen_ai.request.model": self.model,
                "peer.service": self.provider,
            },
            record_exception=False,
            set_status_on_exception=False,
        ) as span:
            try:
                yield self
            except GeneratorExit:
                # Abnormal CM/generator close, not an LLM failure — do not set
                # Prom error status, error.type, record_exception, or histogram.
                emit_metrics = False
                raise
            except BaseException as exc:
                if self._status == "ok":
                    self._status = "error"
                self._exception_type = _exception_type_name(exc)
                self._span_error_status_set = _record_span_exception(span, exc)
                raise
            finally:
                # Do not return from finally — that would discard GeneratorExit.
                # Annotate and metrics are isolated so one failure cannot skip
                # the other.
                if emit_metrics:
                    try:
                        self._annotate_exit(span)
                    except Exception:
                        logger.debug(
                            "observe_llm span exit annotation failed; continuing",
                            exc_info=True,
                        )
                    try:
                        # Still inside the child span — exemplar can read it.
                        self._record_metrics(time.perf_counter() - started_at)
                    except Exception:
                        logger.debug(
                            "observe_llm metrics exit failed; continuing",
                            exc_info=True,
                        )
