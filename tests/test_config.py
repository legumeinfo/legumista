"""Config-resolution tests.

`config` snapshots WORKSPACE at import time, so each scenario runs in a fresh subprocess
with a controlled environment rather than relying on import order within this process.

What is left of config after the pipeline was removed is three things the served tools
depend on, and each has a way of failing quietly: a WORKSPACE that ignores its override
would sandbox the genomics tools to the wrong tree, an unset contact_email would send
anonymous traffic to OpenAlex, and an unfound tools_spec would serve a server with no
instructions.
"""
import json
import os
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

_PROBE = (
    "import config, json;"
    "print(json.dumps({"
    "'workspace': config.WORKSPACE,"
    "'contact_email': config.contact_email(),"
    "'spec_len': len(config.tools_spec()),"
    "}))"
)


def _probe(env_overrides, cwd):
    env = dict(os.environ)
    env["PYTHONPATH"] = str(REPO_ROOT) + os.pathsep + env.get("PYTHONPATH", "")
    env.pop("LEGUMISTA_HOME", None)
    env.pop("LEGUMISTA_CONTACT_EMAIL", None)
    env.pop("LEGUMISTA_PROMPTS_DIR", None)
    env.update(env_overrides)
    proc = subprocess.run([sys.executable, "-c", _PROBE], capture_output=True,
                          text=True, cwd=str(cwd), env=env)
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


def test_workspace_defaults_to_the_launch_directory(tmp_path):
    """With no override, the sandbox root is where the server was started — this is what
    lets one installed server be pointed at different working trees by cd-ing."""
    out = _probe({}, cwd=tmp_path)
    assert out["workspace"] == os.path.realpath(str(tmp_path)) or \
           out["workspace"] == str(tmp_path)


def test_legumista_home_overrides_the_workspace(tmp_path):
    """`legumista mcp -C DIR` sets LEGUMISTA_HOME; if it were ignored the genomics tools
    would confine paths to the wrong tree while appearing to work."""
    out = _probe({"LEGUMISTA_HOME": str(tmp_path)}, cwd=REPO_ROOT)
    assert out["workspace"] == str(tmp_path)


def test_contact_email_default_is_obviously_unset(tmp_path):
    """The polite-pool mailto must default to something visibly fake, so an unset value
    shows up in a request log rather than impersonating a real address."""
    out = _probe({}, cwd=tmp_path)
    assert out["contact_email"] == "you@example.org"
    out = _probe({"LEGUMISTA_CONTACT_EMAIL": "me@example.edu"}, cwd=tmp_path)
    assert out["contact_email"] == "me@example.edu"


def test_tools_spec_is_found_from_an_unrelated_directory(tmp_path):
    """tools_spec ships as package data and becomes the server's MCP `instructions`.
    Resolving it relative to the launch directory would serve an empty instruction set
    to every client that starts the server from somewhere else."""
    out = _probe({}, cwd=tmp_path)
    assert out["spec_len"] > 1000, "packaged tools_native.md should have been found"


def test_empty_contact_email_falls_back_to_the_default(tmp_path):
    """An env var set to the empty string is not the same as unset, but for a polite-pool
    mailto it must behave as if it were — an empty mailto is a malformed request, not a
    quieter one."""
    out = _probe({"LEGUMISTA_CONTACT_EMAIL": ""}, cwd=tmp_path)
    assert out["contact_email"] == "you@example.org"


# --- user agent -------------------------------------------------------------------------
def test_the_user_agent_carries_the_package_version(monkeypatch):
    """Five hard-coded agents had drifted to three different versions (0.2, 1.0, and the
    package's 0.3.0). Every request now reads the one pyproject.toml sets."""
    import tomllib
    import config
    monkeypatch.setenv("LEGUMISTA_CONTACT_EMAIL", "curator@example.org")
    expected = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text())["project"]["version"]
    assert config.version() == expected
    assert config.user_agent() == (f"legumista/{expected} "
                                   "(+https://github.com/legumeinfo/legumista)")
    assert config.user_agent("legumista-report").startswith(f"legumista-report/{expected} ")
    assert config.user_agent(mailto=True).endswith(
        "legumista; mailto:curator@example.org)")


def test_the_version_falls_back_to_package_metadata_when_installed(monkeypatch, tmp_path):
    """Installed from a wheel, no pyproject.toml sits beside config.py."""
    from importlib import metadata
    import config
    monkeypatch.setattr(config, "PKG_ROOT", str(tmp_path))
    monkeypatch.setattr(metadata, "version", lambda name: "9.9.9")
    config.version.cache_clear()
    try:
        assert config.version() == "9.9.9"
    finally:
        config.version.cache_clear()


def test_no_module_hard_codes_a_version_in_its_user_agent():
    import re
    root = REPO_ROOT / "legumista_agent"
    stale = [f"{p.name}:{n}" for p in root.glob("*.py")
             for n, line in enumerate(p.read_text().splitlines(), 1)
             if re.search(r"legumista(-report)?/\d", line)]
    assert not stale, f"hard-coded User-Agent versions: {stale}"
