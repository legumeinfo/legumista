#!/usr/bin/env python3
"""Shared engine for the autonomous pipeline phases.

Every autonomous phase (the Phase-1 crawl judge and the Phase-3 ideation worker) has
the same shape: open one agentic session, then spin an atomic loop that asks the model
for a JSON verdict, commits deterministic state, and stops on a budget. This module
factors out the parts that were identical across phases so each phase file only has to
supply its own per-loop *step* and its own prompts:

  - extract_json    : pull a JSON object out of a model reply
  - call_for_json   : one model call -> parsed verdict (+ raw loop log)
  - run_atomic_loop : the resumable loop engine (session lifecycle, failure counting,
                      cooldown, exception recovery, budget-driven stop)
  - log_model_banner: the standard "which model / which prompt" startup lines

Phase-specific mechanics (candidate scoring, idea maturation, ledger commits) stay in
orchestrator.py / ideation.py; only the generic control flow lives here.
"""
import json
import os
import re
import time

from legumista_agent.runtime import PhaseSession


def _balanced_spans(s: str):
    """Yield (start, end) spans of every top-level balanced {...} object in `s`,
    respecting quoted strings/escapes so braces inside JSON strings don't miscount."""
    spans, stack, start = [], 0, None
    in_str = esc = False
    for i, ch in enumerate(s):
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            if stack == 0:
                start = i
            stack += 1
        elif ch == "}" and stack > 0:
            stack -= 1
            if stack == 0 and start is not None:
                spans.append((start, i + 1))
                start = None
    return spans


def extract_json(text: str):
    """Pull a JSON object out of the model's reply. Tries a ```json (or bare ```) fenced
    block first, then the last balanced {...} span, then the first — a balanced-brace scan
    rather than a greedy regex, so prose braces don't corrupt the parse. Returns the
    parsed dict or None."""
    if not text:
        return None
    # 1) fenced code block — parse the first balanced object inside it.
    m = re.search(r"```(?:json)?\s*(.*?)```", text, re.DOTALL)
    if m:
        block = m.group(1)
        for a, b in _balanced_spans(block):
            try:
                return json.loads(block[a:b])
            except json.JSONDecodeError:
                continue
    # 2) whole text — try the last balanced object, then the first, then any.
    spans = _balanced_spans(text)
    for a, b in ([spans[-1], spans[0]] + spans if spans else []):
        try:
            return json.loads(text[a:b])
        except json.JSONDecodeError:
            continue
    return None


def call_for_json(session: PhaseSession, prompt: str, loop_no: int, log_dir: str, log):
    """One model step expected to yield a JSON verdict. Runs the agentic call, writes the
    raw loop_NNN.log for the run, and extracts the JSON. Returns
    (verdict, content, meta, note): `verdict` is the parsed dict or None; `note` is a
    short human-readable summary of any failure (empty on success). The caller writes its
    own last-loop transcript and decides what the verdict means."""
    os.makedirs(log_dir, exist_ok=True)
    content, meta = session.call(prompt)

    with open(os.path.join(log_dir, f"loop_{loop_no:03d}.log"), "w", encoding="utf-8") as f:
        f.write(f"model={meta.get('model')}\nmeta={meta}\n---RESPONSE---\n{content or ''}")

    verdict, note = None, ""
    if content is None:
        note = f"{meta.get('error')}: {meta.get('detail', '')}".strip(": ")[:200]
        log(f"    LLM error: {note}")
    else:
        verdict = extract_json(content)
        if verdict is None:
            note = content.strip().replace("\n", " ")[:200]
            log(f"    no JSON verdict in reply: {note}")
    return verdict, content, meta, note


def log_model_banner(log) -> None:
    """The startup banner shared by every phase: the resolved model/endpoint and whether
    a base system prompt is in effect."""
    import config
    lc = config.llm()
    log(f"model: {lc['model']} @ {lc['base_url']}"
        + ("" if config.llm_is_local() or config.llm_api_key()
           else "  [!] no API key — set the endpoint's key env var"))
    log("system prompt: " + ("loaded" if config.system_prompt_base() else "(none)"))


def run_atomic_loop(step, *, label, cooldown, max_failures, log, unit="state") -> None:
    """The resumable atomic-loop engine used by every autonomous phase.

    Opens one shared agentic session and repeatedly calls `step(session)`, which performs
    ONE atomic action and returns a status string:
      - "stop"        : a budget/stop condition was hit — end the run
      - "llm_failed"  : the model gave no usable verdict; the frontier/pool is left intact
                        for retry (this loop consumed nothing)
      - anything else : a good loop; reset the failure counter and cool down

    A KeyboardInterrupt ends the run cleanly; any other exception is logged and the loop
    recovers after a short pause, so one bad loop never kills a multi-hour run. After
    `max_failures` consecutive model failures the run aborts with all state preserved.
    `unit` names what stays intact on a model failure (e.g. "frontier", "pool") for the
    log line. The session is always closed on exit.
    """
    session = PhaseSession(label, log=log).open()
    try:
        failures = 0
        while True:
            try:
                status = step(session)
            except KeyboardInterrupt:
                log("[stop] interrupted by user")
                break
            except Exception as e:  # noqa: BLE001 - one bad loop must not kill the run
                log(f"[!] loop exception: {e} — recovering in 30s")
                time.sleep(30)
                continue

            if status == "stop":
                break
            if status == "llm_failed":
                failures += 1
                log(f"[!] no LLM verdict ({failures}/{max_failures}) — {unit} intact")
                if failures >= max_failures:
                    log("[abort] the model returned no usable verdict repeatedly. Check the "
                        "`llm` endpoint in legumista.yml (base_url/model reachable, API key "
                        "set). State is preserved — fix, then rerun.")
                    break
                time.sleep(cooldown)
                continue
            failures = 0                     # a good loop resets the counter
            time.sleep(cooldown)             # cooldown between spins
    finally:
        session.close()
