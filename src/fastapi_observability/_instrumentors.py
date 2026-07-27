"""Optional OTEL client auto-instrumentation lifecycle.

Requested via ``setup_tracing(instrument=...)`` or ``OTEL_INSTRUMENTATIONS``.
Missing extras and per-instrumentor failures are soft: warn and continue.

``sqlalchemy`` / ``redis`` are best-effort process-global ``instrument()``
only — no engine or client binding. Prefer ``observe_dependency`` for
critical SQL/Redis paths.
"""

from __future__ import annotations

import importlib
import logging
import os
from typing import Any, List, Optional, Protocol, Sequence

from opentelemetry.sdk.trace import TracerProvider

logger = logging.getLogger(__name__)

# Private import map — not part of the public API.
_INSTRUMENTOR_SPECS = {
    "httpx": ("opentelemetry.instrumentation.httpx", "HTTPXClientInstrumentor"),
    "requests": ("opentelemetry.instrumentation.requests", "RequestsInstrumentor"),
    "sqlalchemy": (
        "opentelemetry.instrumentation.sqlalchemy",
        "SQLAlchemyInstrumentor",
    ),
    "redis": ("opentelemetry.instrumentation.redis", "RedisInstrumentor"),
}

# Public names accepted by setup_tracing(instrument=...) and OTEL_INSTRUMENTATIONS.
SUPPORTED_INSTRUMENTATIONS = frozenset(_INSTRUMENTOR_SPECS)


class _Instrumentor(Protocol):
    def instrument(self, **kwargs: Any) -> None: ...

    def uninstrument(self, **kwargs: Any) -> None: ...


_active: List[_Instrumentor] = []


def resolve_names(instrument: Optional[Sequence[str]]) -> List[str]:
    """Normalize, lower-case, and dedupe requested instrumentation names.

    Explicit ``instrument=`` wins over ``OTEL_INSTRUMENTATIONS`` env.
    When ``instrument`` is None, parse the env var (comma-separated).
    Order is preserved; duplicates after normalize are dropped.
    """
    if instrument is not None:
        raw_parts = [name for name in instrument if name and str(name).strip()]
    else:
        env = os.getenv("OTEL_INSTRUMENTATIONS", "")
        if not env.strip():
            return []
        raw_parts = env.split(",")

    normalized = [part.strip().lower() for part in raw_parts if part and part.strip()]
    return list(dict.fromkeys(normalized))


def _load(name: str) -> Optional[_Instrumentor]:
    spec = _INSTRUMENTOR_SPECS.get(name)
    if spec is None:
        logger.warning("Unknown OTEL instrumentation %r; skipping", name)
        return None
    module_name, class_name = spec
    try:
        module = importlib.import_module(module_name)
        cls = getattr(module, class_name)
        instance: _Instrumentor = cls()
        return instance
    except (ImportError, AttributeError) as exc:
        logger.warning(
            "OTEL instrumentation %r unavailable (%s); "
            "install fastapi-observability[%s] (optional extra) to enable",
            name,
            exc,
            name,
        )
        return None


def apply(
    provider: TracerProvider, instrument: Optional[Sequence[str]] = None
) -> None:
    """Instrument requested clients; soft-fail each name independently."""
    global _active
    for name in resolve_names(instrument):
        instrumentor = _load(name)
        if instrumentor is None:
            continue
        try:
            instrumentor.instrument(tracer_provider=provider)
        except Exception:
            logger.exception(
                "OTEL instrumentation %r failed to apply; skipping", name
            )
            continue
        _active.append(instrumentor)


def shutdown() -> None:
    """Uninstrument every client applied by this process, best-effort."""
    global _active
    for instrumentor in _active:
        try:
            instrumentor.uninstrument()
        except Exception:  # pragma: no cover - defensive cleanup
            logger.exception("Failed to uninstrument %s", instrumentor)
    _active = []
