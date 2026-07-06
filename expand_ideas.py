#!/usr/bin/env python3
"""
Phase 3b (optional): expand each generated idea into a FULL technical report.

The ideation loop (ideation.py) is a breadth-first idea *generator/curator*: it
keeps its per-loop JSON verdicts short so a small local model stays reliable, so
each idea in ideas/ideas_manifest.json is a compact structured record, not a
write-up. This script turns those records into detailed, citation-grounded
research proposals.

For each idea it re-reads the WHOLE corpus digest + the Phase-2 synthesis, inlines
them alongside the idea, and asks the model — through the same shared agentic base as
every other phase — to write a multi-section markdown proposal that quotes specific
corpus findings and cites them by DOI. The SCRIPT captures the model's output and
writes the files itself.

Inputs:  ideas/ideas_manifest.json (matured ideas; default) or, with
         EXPAND_INCLUDE_POOL=1, every idea in ideation_state.json scoring
         >= EXPAND_MIN_SCORE.
Outputs: ideas/reports/<NN>_<slug>.md   (one report per idea)
         ideas/ideas_full_report.md     (all of them, with a table of contents)

Usage:  legumista expand   (or: python expand_ideas.py)
"""
import json
import os
import re
import sys

# Reuse Phase-3 paths/helpers (importing ideation is side-effect-free).
from ideation import (
    DIGEST_FILE,
    IDEAS_DIR,
    MANIFEST_FILE,
    STATE_FILE,
    latest_synthesis,
    read_text,
)
from orchestrator import WORKSPACE, load_json, log, now_iso
import config  # project identity + prompt templates
from legumista_agent.runtime import PhaseSession  # shared agentic base for all phases

REPORTS_DIR = os.path.join(IDEAS_DIR, "reports")
FULL_REPORT = os.path.join(IDEAS_DIR, "ideas_full_report.md")

CORPUS_CHARS = int(os.environ.get("EXPAND_CORPUS_CHARS", "40000"))
SYNTH_CHARS = int(os.environ.get("EXPAND_SYNTH_CHARS", "16000"))
INCLUDE_POOL = os.environ.get("EXPAND_INCLUDE_POOL", "0") != "0"
MIN_SCORE = int(os.environ.get("EXPAND_MIN_SCORE", "10"))

IDEA_FIELDS = ("title", "one_liner", "hypothesis", "approach", "datasets",
               "tools", "novelty", "feasibility", "risks", "grounding_dois",
               "critique", "scores", "score")


def slug(title: str, idea_id) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", (title or "").lower()).strip("-")[:60]
    return f"{int(idea_id):02d}_{s or 'idea'}"


def pick_ideas() -> list:
    """Which ideas to expand: matured manifest by default; optionally the whole
    scored pool (EXPAND_INCLUDE_POOL=1)."""
    manifest = load_json(MANIFEST_FILE, [])
    if INCLUDE_POOL:
        pool = load_json(STATE_FILE, {}).get("ideas", [])
        seen = {i.get("id") for i in manifest}
        extra = [i for i in pool
                 if i.get("id") not in seen and (i.get("score") or 0) >= MIN_SCORE]
        ideas = manifest + extra
    else:
        ideas = list(manifest)
    return sorted(ideas, key=lambda i: (i.get("score") or 0), reverse=True)


def build_prompt(idea: dict, corpus: str, synth: str) -> str:
    idea_block = json.dumps({k: idea.get(k) for k in IDEA_FIELDS},
                            indent=2, ensure_ascii=False)
    # Wording lives in prompts/expand.md; DOMAIN_CONSTRAINT is auto-injected.
    return config.render(
        "expand.md",
        IDEA_JSON=idea_block,
        SYNTH=synth or "(no synthesis available)",
        CORPUS=corpus,
    )


def expand_one(session: PhaseSession, prompt: str) -> tuple[str, str]:
    """Ask the model (via the shared agentic session) to write the proposal markdown.
    Returns (markdown, note); note is non-empty on failure."""
    content, meta = session.call(prompt)
    if content is None:
        return "", f"{meta.get('error')}: {meta.get('detail', '')}".strip(": ")[:200]
    return content.strip(), ""


def main():
    corpus = read_text(DIGEST_FILE, CORPUS_CHARS)
    if not corpus:
        log(f"[!] {os.path.relpath(DIGEST_FILE, WORKSPACE)} not found — run Phase 2 first.")
        sys.exit(1)
    synth = read_text(latest_synthesis(), SYNTH_CHARS)

    ideas = pick_ideas()
    if not ideas:
        log("[!] no ideas to expand. Let ideation.py mature some ideas (or set "
            "EXPAND_INCLUDE_POOL=1 to expand the scored pool).")
        sys.exit(1)

    os.makedirs(REPORTS_DIR, exist_ok=True)
    log(f"expanding {len(ideas)} ideas -> {os.path.relpath(REPORTS_DIR, WORKSPACE)}/ "
        f"(corpus {len(corpus)} chars, synthesis {'yes' if synth else 'no'})")

    session = PhaseSession("idea write-up", log=log).open()
    written = []
    try:
        for n, idea in enumerate(ideas, 1):
            title = idea.get("title") or f"idea {idea.get('id')}"
            log(f"  [{n}/{len(ideas)}] #{idea.get('id')} {title[:70]} ...")
            md, note = expand_one(session, build_prompt(idea, corpus, synth))
            if not md:
                log(f"      ! failed: {note}")
                md = (f"# {title}\n\n_Report generation failed ({note}). "
                      f"The structured idea record is below._\n\n```json\n"
                      f"{json.dumps(idea, indent=2, ensure_ascii=False)}\n```\n")
            path = os.path.join(REPORTS_DIR, slug(title, idea.get("id")) + ".md")
            with open(path, "w", encoding="utf-8") as f:
                f.write(md.rstrip() + "\n")
            written.append((idea, os.path.relpath(path, IDEAS_DIR), title))
            log(f"      -> {os.path.relpath(path, WORKSPACE)} ({len(md)} chars)")
    finally:
        session.close()

    # Assemble one combined document with a table of contents.
    toc = [f"# {config.name()} — full research-idea proposals", "",
           f"Generated {now_iso()} from `ideas/ideas_manifest.json`. "
           f"{len(written)} ideas, each expanded against the discovered corpus.", "",
           "## Contents", ""]
    for idea, _rel, title in written:
        sc = idea.get("score")
        toc.append(f"- [{title}](#{re.sub(r'[^a-z0-9]+', '-', title.lower()).strip('-')})"
                   f"{f' — {sc}/20' if sc is not None else ''}")
    body = []
    for _idea, rel, _title in written:
        with open(os.path.join(IDEAS_DIR, rel), encoding="utf-8") as f:
            body += ["", "---", "", f.read().rstrip(), ""]
    with open(FULL_REPORT, "w", encoding="utf-8") as f:
        f.write("\n".join(toc) + "\n" + "\n".join(body) + "\n")
    log(f"[✓] {len(written)} proposals -> {os.path.relpath(FULL_REPORT, WORKSPACE)} "
        f"(+ per-idea files in {os.path.relpath(REPORTS_DIR, WORKSPACE)}/)")


if __name__ == "__main__":
    main()
