#!/usr/bin/env python3
"""`guide` — the detail behind each tool family, on demand.

The server's `instructions` (prompts/tools_native.md) stay short: how to read a result,
where to start, and the LIS conventions a model cannot know. Everything a model needs
only once it is working inside one tool family lives in `legumista_assets/guide/`, one
Markdown file per topic, and is served by this tool. A tool is the one channel every
MCP client passes to the model, so the detail is reachable from any of them, and it
costs nothing until it is read.

Each topic file opens with `# <topic> — <summary>`; the summary is what the index shows.
"""
import asyncio
from importlib import resources

from .results import fail
from .tool import Tool


def _files():
    return {p.name[:-3]: p for p in (resources.files("legumista_assets") / "guide").iterdir()
            if p.name.endswith(".md")}


def topics() -> dict:
    """{topic: one-line summary}, from each file's first line."""
    out = {}
    for name, path in sorted(_files().items()):
        first = path.read_text(encoding="utf-8").splitlines()[0].lstrip("# ").strip()
        out[name] = first.split(" — ", 1)[1] if " — " in first else first
    return out


def _guide(args):
    topic = (args.get("topic") or "").strip().lower()
    index = "\n".join(f"  {name}: {summary}" for name, summary in topics().items())
    if not topic:
        return "guide topics — call guide(topic) for one:\n" + index
    path = _files().get(topic)
    if path is None:
        return fail(f"no guide topic {topic!r}. Topics:\n{index}")
    return path.read_text(encoding="utf-8")


def guide_tools() -> list:
    async def run(args):
        return await asyncio.to_thread(_guide, args)
    return [Tool(
        name="guide",
        description=("The detail behind each tool family: argument forms, what each reply "
                     "means, and the traps. Topics: " + ", ".join(topics())
                     + ". No topic lists them with a summary each."),
        parameters={"type": "object",
                    "properties": {"topic": {"type": "string",
                                             "description": "A topic name; omit to list "
                                                            "them."}},
                    "additionalProperties": False},
        read_only=True, run=run)]
