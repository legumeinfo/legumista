#!/usr/bin/env python3
"""Local, read-only tools for the harness.

Only safe, read-only capabilities are exposed — no shell, file-writing, or
code-execution tools. `read_file` opens a local text file; `web_fetch` GETs a URL and
returns readable text. Both are size-capped and never mutate anything.
"""
import os
import re
import urllib.error
import urllib.request

from .tool import Tool
from .tools_native import BlockedURLError, _open_guarded, _sandbox_path

MAX_CHARS = int(os.environ.get("LEGUMISTA_TOOL_MAX_CHARS", "20000"))
FETCH_TIMEOUT = int(os.environ.get("LEGUMISTA_TOOL_FETCH_TIMEOUT", "30"))
_UA = {"User-Agent": "legumista-agent/1.0 (research; +https://openrouter.ai)"}


def _cap(text: str) -> str:
    return text if len(text) <= MAX_CHARS else text[:MAX_CHARS] + f"\n… [truncated to {MAX_CHARS} chars]"


async def _read(args) -> str:
    raw_path = args.get("path") or args.get("file_path") or ""
    path, err = _sandbox_path(raw_path)   # confine reads to the project workspace
    if err:
        return err
    if not os.path.exists(path):
        return f"error: no such file: {raw_path}"
    if os.path.isdir(path):
        return f"error: {raw_path} is a directory"
    try:
        with open(path, "rb") as f:
            raw = f.read(MAX_CHARS * 4 + 8)
        if b"\x00" in raw[:4096]:            # crude binary sniff (e.g. PDF)
            return f"error: {path} looks binary ({os.path.getsize(path)} bytes) — not readable as text"
        return _cap(raw.decode("utf-8", "replace"))
    except OSError as e:
        return f"error: {e}"


def _strip_html(s: str) -> str:
    s = re.sub(r"(?is)<(script|style)\b.*?</\1>", " ", s)
    s = re.sub(r"(?s)<[^>]+>", " ", s)
    s = re.sub(r"[ \t]+", " ", s)
    s = re.sub(r"\n\s*\n\s*\n+", "\n\n", s)
    return s.strip()


async def _web_fetch(args) -> str:
    url = args.get("url") or ""
    if not re.match(r"^https?://", url):
        return "error: 'url' must be an http(s) URL"
    try:
        req = urllib.request.Request(url, headers=_UA)
        with _open_guarded(req, FETCH_TIMEOUT) as resp:   # SSRF-guarded (blocks private hosts)
            ctype = resp.headers.get_content_type()
            body = resp.read(MAX_CHARS * 8).decode("utf-8", "replace")
    except BlockedURLError as e:
        return f"error: blocked URL: {e}"
    except urllib.error.HTTPError as e:
        return f"error: HTTP {e.code} for {url}"
    except Exception as e:  # noqa: BLE001 - network is best-effort
        return f"error: {type(e).__name__}: {e}"
    text = _strip_html(body) if "html" in ctype else body
    return _cap(text or "(empty response)")


def local_read_tools() -> list:
    """The read-only local tools to add to the agent's tool surface."""
    return [
        Tool(name="read_file", read_only=True, run=_read,
             description="Read a local text file and return its contents. Use for project "
                         "files (a corpus digest, a ledger, a context input) — not for URLs "
                         "(use web_fetch) or scholarly PDFs (use read_paper). Binary files "
                         "are refused; output is size-capped.",
             parameters={"type": "object",
                         "properties": {"path": {"type": "string",
                                                 "description": "Path to a local text file "
                                                                "(absolute or relative)."}},
                         "required": ["path"], "additionalProperties": False}),
        Tool(name="web_fetch", read_only=True, run=_web_fetch,
             description="Fetch an http(s) URL and return its readable text (HTML is "
                         "stripped to text; output is size-capped). Use for arbitrary web "
                         "pages; for a scholarly paper prefer read_paper, which resolves the "
                         "open-access PDF.",
             parameters={"type": "object",
                         "properties": {"url": {"type": "string",
                                                "description": "The http(s) URL to fetch."}},
                         "required": ["url"], "additionalProperties": False}),
    ]
