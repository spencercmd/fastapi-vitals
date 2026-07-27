"""Unit tests for RouteTemplateCache policy (negative cache + clear)."""

from __future__ import annotations

from fastapi_vitals.metrics.route_templates import RouteTemplateCache


def test_route_template_cache_put_policy_and_clear():
    """Negative cache is owned by put; clear drops all entries."""
    cache = RouteTemplateCache()
    cache.put(("/a", "GET"), "/a")
    cache.put(("/missing", "GET"), "unmatched")
    assert cache.get(("/a", "GET")) == "/a"
    assert cache.get(("/missing", "GET")) is None

    neg = RouteTemplateCache(cache_unmatched=True)
    neg.put(("/missing", "GET"), "unmatched")
    assert neg.get(("/missing", "GET")) == "unmatched"
    neg.clear()
    assert neg.get(("/missing", "GET")) is None
