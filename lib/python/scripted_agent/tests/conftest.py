"""The network guard every zero-token test runs under."""

from __future__ import annotations

import socket
from typing import Any
from urllib.parse import urlsplit

import pytest

from tests.test_replay import BASE_URL


@pytest.fixture
def no_network(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Fail on any connection to a host other than ANTHROPIC_BASE_URL's."""
    allowed = urlsplit(BASE_URL).hostname
    contacted: list[str] = []

    def guard(host: object) -> None:
        contacted.append(str(host))
        if host != allowed:
            raise AssertionError(f"replayer contacted {host}; only {allowed} is allowed")

    real_connect = socket.socket.connect

    def connect(self: socket.socket, address: Any) -> None:
        if self.family in (socket.AF_INET, socket.AF_INET6):
            guard(address[0])
        real_connect(self, address)

    def getaddrinfo(host: Any, *args: Any, **kwargs: Any) -> Any:
        guard(host)
        raise OSError("no network in tests")

    monkeypatch.setattr(socket.socket, "connect", connect)
    monkeypatch.setattr(socket, "getaddrinfo", getaddrinfo)
    return contacted
