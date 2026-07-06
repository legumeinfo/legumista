#!/usr/bin/env python3
"""Tool-permission gate.

Every tool call passes through `Permissions.check()` before execution. The harness is
non-interactive, so it decides by mode plus explicit allow/deny rules rather than
prompting. Modes:
  - read_only  : allow read-only tools, deny writes — the DEFAULT for the pipeline and
                 `legumista research` (all native tools are read-only, and MCP tools are
                 gated by their readOnlyHint), safe for a non-interactive public beta
  - allow_all  : auto-approve everything — explicit opt-in only (e.g. trusted MCP write
                 tools); no longer the default
  - deny_all   : block everything (dry-run)
`allow`/`deny` are sets of exact tool names that override the mode.
"""
from dataclasses import dataclass, field


@dataclass
class Decision:
    allowed: bool
    reason: str = ""


@dataclass
class Permissions:
    mode: str = "read_only"
    allow: set = field(default_factory=set)
    deny: set = field(default_factory=set)

    def check(self, tool, args) -> Decision:
        if tool.name in self.deny:
            return Decision(False, "denied by rule")
        if tool.name in self.allow:
            return Decision(True)
        if self.mode == "allow_all":
            return Decision(True)
        if self.mode == "deny_all":
            return Decision(False, "deny_all mode")
        # read_only mode
        if getattr(tool, "read_only", False):
            return Decision(True)
        return Decision(False, "write tool requires approval (read_only mode)")
