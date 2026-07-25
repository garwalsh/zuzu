"""Realtime event fan-out to the dashboard.

Spec: prompts/event_bus_Python.prompt

Publishing is fire-and-forget: the voice call is the product and the dashboard
is an observer, so a stalled browser tab must never add latency to a live call.
"""

from __future__ import annotations

import asyncio
import os
from collections import deque
from collections.abc import AsyncIterator
from typing import Protocol

from api.contract import SessionEvent

QUEUE_MAXSIZE = 256
REPLAY_MAXLEN = 50


class EventBus(Protocol):
    async def publish(self, event: SessionEvent) -> None: ...

    def subscribe(self, session_id: str) -> AsyncIterator[SessionEvent]: ...


class InMemoryEventBus:
    """Per-session asyncio queues, with a short replay buffer.

    The replay buffer exists so a dashboard opened mid-call still renders the
    fields already collected instead of showing an empty form.
    """

    def __init__(self) -> None:
        self._subscribers: dict[str, set[asyncio.Queue[SessionEvent]]] = {}
        self._replay: dict[str, deque[SessionEvent]] = {}

    async def publish(self, event: SessionEvent) -> None:
        self._replay.setdefault(event.session_id, deque(maxlen=REPLAY_MAXLEN)).append(event)
        for queue in list(self._subscribers.get(event.session_id, ())):
            try:
                queue.put_nowait(event)
            except asyncio.QueueFull:
                # Drop for this subscriber only. A dead consumer must not be
                # able to slow down, or fail, the applicant's call.
                continue

    async def subscribe(self, session_id: str) -> AsyncIterator[SessionEvent]:
        queue: asyncio.Queue[SessionEvent] = asyncio.Queue(maxsize=QUEUE_MAXSIZE)
        self._subscribers.setdefault(session_id, set()).add(queue)
        try:
            for event in list(self._replay.get(session_id, ())):
                yield event
            while True:
                yield await queue.get()
        finally:
            listeners = self._subscribers.get(session_id)
            if listeners is not None:
                listeners.discard(queue)
                if not listeners:
                    self._subscribers.pop(session_id, None)

    def subscriber_count(self, session_id: str) -> int:
        return len(self._subscribers.get(session_id, ()))


_bus: EventBus | None = None


def get_event_bus() -> EventBus:
    global _bus
    if _bus is None:
        backend = os.environ.get("STORE_BACKEND", "memory").strip().lower()
        if backend == "redis":
            raise NotImplementedError(
                "STORE_BACKEND=redis is Milestone 6 (Layer 2); Redis pub/sub is not wired yet."
            )
        _bus = InMemoryEventBus()
    return _bus


def reset_event_bus() -> None:
    """Drop the singleton. For tests only."""
    global _bus
    _bus = None
