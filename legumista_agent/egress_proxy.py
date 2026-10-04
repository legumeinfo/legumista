#!/usr/bin/env python3
"""Egress guard for htslib: a loopback HTTP proxy that refuses non-public destinations.

The genomics tools may read a remote file from any public host. What they must never do
is reach a private one — loopback, the LAN, or a cloud metadata endpoint. Checking the
URL a caller passes (`tools_native._validate_url`) is not enough on its own, because
htslib does its own HTTP through libcurl, out of our reach:

- **libcurl follows redirects** (hfile_libcurl.c sets CURLOPT_FOLLOWLOCATION), so a
  public URL can 302 to http://169.254.169.254/. A remote FASTA plus its own `.fai` then
  turns the redirect target's response into the "sequence" fasta_fetch returns.
- **libcurl resolves the host itself**, after our check, so a DNS answer that changes in
  between (rebinding) lands on whatever address it names.

libcurl honours `http_proxy`/`https_proxy`, so every htslib process — they are all
`_pysam_worker.py` children — is started with those variables naming this proxy, and
every connection it makes, the first request and each redirect hop alike, arrives here
as `CONNECT host:port` (https) or an absolute-URI `GET` (http). The proxy resolves the
host, refuses unless every address it resolves to is public, and then connects to the
exact address it checked, so there is no second lookup to rebind. TLS stays end to end:
an https tunnel is relayed as opaque bytes.

Policy, for every connection:
  - the destination port is in LEGUMISTA_EGRESS_PORTS (default 80,443)
  - every address the host resolves to is public (`tools_native._ip_is_blocked`)
  - plain http carries GET or HEAD only: htslib reads; it never needs to send
  - the client may send at most LEGUMISTA_EGRESS_MAX_SEND_BYTES (default 64 KiB) on one
    connection: enough for any read's requests, and a hard bound on an upload
  - the connection is closed after LEGUMISTA_EGRESS_IDLE_SECONDS without traffic
    (default 60) and after LEGUMISTA_EGRESS_MAX_SECONDS in all (default 900)

The server's own environment is never changed: only the workers' is. urllib (web_fetch,
the catalog download) and the NCBI CLIs keep whatever proxy configuration the operator
gave them.
"""
import os
import select
import socket
import socketserver
import threading
import time
import urllib.parse

PORTS = {int(p) for p in os.environ.get("LEGUMISTA_EGRESS_PORTS", "80,443").split(",")
         if p.strip()}
IDLE_SECONDS = int(os.environ.get("LEGUMISTA_EGRESS_IDLE_SECONDS", "60"))
# Reads send almost nothing: one request per connection, under 1.2 KB measured for
# region queries and whole-file streams alike. Uploads send the data. Over https the
# method is hidden inside TLS, so this byte budget is what bounds an upload through an
# output option the argv guard does not know about.
MAX_SEND_BYTES = int(os.environ.get("LEGUMISTA_EGRESS_MAX_SEND_BYTES", str(64 * 1024)))
MAX_SECONDS = int(os.environ.get("LEGUMISTA_EGRESS_MAX_SECONDS", "900"))
_MAX_HEAD = 64 * 1024

# Every variable libcurl consults. A worker gets all of them replaced: an operator's
# no_proxy, say, would otherwise let htslib reach the hosts it names directly.
_PROXY_VARS = ("http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY",
               "all_proxy", "ALL_PROXY", "no_proxy", "NO_PROXY")
_STATE = {"url": ""}
_LOCK = threading.Lock()


class Refused(Exception):
    """The destination is not one the genomics tools may reach."""


def resolve(host: str, port: int):
    """Vet a destination and return the (family, sockaddr) to connect to.

    Refuses if ANY resolved address is non-public, not merely the first: a hostile
    resolver can answer with a public and a private address and let the client pick."""
    from .tools_native import _ip_is_blocked   # lazy: tools_native imports this module

    if port not in PORTS:
        raise Refused(f"port {port} is not allowed (LEGUMISTA_EGRESS_PORTS)")
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except (socket.gaierror, UnicodeError) as e:
        raise Refused(f"cannot resolve {host!r}: {e}") from None
    if not infos:
        raise Refused(f"{host!r} resolves to nothing")
    for info in infos:
        if _ip_is_blocked(info[4][0]):
            raise Refused(f"{host} resolves to a non-public address ({info[4][0]})")
    return infos[0][0], infos[0][4]


def _connect(family, sockaddr):
    sock = socket.socket(family, socket.SOCK_STREAM)
    sock.settimeout(IDLE_SECONDS)
    try:
        sock.connect(sockaddr)
    except OSError:
        sock.close()
        raise
    return sock


def _read_head(client) -> bytes:
    """Read the request line and headers; b"" if the client sent nothing usable."""
    buf = b""
    while b"\r\n\r\n" not in buf:
        chunk = client.recv(4096)
        if not chunk:
            return b""
        buf += chunk
        if len(buf) > _MAX_HEAD:
            return b""
    return buf


def _reply(client, status: int, reason: str, detail: str) -> None:
    body = f"legumista egress guard: {detail}\n".encode()
    try:
        client.sendall(f"HTTP/1.1 {status} {reason}\r\nContent-Type: text/plain\r\n"
                       f"Content-Length: {len(body)}\r\nConnection: close\r\n\r\n"
                       .encode() + body)
    except OSError:
        return


def _relay(client, upstream, sent: int = 0) -> None:
    """Copy bytes both ways until either side closes, goes idle, or time runs out, or
    the client tries to send more than MAX_SEND_BYTES (`sent` counts what it already
    has). The chunk that would cross the budget is dropped, not forwarded."""
    deadline = time.monotonic() + MAX_SECONDS
    peers = {client: upstream, upstream: client}
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return
        readable, _, _ = select.select(list(peers), [], [], min(IDLE_SECONDS, remaining))
        if not readable:
            return
        for sock in readable:
            data = sock.recv(65536)
            if not data:
                return
            if sock is client:
                sent += len(data)
                if sent > MAX_SEND_BYTES:
                    return
            peers[sock].sendall(data)


def _split_authority(authority: str):
    host, _, port = authority.rpartition(":")
    if not host or not port.isdigit():
        raise Refused(f"malformed CONNECT target {authority!r}")
    return host.strip("[]"), int(port)


def serve(client) -> None:
    """Handle one proxy connection on an already-accepted socket, then close it."""
    try:
        _serve(client)
    finally:
        client.close()


def _serve(client) -> None:
    client.settimeout(IDLE_SECONDS)
    head = _read_head(client)
    if not head:
        return
    header_block, _, tail = head.partition(b"\r\n\r\n")
    request_line, _, headers = header_block.partition(b"\r\n")
    try:
        method, target, version = request_line.decode("latin-1").split(" ")
    except ValueError:
        _reply(client, 400, "Bad Request", "malformed request line")
        return
    upstream, relaying = None, False
    try:
        if method == "CONNECT":
            upstream = _connect(*resolve(*_split_authority(target)))
            client.sendall(b"HTTP/1.1 200 Connection established\r\n\r\n")
            relaying = True
            if len(tail) > MAX_SEND_BYTES:
                return
            if tail:
                upstream.sendall(tail)
        elif method in ("GET", "HEAD"):
            url = urllib.parse.urlsplit(target)
            if url.scheme != "http" or not url.hostname:
                return _reply(client, 400, "Bad Request",
                              f"expected an absolute http:// URL, got {target!r}")
            upstream = _connect(*resolve(url.hostname, url.port or 80))
            path = (url.path or "/") + (f"?{url.query}" if url.query else "")
            kept = [h for h in headers.split(b"\r\n") if h and not h.lower().startswith(
                (b"proxy-", b"connection:", b"keep-alive:"))]
            upstream.sendall(f"{method} {path} {version}\r\n".encode("latin-1")
                             + b"\r\n".join(kept + [b"Connection: close"]) + b"\r\n\r\n")
        else:
            return _reply(client, 405, "Method Not Allowed",
                          f"{method} is not allowed: the genomics tools only read")
        relaying = True
        _relay(client, upstream, sent=len(tail))
    except Refused as e:
        _reply(client, 403, "Forbidden", str(e))
    except OSError as e:
        if not relaying:       # mid-relay, the client is already reading the far end
            _reply(client, 502, "Bad Gateway", f"{type(e).__name__}: {e}")
    finally:
        if upstream is not None:
            upstream.close()


class _Handler(socketserver.BaseRequestHandler):
    def handle(self):
        serve(self.request)


class _Server(socketserver.ThreadingTCPServer):
    daemon_threads = True
    allow_reuse_address = True


def start() -> str:
    """Start the proxy (once) on an ephemeral loopback port; return its URL."""
    with _LOCK:
        if not _STATE["url"]:
            server = _Server(("127.0.0.1", 0), _Handler)
            threading.Thread(target=server.serve_forever, name="legumista-egress",
                             daemon=True).start()
            _STATE["url"] = f"http://127.0.0.1:{server.server_address[1]}"
        return _STATE["url"]


def child_env() -> dict:
    """The environment for an htslib worker: this process's, with every proxy variable
    replaced by the guard. (libcurl reads lowercase http_proxy only — uppercase is
    ignored for http, as a CGI safeguard — and either case for https.)"""
    url = start()
    env = {k: v for k, v in os.environ.items() if k not in _PROXY_VARS}
    env.update(http_proxy=url, https_proxy=url, HTTPS_PROXY=url)
    return env
