#!/usr/bin/env python3
"""`web_fetch` — retrieve a URL as readable text.

The one tool here. It GETs an http(s) URL through the shared SSRF guard, strips HTML to
text, and caps the result. Read-only and non-mutating, like everything the server serves.
"""
import os
import re
import urllib.error
import urllib.request

from .tool import Tool
from .tools_native import BlockedURLError, _open_guarded

MAX_CHARS = int(os.environ.get("LEGUMISTA_TOOL_MAX_CHARS", "20000"))
FETCH_TIMEOUT = int(os.environ.get("LEGUMISTA_TOOL_FETCH_TIMEOUT", "30"))
_UA = {"User-Agent": "legumista-agent/1.0 (research; +https://openrouter.ai)"}


def _cap(text: str) -> str:
    return text if len(text) <= MAX_CHARS else text[:MAX_CHARS] + f"\n… [truncated to {MAX_CHARS} chars]"


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
