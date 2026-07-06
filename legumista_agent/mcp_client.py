#!/usr/bin/env python3
"""MCP client — connects to optional MCP servers and exposes their tools as `Tool`s.

Uses the official `mcp` Python SDK: spawn each server over stdio, then `initialize`,
`tools/list`, and `tools/call`. Tool names are namespaced `mcp__<server>__<tool>` and
sanitized to the OpenAI function-name charset so tools from different servers can't
collide.
"""
import asyncio
import os
import re
from contextlib import AsyncExitStack

from .tool import Tool

# NOTE: the `mcp` SDK is imported lazily inside __aenter__ so the native-tools agent
# works even when `mcp` isn't installed (it's only needed to connect to MCP servers).


def _safe_name(server: str, tool: str) -> str:
    # OpenAI function names: [a-zA-Z0-9_-], <=64 chars.
    return re.sub(r"[^a-zA-Z0-9_-]", "_", f"mcp__{server}__{tool}")[:64]


def _result_text(result) -> str:
    """Flatten an MCP CallToolResult into text (join text blocks; note non-text)."""
    parts = []
    for block in (getattr(result, "content", None) or []):
        if getattr(block, "type", None) == "text":
            parts.append(block.text)
        else:
            parts.append(f"[{getattr(block, 'type', 'non-text')} content omitted]")
    text = "\n".join(parts).strip()
    if getattr(result, "isError", False):
        return f"[tool error] {text or '(no detail)'}"
    return text or "(empty result)"


class MCPManager:
    """Async context manager holding open stdio sessions to each MCP server for its
    lifetime. `list_tools()` returns wrapped `Tool`s bound to their session."""

    def __init__(self, servers: dict):
        self._servers = servers            # {name: {command, args, env}}
        self._stack = AsyncExitStack()
        self._sessions = {}                # name -> ClientSession

    async def __aenter__(self):
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client
        for name, cfg in self._servers.items():
            params = StdioServerParameters(
                command=cfg["command"],
                args=list(cfg.get("args") or []),
                env={**os.environ, **(cfg.get("env") or {})},
            )
            read, write = await self._stack.enter_async_context(stdio_client(params))
            session = await self._stack.enter_async_context(ClientSession(read, write))
            await session.initialize()
            self._sessions[name] = session
        return self

    async def __aexit__(self, *exc):
        # MCP stdio servers often don't exit promptly on close (the transport only
        # sends an abort), which can hang teardown. Bound it — we're done with the
        # servers, and the OS reaps any stragglers when the process exits.
        try:
            await asyncio.wait_for(self._stack.aclose(), timeout=10)
        except Exception:  # noqa: BLE001 - best-effort shutdown; never hang the caller
            pass

    async def list_tools(self) -> list:
        tools = []
        for server, session in self._sessions.items():
            resp = await session.list_tools()
            for t in resp.tools:
                tools.append(self._wrap(server, session, t))
        return tools

    @staticmethod
    def _wrap(server, session, t) -> Tool:
        schema = t.inputSchema or {"type": "object", "properties": {}}
        ann = getattr(t, "annotations", None)
        read_only = bool(getattr(ann, "readOnlyHint", False)) if ann else False

        async def run(args, _session=session, _tool=t.name):
            result = await _session.call_tool(_tool, args or {})
            return _result_text(result)

        return Tool(name=_safe_name(server, t.name),
                    description=t.description or "",
                    parameters=schema, read_only=read_only, run=run)
