#!/usr/bin/env python3
"""AgentRuntime — a synchronous bridge to the async agent loop + MCP sessions.

The pipelines (orchestrator.py / ideation.py) are synchronous, but MCP sessions and
the agent loop are async and must stay open across many calls. AgentRuntime runs one
asyncio event loop on a background thread, opens the MCP sessions on it once, and
exposes a blocking `.step()` so the sync phases can take an agentic turn-loop without
becoming async themselves. All MCP interaction stays on the one background loop.

Usage:
    with AgentRuntime(servers) as rt:          # opens MCP + assembles tools
        result = rt.step(system=..., user=..., max_turns=8)   # blocking
"""
import asyncio
import threading

from .loop import run_agent
from .mcp_client import MCPManager
from .tools_lis import lis_tools
from .tools_local import local_read_tools
from .tools_mine import mine_tools
from .tools_native import native_tools
from .tools_pysam import bio_tools


class AgentRuntime:
    def __init__(self, servers: dict = None, *, include_local: bool = True,
                 extra_tools: list = None, allow_write: bool = False):
        self._servers = servers or {}
        self._include_local = include_local
        self._extra = list(extra_tools or [])
        # Whether write-capable native tools (the pysam samtools/bcftools dispatchers,
        # tabix_index) expose their write operations. The permission gate is the primary
        # control; this makes the tools themselves fail closed unless writes are enabled.
        self._allow_write = allow_write
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._run_loop, daemon=True)
        self._mgr = None
        self.tools = []

    # --- background event loop ---
    def _run_loop(self):
        asyncio.set_event_loop(self._loop)
        self._loop.run_forever()

    def _submit(self, coro):
        """Run a coroutine on the background loop and block for its result."""
        return asyncio.run_coroutine_threadsafe(coro, self._loop).result()

    # --- lifecycle ---
    def open(self):
        self._thread.start()

        async def _setup():
            tools = []
            if self._servers:
                self._mgr = MCPManager(self._servers)
                await self._mgr.__aenter__()
                tools += await self._mgr.list_tools()
            if self._include_local:
                tools += (local_read_tools() + native_tools() + lis_tools()
                          + mine_tools() + bio_tools(allow_write=self._allow_write))
            return tools + self._extra

        self.tools = self._submit(_setup())
        return self

    def step(self, *, system, user, max_turns, model=None, permissions=None,
             temperature=None, timeout=None, on_event=None):
        """Take one agentic turn-loop (blocking). Returns run_agent's result dict."""
        return self._submit(run_agent(
            user=user, tools=self.tools, system=system, model=model, max_turns=max_turns,
            permissions=permissions, temperature=temperature, timeout=timeout,
            on_event=on_event))

    def close(self):
        async def _teardown():
            if self._mgr is not None:
                await self._mgr.__aexit__(None, None, None)
        try:
            self._submit(_teardown())
        except Exception:  # noqa: BLE001 - best-effort MCP shutdown
            pass
        self._loop.call_soon_threadsafe(self._loop.stop)
        self._thread.join(timeout=10)

    def __enter__(self):
        return self.open()

    def __exit__(self, *exc):
        self.close()


def open_runtime_or_none(label: str = "", log=print):
    """Open an AgentRuntime with the full toolset. Native tools (paper search, OpenAlex,
    grep, read, web) are always available; MCP servers (if configured in .mcp.json) are
    added on top. Returns None only if the runtime genuinely cannot be brought up, so a
    tool-setup failure degrades to a single completion instead of breaking the run."""
    import config
    try:
        rt = AgentRuntime(config.mcp_servers()).open()   # servers may be empty
        shown = ", ".join(t.name for t in rt.tools[:6])
        log(f"agentic mode [{label}]: {len(rt.tools)} tools ({shown}"
            f"{' …' if len(rt.tools) > 6 else ''}), up to {config.agent_max_turns()} turns/step")
        return rt
    except Exception as e:  # noqa: BLE001 - fall back to tool-free on any setup failure
        log(f"[!] agent tools unavailable ({type(e).__name__}: {e}); running tool-free")
        return None


class PhaseSession:
    """The shared agentic base for every pipeline phase (crawl judge, review, ideation).

    All three phases run through this one mechanism: a single tool-enabled runtime,
    opened once, with every model call routed through the same agentic loop under the
    same shared system prompt (`config.system_prompt()`) and the same max-turns/timeout.
    Phases differ ONLY in the *user* prompt they build and the context they feed — never
    in how the model is driven. If the toolset can't be brought up (agent disabled, or a
    setup failure), `call()` transparently degrades to a single completion, identically
    for all phases, so a tool problem never changes a phase's contract.

    Usage:
        with PhaseSession("review", log=log) as session:
            content, meta = session.call(prompt)
    """

    def __init__(self, label: str = "", log=print):
        self._label = label
        self._log = log
        self._runtime = None

    def open(self):
        self._runtime = open_runtime_or_none(self._label, log=self._log)
        return self

    def call(self, prompt: str, *, timeout: int = None):
        """One model step under the shared system prompt. Returns (content, meta):
        `content` is the final assistant text (None on error); the caller parses it.
        Uses the agentic tool-loop when tools are available, else a single completion."""
        import config
        import llm
        system = config.system_prompt() or None
        timeout = config.llm()["timeout"] if timeout is None else timeout
        if self._runtime is None:
            return llm.chat(prompt, system=system, timeout=timeout)
        res = self._runtime.step(system=system, user=prompt,
                                 max_turns=config.agent_max_turns(), timeout=timeout)
        if res.get("ok"):
            return res.get("content"), res.get("meta") or {}
        return None, (res.get("error") or {})

    def close(self):
        if self._runtime is not None:
            self._runtime.close()
            self._runtime = None

    def __enter__(self):
        return self.open()

    def __exit__(self, *exc):
        self.close()
