"""Shared dual sync/async context-manager shell for nested @contextmanager bodies."""

from __future__ import annotations

from contextlib import AbstractContextManager
from types import TracebackType
from typing import Generic, Optional, Type, TypeVar

T = TypeVar("T")


class DualContext(Generic[T]):
    """Nested ``@contextmanager`` body + sync/async enter/exit.

    Subclasses implement ``_body`` as a ``@contextmanager`` generator yielding
    ``T``. Callers may use either ``with`` or ``async with``; async enter/exit
    delegate to the sync path (body work is sync timing + span annotation).
    """

    _cm: Optional[AbstractContextManager[T]] = None

    def _body(self) -> AbstractContextManager[T]:  # pragma: no cover
        raise NotImplementedError

    def __enter__(self) -> T:
        self._cm = self._body()
        return self._cm.__enter__()

    def __exit__(
        self,
        exc_type: Optional[Type[BaseException]],
        exc: Optional[BaseException],
        tb: Optional[TracebackType],
    ) -> bool:
        if self._cm is None:
            return False
        return bool(self._cm.__exit__(exc_type, exc, tb))

    async def __aenter__(self) -> T:
        return self.__enter__()

    async def __aexit__(
        self,
        exc_type: Optional[Type[BaseException]],
        exc: Optional[BaseException],
        tb: Optional[TracebackType],
    ) -> bool:
        return self.__exit__(exc_type, exc, tb)
