"""Result conventions shared by every tool.

Three rules live here so no tool has to re-derive them:

1. A failure is not a finding. A tool that could not answer returns `fail(...)`, which the
   MCP bridge sends with `isError: true`. An empty-but-valid answer ("searched X, found
   nothing") is a normal result.
2. A truncated list says so. `count_phrase` renders "showing N of M" (or "showing the
   first N …; the total is unavailable" when M is unknown) and is the only way a tool
   should describe a count it capped.
3. A multi-source answer names its sources. `SourceReport` records which sources answered
   and which failed, so a partial answer is labelled partial.
"""
import re
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class ToolOutput:
    """A tool result with an explicit status. Handlers may still return a bare `str`."""
    text: str
    is_error: bool = False
    structured: Optional[dict] = None     # reserved for outputSchema-backed tools (V-03)


def fail(text: str) -> ToolOutput:
    """The tool could not answer (bad input, source unreachable, missing dependency)."""
    text = text if text.startswith("error:") else f"error: {text}"
    return ToolOutput(text=text, is_error=True)


# Prefixes the existing string-returning tools already use for failures:
#   "error: ..."            every Python tool
#   "[exit 1] ..."          tools_native._run_cli
#   "[esearch exit 1] ..."  tools_native._cli_raw / _cli_raw_bytes / _edirect
#   "[samtools error] ..."  tools_pysam._dispatch
_LEGACY_ERROR = re.compile(r"^(error:|\[exit -?\d+\]|\[[\w.-]+ exit -?\d+\]|\[(samtools|bcftools) error\])")


def coerce(result) -> ToolOutput:
    """Normalise a handler's return value. Bare strings are classified by prefix so the
    bridge can flag legacy failures without every tool being migrated at once."""
    if isinstance(result, ToolOutput):
        return result
    text = "" if result is None else str(result)
    return ToolOutput(text=text, is_error=bool(_LEGACY_ERROR.match(text)))


def count_phrase(shown: int, total: Optional[int], noun: str, *, capped: bool = False) -> str:
    """How to describe a list that may have been cut short.

    count_phrase(12, 12, "genes")            -> "12 genes"
    count_phrase(40, 46, "species")          -> "showing 40 of 46 species"
    count_phrase(50, None, "rows", capped=True)
        -> "showing the first 50 rows (the result hit its cap; the total is unavailable)"
    """
    if total is not None:
        if shown >= total:
            return f"{total:,} {noun}"
        return f"showing {shown:,} of {total:,} {noun}"
    if capped:
        return f"showing the first {shown:,} {noun} (the result hit its cap; the total is unavailable)"
    return f"{shown:,} {noun}"


@dataclass
class SourceReport:
    """What one upstream source contributed to a multi-source answer."""
    name: str
    ok: bool = True
    hits: int = 0
    error: str = ""

    @classmethod
    def failed(cls, name: str, exc: BaseException) -> "SourceReport":
        msg = f"{type(exc).__name__}: {exc}".strip()
        return cls(name=name, ok=False, error=msg[:160])


@dataclass
class SourceSummary:
    reports: list = field(default_factory=list)

    def add(self, report: SourceReport) -> None:
        self.reports.append(report)

    @property
    def failed(self) -> list:
        return [r for r in self.reports if not r.ok]

    @property
    def answered(self) -> list:
        return [r for r in self.reports if r.ok]

    def all_failed(self) -> bool:
        return bool(self.reports) and not self.answered

    def header(self) -> str:
        """A leading line for partial answers, "" when every source answered."""
        if not self.failed or self.all_failed():
            return ""
        bad = "; ".join(f"{r.name} FAILED ({r.error})" for r in self.failed)
        ok = ", ".join(r.name for r in self.answered)
        return (f"PARTIAL RESULTS — {bad}. The results below come only from {ok}; "
                "do not treat a missing paper as evidence it does not exist.")

    def footer(self) -> str:
        parts = [f"{r.name}: {r.hits} hit(s)" if r.ok else f"{r.name}: failed"
                 for r in self.reports]
        return "sources — " + "; ".join(parts)

    def failure_text(self, what: str) -> str:
        bad = "; ".join(f"{r.name} ({r.error})" for r in self.reports)
        return (f"error: {what} failed at every source — {bad}. This is an outage, not "
                "evidence that nothing exists; retry, or try another tool.")
