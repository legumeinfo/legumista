#!/usr/bin/env python3
"""
Deterministic bridge from Phase 3 (ideation) to a human-readable deliverable.

Reads ideas/ideas_manifest.json (the canonical matured-idea ledger written by
ideation.py) and emits ideas/ideation_report.md: a ranked, review-ready view of
the generated research ideas. Purely mechanical — it invents nothing.

Usage:  legumista report   (or: python build_ideation_report.py)
Output: ideas/ideation_report.md   (+ prints a one-line summary)
"""
import json
import os
from datetime import datetime, timezone

import config              # project identity + active project dir
ROOT = config.WORKSPACE    # the active project directory (data lives here)
MANIFEST = os.path.join(ROOT, "ideas", "ideas_manifest.json")
OUT = os.path.join(ROOT, "ideas", "ideation_report.md")

SCORE_DIMS = ("novelty", "feasibility", "impact", "grounding")


def as_list(v):
    if isinstance(v, list):
        return [str(x) for x in v if x]
    return [str(v)] if v else []


def main():
    if not os.path.exists(MANIFEST):
        raise SystemExit(f"[!] {MANIFEST} not found — run Phase 3 (ideation.py) first.")
    with open(MANIFEST, encoding="utf-8") as f:
        ideas = json.load(f)

    ordered = sorted(ideas, key=lambda i: (i.get("score") or 0), reverse=True)

    lines = [
        f"# {config.name()} — generated research ideas",
        "",
        f"Generated {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')} "
        f"from `ideas/ideas_manifest.json`. **{len(ordered)} matured ideas**, "
        "ranked by total score (novelty + feasibility + impact + grounding, /20). "
        "Every idea is grounded in the discovered corpus.",
        "",
        "| # | Score | Title | Key tools |",
        "|---|-------|-------|-----------|",
    ]
    for i, idea in enumerate(ordered, 1):
        title = (idea.get("title") or "(untitled)").replace("|", "\\|")[:80]
        tools = ", ".join(as_list(idea.get("tools")))[:50].replace("|", "\\|")
        lines.append(f"| {i} | {idea.get('score') if idea.get('score') is not None else '?'}"
                     f"/20 | {title} | {tools} |")

    lines += ["", "## Full ideas", ""]
    for i, idea in enumerate(ordered, 1):
        scores = idea.get("scores") or {}
        score_str = ", ".join(f"{d} {scores.get(d, '?')}" for d in SCORE_DIMS)
        lines += [
            f"### [{i}] {idea.get('title') or '(untitled)'}  "
            f"— {idea.get('score') if idea.get('score') is not None else '?'}/20",
            f"- One-liner: {idea.get('one_liner') or '—'}",
            f"- Hypothesis: {idea.get('hypothesis') or '—'}",
            f"- Approach: {idea.get('approach') or '—'}",
            f"- Datasets: {', '.join(as_list(idea.get('datasets'))) or '—'}",
            f"- Tools: {', '.join(as_list(idea.get('tools'))) or '—'}",
            f"- Novelty: {idea.get('novelty') or '—'}",
            f"- Feasibility: {idea.get('feasibility') or '—'}",
            f"- Risks: {idea.get('risks') or '—'}",
            f"- Grounding DOIs: {', '.join(as_list(idea.get('grounding_dois'))) or '—'}",
            f"- Critique: {idea.get('critique') or '—'}",
            f"- Scores: {score_str}  |  refinements: {idea.get('refined_count', 0)}",
            "",
        ]

    with open(OUT, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    print(f"[ideation-report] {len(ordered)} ideas -> {os.path.relpath(OUT, ROOT)}")


if __name__ == "__main__":
    main()
