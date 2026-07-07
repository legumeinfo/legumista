#!/usr/bin/env python3
"""Tool-permission gate.

Every tool call passes through `Permissions.check()` before execution. The harness is
non-interactive, so it decides by mode plus explicit allow/deny rules rather than
prompting. Modes:
  - read_only  : allow read-only calls, deny writes — the DEFAULT for the pipeline and
                 `legumista research` (all native tools are read-only, and MCP tools are
                 gated by their readOnlyHint), safe for a non-interactive public beta
  - read_write : also allow write calls — opt-in via `--allow-write`, for tools that
                 mutate the workspace (e.g. `samtools sort`/`index`, `bcftools call`)
  - allow_all  : auto-approve everything — explicit opt-in only (e.g. trusted MCP write
                 tools)
  - deny_all   : block everything (dry-run)
`allow`/`deny` are sets of exact tool names that override the mode.

Read vs write is decided per call: a tool may expose a `writes(args) -> bool` classifier
(so a single `samtools` dispatcher can read on `view` and write on `sort`); absent that,
the static `read_only` flag stands for every call.
"""
from dataclasses import dataclass, field


@dataclass
class Decision:
    allowed: bool
    reason: str = ""


def _is_write(tool, args) -> bool:
    """Whether THIS call mutates state. A per-call `writes(args)` classifier wins; else
    the tool is a write iff it is not read-only."""
    classifier = getattr(tool, "writes", None)
    if callable(classifier):
        try:
            return bool(classifier(args or {}))
        except Exception:  # noqa: BLE001 - a misbehaving classifier must fail closed (write)
            return True
    return not getattr(tool, "read_only", False)


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
        if not _is_write(tool, args):             # a read-only call — always fine
            return Decision(True)
        if self.mode == "read_write":             # writes allowed only when opted in
            return Decision(True)
        return Decision(False, "write call requires read_write mode (--allow-write)")
