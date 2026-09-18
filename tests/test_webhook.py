"""Refresh-webhook tests — authentication first, plumbing second.

This endpoint is the one part of legumista that does something on an unauthenticated
stranger's say-so, so the tests that matter are the ones that prove it does not: no secret
means no route at all, and a wrong signature means no fetch, not merely no swap.

The route is driven through Starlette's TestClient against the real FastMCP app, so what
is exercised is the handler as mounted, not a re-implementation of it.
"""
import hashlib
import hmac
import json

import pytest

from legumista_agent import webhook as W

SECRET = "s3cret-shared-with-github"


def _sign(body: bytes, key: str = SECRET) -> str:
    return "sha256=" + hmac.new(key.encode(), body, hashlib.sha256).hexdigest()


# --- signature verification -----------------------------------------------------------
def test_a_correct_signature_verifies():
    body = b'{"action":"published"}'
    assert W.valid_signature(body, _sign(body), SECRET) is True


@pytest.mark.parametrize("header, because", [
    ("", "no header at all"),
    ("sha256=", "an empty digest"),
    ("deadbeef", "no algorithm prefix"),
    ("sha1=" + "0" * 40, "the retired sha1 scheme GitHub also sends"),
    ("sha256=" + "0" * 64, "a well-formed but wrong digest"),
])
def test_bad_signatures_are_refused(header, because):
    assert W.valid_signature(b"{}", header, SECRET) is False


def test_signature_is_rejected_when_the_body_differs():
    """The signature covers the body; a replayed header on altered content must fail."""
    good = _sign(b'{"action":"published"}')
    assert W.valid_signature(b'{"action":"deleted"}', good, SECRET) is False


def test_no_secret_never_verifies():
    """Belt and braces: even if a route were somehow mounted with an empty key, an empty
    secret must not make every signature valid."""
    body = b"{}"
    assert W.valid_signature(body, _sign(body, ""), "") is False


# --- route mounting -------------------------------------------------------------------
def _server(monkeypatch, refresh_fn):
    from fastmcp import FastMCP
    server = FastMCP(name="test")
    registered = W.register(server, refresh_fn=refresh_fn)
    return server, registered


def test_route_is_not_registered_without_a_secret(monkeypatch):
    """Fail closed. A default deployment must expose no way to make the server fetch."""
    monkeypatch.delenv("LEGUMISTA_WEBHOOK_SECRET", raising=False)
    calls = []
    server, registered = _server(monkeypatch, lambda **k: calls.append(k) or {})
    assert registered is False

    from starlette.testclient import TestClient
    with TestClient(server.http_app()) as client:
        assert client.post(W.WEBHOOK_PATH, content=b"{}").status_code == 404
    assert calls == []


@pytest.fixture
def client(monkeypatch):
    """A mounted webhook plus the refresh calls it made."""
    monkeypatch.setenv("LEGUMISTA_WEBHOOK_SECRET", SECRET)
    calls = []

    def refresh(*, force=False):
        calls.append(force)
        return {"status": "updated", "detail": "2 collections", "reloaded": True,
                "stamp": "[catalog built 2026-09-09T00:00:00Z ...]"}

    server, registered = _server(monkeypatch, refresh)
    assert registered is True
    from starlette.testclient import TestClient
    with TestClient(server.http_app()) as c:
        yield c, calls


def test_valid_delivery_triggers_a_forced_refresh(client):
    c, calls = client
    body = json.dumps({"action": "published"}).encode()
    res = c.post(W.WEBHOOK_PATH, content=body,
                 headers={"X-Hub-Signature-256": _sign(body),
                          "X-GitHub-Event": "release"})
    assert res.status_code == 200
    assert res.json()["status"] == "updated" and res.json()["reloaded"] is True
    # force=True: the publisher has asserted it changed, so a stale ETag must not win.
    assert calls == [True]


def test_bad_signature_is_401_and_performs_no_fetch(client):
    """The important half is `calls == []`. Rejecting the swap but still downloading
    would leave an unauthenticated request able to cost bandwidth on demand."""
    c, calls = client
    res = c.post(W.WEBHOOK_PATH, content=b'{"action":"published"}',
                 headers={"X-Hub-Signature-256": "sha256=" + "0" * 64})
    assert res.status_code == 401
    assert calls == []


def test_missing_signature_is_401(client):
    c, calls = client
    assert c.post(W.WEBHOOK_PATH, content=b"{}").status_code == 401
    assert calls == []


def test_github_ping_is_answered_without_fetching(client):
    """GitHub sends `ping` when the hook is created; it must go green without pulling
    2.4 MB."""
    c, calls = client
    body = b'{"zen":"Non-blocking is better than blocking."}'
    res = c.post(W.WEBHOOK_PATH, content=body,
                 headers={"X-Hub-Signature-256": _sign(body),
                          "X-GitHub-Event": "ping"})
    assert res.status_code == 200 and res.json()["pong"] is True
    assert calls == []


def test_a_failed_refresh_answers_502(monkeypatch):
    """A failed delivery should look failed in the sender's log, not be swallowed by a
    200 that nobody reads."""
    monkeypatch.setenv("LEGUMISTA_WEBHOOK_SECRET", SECRET)

    def refresh(*, force=False):
        return {"status": "error", "detail": "HTTP 404 Not Found", "reloaded": False}

    server, _ = _server(monkeypatch, refresh)
    from starlette.testclient import TestClient
    with TestClient(server.http_app()) as c:
        res = c.post(W.WEBHOOK_PATH, content=b"{}",
                     headers={"X-Hub-Signature-256": _sign(b"{}")})
    assert res.status_code == 502
    assert res.json()["status"] == "error"


def test_a_pinned_catalog_answers_200_and_says_so(monkeypatch):
    """Pinning is a deliberate operator choice, not a fault: the hook should report it
    rather than fail, so a shared publisher hitting many servers sees the difference."""
    monkeypatch.setenv("LEGUMISTA_WEBHOOK_SECRET", SECRET)
    server, _ = _server(monkeypatch, lambda **k: {
        "status": "pinned", "reloaded": False,
        "detail": "a local catalog.json is in force"})
    from starlette.testclient import TestClient
    with TestClient(server.http_app()) as c:
        res = c.post(W.WEBHOOK_PATH, content=b"{}",
                     headers={"X-Hub-Signature-256": _sign(b"{}")})
    assert res.status_code == 200 and res.json()["status"] == "pinned"
