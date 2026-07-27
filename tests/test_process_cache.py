"""ProcessCache: process-local freeze of a loader result until reset."""

from __future__ import annotations

from fastapi_observability._process_cache import ProcessCache


def test_process_cache_loads_once_and_freezes():
    calls = {"n": 0}

    def loader() -> str:
        calls["n"] += 1
        return f"value-{calls['n']}"

    cache = ProcessCache(loader)
    assert cache.get() == "value-1"
    assert cache.get() == "value-1"
    assert calls["n"] == 1


def test_process_cache_reset_reloads():
    calls = {"n": 0}

    def loader() -> int:
        calls["n"] += 1
        return calls["n"]

    cache = ProcessCache(loader)
    assert cache.get() == 1
    cache.reset()
    assert cache.get() == 2
    assert calls["n"] == 2


def test_process_cache_freezes_false_and_none():
    """False and None are valid cached values (not confused with unset)."""
    cache_false = ProcessCache(lambda: False)
    assert cache_false.get() is False
    assert cache_false.get() is False

    cache_none = ProcessCache(lambda: None)
    assert cache_none.get() is None
    assert cache_none.get() is None
