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
