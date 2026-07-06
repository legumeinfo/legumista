#!/usr/bin/env python3
"""
Project configuration — the one place that makes this pipeline topic-specific.

Everything about *what field* we are researching lives in `legumista.yml` (identity,
scope, lexical signal terms, years, contact email, domain constraint) and in the
editable prompt templates under `prompts/`. The Python code reads through this
module so re-pointing the whole pipeline at a new subject is a config edit, not a
code edit. See PIPELINE.md §"Adapting the pipeline to a new research topic".

Safe by construction:
  - The project file is YAML, parsed with PyYAML's `safe_load` (never `load`).
  - If `legumista.yml` is missing (or a key is absent) we fall back to the built-in
    _DEFAULTS below — a neutral, topic-free skeleton — so an un-configured checkout
    still imports and runs.
"""
import json
import os

try:
    import yaml
except ModuleNotFoundError:                      # PyYAML not installed
    yaml = None

# Where legumista's own code lives (the installed package root).
PKG_ROOT = os.path.dirname(os.path.abspath(__file__))


def _pkg_assets() -> str:
    """Directory of bundled defaults (prompt templates + legumista.example.yml),
    shipped in the `legumista_assets` package so they survive a non-editable
    `pip install`. Falls back to a sibling dir if the package isn't importable."""
    try:
        from importlib import resources
        return str(resources.files("legumista_assets"))
    except Exception:
        return os.path.join(PKG_ROOT, "legumista_assets")


PKG_ASSETS = _pkg_assets()


def _find_project_root() -> str:
    """The active PROJECT directory — holds legumista.yml + all data/ledgers.
    Precedence: $LEGUMISTA_HOME, then the nearest ancestor of the cwd that contains a
    legumista.yml (git-style), else the cwd. Kept separate from PKG_ROOT so one
    installed `legumista` can drive many project directories (see `legumista init`)."""
    env = os.environ.get("LEGUMISTA_HOME")
    if env:
        return os.path.abspath(env)
    d = os.getcwd()
    while True:
        if os.path.exists(os.path.join(d, "legumista.yml")):
            return d
        parent = os.path.dirname(d)
        if parent == d:
            return os.getcwd()
        d = parent


WORKSPACE = _find_project_root()
PROJECT_FILE = os.environ.get("LEGUMISTA_PROJECT_FILE", os.path.join(WORKSPACE, "legumista.yml"))

# Fallbacks are a NEUTRAL, topic-free skeleton — never a specific field. They exist so
# the pipeline still imports and runs with a partial/absent legumista.yml; a real project
# supplies its identity in legumista.yml (see legumista.example.yml, or `legumista init`).
# (Keeping domain values here would silently bias a new topic — every unset key would
# inherit them per-leaf — so the defaults are deliberately empty/generic.)
_DEFAULTS = {
    "name": "unset",
    "slug": "project",
    "subject": "the research subject",
    "scope": "Set `scope` in legumista.yml to define what the crawl must stay inside.",
    "years": [1900, 2100],                 # wide-open until a project narrows it
    "contact_email": "you@example.org",    # OpenAlex/Unpaywall polite-pool mailto — set yours
    "lexical": {
        "strong": [],                      # per-project lexical pre-ranking terms (see legumista.yml)
        "weak": [],
    },
    "ideation": {
        "domain_constraint": "concrete, well-scoped, and grounded in the corpus",
    },
    # OpenAI-compatible Chat Completions endpoint. Default: OpenRouter with a free
    # ~3B model so the pipeline runs out of the box for a cheap demo. Point this at
    # local ollama (http://localhost:11434/v1) for real runs — see PIPELINE.md.
    "llm": {
        "base_url": "https://openrouter.ai/api/v1",
        "model": "meta-llama/llama-3.2-3b-instruct:free",
        "small_model": "",                 # optional; falls back to `model`
        "api_key_env": "OPENROUTER_API_KEY",
        "temperature": 0.3,
        "max_tokens": 0,                   # 0 => omit (let the provider decide)
        "timeout": 3600,                   # generous: large-corpus synthesis on a big model
        "json_mode": False,                # send response_format json_object on JSON calls
        "headers": {},                     # extra HTTP headers (e.g. OpenRouter ranking)
    },
}


def _load():
    if yaml and os.path.exists(PROJECT_FILE):
        with open(PROJECT_FILE, encoding="utf-8") as f:
            return yaml.safe_load(f) or {}
    return {}


_CFG = _load()


def _get(dotted, default=None):
    """Fetch a dotted key from legumista.yml, else _DEFAULTS, else `default`."""
    for source in (_CFG, _DEFAULTS):
        cur, ok = source, True
        for part in dotted.split("."):
            if isinstance(cur, dict) and part in cur:
                cur = cur[part]
            else:
                ok = False
                break
        if ok:
            return cur
    return default


# --- typed accessors -------------------------------------------------------
def name() -> str: return _get("name")
def slug() -> str: return _get("slug")
def subject() -> str: return _get("subject")
def scope() -> str: return _get("scope")
def contact_email() -> str: return _get("contact_email")
def lexical_strong() -> list: return list(_get("lexical.strong") or [])
def lexical_weak() -> list: return list(_get("lexical.weak") or [])
def ideation_domain_constraint() -> str: return _get("ideation.domain_constraint")


def years() -> tuple:
    y = _get("years") or [2006, 2026]
    return int(y[0]), int(y[1])


# --- LLM endpoint (OpenAI-compatible /chat/completions) ---------------------
def _env(name):
    v = os.environ.get(name)
    return v if v not in (None, "") else None


def _as_bool(v, default=False) -> bool:
    if isinstance(v, bool):
        return v
    if v is None:
        return default
    return str(v).strip().lower() in ("1", "true", "yes", "on")


def llm() -> dict:
    """Resolved OpenAI-compatible endpoint config: legumista.yml `llm` with per-run
    LLM_* environment overrides."""
    return {
        "base_url": _env("LLM_BASE_URL") or _get("llm.base_url"),
        "model": _env("LLM_MODEL") or _get("llm.model"),
        "small_model": _env("LLM_SMALL_MODEL") or _get("llm.small_model") or "",
        "api_key_env": _env("LLM_API_KEY_ENV") or _get("llm.api_key_env") or "",
        "temperature": float(_env("LLM_TEMPERATURE") or _get("llm.temperature") or 0.3),
        "max_tokens": int(_env("LLM_MAX_TOKENS") or _get("llm.max_tokens") or 0),
        "timeout": int(_env("LLM_TIMEOUT") or _get("llm.timeout") or 3600),
        "json_mode": _as_bool(_env("LLM_JSON_MODE"), _get("llm.json_mode") or False),
        "headers": dict(_get("llm.headers") or {}),
    }


def llm_api_key() -> str:
    """API key for the endpoint: LLM_API_KEY, else the env var named by api_key_env
    (e.g. OPENROUTER_API_KEY). Empty is fine for a keyless local endpoint."""
    direct = _env("LLM_API_KEY")
    if direct:
        return direct
    name = llm()["api_key_env"]
    return os.environ.get(name, "") if name else ""


def llm_is_local() -> bool:
    """True if the endpoint is a local host (ollama etc.) — no API key required."""
    from urllib.parse import urlparse
    host = (urlparse(llm()["base_url"]).hostname or "").lower()
    return host in ("localhost", "127.0.0.1", "0.0.0.0", "::1")


# --- Agentic harness (legumista_agent) -----------------------------------------
def mcp_servers() -> dict:
    """MCP servers for the agentic harness, from .mcp.json in the project dir
    (`{name: {command, args, env}}`). Empty dict if the file is absent."""
    path = os.environ.get("LEGUMISTA_MCP_CONFIG", os.path.join(WORKSPACE, ".mcp.json"))
    if not os.path.exists(path):
        return {}
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    return data.get("mcpServers") or {}


def system_prompt_base() -> str:
    """The base system-prompt text (scientist persona + evidential-integrity rules +
    tool doctrine), resolved so it works with zero project setup:
      1. $LEGUMISTA_SYSTEM_PROMPT           — an explicit path override
      2. <project>/system-prompt.md         — a per-project override, if the user drops one in
      3. legumista_assets/system-prompt.md  — the built-in default shipped with the package
    Returns the text, or None if no non-empty file is found."""
    for cand in (os.environ.get("LEGUMISTA_SYSTEM_PROMPT"),
                 os.path.join(WORKSPACE, "system-prompt.md"),
                 os.path.join(PKG_ASSETS, "system-prompt.md")):
        if cand and os.path.exists(cand):
            with open(cand, encoding="utf-8") as f:
                txt = f.read().strip()
            if txt:
                return txt
    return None


def system_prompt() -> str:
    """The full system message for a model call: the base prompt (see system_prompt_base)
    plus the native-tools spec. Used identically by every phase and by `research`."""
    return "\n\n".join(p for p in (system_prompt_base(), tools_spec()) if p)


def tools_spec() -> str:
    """The native-tools usage spec appended to the system prompt (prompts/
    tools_native.md), so the model gets the tool-use doctrine alongside the function
    schemas. Empty string if the file is absent."""
    try:
        return render("tools_native.md")
    except FileNotFoundError:
        return ""


def agent_max_turns() -> int:
    """Max tool-loop turns (completions) per model step. Default 8."""
    return int(os.environ.get("LEGUMISTA_AGENT_MAX_TURNS", str(_get("agent.max_turns") or 8)))


# --- prompt templating -----------------------------------------------------
def _project_vars() -> dict:
    ymin, ymax = years()
    return {
        "NAME": name(), "SLUG": slug(), "SUBJECT": subject(), "SCOPE": scope(),
        "YEAR_MIN": ymin, "YEAR_MAX": ymax,
        "DOMAIN_CONSTRAINT": ideation_domain_constraint(),
    }


def _prompt_path(name: str) -> str:
    """Locate a prompt template: an LEGUMISTA_PROMPTS_DIR override, then the active
    project's prompts/, then the bundled defaults in the package. Lets a project
    override only the prompts it cares to edit and inherit the rest."""
    for base in (os.environ.get("LEGUMISTA_PROMPTS_DIR"),
                 os.path.join(WORKSPACE, "prompts"),
                 os.path.join(PKG_ASSETS, "prompts")):
        if base:
            cand = os.path.join(base, name)
            if os.path.exists(cand):
                return cand
    return os.path.join(WORKSPACE, "prompts", name)   # clear error if truly missing


def render(template_name: str, **vars) -> str:
    """Load a prompt template and substitute {{PLACEHOLDER}} slots.

    Project identity vars (NAME, SUBJECT, SCOPE, YEAR_MIN/MAX, DOMAIN_CONSTRAINT,
    SLUG) are injected automatically; caller-supplied vars override them. We use a
    literal `{{VAR}}` -> value replacement (NOT str.format) precisely because the
    prompts contain literal `{ }` JSON-schema braces that must pass through intact.
    """
    path = _prompt_path(template_name)
    with open(path, encoding="utf-8") as f:
        tpl = f.read()
    merged = {**_project_vars(), **vars}
    for k, v in merged.items():
        tpl = tpl.replace("{{" + k + "}}", str(v))
    return tpl
