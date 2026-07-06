"""Packaged assets must be discoverable via importlib.resources and non-empty."""
from importlib import resources


def _read(traversable):
    return traversable.read_text(encoding="utf-8")


def test_top_level_assets_exist():
    root = resources.files("legumista_assets")
    for name in ("system-prompt.md", "legumista.example.yml"):
        f = root / name
        assert f.is_file(), f"missing asset: {name}"
        assert _read(f).strip(), f"empty asset: {name}"


def test_prompt_templates_exist():
    prompts = resources.files("legumista_assets") / "prompts"
    mds = [p for p in prompts.iterdir() if p.name.endswith(".md")]
    assert mds, "no prompts/*.md bundled"
    for p in mds:
        assert _read(p).strip(), f"empty prompt: {p.name}"
