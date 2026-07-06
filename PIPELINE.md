# The research-discovery pipeline

This project automates a three-stage arc of scientific research discovery, driven by
an **OpenAI-compatible LLM endpoint** (local ollama, OpenRouter, or any such server):

1. **Discovery** — build a focused literature corpus by crawling the citation graph.
2. **Synthesis** — turn that corpus into a structured literature review.
3. **Ideation** — turn the corpus *and* its synthesis into new, concrete research
   directions (optionally expanded into full technical proposals).

The three phases mirror how a researcher actually enters a new field: first you
*gather* what's known, then you *make sense* of it, then you *find the openings*.
Each phase is a separate, resumable program so you can stop, inspect, re-steer, and
continue at any point.

---

## Design philosophy (read this first — it explains every choice below)

Two invariants hold across all phases:

- **Python owns the mechanics and the files; the LLM is a narrow worker.** The
  deterministic parts — fetching citation edges, filtering, scoring, downloading,
  deduping, and *writing every canonical file* — are done in Python. The model is
  only ever asked to make a bounded judgement (which paper is relevant, what the
  review says, which idea is worth developing) and return it as text/JSON. It never
  edits the ledgers. This means a bad model turn (malformed JSON, a refusal, a
  timeout) **skips a step instead of corrupting state**.
- **Everything is grounded in retrieved evidence, never model memory.** The corpus
  is built from a verified anchor set and real citation edges; the review and the
  ideas are written only from the corpus text that Python feeds in. This is what
  keeps the output trustworthy and free of hallucinated papers, DOIs, or findings.

Because it assumes only a **single model endpoint** (and is happy on modest or local
hardware), the pipeline is strictly **sequential**: exactly one blocking model call at a
time, a cooldown between calls, and no parallelism. State is flushed to disk every loop,
so loop 1 and loop 100 are equally cheap — no context growth, no memory blow-up over a
multi-hour run. Run long phases under `tmux`.

---

## Data lineage (where everything lives)

```
   INPUTS (you curate)                 PHASE 1: DISCOVERY
   ┌────────────────────┐              ┌──────────────────────────┐
   │ agent_state.json   │  anchors ──► │ orchestrator.py (crawl)  │
   │   .anchor_dois     │              │  OpenAlex edges + scoring│
   │   .pending_dois    │              │  + OA PDF download       │
   │ context_inputs/*.md│  steering ──►│                          │
   └────────────────────┘              └───────────┬──────────────┘
                                                    │ writes
                                        library_manifest.json  +  papers/*.pdf
                                                    │
                                     build_digest.py (deterministic)
                                                    ▼
   PHASE 2: SYNTHESIS               corpus/corpus_digest.md
   ┌──────────────────────────┐              │
   │ legumista review             │  reads ◄─────┘
   │  (digest inlined ->       │
   │   /chat/completions)      │ ──► reviews/<ts>_final/review.md
   └──────────────────────────┘
                                                    │
   PHASE 3: IDEATION                                ▼  (corpus + synthesis)
   ┌──────────────────────────┐        ┌──────────────────────────┐
   │ ideation-goal.md (you)   │ ─────► │ ideation.py (idea loop)  │
   └──────────────────────────┘        └───────────┬──────────────┘
                                                    │ writes
                             ideation_state.json (pool) + ideas/ideas_manifest.json
                                                    │
                          build_ideation_report.py  (quick ranked index)
                                                    ▼  ideas/ideation_report.md
   PHASE 3b: EXPANSION                              │
   ┌──────────────────────────┐        ┌───────────┴──────────────┐
   │ expand_ideas.py          │ ─────► │ full proposals per idea  │
   └──────────────────────────┘        └──────────────────────────┘
                              ideas/reports/*.md + ideas/ideas_full_report.md
```

**Canonical files are written only by their phase's program — never hand-edit them.**
`library_manifest.json`, `ideation_state.json`, and `ideas/ideas_manifest.json` are
ledgers; the `.md` reports and digests are regenerable views built from them.

---

## Phase 1 — Discovery (citation-graph crawl)

**Why a citation-graph crawl?** Keyword search alone misses the field: it surfaces
whatever matches your terms and drowns you in generic method papers. A citation
crawl instead starts from a small set of *verified core papers* (the **anchors**)
and walks their references (backward) and citations (forward), so it discovers the
literature the field itself considers connected — including papers that use
different vocabulary. The risk is drift (wandering into adjacent fields), which the
pipeline actively fights.

**What it does, each loop:**
1. Pop the next paper from the frontier (`pending_dois`).
2. Fetch its citation edges from OpenAlex (references + citing works).
3. Filter to scope (has a DOI, within the year range) and dedupe.
4. **Score each candidate for closeness to the anchor set** and drop anything below
   the gate (anti-drift).
5. Pre-rank the survivors and hand a short list to the LLM, which returns a JSON
   verdict: the single best paper to collect now, plus which DOIs to keep expanding.
6. Python collects the chosen paper (downloads its OA PDF if available), enqueues
   the approved DOIs, and commits `agent_state.json` + `library_manifest.json`.

**Anti-drift — the two distance metrics.** Every candidate is scored against the
anchors by one of two algorithms (set with `CRAWL_SIMILARITY`):

- **`content`** (default) — cosine similarity of concept/topic vectors (topical
  labels). "Is this about the same *subject*?"
- **`structural`** — bibliographic-coupling cosine (how much the candidate cites the
  same foundational works as the anchors). "Does this draw on the same *literature*?"
  This catches recent or under-tagged papers the content metric misses.

Candidates below `CRAWL_MIN_SIMILARITY` are dropped before the model sees them, and
a `CRAWL_MAX_DEPTH` hop cap bounds how far from the core the frontier may travel.
Each kept paper records its `anchor_similarity` in the manifest.

**Running both metrics and merging.** They're complementary, so run each as an
independent namespaced crawl and union the results:

```bash
legumista crawl -s content    --run content
legumista crawl -s structural --run structural
legumista merge content structural         # -> library_manifest.json (+ Venn)
```

`legumista merge` dedups by DOI, tags each paper with `discovered_by`
(`content`/`structural`/both), and prints how many papers each metric found uniquely
vs. in common — papers found by *both* are the strongest core.

**Inputs you control:** `agent_state.json:anchor_dois` (the verified core — the
single source of truth for "on-topic") and `context_inputs/*.md` (steering: ranking
priorities and scope, **re-read every loop** so you can pivot a running crawl).

**Run / reset:** (endpoint configured in `legumista.yml `llm`` — see README)
```bash
legumista crawl                       # (or `legumista discover` to chain synthesis) to the ceiling
legumista reset crawl                 # frontier back to anchors (--hard also wipes PDFs)
```

**Output:** `library_manifest.json` (the canonical corpus ledger) + PDFs in `papers/`.

---

## Phase 2 — Synthesis (structured review)

**Why synthesize?** A pile of papers isn't understanding. This phase turns the
corpus into the kind of review a researcher would write: what the field agrees on,
where it disagrees, and what's missing. That last part — the **research gaps** — is
the bridge to ideation.

**What it does:**
1. `build_digest.py` deterministically rebuilds `corpus/corpus_digest.md` from the
   manifest: a ranked, deduplicated, review-ready text digest (title, authors, year,
   venue, similarity, abstract/summary per paper). This is the *seam* between phases
   — synthesis reads this text, never the raw JSON, and never the PDFs.
2. The digest text is **inlined into the prompt** and sent as a single
   chat-completion request; the model returns the review as its message content,
   which the script captures and writes to `review.md` itself.

**Why inline the digest?** The digest is inlined as the model's grounded **citation
base**: the review must cite only what's in it, and the script owns both the input (the
inlined digest) and the output (`review.md`), so the model can't smuggle in un-vetted
sources or fail to "save" the file. The model *may* additionally call the read-only
research tools to verify a claim or pull an exact statistic while it writes (e.g.
`read_paper` on a corpus DOI) — but those tools return extracted **text**, never
images/PDFs, so nothing chokes the endpoint. A modest model handles the core task
because the evidence is right there in the prompt.

The review follows a fixed structure: **Consensus Overview → Key Thematic Pillars →
Friction Points → Research Gaps → References** (only papers in the digest).

**Run:**
```bash
legumista review                      # or: legumista review --angle "your framing"
```

**Output:** `reviews/<timestamp>_final/review.md` (+ a `corpus_digest.md` snapshot
and `run.jsonl` log alongside it).

---

## Phase 3 — Ideation (autonomous idea generation)

**Why ideate?** Once you know the field and its gaps, the payoff is *new research
directions*. This phase reuses the Phase-1 engine — the same autonomous, atomic-loop,
one-model-call-per-step design — but instead of expanding a citation graph it grows a
**pool of research ideas**, each grounded in the discovered corpus and its synthesis.

**What to discover is entirely up to you:** `ideation-goal.md` (you author it) is the
framework the phase executes on — e.g. "purely computational / bioinformatics paper
ideas exploitable across the whole discovered corpus." The loop reads it (plus the
corpus digest and the latest review) fresh every loop.

**The loop, each step:**
- **GENERATE** (while the pool has few pending ideas) — propose a few new, distinct
  ideas grounded in specific corpus papers.
- **DEVELOP** (otherwise) — take the least-refined pending idea, deepen its approach,
  ground each claim in specific corpus DOIs, write an honest self-critique, and score
  it 0–5 on novelty / feasibility / impact / grounding (total /20). May spawn a
  spin-off idea.
- **PROMOTE** — when an idea has been developed at least `IDEATE_MIN_REFINEMENTS`
  times **and** scores ≥ `IDEATE_PROMOTE_SCORE`/20, it's copied to the canonical
  `ideas/ideas_manifest.json` (the deliverable).

It develops the idea **closest to maturity first** (most refinements, then highest
score), so a promising idea is pushed over the promote threshold within a couple of
loops instead of being starved by the fresh spin-offs each `develop` can spawn. An
idea that uses its whole refinement budget (`IDEATE_MIN_REFINEMENTS`) without
reaching `IDEATE_PROMOTE_SCORE` is **retired** so it stops competing for loops. A
very short or cancelled run may still end before anything matures; the *pool*
(`ideation_state.json`) holds the work in progress, and `EXPAND_INCLUDE_POOL=1`
(below) lets you expand it. To extend a finished run, re-run with a higher
`--max-loops` — the ceiling is taken from config each time, so it resumes.

**Run / reset:**
```bash
legumista ideate                      # loop -> index -> (Phase 3b) full proposals
legumista reset ideation              # clear generated pool/manifest/reports/logs
```

**Output:** `ideas/ideas_manifest.json` (matured ideas) and `ideas/ideation_report.md`
(quick ranked index).

---

## Phase 3b — Expansion (full technical proposals)

**Why a separate pass?** The ideation loop keeps each idea a *compact structured
record* so the local model's per-loop JSON stays reliable. To get depth, this pass
re-reads the **whole** corpus digest + synthesis for each idea and asks the model to
write a full, citation-grounded proposal — Background, Data & inputs, Computational
approach, Novelty vs. prior work, Feasibility, Risks, Validation, References — citing
specific corpus findings by DOI. Same hardening as Phase 2 (digest inlined as the
citation base, prompt on **stdin** to dodge the argument-length cap, deterministic
capture; the model may verify with read-only tools but cites only the corpus).

It runs automatically at the end of `legumista ideate` (disable with `--no-expand`), or
standalone:
```bash
legumista expand                    # expands matured ideas
legumista expand --include-pool     # also expand the scored pool
```

**Output:** `ideas/reports/<NN>_<slug>.md` per idea + a combined
`ideas/ideas_full_report.md`.

---

## Running the whole arc

Prerequisites: `legumista` installed (`pip install -e .`) and a model endpoint
configured in `legumista.yml `llm`` — either an OpenRouter key (`export
OPENROUTER_API_KEY=...`) or local ollama (`ollama serve`). See README
§"Model configuration". No proxy, no external runtime.

```bash
# 0. Curate: put verified anchor DOIs in agent_state.json. Optionally add scope in
#    context_inputs/*.md and author ideation-goal.md (both inherit defaults if absent).
legumista status           # confirm the endpoint is reachable / key is set
tmux new -s discovery
legumista discover         # Phase 1 + 2 (crawl: hours, then synthesis: minutes)
legumista ideate           # Phase 3 + 3b (hours)
```

Everything is resumable: re-running a phase continues from its committed ledger.
`legumista reset crawl` / `legumista reset ideation` start a phase over; `legumista status`
shows corpus/idea counts and whether the configured endpoint is reachable.

### Timing & resources
The pipeline is strictly **sequential** — one chat-completion request at a time. On a
local model each call can take minutes (budget hours for Phase 1 and Phase 3; use
`tmux`); against a hosted endpoint it's faster but still one call at a time. There is
no parallelism, so a single-GPU ollama box never has to hold more than one context.

---

## Adapting the pipeline to a new research topic

The pipeline is topic-agnostic; Lupinus genomics is just the shipped example. Each
topic is its **own project directory** (`legumista.yml` + data); one installed
`legumista` drives any number of them. `legumista` finds the project git-style — the nearest
`legumista.yml` up from the current directory — or you point at one with `-C <dir>`.

Scaffold a new topic and fill it in. `init` writes only the **bare config** —
`legumista.yml` and an empty `agent_state.json`; output dirs (`papers/`, `reviews/`,
`corpus/`, `ideas/`) are created on the first run, prompts fall back to the packaged
defaults, and steering/goal files are optional (add them when you want them):

```bash
legumista init ~/topics/crispr        # writes legumista.yml + agent_state.json only
cd ~/topics/crispr
```

1. **Edit `legumista.yml`** — the identity file: `name`, `slug`, `subject`, `scope`,
   `years`, `contact_email`, `lexical` pre-ranking terms, `ideation.domain_constraint`,
   and the `llm` endpoint. (Missing/partial keys fall back to `config.py` defaults.)
2. **Seed `agent_state.json`** with 3–15 *verified* anchor DOIs (both `anchor_dois`
   and `pending_dois`; never invent DOIs).
3. **(Optional) add `context_inputs/*.md`** — scope + ranking rules, re-read every loop.
   Absent = no extra steering; add a file to pivot a running crawl.
4. **(Optional) add `ideation-goal.md`** — what Phase 3 should discover. Absent = the
   packaged default goal.
5. **(Optional) override a prompt** — drop a `prompts/<name>.md` in the project (e.g.
   `prompts/review.md`) to replace just that phase's wording; `{{PLACEHOLDER}}` slots are
   filled from `legumista.yml`. Any prompt you don't override inherits the packaged default.
6. **(Optional) override the system prompt** — every model call uses the base prompt
   shipped with the package; drop a `system-prompt.md` in the project dir (or point
   `$LEGUMISTA_SYSTEM_PROMPT` at one) to customise persona/rules for your field.
7. Set the model endpoint in `legumista.yml `llm`` (OpenRouter key or local ollama —
   README §"Model configuration"), then `legumista discover`.

**Code (installed once, shared):** `legumista_cli.py`, `orchestrator.py`, `ideation.py`,
`config.py`, `legumista_agent/`, and the bundled default `prompts/`. **Data you own per
project directory:** `legumista.yml`, `agent_state.json`, any optional `context_inputs/`,
`ideation-goal.md`, overridden `prompts/`, and all outputs (`library_manifest.json`,
`corpus/`, `reviews/`, `ideas/`, `papers/`). See [`examples/lupinus/`](examples/lupinus/)
for a fully populated project.

## Knobs (environment variables)

| Phase | Variable | Default | Effect |
|---|---|---|---|
| 1 | `CRAWL_SIMILARITY` | `content` | `content` or `structural` distance metric |
| 1 | `CRAWL_RUN` | (none) | namespace for parallel/independent crawls |
| 1 | `CRAWL_MIN_SIMILARITY` | 0.35 / 0.08 | anti-drift gate (per metric) |
| 1 | `CRAWL_MAX_DEPTH` | 3 | max citation hops from an anchor |
| 1 | `CRAWL_MAX_LOOPS` / `CRAWL_MAX_PAPERS` | 100 / 250 | crawl ceilings |
| 1 | `CRAWL_YEAR_MIN` / `CRAWL_YEAR_MAX` | 2006 / 2026 | scope years |
| 1 | `CRAWL_DOWNLOAD_PDFS` | 1 | fetch OA PDFs (0 = metadata only) |
| 3 | `IDEATE_MAX_LOOPS` / `IDEATE_MAX_IDEAS` | 60 / 12 | ideation ceilings |
| 3 | `IDEATE_MIN_REFINEMENTS` | 2 | develops before an idea can mature |
| 3 | `IDEATE_PROMOTE_SCORE` | 14 | score (/20) required to mature |
| 3 | `IDEATE_SEED_MIN` / `IDEATE_SEEDS_PER_GEN` | 6 / 3 | pool refill behaviour |
| 3b | `IDEATE_EXPAND` | 1 | run the write-up pass after the loop |
| 3b | `EXPAND_INCLUDE_POOL` | 0 | expand the scored pool, not just matured |
| all | `LLM_TIMEOUT` | 3600 | per-call cap in seconds (mirrors `llm.timeout`) |
| all | `LEGUMISTA_AGENT_MAX_TURNS` | 8 | tool-loop turns per model step (config: `agent.max_turns`) |
| all | `*_COOLDOWN_SECONDS` | — | pacing between calls |

---

## Troubleshooting (issues seen in practice)

| Symptom | Cause | Fix |
|---|---|---|
| Connection refused / connection timed out | endpoint unreachable (ollama not running, wrong `base_url`, network) | start the server / fix `base_url`; run `legumista status` to confirm reachability |
| Generation times out mid-run | a call exceeded `llm.timeout` (big corpus on a big model) | raise `llm.timeout` (default 3600) or export `LLM_TIMEOUT` |
| Repeated "no LLM verdict" then abort | endpoint down / wrong model / missing key | `legumista status`; check `legumista.yml `llm`` base_url, model, api key |
| `Failed to load image or audio file` | an image/PDF block was sent to the model | fixed: the digest is inlined as text and `read_paper` returns extracted text — the pipeline never sends image/PDF blocks |
| `Argument list too long` | huge digest passed as a CLI arg | fixed: the prompt is fed on stdin |
| `ideas_manifest.json` is `[]` after a short run | nothing matured yet (needs 2 refinements + score) | expected — let it run, or `EXPAND_INCLUDE_POOL=1` |
| Review "succeeds" over nothing | empty `library_manifest.json` | run Phase 1; if you used `CRAWL_RUN` namespaces, run `legumista merge <ns1> <ns2>` |
