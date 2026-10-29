"""Live pushes to connected browsers (``WS /api/v1/ws``, M5.3).

The poll scheduler publishes small JSON messages here; every open socket has its own
bounded queue. A client that doesn't keep up is dropped rather than letting its queue
grow. Messages are server-to-client only and never carry config or secrets: just the
instance id and the same ``status`` object the REST API returns.

Message types (``type``):

* ``instance_status``: ``{instance_id, status: {state, checked_at, last_success_at, error}}``
* ``refresh``: ``{instance_id}``, sent after each successful poll so cards refetch.
* ``alert_event``: reserved for M6; nothing sends it yet.
"""

import asyncio
import uuid
from typing import Any

from app.collectors.store import InstanceStatus

QUEUE_SIZE = 64
MAX_CONNECTIONS = 200

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
    def __init__(self) -> None:
        self.queue: asyncio.Queue[Message] = asyncio.Queue(maxsize=QUEUE_SIZE)
        # Set when the queue overflowed; the socket handler then closes the connection.
        self.dropped = asyncio.Event()


class EventHub:
    def __init__(self) -> None:
        self._subscribers: set[Subscriber] = set()

    def subscribe(self) -> Subscriber | None:
        """A new subscriber, or None when too many sockets are open."""
        if len(self._subscribers) >= MAX_CONNECTIONS:
            return None
        sub = Subscriber()
        self._subscribers.add(sub)
        return sub

    def unsubscribe(self, sub: Subscriber) -> None:
        self._subscribers.discard(sub)

    def publish(self, message: Message) -> None:
        for sub in list(self._subscribers):
            try:
                sub.queue.put_nowait(message)
            except asyncio.QueueFull:
                self._subscribers.discard(sub)
                sub.dropped.set()
