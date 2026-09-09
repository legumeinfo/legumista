"""Local read-tool tests — `web_fetch`'s non-network validation paths and its HTML
stripping. No network is performed."""
import asyncio

from legumista_agent import tools_local as L


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def test_local_read_tools_shape():
    """web_fetch is all that remains here — read_file went with the agent loop."""
    tools = {t.name: t for t in L.local_read_tools()}
    assert set(tools) == {"web_fetch"}
    assert all(t.read_only for t in tools.values())


def test_web_fetch_rejects_non_http():
    assert _run(L._web_fetch({"url": "ftp://example.org/x"})) == "error: 'url' must be an http(s) URL"
    assert _run(L._web_fetch({"url": ""})) == "error: 'url' must be an http(s) URL"


def test_strip_html_removes_scripts_tags_and_whitespace():
    html = "<script>evil()</script><style>x{}</style><p>Hi   there</p>\n\n\n<b>bye</b>"
    out = L._strip_html(html)
    assert "evil" not in out and "x{}" not in out
    assert "Hi there" in out and "bye" in out
    assert "<" not in out and ">" not in out
