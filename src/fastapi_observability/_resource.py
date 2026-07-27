"""OTEL resource attribute assembly (service identity, process.runtime).

Optional platform enrichers (e.g. AWS ECS) live under
``fastapi_observability.adapters`` and are applied only when available —
core setup never requires cloud metadata.
"""

from __future__ import annotations

import logging
import os
import socket
import sys
from typing import Callable, Dict, List, Optional, Sequence

from ._identity import identity_labels

logger = logging.getLogger(__name__)

ResourceEnricher = Callable[[], Dict[str, str]]

# Optional enrichers registered at process start. Empty by default; ECS is
# auto-registered when its metadata URI env is present (see _default_enrichers).
_extra_enrichers: List[ResourceEnricher] = []


def process_runtime_attributes() -> Dict[str, str]:
    """Return process.runtime.* attributes (no pid/command paths)."""
    if sys.version_info.releaselevel == "final" and not sys.version_info.serial:
        runtime_version = ".".join(map(str, sys.version_info[:3]))
    else:
        runtime_version = ".".join(map(str, sys.version_info))
    return {
        "process.runtime.name": sys.implementation.name,
        "process.runtime.version": runtime_version,
        "process.runtime.description": sys.version,
    }


def service_instance_id(preferred: Optional[str] = None) -> Optional[str]:
    """Prefer an explicit id (e.g. from an adapter), then HOSTNAME, then gethostname()."""
    if preferred:
        return preferred
    host = (os.getenv("HOSTNAME") or "").strip()
    if host:
        return host
    try:
        host = socket.gethostname().strip()
    except OSError:
        return None
    return host or None


def register_resource_enricher(enricher: ResourceEnricher) -> None:
    """Register a custom resource enricher (tests / non-ECS platforms)."""
    _extra_enrichers.append(enricher)


def clear_resource_enrichers() -> None:
    """Drop manually registered enrichers (tests)."""
    _extra_enrichers.clear()


def _default_enrichers() -> List[ResourceEnricher]:
    """Lazy-load optional adapters that self-detect their runtime."""
    enrichers: List[ResourceEnricher] = list(_extra_enrichers)
    try:
        from fastapi_observability.adapters import ecs

        if ecs.is_available():
            enrichers.append(ecs.enrich)
    except Exception:  # pragma: no cover - defensive import boundary
        logger.debug("ECS adapter unavailable", exc_info=True)
    return enrichers


def build_resource_attributes(
    service_name: str,
    *,
    enrichers: Optional[Sequence[ResourceEnricher]] = None,
) -> Dict[str, str]:
    """Assemble OTEL resource attributes for this process.

    Always includes service identity + process.runtime.* and
    ``service.instance.id`` when known (HOSTNAME → hostname).

    Optional enrichers may add cloud.* attributes or override
    ``service.instance.id`` (e.g. ECS task id). Pass ``enrichers=()`` to
    disable auto-detection; omit for default adapters.

    This function never *writes* ``cloud.account.id``. Operator-supplied
    ``OTEL_RESOURCE_ATTRIBUTES`` is applied later by ``Resource.create``
    and is out of scope here.
    """
    # env/version via identity_labels so RED labels and OTEL resources cannot
    # drift; service.name stays caller-supplied (OTEL_SERVICE_NAME path).
    _, env, version = identity_labels()
    attrs: Dict[str, str] = {
        "service.name": service_name,
        "service.version": version,
        "deployment.environment": env,
    }
    attrs.update(process_runtime_attributes())

    preferred_instance: Optional[str] = None
    active = list(enrichers) if enrichers is not None else _default_enrichers()
    for enricher in active:
        try:
            extra = enricher() or {}
        except Exception:
            logger.debug("Resource enricher failed; continuing", exc_info=True)
            continue
        # Adapters may set service.instance.id; pull it aside so hostname
        # fallback still runs when they only set cloud.*.
        if "service.instance.id" in extra:
            preferred_instance = extra.pop("service.instance.id")
        attrs.update(extra)

    instance_id = service_instance_id(preferred_instance)
    if instance_id:
        attrs["service.instance.id"] = instance_id

    return attrs
