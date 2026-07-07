"""Distribution-coherence tests — invariants that silently drift on a release and break
publishing: the official MCP Registry manifest (server.json) must stay in lockstep with
the package, and the registry's namespace-verification marker must match. Pure file
checks: no network."""
import json
import pathlib
import tomllib

ROOT = pathlib.Path(__file__).resolve().parent.parent


def _pyproject():
    with open(ROOT / "pyproject.toml", "rb") as f:
        return tomllib.load(f)


def test_server_json_matches_package():
    """server.json must point at THIS package with a version identical to pyproject —
    the exact pair that drifts when the version is bumped without updating the manifest,
    which the registry then rejects."""
    proj = _pyproject()["project"]
    doc = json.loads((ROOT / "server.json").read_text())
    assert doc["version"] == proj["version"]

    pkg = next(p for p in doc["packages"] if p["registryType"] == "pypi")
    assert pkg["identifier"] == proj["name"]
    assert pkg["version"] == proj["version"]
    assert pkg["transport"]["type"] == "stdio"
    # everything ships in one package -> invocation is `uvx legumista mcp`, no `--from extra`
    assert any(a.get("value") == "mcp" for a in pkg["packageArguments"])
    assert not any(a.get("name") == "--from" for a in pkg.get("runtimeArguments", []))


def test_readme_namespace_marker_matches_manifest():
    """The registry verifies the PyPI namespace via an `mcp-name:` marker in the README
    (the PyPI description); it must match server.json's name or verification fails."""
    name = json.loads((ROOT / "server.json").read_text())["name"]
    assert f"mcp-name: {name}" in (ROOT / "README.md").read_text()
