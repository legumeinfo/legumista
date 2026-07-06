#!/usr/bin/env python3
"""
Autonomous citation-graph crawl orchestrator (Phase 1).

Design (per team decisions):
  - Python does ALL deterministic mechanics: it pulls citation edges from
    OpenAlex, filters to scope, downloads PDFs, and commits state.
  - The LLM (an OpenAI-compatible /chat/completions endpoint — ollama, OpenRouter,
    …, configured in legumista.yml `llm`) is used ONLY as a relevance judge: given a
    small candidate list, it returns a JSON verdict (which paper to collect, which
    DOIs to keep expanding). It never edits the canonical ledgers.
  - State is flushed every loop: one atomic expand-a-node action per invocation,
    then the process exits. Keeps loop 1 and loop 100 equally cheap (no context
    bloat).

Ledgers (canonical, only this script writes them):
  agent_state.json      -> frontier queue + loop/budget counters
  library_manifest.json -> collected papers (dedup source of truth)

The LLM's only output is a JSON verdict parsed from the reply; if it is malformed
the loop is skipped WITHOUT mutating state (retry-safe).
"""

import hashlib
import json
import math
import os
import re
import shutil
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone

import config  # project identity + prompt templates (topic-specific config)
# Shared pipeline engine (identical control flow across phases):
from pipeline import call_for_json, extract_json, log_model_banner, run_atomic_loop

# ----------------------------------------------------------------------------
# Configuration (override via environment)
# ----------------------------------------------------------------------------
WORKSPACE = config.WORKSPACE          # the active project directory (data lives here)
CONTEXT_DIR = os.path.join(WORKSPACE, "context_inputs")

# Which distance algorithm scores candidates:
#   content    = concept/topic cosine to the anchor profile (topical labels)
#   structural = bibliographic-coupling cosine (shared references w/ anchors)
SIMILARITY = os.environ.get("CRAWL_SIMILARITY", "content").lower()
# Namespace for independent runs. Empty = default files; e.g. CRAWL_RUN=structural
# writes agent_state.structural.json / library_manifest.structural.json so two
# algorithms can crawl in parallel without colliding. Anchors come from the base
# agent_state.json either way.
RUN = os.environ.get("CRAWL_RUN", "").strip()
_sfx = f".{RUN}" if RUN else ""

BASE_STATE_FILE = os.path.join(WORKSPACE, "agent_state.json")   # anchor template
STATE_FILE = os.path.join(WORKSPACE, f"agent_state{_sfx}.json")
MANIFEST_FILE = os.path.join(WORKSPACE, f"library_manifest{_sfx}.json")
ANCHOR_CACHE = os.path.join(WORKSPACE, f"anchor_profile.{SIMILARITY}{_sfx}.json")
PAPERS_DIR = os.path.join(WORKSPACE, "papers")          # shared, deduped by DOI
LOG_DIR = os.path.join(WORKSPACE, "reviews", f"crawl_logs{_sfx}")
# Single overwriting transcript of the MOST RECENT loop's full judge exchange
# (prompt + response + metadata) for eyeballing prompt/answer quality. Each loop
# clobbers it; the numbered loop_NNN.log in LOG_DIR remains the append audit trail.
LAST_LOOP_TRANSCRIPT = os.path.join(LOG_DIR, "last_loop.md")

MAX_LOOPS = int(os.environ.get("CRAWL_MAX_LOOPS", "100"))
MAX_PAPERS = int(os.environ.get("CRAWL_MAX_PAPERS", "250"))
WALLCLOCK_HOURS = float(os.environ.get("CRAWL_WALLCLOCK_HOURS", "48"))
COOLDOWN_SECONDS = int(os.environ.get("CRAWL_COOLDOWN_SECONDS", "25"))
CANDIDATES_PER_LOOP = int(os.environ.get("CRAWL_CANDIDATES_PER_LOOP", "30"))
_YEARS = config.years()
YEAR_MIN = int(os.environ.get("CRAWL_YEAR_MIN", _YEARS[0]))
YEAR_MAX = int(os.environ.get("CRAWL_YEAR_MAX", _YEARS[1]))
# Distance gate: candidates scoring below this (by the selected algorithm) are
# dropped before the LLM sees them. Sensible per-metric defaults; override with
# CRAWL_MIN_SIMILARITY. (Content and structural cosines live on different scales.)
_DEFAULT_MIN_SIM = {"content": 0.35, "structural": 0.08}
MIN_SIMILARITY = float(os.environ.get("CRAWL_MIN_SIMILARITY",
                                      _DEFAULT_MIN_SIM.get(SIMILARITY, 0.2)))
# Secondary guard: how many citation hops from a core/anchor paper we allow the
# frontier to travel. Anchors are depth 0.
MAX_DEPTH = int(os.environ.get("CRAWL_MAX_DEPTH", "3"))
OPENALEX_MAILTO = os.environ.get("OPENALEX_MAILTO", config.contact_email())
UNPAYWALL_EMAIL = os.environ.get("UNPAYWALL_EMAIL", OPENALEX_MAILTO)
MAX_LLM_FAILURES = int(os.environ.get("CRAWL_MAX_LLM_FAILURES", "3"))  # abort after N

OPENALEX = "https://api.openalex.org"
UNPAYWALL = "https://api.unpaywall.org/v2"
# Polite-pool UA for the OpenAlex JSON API (identifies us + mailto).
UA = {"User-Agent": f"legumista/1.0 (mailto:{OPENALEX_MAILTO})"}
# Browser-like UA for fetching PDFs from OA hosts, which routinely 403 bots.
BROWSER_UA = {
    "User-Agent": ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/125.0 Safari/537.36"),
    "Accept": "application/pdf,application/octet-stream,*/*",
}
# Set CRAWL_DOWNLOAD_PDFS=0 to skip PDF fetching entirely (metadata-only crawl).
DOWNLOAD_PDFS = os.environ.get("CRAWL_DOWNLOAD_PDFS", "1") != "0"

# Lexical relevance signal used to PRE-RANK candidates before the LLM sees them.
# Without this, generic high-citation method papers (DESeq2, BWA, ...) dominate
# the candidate cap and starve the model of on-topic work. Terms are project
# config (legumista.yml lexical): strong weighted 5x, weak weighted 1x.
TOPIC_STRONG = tuple(config.lexical_strong())
TOPIC_WEAK = tuple(config.lexical_weak())


def relevance_score(cand: dict) -> int:
    hay = f"{cand.get('title', '')} {cand.get('abstract', '')}".lower()
    score = 5 * sum(t in hay for t in TOPIC_STRONG)
    score += sum(t in hay for t in TOPIC_WEAK)
    return score


# --- Topical distance metric (concept/topic cosine vs. the anchor profile) -----
def extract_vector(work: dict) -> dict:
    """Concept+topic feature vector for a work. Drops level-0 mega-concepts
    (Biology, Chemistry) so generic overlap can't inflate similarity."""
    vec = {}
    for c in work.get("concepts") or []:
        if (c.get("level") or 0) >= 1:
            k = "C:" + openalex_id_short(c.get("id", ""))
            vec[k] = max(vec.get(k, 0.0), c.get("score") or 0.0)
    for t in work.get("topics") or []:
        k = "T:" + openalex_id_short(t.get("id", ""))
        vec[k] = max(vec.get(k, 0.0), t.get("score") or 0.0)
    return vec


def cosine(a: dict, b: dict) -> float:
    if not a or not b:
        return 0.0
    dot = sum(a[k] * b[k] for k in (a.keys() & b.keys()))
    na = math.sqrt(sum(v * v for v in a.values()))
    nb = math.sqrt(sum(v * v for v in b.values()))
    return dot / (na * nb) if na and nb else 0.0


def build_anchor_profile(anchor_dois: list) -> dict:
    """Aggregate concept/topic vector of the core papers (cached to disk;
    rebuilt only when the anchor DOI set changes)."""
    key = hashlib.sha1(",".join(sorted(anchor_dois)).encode()).hexdigest()
    if os.path.exists(ANCHOR_CACHE):
        cached = load_json(ANCHOR_CACHE, {})
        if cached.get("key") == key:
            log(f"anchor profile: cached ({len(cached['profile'])} features)")
            return cached["profile"]
    log(f"building anchor profile from {len(anchor_dois)} core papers ...")
    profile, resolved = {}, 0
    for doi in anchor_dois:
        w = fetch_work_by_doi(doi)
        if not w:
            log(f"    anchor unresolved (skipped): {doi}")
            continue
        resolved += 1
        for k, v in extract_vector(w).items():
            profile[k] = profile.get(k, 0.0) + v
    atomic_write(ANCHOR_CACHE, {"key": key, "profile": profile})
    log(f"anchor profile: {resolved}/{len(anchor_dois)} papers, {len(profile)} features")
    return profile


def build_ref_profile(anchor_dois: list) -> dict:
    """Structural profile for bibliographic coupling: how many anchor papers cite
    each referenced work. Candidates that cite the same foundational literature as
    the core papers score high — a content-independent signal of kinship."""
    key = hashlib.sha1(("refs:" + ",".join(sorted(anchor_dois))).encode()).hexdigest()
    if os.path.exists(ANCHOR_CACHE):
        cached = load_json(ANCHOR_CACHE, {})
        if cached.get("key") == key:
            log(f"anchor ref-profile: cached ({len(cached['profile'])} refs)")
            return cached["profile"]
    log(f"building structural (reference) profile from {len(anchor_dois)} core papers ...")
    profile, resolved = {}, 0
    for doi in anchor_dois:
        w = fetch_work_by_doi(doi)
        if not w:
            log(f"    anchor unresolved (skipped): {doi}")
            continue
        resolved += 1
        for r in (w.get("referenced_works") or []):
            rid = openalex_id_short(r)
            profile[rid] = profile.get(rid, 0.0) + 1.0
    atomic_write(ANCHOR_CACHE, {"key": key, "profile": profile})
    log(f"anchor ref-profile: {resolved}/{len(anchor_dois)} papers, {len(profile)} unique refs")
    return profile


def build_profile(anchor_dois: list) -> dict:
    """Build the anchor profile for whichever algorithm is selected."""
    return (build_ref_profile if SIMILARITY == "structural"
            else build_anchor_profile)(anchor_dois)


def score(cand: dict, profile: dict) -> float:
    """Distance score in [0,1] by the selected algorithm.
    content    -> cosine of concept/topic vectors
    structural -> cosine of reference-set vectors (bibliographic coupling)."""
    if SIMILARITY == "structural":
        vec = {rid: 1.0 for rid in cand.get("ref_ids") or []}
    else:
        vec = cand.get("vector") or {}
    return round(cosine(vec, profile), 3)


# ----------------------------------------------------------------------------
# Small utilities
# ----------------------------------------------------------------------------
def log(msg: str) -> None:
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def norm_doi(doi: str) -> str:
    """Canonicalise a DOI to bare lowercase form (no URL prefix)."""
    if not doi:
        return ""
    doi = doi.strip().lower()
    doi = re.sub(r"^https?://(dx\.)?doi\.org/", "", doi)
    return doi


def http_json(url: str, retries: int = 3):
    """GET JSON with polite retry/backoff. Returns dict or None."""
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers=UA)
            with urllib.request.urlopen(req, timeout=30) as resp:
                return json.load(resp)
        except Exception as e:  # noqa: BLE001 - network is best-effort
            wait = 2 ** attempt
            log(f"    HTTP retry {attempt + 1}/{retries} in {wait}s ({e})")
            time.sleep(wait)
    return None


def reconstruct_abstract(inv_index) -> str:
    if not inv_index:
        return ""
    positions = {}
    for word, idxs in inv_index.items():
        for i in idxs:
            positions[i] = word
    return " ".join(positions[i] for i in sorted(positions))


# ----------------------------------------------------------------------------
# State I/O (this script is the ONLY writer)
# ----------------------------------------------------------------------------
def load_json(path, default):
    if not os.path.exists(path):
        return default
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        # A corrupt ledger: fall back to the .bak if present.
        bak = f"{path}.bak"
        if os.path.exists(bak):
            log(f"[!] {os.path.basename(path)} unreadable ({e}); restoring .bak")
            shutil.copy2(bak, path)
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        raise


def backup_ledgers():
    for path in (STATE_FILE, MANIFEST_FILE):
        if os.path.exists(path):
            shutil.copy2(path, f"{path}.bak")


def atomic_write(path, obj):
    tmp = f"{path}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, indent=2, ensure_ascii=False)
    os.replace(tmp, path)


def atomic_write_text(path, text):
    tmp = f"{path}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(text)
    os.replace(tmp, path)


# ----------------------------------------------------------------------------
# OpenAlex graph mechanics (deterministic; the LLM never does this)
# ----------------------------------------------------------------------------
def fetch_work_by_doi(doi: str):
    url = f"{OPENALEX}/works/doi:{urllib.parse.quote(norm_doi(doi))}?mailto={OPENALEX_MAILTO}"
    return http_json(url)


def openalex_id_short(work_id: str) -> str:
    return (work_id or "").rsplit("/", 1)[-1]


def work_to_candidate(w) -> dict:
    return {
        "doi": norm_doi(w.get("doi", "") or ""),
        "openalex_id": openalex_id_short(w.get("id", "")),
        "title": w.get("title") or "(untitled)",
        "year": w.get("publication_year"),
        "cited_by_count": w.get("cited_by_count", 0),
        "authors": [
            (a.get("author") or {}).get("display_name")
            for a in (w.get("authorships") or [])[:12]
            if (a.get("author") or {}).get("display_name")
        ],
        "abstract": reconstruct_abstract(w.get("abstract_inverted_index")),
        "oa_url": (w.get("open_access") or {}).get("oa_url"),
        "pdf_urls": pdf_url_candidates(w),
        "venue": (((w.get("primary_location") or {}).get("source")) or {}).get("display_name"),
        "vector": extract_vector(w),
        "ref_ids": [openalex_id_short(r) for r in (w.get("referenced_works") or [])],
    }


def pdf_url_candidates(w) -> list:
    """Ordered, de-duplicated list of URLs to try for a full-text PDF.
    Direct pdf_url's from OA locations first; landing pages / oa_url last."""
    urls = []

    def add(u):
        if u and u not in urls:
            urls.append(u)

    best = w.get("best_oa_location") or {}
    add(best.get("pdf_url"))
    add((w.get("primary_location") or {}).get("pdf_url"))
    for loc in w.get("locations") or []:
        if (loc or {}).get("is_oa"):
            add(loc.get("pdf_url"))
    add(best.get("landing_page_url"))
    add((w.get("open_access") or {}).get("oa_url"))
    return urls


def fetch_references(work) -> list:
    """Backward edges: works this paper cites."""
    ref_ids = [openalex_id_short(r) for r in work.get("referenced_works", [])]
    out = []
    for i in range(0, len(ref_ids), 50):  # OpenAlex OR-filter batches
        chunk = "|".join(ref_ids[i:i + 50])
        url = (f"{OPENALEX}/works?filter=openalex_id:{chunk}"
               f"&per-page=50&mailto={OPENALEX_MAILTO}")
        data = http_json(url)
        if data:
            out.extend(work_to_candidate(w) for w in data.get("results", []))
    return out


def fetch_citations(work, limit=100) -> list:
    """Forward edges: works citing this paper."""
    wid = openalex_id_short(work.get("id", ""))
    url = (f"{OPENALEX}/works?filter=cites:{wid}"
           f"&per-page={min(limit, 100)}&sort=cited_by_count:desc&mailto={OPENALEX_MAILTO}")
    data = http_json(url)
    return [work_to_candidate(w) for w in (data.get("results", []) if data else [])]


def in_scope(cand: dict) -> bool:
    y = cand.get("year")
    return bool(cand.get("doi")) and isinstance(y, int) and YEAR_MIN <= y <= YEAR_MAX


def unpaywall_pdf_urls(doi: str) -> list:
    """Ask Unpaywall for OA PDF locations of a DOI. Often finds a repository
    copy (PMC, institutional, preprint) when the publisher blocks bots."""
    if not doi:
        return []
    data = http_json(f"{UNPAYWALL}/{urllib.parse.quote(doi)}?email={UNPAYWALL_EMAIL}",
                     retries=2)
    if not data:
        return []
    urls = []

    def add(u):
        if u and u not in urls:
            urls.append(u)

    best = data.get("best_oa_location") or {}
    add(best.get("url_for_pdf"))
    for loc in data.get("oa_locations") or []:
        add((loc or {}).get("url_for_pdf"))
    add(best.get("url"))
    return urls


def download_pdf(cand: dict) -> str:
    """Best-effort OA PDF download. Tries OpenAlex's OA URLs first, then falls
    back to Unpaywall, all with a browser-like UA. Returns local path or ''
    (paywalled/blocked papers stay metadata-only — expected, so failures are
    silent)."""
    if not DOWNLOAD_PDFS:
        return ""
    safe = re.sub(r"[^a-z0-9]+", "_", cand["doi"].lower()).strip("_")[:80]
    dest = os.path.join(PAPERS_DIR, f"{safe}.pdf")
    tried = set()

    def try_urls(url_list) -> bool:
        for url in url_list:
            if not url or url in tried:
                continue
            tried.add(url)
            try:
                req = urllib.request.Request(url, headers=BROWSER_UA)
                with urllib.request.urlopen(req, timeout=60) as resp:
                    data = resp.read()
                if data[:4] != b"%PDF":   # HTML landing page — try next
                    continue
                with open(dest, "wb") as f:
                    f.write(data)
                return True
            except Exception:  # noqa: BLE001 - 403/404/timeout: try the next URL
                continue
        return False

    openalex_urls = cand.get("pdf_urls") or ([cand["oa_url"]] if cand.get("oa_url") else [])
    if try_urls(openalex_urls):
        return os.path.relpath(dest, WORKSPACE)
    # Fallback: Unpaywall (only queried when OpenAlex's URLs didn't yield a PDF).
    if try_urls(unpaywall_pdf_urls(cand.get("doi", ""))):
        return os.path.relpath(dest, WORKSPACE)
    return ""


# ----------------------------------------------------------------------------
# LLM relevance judgment (the ONLY job the model does)
# ----------------------------------------------------------------------------
def read_context() -> str:
    """Concatenated steering from context_inputs/*.md, or "" if none — steering is
    optional, so callers omit the whole section (header included) when this is empty."""
    if not os.path.isdir(CONTEXT_DIR):
        return ""
    blocks = []
    for name in sorted(os.listdir(CONTEXT_DIR)):
        if name.endswith(".md"):
            with open(os.path.join(CONTEXT_DIR, name), encoding="utf-8") as f:
                body = f.read().strip()
                if body:
                    blocks.append(f"### STEERING — {name}\n{body}")
    return "\n\n".join(blocks)


def build_prompt(loop_no, parent, candidates, steering) -> str:
    cand_lines = []
    for i, c in enumerate(candidates):
        abstract = (c["abstract"] or "")[:600]
        cand_lines.append(
            f'[{i}] doi={c["doi"]} year={c["year"]} cites={c["cited_by_count"]} '
            f'core_similarity={c.get("similarity")}\n'
            f'    title: {c["title"]}\n'
            f'    abstract: {abstract}'
        )
    cand_block = "\n".join(cand_lines) if cand_lines else "(no candidates)"
    # Steering is optional: include the whole section (header + body) only when present,
    # so an empty context_inputs/ leaves no orphan "STEERING GUIDELINES" header in the prompt.
    steering_block = (
        "STEERING GUIDELINES FOR THIS RUN (these override defaults, re-read every loop):\n"
        f"{steering}\n\n" if steering.strip() else "")
    # Prompt wording lives in prompts/crawl_judge.md; SUBJECT is auto-injected.
    return config.render(
        "crawl_judge.md",
        LOOP_NO=loop_no, STEERING=steering_block,
        PARENT_DOI=parent.get("doi"), PARENT_TITLE=parent.get("title"),
        YEAR_MIN=YEAR_MIN, YEAR_MAX=YEAR_MAX, CANDIDATES=cand_block,
    )


def write_transcript(loop_no, prompt, content, meta, verdict, note=""):
    """Overwrite the single last_loop.md transcript with the full judge exchange for
    this loop: the exact prompt sent, the full assistant reply, the parsed verdict,
    and endpoint metadata. Written on EVERY outcome so a failed loop is inspectable.
    Deliberately clobbers the previous loop — a quality spot-check, not the audit log."""
    meta = meta or {}
    parts = [
        f"# Crawl judge transcript — loop {loop_no}",
        "",
        f"- generated: {now_iso()}",
        f"- run: {SIMILARITY}{'/' + RUN if RUN else ''}",
        f"- model: {meta.get('model', '?')}",
    ]
    if meta.get("usage"):
        parts.append(f"- usage: {meta['usage']}")
    if meta.get("finish_reason"):
        parts.append(f"- finish_reason: {meta['finish_reason']}")
    if note:
        parts.append(f"- note: {note}")
    parts += [
        "",
        "## Prompt (sent to judge)",
        "",
        "```text",
        prompt,
        "```",
        "",
        "## Assistant response",
        "",
        "```text",
        (content or "").strip() or "(empty)",
        "```",
        "",
        "## Parsed verdict",
        "",
        "```json",
        (json.dumps(verdict, indent=2, ensure_ascii=False)
         if verdict is not None else "(no valid verdict parsed)"),
        "```",
        "",
    ]
    atomic_write_text(LAST_LOOP_TRANSCRIPT, "\n".join(parts))


def run_llm(session, prompt: str, loop_no: int):
    """Get a JSON verdict from the model as the relevance judge (the judge may call tools
    to verify/enrich before deciding). Returns the parsed dict or None; always overwrites
    last_loop.md with the prompt+response transcript."""
    verdict, content, meta, note = call_for_json(session, prompt, loop_no, LOG_DIR, log)
    write_transcript(loop_no, prompt, content, meta, verdict, note)
    return verdict


# ----------------------------------------------------------------------------
# One atomic loop
# ----------------------------------------------------------------------------
def run_loop(session, state, manifest, manifest_dois, anchor_profile, started_at) -> str:
    """Run one atomic loop. Returns a status: 'continue', 'stop', or
    'llm_failed' (infra failure — frontier deliberately left intact)."""
    loop_no = state["current_loop"] + 1
    depth = state.setdefault("depth", {})

    # --- stop conditions ---
    if state["current_loop"] >= state.get("max_loops", MAX_LOOPS):
        log(f"[stop] loop ceiling ({state['max_loops']}) reached")
        return "stop"
    if len(manifest) >= MAX_PAPERS:
        log(f"[stop] paper ceiling ({MAX_PAPERS}) reached")
        return "stop"
    if (time.time() - started_at) > WALLCLOCK_HOURS * 3600:
        log(f"[stop] wall-clock budget ({WALLCLOCK_HOURS}h) reached")
        return "stop"

    # --- pick next frontier node ---
    pending = [d for d in state["pending_dois"] if d not in state["crawled_dois"]]
    if not pending:
        log("[stop] frontier empty — nothing left to expand")
        return "stop"
    parent_doi = pending[0]
    parent_depth = depth.get(parent_doi, 0)

    log(f"===== LOOP {loop_no}/{state['max_loops']}  |  expanding {parent_doi} "
        f"(depth {parent_depth})  |  library={len(manifest)} pending={len(pending)} =====")

    if parent_depth > MAX_DEPTH:
        log(f"    depth {parent_depth} > MAX_DEPTH {MAX_DEPTH}; not expanding")
        state["crawled_dois"].append(parent_doi)
        state["current_loop"] = loop_no
        commit(state, manifest)
        return "continue"

    work = fetch_work_by_doi(parent_doi)
    if not work:
        log(f"    could not resolve {parent_doi} on OpenAlex; marking crawled")
        state["crawled_dois"].append(parent_doi)
        state["current_loop"] = loop_no
        commit(state, manifest)
        return "continue"

    parent_cand = work_to_candidate(work)
    parent_cand["similarity"] = score(parent_cand, anchor_profile)
    # Collect the parent itself the first time we see it (seeds get archived too).
    if parent_cand["doi"] and parent_cand["doi"] not in manifest_dois and in_scope(parent_cand):
        add_to_manifest(parent_cand, manifest, manifest_dois, reason="frontier parent")

    # --- deterministic graph fetch + scope filter + dedup + distance gate ---
    edges = fetch_references(work) + fetch_citations(work)
    seen, candidates, dropped_far = set(), [], 0
    for c in edges:
        d = c["doi"]
        if not d or d in seen or d in state["crawled_dois"] or d in manifest_dois:
            continue
        if not in_scope(c):
            continue
        seen.add(d)
        c["similarity"] = score(c, anchor_profile)
        if c["similarity"] < MIN_SIMILARITY:
            dropped_far += 1          # too far from the core papers — drift
            continue
        candidates.append(c)
    # Rank by topical closeness to the core first, then keyword relevance,
    # then citations/recency. Keeps the crawl tight around the anchor set.
    candidates.sort(
        key=lambda c: (c["similarity"], relevance_score(c),
                       c["cited_by_count"], c["year"] or 0),
        reverse=True,
    )
    candidates = candidates[:CANDIDATES_PER_LOOP]
    log(f"    {len(edges)} raw edges -> {len(candidates)} kept "
        f"({dropped_far} dropped as off-topic; "
        f"sim range {candidates[-1]['similarity'] if candidates else 0}"
        f"-{candidates[0]['similarity'] if candidates else 0})")

    verdict = None
    if candidates:
        verdict = run_llm(session, build_prompt(loop_no, parent_cand, candidates, read_context()), loop_no)
        if verdict is None:
            # Candidates existed but the model gave no usable verdict — an infra
            # failure (endpoint/model), not a legitimate empty result. Do NOT consume
            # this frontier node; leave it to retry and signal upward so a broken
            # LLM can never silently exhaust the frontier.
            log("    LLM gave no verdict — frontier node preserved for retry")
            return "llm_failed"

    # --- commit (validated). LLM proposal is untrusted input. ---
    cand_by_doi = {c["doi"]: c for c in candidates}
    if verdict:
        chosen = norm_doi(verdict.get("chosen_doi") or "")
        if chosen and chosen in cand_by_doi:
            cc = cand_by_doi[chosen]
            add_to_manifest(cc, manifest, manifest_dois,
                            reason=verdict.get("chosen_reason", ""),
                            similarity=cc["similarity"])
        for d in verdict.get("enqueue_dois", []) or []:
            d = norm_doi(d)
            if d in cand_by_doi and d not in state["pending_dois"] \
                    and d not in state["crawled_dois"]:
                state["pending_dois"].append(d)
                depth[d] = parent_depth + 1        # graph distance from core
                log(f"    + enqueued {d} (depth {depth[d]}, sim {cand_by_doi[d]['similarity']})")
    else:
        log("    no in-scope candidates to expand from this node")

    state["crawled_dois"].append(parent_doi)
    state["current_loop"] = loop_no
    commit(state, manifest)
    return "continue"


def add_to_manifest(cand, manifest, manifest_dois, reason="", similarity=None):
    local = download_pdf(cand)
    manifest.append({
        "doi": cand["doi"],
        "title": cand["title"],
        "year": cand["year"],
        "authors": cand.get("authors", []),
        "venue": cand.get("venue"),
        "cited_by_count": cand["cited_by_count"],
        "anchor_similarity": similarity,
        "discovered_by": SIMILARITY,
        "summary": (cand["abstract"] or "")[:500],
        "local_path": local or None,
        "selection_reason": reason,
        "timestamp_added": now_iso(),
    })
    manifest_dois.add(cand["doi"])
    log(f"    + library: {cand['doi']} ({'PDF' if local else 'metadata only'})")


def commit(state, manifest):
    backup_ledgers()
    atomic_write(STATE_FILE, state)
    atomic_write(MANIFEST_FILE, manifest)


# ----------------------------------------------------------------------------
def seed_state_from_base() -> dict:
    """For a namespaced run whose state file doesn't exist yet, start a fresh
    frontier from the base agent_state.json anchor set."""
    base = load_json(BASE_STATE_FILE, {})
    anchors = base.get("anchor_dois") or base.get("pending_dois") or []
    return {
        "current_loop": 0, "max_loops": MAX_LOOPS,
        "anchor_dois": list(anchors), "crawled_dois": [],
        "pending_dois": list(anchors), "depth": {},
    }


def main():
    os.makedirs(PAPERS_DIR, exist_ok=True)
    state = load_json(STATE_FILE, None) or seed_state_from_base()
    state.setdefault("max_loops", MAX_LOOPS)
    manifest = load_json(MANIFEST_FILE, [])
    manifest_dois = {norm_doi(m.get("doi", "")) for m in manifest}

    if not state["pending_dois"]:
        log("[!] pending_dois is empty. Seed agent_state.json with 1-3 DOIs first.")
        sys.exit(1)

    anchor_dois = [norm_doi(d) for d in state.get("anchor_dois") or state["pending_dois"]]
    anchor_profile = build_profile(anchor_dois)
    if not anchor_profile:
        log(f"[!] empty {SIMILARITY} anchor profile — cannot score candidates. Aborting.")
        sys.exit(1)
    # Anchors sit at the centre of the graph: depth 0.
    depth = state.setdefault("depth", {})
    for d in anchor_dois:
        depth.setdefault(d, 0)

    started_at = time.time()
    log(f"Crawl start [{SIMILARITY}{'/' + RUN if RUN else ''}] — {len(manifest)} papers, "
        f"{len(state['pending_dois'])} seeds, ceiling {state['max_loops']} loops, "
        f"min_similarity {MIN_SIMILARITY}, max_depth {MAX_DEPTH}")
    log_model_banner(log)

    run_atomic_loop(
        lambda session: run_loop(session, state, manifest, manifest_dois, anchor_profile, started_at),
        label="crawl judge", cooldown=COOLDOWN_SECONDS, max_failures=MAX_LLM_FAILURES,
        log=log, unit="frontier")

    log(f"Crawl finished — {len(manifest)} papers in library_manifest.json")


if __name__ == "__main__":
    main()
