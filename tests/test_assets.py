"""Packaged assets must be discoverable via importlib.resources and non-empty.

Only one prompt survives the pipeline's removal — prompts/tools_native.md, which is
served as the MCP server's `instructions`. It is package data, so a packaging mistake
makes it silently absent rather than raising, and the server then falls back to a
one-line description that costs the client the whole tool-use doctrine.
"""
from importlib import resources


def _read(traversable):
    return traversable.read_text(encoding="utf-8")


def test_tools_native_prompt_is_bundled():
    spec = resources.files("legumista_assets") / "prompts" / "tools_native.md"
    assert spec.is_file(), "prompts/tools_native.md is not packaged"
    assert len(_read(spec).strip()) > 1000, "the served instructions look truncated"


def test_no_orphaned_pipeline_prompts_remain():
    """The pipeline prompts were removed with the pipeline; a stray one would ship dead
    weight to every install and re-suggest a workflow the package no longer has."""
    prompts = resources.files("legumista_assets") / "prompts"
    names = {p.name for p in prompts.iterdir() if p.name.endswith(".md")}
    assert names == {"tools_native.md"}, f"unexpected bundled prompts: {names}"


# The instructions are sent at initialize and stay in the client model's context for the
# whole session, with the resident catalog map (~1,400 tokens) appended. Growing them
# should be a decision made in review, not drift: raise this in the same PR, with a reason.
# ~8,000 tokens (at ~4 characters a token). Raised from 16,500 when the first wave of
# new tools (gene sets, feature extraction, viewer links, data-issue reports) was planned:
# the file was 15,953 characters, with no room for a line per new tool. Room to add
# tools is not licence to pad: keep each line terse but self-explanatory.
INSTRUCTIONS_BUDGET_CHARS = 32_000


def _instructions() -> str:
    return _read(resources.files("legumista_assets") / "prompts" / "tools_native.md")


def _served_tool_names() -> set:
    """Every tool a server can serve, including report_data_issue, which exists only
    with --allow-report and a GitHub App but still needs its line of doctrine."""
    from legumista_agent import tools_report
    from legumista_agent.tools_browser import browser_tools
    from legumista_agent.tools_catalog import catalog_tools
    from legumista_agent.tools_extract import extract_tools
    from legumista_agent.tools_lis import lis_tools
    from legumista_agent.tools_local import local_read_tools
    from legumista_agent.tools_mine import mine_tools
    from legumista_agent.tools_native import native_tools
    from legumista_agent.tools_pysam import bio_tools
    from legumista_agent.tools_verify import verify_tools
    real = tools_report._app_configured
    tools_report._app_configured = lambda: True
    try:
        report = tools_report.report_tools(allow_report=True)
    finally:
        tools_report._app_configured = real
    return {t.name for t in (local_read_tools() + native_tools() + lis_tools()
                             + mine_tools() + catalog_tools() + bio_tools()
                             + extract_tools() + browser_tools() + report
                             + verify_tools())}


def test_instructions_name_every_served_tool():
    """A tool the instructions never mention has no doctrine: the model learns its caveats
    (units, homology vs orthology, "no gene" vs "no rows") only from its description, if at
    all. Word boundaries matter — `lis_gene` must not be satisfied by `lis_gene_symbol`."""
    import re
    text = _instructions()
    missing = sorted(name for name in _served_tool_names()
                     if not re.search(rf"(?<![\w-]){re.escape(name)}(?![\w-])", text))
    assert not missing, f"served tools the instructions never name: {missing}"


def test_instructions_stay_within_budget():
    size = len(_instructions())
    assert size <= INSTRUCTIONS_BUDGET_CHARS, (
        f"tools_native.md is {size} characters (budget {INSTRUCTIONS_BUDGET_CHARS}); cut "
        "something or raise the budget deliberately")
