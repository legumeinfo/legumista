"""tools_native pure-helper tests — the DOI/abstract/formatting utilities and the SSRF
guard, none of which need the network (the SSRF checks use literal IPs, which resolve
locally). Complements test_tools_native.py, which covers the workspace sandbox + grep."""
import pytest

from legumista_agent import tools_native as N
from legumista_agent.tools_native import BlockedURLError, _validate_url


# --- identifier / text helpers -------------------------------------------------------
def test_norm_doi_strips_resolver_prefix_and_lowercases():
    assert N._norm_doi("https://doi.org/10.1/AbC") == "10.1/abc"
    assert N._norm_doi("http://dx.doi.org/10.1/X") == "10.1/x"
    assert N._norm_doi("10.1/Plain") == "10.1/plain"
    assert N._norm_doi("") == "" and N._norm_doi(None) == ""


def test_clean_abstract_strips_jats_and_unescapes():
    assert N._clean_abstract("<jats:p>Hello &amp; bye</jats:p>") == "Hello & bye"
    assert N._clean_abstract("  multiple    spaces\n\tcollapse ") == "multiple spaces collapse"
    assert N._clean_abstract("") == ""


def test_reconstruct_abstract_orders_by_position():
    inv = {"quick": [1], "the": [0], "fox": [3], "brown": [2]}
    assert N._reconstruct_abstract(inv) == "the quick brown fox"
    assert N._reconstruct_abstract({}) == "" and N._reconstruct_abstract(None) == ""


def test_fmt_renders_results_with_overflow_and_fallbacks():
    assert N._fmt([]) == "No results."
    out = N._fmt([{
        "title": "A Study", "year": 2021, "doi": "10.1/x", "venue": "Nature",
        "authors": ["A", "B", "C", "D", "E"], "abstract": "we did science",
    }])
    assert "[1] A Study (2021)" in out
    assert "doi:10.1/x" in out and "Nature" in out
    assert "… (+1)" in out                    # 5 authors -> first 4 + overflow marker
    assert "we did science" in out
    # missing title/year fall back rather than error
    assert "(untitled)" in N._fmt([{"authors": []}])
    assert "n.d." in N._fmt([{"authors": []}])


def test_cap_truncates_over_limit(monkeypatch):
    monkeypatch.setattr(N, "MAX_CHARS", 8)
    assert N._cap("short") == "short"
    capped = N._cap("x" * 20)
    assert capped.startswith("x" * 8) and "truncated to 8 chars" in capped


# --- SSRF guard: one case per blocking branch, plus a public control -----------------
@pytest.mark.parametrize("ip,blocked", [
    ("127.0.0.1", True),          # loopback
    ("10.0.0.1", True),           # RFC-1918 private
    ("169.254.169.254", True),    # link-local (cloud metadata endpoint)
    ("::1", True),                # IPv6 loopback
    ("::ffff:127.0.0.1", True),   # IPv4-mapped loopback (must be unwrapped, then blocked)
    ("garbage", True),            # unparseable -> refuse
    ("8.8.8.8", False),           # public -> allowed
])
def test_ip_is_blocked(ip, blocked):
    assert N._ip_is_blocked(ip) is blocked


def test_validate_url_enforces_public_http_only():
    """The URL guard: non-http(s) scheme, missing host, and hosts resolving to private/
    loopback/metadata addresses all raise; a public http(s) literal passes."""
    for bad in ("ftp://example.org/x", "http://",               # scheme / no host
                "http://127.0.0.1/x", "http://169.254.169.254/latest/meta-data/"):
        with pytest.raises(BlockedURLError):
            _validate_url(bad)
    _validate_url("http://8.8.8.8/")           # public literal IP -> no raise (no DNS needed)
