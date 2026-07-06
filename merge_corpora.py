#!/usr/bin/env python3
"""
Combine the manifests from separate crawl runs into the canonical corpus.

Unions by DOI, records which algorithm(s) discovered each paper, and prints a
Venn summary (content-only / structural-only / both). Output feeds Phase 2.

Usage:
  legumista merge content structural   (or: python merge_corpora.py content structural)
      -> reads library_manifest.content.json + library_manifest.structural.json
      -> writes library_manifest.json  (canonical, deduped, with provenance)
  legumista merge                                    # defaults to content structural
"""
import json
import os
import sys

import config              # active project dir
ROOT = config.WORKSPACE    # union into the active project's canonical manifest


def norm(doi):
    return (doi or "").strip().lower()


def load(run):
    path = os.path.join(ROOT, f"library_manifest.{run}.json")
    if not os.path.exists(path):
        sys.exit(f"[!] missing {path} — run the '{run}' crawl first "
                 f"(`legumista crawl -s {run} --run {run}`)")
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def main(runs=None):
    """Union the given crawl-run manifests (default: content + structural). `runs` may be
    passed directly (by the CLI) or, when run as a script, taken from argv."""
    runs = list(runs) if runs else (sys.argv[1:] or ["content", "structural"])
    merged, sources = {}, {r: set() for r in runs}

    for run in runs:
        for p in load(run):
            d = norm(p.get("doi"))
            if not d:
                continue
            sources[run].add(d)
            if d not in merged:
                p = dict(p)
                p["discovered_by"] = [run]
                merged[d] = p
            else:
                m = merged[d]
                if run not in m.get("discovered_by", []):
                    m.setdefault("discovered_by", []).append(run)
                # keep the higher similarity + any PDF we managed to fetch
                if (p.get("anchor_similarity") or 0) > (m.get("anchor_similarity") or 0):
                    m["anchor_similarity"] = p.get("anchor_similarity")
                m["local_path"] = m.get("local_path") or p.get("local_path")

    out = sorted(merged.values(),
                 key=lambda p: (len(p.get("discovered_by", [])),
                                p.get("anchor_similarity") or 0),
                 reverse=True)
    with open(os.path.join(ROOT, "library_manifest.json"), "w", encoding="utf-8") as f:
        json.dump(out, f, indent=2, ensure_ascii=False)

    # Venn summary
    print(f"[merge] {len(out)} unique papers -> library_manifest.json")
    if len(runs) == 2:
        a, b = runs
        A, B = sources[a], sources[b]
        both = A & B
        print(f"    {a}-only:     {len(A - B)}")
        print(f"    {b}-only:     {len(B - A)}")
        print(f"    both:         {len(both)}")
        union = len(A | B)
        if union:
            print(f"    overlap:      {len(both) / union:.0%} of the combined set")
    else:
        for r in runs:
            print(f"    {r}: {len(sources[r])} papers")
    with_pdf = sum(1 for p in out if p.get("local_path"))
    print(f"    with full-text PDF: {with_pdf}/{len(out)}")


if __name__ == "__main__":
    main()
