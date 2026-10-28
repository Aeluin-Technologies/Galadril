"""Serialize native inference and keep only one heavyweight model resident."""

import gc
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from threading import Condition
from time import monotonic

import structlog


@dataclass(slots=True, eq=False)
class _Waiting:
    owner: object
    interactive: bool


class ModelResidency:
    __slots__ = (
        "_condition",
        "_owner",
        "_release",
        "_closed",
        "_waiting",
        "_busy",
        "_burst",
        "_finished",
    )

    def __init__(self) -> None:
        self._condition = Condition()
        self._owner: object | None = None
        self._release: Callable[[], None] | None = None
        self._closed = False
        self._waiting: list[_Waiting] = []
        self._busy = False
        self._burst = 0
        self._finished = 0.0

    def _next(self) -> _Waiting | None:
        interactive = next(
            (item for item in self._waiting if item.interactive), None
        )
        if interactive is not None:
            return interactive
        if self._burst < 4:
            same = next(
                (item for item in self._waiting if item.owner is self._owner),
                None,
            )
            if same is not None:
                return same
        else:
            other = next(
                (
                    item
                    for item in self._waiting
                    if item.owner is not self._owner
                ),
                None,
            )
            if other is not None:
                return other
        return next(iter(self._waiting), None)

    @contextmanager
    def use(
        self,
        owner: object,
        release: Callable[[], None],
        *,
        interactive: bool = False,
    ) -> Iterator[None]:
        """Favor short same-model bursts while letting interactive work preempt between calls."""
        ticket = _Waiting(owner, interactive)
        with self._condition:
            if self._closed:
                raise RuntimeError("Native model residency is closed")
            self._waiting.append(ticket)
            self._condition.notify_all()
            while True:
                if self._closed:
                    self._waiting.remove(ticket)
                    raise RuntimeError("Native model residency is closed")
                if not self._busy and self._next() is ticket:
                    # A brief handoff window lets the next page/vector reuse weights
                    # without delaying an already queued interactive request.
                    delay = self._finished + 0.005 - monotonic()
                    if (
                        not interactive
                        and owner is not self._owner
                        and self._owner is not None
                        and self._burst < 4
                        and delay > 0
                    ):
                        self._condition.wait(delay)
                        continue
                    self._waiting.remove(ticket)
                    self._busy = True
                    break
                self._condition.wait()
        try:
            if owner is not self._owner:
                if self._release is not None:
                    self._release()
                    gc.collect()
                self._owner = owner
                self._release = release
                self._burst = 0
                structlog.get_logger(__name__).info(
                    "models.residency.changed", model=type(owner).__name__
                )
            self._burst += 1
            yield
        finally:
            with self._condition:
                self._busy = False
                self._finished = monotonic()
                self._condition.notify_all()

    def close(self) -> None:
        with self._condition:
            while self._busy:
                self._condition.wait()
            self._closed = True
            try:
                if self._release is not None:
                    self._release()
            finally:
                self._owner = None
                self._release = None
                self._condition.notify_all()
