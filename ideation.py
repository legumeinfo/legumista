#!/usr/bin/env python3
"""
Autonomous research-ideation orchestrator (Phase 3, optional).

Same skeleton as the Phase-1 citation crawl (orchestrator.py): Python does the
deterministic mechanics (state, selection, promotion, logging, atomic commits); the
model is the creative worker, invoked once per loop and returning a JSON verdict that
never touches the canonical ledgers.

Where Phase 1 expands a citation graph, Phase 3 grows a *pool of ideas*. Each
loop does ONE atomic action:
  - GENERATE : propose N new, distinct ideas grounded in the corpus + synthesis
  - DEVELOP  : deepen + critically stress-test + score the best pending idea
               (and optionally spawn spin-off ideas)
An idea that has been refined enough and scores highly is PROMOTED to the
canonical ideas manifest. State is flushed every loop, so loop 1 and loop 100 are
equally cheap even on a modest model.

Inputs (read fresh each loop so edits steer a running session):
  ideation-goal.md              -> WHAT to discover (the framework; user-authored)
  corpus/corpus_digest.md       -> the discovered citation corpus (Phase 1 -> 2)
  reviews/<ts>_final/review.md  -> our synthesis of that corpus (latest)
  context_inputs/*.md           -> shared steering (re-read every loop)

Ledgers (canonical, only this script writes them):
  ideation_state.json           -> idea pool + loop/budget counters
  ideas/ideas_manifest.json     -> promoted (matured) ideas — the deliverable

A malformed / missing LLM verdict skips the loop WITHOUT mutating state
(retry-safe), exactly like Phase 1.
"""

import glob
import json
import os
import re
import sys
import time

# Reuse the generic plumbing from Phase 1 (pure helpers, no side effects on
# import — orchestrator only touches disk under __main__).
from orchestrator import (
    WORKSPACE,
    atomic_write,
    atomic_write_text,
    load_json,
    log,
    now_iso,
    read_context,
)

import config  # project identity + prompt templates (topic-specific config)
# Shared pipeline engine (identical control flow across phases):
from pipeline import call_for_json, log_model_banner, run_atomic_loop

# ----------------------------------------------------------------------------
# Configuration (override via environment)
# ----------------------------------------------------------------------------
GOAL_FILE = os.environ.get("IDEATE_GOAL_FILE", os.path.join(WORKSPACE, "ideation-goal.md"))
DIGEST_FILE = os.path.join(WORKSPACE, "corpus", "corpus_digest.md")
STATE_FILE = os.path.join(WORKSPACE, "ideation_state.json")
IDEAS_DIR = os.path.join(WORKSPACE, "ideas")
MANIFEST_FILE = os.path.join(IDEAS_DIR, "ideas_manifest.json")
LOG_DIR = os.path.join(WORKSPACE, "reviews", "ideation_logs")
# Overwriting transcript of the latest loop's full exchange (prompt+response),
# for eyeballing idea/prompt quality. Numbered loop_NNN.log is the audit trail.
LAST_LOOP_TRANSCRIPT = os.path.join(LOG_DIR, "last_loop.md")

MAX_LOOPS = int(os.environ.get("IDEATE_MAX_LOOPS", "60"))
MAX_IDEAS = int(os.environ.get("IDEATE_MAX_IDEAS", "12"))       # matured target -> stop
WALLCLOCK_HOURS = float(os.environ.get("IDEATE_WALLCLOCK_HOURS", "24"))
COOLDOWN_SECONDS = int(os.environ.get("IDEATE_COOLDOWN_SECONDS", "15"))
SEED_MIN = int(os.environ.get("IDEATE_SEED_MIN", "6"))          # keep >= this many pending
SEEDS_PER_GEN = int(os.environ.get("IDEATE_SEEDS_PER_GEN", "3"))
MIN_REFINEMENTS = int(os.environ.get("IDEATE_MIN_REFINEMENTS", "2"))
PROMOTE_SCORE = int(os.environ.get("IDEATE_PROMOTE_SCORE", "14"))   # of 20 (4 dims x5)
MAX_CHILDREN = int(os.environ.get("IDEATE_MAX_CHILDREN", "1"))
CORPUS_CHARS = int(os.environ.get("IDEATE_CORPUS_CHARS", "16000"))  # digest budget
SYNTH_CHARS = int(os.environ.get("IDEATE_SYNTH_CHARS", "12000"))    # synthesis budget
MAX_LLM_FAILURES = int(os.environ.get("IDEATE_MAX_LLM_FAILURES", "3"))

SCORE_DIMS = ("novelty", "feasibility", "impact", "grounding")


# ----------------------------------------------------------------------------
# Inputs
# ----------------------------------------------------------------------------
def read_text(path: str, limit: int = 0) -> str:
    if not os.path.exists(path):
        return ""
    with open(path, encoding="utf-8") as f:
        txt = f.read().strip()
    if limit and len(txt) > limit:
        txt = txt[:limit] + f"\n\n[... truncated to {limit} chars ...]"
    return txt


def latest_synthesis() -> str:
    """Most recent Phase-2 review.md (timestamped dirs sort lexically)."""
    hits = sorted(glob.glob(os.path.join(WORKSPACE, "reviews", "*_final", "review.md")))
    return hits[-1] if hits else ""


# ----------------------------------------------------------------------------
# State I/O (this script is the ONLY writer)
# ----------------------------------------------------------------------------
def new_state() -> dict:
    return {"current_loop": 0, "max_loops": MAX_LOOPS, "next_id": 1, "ideas": []}


def backup(path: str) -> None:
    if os.path.exists(path):
        import shutil
        shutil.copy2(path, f"{path}.bak")


def commit(state: dict, manifest: list) -> None:
    backup(STATE_FILE)
    backup(MANIFEST_FILE)
    atomic_write(STATE_FILE, state)
    atomic_write(MANIFEST_FILE, manifest)


def norm_title(t: str) -> str:
    return re.sub(r"\s+", " ", (t or "").strip().lower())


# ----------------------------------------------------------------------------
# LLM ideation worker (the ONLY job the model does; no tools)
# ----------------------------------------------------------------------------
IDEA_SCHEMA = (
    '{"title": "<short title>", "one_liner": "<one sentence>", '
    '"hypothesis": "<the concrete discovery/claim to test>", '
    '"approach": "<method outline; inputs->tools/methods->analysis>", '
    '"datasets": ["<which corpus dataset/resource, with DOI where known>"], '
    '"tools": ["<software/pipeline/method>"], '
    '"novelty": "<why this is not already done in the corpus>", '
    '"feasibility": "<data/resource feasibility>", '
    '"risks": "<main risks/unknowns>", '
    '"grounding_dois": ["<corpus DOI this builds on>"]}'
)


def grounding_block(goal, corpus, synth, steering) -> str:
    # Steering is optional: append its labelled section only when present.
    steer = f"\n\nSTEERING (re-read every loop):\n{steering}" if steering.strip() else ""
    return f"""GOAL FRAMEWORK — the definition of what to discover; obey it strictly:
{goal}

DISCOVERED CORPUS (Phase 1/2 citation library, closest-to-core first):
{corpus or '(no corpus digest found)'}

OUR SYNTHESIS OF THE CORPUS (Phase 2 review):
{synth or '(no synthesis review found)'}{steer}"""


def build_generate_prompt(loop_no, existing_titles, ctx) -> str:
    existing = "\n".join(f"  - {t}" for t in existing_titles) or "  (none yet)"
    # Wording lives in prompts/ideate_generate.md; DOMAIN_CONSTRAINT auto-injected.
    return config.render(
        "ideate_generate.md",
        LOOP_NO=loop_no, CTX=ctx, EXISTING=existing,
        SEEDS_PER_GEN=SEEDS_PER_GEN, IDEA_SCHEMA=IDEA_SCHEMA,
    )


def build_develop_prompt(loop_no, idea, ctx) -> str:
    idea_json = json.dumps(
        {k: idea.get(k) for k in
         ('title', 'one_liner', 'hypothesis', 'approach', 'datasets',
          'tools', 'novelty', 'feasibility', 'risks', 'grounding_dois')},
        indent=2, ensure_ascii=False)
    # Wording lives in prompts/ideate_develop.md; DOMAIN_CONSTRAINT auto-injected.
    return config.render(
        "ideate_develop.md",
        LOOP_NO=loop_no, CTX=ctx, IDEA_ID=idea['id'],
        SCORE_DIMS=", ".join(SCORE_DIMS), MAX_CHILDREN=MAX_CHILDREN,
        IDEA_JSON=idea_json, IDEA_SCHEMA=IDEA_SCHEMA,
    )


# Ideation runs through the same shared pipeline engine as the crawl (see pipeline.py).
# Its behaviour is steered by ideation-goal.md and the ideate_* prompt templates, not by
# any separate system prompt or control flow.


def write_transcript(loop_no, action, prompt, content, meta, verdict, note=""):
    """Overwrite last_loop.md with the full worker exchange for this loop. Written
    on every outcome so a failed loop is still inspectable (mirrors Phase 1)."""
    meta = meta or {}
    parts = [
        f"# Ideation transcript — loop {loop_no} ({action})",
        "",
        f"- generated: {now_iso()}",
        f"- model: {meta.get('model', '?')}",
    ]
    if meta.get("usage"):
        parts.append(f"- usage: {meta['usage']}")
    if meta.get("finish_reason"):
        parts.append(f"- finish_reason: {meta['finish_reason']}")
    if note:
        parts.append(f"- note: {note}")
    parts += [
        "", "## Prompt (sent to worker)", "", "```text", prompt, "```",
        "", "## Worker response", "", "```text", (content or "").strip() or "(empty)", "```",
        "", "## Parsed verdict", "", "```json",
        (json.dumps(verdict, indent=2, ensure_ascii=False)
         if verdict is not None else "(no valid verdict parsed)"), "```", "",
    ]
    atomic_write_text(LAST_LOOP_TRANSCRIPT, "\n".join(parts))


def run_llm(session, prompt: str, loop_no: int, action: str):
    """Ask the model (via the shared agentic session) for a JSON verdict as the ideation
    worker. Returns the parsed dict, or None on failure. Always overwrites last_loop.md
    with the full transcript."""
    verdict, content, meta, note = call_for_json(session, prompt, loop_no, LOG_DIR, log)
    write_transcript(loop_no, action, prompt, content, meta, verdict, note)
    return verdict


# ----------------------------------------------------------------------------
# Idea-pool mechanics (deterministic)
# ----------------------------------------------------------------------------
IDEA_FIELDS = ("title", "one_liner", "hypothesis", "approach", "datasets",
               "tools", "novelty", "feasibility", "risks", "grounding_dois")


def make_idea(state, raw, loop_no, parent_id=None) -> dict | None:
    """Build a pool record from an LLM-proposed idea. Returns None if it has no
    title or duplicates an existing one."""
    title = (raw.get("title") or "").strip()
    if not title:
        return None
    seen = {norm_title(i["title"]) for i in state["ideas"]}
    if norm_title(title) in seen:
        return None
    idea = {k: raw.get(k) for k in IDEA_FIELDS}
    idea.update({
        "id": state["next_id"],
        "status": "pending",
        "score": None,
        "scores": None,
        "critique": None,
        "refined_count": 0,
        "parent_id": parent_id,
        "created_loop": loop_no,
        "updated_loop": loop_no,
        "history": [f"loop {loop_no}: proposed"
                    + (f" (spin-off of #{parent_id})" if parent_id else "")],
    })
    state["next_id"] += 1
    return idea


def pick_target(state) -> dict | None:
    """Next pending idea to develop: CLOSEST to maturity first (most refinements,
    then highest score), so promising ideas actually cross the promote threshold.
    Developing least-refined first instead starves maturation, because every loop
    can spawn a fresh refined_count=0 spin-off — so nothing ever reaches the second
    refinement. Pending ideas always have refined_count < MIN_REFINEMENTS (once an
    idea hits that count it is matured or retired, leaving 'pending')."""
    pending = [i for i in state["ideas"] if i["status"] == "pending"]
    if not pending:
        return None
    return sorted(pending, key=lambda i: (-i["refined_count"],
                                          -(i["score"] or 0), i["id"]))[0]


def clamp_score(v) -> int:
    try:
        return max(0, min(5, int(v)))
    except (TypeError, ValueError):
        return 0


def matured_count(manifest) -> int:
    return len(manifest)


# ----------------------------------------------------------------------------
# One atomic loop
# ----------------------------------------------------------------------------
def run_loop(session, state, manifest, ctx, started_at) -> str:
    """Run one atomic loop. Returns 'continue', 'stop', or 'llm_failed'
    (infra failure — pool deliberately left intact for retry)."""
    loop_no = state["current_loop"] + 1

    # --- stop conditions ---
    if state["current_loop"] >= state.get("max_loops", MAX_LOOPS):
        log(f"[stop] loop ceiling ({state['max_loops']}) reached")
        return "stop"
    if matured_count(manifest) >= MAX_IDEAS:
        log(f"[stop] matured-idea ceiling ({MAX_IDEAS}) reached")
        return "stop"
    if (time.time() - started_at) > WALLCLOCK_HOURS * 3600:
        log(f"[stop] wall-clock budget ({WALLCLOCK_HOURS}h) reached")
        return "stop"

    pending = [i for i in state["ideas"] if i["status"] == "pending"]
    # Generate to keep the pool topped up; develop otherwise. But if a previous
    # generate loop could produce no NEW distinct ideas (`gen_exhausted`), stop
    # trying to refill and just develop what we already have — otherwise a tapped-
    # out model would burn every remaining loop re-proposing duplicates.
    if not pending:
        action = "generate"
    elif len(pending) < SEED_MIN and not state.get("gen_exhausted"):
        action = "generate"
    else:
        action = "develop"
    log(f"===== IDEATION LOOP {loop_no}/{state['max_loops']}  |  {action}  |  "
        f"pool={len(state['ideas'])} pending={len(pending)} "
        f"matured={matured_count(manifest)}/{MAX_IDEAS} =====")

    if action == "generate":
        titles = [i["title"] for i in state["ideas"]]
        verdict = run_llm(session, build_generate_prompt(loop_no, titles, ctx), loop_no, action)
        if verdict is None:
            log("    LLM gave no verdict — pool preserved for retry")
            return "llm_failed"
        added = 0
        for raw in (verdict.get("ideas") or [])[:SEEDS_PER_GEN]:
            idea = make_idea(state, raw, loop_no)
            if idea:
                state["ideas"].append(idea)
                added += 1
                log(f"    + idea #{idea['id']}: {idea['title']}")
        if added:
            state["gen_exhausted"] = False
        else:
            state["gen_exhausted"] = True   # nothing new -> pivot to developing
            if not any(i["status"] == "pending" for i in state["ideas"]):
                log("[stop] cannot generate new ideas and nothing left to develop")
                state["current_loop"] = loop_no
                commit(state, manifest)
                return "stop"
            log("    (no new distinct ideas; will develop existing pending ideas)")
    else:  # develop
        target = pick_target(state)
        verdict = run_llm(session, build_develop_prompt(loop_no, target, ctx), loop_no, action)
        if verdict is None:
            log("    LLM gave no verdict — pool preserved for retry")
            return "llm_failed"
        apply_develop(state, manifest, target, verdict, loop_no)

    state["current_loop"] = loop_no
    commit(state, manifest)
    return "continue"


def apply_develop(state, manifest, target, verdict, loop_no) -> None:
    upd = verdict.get("updated") or {}
    for k in IDEA_FIELDS:
        if upd.get(k):                       # only overwrite with non-empty values
            target[k] = upd[k]
    if verdict.get("critique"):
        target["critique"] = verdict["critique"]
    raw_scores = verdict.get("scores") or {}
    scores = {d: clamp_score(raw_scores.get(d)) for d in SCORE_DIMS}
    target["scores"] = scores
    target["score"] = sum(scores.values())
    target["refined_count"] += 1
    target["updated_loop"] = loop_no
    target["history"].append(
        f"loop {loop_no}: refined #{target['refined_count']} "
        f"(score {target['score']}/20)")
    log(f"    ~ developed #{target['id']} '{target['title']}' "
        f"-> score {target['score']}/20, refinements {target['refined_count']}")

    # Spin-off ideas
    for raw in (verdict.get("children") or [])[:MAX_CHILDREN]:
        child = make_idea(state, raw, loop_no, parent_id=target["id"])
        if child:
            state["ideas"].append(child)
            log(f"    + spin-off idea #{child['id']}: {child['title']}")

    # Resolve the idea once it has used its refinement budget: promote if it scores
    # highly enough, otherwise retire it so it leaves the pending pool (a still-
    # 'pending' idea at the refinement cap would otherwise be re-picked forever and
    # block maturation of everything behind it).
    if target["refined_count"] >= MIN_REFINEMENTS:
        if target["score"] >= PROMOTE_SCORE:
            target["status"] = "matured"
            target["timestamp_matured"] = now_iso()
            manifest.append(dict(target))
            log(f"    ★ PROMOTED #{target['id']} '{target['title']}' to manifest "
                f"({matured_count(manifest)}/{MAX_IDEAS})")
        else:
            target["status"] = "rejected"
            log(f"    ✗ retired #{target['id']} '{target['title']}' "
                f"(score {target['score']}/20 < {PROMOTE_SCORE} after "
                f"{target['refined_count']} refinements)")


# ----------------------------------------------------------------------------
def main():
    # The ideation goal (WHAT to discover) is OPTIONAL per project: use the project's
    # ideation-goal.md if it supplies one, else the clean default shipped with the package.
    # No placeholder scaffold — it reads naturally when a project provides nothing.
    default_goal = read_text(os.path.join(config.PKG_ASSETS, "ideation-goal.md"))
    have_goal = bool(read_text(GOAL_FILE))

    corpus = read_text(DIGEST_FILE, CORPUS_CHARS)
    if not corpus:
        log(f"[!] {os.path.relpath(DIGEST_FILE, WORKSPACE)} not found. Run "
            "`legumista review` (or at least `legumista digest`) so ideation has a "
            "corpus to ground on.")
        sys.exit(1)
    synth_path = latest_synthesis()
    synth = read_text(synth_path, SYNTH_CHARS)
    if not synth:
        log("[!] no Phase-2 review.md found under reviews/*_final/ — ideating on the "
            "corpus digest alone (synthesis strongly recommended; run Phase 2 first).")

    os.makedirs(IDEAS_DIR, exist_ok=True)
    state = load_json(STATE_FILE, None) or new_state()
    # Take the ceiling from config each run so resuming with a higher IDEATE_MAX_LOOPS
    # actually extends a finished/exhausted run (rather than being pinned to whatever
    # was baked into the state file on the first run).
    state["max_loops"] = MAX_LOOPS
    manifest = load_json(MANIFEST_FILE, [])

    log(f"Ideation start — pool {len(state['ideas'])}, matured {len(manifest)}, "
        f"ceiling {state['max_loops']} loops / {MAX_IDEAS} ideas, "
        f"promote>= {PROMOTE_SCORE}/20 after {MIN_REFINEMENTS} refinements")
    log(f"inputs: goal={os.path.relpath(GOAL_FILE, WORKSPACE) if have_goal else '(packaged default)'}, "
        f"corpus={os.path.relpath(DIGEST_FILE, WORKSPACE)}, "
        f"synthesis={os.path.relpath(synth_path, WORKSPACE) if synth_path else '(none)'}")
    log_model_banner(log)

    # Steering + heavy inputs are re-read each loop (cheap; lets you edit ideation-goal.md
    # / context_inputs mid-run to steer without a restart). The goal falls back to the
    # packaged default when the project doesn't supply one.
    started_at = time.time()

    def step(session):
        goal = read_text(GOAL_FILE) or default_goal    # re-read: live steering
        ctx = grounding_block(goal, corpus, synth, read_context())
        return run_loop(session, state, manifest, ctx, started_at)

    run_atomic_loop(step, label="ideation worker", cooldown=COOLDOWN_SECONDS,
                    max_failures=MAX_LLM_FAILURES, log=log, unit="pool")

    log(f"Ideation finished — {len(manifest)} matured ideas in "
        f"{os.path.relpath(MANIFEST_FILE, WORKSPACE)} "
        f"(pool {len(state['ideas'])}). Build the report with `legumista report`.")


if __name__ == "__main__":
    main()
