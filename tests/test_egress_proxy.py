"""The egress guard htslib's traffic is routed through.

Offline, like every test here: the resolver is faked and each "TCP" connection is a
Unix socketpair, so these exercise the proxy's decisions and its byte handling without
a network. The live behaviour — libcurl honouring the proxy variables, a redirect hop to
a private address being refused — was checked by hand against real hosts."""
import os
import socket
import threading

import pytest

from legumista_agent import egress_proxy as E
from legumista_agent import tools_native as N

PUBLIC, PRIVATE = "93.184.216.34", "169.254.169.254"


@pytest.fixture
def dns(monkeypatch):
    """Answer getaddrinfo from a {host: [ip, ...]} table the test fills in."""
    table = {}

    def fake(host, port, *a, **k):
        if host not in table:
            raise socket.gaierror(f"no such host {host}")
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, port)) for ip in table[host]]

    monkeypatch.setattr(socket, "getaddrinfo", fake)
    return table


@pytest.fixture
def upstream(monkeypatch):
    """Replace the outbound connect with a socketpair; record what was dialled."""
    dialled, ends = [], []

    def fake_connect(family, sockaddr):
        dialled.append(sockaddr)
        ours, theirs = socket.socketpair()
        ends.append(theirs)
        return ours

    monkeypatch.setattr(E, "_connect", fake_connect)
    return dialled, ends


def _serve_in_thread():
    client, proxy_side = socket.socketpair()
    thread = threading.Thread(target=E.serve, args=(proxy_side,), daemon=True)
    thread.start()
    return client, proxy_side, thread


def _read_all(sock) -> bytes:
    sock.settimeout(5)
    data = b""
    while True:
        chunk = sock.recv(65536)
        if not chunk:
            return data
        data += chunk


# --- the policy ------------------------------------------------------------------------
def test_resolve_allows_public_and_returns_the_checked_address(dns):
    dns["data.example"] = [PUBLIC]
    family, sockaddr = E.resolve("data.example", 443)
    assert family == socket.AF_INET and sockaddr == (PUBLIC, 443)


@pytest.mark.parametrize("answers", [[PRIVATE], ["127.0.0.1"], [PUBLIC, "10.0.0.5"],
                                     ["100.100.100.200"]])
def test_resolve_refuses_any_non_public_answer(dns, answers):
    """Mixed answers are refused too: a hostile resolver can offer one of each."""
    dns["evil.example"] = answers
    with pytest.raises(E.Refused, match="non-public"):
        E.resolve("evil.example", 443)


def test_resolve_refuses_other_ports_and_unresolvable_hosts(dns):
    dns["data.example"] = [PUBLIC]
    with pytest.raises(E.Refused, match="port 25"):
        E.resolve("data.example", 25)
    with pytest.raises(E.Refused, match="cannot resolve"):
        E.resolve("nowhere.example", 443)


# --- the protocol ----------------------------------------------------------------------
def test_connect_tunnel_dials_the_checked_address_and_relays(dns, upstream):
    dns["data.example"] = [PUBLIC]
    dialled, ends = upstream
    client, _, thread = _serve_in_thread()
    client.sendall(b"CONNECT data.example:443 HTTP/1.1\r\nHost: data.example:443\r\n\r\n")
    client.settimeout(5)
    assert client.recv(1024).startswith(b"HTTP/1.1 200")
    assert dialled == [(PUBLIC, 443)], "must connect to the address it vetted"
    client.sendall(b"tls-client-hello")
    ends[0].settimeout(5)
    assert ends[0].recv(1024) == b"tls-client-hello"
    ends[0].sendall(b"tls-server-hello")
    assert client.recv(1024) == b"tls-server-hello"
    ends[0].close()
    thread.join(5)
    assert not thread.is_alive()


def test_connect_to_a_private_host_is_refused_without_dialling(dns, upstream):
    """The redirect hop: libcurl asks for the metadata endpoint and is told no."""
    dns["169.254.169.254"] = [PRIVATE]
    dialled, _ = upstream
    client, _, thread = _serve_in_thread()
    client.sendall(b"CONNECT 169.254.169.254:80 HTTP/1.1\r\n\r\n")
    reply = _read_all(client)
    thread.join(5)
    assert reply.startswith(b"HTTP/1.1 403") and b"non-public" in reply
    assert dialled == []


def test_plain_http_get_is_rewritten_to_origin_form(dns, upstream):
    dns["data.example"] = [PUBLIC]
    _, ends = upstream
    client, _, thread = _serve_in_thread()
    client.sendall(b"GET http://data.example/x.fa.fai?v=1 HTTP/1.1\r\nHost: data.example\r\n"
                   b"Range: bytes=0-99\r\nProxy-Connection: Keep-Alive\r\n\r\n")
    ends_sock = None
    for _ in range(50):                       # wait for the proxy to dial
        if ends:
            ends_sock = ends[0]
            break
        threading.Event().wait(0.05)
    ends_sock.settimeout(5)
    forwarded = ends_sock.recv(65536)
    assert forwarded.startswith(b"GET /x.fa.fai?v=1 HTTP/1.1\r\n")
    assert b"Range: bytes=0-99" in forwarded and b"Connection: close" in forwarded
    assert b"Proxy-Connection" not in forwarded
    ends_sock.sendall(b"HTTP/1.1 206 Partial Content\r\nContent-Length: 2\r\n\r\nok")
    ends_sock.close()
    assert _read_all(client).endswith(b"ok")
    thread.join(5)


@pytest.mark.parametrize("method", [b"PUT", b"POST", b"DELETE"])
def test_plain_http_only_reads(dns, upstream, method):
    dns["data.example"] = [PUBLIC]
    dialled, _ = upstream
    client, _, thread = _serve_in_thread()
    client.sendall(method + b" http://data.example/x HTTP/1.1\r\nHost: data.example\r\n\r\n")
    reply = _read_all(client)
    thread.join(5)
    assert reply.startswith(b"HTTP/1.1 405") and dialled == []


def test_relay_drops_an_idle_connection(dns, upstream, monkeypatch):
    """The in-process helpers have no timeout of their own; this is their bound."""
    monkeypatch.setattr(E, "IDLE_SECONDS", 0.2)
    dns["slow.example"] = [PUBLIC]
    client, _, thread = _serve_in_thread()
    client.sendall(b"CONNECT slow.example:443 HTTP/1.1\r\n\r\n")
    thread.join(5)
    assert not thread.is_alive(), "an idle tunnel must be closed"


# --- the workers' environment ---------------------------------------------------------
def test_child_env_routes_htslib_through_the_guard_and_leaves_the_server_alone(monkeypatch):
    monkeypatch.setattr(E, "_STATE", {"url": ""})
    monkeypatch.setenv("HTTPS_PROXY", "http://corporate.example:3128")
    monkeypatch.setenv("no_proxy", "*")
    env = E.child_env()
    url = E._STATE["url"]
    assert url.startswith("http://127.0.0.1:")
    assert env["https_proxy"] == env["http_proxy"] == env["HTTPS_PROXY"] == url
    assert "no_proxy" not in env, "a no_proxy would let htslib bypass the guard"
    # The server's own environment — urllib, the NCBI CLIs — is untouched.
    assert os.environ["HTTPS_PROXY"] == "http://corporate.example:3128"
    assert os.environ["no_proxy"] == "*"
    assert E.child_env()["https_proxy"] == url          # one proxy per process


# --- the address policy the proxy and every URL guard share ----------------------------
@pytest.mark.parametrize("ip,blocked", [
    ("100.100.100.200", True),     # shared address space: Alibaba Cloud metadata
    ("100.64.0.1", True),          # CGNAT / Tailscale
    ("64:ff9b::a9fe:a9fe", True),  # NAT64-wrapped metadata endpoint; Python calls it global
    ("2002:a9fe:a9fe::1", True),   # 6to4-wrapped metadata endpoint
    ("fe80::1%eth0", True),        # scoped link-local
    ("192.0.2.1", True),           # documentation range
    ("64:ff9b::808:808", False),   # NAT64 of a public address is fine
    ("2606:4700::1111", False),    # public IPv6
])
def test_ip_policy_is_public_unicast_only(ip, blocked):
    assert N._ip_is_blocked(ip) is blocked
