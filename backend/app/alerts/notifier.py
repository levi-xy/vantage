"""Where alert texts go (M7.1 defines the seam; M7.2 provides the real sender)."""

from typing import Protocol


class Notifier(Protocol):
    async def send(self, channel_ids: list[int], text: str) -> None:
        """Deliver ``text`` to the channels. ``text`` never contains secrets."""
        ...


class NullNotifier:
    """Does nothing. The default until channels exist."""

    async def send(self, channel_ids: list[int], text: str) -> None:
        return None
