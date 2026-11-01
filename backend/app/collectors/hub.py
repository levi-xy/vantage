"""Live pushes to connected browsers (``WS /api/v1/ws``, M5.3).

The poll scheduler publishes small JSON messages here; every open socket has its own
bounded queue. A client that doesn't keep up is dropped rather than letting its queue
grow. Messages are server-to-client only and never carry config or secrets: just the
instance id and the same ``status`` object the REST API returns.

Message types (``type``):

* ``instance_status``: ``{instance_id, status: {state, checked_at, last_success_at, error}}``
* ``refresh``: ``{instance_id}``, sent after each successful poll so cards refetch.
* ``alert_event``: ``{event_id, rule_id, severity, state, started_at, ended_at, summary}``,
  when the alert evaluator (M7.1) fires or resolves a rule. Ids, times and a cleaned,
  capped summary only.
* ``live_rate``: ``{in_bps, out_bps, ports, at}``, the summed traffic of the dashboard
  throughput card's ports, every couple of seconds while a browser is connected
  (``app/live/rates.py``). Numbers only: no names, addresses or config.
"""

import asyncio
import uuid
from typing import Any

from app.collectors.store import InstanceStatus

QUEUE_SIZE = 64
MAX_CONNECTIONS = 200
# Sockets one login session may hold open (a few browser tabs); more is refused, so one
# user can't use up MAX_CONNECTIONS for everybody.
MAX_CONNECTIONS_PER_SESSION = 8

Message = dict[str, Any]


def status_message(instance_id: uuid.UUID, status: InstanceStatus) -> Message:
    return {
        "type": "instance_status",
        "instance_id": str(instance_id),
        "status": {
            "state": status.state,
            "checked_at": status.checked_at.isoformat() if status.checked_at else None,
            "last_success_at": (
                status.last_success_at.isoformat() if status.last_success_at else None
            ),
            "error": status.error,
        },
    }


def refresh_message(instance_id: uuid.UUID) -> Message:
    return {"type": "refresh", "instance_id": str(instance_id)}


class Subscriber:
    def __init__(self, key: str | None = None) -> None:
        # Who holds it (a session id), for the per-session cap; None = not counted.
        self.key = key
        self.queue: asyncio.Queue[Message] = asyncio.Queue(maxsize=QUEUE_SIZE)
        # Set when the queue overflowed; the socket handler then closes the connection.
        self.dropped = asyncio.Event()
        # Set once its per-session slot is given back (unsubscribe may run twice).
        self.released = False


class EventHub:
    def __init__(self) -> None:
        self._subscribers: set[Subscriber] = set()
        self._per_key: dict[str, int] = {}

    def subscribe(self, key: str | None = None) -> Subscriber | None:
        """A new subscriber, or None when too many sockets are open, in total or for
        ``key`` (the session id of the socket)."""
        if len(self._subscribers) >= MAX_CONNECTIONS:
            return None
        if key is not None:
            if self._per_key.get(key, 0) >= MAX_CONNECTIONS_PER_SESSION:
                return None
            self._per_key[key] = self._per_key.get(key, 0) + 1
        sub = Subscriber(key)
        self._subscribers.add(sub)
        return sub

    @property
    def subscriber_count(self) -> int:
        return len(self._subscribers)

    def unsubscribe(self, sub: Subscriber) -> None:
        self._subscribers.discard(sub)
        if sub.key is not None and not sub.released:
            sub.released = True
            left = self._per_key.get(sub.key, 0) - 1
            if left > 0:
                self._per_key[sub.key] = left
            else:
                self._per_key.pop(sub.key, None)

    def publish(self, message: Message) -> None:
        for sub in list(self._subscribers):
            try:
                sub.queue.put_nowait(message)
            except asyncio.QueueFull:
                self._subscribers.discard(sub)
                sub.dropped.set()
