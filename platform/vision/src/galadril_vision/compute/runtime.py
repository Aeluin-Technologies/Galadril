"""Sync bridges for Daft UDFs that need to run async coroutines."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable
from typing import TypeVar

_T = TypeVar("_T")


def run_blocking[T](coro: Awaitable[_T]) -> _T:
    """Bridge a coroutine back into the sync Daft UDF call site."""
    return asyncio.run(coro)
