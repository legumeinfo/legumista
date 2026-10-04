#!/usr/bin/env python3
"""Native, in-package research tools — so the pipeline needs no MCP servers.

Everything here is implemented in Python against **keyless** public APIs (OpenAlex,
Crossref, Europe PMC) plus local grep and keyless web search (DuckDuckGo via
`ddgs`). Users can still plug in MCP servers (see .mcp.json) — those tools are added
alongside these. Each tool returns a compact text result for the model, and blocking
work is offloaded with `asyncio.to_thread` so it never stalls the agent's event loop.
"""
import asyncio
import io
import ipaddress
import json
import os
import re
import shutil
import socket
import subprocess
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import config

from .results import count_phrase
from .tool import Tool

MAX_CHARS = int(os.environ.get("LEGUMISTA_TOOL_MAX_CHARS", "20000"))
HTTP_TIMEOUT = int(os.environ.get("LEGUMISTA_TOOL_HTTP_TIMEOUT", "30"))
GET_MAX_BYTES = int(os.environ.get("LEGUMISTA_TOOL_GET_MAX_BYTES", str(40_000_000)))


def _cap(text: str) -> str:
    return text if len(text) <= MAX_CHARS else text[:MAX_CHARS] + f"\n… [truncated to {MAX_CHARS} chars]"


# --- SSRF guard: only fetch public http(s) hosts, and re-check every redirect ---
# Model-supplied URLs (web_fetch, read_paper, and the scholarly APIs)
# must never be turned into requests against loopback, link-local (incl. the cloud
# metadata endpoint 169.254.169.254), or private/RFC-1918 addresses. We resolve the
# host up front and reject blocked targets, then follow redirects through a handler
# that re-validates each hop (a public URL can 302 to http://169.254.169.254/).
class BlockedURLError(Exception):
    """Raised when a URL resolves to a disallowed scheme/host/address."""


# NAT64's well-known prefix carries an IPv4 address in its low 32 bits. Python counts
# 64:ff9b::/96 as global, so `64:ff9b::a9fe:a9fe` — the metadata endpoint, on a NAT64
# network — would pass an is_global test unless it is unwrapped first.
_NAT64 = ipaddress.ip_network("64:ff9b::/96")


def _ip_is_blocked(ip_str: str) -> bool:
    """True unless `ip_str` is a public unicast address.

    An allowlist (`is_global`) rather than a list of private ranges: the ranges are not
    all "private" — 100.64.0.0/10 (shared address space) holds Alibaba Cloud's metadata
    service and Tailscale's addresses, and is_private misses it. The named checks stay
    as a second net. IPv4 addresses embedded in IPv6 (mapped, NAT64, 6to4) are judged
    as the IPv4 address they reach."""
    try:
        ip = ipaddress.ip_address(ip_str)
    except ValueError:
        return True                       # unparseable (or a scoped fe80::1%eth0) -> refuse
    if ip.version == 6:
        if ip.ipv4_mapped is not None:
            ip = ip.ipv4_mapped
        elif ip in _NAT64:
            ip = ipaddress.IPv4Address(int(ip) & 0xFFFFFFFF)
        elif ip.sixtofour is not None:
            ip = ip.sixtofour
    return bool(not ip.is_global or ip.is_loopback or ip.is_link_local or ip.is_private
                or ip.is_reserved or ip.is_multicast or ip.is_unspecified)


def _validate_url(url: str) -> None:
    """Raise BlockedURLError unless `url` is a public http(s) target."""
    parsed = urllib.parse.urlparse(url or "")
    if parsed.scheme not in ("http", "https"):
        raise BlockedURLError(f"refusing non-http(s) URL scheme: {parsed.scheme or '(none)'!r}")
    host = parsed.hostname
    if not host:
        raise BlockedURLError("URL has no host")
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    try:
        infos = socket.getaddrinfo(host, port, proto=socket.IPPROTO_TCP)
    except socket.gaierror as e:
        raise BlockedURLError(f"cannot resolve host {host!r}: {e}")
    for info in infos:
        if _ip_is_blocked(info[4][0]):
            raise BlockedURLError(
                f"refusing to fetch private/loopback/link-local address ({host} -> {info[4][0]})")


class _GuardedRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        _validate_url(newurl)             # re-validate before following the hop
        return super().redirect_request(req, fp, code, msg, headers, newurl)


_GUARDED_OPENER = urllib.request.build_opener(_GuardedRedirectHandler)


def _open_guarded(req: urllib.request.Request, timeout: int):
    """urlopen `req` through the SSRF guard (validates the URL + every redirect)."""
    _validate_url(req.full_url)
    return _GUARDED_OPENER.open(req, timeout=timeout)


# --- workspace sandbox: local file tools may only touch config.WORKSPACE ---------
_SECRET_RE = re.compile(
    r"(?i)(^\.env$|^\.env\.|secret|credential|password|\.pem$|\.key$|"
    r"id_rsa|id_dsa|id_ecdsa|id_ed25519|\.pgpass$|\.htpasswd$|\.netrc$)")


def _is_sensitive(realpath: str) -> bool:
    """True if any path component is a dotfile/dotdir or the basename looks secret."""
    root = os.path.realpath(config.WORKSPACE)
    rel = os.path.relpath(realpath, root)
    # `relpath` yields a bare "." only when realpath IS the workspace root; that
    # current-dir marker is not a hidden component, so don't treat it as one (it's
    # what made a default-path `grep`/`read` of the workspace root refuse itself).
    for comp in rel.split(os.sep):
        if comp and comp not in ("..", ".") and comp.startswith("."):
            return True
    return bool(_SECRET_RE.search(os.path.basename(realpath)))


def _is_server_state(realpath: str) -> bool:
    """True for the server's own state: the pinned catalog and the cache directory (the
    downloaded catalog, its metadata, htslib's scratch space). Either may sit inside the
    workspace if an operator points them there, and a tool that could overwrite them
    could change what every later call is told, so no tool may address them at all."""
    from . import catalog_source, tools_catalog   # lazy: both import this module

    if tools_catalog.CATALOG_PATH and realpath == os.path.realpath(tools_catalog.CATALOG_PATH):
        return True
    cache = os.path.realpath(catalog_source.cache_dir())
    return realpath == cache or realpath.startswith(cache + os.sep)


def _sandbox_path(path: str):
    """Resolve `path` (absolute, or relative to WORKSPACE) inside the project
    workspace. Returns (realpath, None) if allowed, else (None, error_string)."""
    if not path:
        return None, "error: missing 'path'"
    root = os.path.realpath(config.WORKSPACE)
    base = path if os.path.isabs(path) else os.path.join(root, path)
    rp = os.path.realpath(base)
    if rp != root and not rp.startswith(root + os.sep):
        return None, (f"error: refused — '{path}' is outside the project workspace "
                      f"({root}); only files under the workspace may be read")
    if _is_sensitive(rp):
        return None, f"error: refused — '{path}' is a dotfile or secret-like file"
    if _is_server_state(rp):
        return None, f"error: refused — '{path}' is the server's own catalog or cache"
    return rp, None


def _get(url: str, accept: str = "application/json"):
    ua = f"legumista/0.2 (+https://github.com/legumeinfo/legumista; mailto:{config.contact_email()})"
    req = urllib.request.Request(url, headers={"User-Agent": ua, "Accept": accept})
    with _open_guarded(req, HTTP_TIMEOUT) as resp:
        raw = resp.read(GET_MAX_BYTES).decode("utf-8", "replace")
    return json.loads(raw) if "json" in accept else raw


def _get_bytes(url: str, limit: int = 40_000_000) -> bytes:
    """Fetch a URL as raw bytes (for PDFs), size-capped."""
    ua = f"legumista/0.2 (+https://github.com/legumeinfo/legumista; mailto:{config.contact_email()})"
    req = urllib.request.Request(url, headers={"User-Agent": ua, "Accept": "*/*"})
    with _open_guarded(req, HTTP_TIMEOUT) as resp:
        return resp.read(limit)


def _reconstruct_abstract(inv_index) -> str:
    if not inv_index:
        return ""
    pos = {}
    for word, idxs in inv_index.items():
        for i in idxs:
            pos[i] = word
    return " ".join(pos[i] for i in sorted(pos))


def _fmt(papers: list, abstract_chars=400) -> str:
    """Render paper dicts to text. List views truncate abstracts to `abstract_chars`
    (marked with an ellipsis); pass None for the whole abstract (single-work views).

    Recognised keys: title, authors, year, doi, pmid, pmcid, venue, cited_by, source or
    sources, preprint (bool), status (a pubstatus report), abstract."""
    from . import pubstatus  # lazy: pubstatus imports this module's _get

    if not papers:
        return "No results."
    out = []
    for i, p in enumerate(papers, 1):
        authors = [x for x in (p.get("authors") or []) if x]
        a = ", ".join(authors[:4]) + (f", … (+{len(authors) - 4})" if len(authors) > 4 else "")
        flag = pubstatus.describe(p.get("status") or {})
        tags = ""
        if (p.get("status") or {}).get("status") in ("retracted", "concern"):
            tags = flag.split(" (")[0] + " — "
        if p.get("preprint"):
            tags += "PREPRINT (unreviewed) — "
        line = f"[{i}] {tags}{p.get('title') or '(untitled)'} ({p.get('year') or 'n.d.'})"
        meta = [f"{k}:{p[k]}" for k in ("doi", "pmid", "pmcid") if p.get(k)]
        if p.get("venue"):
            meta.append(p["venue"])
        if p.get("cited_by") is not None:
            meta.append(f"cited-by:{p['cited_by']}")
        src = "+".join(p.get("sources") or ([p["source"]] if p.get("source") else []))
        if src:
            meta.append(f"src:{src}")
        abstract = p.get("abstract") or ""
        if abstract and abstract_chars is not None and len(abstract) > abstract_chars:
            abstract = abstract[:abstract_chars].rstrip() + " …"
        out.append(line + ("\n    " + " | ".join(meta) if meta else "")
                   + (f"\n    status: {flag}" if flag else "")
                   + (f"\n    authors: {a}" if a else "")
                   + (f"\n    {abstract}" if abstract else ""))
    return _cap("\n".join(out))


def _id_tail(value) -> str:
    """'https://pubmed.ncbi.nlm.nih.gov/32426053' -> '32426053'; plain ids pass through."""
    return str(value or "").rstrip("/").rsplit("/", 1)[-1]


def _norm_doi(doi):
    if not doi:
        return ""
    return re.sub(r"^https?://(dx\.)?doi\.org/", "", doi.strip().lower())


def _clean_abstract(text: str) -> str:
    """Strip JATS/XML tags, unescape entities, and collapse whitespace — Crossref
    abstracts arrive as messy JATS markup."""
    import html
    if not text:
        return ""
    return " ".join(html.unescape(re.sub(r"<[^>]+>", " ", text)).split())


# --- web search (DuckDuckGo, keyless) ---------------------------------------
def _web_search(args) -> str:
    query = args.get("query") or ""
    if not query:
        return "error: missing 'query'"
    n = int(args.get("max_results") or 8)
    try:
        from ddgs import DDGS
    except ModuleNotFoundError:
        try:
            from duckduckgo_search import DDGS      # older package name
        except ModuleNotFoundError:
            return "error: web search needs the 'ddgs' package (pip install ddgs)"
    try:
        with DDGS() as ddgs:
            results = list(ddgs.text(query, max_results=n))
    except Exception as e:  # noqa: BLE001 - search is best-effort
        return f"error: web search failed: {type(e).__name__}: {e}"
    if not results:
        return "No results."
    out = [f"[{i}] {r.get('title', '')}\n    {r.get('href') or r.get('url', '')}"
           f"\n    {(r.get('body') or '')[:300]}" for i, r in enumerate(results, 1)]
    return _cap("\n".join(out))


# --- OpenAlex (keyless) -----------------------------------------------------
def _openalex_paper(w: dict) -> dict:
    """Map an OpenAlex work onto the common paper dict (ids, status included)."""
    from . import pubstatus
    ids = w.get("ids") or {}
    return {"title": w.get("title"), "doi": _norm_doi(w.get("doi") or ""),
            "pmid": _id_tail(ids.get("pmid")), "pmcid": _id_tail(ids.get("pmcid")),
            "year": w.get("publication_year"), "cited_by": w.get("cited_by_count"),
            "venue": (((w.get("primary_location") or {}).get("source")) or {}).get("display_name"),
            "authors": [(a.get("author") or {}).get("display_name")
                        for a in (w.get("authorships") or [])],
            "abstract": _reconstruct_abstract(w.get("abstract_inverted_index")),
            "preprint": (w.get("type") == "preprint"),
            "status": pubstatus.from_openalex(w), "source": "openalex"}


def _openalex_by_doi(args):
    """The verification path for one work: full abstract, identifiers, work type,
    open-access status, and retraction status from BOTH OpenAlex and Crossref."""
    from . import pubstatus
    from .results import fail
    doi = _norm_doi(args.get("doi") or "")
    if not doi:
        return "error: missing 'doi'"
    try:
        w = _get(f"https://api.openalex.org/works/doi:{urllib.parse.quote(doi)}"
                 f"?mailto={config.contact_email()}")
    except urllib.error.HTTPError as e:
        if e.code == 404:
            crossref = pubstatus.check_doi(doi)
            st = crossref.get("status")
            if st == "not_found":
                where = "doi.org does not know it either — the DOI is wrong or fabricated"
            elif st == "non_crossref":
                where = ("It is registered with another agency (e.g. DataCite, usual for "
                         "datasets and software), so it exists; no metadata or retraction "
                         "status is available here")
            elif st == "unknown":
                where = ("Whether it exists elsewhere could not be checked "
                         f"({crossref.get('error', '?')}); treat it as unverified")
            else:
                where = ("Crossref has it: " + (pubstatus.describe(crossref)
                                                or "registered, no notices")
                         + (f' — "{crossref["title"]}"' if crossref.get("title") else ""))
            return f"OpenAlex has no work with doi:{doi}. {where}."
        return fail(f"OpenAlex lookup failed: HTTP {e.code}")
    except Exception as e:  # noqa: BLE001
        return fail(f"OpenAlex lookup failed: {type(e).__name__}: {e}")
    p = _openalex_paper(w)
    p["doi"] = p["doi"] or doi
    crossref = pubstatus.check_doi(doi)
    p["status"] = pubstatus.merge(p["status"], crossref)
    oa = w.get("open_access") or {}
    refs = w.get("referenced_works") or []
    extra = (f"\n\ntype: {w.get('type') or '?'} | open access: "
             f"{oa.get('oa_status') or ('yes' if oa.get('is_oa') else 'no')} | "
             f"referenced_works: {len(refs)} | cited_by: {w.get('cited_by_count')} | "
             f"retraction check: {p['status'].get('checked') or 'none'}"
             + ("" if crossref.get("status") in pubstatus.CHECKED
                else f" (Crossref: {pubstatus.describe(crossref)})"))
    return _fmt([p], abstract_chars=None) + extra


# --- Europe PMC (keyless; PubMed/PMC/preprints) -----------------------------
def _europepmc_rows(query, n, include_preprints=True, from_year=None, to_year=None):
    """One Europe PMC search, mapped onto the common paper dict. Raises on failure.

    Preprints (SRC:PPR) are excluded unless asked for, matching paper_search's policy,
    and are always flagged when present. PMID/PMCID are carried because many Europe PMC
    records (older papers, Agricola) have no DOI to carry forward."""
    q = f"({query})"
    if not include_preprints:
        q += " AND NOT SRC:PPR"
    if from_year or to_year:
        q += f" AND PUB_YEAR:[{int(from_year or 1800)} TO {int(to_year or 3000)}]"
    url = ("https://www.ebi.ac.uk/europepmc/webservices/rest/search?"
           f"query={urllib.parse.quote(q)}&format=json&pageSize={n}&resultType=core")
    data = _get(url)
    papers = []
    for r in ((data.get("resultList") or {}).get("result") or []):
        papers.append({
            "title": r.get("title"), "doi": _norm_doi(r.get("doi") or ""),
            "pmid": r.get("pmid") or "", "pmcid": r.get("pmcid") or "",
            "year": r.get("pubYear"), "cited_by": r.get("citedByCount"),
            "venue": r.get("journalTitle") or "",
            "authors": [a.strip() for a in (r.get("authorString") or "").split(",")[:8] if a.strip()],
            "abstract": r.get("abstractText") or "",
            "preprint": r.get("source") == "PPR",
            "status": {}, "source": "europepmc"})
    return papers


def _europepmc_search(args):
    """Europe PMC alone — for its own query syntax (e.g. ORGANISM:, SRC:AGR). Everyday
    discovery should use paper_search, which already includes Europe PMC."""
    from .results import fail
    query = args.get("query") or ""
    if not query:
        return "error: missing 'query'"
    n = min(int(args.get("max_results") or 8), 25)
    try:
        papers = _europepmc_rows(query, n, include_preprints=True)
    except Exception as e:  # noqa: BLE001
        return fail(f"Europe PMC request failed: {type(e).__name__}: {e}")
    if not papers:
        return "No results from Europe PMC for this query (the search ran)."
    return (_fmt(papers) + "\n\nRetraction status is not checked here; run verify_ids "
            "(or openalex_by_doi) on any paper before citing it.")


# --- read_paper: fetch an open-access PDF and extract its text ----------------
def _oa_pdf_url(doi: str):
    """Resolve an open-access PDF URL for a DOI: OpenAlex locations first, then Unpaywall
    (both keyless). Returns (url, failures): `failures` names each lookup that could not
    be completed, so "no OA copy is listed" and "could not check" stay distinct."""
    failures = []
    try:
        w = _get(f"https://api.openalex.org/works/doi:{urllib.parse.quote(doi)}"
                 f"?mailto={config.contact_email()}")
        for loc in ([w.get("best_oa_location"), w.get("primary_location")]
                    + (w.get("locations") or [])):
            if loc and loc.get("pdf_url"):
                return loc["pdf_url"], []
    except Exception as e:  # noqa: BLE001
        failures.append(f"OpenAlex ({type(e).__name__}: {e})")
    try:
        up = _get(f"https://api.unpaywall.org/v2/{urllib.parse.quote(doi)}"
                  f"?email={config.contact_email()}")
        loc = up.get("best_oa_location") or {}
        if loc.get("url_for_pdf"):
            return loc["url_for_pdf"], []
    except Exception as e:  # noqa: BLE001
        failures.append(f"Unpaywall ({type(e).__name__}: {e})")
    return "", failures


def _fetch_pdf_text(doi: str, url: str, max_pages, start_page=1):
    """Resolve/download an OA PDF and extract text page by page. Returns
    (pages, source_url, n_pages, first_page, error) where `pages` is a list of
    (page_number, text) — error is None on success."""
    url = (url or "").strip()
    doi = _norm_doi(doi or "")
    if not url and not doi:
        return None, "", 0, 0, "error: provide 'doi' or 'url'"
    if not url:
        url, failures = _oa_pdf_url(doi)
        if not url and len(failures) == 2:
            return None, "", 0, 0, ("error: could not check for an open-access PDF of "
                                    f"doi:{doi} — {'; '.join(failures)}. This says nothing "
                                    "about whether one exists; retry later.")
        if not url:
            partial = (f" (the {failures[0]} lookup failed, so a copy may still exist)"
                       if failures else "")
            return None, "", 0, 0, (f"No open-access PDF is listed for doi:{doi}{partial}. "
                                    "Read the abstract via openalex_by_doi, or try a "
                                    "different DOI.")
    try:
        from pypdf import PdfReader
    except ModuleNotFoundError:
        return None, url, 0, 0, "error: needs the 'pypdf' package (pip install pypdf)"
    try:
        data = _get_bytes(url)
    except Exception as e:  # noqa: BLE001
        return None, url, 0, 0, f"error: PDF download failed: {type(e).__name__}: {e}"
    if not data[:5].startswith(b"%PDF"):
        return None, url, 0, 0, (f"error: {url} did not return a PDF (got "
                                 f"{data[:60].decode('latin-1', 'replace')!r}…). "
                                 "It may be a landing page.")
    try:
        reader = PdfReader(io.BytesIO(data))
    except Exception as e:  # noqa: BLE001
        return None, url, 0, 0, f"error: could not parse PDF: {type(e).__name__}: {e}"
    npages = len(reader.pages)
    first = max(1, min(int(start_page or 1), npages or 1))
    last = npages if max_pages is None else min(npages, first - 1 + int(max_pages))
    pages = [(i + 1, reader.pages[i].extract_text() or "") for i in range(first - 1, last)]
    return pages, url, npages, first, None


_MAX_HITS = 100


def _read_paper(args):
    """Download an open-access PDF (by DOI or direct url) and extract its text; with a
    `pattern`, return only the lines matching it. One fetch, two output shapes — pulling
    a single figure (an N50, a '2n=', an accession) out of a paper is the same call as
    reading it, so the model never has to pick between two tools over one download.

    Every page is labelled and every hit carries its page number, so a claim can be cited
    to a page; `start_page` reaches text beyond the 20,000-character cap."""
    pattern = args.get("pattern") or ""
    rx = None
    if pattern:
        try:
            rx = re.compile(pattern, re.IGNORECASE if args.get("ignore_case", True) else 0)
        except re.error as e:
            return f"error: bad regex: {e}"
    raw_context = args.get("context")
    context = max(0, min(int(raw_context) if raw_context is not None else (1 if rx else 0), 5))
    # A grep wants every page searched; a plain read wants a bounded dump it can afford
    # to put in context — hence the different max_pages defaults.
    pages, url, npages, first, err = _fetch_pdf_text(
        args.get("doi"), args.get("url"), args.get("max_pages") or (None if rx else 30),
        start_page=args.get("start_page") or 1)
    if err:
        return err
    if not any(text.strip() for _n, text in pages):
        return f"(no extractable text from {url} — likely a scanned/image PDF)"
    if rx is None:
        parts, used, stopped_at = [], 0, None
        for number, text in pages:
            block = f"--- page {number} of {npages} ---\n" + re.sub(r"\n{3,}", "\n\n", text).strip()
            if used + len(block) > MAX_CHARS:
                stopped_at = number
                break
            parts.append(block)
            used += len(block) + 1
        if stopped_at == first:
            # A single page longer than the cap: hard-cut it and point at the next page.
            more = (f"\n[continue with start_page={first + 1}]" if first < npages else "")
            return (f"[page {first} of {npages}, truncated] source: {url}\n\n"
                    + _cap(pages[0][1]) + more)
        last_shown = stopped_at - 1 if stopped_at else pages[-1][0]
        head = f"[pages {first}–{last_shown} of {npages}] source: {url}\n\n"
        tail = (f"\n\n[stopped before page {stopped_at} to stay under {MAX_CHARS:,} "
                f"characters — continue with start_page={stopped_at}]" if stopped_at else "")
        return head + "\n".join(parts) + tail
    hits, total = [], 0
    for number, text in pages:
        lines = [" ".join(line.split()) for line in text.splitlines()]
        for i, line in enumerate(lines):
            if not (line and rx.search(line)):
                continue
            total += 1
            if len(hits) < _MAX_HITS:
                window = [x for x in lines[max(0, i - context): i + context + 1] if x]
                hits.append(f"p.{number}: " + " ⏎ ".join(x[:200] for x in window))
    if not hits:
        return (f"No matches for /{pattern}/ in {url} (searched pages {first}–"
                f"{pages[-1][0]} of {npages}).")
    shown = count_phrase(len(hits), total, "match(es)")
    return _cap(f"{shown} for /{pattern}/ in {url} (pages {first}–{pages[-1][0]} of "
                f"{npages}; ⏎ joins adjacent lines):\n" + "\n".join(hits))


# --- NCBI Datasets CLI (genomes/taxonomy/genes) ------------------------------
def _run_cli(argv: list, stdin: bytes = None, timeout: int = 90) -> str:
    """Run a local CLI (no shell — argv list) and return capped stdout, or an error."""
    try:
        p = subprocess.run(argv, input=stdin, capture_output=True,
                           timeout=timeout, check=False)
    except FileNotFoundError:
        return f"error: '{argv[0]}' is not installed on this machine."
    except subprocess.TimeoutExpired:
        return f"error: '{argv[0]}' timed out after {timeout}s."
    out = p.stdout.decode("utf-8", "replace").strip()
    err = p.stderr.decode("utf-8", "replace").strip()
    if p.returncode != 0 and not out:
        return f"[exit {p.returncode}] {err or '(no output)'}"
    return _cap(out + (f"\n[stderr] {err}" if err else "")) or "(empty output)"


def _cli_raw(argv: list, stdin: bytes = None, timeout: int = 120):
    """Run a local CLI; return (stdout_text, error_message_or_None) — raw stdout for
    callers that need to parse it (unlike _run_cli, which caps and formats)."""
    if shutil.which(argv[0]) is None:
        return None, f"__missing__{argv[0]}"
    try:
        p = subprocess.run(argv, input=stdin, capture_output=True,
                           timeout=timeout, check=False)
    except subprocess.TimeoutExpired:
        return None, f"error: '{argv[0]}' timed out after {timeout}s."
    out = p.stdout.decode("utf-8", "replace")
    err = p.stderr.decode("utf-8", "replace").strip()
    if p.returncode != 0 and not out.strip():
        return None, f"[{argv[0]} exit {p.returncode}] {err or '(no output)'}"
    return out, None


# `datasets` subcommands that only READ from NCBI. `download`/`rehydrate` write files
# to disk, so they are blocked to keep this tool genuinely read-only.
_DATASETS_READONLY = {"summary", "taxonomy", "version"}


def _ncbi_datasets(args) -> str:
    """Thin wrapper over the NCBI `datasets` CLI (read-only subcommands only). Pass the
    subcommand + flags as an argv list, e.g. ["summary","genome","taxon","Homo sapiens"]."""
    argv = args.get("args")
    if not isinstance(argv, list) or not argv:
        return ('error: pass "args" as a list, e.g. '
                '["summary","genome","taxon","Homo sapiens","--as-json-lines"]')
    sub = str(argv[0]).strip().lower()
    if sub not in _DATASETS_READONLY:
        return (f"error: subcommand '{sub}' is not permitted (this tool is read-only). "
                f"Allowed: {', '.join(sorted(_DATASETS_READONLY))}. "
                "Writing/downloading (e.g. 'download', 'rehydrate') is disabled.")
    if shutil.which("datasets") is None:
        return ("error: the NCBI 'datasets' CLI is not installed. See "
                "https://www.ncbi.nlm.nih.gov/datasets/docs/v2/command-line-tools/")
    return _run_cli(["datasets", *[str(a) for a in argv]])


def _first_bioproject(ai: dict) -> str:
    if ai.get("bioproject_accession"):
        return ai["bioproject_accession"]
    for lin in (ai.get("bioproject_lineage") or []):
        for bp in (lin.get("bioprojects") or []):
            if bp.get("accession"):
                return bp["accession"]
    return ""


_ASSEMBLY_ROWS = 50
_LEVEL_RANK = {"Complete Genome": 0, "Chromosome": 1, "Scaffold": 2, "Contig": 3}



def _ncbi_assembly_status(args) -> str:
    """Answer 'what reference genomes/assemblies exist for this taxon?' — wraps
    `datasets summary genome taxon <taxon>` and returns a compact table (accession,
    level, contig N50, date, organism)."""
    taxon = (args.get("taxon") or "").strip()
    if not taxon:
        return "error: missing 'taxon' (e.g. 'Homo sapiens' or a taxid)"
    out, err = _cli_raw(["datasets", "summary", "genome", "taxon", taxon,
                         "--as-json-lines"])
    if err:
        if err.startswith("__missing__"):
            return ("error: the NCBI 'datasets' CLI is not installed. See "
                    "https://www.ncbi.nlm.nih.gov/datasets/docs/v2/command-line-tools/")
        low = err.lower()
        if "no genome data" in low or "no assemblies" in low or "no records" in low:
            return (f"No genome assemblies are currently available in NCBI for "
                    f"taxon '{taxon}' (the name resolves, but no genome data exists).")
        return err
    rows = []
    for line in out.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            r = json.loads(line)
        except json.JSONDecodeError:
            continue
        ai = r.get("assembly_info") or {}
        st = r.get("assembly_stats") or {}
        org = r.get("organism") or {}
        rows.append((r.get("accession") or "?",
                     ai.get("assembly_level") or "?",
                     str(st.get("contig_n50") or "?"),
                     ai.get("release_date") or "?",
                     _first_bioproject(ai) or "-",
                     org.get("organism_name") or "?"))
    if not rows:
        return f"No assemblies found in NCBI for taxon '{taxon}'."
    # Count everything the CLI returned, then show the most complete assemblies first
    # (level, then newest), so the displayed slice is the informative one and the header
    # never presents the display cap as the total.
    rows.sort(key=lambda r: (_LEVEL_RANK.get(r[1], 9), -int(re.sub(r"\D", "", r[3]) or 0)))
    total, rows = len(rows), rows[:_ASSEMBLY_ROWS]
    hdr = (f"{count_phrase(len(rows), total, 'assembly(ies)')} for '{taxon}', most complete "
           "first (accession | level | contig N50 | date | BioProject | organism):\n")
    body = "\n".join(f"  {acc}  [{lvl}]  contigN50={n50}  {date}  {bp}  {name}"
                     for acc, lvl, n50, date, bp, name in rows)
    return _cap(hdr + body)


def _sra_runs(args) -> str:
    """Answer 'is there public sequencing data for X?' — esearch the SRA db and parse
    efetch runinfo (CSV) into rows (run, platform, model, spots, bases, layout)."""
    query = (args.get("query") or "").strip()
    if not query:
        return "error: missing 'query' (e.g. 'Escherichia coli')"
    retmax = min(int(args.get("retmax") or 20), 200)
    search = _cli_raw_bytes(["esearch", "-db", "sra", "-query", query], timeout=60)
    if isinstance(search, str):   # error message
        return search
    # esearch prints an ENTREZ_DIRECT envelope whose <Count> is the total hit count.
    found = re.search(rb"<Count>(\d+)</Count>", search or b"")
    total = int(found.group(1)) if found else None
    out, err = _cli_raw(["efetch", "-format", "runinfo", "-stop", str(retmax)],
                        stdin=search)
    if err:
        if err.startswith("__missing__"):
            return ("error: NCBI EDirect is not installed. See "
                    "https://www.ncbi.nlm.nih.gov/books/NBK179288/")
        return err
    import csv
    rows = []
    for row in csv.DictReader(io.StringIO(out)):
        run = row.get("Run")
        if not run:
            continue
        rows.append((run, row.get("Platform") or "?", row.get("Model") or "?",
                     row.get("spots") or "?", row.get("bases") or "?",
                     row.get("LibraryLayout") or "?", row.get("ScientificName") or "?"))
        if len(rows) >= retmax:
            break
    if not rows:
        return f"No SRA runs found for '{query}'."
    # The Count is of SRA records (experiments/runs as indexed), so it can differ from the
    # number of run rows; label it as the search total, not as a run count.
    hdr = (f"{len(rows)} SRA run(s) for '{query}'"
           + (f" (the search matched {total:,} SRA record(s); raise retmax, max 200, to see more)"
              if total is not None and total > len(rows) else "")
           + " (run | platform | model | spots | bases | layout | organism):\n")
    body = "\n".join(f"  {r[0]}  {r[1]}/{r[2]}  spots={r[3]}  bases={r[4]}  {r[5]}  {r[6]}"
                     for r in rows)
    return _cap(hdr + body)


def _cli_raw_bytes(argv: list, timeout: int = 60):
    """Run a CLI and return raw stdout bytes (for piping), or an error string."""
    if shutil.which(argv[0]) is None:
        return ("error: NCBI EDirect is not installed. See "
                "https://www.ncbi.nlm.nih.gov/books/NBK179288/")
    try:
        p = subprocess.run(argv, capture_output=True, timeout=timeout, check=False)
    except subprocess.TimeoutExpired:
        return f"error: '{argv[0]}' timed out after {timeout}s."
    if p.returncode != 0 and not p.stdout.strip():
        return f"[{argv[0]} exit {p.returncode}] {p.stderr.decode('utf-8','replace').strip()}"
    return p.stdout


# --- NCBI EDirect (Entrez: esearch → esummary/efetch) ------------------------
def _edirect(args) -> str:
    """Run an Entrez query via EDirect: `esearch -db <db> -query <q>` piped into
    `esummary` (default) or `efetch -format <format>`. Covers the common search→fetch
    path without a shell."""
    db = (args.get("db") or "").strip()
    query = (args.get("query") or "").strip()
    if not db or not query:
        return 'error: provide "db" (e.g. "sra","assembly","pubmed","nuccore") and "query"'
    action = (args.get("action") or "esummary").strip()
    if action not in ("esummary", "efetch"):
        return 'error: "action" must be "esummary" or "efetch"'
    if shutil.which("esearch") is None:
        return ("error: NCBI EDirect is not installed. See "
                "https://www.ncbi.nlm.nih.gov/books/NBK179288/")
    retmax = min(int(args.get("retmax") or 20), 200)
    search = subprocess.run(["esearch", "-db", db, "-query", query],
                            capture_output=True, timeout=60, check=False)
    if search.returncode != 0:
        return f"[esearch exit {search.returncode}] {search.stderr.decode('utf-8','replace').strip()}"
    fetch_argv = [action, "-stop", str(retmax)]
    if action == "efetch" and args.get("format"):
        fetch_argv += ["-format", str(args["format"])]
    return _run_cli(fetch_argv, stdin=search.stdout)


# --- aggregator: fan out + dedupe by DOI ------------------------------------
# Ask each source for this many times `max_results`, then return only `max_results`.
# Fetching exactly n per source and truncating the merged list to n discarded almost
# everything the second source contributed (measured: 9-10 of every 10 Crossref hits
# never survived the cut), which made the aggregate no broader than a single source —
# the whole point of merging. Fetching wider gives the dedupe something to work on.
_FANOUT = 3
_FANOUT_CAP = 60


def _crossref_row(it, source, date_key="issued"):
    """Map one Crossref `items` entry onto the common paper dict. `date_key` differs by
    record type: journal articles carry `issued`, preprints `posted`. Search items carry
    `updated-by`, so retraction notices are read here at no extra cost."""
    from . import pubstatus
    dated = ((it.get(date_key) or it.get("created") or {}).get("date-parts") or [[None]])[0][0]
    return {"title": _clean_abstract((it.get("title") or ["(untitled)"])[0]),
            "doi": _norm_doi(it.get("DOI") or ""), "year": dated,
            "cited_by": it.get("is-referenced-by-count"),
            "venue": (it.get("container-title") or [None])[0] or source,
            "authors": [f"{x.get('given','')} {x.get('family','')}".strip()
                        for x in (it.get("author") or [])[:6]],
            "abstract": _clean_abstract(it.get("abstract") or ""),
            "preprint": it.get("type") == "posted-content",
            "status": pubstatus.from_crossref(it), "source": source}


def _preprint_source(it) -> str:
    """Cold Spring Harbor Lab (Crossref member 246) registers bioRxiv and medRxiv alike
    as `posted-content`; only the institution/group name tells the two servers apart."""
    inst = " ".join(i.get("name", "") for i in (it.get("institution") or [])).lower()
    group = (it.get("group-title") or "").lower()
    return "medrxiv" if ("medrxiv" in inst or "medrxiv" in group) else "biorxiv"


# Reciprocal rank fusion (Cormack, Clarke & Büttcher 2009): score = sum over sources of
# 1 / (K + rank). K=60 is the published default; it rewards agreement between sources
# without letting one source's top hit dominate.
_RRF_K = 60


def _paper_key(p) -> str:
    """Identity for merging across sources: DOI, else PMID, else a normalised title."""
    if p.get("doi"):
        return "doi:" + p["doi"]
    if p.get("pmid"):
        return "pmid:" + str(p["pmid"])
    title = re.sub(r"[^a-z0-9]+", " ", (p.get("title") or "").lower()).strip()
    return "title:" + title[:80] if title else ""


def _merge_paper(old, new):
    from . import pubstatus
    if old is None:
        merged = dict(new)
        merged["sources"] = [new["source"]]
        return merged
    for key, value in new.items():
        if key not in ("source", "status", "preprint") and value and not old.get(key):
            old[key] = value
    old["sources"].append(new["source"])
    old["status"] = pubstatus.merge(old.get("status") or {}, new.get("status") or {})
    old["preprint"] = bool(old.get("preprint") or new.get("preprint"))
    return old


def _fuse(ranked):
    """`ranked` is [(source, [paper, ...]), ...], each list in that source's own order."""
    scores, rows = {}, {}
    for _source, papers in ranked:
        seen_here = set()
        for rank, paper in enumerate(papers, 1):
            key = _paper_key(paper)
            if not key or key in seen_here:
                continue
            seen_here.add(key)
            scores[key] = scores.get(key, 0.0) + 1.0 / (_RRF_K + rank)
            rows[key] = _merge_paper(rows.get(key), paper)
    return [rows[k] for k in sorted(scores, key=lambda k: (-scores[k], k))]


def _paper_search(args):
    """OpenAlex + Crossref + Europe PMC in parallel, fused by reciprocal rank, with each
    source's outcome reported so a partial answer is labelled partial and a total outage
    is an error rather than 'No results.'"""
    from .results import SourceReport, SourceSummary, fail
    query = args.get("query") or ""
    if not query:
        return "error: missing 'query'"
    n = min(int(args.get("max_results") or 8), 20)
    wide = min(n * _FANOUT, _FANOUT_CAP)
    include_preprints = bool(args.get("include_preprints"))
    # Year bounds. Each API spells this differently, so build the fragments once rather
    # than filtering after the fact -- post-filtering would silently shrink the page and
    # make `max_results` mean something different when a year is given.
    from_year, to_year = args.get("from_year"), args.get("to_year")
    oa_filter, cr_filter = [], []
    if from_year:
        oa_filter.append(f"from_publication_date:{int(from_year)}-01-01")
        cr_filter.append(f"from-pub-date:{int(from_year)}-01-01")
    if to_year:
        oa_filter.append(f"to_publication_date:{int(to_year)}-12-31")
        cr_filter.append(f"until-pub-date:{int(to_year)}-12-31")
    oa_q = "&filter=" + urllib.parse.quote(",".join(oa_filter)) if oa_filter else ""
    cr_q = "&filter=" + urllib.parse.quote(",".join(cr_filter)) if cr_filter else ""
    q = urllib.parse.quote(query)
    mail = config.contact_email()

    def openalex():
        data = _get(f"https://api.openalex.org/works?search={q}&per-page={wide}{oa_q}"
                    f"&mailto={mail}")
        return [_openalex_paper(w) for w in data.get("results", [])]

    def crossref():
        data = _get(f"https://api.crossref.org/works?query={q}&rows={wide}{cr_q}"
                    f"&mailto={mail}")
        return [_crossref_row(it, "crossref") for it in (data.get("message") or {}).get("items", [])]

    def europepmc():
        return _europepmc_rows(query, wide, include_preprints, from_year, to_year)

    def preprints():
        data = _get(f"https://api.crossref.org/works?query={q}"
                    f"&filter=type:posted-content,member:246&rows={wide}&mailto={mail}")
        return [_crossref_row(it, _preprint_source(it), date_key="posted")
                for it in (data.get("message") or {}).get("items", [])]

    jobs = [("OpenAlex", openalex), ("Crossref", crossref), ("Europe PMC", europepmc)]
    if include_preprints:
        # Preprints are off by default: they are unreviewed, and a corpus built from
        # them reads as settled literature unless the caller asked for them.
        jobs.append(("bioRxiv/medRxiv", preprints))
    summary, ranked = SourceSummary(), []
    with ThreadPoolExecutor(max_workers=len(jobs)) as pool:
        futures = [(name, pool.submit(fn)) for name, fn in jobs]
        for name, future in futures:
            try:
                papers = future.result()
            except Exception as e:  # noqa: BLE001 - reported per source, never swallowed
                summary.add(SourceReport.failed(name, e))
                continue
            summary.add(SourceReport(name, hits=len(papers)))
            ranked.append((name, papers))
    if summary.all_failed():
        return fail(summary.failure_text("paper search"))
    merged = _fuse(ranked)
    shown = merged[:n]
    if not shown:
        body = ("No results for this query from "
                + ", ".join(r.name for r in summary.answered) + " (each search ran).")
    else:
        body = _fmt(shown)
    unchecked = sum(1 for p in shown if not (p.get("status") or {}).get("checked"))
    lines = [summary.header(), body, "",
             f"({len(merged)} unique across sources; {summary.footer()})"]
    if unchecked:
        lines.append(f"{unchecked} result(s) above came only from Europe PMC, so their "
                     "retraction status was not checked — run verify_ids (or "
                     "openalex_by_doi) before citing them.")
    return "\n".join(line for line in lines if line is not None).lstrip("\n")


def _mk(name, description, params, sync_fn, read_only=True):
    async def run(a, _f=sync_fn):
        return await asyncio.to_thread(_f, a)
    return Tool(name=name, description=description, parameters=params,
                read_only=read_only, run=run)


def native_tools() -> list:
    """The in-package research toolset exposed to the model (all read-only)."""
    q = {"query": {"type": "string",
                   "description": "Natural-language topic or keywords to search for."},
         "max_results": {"type": "integer",
                         "description": "Max hits to return, 1–25 (default 8)."}}
    return [
        _mk("web_search", "Search the open web (DuckDuckGo, keyless) and return "
            "title/URL/snippet per hit. Use for non-bibliographic context (a lab site, a "
            "data portal, a news item). Never cite a paper from here — confirm it via "
            "paper_search/openalex_by_doi to get a real DOI first.",
            {"type": "object", "properties": q, "required": ["query"],
             "additionalProperties": False}, _web_search),
        _mk("openalex_by_doi", "The verification path for one work, by DOI: its full "
            "abstract, DOI/PMID/PMCID, work type, open-access status, citation counts, and "
            "retraction status checked against both OpenAlex and Crossref. A DOI OpenAlex "
            "lacks is checked at Crossref and doi.org, so 'this DOI does not exist' and "
            "'not indexed here' stay distinct. Args: {doi}.",
            {"type": "object",
             "properties": {"doi": {"type": "string",
                                    "description": "DOI, with or without the "
                                                   "https://doi.org/ prefix."}},
             "required": ["doi"]},
            _openalex_by_doi),
        _mk("europepmc_search", "Search Europe PMC alone, with its own query syntax "
            "(e.g. ORGANISM:, SRC:AGR for Agricola). paper_search already includes Europe "
            "PMC — use this only for field queries. Preprints are included and flagged. "
            "Args: {query, max_results?}.",
            {"type": "object", "properties": q, "required": ["query"]}, _europepmc_search),
        _mk("read_paper", "Download an open-access PDF (by DOI or direct URL) and "
            "extract its text, labelled by page. Args: {doi?, url?, pattern?, "
            "ignore_case?, max_pages?, start_page?, context?}. Provide one of doi/url. "
            "With `pattern` (a regex) it returns only the matching lines, each with its "
            "page ('p.N:') and `context` lines — e.g. pull an N50, '2n=', or an "
            "accession without loading the whole paper; without it, the full text "
            "(default 30 pages; a pattern search covers every page). A read that reaches "
            "the size cap ends with 'continue with start_page=N'. Cite passages by page. "
            "Image-only PDFs yield nothing.",
            {"type": "object",
             "properties": {"doi": {"type": "string",
                                    "description": "DOI of an open-access work."},
                            "url": {"type": "string",
                                    "description": "Direct http(s) URL of a PDF."},
                            "pattern": {"type": "string",
                                        "description": "Regex; return only matching lines."},
                            "ignore_case": {"type": "boolean",
                                            "description": "Case-insensitive pattern (default: true)."},
                            "max_pages": {"type": "integer",
                                          "description": "Page cap (default 30, or all pages with a pattern)"},
                            "start_page": {"type": "integer",
                                           "description": "First page to read, 1-based (default 1). "
                                                          "Use the page a capped read stopped at."},
                            "context": {"type": "integer",
                                        "description": "Lines of context around each pattern "
                                                       "hit, 0-5 (default 1)."}}},
            _read_paper),
        _mk("ncbi_datasets", "Run the NCBI `datasets` CLI (genome/gene/taxonomy data). "
            "Args: {args: [string,...]} — the subcommand + flags, e.g. "
            "[\"summary\",\"genome\",\"taxon\",\"Homo sapiens\",\"--as-json-lines\"]. "
            "Requires the CLI to be installed locally.",
            {"type": "object",
             "properties": {"args": {"type": "array", "items": {"type": "string"},
                                     "description": "The datasets subcommand and flags, "
                                                    "e.g. ['summary', 'genome', 'taxon', "
                                                    "'Glycine max', '--as-json-lines']."}},
             "required": ["args"]}, _ncbi_datasets),
        _mk("ncbi_assembly_status", "Does a reference genome exist for a taxon, and at "
            "what quality? Returns a table (accession, assembly level, contig N50, "
            "date, BioProject, organism), most complete first, at most 50 rows; the "
            "header gives the total when there are more. Args: {taxon}. Requires the "
            "NCBI `datasets` CLI.",
            {"type": "object",
             "properties": {"taxon": {"type": "string",
                                      "description": "Scientific name (e.g. 'Glycine "
                                                     "max') or NCBI taxid."}},
             "required": ["taxon"]}, _ncbi_assembly_status),
        _mk("sra_runs", "Is there public sequencing data for X? Searches the NCBI SRA "
            "and returns runs (accession, platform/model, spots, bases, layout, "
            "organism); the header gives the search's total when more records matched "
            "than are shown. Args: {query, retmax?}. Requires NCBI EDirect.",
            {"type": "object",
             "properties": {"query": {"type": "string",
                                      "description": "Entrez search term, e.g. 'Glycine "
                                                     "max'."},
                            "retmax": {"type": "integer", "description": "1–200 (default 20)"}},
             "required": ["query"]}, _sra_runs),
        _mk("edirect", "Query NCBI Entrez via EDirect: esearch on a database piped to "
            "esummary/efetch. Args: {db, query, action?(esummary|efetch), format?, "
            "retmax?}. e.g. {db:'assembly', query:'Homo sapiens[Organism]'}. "
            "Requires EDirect installed locally.",
            {"type": "object",
             "properties": {"db": {"type": "string",
                                   "description": "Entrez database, e.g. 'sra', "
                                                  "'assembly', 'nuccore', 'taxonomy', "
                                                  "'pubmed'."},
                            "query": {"type": "string",
                                      "description": "Entrez query, e.g. 'Glycine "
                                                     "max[Organism]'."},
                            "action": {"type": "string", "enum": ["esummary", "efetch"],
                                       "description": "'esummary' (default) or 'efetch'."},
                            "format": {"type": "string",
                                       "description": "efetch output format, e.g. "
                                                      "'runinfo', 'fasta', 'docsum'."},
                            "retmax": {"type": "integer", "description": "1–200 (default 20)"}},
             "required": ["db", "query"]}, _edirect),
        _mk("paper_search", "Broad literature search across OpenAlex, Crossref and "
            "Europe PMC, run in parallel and merged by reciprocal rank (deduplicated by "
            "DOI/PMID). The main discovery tool. Each hit shows DOI/PMID/PMCID, flags "
            "RETRACTED/EXPRESSION OF CONCERN from source metadata, and the footer says "
            "which sources answered — a PARTIAL RESULTS header means one failed. "
            "Args: {query, max_results?, from_year?, to_year?, include_preprints?}. "
            "include_preprints also pulls bioRxiv/medRxiv posted-content (unreviewed; "
            "off by default).",
            {"type": "object",
             "properties": {**q,
                            "max_results": {"type": "integer",
                                            "description": "Max hits to return, 1–20 "
                                                           "(default 8)."},
                            "from_year": {
                                "type": "integer",
                                "description": "Earliest publication year (inclusive)."},
                            "to_year": {
                                "type": "integer",
                                "description": "Latest publication year (inclusive)."},
                            "include_preprints": {
                                "type": "boolean",
                                "description": "Also search bioRxiv/medRxiv preprints "
                                               "(default: false)."}},
             "required": ["query"]}, _paper_search),
    ]
