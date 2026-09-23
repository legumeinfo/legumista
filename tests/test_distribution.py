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


def test_dockerfile_smoke_check_names_only_tools_that_exist():
    """The image's build-time smoke check asserts a hardcoded set of tool names. Renaming
    a tool without updating it fails the Docker build long after the test suite is green —
    which is exactly how `legumemine_gene_orthologs` -> `legumemine_gene_family_members`
    broke it. Catch the drift here instead.

    Only one direction is checked: every name the Dockerfile asserts must be served. The
    reverse is deliberately not required, because the list is a hand-picked smoke sample,
    not an inventory."""
    import re
    from legumista_agent.tools_catalog import catalog_tools
    from legumista_agent.tools_lis import lis_tools
    from legumista_agent.tools_local import local_read_tools
    from legumista_agent.tools_mine import mine_tools
    from legumista_agent.tools_native import native_tools
    from legumista_agent.tools_pysam import bio_tools
    from legumista_agent.tools_verify import verify_tools

    served = {t.name for t in (local_read_tools() + native_tools() + lis_tools()
                               + mine_tools() + catalog_tools() + bio_tools()
                               + verify_tools())}
    text = (ROOT / "Dockerfile").read_text()
    block = text.split("missing = {", 1)[1].split("} \\", 1)[0]
    asserted = set(re.findall(r"'([a-z0-9_]+)'", block))
    assert asserted, "could not parse the Dockerfile's tool assertion"
    assert asserted <= served, f"Dockerfile asserts tools that are not served: {sorted(asserted - served)}"
