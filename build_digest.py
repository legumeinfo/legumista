#!/usr/bin/env python3
"""
Deterministic bridge from Phase 1 (crawl) to Phase 2 (review).

Reads library_manifest.json (the canonical corpus written by orchestrator.py)
and emits corpus/corpus_digest.md: a sorted, deduplicated, review-ready view
that the synthesis model consumes instead of raw JSON. Purely mechanical — it
invents nothing, so it cannot hallucinate the corpus.

Usage:  legumista digest   (or: python build_digest.py)
Output: corpus/corpus_digest.md   (+ prints a one-line summary)
"""
import json
import os
from datetime import datetime, timezone

import config              # project identity + active project dir
ROOT = config.WORKSPACE    # the active project directory (data lives here)
MANIFEST = os.path.join(ROOT, "library_manifest.json")
OUT_DIR = os.path.join(ROOT, "corpus")
OUT = os.path.join(OUT_DIR, "corpus_digest.md")


def fmt_authors(authors):
    if not authors:
        return "(authors not recorded)"
    if len(authors) > 6:
        return ", ".join(authors[:6]) + f", … (+{len(authors) - 6})"
    return ", ".join(authors)


def main():
    if not os.path.exists(MANIFEST):
        print(f"[digest] {os.path.relpath(MANIFEST, ROOT)} not found — "
              "run a crawl first (`legumista crawl`/`legumista discover`).")
        return
    with open(MANIFEST, encoding="utf-8") as f:
        papers = json.load(f)

    # Dedup by DOI (last write wins) and sort closest-to-core first, then recent.
    by_doi = {p.get("doi"): p for p in papers if p.get("doi")}
    ordered = sorted(
        by_doi.values(),
        key=lambda p: (p.get("anchor_similarity") or 0, p.get("year") or 0),
        reverse=True,
    )
    with_pdf = sum(1 for p in ordered if p.get("local_path"))

    os.makedirs(OUT_DIR, exist_ok=True)
    lines = [
        f"# {config.name()} — corpus digest",
        "",
        f"Generated {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')} "
        f"from `library_manifest.json`. **{len(ordered)} papers** "
        f"({with_pdf} with local full-text PDF). Ordered by closeness to the "
        f"core anchor set (`anchor_similarity`, 1.0 = on-topic centre).",
        "",
        "| # | Year | Sim | Cites | Title | PDF |",
        "|---|------|-----|-------|-------|-----|",
    ]
    for i, p in enumerate(ordered, 1):
        title = (p.get("title") or "").replace("|", "\\|")[:80]
        lines.append(
            f"| {i} | {p.get('year') or '?'} | {p.get('anchor_similarity')} | "
            f"{p.get('cited_by_count') or 0} | {title} | "
            f"{'✔' if p.get('local_path') else '—'} |"
        )

    lines += ["", "## Full entries", ""]
    for i, p in enumerate(ordered, 1):
        pdf = p.get("local_path") or "metadata only (no OA PDF)"
        lines += [
            f"### [{i}] {p.get('title') or '(untitled)'} ({p.get('year') or 'n.d.'})",
            f"- DOI: {p.get('doi')}",
            f"- Authors: {fmt_authors(p.get('authors'))}",
            f"- Venue: {p.get('venue') or 'n/a'} | cited-by: {p.get('cited_by_count') or 0}"
            f" | core similarity: {p.get('anchor_similarity')}",
            f"- Full text: {pdf}",
            f"- Why collected: {p.get('selection_reason') or '—'}",
            f"- Abstract/summary: {p.get('summary') or '(none)'}",
            "",
        ]

    with open(OUT, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    print(f"[digest] {len(ordered)} papers ({with_pdf} PDFs) -> {os.path.relpath(OUT, ROOT)}")


if __name__ == "__main__":
    main()
