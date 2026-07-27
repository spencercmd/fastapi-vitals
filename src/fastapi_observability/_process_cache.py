"""Process-local freeze of a loader result until ``reset``.

Used for identity labels, metrics_enabled, and metrics tracer so production
env/provider values are not re-read on every hot-path call, while tests can
clear the freeze after monkeypatching.
"""

from __future__ import annotations

from typing import Callable, Generic, TypeVar, cast

T = TypeVar("T")

_UNSET = object()


class ProcessCache(Generic[T]):
    """Cache the first ``loader()`` result for the process lifetime.

    Distinguishes unset from valid cached ``None`` / ``False`` via an internal
    sentinel so boolean and optional loaders stay correct.
    """

    __slots__ = ("_loader", "_value")

    def __init__(self, loader: Callable[[], T]) -> None:
        self._loader = loader
        self._value: object = _UNSET

    def get(self) -> T:
        if self._value is _UNSET:
            self._value = self._loader()
        return cast(T, self._value)

    def reset(self) -> None:
        """Drop the cached value so the next ``get`` re-runs the loader."""
        self._value = _UNSET
