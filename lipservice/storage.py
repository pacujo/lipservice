from __future__ import annotations

from abc import ABC, abstractmethod
from collections import deque

from lipservice.models import Message, NetworkConfig, Session


class StorageBackend(ABC):
    """Abstract interface for message persistence."""

    @abstractmethod
    def next_message_id(self) -> str: ...

    @abstractmethod
    def append_channel_message(
        self, network: str, channel: str, msg: Message,
    ) -> None: ...

    @abstractmethod
    def append_meta_message(
        self, network: str, msg: Message,
    ) -> None: ...

    @abstractmethod
    def append_private_message(
        self, network: str, nick: str, msg: Message,
    ) -> None: ...

    @abstractmethod
    def get_channel_messages(
        self, network: str, channel: str,
    ) -> list[Message]: ...

    @abstractmethod
    def get_meta_messages(self, network: str) -> list[Message]: ...

    @abstractmethod
    def get_private_messages(
        self, network: str, nick: str,
    ) -> list[Message]: ...

    @abstractmethod
    def list_private_peers(self, network: str) -> list[str]: ...

    @abstractmethod
    def remove_private_peer(
        self, network: str, nick: str,
    ) -> None: ...

    @abstractmethod
    def get_session(self) -> Session: ...

    @abstractmethod
    def set_session(
        self, current_network: str | None,
        current_channel: str | None, current_query: str | None,
    ) -> None: ...

    @abstractmethod
    def get_pointer(self, network: str, target: str) -> str | None: ...

    @abstractmethod
    def set_pointer(
        self, network: str, target: str, last_read_id: str,
    ) -> None: ...

    @abstractmethod
    def get_all_pointers(self) -> dict[str, str]: ...

    @abstractmethod
    def list_networks(self) -> list[NetworkConfig]: ...

    @abstractmethod
    def save_network(self, config: NetworkConfig) -> None: ...

    @abstractmethod
    def delete_network(self, name: str) -> None: ...

    @abstractmethod
    def remove_network_data(self, network: str) -> None: ...


class MemoryBackend(StorageBackend):
    """Bounded in-memory message store backed by deques."""

    def __init__(self, max_backlog: int = 1000) -> None:
        self._max_backlog = max_backlog
        self._msg_counter = 0
        self._channel_msgs: dict[tuple[str, str], deque[Message]] = {}
        self._meta_msgs: dict[str, deque[Message]] = {}
        self._private_msgs: dict[tuple[str, str], deque[Message]] = {}
        self._networks: dict[str, NetworkConfig] = {}
        self._session: dict[str, str | None] = {
            "current_network": None,
            "current_channel": None,
            "current_query": None,
        }
        self._pointers: dict[str, str] = {}

    def next_message_id(self) -> str:
        self._msg_counter += 1
        return f"msg_{self._msg_counter:012d}"

    def _channel_deque(
        self, network: str, channel: str,
    ) -> deque[Message]:
        key = (network, channel)
        if key not in self._channel_msgs:
            self._channel_msgs[key] = deque(maxlen=self._max_backlog)
        return self._channel_msgs[key]

    def _meta_deque(self, network: str) -> deque[Message]:
        if network not in self._meta_msgs:
            self._meta_msgs[network] = deque(maxlen=self._max_backlog)
        return self._meta_msgs[network]

    def _private_deque(
        self, network: str, nick: str,
    ) -> deque[Message]:
        key = (network, nick)
        if key not in self._private_msgs:
            self._private_msgs[key] = deque(maxlen=self._max_backlog)
        return self._private_msgs[key]

    def append_channel_message(
        self, network: str, channel: str, msg: Message,
    ) -> None:
        self._channel_deque(network, channel).append(msg)

    def append_meta_message(
        self, network: str, msg: Message,
    ) -> None:
        self._meta_deque(network).append(msg)

    def append_private_message(
        self, network: str, nick: str, msg: Message,
    ) -> None:
        self._private_deque(network, nick).append(msg)

    def get_channel_messages(
        self, network: str, channel: str,
    ) -> list[Message]:
        return list(self._channel_deque(network, channel))

    def get_meta_messages(self, network: str) -> list[Message]:
        return list(self._meta_deque(network))

    def get_private_messages(
        self, network: str, nick: str,
    ) -> list[Message]:
        return list(self._private_deque(network, nick))

    def get_session(self) -> Session:
        return Session(
            current_network=self._session["current_network"],
            current_channel=self._session["current_channel"],
            current_query=self._session["current_query"],
            pointers=dict(self._pointers),
        )

    def set_session(
        self, current_network: str | None,
        current_channel: str | None, current_query: str | None,
    ) -> None:
        self._session["current_network"] = current_network
        self._session["current_channel"] = current_channel
        self._session["current_query"] = current_query

    def get_pointer(self, network: str, target: str) -> str | None:
        return self._pointers.get(f"{network}/{target}")

    def set_pointer(
        self, network: str, target: str, last_read_id: str,
    ) -> None:
        self._pointers[f"{network}/{target}"] = last_read_id

    def get_all_pointers(self) -> dict[str, str]:
        return dict(self._pointers)

    def list_private_peers(self, network: str) -> list[str]:
        return sorted({
            nick for net, nick in self._private_msgs if net == network
        })

    def remove_private_peer(
        self, network: str, nick: str,
    ) -> None:
        self._private_msgs.pop((network, nick), None)

    def list_networks(self) -> list[NetworkConfig]:
        return list(self._networks.values())

    def save_network(self, config: NetworkConfig) -> None:
        self._networks[config.name] = config

    def delete_network(self, name: str) -> None:
        self._networks.pop(name, None)

    def remove_network_data(self, network: str) -> None:
        self._meta_msgs.pop(network, None)
        for key in [k for k in self._channel_msgs if k[0] == network]:
            del self._channel_msgs[key]
        for key in [k for k in self._private_msgs if k[0] == network]:
            del self._private_msgs[key]
