"""Sync bridges for Daft UDFs that need to run async coroutines."""

from __future__ import annotations

import asyncio
from typing import Awaitable, TypeVar

_T = TypeVar("_T")


def run_blocking(coro: Awaitable[_T]) -> _T:
    """Bridge a coroutine back into the sync Daft UDF call site."""
    return asyncio.run(coro)
