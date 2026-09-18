"""Catalog fetch/cache tests.

The catalog is no longer vendored, so the download path is now load-bearing: it decides
whether the six lis_* tools can answer at all. Everything here is offline — the only
network entry point (`_open`) is stubbed.

The properties worth pinning are the ones whose failure is silent rather than loud. A bad
publish must not replace a good catalog; a conditional request must actually carry its
validators, or a 24-hour poll becomes a 24-hour download; and a swap must be all-or-nothing.
"""
import json
import urllib.error

import pytest

from legumista_agent import catalog_source as S

GOOD = {
    "schema": 1,
    "built_at": "2026-09-09T14:39:54Z",
    "source_commit": "04d9d86e",
    "stats": {"collections": 1},
    "taxa": {},
    "collections": [{"path": "Glycine/max/genomes/X", "id": "X", "type": "genomes",
                     "genus": "Glycine", "species": "max", "files": []}],
}


class _Resp:
    """Minimal stand-in for the urlopen context manager."""

    def __init__(self, body: bytes, headers: dict = None):
        self._body = body
        self.headers = headers or {}

    def read(self, n=-1):
        return self._body if n is None or n < 0 else self._body[:n]

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _stub(monkeypatch, body=None, headers=None, raising=None):
    """Replace the guarded opener. Returns the list of Requests it was handed."""
    seen = []

    def fake(request, timeout):
        seen.append(request)
        if raising is not None:
            raise raising
        return _Resp(body if body is not None else json.dumps(GOOD).encode(), headers)

    monkeypatch.setattr(S, "_open", fake)
    return seen


# --- validate: the guard that keeps a plausible-but-wrong file out --------------------
def test_validate_accepts_a_real_catalog():
    assert S.validate(json.dumps(GOOD).encode())["stats"]["collections"] == 1


@pytest.mark.parametrize("payload, because", [
    (b"<html><body>Sign in to GitHub</body></html>", "an HTML login page"),
    (b'{"collections": [], "stats": {}}', "an empty collection list"),
    (b'{"collections": [{}]}', "no stats object"),
    (b'["not", "an", "object"]', "a JSON array"),
    (b'{"collections": [{"id": "x"}], "stats": {}', "a truncated download"),
])
def test_validate_rejects_plausible_junk(payload, because):
    """Each of these parses as *something*; none is a catalog. Accepting one would swap
    it in and leave every lis_* tool confidently answering from nothing."""
    with pytest.raises(ValueError):
        S.validate(payload)


# --- fetch ----------------------------------------------------------------------------
def test_fetch_writes_cache_and_records_validators(monkeypatch):
    _stub(monkeypatch, headers={"ETag": '"abc"', "Last-Modified": "Wed, 09 Sep 2026 00:00:00 GMT"})
    report = S.fetch()
    assert report["status"] == "updated"
    assert json.loads(S.cache_path().read_text())["source_commit"] == "04d9d86e"
    meta = S.read_meta()
    assert meta["etag"] == '"abc"' and meta["collections"] == 1
    assert meta["url"] == S.catalog_url()


def test_fetch_sends_conditional_headers_on_the_second_call(monkeypatch):
    """Without these the poller re-downloads 2.4 MB every interval instead of taking a
    304. The headers must be present AND carry the stored values."""
    _stub(monkeypatch, headers={"ETag": '"v1"', "Last-Modified": "Wed, 09 Sep 2026 00:00:00 GMT"})
    S.fetch()
    seen = _stub(monkeypatch, headers={"ETag": '"v1"'})
    S.fetch()
    sent = {k.lower(): v for k, v in seen[0].header_items()}
    assert sent["If-none-match".lower()] == '"v1"'
    assert sent["If-modified-since".lower()] == "Wed, 09 Sep 2026 00:00:00 GMT"


def test_force_skips_the_conditional_headers(monkeypatch):
    """The webhook forces: a publisher saying 'it changed' should not be argued with by
    a stale ETag."""
    _stub(monkeypatch, headers={"ETag": '"v1"'})
    S.fetch()
    seen = _stub(monkeypatch, headers={"ETag": '"v2"'})
    S.fetch(force=True)
    sent = {k.lower() for k, _ in seen[0].header_items()}
    assert "if-none-match" not in sent


def test_validators_are_dropped_when_the_url_changes(monkeypatch, tmp_path):
    """An ETag belongs to a URL. Re-pointing the server at a different catalog must not
    send the old host's validator and risk a bogus 304."""
    _stub(monkeypatch, headers={"ETag": '"v1"'})
    S.fetch()
    monkeypatch.setenv("LEGUMISTA_CATALOG_URL", "https://example.org/other.json")
    seen = _stub(monkeypatch)
    S.fetch()
    sent = {k.lower() for k, _ in seen[0].header_items()}
    assert "if-none-match" not in sent


def test_304_reports_unchanged_and_leaves_the_cache_alone(monkeypatch):
    _stub(monkeypatch, headers={"ETag": '"v1"'})
    S.fetch()
    before = S.cache_path().read_bytes()
    _stub(monkeypatch, raising=urllib.error.HTTPError(
        S.catalog_url(), 304, "Not Modified", {}, None))
    report = S.fetch()
    assert report["status"] == "unchanged"
    assert S.cache_path().read_bytes() == before


def test_a_bad_publish_does_not_clobber_a_good_cache(monkeypatch):
    """The property the whole validate step exists for. A catalog that 500s, or serves an
    error page, must leave the server on the last catalog that worked."""
    _stub(monkeypatch)
    S.fetch()
    good = S.cache_path().read_bytes()

    _stub(monkeypatch, body=b"<html>502 Bad Gateway</html>")
    assert S.fetch(force=True)["status"] == "error"
    assert S.cache_path().read_bytes() == good

    _stub(monkeypatch, raising=urllib.error.HTTPError(
        S.catalog_url(), 500, "Server Error", {}, None))
    report = S.fetch(force=True)
    assert report["status"] == "error" and "500" in report["detail"]
    assert S.cache_path().read_bytes() == good


def test_oversized_download_is_refused(monkeypatch):
    monkeypatch.setattr(S, "MAX_BYTES", 32)
    _stub(monkeypatch, body=json.dumps(GOOD).encode())
    report = S.fetch()
    assert report["status"] == "error" and "larger than" in report["detail"]
    assert not S.cache_path().exists()


def test_network_failure_is_a_report_not_an_exception(monkeypatch):
    """Startup and the poller both call this; an exception here would take the server
    down over a transient DNS failure."""
    _stub(monkeypatch, raising=OSError("name resolution failed"))
    report = S.fetch()
    assert report["status"] == "error" and "OSError" in report["detail"]


def test_corrupt_meta_is_survivable(monkeypatch):
    """A half-written sidecar must degrade to an unconditional fetch, not a crash."""
    S.cache_dir().mkdir(parents=True, exist_ok=True)
    S._meta_path().write_text("{not json", encoding="utf-8")
    assert S.read_meta() == {}
    _stub(monkeypatch)
    assert S.fetch()["status"] == "updated"


# --- poller ---------------------------------------------------------------------------
def test_poller_is_disabled_by_a_zero_interval(monkeypatch):
    monkeypatch.setenv("LEGUMISTA_CATALOG_POLL", "0")
    S.reset_poller()
    assert S.start_poller(lambda **_: {}) is False


def test_poller_runs_the_refresh_and_survives_its_exceptions(monkeypatch):
    """A refresh that raises must not kill the thread — otherwise one transient failure
    silently ends auto-updating for the life of the process."""
    import threading
    calls, done = [], threading.Event()

    def boom():
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("transient")
        done.set()
        return {"status": "unchanged", "detail": ""}

    S.reset_poller()
    assert S.start_poller(boom, interval=0.01) is True
    assert done.wait(timeout=5), "poller stopped after the first exception"
    S.reset_poller()


def test_poll_interval_defaults_to_a_day_and_tolerates_garbage(monkeypatch):
    monkeypatch.delenv("LEGUMISTA_CATALOG_POLL", raising=False)
    assert S.poll_interval() == 86400
    monkeypatch.setenv("LEGUMISTA_CATALOG_POLL", "not-a-number")
    assert S.poll_interval() == 86400


def test_non_http_schemes_are_refused(monkeypatch):
    """The catalog URL is operator config, so private/loopback hosts are deliberately
    allowed (an internal mirror is legitimate). The scheme is not: a file:// or ftp://
    URL must never be read as a catalog."""
    for url in ("file:///etc/passwd", "ftp://example.org/catalog.json", "notaurl"):
        monkeypatch.setenv("LEGUMISTA_CATALOG_URL", url)
        report = S.fetch()
        assert report["status"] == "error"
        assert "scheme" in report["detail"] or "unusable" in report["detail"], report


def test_a_localhost_mirror_is_allowed():
    """Regression: reusing the model-facing SSRF guard here blocked every internal
    mirror, which is a supported deployment."""
    S._check_scheme("http://127.0.0.1:8099/catalog.json")
    S._check_scheme("https://catalog.internal.example/catalog.json")
