#!/usr/bin/env python3
"""The catalog refresh webhook: `POST /catalog/refresh`.

A published catalog is only useful if running servers pick it up. Polling alone means a
new catalog waits up to `LEGUMISTA_CATALOG_POLL` (24 h by default) to be noticed; this
endpoint closes that gap, so the job that publishes a catalog can tell the server about it
the moment it lands.

**Authentication is GitHub's webhook scheme** — an HMAC-SHA256 of the raw request body,
keyed by a shared secret, in `X-Hub-Signature-256`. That choice means a GitHub release (or
`workflow_run`, or `repository_dispatch`) can call this endpoint directly with no glue
code: set the same secret on both ends and GitHub signs every delivery itself.

Two properties matter more than the mechanism:

* **It fails closed.** With no `LEGUMISTA_WEBHOOK_SECRET` set, the route is never
  registered and the path 404s. Refresh-on-demand is opt-in, so a default deployment
  exposes no way to make the server fetch anything.
* **The comparison is constant-time.** `hmac.compare_digest` does not leak, through timing,
  how much of a candidate signature was correct — which is what would otherwise turn a
  rejected signature into an oracle for forging an accepted one.

The endpoint is registered only under the HTTP transport, because stdio serves no HTTP.
A stdio server is spawned per client and is short-lived, so it refreshes at startup and
has little staleness to close.
"""
import hashlib
import hmac
import os
import threading

WEBHOOK_PATH = "/catalog/refresh"

# One refresh at a time. A publish that fans out to several deliveries (or a retry
# arriving while the first is still downloading) should not start a second 2.4 MB fetch;
# the second caller is told the first is in flight rather than being queued behind it.
_BUSY = threading.Lock()


def secret() -> str:
    """The shared secret, read at call time so tests and restarts pick up changes."""
    return os.environ.get("LEGUMISTA_WEBHOOK_SECRET", "")


def valid_signature(body: bytes, header: str, key: str) -> bool:
    """Is `header` a valid `sha256=<hex>` GitHub signature for `body` under `key`?"""
    if not key or not header:
        return False
    algo, _, sent = header.partition("=")
    if algo != "sha256" or not sent:
        return False
    expected = hmac.new(key.encode("utf-8"), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, sent.strip())


def register(server, *, refresh_fn, log=None) -> bool:
    """Attach the refresh route to a FastMCP server. False if no secret is configured.

    `refresh_fn(force=...)` is `tools_catalog.refresh` — the webhook owns authentication
    and the HTTP shape, and nothing else, so a clock-driven and a webhook-driven refresh
    run exactly the same code.
    """
    key = secret()
    if not key:
        if log:
            log("[*] catalog refresh webhook: disabled "
                "(set LEGUMISTA_WEBHOOK_SECRET to enable)")
        return False

    from starlette.responses import JSONResponse  # noqa: PLC0415 - fastmcp's HTTP stack

    @server.custom_route(WEBHOOK_PATH, methods=["POST"])
    async def refresh_route(request):            # noqa: ANN001 - starlette Request
        body = await request.body()
        if not valid_signature(body, request.headers.get("X-Hub-Signature-256", ""), key):
            if log:
                log("[!] catalog refresh: rejected delivery (bad signature)")
            return JSONResponse({"error": "invalid signature"}, status_code=401)

        # GitHub sends `ping` once when a webhook is created. Answering it is what makes
        # the hook show as green in the UI; it must not trigger a download.
        if request.headers.get("X-GitHub-Event", "") == "ping":
            return JSONResponse({"ok": True, "pong": True, "path": WEBHOOK_PATH})

        if not _BUSY.acquire(blocking=False):
            return JSONResponse({"status": "busy",
                                 "detail": "a refresh is already in progress"},
                                status_code=409)
        try:
            import anyio  # noqa: PLC0415 - bundled with starlette/fastmcp
            # refresh_fn is blocking urllib + JSON parsing; running it on the event loop
            # would stall every other MCP request for the length of the download.
            report = await anyio.to_thread.run_sync(lambda: refresh_fn(force=True))
        finally:
            _BUSY.release()

        if log:
            log(f"[*] catalog refresh: {report.get('status')}"
                + (f" — {report.get('detail')}" if report.get("detail") else ""))
        # A failed refresh answers 502 so it shows as a failed delivery in the sender's
        # log rather than being silently swallowed behind a 200.
        code = 502 if report.get("status") == "error" else 200
        return JSONResponse(report, status_code=code)

    if log:
        log(f"[*] catalog refresh webhook: POST {WEBHOOK_PATH} (HMAC-SHA256)")
    return True
