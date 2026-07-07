#!/usr/bin/env python3
"""Blocking entry point for tool-using research: run the agent loop over the full
tool surface (native tools always + any MCP servers) to a final answer."""
from .permissions import Permissions
from .runtime import AgentRuntime


def research(topic, *, servers=None, system=None, model=None, max_turns=8,
             permissions=None, on_event=None, timeout=None, allow_write=False):
    """Run the research agent to completion. Native tools (paper search, OpenAlex,
    grep, read, web, and the pysam genomics tools) are always available; MCP servers in
    `servers` are added on top. `allow_write` enables the write-capable tools' write
    operations and defaults the permission mode to read_write. Blocking. Returns the
    run_agent result dict."""
    rt = AgentRuntime(servers or {}, allow_write=allow_write).open()
    try:
        if on_event:
            on_event({"type": "tools_ready", "tools": [t.name for t in rt.tools]})
        default_perms = Permissions(mode="read_write" if allow_write else "read_only")
        return rt.step(user=topic, system=system, model=model, max_turns=max_turns,
                       permissions=permissions or default_perms,
                       timeout=timeout, on_event=on_event)
    finally:
        rt.close()
