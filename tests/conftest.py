"""Suite-wide isolation.

Two globals would otherwise leak between the tests and the developer's machine: the
catalog cache directory (a real one exists at ~/.cache/legumista once the server has run
once) and the webhook secret. A test that expects "no catalog" would quietly pass or fail
depending on whether the machine happened to have a cached one, which is exactly the kind
of failure that only shows up in CI.
"""
import pytest


@pytest.fixture(autouse=True)
def _isolate_catalog_env(tmp_path, monkeypatch):
    monkeypatch.setenv("LEGUMISTA_CACHE_DIR", str(tmp_path / "cache"))
    monkeypatch.delenv("LEGUMISTA_CATALOG_URL", raising=False)
    monkeypatch.delenv("LEGUMISTA_WEBHOOK_SECRET", raising=False)
    monkeypatch.setenv("LEGUMISTA_CATALOG_POLL", "0")   # never start a poller in tests


# --- no network, ever --------------------------------------------------------------------
# AGENTS.md promises an offline suite; this makes the promise enforced rather than hoped
# for. Any test that opens a TCP connection or resolves a hostname fails with the address
# it tried. IP literals and "localhost" resolve without DNS and stay allowed (the SSRF
# tests resolve 127.0.0.1 and 169.254.169.254 on purpose). Unix sockets are untouched, so
# asyncio's self-pipe and FastMCP's in-memory transport keep working.
import ipaddress  # noqa: E402
import socket  # noqa: E402

_REAL_GETADDRINFO = socket.getaddrinfo
_REAL_CONNECT = socket.socket.connect


class NetworkBlocked(RuntimeError):
    """A test reached for the network."""


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    def guarded_connect(sock, address):
        if sock.family in (socket.AF_INET, socket.AF_INET6):
            raise NetworkBlocked(f"test tried to connect to {address!r}")
        return _REAL_CONNECT(sock, address)

    def guarded_getaddrinfo(host, *args, **kwargs):
        try:
            ipaddress.ip_address(str(host))
        except ValueError:
            if host != "localhost":
                raise NetworkBlocked(f"test tried to resolve {host!r}") from None
        return _REAL_GETADDRINFO(host, *args, **kwargs)

    monkeypatch.setattr(socket.socket, "connect", guarded_connect)
    monkeypatch.setattr(socket, "getaddrinfo", guarded_getaddrinfo)
