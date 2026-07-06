#!/usr/bin/env python3
"""Blocking entry point for tool-using research: run the agent loop over the full
tool surface (native tools always + any MCP servers) to a final answer."""
from .permissions import Permissions
from .runtime import AgentRuntime


def research(topic, *, servers=None, system=None, model=None, max_turns=8,
             permissions=None, on_event=None, timeout=None):
    """Run the research agent to completion. Native tools (paper search, OpenAlex,
    grep, read, web) are always available; MCP servers in `servers` are added on top.
    Blocking. Returns the run_agent result dict."""
    rt = AgentRuntime(servers or {}).open()
    try:
        if on_event:
            on_event({"type": "tools_ready", "tools": [t.name for t in rt.tools]})
        return rt.step(user=topic, system=system, model=model, max_turns=max_turns,
                       permissions=permissions or Permissions(mode="read_only"),
                       timeout=timeout, on_event=on_event)
    finally:
        rt.close()
