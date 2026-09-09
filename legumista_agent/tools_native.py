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
from pathlib import Path

import config

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


def _ip_is_blocked(ip_str: str) -> bool:
    try:
        ip = ipaddress.ip_address(ip_str)
    except ValueError:
        return True                       # unparseable -> refuse
    mapped = getattr(ip, "ipv4_mapped", None)
    if mapped is not None:
        ip = mapped                       # unwrap ::ffff:a.b.c.d so v4 rules apply
    return bool(ip.is_loopback or ip.is_link_local or ip.is_private
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
    return rp, None


def _get(url: str, accept: str = "application/json"):
    ua = f"legumista-agent/1.0 (research; mailto:{config.contact_email()})"
    req = urllib.request.Request(url, headers={"User-Agent": ua, "Accept": accept})
    with _open_guarded(req, HTTP_TIMEOUT) as resp:
        raw = resp.read(GET_MAX_BYTES).decode("utf-8", "replace")
    return json.loads(raw) if "json" in accept else raw


def _get_bytes(url: str, limit: int = 40_000_000) -> bytes:
    """Fetch a URL as raw bytes (for PDFs), size-capped."""
    ua = f"legumista-agent/1.0 (research; mailto:{config.contact_email()})"
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


def _fmt(papers: list) -> str:
    """Render a list of {title,authors,year,doi,venue,abstract,source} to text."""
    if not papers:
        return "No results."
    out = []
    for i, p in enumerate(papers, 1):
        authors = p.get("authors") or []
        a = ", ".join(authors[:4]) + (f", … (+{len(authors) - 4})" if len(authors) > 4 else "")
        line = f"[{i}] {p.get('title') or '(untitled)'} ({p.get('year') or 'n.d.'})"
        meta = []
        if p.get("doi"):
            meta.append(f"doi:{p['doi']}")
        if p.get("venue"):
            meta.append(p["venue"])
        if p.get("cited_by") is not None:
            meta.append(f"cited-by:{p['cited_by']}")
        if p.get("source"):
            meta.append(f"src:{p['source']}")
        out.append(line + ("\n    " + " | ".join(meta) if meta else "")
                   + (f"\n    authors: {a}" if a else "")
                   + (f"\n    {(p.get('abstract') or '')[:400]}" if p.get("abstract") else ""))
    return _cap("\n".join(out))


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


# --- grep (local, stdlib) ---------------------------------------------------
def _grep(args) -> str:
    pattern = args.get("pattern") or ""
    if not pattern:
        return "error: missing 'pattern'"
    root = args.get("path") or "."
    globpat = args.get("glob") or "**/*"
    try:
        rx = re.compile(pattern, re.IGNORECASE if args.get("ignore_case", True) else 0)
    except re.error as e:
        return f"error: bad regex: {e}"
    root_rp, err = _sandbox_path(root)     # confine the search to the workspace
    if err:
        return err
    hits, n = [], 0
    base = Path(root_rp)
    paths = [base] if base.is_file() else sorted(base.glob(globpat))
    for path in paths:
        if not path.is_file():
            continue
        if _sandbox_path(str(path))[1]:    # skip files outside the sandbox / secret-like
            continue
        try:
            with open(path, "r", encoding="utf-8", errors="ignore") as f:
                for lineno, line in enumerate(f, 1):
                    if rx.search(line):
                        hits.append(f"{path}:{lineno}: {line.rstrip()[:200]}")
                        n += 1
                        if n >= 200:
                            hits.append("… [200-match cap]")
                            return _cap("\n".join(hits))
        except (OSError, UnicodeError):
            continue
    return _cap("\n".join(hits)) if hits else "No matches."


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
def _openalex_by_doi(args) -> str:
    doi = _norm_doi(args.get("doi") or "")
    if not doi:
        return "error: missing 'doi'"
    try:
        w = _get(f"https://api.openalex.org/works/doi:{urllib.parse.quote(doi)}"
                 f"?mailto={config.contact_email()}")
    except Exception as e:  # noqa: BLE001
        return f"error: OpenAlex lookup failed: {type(e).__name__}: {e}"
    refs = w.get("referenced_works") or []
    p = {"title": w.get("title"), "doi": doi, "year": w.get("publication_year"),
         "cited_by": w.get("cited_by_count"),
         "venue": (((w.get("primary_location") or {}).get("source")) or {}).get("display_name"),
         "authors": [(a.get("author") or {}).get("display_name")
                     for a in (w.get("authorships") or []) if (a.get("author") or {}).get("display_name")],
         "abstract": _reconstruct_abstract(w.get("abstract_inverted_index")), "source": "openalex"}
    return _fmt([p]) + f"\n\nreferenced_works: {len(refs)} | cited_by: {w.get('cited_by_count')}"


# --- Europe PMC (keyless; PubMed/PMC/preprints) -----------------------------
def _europepmc_search(args) -> str:
    query = args.get("query") or ""
    if not query:
        return "error: missing 'query'"
    n = min(int(args.get("max_results") or 8), 25)
    url = ("https://www.ebi.ac.uk/europepmc/webservices/rest/search?"
           f"query={urllib.parse.quote(query)}&format=json&pageSize={n}&resultType=core")
    try:
        data = _get(url)
    except Exception as e:  # noqa: BLE001
        return f"error: Europe PMC request failed: {type(e).__name__}: {e}"
    papers = []
    for r in ((data.get("resultList") or {}).get("result") or []):
        papers.append({
            "title": r.get("title"), "doi": _norm_doi(r.get("doi") or ""),
            "year": r.get("pubYear"), "cited_by": r.get("citedByCount"),
            "venue": r.get("journalTitle") or r.get("source"),
            "authors": [a.strip() for a in (r.get("authorString") or "").split(",")[:8] if a.strip()],
            "abstract": r.get("abstractText") or "", "source": "europepmc"})
    return _fmt(papers)


# --- read_paper: fetch an open-access PDF and extract its text ----------------
def _oa_pdf_url(doi: str) -> str:
    """Resolve an open-access PDF URL for a DOI: try OpenAlex locations first, then
    Unpaywall (both keyless). Returns "" if no OA PDF is known."""
    try:
        w = _get(f"https://api.openalex.org/works/doi:{urllib.parse.quote(doi)}"
                 f"?mailto={config.contact_email()}")
        for loc in ([w.get("best_oa_location"), w.get("primary_location")]
                    + (w.get("locations") or [])):
            if loc and loc.get("pdf_url"):
                return loc["pdf_url"]
    except Exception:  # noqa: BLE001
        pass
    try:
        up = _get(f"https://api.unpaywall.org/v2/{urllib.parse.quote(doi)}"
                  f"?email={config.contact_email()}")
        loc = up.get("best_oa_location") or {}
        return loc.get("url_for_pdf") or ""
    except Exception:  # noqa: BLE001
        return ""


def _fetch_pdf_text(doi: str, url: str, max_pages):
    """Resolve/download an OA PDF and extract text. Returns
    (text, source_url, n_pages, n_extracted, error) — error is None on success."""
    url = (url or "").strip()
    doi = _norm_doi(doi or "")
    if not url and not doi:
        return None, "", 0, 0, "error: provide 'doi' or 'url'"
    if not url:
        url = _oa_pdf_url(doi)
        if not url:
            return None, "", 0, 0, (f"No open-access PDF found for doi:{doi}. Read the "
                                    "abstract via openalex_by_doi, or try a different DOI.")
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
    mp = npages if max_pages is None else min(int(max_pages), npages)
    text = "\n".join((reader.pages[i].extract_text() or "") for i in range(mp))
    return text, url, npages, mp, None


def _read_paper(args) -> str:
    """Download an open-access PDF (by DOI or direct url) and extract its text; with a
    `pattern`, return only the lines matching it. One fetch, two output shapes — pulling
    a single figure (an N50, a '2n=', an accession) out of a paper is the same call as
    reading it, so the model never has to pick between two tools over one download."""
    pattern = args.get("pattern") or ""
    rx = None
    if pattern:
        try:
            rx = re.compile(pattern, re.IGNORECASE if args.get("ignore_case", True) else 0)
        except re.error as e:
            return f"error: bad regex: {e}"
    # A grep wants every page searched; a plain read wants a bounded dump it can afford
    # to put in context — hence the different max_pages defaults.
    text, url, npages, extracted, err = _fetch_pdf_text(
        args.get("doi"), args.get("url"), args.get("max_pages") or (None if rx else 30))
    if err:
        return err
    if rx is None:
        text = re.sub(r"\n{3,}", "\n\n", text).strip()
        head = f"[{npages} pages; extracted {extracted}] source: {url}\n\n"
        return head + (_cap(text) if text else "(no extractable text — likely a scanned/image PDF)")
    if not text:
        return f"(no extractable text from {url} — likely a scanned/image PDF)"
    hits = []
    for line in text.splitlines():
        line = " ".join(line.split())
        if line and rx.search(line):
            hits.append(line[:200])
            if len(hits) >= 100:
                hits.append("… [100-match cap]")
                break
    if not hits:
        return f"No matches for /{pattern}/ in {url} ({npages} pages)."
    return _cap(f"{len(hits)} match(es) for /{pattern}/ in {url}:\n" + "\n".join(hits))


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
        if len(rows) >= 50:
            break
    if not rows:
        return f"No assemblies found in NCBI for taxon '{taxon}'."
    hdr = (f"{len(rows)} assembly(ies) for '{taxon}' "
           "(accession | level | contig N50 | date | BioProject | organism):\n")
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
    hdr = f"{len(rows)} SRA run(s) for '{query}' (run | platform | model | spots | bases | layout | organism):\n"
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
    record type: journal articles carry `issued`, preprints `posted`."""
    dated = ((it.get(date_key) or it.get("created") or {}).get("date-parts") or [[None]])[0][0]
    return {"title": _clean_abstract((it.get("title") or ["(untitled)"])[0]),
            "doi": _norm_doi(it.get("DOI") or ""), "year": dated,
            "cited_by": it.get("is-referenced-by-count"),
            "venue": (it.get("container-title") or [None])[0] or source,
            "authors": [f"{x.get('given','')} {x.get('family','')}".strip()
                        for x in (it.get("author") or [])[:6]],
            "abstract": _clean_abstract(it.get("abstract") or ""), "source": source}


def _preprint_source(it) -> str:
    """Cold Spring Harbor Lab (Crossref member 246) registers bioRxiv and medRxiv alike
    as `posted-content`; only the institution/group name tells the two servers apart."""
    inst = " ".join(i.get("name", "") for i in (it.get("institution") or [])).lower()
    group = (it.get("group-title") or "").lower()
    return "medrxiv" if ("medrxiv" in inst or "medrxiv" in group) else "biorxiv"


def _paper_search(args) -> str:
    query = args.get("query") or ""
    if not query:
        return "error: missing 'query'"
    n = min(int(args.get("max_results") or 8), 20)
    wide = min(n * _FANOUT, _FANOUT_CAP)
    merged, seen, rows = [], set(), []      # fan out at the data layer, dedupe by DOI
    sources = ["OpenAlex", "Crossref"]
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
    try:
        oa = _get(f"https://api.openalex.org/works?search={urllib.parse.quote(query)}"
                  f"&per-page={wide}{oa_q}&mailto={config.contact_email()}").get("results", [])
        for w in oa:
            rows.append({"title": w.get("title"), "doi": _norm_doi(w.get("doi") or ""),
                         "year": w.get("publication_year"), "cited_by": w.get("cited_by_count"),
                         "venue": (((w.get("primary_location") or {}).get("source")) or {}).get("display_name"),
                         "authors": [(x.get("author") or {}).get("display_name")
                                     for x in (w.get("authorships") or [])[:6]],
                         "abstract": _reconstruct_abstract(w.get("abstract_inverted_index")),
                         "source": "openalex"})
    except Exception:  # noqa: BLE001
        pass
    try:
        cr = (_get(f"https://api.crossref.org/works?query={urllib.parse.quote(query)}"
                   f"&rows={wide}{cr_q}&mailto={config.contact_email()}").get("message")
              or {}).get("items", [])
        rows += [_crossref_row(it, "crossref") for it in cr]
    except Exception:  # noqa: BLE001
        pass
    if args.get("include_preprints"):
        # Preprints are off by default: they are unreviewed, and a corpus built from
        # them reads as settled literature unless the caller asked for them.
        sources.append("bioRxiv/medRxiv")
        try:
            pp = (_get(f"https://api.crossref.org/works?query={urllib.parse.quote(query)}"
                       f"&filter=type:posted-content,member:246&rows={wide}"
                       f"&mailto={config.contact_email()}").get("message") or {}).get("items", [])
            rows += [_crossref_row(it, _preprint_source(it), date_key="posted") for it in pp]
        except Exception:  # noqa: BLE001
            pass
    for r in rows:
        key = r["doi"] or (r.get("title") or "").lower()[:80]
        if key and key not in seen:
            seen.add(key)
            merged.append(r)
    return _fmt(merged[:n]) + f"\n\n({len(merged)} unique across {'+'.join(sources)})"


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
        _mk("grep", "Search local files for a regular expression and return matching "
            "`file:line: text` lines (capped at 200). Use to find text in project files; "
            "this searches the local filesystem, not the web (use web_search) or paper PDFs "
            "(use read_paper with a pattern).",
            {"type": "object",
             "properties": {"pattern": {"type": "string",
                                        "description": "Python regular expression to match."},
                            "path": {"type": "string",
                                     "description": "Directory or file to search (default: current dir)."},
                            "glob": {"type": "string",
                                     "description": "Filename glob to filter files, e.g. '**/*.md'."},
                            "ignore_case": {"type": "boolean",
                                            "description": "Case-insensitive match (default: true)."}},
             "required": ["pattern"], "additionalProperties": False}, _grep),
        _mk("web_search", "Search the open web (DuckDuckGo, keyless) and return "
            "title/URL/snippet per hit. Use for non-bibliographic context (a lab site, a "
            "data portal, a news item). Never cite a paper from here — confirm it via "
            "paper_search/openalex_by_doi to get a real DOI first.",
            {"type": "object", "properties": q, "required": ["query"],
             "additionalProperties": False}, _web_search),
        _mk("openalex_by_doi", "Fetch one work from OpenAlex by DOI (metadata + "
            "reference/citation counts). Args: {doi}.",
            {"type": "object", "properties": {"doi": {"type": "string"}}, "required": ["doi"]},
            _openalex_by_doi),
        _mk("europepmc_search", "Search Europe PMC (PubMed/PMC/preprints, life sciences), "
            "with abstracts. Args: {query, max_results?}.",
            {"type": "object", "properties": q, "required": ["query"]}, _europepmc_search),
        _mk("read_paper", "Download an open-access PDF (by DOI or direct URL) and "
            "extract its text. Args: {doi?, url?, pattern?, ignore_case?, max_pages?}. "
            "Provide one of doi/url. With `pattern` (a regex) it returns only the "
            "matching lines — e.g. pull an N50, '2n=', or an accession without loading "
            "the whole paper; without it, the full extracted text (default 30 pages; "
            "a pattern search covers every page). Image-only PDFs yield nothing.",
            {"type": "object",
             "properties": {"doi": {"type": "string"}, "url": {"type": "string"},
                            "pattern": {"type": "string",
                                        "description": "Regex; return only matching lines."},
                            "ignore_case": {"type": "boolean",
                                            "description": "Case-insensitive pattern (default: true)."},
                            "max_pages": {"type": "integer",
                                          "description": "Page cap (default 30, or all pages with a pattern)"}}},
            _read_paper),
        _mk("ncbi_datasets", "Run the NCBI `datasets` CLI (genome/gene/taxonomy data). "
            "Args: {args: [string,...]} — the subcommand + flags, e.g. "
            "[\"summary\",\"genome\",\"taxon\",\"Homo sapiens\",\"--as-json-lines\"]. "
            "Requires the CLI to be installed locally.",
            {"type": "object",
             "properties": {"args": {"type": "array", "items": {"type": "string"}}},
             "required": ["args"]}, _ncbi_datasets),
        _mk("ncbi_assembly_status", "Does a reference genome exist for a taxon, and at "
            "what quality? Returns a table (accession, assembly level, contig N50, "
            "date, BioProject, organism). Args: {taxon}. Requires the NCBI `datasets` CLI.",
            {"type": "object", "properties": {"taxon": {"type": "string"}},
             "required": ["taxon"]}, _ncbi_assembly_status),
        _mk("sra_runs", "Is there public sequencing data for X? Searches the NCBI SRA "
            "and returns runs (accession, platform/model, spots, bases, layout, "
            "organism). Args: {query, retmax?}. Requires NCBI EDirect.",
            {"type": "object",
             "properties": {"query": {"type": "string"},
                            "retmax": {"type": "integer", "description": "1–200 (default 20)"}},
             "required": ["query"]}, _sra_runs),
        _mk("edirect", "Query NCBI Entrez via EDirect: esearch on a database piped to "
            "esummary/efetch. Args: {db, query, action?(esummary|efetch), format?, "
            "retmax?}. e.g. {db:'assembly', query:'Homo sapiens[Organism]'}. "
            "Requires EDirect installed locally.",
            {"type": "object",
             "properties": {"db": {"type": "string"}, "query": {"type": "string"},
                            "action": {"type": "string", "enum": ["esummary", "efetch"]},
                            "format": {"type": "string"},
                            "retmax": {"type": "integer", "description": "1–200 (default 20)"}},
             "required": ["db", "query"]}, _edirect),
        _mk("paper_search", "Broad literature search across OpenAlex + Crossref, "
            "deduplicated by DOI — each source is queried wider than the result set so "
            "the merge is genuinely multi-source. The main discovery tool; use "
            "europepmc_search alongside it for life-science coverage it does not reach. "
            "Args: {query, max_results?, from_year?, to_year?, include_preprints?}. "
            "include_preprints also pulls bioRxiv/medRxiv posted-content (unreviewed; "
            "off by default).",
            {"type": "object",
             "properties": {**q,
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
