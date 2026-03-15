from __future__ import annotations

from abc import ABC, abstractmethod
from collections import deque
from typing import Any


class StorageBackend(ABC):
    """Abstract interface for message persistence."""

    @abstractmethod
    def next_message_id(self) -> str: ...

    @abstractmethod
    def append_channel_message(
        self, network: str, channel: str, msg: dict[str, Any],
    ) -> None: ...

    @abstractmethod
    def append_meta_message(
        self, network: str, msg: dict[str, Any],
    ) -> None: ...

    @abstractmethod
    def append_private_message(
        self, network: str, nick: str, msg: dict[str, Any],
    ) -> None: ...

    @abstractmethod
    def get_channel_messages(
        self, network: str, channel: str,
    ) -> list[dict[str, Any]]: ...

    @abstractmethod
    def get_meta_messages(self, network: str) -> list[dict[str, Any]]: ...

    @abstractmethod
    def get_private_messages(
        self, network: str, nick: str,
    ) -> list[dict[str, Any]]: ...

    @abstractmethod
    def remove_network(self, network: str) -> None: ...


class MemoryBackend(StorageBackend):
    """Bounded in-memory message store backed by deques."""

    def __init__(self, max_backlog: int = 1000) -> None:
        self._max_backlog = max_backlog
        self._msg_counter = 0
        self._channel_msgs: dict[
            tuple[str, str], deque[dict[str, Any]]
        ] = {}
        self._meta_msgs: dict[str, deque[dict[str, Any]]] = {}
        self._private_msgs: dict[
            tuple[str, str], deque[dict[str, Any]]
        ] = {}

    def next_message_id(self) -> str:
        self._msg_counter += 1
        return f"msg_{self._msg_counter:012d}"

    def _channel_deque(
        self, network: str, channel: str,
    ) -> deque[dict[str, Any]]:
        key = (network, channel)
        if key not in self._channel_msgs:
            self._channel_msgs[key] = deque(maxlen=self._max_backlog)
        return self._channel_msgs[key]

    def _meta_deque(self, network: str) -> deque[dict[str, Any]]:
        if network not in self._meta_msgs:
            self._meta_msgs[network] = deque(maxlen=self._max_backlog)
        return self._meta_msgs[network]

    def _private_deque(
        self, network: str, nick: str,
    ) -> deque[dict[str, Any]]:
        key = (network, nick)
        if key not in self._private_msgs:
            self._private_msgs[key] = deque(maxlen=self._max_backlog)
        return self._private_msgs[key]

    def append_channel_message(
        self, network: str, channel: str, msg: dict[str, Any],
    ) -> None:
        self._channel_deque(network, channel).append(msg)

    def append_meta_message(
        self, network: str, msg: dict[str, Any],
    ) -> None:
        self._meta_deque(network).append(msg)

    def append_private_message(
        self, network: str, nick: str, msg: dict[str, Any],
    ) -> None:
        self._private_deque(network, nick).append(msg)

    def get_channel_messages(
        self, network: str, channel: str,
    ) -> list[dict[str, Any]]:
        return list(self._channel_deque(network, channel))

    def get_meta_messages(self, network: str) -> list[dict[str, Any]]:
        return list(self._meta_deque(network))

    def get_private_messages(
        self, network: str, nick: str,
    ) -> list[dict[str, Any]]:
        return list(self._private_deque(network, nick))

    def remove_network(self, network: str) -> None:
        self._meta_msgs.pop(network, None)
        for key in [k for k in self._channel_msgs if k[0] == network]:
            del self._channel_msgs[key]
        for key in [k for k in self._private_msgs if k[0] == network]:
            del self._private_msgs[key]
