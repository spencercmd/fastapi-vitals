"""Route-template resolution and bounded LRU cache for RED / in-flight labels.

Middleware runs outside the FastAPI router, so ``scope[\"route\"]`` is unset
at enter. This module walks the app route table (flat routes,
``include_router`` / ``effective_candidates``, Mounts) to recover a low-
cardinality path template for Prometheus labels.
"""

from __future__ import annotations

import logging
from collections import OrderedDict
from typing import (
    Any,
    Callable,
    Mapping,
    MutableMapping,
    Optional,
    Sequence,
    Tuple,
    cast,
)

from fastapi import Request
from starlette.routing import Match, Mount

logger = logging.getLogger(__name__)

# Bounded concrete-URL → template memo (paths are high-cardinality; templates low).
_ROUTE_TEMPLATE_CACHE_MAX = 1024
_UNMATCHED = "unmatched"

# Duck-typed Starlette ``route.matches(scope)`` return shape.
_MatchesFn = Callable[
    [Mapping[str, Any]],
    Tuple[Match, Optional[MutableMapping[str, Any]]],
]


class RouteTemplateCache:
    """Bounded LRU of ``(path, method) → route template`` for hot paths.

    By default only successful walk results are stored (never ``unmatched``)
    so late route registration cannot stick a permanent miss. Set
    ``cache_unmatched=True`` only after routes are stable (opt-in negative
    cache) to skip repeated full walks for known 404s.

    Negative-cache policy lives in ``put``: callers always ``put`` the
    resolved label; ``unmatched`` is a no-op unless opted in.

    Concrete URLs are the keys; values remain low-cardinality Prometheus labels.
    """

    __slots__ = ("_data", "_maxsize", "cache_unmatched")

    def __init__(
        self,
        maxsize: int = _ROUTE_TEMPLATE_CACHE_MAX,
        *,
        cache_unmatched: bool = False,
    ) -> None:
        self._data: "OrderedDict[Tuple[str, str], str]" = OrderedDict()
        self._maxsize = maxsize
        self.cache_unmatched = cache_unmatched

    def get(self, key: Tuple[str, str]) -> Optional[str]:
        try:
            value = self._data[key]
        except KeyError:
            return None
        self._data.move_to_end(key)
        return value

    def put(self, key: Tuple[str, str], value: str) -> None:
        """Store ``value`` for ``key``, respecting negative-cache policy."""
        if value == _UNMATCHED and not self.cache_unmatched:
            return
        if key in self._data:
            self._data.move_to_end(key)
        self._data[key] = value
        while len(self._data) > self._maxsize:
            self._data.popitem(last=False)

    def clear(self) -> None:
        """Drop all entries (tests / micro-benches that need a cold cache)."""
        self._data.clear()


def _route_path_attr(route: object) -> Optional[str]:
    """Prefer full template (``path_format``) then Starlette ``path``."""
    path = getattr(route, "path_format", None) or getattr(route, "path", None)
    return path if isinstance(path, str) else None


def _as_route_sequence(value: object) -> Optional[Sequence[object]]:
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        return value
    return None


def _child_routes(route: object) -> Optional[Sequence[object]]:
    """Nested routes on a Mount / sub-app, if any."""
    nested = _as_route_sequence(getattr(route, "routes", None))
    if nested is not None:
        return nested
    app = getattr(route, "app", None)
    if app is None:
        return None
    return _as_route_sequence(getattr(app, "routes", None))


def _walk_children(
    route: object,
    scope: Mapping[str, Any],
    child_scope: Optional[MutableMapping[str, Any]],
) -> Optional[str]:
    """Merge match child_scope into a walk scope and recurse into nested routes."""
    child_routes = _child_routes(route)
    if not child_routes:
        return None
    walk_scope: dict = dict(scope)
    if child_scope:
        walk_scope.update(child_scope)
    return match_route_template(child_routes, walk_scope)


def _with_mount_prefix(route: object, found: str) -> str:
    """Prefix a child template with the Mount path when not already included."""
    if not isinstance(route, Mount):
        return found
    prefix = (getattr(route, "path", None) or "").rstrip("/")
    if prefix and found != prefix and not found.startswith(prefix + "/"):
        return f"{prefix}{found}" if found.startswith("/") else f"{prefix}/{found}"
    return found


def _on_full_match(
    route: object,
    scope: Mapping[str, Any],
    child_scope: Optional[MutableMapping[str, Any]],
) -> Optional[str]:
    """Handle ``Match.FULL``: walk children, then leaf path; Mount prefix if needed."""
    found = _walk_children(route, scope, child_scope)
    if found is not None:
        return _with_mount_prefix(route, found)
    return _route_path_attr(route)


def match_route_template(
    routes: Sequence[object], scope: Mapping[str, Any]
) -> Optional[str]:
    """Walk app routes (including FastAPI ``include_router`` / Mount) for a template.

    Middleware runs outside the router, so ``scope[\"route\"]`` is unset at enter.
    FastAPI's ``_IncludedRouter`` returns Match.FULL with no ``path``; its
    ``effective_candidates()`` carry the prefixed ``path_format`` we need.

    Mount matches return ``Match.FULL`` with a partial child scope (``root_path``
    updated). Merge that scope before walking children so ``get_route_path`` /
    child ``matches`` see the remaining path; prefix child templates with the
    mount path for stable low-cardinality labels.

    ``Match.FULL``: walk children, then leaf path. ``Match.PARTIAL`` (path
    matched, method did not — 405 on leaf routes): take the Starlette path
    template without inventing one. Mounts on this stack return FULL for path
    prefix matches; nested 405s still get mount prefixes when the parent FULL
    branch stitches a child PARTIAL result. ``Match.NONE`` continues the walk.

    The route table is heterogeneous; both ``effective_candidates`` and
    ``matches`` are discovered via duck typing (``getattr`` + ``callable``).
    """
    for route in routes:
        # FastAPI included routers: prefer effective candidates (full prefix).
        effective = getattr(route, "effective_candidates", None)
        if callable(effective):
            candidates = effective()
            if isinstance(candidates, Sequence) and not isinstance(
                candidates, (str, bytes)
            ):
                found = match_route_template(candidates, scope)
                if found is not None:
                    return found
            continue

        matches = getattr(route, "matches", None)
        if not callable(matches):
            continue
        try:
            match, child_scope = cast(_MatchesFn, matches)(scope)
        except Exception:
            # Fail-open: broken custom routes become ``unmatched`` rather than
            # breaking the request path. Log at debug so silent label pollution
            # is diagnosable without warn noise on hot paths.
            logger.debug(
                "route.matches failed for %s; continuing walk",
                type(route).__name__,
                exc_info=True,
            )
            continue

        if match == Match.FULL:
            found = _on_full_match(route, scope, child_scope)
            if found is not None:
                return found
        elif match == Match.PARTIAL:
            # Leaf only — Mounts on this stack return FULL for path prefixes.
            found = _route_path_attr(route)
            if found is not None:
                return found
    return None


def route_template(request: Request) -> str:
    """Low-cardinality route template for RED and in-flight labels.

    Always resolves before ``call_next`` by walking the app route table (flat
    routes, ``include_router`` prefixes, Mounts). Results are memoized on
    ``app.state`` when ``setup_metrics`` installed a cache. ``put`` enforces
    negative-cache policy (``unmatched`` stored only when
    ``cache_unmatched=True``).
    """
    path = request.scope.get("path")
    method = request.scope.get("method") or request.method
    raw_cache = getattr(request.app.state, "route_template_cache", None)
    cache = raw_cache if isinstance(raw_cache, RouteTemplateCache) else None
    cache_key = (
        (path, method)
        if cache is not None and isinstance(path, str) and isinstance(method, str)
        else None
    )
    if cache is not None and cache_key is not None:
        hit = cache.get(cache_key)
        if hit is not None:
            return hit

    found = match_route_template(request.app.routes, request.scope)
    result = found if found is not None else _UNMATCHED
    if cache is not None and cache_key is not None:
        cache.put(cache_key, result)
    return result
