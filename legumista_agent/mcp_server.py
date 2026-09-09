#!/usr/bin/env python3
"""FastMCP server — expose legumista's native research tools over the Model Context
Protocol, so any MCP-speaking client (Claude Desktop, an IDE, another agent) can drive
the same literature search (paper_search, europepmc_search, openalex_by_doi), read_paper,
NCBI datasets+EDirect, web search, and genomics tools the `legumista research` agent uses.

We already model every tool as a `legumista_agent.tool.Tool` (name + JSON-schema
parameters + read-only flag + async `run(args) -> str`). This module bridges each one
to a FastMCP `Tool` subclass whose `run` calls back into the legumista handler and wraps
the text result in a spec-compliant `ToolResult` (a single text content block). The
tool's read-only flag becomes the MCP `readOnlyHint` annotation.

`fastmcp` is a dependency of legumista; it is imported inside `build_server` (not at
module top) so merely importing this module stays cheap until you actually serve. The
server is started only via `legumista mcp` (there is no separate server binary — the whole
tool is one `legumista` command).
"""


# Tools we implement but do NOT serve over MCP: an MCP client essentially always has
# file tools of its own (Claude Code has Read/Grep; so do the IDE integrations), and ours
# are strictly more restricted (workspace sandbox, size caps). Shipping both only makes
# the model choose between two ways to read the same file. The internal `legumista
# research` agent (runtime.py) keeps them — it has no host to borrow file tools from.
# That asymmetry is deliberate: the server assumes a capable client, the agent does not.
_CLIENT_PROVIDED = {"read_file", "grep"}


# Registry mapping tool name -> the legumista async handler. Kept module-level (not a
# field on the pydantic FastMCP Tool model) so the bridge subclass stays a plain schema
# carrier and the handler lookup can't collide with pydantic's field machinery.
_HANDLERS: dict = {}


def _normalize_params(schema: dict) -> dict:
    """Guarantee a well-formed `{"type":"object","properties":{...}}` envelope with
    `additionalProperties:false` — the same normalization `Tool.to_openai()` applies for
    reliable function calling, so the MCP-advertised schema matches the agent's."""
    params = dict(schema or {})
    params.setdefault("type", "object")
    params.setdefault("properties", {})
    params.setdefault("additionalProperties", False)
    return params


def _make_bridge_class():
    """Build the FastMCP Tool subclass lazily (needs fastmcp imported first)."""
    # Import both from the `fastmcp.tools` package root, not `fastmcp.tools.tool`:
    # fastmcp 4.0 removed that submodule, and the package root exports both names on
    # 3.x and 4.x alike. The dependency is floor-pinned (fastmcp>=2.9), so a fresh
    # install picks up whatever is current — this import must not care which.
    from fastmcp.tools import Tool as FastMCPTool, ToolResult
    from mcp.types import TextContent

    class _BridgeTool(FastMCPTool):
        """A FastMCP tool that delegates execution to a legumista `Tool.run` handler."""

        async def run(self, arguments: dict) -> ToolResult:
            text = await _HANDLERS[self.name](arguments or {})
            return ToolResult(content=[TextContent(type="text", text=str(text))])

    return _BridgeTool


def build_server(name: str = "legumista", *, allow_write: bool = False):
    """Assemble a FastMCP server exposing legumista's native + local tools.

    Returns the `FastMCP` instance (not yet running). Every tool is registered with its
    original name, description, and JSON-schema parameters. A tool's read-only status
    becomes the MCP `readOnlyHint` annotation. The MCP server has no permission gate, so
    `allow_write` is the sole control over the write-capable genomics tools: when False
    (default) their write operations fail closed and only read subcommands run."""
    from fastmcp import FastMCP

    import config
    from .tools_catalog import catalog_tools
    from .tools_lis import lis_tools
    from .tools_local import local_read_tools
    from .tools_mine import mine_tools
    from .tools_native import native_tools
    from .tools_pysam import bio_tools

    Bridge = _make_bridge_class()
    # The native-tools usage doctrine (prompts/tools_native.md) doubles as server-level
    # `instructions` — client-facing guidance on how to use the toolset. Falls back to a
    # one-liner when the project ships no such prompt.
    instructions = config.tools_spec() or (
        "Read-only literature-research tools: scholarly search (OpenAlex/Crossref/"
        "Europe PMC), open-access full-text read/grep, NCBI datasets & EDirect, "
        "keyless web search, and legume genomics data.")
    # When a catalog is loaded, append a ~1,400-token projection of it. It answers the
    # exploratory questions ("which genera exist", "what does this species have", "what
    # is soybean called here") that would otherwise each cost a tool call, and it is the
    # only place absence is visible. Omitted entirely without a catalog, so the
    # instructions never advertise knowledge the server does not have.
    from .tools_catalog import catalog_map
    resident_map = catalog_map()
    if resident_map:
        instructions = f"{instructions}\n\n---\n\n{resident_map}"

    server = FastMCP(name=name, instructions=instructions)

    _HANDLERS.clear()
    for tool in (local_read_tools() + native_tools() + lis_tools() + mine_tools()
                 + catalog_tools() + bio_tools(allow_write=allow_write)):
        if tool.name in _CLIENT_PROVIDED:
            continue
        _HANDLERS[tool.name] = tool.run
        server.add_tool(Bridge(
            name=tool.name,
            description=tool.description,
            parameters=_normalize_params(tool.parameters),
            annotations={"readOnlyHint": bool(tool.read_only)},
        ))
    return server


def serve(transport: str = "stdio", host: str = "127.0.0.1", port: int = 8000,
          show_banner: bool = False, *, allow_write: bool = False) -> None:
    """Build and run the server (blocking). `transport` is 'stdio' (default; how MCP
    clients spawn a server) or 'http' (a long-running HTTP endpoint on host:port).
    `allow_write` enables the write-capable genomics tools' write operations."""
    server = build_server(allow_write=allow_write)
    if transport == "stdio":
        server.run(transport="stdio", show_banner=show_banner)
    else:
        server.run(transport=transport, host=host, port=port, show_banner=show_banner)
