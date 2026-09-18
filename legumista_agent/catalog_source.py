#!/usr/bin/env python3
"""Where `catalog.json` comes from: a published URL, with an on-disk cache.

The catalog used to be vendored in the repository, which coupled a 2.4 MB data artifact
to the code's release cycle — a catalog rebuilt from a fresher `datastore-metadata` meant
a commit, a release, and a redeploy, for a file that contains no code. It is now fetched
from a published URL and cached on disk, so data and code move independently.

Resolution order, highest first:

1. **A local `catalog.json`** — at the install root or in the working directory. This is
   the pin/offline path: present, it is used verbatim and nothing is fetched. It is what a
   `-v /path/catalog.json:/work/catalog.json:ro` mount lands on, and what a developer gets
   from a checkout that still has one.
2. **The cache** — a previously downloaded copy under the cache directory.
3. **The network** — an explicit fetch, at startup or on demand.

Note the asymmetry: a *read* never touches the network. `controller()` in tools_catalog
loads whatever is already on disk, so no tool call can block on a 2.4 MB download or hang
on a slow host. Fetching happens at three explicit moments — server startup, the refresh
webhook, and the background poller — and each one swaps the loaded catalog only after the
download has been validated.

Freshness is cheap: the server records the `ETag` and `Last-Modified` it was served and
sends them back as `If-None-Match`/`If-Modified-Since`. An unchanged catalog costs one 304
with no body, which is what makes a 24-hour poll unremarkable.

Environment:

    LEGUMISTA_CATALOG_URL    where to fetch from (default: the LIS-autocontent release)
    LEGUMISTA_CACHE_DIR      where to cache it (default: ~/.cache/legumista)
    LEGUMISTA_CATALOG_POLL   seconds between background checks; 0 disables (default 86400)
"""
import hashlib
import json
import os
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from .tools_native import BlockedURLError

# The published catalog. `releases/latest/download/` is a stable alias that always
# resolves to the newest release's asset, so the URL never changes as catalogs are
# republished. It 302s to objects.githubusercontent.com, so every redirect hop is
# re-checked below.
DEFAULT_CATALOG_URL = (
    "https://github.com/matthewwiese/LIS-autocontent/releases/latest/download/catalog.json"
)

# Downloads larger than this are refused outright. The real artifact is ~2.4 MB; the cap
# exists so a misconfigured URL pointing at something enormous fails fast instead of
# being read into memory.
MAX_BYTES = 64 * 1024 * 1024

FETCH_TIMEOUT = int(os.environ.get("LEGUMISTA_CATALOG_TIMEOUT", "60"))
_UA = {"User-Agent": "legumista/1.0 (+https://github.com/legumeinfo/legumista)"}


# Why this does NOT reuse tools_native's `_open_guarded`: that guard refuses private,
# loopback and link-local addresses, because it protects fetches whose URL came from the
# MODEL — where reaching an internal service is the whole attack. The catalog URL is
# operator configuration, at the same trust level as the code itself, and an internal
# mirror (or a localhost origin in a test) is a legitimate place to publish a catalog.
# Applying the anti-SSRF guard here would block that while protecting nothing: an operator
# who can set LEGUMISTA_CATALOG_URL can already run arbitrary code.
#
# What is still enforced, on the initial URL and on every redirect hop, is the scheme —
# so a redirect cannot walk the fetch over to file:// or ftp:// and have us read it.
def _check_scheme(url: str) -> None:
    scheme = urllib.parse.urlparse(url or "").scheme
    if scheme not in ("http", "https"):
        raise BlockedURLError(
            f"refusing non-http(s) catalog URL scheme: {scheme or '(none)'!r}")


class _CatalogRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        _check_scheme(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


_OPENER = urllib.request.build_opener(_CatalogRedirectHandler)


def _open(request, timeout):
    """urlopen the catalog request, enforcing the scheme on every hop."""
    _check_scheme(request.full_url)
    return _OPENER.open(request, timeout=timeout)


def catalog_url() -> str:
    """Read the env at call time, not import time, so tests can redirect it."""
    return os.environ.get("LEGUMISTA_CATALOG_URL") or DEFAULT_CATALOG_URL


def cache_dir() -> Path:
    return Path(os.environ.get("LEGUMISTA_CACHE_DIR")
                or Path.home() / ".cache" / "legumista")


def cache_path() -> Path:
    return cache_dir() / "catalog.json"


def _meta_path() -> Path:
    return cache_dir() / "catalog.meta.json"


def read_meta() -> dict:
    """The cache's provenance sidecar: ETag, Last-Modified, when and from where.

    Missing or corrupt is not an error — it only means the next fetch is unconditional.
    """
    try:
        with open(_meta_path(), encoding="utf-8") as handle:
            meta = json.load(handle)
        return meta if isinstance(meta, dict) else {}
    except (OSError, ValueError):
        return {}


def _write_meta(meta: dict) -> None:
    try:
        cache_dir().mkdir(parents=True, exist_ok=True)
        tmp = _meta_path().with_suffix(".tmp")
        with open(tmp, "w", encoding="utf-8") as handle:
            json.dump(meta, handle, indent=2, sort_keys=True)
        os.replace(tmp, _meta_path())
    except OSError:
        pass          # a cache that cannot record its ETag still works, just less cheaply


def validate(raw: bytes) -> dict:
    """Parse and sanity-check a downloaded catalog, or raise ValueError.

    This is the guard that keeps a bad download from replacing a good catalog. The
    failure it exists for is not a corrupt file but a *plausible* one: a login page, an
    S3 error document, or a truncated transfer, any of which parse as "some bytes" and
    would otherwise be swapped in and leave every lis_* tool answering nonsense.
    """
    try:
        doc = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as e:
        raise ValueError(f"not valid JSON ({e})") from e
    if not isinstance(doc, dict):
        raise ValueError(f"expected a JSON object, got {type(doc).__name__}")
    collections = doc.get("collections")
    if not isinstance(collections, list) or not collections:
        raise ValueError("no non-empty 'collections' array — not a catalog document")
    if not isinstance(doc.get("stats"), dict):
        raise ValueError("no 'stats' object — not a catalog document")
    return doc


def fetch(*, force: bool = False, timeout: int = None) -> dict:
    """Fetch the catalog into the cache if it has changed.

    Returns a report dict with `status` one of:
      `updated`   — a new catalog was downloaded, validated and written to the cache
      `unchanged` — the server answered 304; the cache is already current
      `error`     — nothing was written; `detail` says why, the cache is untouched

    `force` skips the conditional headers, re-downloading even an unchanged catalog.
    Nothing here mutates the loaded controller — the caller decides whether to swap.
    """
    url = catalog_url()
    meta = {} if force else read_meta()
    report = {"status": "error", "url": url, "detail": "", "path": str(cache_path())}

    # Validate before constructing the Request: `Request("notaurl")` raises ValueError
    # in its constructor, which would escape fetch() and take down startup or the poller
    # thread over a typo in an env var.
    try:
        _check_scheme(url)
        request = urllib.request.Request(url, headers=dict(_UA))
    except (BlockedURLError, ValueError) as e:
        report["detail"] = f"unusable catalog URL: {e}"
        return report

    # Only send validators that belong to THIS url; a changed url invalidates them.
    if meta.get("url") == url:
        if meta.get("etag"):
            request.add_header("If-None-Match", meta["etag"])
        if meta.get("last_modified"):
            request.add_header("If-Modified-Since", meta["last_modified"])

    try:
        with _open(request, timeout or FETCH_TIMEOUT) as response:
            # Read one byte past the cap so an oversized body is detected rather than
            # silently truncated into something that might still parse.
            raw = response.read(MAX_BYTES + 1)
            headers = response.headers
    except urllib.error.HTTPError as e:
        if e.code == 304:                      # conditional GET: cache is current
            report.update(status="unchanged", detail="304 Not Modified")
            return report
        report["detail"] = f"HTTP {e.code} {e.reason}"
        return report
    except Exception as e:                     # noqa: BLE001 - network is best-effort
        report["detail"] = f"{type(e).__name__}: {e}"
        return report

    if len(raw) > MAX_BYTES:
        report["detail"] = f"refusing catalog larger than {MAX_BYTES} bytes"
        return report

    try:
        doc = validate(raw)
    except ValueError as e:
        report["detail"] = f"downloaded file rejected: {e}"
        return report

    try:
        cache_dir().mkdir(parents=True, exist_ok=True)
        tmp = cache_path().with_suffix(".tmp")
        with open(tmp, "wb") as handle:
            handle.write(raw)
        # Atomic: readers either see the whole old file or the whole new one, never a
        # partial write. os.replace is atomic within a filesystem.
        os.replace(tmp, cache_path())
    except OSError as e:
        report["detail"] = f"cannot write cache at {cache_path()}: {e}"
        return report

    _write_meta({
        "url": url,
        "etag": headers.get("ETag") or "",
        "last_modified": headers.get("Last-Modified") or "",
        "fetched_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "bytes": len(raw),
        "sha256": hashlib.sha256(raw).hexdigest(),
        "built_at": doc.get("built_at") or "",
        "source_commit": doc.get("source_commit") or "",
        "collections": len(doc["collections"]),
    })
    report.update(status="updated", detail=f"{len(raw)} bytes, "
                  f"{len(doc['collections'])} collections")
    return report


# --- background poller -----------------------------------------------------------------
# A daemon thread rather than an asyncio task: `fetch` is synchronous urllib, and a thread
# works identically under both transports without depending on the server's event loop or
# its lifespan API.
_POLLER = {"thread": None}
_POLLER_LOCK = threading.Lock()


def poll_interval() -> int:
    """Seconds between background checks. 0 disables polling."""
    try:
        return max(0, int(os.environ.get("LEGUMISTA_CATALOG_POLL", "86400")))
    except ValueError:
        return 86400


def start_poller(refresh_fn, *, interval: int = None, log=None) -> bool:
    """Start the background refresh thread. Returns False if polling is disabled.

    `refresh_fn()` performs the whole check-and-swap and returns a report dict — in
    practice `tools_catalog.refresh`. The poller owns only the schedule, so there is one
    code path for a refresh whether it was triggered by the clock or by the webhook.
    Idempotent: a second call is a no-op.
    """
    seconds = poll_interval() if interval is None else interval
    if seconds <= 0:
        return False
    with _POLLER_LOCK:
        if _POLLER["thread"] is not None:
            return True

        def loop():
            while True:
                time.sleep(seconds)
                try:
                    report = refresh_fn()
                    status, detail = report.get("status"), report.get("detail")
                    # Routine no-ops stay quiet; only a change or a problem is worth a
                    # line in a log that is otherwise silent for days at a time.
                    if log and status != "unchanged":
                        log(f"[*] catalog poll: {status}"
                            + (f" — {detail}" if detail else ""))
                except Exception as e:  # noqa: BLE001 - the poller must never die
                    if log:
                        log(f"[!] catalog poll failed: {type(e).__name__}: {e}")

        thread = threading.Thread(target=loop, name="legumista-catalog-poll",
                                  daemon=True)
        _POLLER["thread"] = thread
        thread.start()
        return True


def reset_poller() -> None:
    """Forget the poller handle. For tests; the daemon thread itself is not joined."""
    with _POLLER_LOCK:
        _POLLER["thread"] = None
