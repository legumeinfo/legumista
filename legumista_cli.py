#!/usr/bin/env python3
"""legumista — the command-line interface for the research-discovery pipeline.

A single Typer app over the pipeline modules. The headline flows:

  legumista discover   Phase 1 + 2: crawl the citation graph, then synthesize the review
  legumista ideate     Phase 3 (+ 3b): grow research ideas, then expand them to proposals

plus granular pieces (crawl, merge, review, digest, expand, report) and housekeeping
(reset, status, init). Every phase talks to an OpenAI-compatible endpoint configured in
legumista.yml `llm` — there are no external processes to launch. Topic identity and prompt
wording come from legumista.yml + prompts/ via config.py, so this file stays pure
orchestration.
"""
import json
import os
import re
import shutil
import sys
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import List, Optional

import typer

HERE = Path(__file__).resolve().parent
# All importable modules (config, orchestrator, ideation, build_digest, expand_ideas,
# …) sit at the package root; put it on the path so a direct `python legumista_cli.py`
# also resolves them (an installed console-script resolves them as normal modules).
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

app = typer.Typer(
    add_completion=False, no_args_is_help=True,
    help="legumista — autonomous research discovery → synthesis → ideation over any "
         "OpenAI-compatible LLM endpoint.")
reset_app = typer.Typer(no_args_is_help=True, help="Clear a phase's generated artifacts.")
app.add_typer(reset_app, name="reset")


@app.callback()
def _root(
    project: Optional[Path] = typer.Option(
        None, "--project", "-C",
        help="Project directory to act on (default: nearest legumista.yml up from the "
             "cwd, else the cwd)."),
):
    """legumista — each research topic is its own project directory (legumista.yml + data).
    Run inside it, or target one with -C. Scaffold a new one with `legumista init`."""
    if project is not None:
        os.environ["LEGUMISTA_HOME"] = str(project.resolve())


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------
def _out(msg: str) -> None:
    typer.echo(msg)


def _err(msg: str) -> None:
    typer.echo(msg, err=True)


def _setenv(**kv) -> None:
    """Set CRAWL_*/IDEATE_* env vars from CLI options (skip the unset ones), so the
    modules pick them up when imported. Must run BEFORE importing orchestrator/
    ideation, which read their config at import time."""
    for k, v in kv.items():
        if v is not None:
            os.environ[k] = str(v)


def _reachable(url: str, timeout: int = 3) -> bool:
    """True if the host answers at all. An HTTP error status still means the service
    is up (mirrors `curl -s` succeeding on a 404)."""
    try:
        urllib.request.urlopen(url, timeout=timeout)
        return True
    except urllib.error.HTTPError:
        return True
    except Exception:
        return False


def _require_project() -> None:
    """Fail with guidance if the resolved directory isn't a legumista project."""
    import config
    if not os.path.exists(config.PROJECT_FILE):
        _err(f"[!] no legumista project here (no legumista.yml in {config.WORKSPACE}).")
        _err("    Run `legumista init` to scaffold one, cd into a project, or pass `-C <dir>`.")
        raise typer.Exit(1)


# ---------------------------------------------------------------------------
# Phase 2 review: rebuild the digest, inline it, and ask the OpenAI-compatible
# endpoint for the synthesis. The script writes review.md (never the model).
# ---------------------------------------------------------------------------
def _run_review(angle: Optional[str]) -> None:
    import build_digest
    import config
    from orchestrator import MANIFEST_FILE, WORKSPACE, load_json

    manifest = load_json(MANIFEST_FILE, [])
    n = len(manifest)
    if n == 0:
        _err("[!] corpus is empty — run `legumista discover`/`legumista crawl` first "
             "(and `legumista merge` if you crawled with namespaced runs).")
        raise typer.Exit(1)

    build_digest.main()                                   # rebuild the digest
    digest_path = os.path.join(WORKSPACE, "corpus", "corpus_digest.md")
    if not os.path.exists(digest_path):
        _err("[!] digest build produced nothing.")
        raise typer.Exit(1)

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    outdir = os.path.join(WORKSPACE, "reviews", f"{stamp}_final")
    os.makedirs(outdir, exist_ok=True)
    shutil.copy2(digest_path, os.path.join(outdir, "corpus_digest.md"))  # provenance

    with open(digest_path, encoding="utf-8") as f:
        digest_text = f.read()
    prompt = config.render(
        "review.md", N=n, DIGEST=digest_text,
        ANGLE=angle or "Comprehensive state of the field across the whole collected corpus.")

    _out(f"[*] Corpus: {n} papers -> {os.path.relpath(outdir, WORKSPACE)}/review.md "
         "(synthesizing — this can take a few minutes) ...")
    # Review runs through the same shared agentic base as every other phase: the reviewer
    # may read_file/web_fetch/search to verify while it writes. Its behaviour is guided by
    # the review prompt (which asks it to synthesize from the inlined digest), not a
    # special code path.
    from legumista_agent.runtime import PhaseSession
    with PhaseSession("review", log=_out) as session:
        content, meta = session.call(prompt)
    with open(os.path.join(outdir, "run.txt"), "w", encoding="utf-8") as f:
        f.write(f"model={meta.get('model')}\nmeta={meta}\n---RESPONSE---\n{content or ''}")

    review_path = os.path.join(outdir, "review.md")
    if content and content.strip():
        with open(review_path, "w", encoding="utf-8") as f:
            f.write(content.strip() + "\n")
        _out(f"[✓] Review: {os.path.relpath(review_path, WORKSPACE)} "
             f"({content.count(chr(10)) + 1} lines; corpus snapshot alongside it)")
    else:
        _err(f"[!] No review text ({meta.get('error')}: {meta.get('detail', '')}). "
             f"See {os.path.relpath(outdir, WORKSPACE)}/run.txt.")
        raise typer.Exit(1)


# --- research <-> discover seed bridge ---
_DOI_RE = re.compile(r"10\.\d{4,9}/[^\s\"'<>)\]}]+", re.I)


def _dois_from_messages(messages: list) -> list:
    """DOIs seen in tool RESULTS (role:'tool') — grounded, not model-invented."""
    seen, out = set(), []
    for m in messages:
        if m.get("role") != "tool":
            continue
        for raw in _DOI_RE.findall(m.get("content") or ""):
            d = raw.rstrip(".,;:)").lower()
            if d not in seen:
                seen.add(d)
                out.append(d)
    return out


def _write_seed(path, topic, dois, answer, workspace) -> None:
    rel = os.path.relpath(path, workspace)
    lines = [
        f"# Research seed — {topic}", "",
        "_Generated by `legumista research`. Edit the DOI list below (remove anything "
        "off-topic or wrong), then seed the crawl:_", "",
        f"    legumista discover --seed {rel}", "",
        "## Candidate anchor DOIs (extracted from tool results)", "",
    ]
    lines += ([f"- {d}" for d in dois] or ["_(no DOIs found)_"])
    lines += ["", "## Agent answer", "", answer.rstrip(), ""]
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")


def _apply_seed(path) -> None:
    """Parse DOIs from a Markdown seed and merge them into agent_state.json
    (anchor_dois + pending_dois), so `legumista research` output can seed the crawl."""
    import config
    p = Path(path)
    if not p.exists():
        _err(f"[!] seed file not found: {path}")
        raise typer.Exit(1)
    seen, dois = set(), []
    for raw in _DOI_RE.findall(p.read_text(encoding="utf-8")):
        d = raw.rstrip(".,;:)").lower()
        if d not in seen:
            seen.add(d)
            dois.append(d)
    if not dois:
        _err(f"[!] no DOIs found in {path}")
        raise typer.Exit(1)
    state_path = Path(config.WORKSPACE) / "agent_state.json"
    state = (json.loads(state_path.read_text(encoding="utf-8")) if state_path.exists()
             else {"current_loop": 0, "max_loops": 100, "anchor_dois": [],
                   "crawled_dois": [], "pending_dois": [], "depth": {}})
    anchors, pending = state.setdefault("anchor_dois", []), state.setdefault("pending_dois", [])
    crawled = state.get("crawled_dois", [])
    na = np = 0
    for d in dois:
        if d not in anchors:
            anchors.append(d); na += 1
        if d not in pending and d not in crawled:
            pending.append(d); np += 1
    state_path.write_text(json.dumps(state, indent=2, ensure_ascii=False), encoding="utf-8")
    _out(f"[*] seeded {len(dois)} DOIs from {os.path.relpath(str(p), config.WORKSPACE)} "
         f"-> agent_state.json (+{na} anchors, +{np} pending)")


def _run_expand(include_pool: bool) -> None:
    if include_pool:
        os.environ["EXPAND_INCLUDE_POOL"] = "1"
    import expand_ideas
    try:
        expand_ideas.main()
    except SystemExit as e:                 # expand_ideas exits 1 when nothing to do
        if e.code:
            _out("[i] Nothing expanded yet (need matured ideas). Run `legumista expand` "
                 "later, or `legumista expand --include-pool` to expand the scored pool.")


def _run_phase(fn) -> None:
    """Run a phase module's main(), translating its sys.exit into a clean Typer exit
    (so a stop condition surfaces as a normal non-zero exit, not a stack unwind)."""
    try:
        fn()
    except SystemExit as e:
        if e.code:
            raise typer.Exit(e.code)


# ---------------------------------------------------------------------------
# Headline commands
# ---------------------------------------------------------------------------
@app.command()
def discover(
    similarity: str = typer.Option("content", "--similarity", "-s",
                                   help="Distance metric: content | structural."),
    max_loops: Optional[int] = typer.Option(None, help="Crawl loop ceiling."),
    max_papers: Optional[int] = typer.Option(None, help="Collected-paper ceiling."),
    min_similarity: Optional[float] = typer.Option(None, help="Anti-drift gate."),
    max_depth: Optional[int] = typer.Option(None, help="Max citation hops from an anchor."),
    year_min: Optional[int] = typer.Option(None, help="Scope start year."),
    year_max: Optional[int] = typer.Option(None, help="Scope end year."),
    angle: Optional[str] = typer.Option(None, help="Framing/angle for the synthesis."),
    seed: Optional[Path] = typer.Option(None, "--seed",
        help="Markdown seed (e.g. from `legumista research`): merge its DOIs into "
             "agent_state.json anchors/frontier before crawling."),
    review: bool = typer.Option(True, "--review/--no-review",
                                help="Run Phase 2 synthesis after the crawl."),
):
    """Phase 1 + 2: crawl the citation graph, then synthesize the review."""
    _require_project()
    if seed is not None:
        _apply_seed(seed)
    _setenv(CRAWL_SIMILARITY=similarity, CRAWL_MAX_LOOPS=max_loops,
            CRAWL_MAX_PAPERS=max_papers, CRAWL_MIN_SIMILARITY=min_similarity,
            CRAWL_MAX_DEPTH=max_depth, CRAWL_YEAR_MIN=year_min, CRAWL_YEAR_MAX=year_max)
    import orchestrator
    _run_phase(orchestrator.main)
    if review:
        _run_review(angle)


@app.command()
def ideate(
    max_loops: Optional[int] = typer.Option(None, help="Ideation loop ceiling."),
    max_ideas: Optional[int] = typer.Option(None, help="Matured-idea target (then stop)."),
    promote_score: Optional[int] = typer.Option(None, help="Score (/20) to mature an idea."),
    min_refinements: Optional[int] = typer.Option(None, help="Develops before maturing."),
    seed_min: Optional[int] = typer.Option(None, help="Keep >= this many pending ideas."),
    seeds_per_gen: Optional[int] = typer.Option(None, help="New ideas per generate loop."),
    expand: bool = typer.Option(True, "--expand/--no-expand",
                                help="Expand matured ideas into full proposals (Phase 3b)."),
    include_pool: bool = typer.Option(False, "--include-pool",
                                      help="Also expand the scored pool, not just matured."),
):
    """Phase 3 (+ 3b): grow research ideas from the corpus + synthesis, then expand them."""
    _require_project()
    _setenv(IDEATE_MAX_LOOPS=max_loops, IDEATE_MAX_IDEAS=max_ideas,
            IDEATE_PROMOTE_SCORE=promote_score, IDEATE_MIN_REFINEMENTS=min_refinements,
            IDEATE_SEED_MIN=seed_min, IDEATE_SEEDS_PER_GEN=seeds_per_gen)
    import ideation
    _run_phase(ideation.main)
    import build_ideation_report
    build_ideation_report.main()
    if expand:
        _run_expand(include_pool)


# ---------------------------------------------------------------------------
# Granular pieces
# ---------------------------------------------------------------------------
@app.command()
def crawl(
    similarity: str = typer.Option("content", "--similarity", "-s", help="content | structural."),
    run: Optional[str] = typer.Option(None, "--run",
                                      help="Namespace for an independent crawl, e.g. structural."),
    max_loops: Optional[int] = typer.Option(None),
    max_papers: Optional[int] = typer.Option(None),
    min_similarity: Optional[float] = typer.Option(None),
    max_depth: Optional[int] = typer.Option(None),
):
    """Phase 1 only: run the citation-graph crawl."""
    _require_project()
    _setenv(CRAWL_SIMILARITY=similarity, CRAWL_RUN=run, CRAWL_MAX_LOOPS=max_loops,
            CRAWL_MAX_PAPERS=max_papers, CRAWL_MIN_SIMILARITY=min_similarity,
            CRAWL_MAX_DEPTH=max_depth)
    import orchestrator
    _run_phase(orchestrator.main)


@app.command()
def merge(runs: List[str] = typer.Argument(None,
          help="Runs to union, e.g. content structural (default: both).")):
    """Union namespaced crawl manifests into the canonical library_manifest.json."""
    _require_project()
    import merge_corpora
    merge_corpora.main(list(runs) if runs else None)


@app.command()
def review(
    angle: Optional[str] = typer.Option(None, help="Framing/angle for the synthesis."),
):
    """Phase 2 only: rebuild the digest and synthesize the review."""
    _require_project()
    _run_review(angle)


@app.command()
def digest():
    """Rebuild corpus/corpus_digest.md from the manifest (deterministic, no model)."""
    _require_project()
    import build_digest
    build_digest.main()


@app.command()
def expand(
    include_pool: bool = typer.Option(False, "--include-pool",
                                      help="Also expand the scored pool, not just matured."),
):
    """Phase 3b only: expand ideas into full, citation-grounded proposals."""
    _require_project()
    _run_expand(include_pool)


@app.command()
def report():
    """Rebuild ideas/ideation_report.md from the matured-idea manifest (no model)."""
    _require_project()
    import build_ideation_report
    build_ideation_report.main()


# ---------------------------------------------------------------------------
# Agentic (tool-using) research
# ---------------------------------------------------------------------------
@app.command()
def research(
    topic: str = typer.Argument(..., help="Research question / topic for the agent."),
    max_turns: Optional[int] = typer.Option(None, help="Max tool-use turns."),
    allow_write: bool = typer.Option(
        False, "--allow-write",
        help="Grant write access (read_write permission): the genomics tools may run "
             "writing operations (samtools sort/index, bcftools call, tabix_index). Off "
             "by default — reads only."),
):
    """Agentic literature research: the model uses native paper-search tools
    (OpenAlex/Crossref/arXiv/Europe PMC + web/grep/read + samtools/bcftools genomics) —
    plus any MCP servers in .mcp.json — in a tool-call loop, then saves the answer + a DOI
    seed + transcript."""
    _require_project()
    import config
    from legumista_agent.agent import research as run_research
    from orchestrator import WORKSPACE

    servers = config.mcp_servers()   # optional; native tools are always available

    def on_event(ev):
        t = ev.get("type")
        if t == "tools_ready":
            _out(f"[*] {len(ev['tools'])} tools: {', '.join(ev['tools'][:8])}"
                 + (" …" if len(ev["tools"]) > 8 else ""))
        elif t == "assistant" and ev.get("tool_calls"):
            _out(f"    → turn {ev['turn']}: calling {', '.join(ev['tool_calls'])}")
        elif t == "tool_result":
            _out(f"    ← {ev['tool']} ({ev['chars']} chars)")

    _out(f"[*] researching: {topic}")
    # Same system prompt as every other phase: the shared base + native-tools spec.
    system = config.system_prompt() or None
    if allow_write:
        _out("[*] write access enabled (read_write): genomics tools may write to the workspace")
    try:
        result = run_research(topic, servers=servers,
                              system=system,
                              max_turns=max_turns or config.agent_max_turns(),
                              on_event=on_event, allow_write=allow_write)
    except Exception as e:  # noqa: BLE001 - surface endpoint / MCP-spawn failures clearly
        _err(f"[!] agent run failed: {type(e).__name__}: {e}")
        _err("    (check the model endpoint in legumista.yml `llm`, and any MCP servers in .mcp.json)")
        raise typer.Exit(1)

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    outdir = os.path.join(WORKSPACE, "reviews", f"{stamp}_research")
    os.makedirs(outdir, exist_ok=True)
    with open(os.path.join(outdir, "transcript.json"), "w", encoding="utf-8") as f:
        json.dump(result.get("messages", []), f, indent=2, ensure_ascii=False)

    if not result.get("ok"):
        _err(f"[!] research did not finish: {result.get('error')}. "
             f"Transcript: {os.path.relpath(outdir, WORKSPACE)}/transcript.json")
        raise typer.Exit(1)

    answer = result["content"]
    with open(os.path.join(outdir, "answer.md"), "w", encoding="utf-8") as f:
        f.write(answer.rstrip() + "\n")

    # Emit an editable Markdown seed: DOIs extracted from the tool RESULTS (grounded,
    # not model-invented) so it can feed the crawl via `legumista discover --seed`.
    dois = _dois_from_messages(result.get("messages", []))
    seed_path = os.path.join(outdir, "seed.md")
    _write_seed(seed_path, topic, dois, answer, WORKSPACE)

    _out(f"[✓] {os.path.relpath(outdir, WORKSPACE)}/answer.md ({result['turns']} turns); "
         f"{len(dois)} DOIs -> {os.path.relpath(seed_path, WORKSPACE)}")
    _out(f"    seed the crawl:  legumista discover --seed {os.path.relpath(seed_path, WORKSPACE)}")
    _out("")
    _out(answer)


# ---------------------------------------------------------------------------
# MCP server — expose the native research toolset over the Model Context Protocol
# ---------------------------------------------------------------------------
@app.command()
def mcp(
    transport: str = typer.Option("stdio", "--transport", "-t",
        help="Transport: 'stdio' (default; how MCP clients spawn a server) or 'http'."),
    host: str = typer.Option("127.0.0.1", help="Bind host (http transport only)."),
    port: int = typer.Option(8000, help="Bind port (http transport only)."),
    allow_write: bool = typer.Option(
        False, "--allow-write",
        help="Expose the genomics tools' write operations (samtools sort/index, bcftools "
             "call, tabix_index). Off by default — read operations only."),
):
    """Start a spec-compliant FastMCP server exposing legumista's native tools — scholarly
    search (OpenAlex/Crossref/arXiv/Europe PMC/bioRxiv), read_paper, NCBI datasets+EDirect,
    web search, workspace-sandboxed grep/read, and the pysam genomics suite
    (samtools/bcftools/fasta_fetch/tabix) — so any MCP client can drive them.

    Tools are sandboxed to the active project (paths resolve inside it), so run inside a
    project or target one with -C. Needs the 'serve' extra (pip install 'legumista[serve]');
    the genomics tools also need 'bio' (pysam)."""
    if transport not in ("stdio", "http"):
        _err(f"[!] unknown transport {transport!r} — use 'stdio' or 'http'.")
        raise typer.Exit(1)
    from legumista_agent.mcp_server import serve
    # stdio speaks the protocol on stdout, so status must go to stderr to avoid corrupting
    # the JSON-RPC stream; http is a plain server, so a friendly stdout line is fine.
    log = _err if transport == "stdio" else _out
    log(f"[*] legumista MCP server (transport={transport}"
        + (f", http://{host}:{port}/mcp" if transport == "http" else "")
        + (", writes ENABLED" if allow_write else "") + ") …")
    try:
        serve(transport=transport, host=host, port=port, allow_write=allow_write)
    except KeyboardInterrupt:  # graceful Ctrl-C
        _err("[*] MCP server stopped.")


# ---------------------------------------------------------------------------
# Housekeeping
# ---------------------------------------------------------------------------
@reset_app.command("crawl")
def reset_crawl(
    hard: bool = typer.Option(False, "--hard",
                              help="Also wipe the collected library, anchor caches, and PDFs."),
):
    """Reset the crawl frontier back to the anchor set (Phase 1)."""
    _require_project()
    import config
    proj = Path(config.WORKSPACE)
    state_path = proj / "agent_state.json"
    if not state_path.exists():
        _err("[!] agent_state.json not found."); raise typer.Exit(1)
    st = json.loads(state_path.read_text(encoding="utf-8"))
    anchors = st.get("anchor_dois") or []
    st.update(current_loop=0, crawled_dois=[], depth={}, pending_dois=list(anchors))
    state_path.write_text(json.dumps(st, indent=2, ensure_ascii=False), encoding="utf-8")
    _out(f"[reset] frontier restored to {len(anchors)} anchors; current_loop=0")
    if hard:
        (proj / "library_manifest.json").write_text("[]\n")
        removed = 0
        for p in proj.glob("anchor_profile*.json"):
            p.unlink(); removed += 1
        papers = proj / "papers"
        n = 0
        if papers.is_dir():
            for pdf in papers.glob("*.pdf"):
                pdf.unlink(); n += 1
        _out(f"[reset --hard] cleared manifest, {removed} anchor cache(s), {n} PDFs")


@reset_app.command("ideation")
def reset_ideation(
    dry_run: bool = typer.Option(False, "--dry-run", help="List what would go; delete nothing."),
):
    """Clear generated ideation artifacts (Phase 3). Keeps inputs and Phase 1/2 output."""
    _require_project()
    import config
    proj = Path(config.WORKSPACE)
    files = ["ideation_state.json", "ideation_state.json.bak",
             "ideas/ideas_manifest.json", "ideas/ideas_manifest.json.bak",
             "ideas/ideation_report.md", "ideas/ideas_full_report.md"]
    dirs = ["ideas/reports", "reviews/ideation_logs"]
    removed = 0
    for rel in files:
        p = proj / rel
        if p.exists():
            _out(("would remove " if dry_run else "removed   ") + rel)
            if not dry_run:
                p.unlink()
            removed += 1
    for rel in dirs:
        p = proj / rel
        if p.is_dir():
            n = sum(len(fs) for _, _, fs in os.walk(p))
            _out(("would remove " if dry_run else "removed   ") + f"{rel}/ ({n} files)")
            if not dry_run:
                shutil.rmtree(p)
            removed += 1
    if not removed:
        _out("[reset-ideation] nothing to clear — already clean.")
    else:
        _out(f"[reset-ideation] {'(dry run) ' if dry_run else ''}{removed} artifact(s) "
             f"{'to clear' if dry_run else 'cleared'}. Inputs and Phase 1/2 kept.")


# Bare-essentials scaffold written by `legumista init`. Everything else is optional and
# inherited or auto-created: prompt wording falls back to the packaged defaults (drop a
# prompts/<name>.md here only to override one), output dirs (papers/, reviews/, corpus/,
# ideas/) are created by the pipeline at run time, and the optional steering files
# (context_inputs/*.md, ideation-goal.md) are added by the user when wanted.
_SKEL_AGENT_STATE = """{
  "_comment": "anchor_dois = your VERIFIED core papers (the topical centre; never invent DOIs). pending_dois = the live crawl frontier — seed it to the same set.",
  "current_loop": 0,
  "max_loops": 100,
  "anchor_dois": [],
  "crawled_dois": [],
  "pending_dois": [],
  "depth": {}
}
"""
def _write_if_absent(path: Path, content: str) -> None:
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")


@app.command()
def init(
    directory: Path = typer.Argument(Path("."),
              help="Where to create the project (default: current directory)."),
    force: bool = typer.Option(False, "--force", help="Overwrite an existing legumista.yml."),
):
    """Scaffold a new legumista project — just the bare config: legumista.yml and an empty
    agent_state.json. Prompts fall back to the packaged defaults, output dirs are created
    on the first run, and steering (context_inputs/, ideation-goal.md) is optional — add
    it when you want it (see PIPELINE.md)."""
    import config
    target = directory.resolve()
    target.mkdir(parents=True, exist_ok=True)
    proj_file = target / "legumista.yml"
    if proj_file.exists() and not force:
        _err(f"[!] {proj_file} already exists (use --force to overwrite).")
        raise typer.Exit(1)

    # legumista.yml from the annotated example + an empty agent_state.json (never clobbered).
    shutil.copyfile(Path(config.PKG_ASSETS) / "legumista.example.yml", proj_file)
    _write_if_absent(target / "agent_state.json", _SKEL_AGENT_STATE)

    _out(f"[✓] Initialised legumista project in {target}")
    _out("    Next: 1) edit legumista.yml (identity + llm endpoint)")
    _out("          2) add verified anchor DOIs to agent_state.json")
    _out("          3) `legumista discover`   (optional: add context_inputs/*.md steering,")
    _out("             ideation-goal.md for Phase 3, or prompts/<name>.md to override a prompt)")


@app.command()
def status():
    """Show the active project, corpus/idea counts, and the LLM endpoint."""
    import config
    proj = Path(config.WORKSPACE)
    corpus = json.loads((proj / "library_manifest.json").read_text()) \
        if (proj / "library_manifest.json").exists() else []
    ideas_path = proj / "ideas" / "ideas_manifest.json"
    ideas = json.loads(ideas_path.read_text()) if ideas_path.exists() else []
    ymin, ymax = config.years()
    has_proj = os.path.exists(config.PROJECT_FILE)
    c = config.llm()
    if config.llm_is_local():
        keystate = "n/a (local)"
    else:
        keystate = "set" if config.llm_api_key() else f"MISSING (${c['api_key_env']})"
    _out(f"project:   {config.name()}  —  {config.subject()}")
    _out(f"dir:       {proj}" + ("" if has_proj else "   (no legumista.yml — `legumista init`?)"))
    _out(f"config:    {'legumista.yml' if has_proj else 'defaults in config.py'}"
         f"   scope years {ymin}-{ymax}")
    _out(f"corpus:    {len(corpus)} papers")
    _out(f"ideas:     {len(ideas)} matured")
    _out(f"endpoint:  {c['base_url']}  ({'reachable' if _reachable(c['base_url']) else 'DOWN'})")
    _out(f"model:     {c['model']}")
    _out(f"api key:   {keystate}")


def main() -> None:
    app()


if __name__ == "__main__":
    main()
