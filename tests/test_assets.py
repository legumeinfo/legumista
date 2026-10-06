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
# ~2,500 tokens (at ~4 characters a token; the file was 5,394 characters). Cut from 32,000 when the per-tool detail
# moved to the `guide` tool, which costs nothing until it is read: the instructions keep
# only how to read a result, where to start, and the LIS conventions. Detail belongs in
# a guide topic, not here.
INSTRUCTIONS_BUDGET_CHARS = 10_000


def _instructions() -> str:
    return _read(resources.files("legumista_assets") / "prompts" / "tools_native.md")


def _served_tool_names() -> set:
    """Every tool a server can serve, including report_data_issue, which exists only
    with --allow-report and a GitHub App but still needs its line of doctrine."""
    from legumista_agent import tools_report
    from legumista_agent.tools_browser import browser_tools
    from legumista_agent.tools_catalog import catalog_tools
    from legumista_agent.tools_extract import extract_tools
    from legumista_agent.tools_guide import guide_tools
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
                             + verify_tools() + guide_tools())}


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


# --- guide topics ---------------------------------------------------------------------
def _guides() -> dict:
    folder = resources.files("legumista_assets") / "guide"
    return {p.name[:-3]: _read(p) for p in folder.iterdir() if p.name.endswith(".md")}


def test_guide_topics_are_bundled_with_a_summary_line():
    """The `guide` index shows each file's first line, `# <topic> — <summary>`."""
    guides = _guides()
    assert guides, "guide/*.md is not packaged"
    for name, text in guides.items():
        first = text.splitlines()[0]
        assert first.startswith(f"# {name} — ") and len(first) > len(name) + 10, name


def test_guides_name_only_tools_that_exist():
    """A guide that names a renamed or removed tool sends the model to a dead end."""
    import re
    served = _served_tool_names()
    prefixes = ("lis_", "mine_", "ncbi_", "tabix_", "fasta_", "extract_",
                "browser_", "report_", "verify_", "paper_", "europepmc_", "openalex_",
                "read_", "web_", "sra_")
    for name, text in _guides().items():
        named = {w for w in re.findall(r"(?<![\w.-])([a-z]+_[a-z_]+)(?![\w(-])", text)
                 if w.startswith(prefixes) and not w.endswith("_")}
        named -= {"mine_gene_", "mine_trait_"}
        unknown = sorted(w for w in named if w not in served and not any(
            s.startswith(w) for s in served))
        assert not unknown, f"guide {name!r} names tools that are not served: {unknown}"


def test_guide_tool_serves_topics_and_refuses_unknown_ones():
    import asyncio
    from legumista_agent.results import coerce
    from legumista_agent.tools_guide import guide_tools
    tool = guide_tools()[0]
    index = coerce(asyncio.run(tool.run({}))).text
    for name in _guides():
        assert f"  {name}: " in index
    assert coerce(asyncio.run(tool.run({"topic": "genes"}))).text == _guides()["genes"]
    missing = coerce(asyncio.run(tool.run({"topic": "nonesuch"})))
    assert missing.is_error and "datastore" in missing.text
