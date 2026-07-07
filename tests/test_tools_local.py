"""Local read-tool tests — `read_file` (workspace-sandboxed text read) and `web_fetch`
(the non-network validation paths + HTML stripping). File reads use a tmp workspace; no
network is performed."""
import asyncio

import config
from legumista_agent import tools_local as L


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def test_local_read_tools_shape():
    tools = {t.name: t for t in L.local_read_tools()}
    assert set(tools) == {"read_file", "web_fetch"}
    assert all(t.read_only for t in tools.values())


def test_read_file_returns_contents(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "WORKSPACE", str(tmp_path))
    (tmp_path / "note.txt").write_text("hello world\n")
    assert _run(L._read({"path": "note.txt"})) == "hello world\n"
    # the `file_path` alias is accepted too
    assert _run(L._read({"file_path": "note.txt"})) == "hello world\n"


def test_read_file_missing_and_directory(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "WORKSPACE", str(tmp_path))
    (tmp_path / "sub").mkdir()
    assert "no such file" in _run(L._read({"path": "nope.txt"}))
    assert "is a directory" in _run(L._read({"path": "sub"}))


def test_read_file_rejects_binary(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "WORKSPACE", str(tmp_path))
    (tmp_path / "b.bin").write_bytes(b"\x00\x01\x02hello")
    assert "looks binary" in _run(L._read({"path": "b.bin"}))


def test_read_file_sandboxed_to_workspace(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "WORKSPACE", str(tmp_path))
    assert "outside the project workspace" in _run(L._read({"path": "/etc/passwd"}))


def test_read_file_truncates_to_cap(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "WORKSPACE", str(tmp_path))
    monkeypatch.setattr(L, "MAX_CHARS", 10)
    (tmp_path / "big.txt").write_text("x" * 50)
    out = _run(L._read({"path": "big.txt"}))
    assert out.startswith("x" * 10) and "truncated" in out


def test_web_fetch_rejects_non_http():
    assert _run(L._web_fetch({"url": "ftp://example.org/x"})) == "error: 'url' must be an http(s) URL"
    assert _run(L._web_fetch({"url": ""})) == "error: 'url' must be an http(s) URL"


def test_strip_html_removes_scripts_tags_and_whitespace():
    html = "<script>evil()</script><style>x{}</style><p>Hi   there</p>\n\n\n<b>bye</b>"
    out = L._strip_html(html)
    assert "evil" not in out and "x{}" not in out
    assert "Hi there" in out and "bye" in out
    assert "<" not in out and ">" not in out
