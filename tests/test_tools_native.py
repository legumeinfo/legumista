"""Native-tool sandbox tests — the workspace confinement guard (`_sandbox_path` /
`_is_sensitive`) and the local `grep`. No network, no model calls."""
import os

import config
from legumista_agent.tools_native import _grep, _is_sensitive, _sandbox_path


def test_workspace_root_is_not_sensitive():
    """The workspace root itself must be allowed. `os.path.relpath(root, root)` is a
    bare '.', which used to be mistaken for a hidden component and refused — regression
    for a default-path grep/read of the project refusing itself."""
    assert _is_sensitive(config.WORKSPACE) is False
    rp, err = _sandbox_path(".")
    assert err is None and rp == os.path.realpath(config.WORKSPACE)


def test_grep_default_path_searches_workspace(tmp_path, monkeypatch):
    """`grep` with no `path` defaults to '.' and must search the workspace, not refuse."""
    monkeypatch.setattr(config, "WORKSPACE", str(tmp_path))
    (tmp_path / "note.md").write_text("alpha beta gamma\n", encoding="utf-8")
    out = _grep({"pattern": "beta", "glob": "**/*.md"})
    assert "note.md" in out and "beta" in out
    assert not out.startswith("error")


def test_sandbox_still_refuses_hidden_and_secret_files():
    """The fix must not weaken the guard: dotfiles, dotdirs, and secret-like names stay
    refused, and paths escaping the workspace are still rejected."""
    for p in (".env", ".git/config", "knowledge/../.hidden",
              "secrets.key", "sub/.ssh/id_rsa"):
        _, err = _sandbox_path(p)
        assert err, f"expected {p!r} to be refused"
    _, err = _sandbox_path("/etc/passwd")
    assert err, "paths outside the workspace must be refused"
