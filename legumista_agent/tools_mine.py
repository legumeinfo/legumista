#!/usr/bin/env python3
"""LIS InterMine tools — narrow, per-question queries against a LIS mine.

Complements `tools_lis.py` (the Data Store). The Data Store serves *files*; a mine serves
*curated relationships* — gene families, ontology annotations, protein records, expression
values. Notably `geneFamilyAssignments` is queryable here while the Data Store ships it as
an unindexed `.gfa.tsv.gz` that no tool can read.

**One tool per biologist question, not one tool per data class.** The mine's model has ~91
classes; exposing a general query builder would push InterMine's failure modes onto the
model. Each tool below owns a single tested PathQuery, so the traps are handled once:

1. **Silent empty results.** InterMine answers HTTP *200* with `results: []` and puts the
   real reason in `wasSuccessful`/`error` — e.g. an under-specified template returns
   "There isn't a specified constraint value ... this constraint is required." A client
   that reads `results` alone concludes "no data" when the truth is "bad query". Every
   result here goes through `_run`, which separates *failed* from *valid but empty*.
2. **Which identifier field?** A LIS gene is `Glyma.12G040000` (name / secondaryIdentifier)
   and `glyma.Wm82.gnm4.ann1.Glyma.12G040000` (primaryIdentifier). Rather than choose, all
   gene constraints use InterMine's `LOOKUP` operator, which searches identifier fields.
3. **One gene name, several assemblies.** A bare name matches gnm2, gnm4 AND gnm6 — three
   different loci. Results always carry the assembly/annotation, and `assembly`/
   `annotation` narrow it.
4. **Large result sets.** One gene has 639 expression values, and a description search can
   return 200,000 genes. Every result is fetched whole, in parallel chunks under a sort
   with no ties, up to a safety limit that is always reported (`_fetch_all`), and paged
   from the merged list. A fetch that outlasts TIME_BUDGET keeps running, and the same
   call collects it.
5. **A mine that does not exist.** Only ~10 genera have a mine; the store holds ~48 species
   that have none. Routing them by name produced a 404 the model reads as an outage, so
   the catalog's published mines are checked BEFORE any request (see `_known_mines`).

Merged results are cached for paging, bounded by row count (`_merged_result`); small
lookups are memoized for the life of the process (`_cached`). Errors are never cached, so
a retry retries.

Mine selection (`_plan`): every query goes to `MINE` (env `LEGUMISTA_LIS_MINE`, default
`legumemine`, the pan-legume mine) whenever its data model has the class queried, and to
the subject genus's own mine too, when one exists and has it. Neither mine holds
everything the other does, and agents told so still queried legumemine alone, so the
tools ask both and merge them (`_merge`), marking each row with the mine(s) holding it.
'taxon' names the subject and filters by it; 'mine' queries one mine alone.
"""
import asyncio
import os
import re
import threading
import urllib.parse
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeout
from xml.sax.saxutils import quoteattr

from . import tools_catalog
from .results import count_phrase
from .tool import Tool
from .tools_native import MAX_CHARS, _cap, _get, _validate_url

MINES_BASE = os.environ.get("LEGUMISTA_LIS_MINES_BASE",
                            "https://mines.legumeinfo.org").rstrip("/")
MINE = os.environ.get("LEGUMISTA_LIS_MINE", "legumemine")
# Characters of rows per reply. The rest of MAX_CHARS is left for what callers append (a
# footer, the family caveat), so rows are dropped whole here and never cut mid-row by
# _cap -- a list cut mid-row has no honest "continue from" point.
ROW_BUDGET = MAX_CHARS - 2500


def _service(mine: str) -> str:
    return f"{MINES_BASE}/{mine}/service"


def _pathquery(view, constraints, sort=None) -> str:
    """Build PathQuery XML. Values are attribute-escaped — a gene name is model-supplied
    input and must never be able to close the tag and inject a constraint."""
    parts = [f'<query model="genomic" view={quoteattr(" ".join(view))}']
    if sort:
        parts.append(f" sortOrder={quoteattr(sort)}")
    parts.append(">")
    for path, op, value in constraints:
        parts.append(f'<constraint path={quoteattr(path)} op={quoteattr(op)} '
                     f'value={quoteattr(str(value))}/>')
    parts.append("</query>")
    return "".join(parts)


def _url(mine: str, xml: str, fmt: str, size: int = None, start: int = 0) -> str:
    params = {"query": xml, "format": fmt}
    if size is not None:
        params["size"] = str(size)
    if start:
        params["start"] = str(start)
    return f"{_service(mine)}/query/results?" + urllib.parse.urlencode(params)


# --- response cache --------------------------------------------------------------------
# A mine answer is deterministic for the life of a request: the same PathQuery at the same
# size returns the same rows. An agent working one gene re-issues them anyway — asking for
# a gene's family members re-derives the gene->family mapping `mine_gene_families`
# just fetched — so identical queries are collapsed to one round trip.
#
# ONLY successes are cached. Caching an error would turn a transient outage into a
# permanent one for this process, and the "retry later" advice we hand back would be a lie.
_CACHE: dict = {}
_CACHE_LOCK = threading.Lock()


def _cached(key, compute):
    """Memoize `compute()` under `key`, unless it reports failure.

    `compute` returns (value, cacheable); the miss is recomputed every time when
    `cacheable` is false. Same shape as tools_catalog's lazy load: a plain dict behind a
    lock, since the values are immutable results and the cost of a double-compute race is
    one extra request, not corruption."""
    with _CACHE_LOCK:
        if key in _CACHE:
            return _CACHE[key]
    value, cacheable = compute()
    if cacheable:
        with _CACHE_LOCK:
            _CACHE[key] = value
    return value


def reset_cache():
    """Forget every cached mine response. For tests, and for a future reload command."""
    with _CACHE_LOCK:
        _CACHE.clear()
    with _MERGED_LOCK:
        _MERGED.clear()
        _PENDING.clear()
    with _MINES_LOCK:
        _KNOWN.clear()
        _PROBED.clear()


def _count(mine: str, xml: str):
    """Total matching rows, or None if the count itself failed (never fatal — a missing
    count only costs the caller the 'showing N of M' line)."""
    def compute():
        try:
            url = _url(mine, xml, "count")
            _validate_url(url)
            text = _get(url, accept="text/plain").strip()
        except Exception:  # noqa: BLE001
            return None, False
        return (int(text), True) if text.isdigit() else (None, False)

    return _cached(("count", mine, xml), compute)


def _run(mine: str, xml: str, size: int, start: int = 0):
    """Execute a PathQuery. Returns (rows, columns, error_text).

    The whole point: InterMine reports failure inside a 200 body, so `wasSuccessful` is
    checked before `results` is trusted. `start` skips that many rows, for paging."""
    return _cached(("run", mine, xml, size, start),
                   lambda: _run_uncached(mine, xml, size, start))


def _run_uncached(mine: str, xml: str, size: int, start: int = 0):
    """One PathQuery round trip. Returns ((rows, columns, error), cacheable) — an error is
    never cacheable, so a retry after an outage really retries."""
    result = _fetch(mine, xml, size, start)
    return result, result[2] is None


def _fetch(mine: str, xml: str, size: int, start: int = 0):
    try:
        url = _url(mine, xml, "json", size, start)
        _validate_url(url)
        doc = _get(url, accept="application/json")
    except Exception as e:  # noqa: BLE001
        return None, None, f"error: {mine} request failed: {type(e).__name__}: {e}"
    if not isinstance(doc, dict):
        return None, None, f"error: {mine} returned an unexpected (non-JSON) response."
    if not doc.get("wasSuccessful", False):
        detail = str(doc.get("error") or "no reason given").strip()
        hint = ("  The mine's query service is failing, not your query — try another mine "
                "via 'mine', or retry later." if "Service failed" in detail else "")
        return None, None, f"error: {mine} rejected the query: {detail}{hint}"
    return doc.get("results", []), _columns(doc.get("columnHeaders", [])), None


def _columns(headers):
    """Shorten InterMine's "Gene > Chromosome > Identifier" headers to their last segment,
    but keep enough path to stay unambiguous: a QTL view selecting both `QTL.name` and
    `QTL.linkageGroup.name` would otherwise print two columns called "Name"."""
    short = [h.split(" > ")[-1] for h in headers]
    out = []
    for header, name in zip(headers, short):
        if short.count(name) > 1:
            parts = header.split(" > ")
            name = " ".join(parts[-2:]) if len(parts) > 1 else name
        out.append(name)
    return out


def _gene_constraints(args, root="Gene"):
    """LOOKUP on the gene, plus optional assembly/annotation narrowing."""
    gene = (args.get("gene") or "").strip()
    cons = [(root, "LOOKUP", gene)]
    if (args.get("assembly") or "").strip():
        cons.append(("Gene.assemblyVersion", "=", args["assembly"].strip()))
    if (args.get("annotation") or "").strip():
        cons.append(("Gene.annotationVersion", "=", args["annotation"].strip()))
    return cons


def _render(title, mine, gene, rows, cols, total, size, note="", capped=False, offset=0,
            pageable=False, size_max=500):
    """Rows as a table. Rows past ROW_BUDGET are dropped whole and counted, and a
    `pageable` tool's reply names the offset that continues the list."""
    if not rows:
        return (f"{title}: no matches for {gene!r} in {mine}. The query was valid and "
                "returned zero rows — check the identifier, or widen 'assembly'/"
                "'annotation'.")
    head = f"{title} — {gene} [mine: {mine}]"
    header = "  " + " | ".join(cols)
    body, used = [], len(head) + len(header) + len(note) + 300
    for r in rows:
        line = "  " + " | ".join("" if v is None else str(v) for v in r)
        if body and used + len(line) + 1 > ROW_BUDGET:
            break
        body.append(line)
        used += len(line) + 1
    cut = len(body) < len(rows)
    shown = count_phrase(len(body), total, "row(s)", capped=capped or cut, start=offset)
    end = offset + len(body)
    if (total is not None and end < total) or (total is None and (capped or cut)):
        if pageable:
            shown += f" — continue with offset={end}"
            if not cut and size < size_max:
                shown += f" (or raise 'max_results', up to {size_max})"
        elif cut:
            shown += (f" — the reply's size limit stopped the list at {len(body)} of the "
                      f"{len(rows)} rows fetched; narrow the query to see the rest")
        else:
            shown += (f" — capped at {size}; raise 'max_results' (up to {size_max}) to see "
                      "more")
    lines = [head, shown + (f"  {note}" if note else ""), header] + body
    return _cap("\n".join(lines))


def _assembly_note(rows, idx):
    """Warn when one bare gene name resolved to several assemblies — those are different
    loci, and silently mixing them is a correctness bug in the caller's analysis."""
    seen = {r[idx] for r in rows if len(r) > idx and r[idx]}
    if len(seen) > 1:
        return (f"NOTE: matched {len(seen)} assemblies ({', '.join(sorted(map(str, seen)))})"
                " — pass 'assembly' to pick one.")
    return ""


# --- which mines exist -----------------------------------------------------------------
# Every LIS mine is named "<genus>mine" in lower case, so a taxon routes to its mine
# without a lookup table. The guess is right for every genus that HAS a mine — but the
# store holds ~48 species that have none, and for those the guess used to produce a doomed
# request whose 404 reads like an outage ("viciamine request failed: HTTP Error 404"). The
# model cannot tell "this species has no mine" from "the mine is down", so it retries or
# concludes the data does not exist. The catalog knows which mines are published, so an
# unpublished one is refused before any request is made.
_MINE_URL = re.compile(r"^https?://[^/]+/([A-Za-z0-9_-]+mine)(?:/|$)", re.IGNORECASE)

# The catalog's per-taxon `resources` are curated for the web site and list 8 of the 10
# live genus mines -- cajanusmine and lensmine answer /service/version but appear nowhere
# in it. Trusting the catalog alone would therefore refuse two real mines (pigeonpea and
# lentil, both with breeding data), so a name the catalog does not know is PROBED once
# before being refused rather than being carried in a hardcoded list. A list would drift
# silently in the refusing direction every time LIS adds a mine; a probe self-corrects,
# and only ever runs on the miss path.
_KNOWN: dict = {}
_PROBED: dict = {}
_MINES_LOCK = threading.Lock()


def _known_mines():
    """The set of mine names known to exist, or None when no catalog is loaded.

    None is not an empty set: it means we cannot know, and callers must fall back to the
    genus guess rather than refuse everything."""
    with _MINES_LOCK:
        if "mines" in _KNOWN:
            return _KNOWN["mines"]
    ctl = tools_catalog.controller()
    mines = None
    if ctl is not None:
        # Some `resources` entries are split across several dicts (Aeschynomene's name,
        # URL and description each sit in their own), so scan for URLs, not for names.
        found = set()
        for meta in (ctl.document.get("taxa") or {}).values():
            resources = meta.get("resources") if isinstance(meta, dict) else None
            for resource in resources or []:
                url = resource.get("URL") if isinstance(resource, dict) else None
                match = _MINE_URL.match(str(url or ""))
                if match:
                    found.add(match.group(1).lower())
        # MINE is the configured default (the pan-legume mine); it is a mine of no genus,
        # so no taxon lists it and it must be added by hand.
        mines = found | {MINE.lower()}
    with _MINES_LOCK:
        _KNOWN["mines"] = mines
    return mines


def _mine_for_taxon(taxon: str) -> str:
    """'<genus>mine' for a taxon.

    The genus comes from the catalog's resolver, so a common name ('soybean') or
    abbreviation ('glyma') routes to glycinemine rather than to a nonexistent
    'soybeanmine'. An ambiguous name whose candidates share a genus ('wild peanut': two
    Arachis species) routes to that genus. Otherwise the first word is used: the catalog
    is incomplete, and _resolve_mine probes a guessed mine before refusing it."""
    match = tools_catalog.resolve_taxon(taxon)
    genus = match.genus
    if not genus and match.candidates:
        genera = {c.split()[0] for c in match.candidates}
        genus = genera.pop() if len(genera) == 1 else ""
    genus = genus or ((taxon or "").replace("_", " ").split() or [""])[0]
    return genus.lower() + "mine"


def _mine_exists(mine: str) -> bool:
    """Does this mine answer? Asked only when the catalog has not heard of it.

    One cheap /service/version call, cached for the process. This is what keeps the
    catalog's incompleteness from hardening into a wrong refusal: the catalog is the
    fast path, the probe is the correction.
    """
    with _MINES_LOCK:
        if mine in _PROBED:
            return _PROBED[mine]
    try:
        url = f"{_service(mine)}/version"
        _validate_url(url)
        _get(url, accept="text/plain")
        alive = True
    except Exception:  # noqa: BLE001 - a 404 (or anything else) means "not usable"
        alive = False
    with _MINES_LOCK:
        _PROBED[mine] = alive
    return alive


def _no_mine(taxon: str, mine: str) -> str:
    """The honest answer for a species with no mine — no request, no ambiguous 404."""
    return (f"{taxon} has no InterMine ({mine} is not a published LIS mine, so this is "
            "not an outage and retrying will not help). QTL/GWAS/marker/gene data for it, "
            f"if any, is in the LIS Data Store — try lis_find(taxon={taxon!r}, type='qtl') "
            "(also 'gwas', 'markers', 'maps', 'annotations'), then lis_files for the "
            "files themselves.")


def _resolve_mine(args, require_taxon=False):
    """Pick the mine: explicit `mine` wins, else route from `taxon`/`genus`.

    `require_taxon` is set by the breeding tools because QTL/GWAS/marker/map classes exist
    ONLY in the per-species mines — legumemine has no such classes, so defaulting there
    would raise a model error rather than return an honest empty result."""
    explicit = (args.get("mine") or "").strip()
    if explicit:
        # An explicit mine is the caller's own assertion; we route, we do not veto.
        return explicit, None
    taxon = (args.get("taxon") or args.get("genus") or "").strip()
    if taxon:
        match = tools_catalog.resolve_taxon(taxon)
        if len({c.split()[0] for c in match.candidates}) > 1:
            return None, match.problem()          # ambiguous across genera: ask, don't guess
        mine = _mine_for_taxon(taxon)
        known = _known_mines()
        if known is not None and mine not in known and not _mine_exists(mine):
            if not (match.genus or match.candidates):
                # The name did not resolve, and no mine answers to its first word: the
                # problem is the NAME, so say that rather than "X has no InterMine".
                return None, match.problem()
            return None, _no_mine(taxon, mine)
        return mine, None
    if require_taxon:
        return None, ("error: missing 'taxon' — QTL/GWAS/marker data lives only in the "
                      "per-species mines (legumemine has none of it), so name the species, "
                      "e.g. taxon='Glycine max' or taxon='Phaseolus vulgaris'.")
    return MINE, None


def _gene_presence(mine, args):
    """Does the gene exist in this mine at all, ignoring what the tool joins it to?

    An InterMine view is an inner join, so "gene has no GO terms" and "no such gene"
    both come back as zero rows. This separate LOOKUP tells them apart.
    Returns (primary_identifiers, error_text)."""
    xml = _pathquery(["Gene.primaryIdentifier"], _gene_constraints(args))
    rows, _cols, err = _run(mine, xml, 20)
    if err:
        return None, err
    return sorted({str(r[0]) for r in rows or [] if r and r[0]}), None


def _render_empty(title, mine, args, subject, what):
    """Zero rows for a gene-rooted query, explained."""
    narrowing = ", ".join(f"{k}={args[k]!r}" for k in ("assembly", "annotation")
                          if (args.get(k) or "").strip())
    scope = f" (with {narrowing})" if narrowing else ""
    ids, err = _gene_presence(mine, args)
    if err:
        return (f"{title}: zero rows for {subject!r} in {mine}{scope}, and checking whether "
                f"the gene exists failed ({err}). Treat the answer as unknown, not as absence.")
    if not ids:
        return (f"{title}: no gene matching {subject!r} in {mine}{scope} (InterMine LOOKUP "
                "over identifier fields). The ID may be misspelled, from an assembly this "
                "mine does not load, or from a species it does not cover.")
    shown = ", ".join(ids[:5]) + (f" (+{len(ids) - 5} more)" if len(ids) > 5 else "")
    return (f"{title}: {subject!r} exists in {mine}{scope} ({shown}) but has no {what} "
            "there. This is an empty result for this gene, not a failed lookup.")


def _explain_empty(args, what):
    """on_empty hook for gene-rooted tools: tell 'no such gene' from 'no rows of this kind'."""
    return lambda mine, subject: _render_empty(_TITLES.get(what, what), mine, args, subject,
                                               what)


_TITLES = {"protein records": "Proteins", "gene family assignments": "Gene families",
           "ontology annotations": "Ontology annotations",
           "expression values": "Expression values"}


# --- every mine that covers the subject ---------------------------------------------------
# A genus with its own mine is covered twice, by legumemine and by that mine, and neither
# holds everything the other does. Agents asked about one species queried legumemine alone,
# however plainly the genus mines were described, so the tools query both themselves: the
# full result from each, merged, every row marked with the mine(s) that hold it.
FETCH_CHUNK = int(os.environ.get("LEGUMISTA_MINE_FETCH_CHUNK", "10000"))
# A safety limit, not a page size: the whole result is fetched up to here, and a reply
# that reaches it says so. 300,000 rows covers the largest gene query seen (266,913).
FETCH_MAX = int(os.environ.get("LEGUMISTA_MINE_FETCH_MAX", "300000"))
FETCH_WORKERS = int(os.environ.get("LEGUMISTA_MINE_FETCH_WORKERS", "6"))
PAGE_MAX = 500
# Rows kept across cached merged results, so paging a large result does not refetch it.
MERGED_CACHE_ROWS = int(os.environ.get("LEGUMISTA_MINE_CACHE_ROWS", "500000"))
# Seconds a call waits for a fetch before replying that it is still running. A mine
# computing a large result it has not seen before can take a minute or more, longer than
# some clients wait for a tool; the fetch carries on, and the same call collects it.
TIME_BUDGET = float(os.environ.get("LEGUMISTA_MINE_TIME_BUDGET", "45"))

_QUALIFIED = re.compile(r"^(?P<abbrev>[a-z]{4,6})\.(?P<stem>[A-Za-z0-9_-]+\.gnm\d+\.ann\d+)\."
                        r"(?P<name>.+)$")
# Where each query root reaches an organism, for filtering by taxon. A root not listed
# (QTL, GWASResult, GeneticMarker) lives in genus mines only and is not filtered.
_ORGANISM_PATH = {"Gene": "Gene.organism", "GeneFunction": "GeneFunction.gene.organism",
                  "GeneFamily": "GeneFamily.genes.organism",
                  "ExpressionValue": "ExpressionValue.feature.organism"}


def _genus_of(name: str, exact: bool = False) -> str:
    """The genus a taxon name, abbreviation or gene-name prefix resolves to, or "".
    `exact` refuses an ambiguous match; suggestions are never used."""
    if tools_catalog.controller() is None or not name:
        return ""
    match = tools_catalog.resolve_taxon(name)
    if match.ok or (match.genus and not exact and not match.candidates):
        return match.genus
    if not exact and match.candidates:
        genera = {c.split()[0] for c in match.candidates}
        return genera.pop() if len(genera) == 1 else ""
    return ""


def _gene_genus(gene: str) -> str:
    """The genus a gene ID names: the abbreviation of a qualified ID, else a bare name's
    prefix when it is the species abbreviation (Glyma., Arahy.). "" otherwise."""
    gene = (gene or "").strip()
    match = _QUALIFIED.match(gene)
    prefix = match.group("abbrev") if match else gene.split(".", 1)[0] if "." in gene else ""
    return _genus_of(prefix, exact=True) if prefix else ""


def _has_class(mine: str, cls: str):
    """Does this mine's data model have class `cls`? True, False, or None when the model
    could not be read. Mines differ: legumemine has no QTL, GWASResult or GeneticMarker
    class, and only legumemine and glycinemine have GeneFunction."""
    def compute():
        try:
            url = f"{_service(mine)}/model?format=json"
            _validate_url(url)
            doc = _get(url, accept="application/json")
            classes = set(((doc or {}).get("model") or {}).get("classes") or {})
        except Exception:  # noqa: BLE001 - an unreadable model means "do not add this mine"
            return None, False
        return (classes or None), bool(classes)
    classes = _cached(("model", mine), compute)
    return None if classes is None else cls in classes


def _other_spellings(gene: str) -> list:
    """The same gene ID as another LIS mine may spell it: with the name's first token
    dropped (ArachisMine's ...ann1.GHMM2H for ...ann1.Arahy.GHMM2H), or added back from the
    species abbreviation. Only for fully qualified IDs, where the abbreviation is known."""
    match = _QUALIFIED.match(gene)
    if not match:
        return []
    head = f"{match.group('abbrev')}.{match.group('stem')}."
    name, token = match.group("name"), match.group("abbrev").capitalize()
    if name.startswith(token + "."):
        return [head + name[len(token) + 1:]]
    return [head + f"{token}.{name}"]


def _latin(taxon: str):
    """(genus, species) for a Latin genus or binomial ('Cajanus cajan'), else None."""
    bits = taxon.replace("_", " ").split()
    if not bits or not bits[0][:1].isupper() or not bits[0][1:].islower() or len(bits) > 2:
        return None
    return bits[0], (bits[1].lower() if len(bits) > 1 else "")


def _taxon_scope(taxon: str):
    """(genus, species, label, error, unresolved) for a taxon argument.

    A name the catalog resolves is used as resolved. A Latin genus or binomial it does
    not know is used as written, with `unresolved` holding the catalog's explanation: the
    catalog is incomplete, and a genus mine it does not list can still hold the taxon
    (the caller probes for it, and reports `unresolved` if nothing answers). Anything
    else, a common name above all, is never guessed at."""
    taxon = (taxon or "").strip()
    if not taxon:
        return "", "", "", None, ""
    unresolved = ""
    if tools_catalog.controller() is not None:
        match = tools_catalog.resolve_taxon(taxon)
        if match.ok:
            species = "" if match.species == "GENUS" else match.species
            return match.genus, species, f"{match.genus} {species}".strip(), None, ""
        if match.candidates:
            return "", "", "", f"error: taxon: {match.problem()}", ""
        unresolved = match.problem()
    latin = _latin(taxon)
    if latin is None:
        return "", "", "", ("error: taxon: " + (unresolved or
                            "no LIS catalog is loaded, so only a Latin genus or binomial "
                            "is accepted (e.g. 'Cicer arietinum').")), ""
    genus, species = latin
    return genus, species, f"{genus} {species}".strip(), None, unresolved


def _genus_mine(genus: str, root: str, trust_guess: bool = False) -> str:
    """The genus's own mine when it exists and its data model holds `root`, else "".

    Existence comes from the catalog, or a probe for a mine it does not list. With no
    catalog loaded nothing can be checked: `trust_guess` then takes '<genus>mine' on
    faith, which a tool whose data lives only in genus mines must, and a tool that has
    legumemine to answer need not."""
    if not genus or not genus[:1].isupper() or genus.isupper():
        return ""
    mine = genus.lower() + "mine"
    known = _known_mines()
    if known is None:
        return mine if trust_guess else ""
    if mine not in known and not _mine_exists(mine):
        return ""
    return mine if root == "Gene" or _has_class(mine, root) is not False else ""


def _plan(args, root, require_taxon=False, genus_hint=""):
    """Which mines answer, and the taxon filter. Returns (mines, filter_constraints,
    scope_label, error).

    An explicit 'mine' is queried alone. Otherwise legumemine answers whenever its data
    model has the root class, and so does the subject genus's own mine: the genus of
    'taxon', else of the gene ID (or `genus_hint`). 'taxon' also filters rows to that
    species or genus, in every mine."""
    explicit = (args.get("mine") or "").strip()
    genus, species, label, err, unresolved = _taxon_scope(
        args.get("taxon") or args.get("genus") or "")
    if err:
        return [], [], "", err
    path = _ORGANISM_PATH.get(root)
    cons = []
    if genus and path:
        cons.append((f"{path}.genus", "=", genus))
        if species:
            cons.append((f"{path}.species", "=", species))
    if explicit:
        return [explicit], cons, label, None
    if require_taxon and not genus:
        return [], [], "", ("error: missing 'taxon' — QTL/GWAS/marker data lives only in the "
                            "per-species mines (legumemine has none of it), so name the "
                            "species, e.g. taxon='Glycine max' or taxon='Phaseolus vulgaris'.")
    subject = genus or genus_hint or _gene_genus(args.get("gene") or "")
    mines = []
    if root == "Gene" or _has_class(MINE, root) is not False:
        mines.append(MINE)
    other = _genus_mine(subject, root, trust_guess=require_taxon)
    if other and other not in mines:
        mines.append(other)
    if unresolved and not other:
        # A name the catalog does not know, and no mine answers to its genus: the
        # problem is the name, so say that rather than "X has no InterMine".
        return [], [], "", f"error: taxon: {unresolved}"
    if not mines:
        taxon = (args.get("taxon") or "").strip()
        if taxon and require_taxon:
            return [], [], "", _no_mine(taxon, genus.lower() + "mine")
        return [], [], "", (f"error: no LIS mine holds {root} data"
                            + (f" for {label}" if label else "") + ".")
    return mines, cons, label, None


def _full_sort(view, sort):
    """The tool's sort, then every view column: a complete order, so the chunks of a
    large result neither repeat nor skip a row."""
    named = set((sort or "").split()[0::2])
    return " ".join(([sort] if sort else []) + [f"{p} asc" for p in view if p not in named])


class _Fetched:
    """One mine's whole answer to one query."""
    def __init__(self, mine):
        self.mine, self.rows, self.cols = mine, [], []
        self.total = None          # rows the mine has for the query; None if not counted
        self.error = ""            # the mine could not answer at all
        self.incomplete = ""       # some rows could not be fetched (kept rows are valid)
        self.respelled = ""        # the gene ID this mine answered under, if not as asked


def _fetch_all(mine, view, constraints, sort, gene=""):
    """Every row this mine has for the query, in FETCH_CHUNK requests run in parallel
    up to FETCH_MAX. A fully qualified gene ID the mine does not know as written is
    retried in its other spelling."""
    got = _Fetched(mine)
    order = _full_sort(view, sort)
    xml = _pathquery(view, constraints, order)
    rows, cols, err = _fetch(mine, xml, FETCH_CHUNK)
    if err:
        got.error = err
        return got
    if not rows and gene:
        for alt in _other_spellings(gene):
            alt_xml = _pathquery(view, [(p, op, alt if op == "LOOKUP" else v)
                                        for p, op, v in constraints], order)
            alt_rows, alt_cols, alt_err = _fetch(mine, alt_xml, FETCH_CHUNK)
            if alt_err:
                got.error = alt_err
                return got
            if alt_rows:
                rows, cols, xml, got.respelled = alt_rows, alt_cols, alt_xml, alt
                break
    got.rows, got.cols = list(rows), cols
    if len(rows) < FETCH_CHUNK:
        got.total = len(rows)
        return got
    total = _count(mine, xml)
    got.total = total
    if total is None:
        start = FETCH_CHUNK
        while start < FETCH_MAX:
            more, _c, err = _fetch(mine, xml, FETCH_CHUNK, start)
            if err:
                got.incomplete = f"rows from {start:,} on could not be fetched ({err})"
                return got
            got.rows += more
            if len(more) < FETCH_CHUNK:
                return got
            start += FETCH_CHUNK
        got.incomplete = (f"stopped at the {FETCH_MAX:,}-row fetch limit "
                          "(LEGUMISTA_MINE_FETCH_MAX), and the total could not be counted")
        return got
    limit = min(total, FETCH_MAX)
    starts = list(range(FETCH_CHUNK, limit, FETCH_CHUNK))
    with ThreadPoolExecutor(max_workers=max(1, FETCH_WORKERS)) as pool:
        chunks = list(pool.map(
            lambda s: _fetch(mine, xml, min(FETCH_CHUNK, limit - s), s), starts))
    for start, (more, _c, err) in zip(starts, chunks):
        if err:
            got.incomplete = f"rows from {start:,} on could not be fetched ({err})"
            return got
        got.rows += more
    if total > FETCH_MAX:
        got.incomplete = (f"fetched the first {FETCH_MAX:,} of {total:,} rows, the fetch "
                          "limit (LEGUMISTA_MINE_FETCH_MAX)")
    return got


def _spelling_tokens(genera) -> set:
    """The capitalized species abbreviations ('Arahy') that start gene names in these
    genera, the token a genus mine may drop."""
    ctl = tools_catalog.controller()
    if ctl is None:
        return set()
    tokens = set()
    for key, meta in (ctl.document.get("taxa") or {}).items():
        if isinstance(meta, dict) and key.split("/")[0] in genera and meta.get("abbrev"):
            tokens.add(str(meta["abbrev"]).capitalize())
    return tokens


def _merge(fetched, tokens, view, sort, key_cols=None):
    """One list from every mine's rows: rows equal once the dropped name token is
    restored count once, marked with every mine that holds them. Returns [(row, mines)]
    in the tool's sort order.

    `key_cols` picks the columns that identify a row (default: all of them). For a
    PathQuery every column is part of the answer, so rows that differ anywhere are two
    findings. A keyword-search hit is one object whatever the mine shows beside it: one
    mine names a gene where the other leaves the name blank."""
    strip = (re.compile(r"(?<!\w)(?:" + "|".join(map(re.escape, sorted(tokens))) + r")\.")
             if tokens else None)

    def key(row):
        cells = row if key_cols is None else [row[i] for i in key_cols if i < len(row)]
        return tuple(strip.sub("", c) if strip is not None and isinstance(c, str) else c
                     for c in cells)
    # A row's key carries its occurrence number within its mine, so rows a mine returns
    # twice (two records alike in every column shown) stay two, and match the other
    # mine's first and second copies, rather than collapsing into one.
    merged, order, shared = {}, [], {}
    for got in fetched:
        seen = {}
        for row in got.rows:
            base = key(row)
            seen[base] = seen.get(base, 0) + 1
            k = base + (seen[base],)
            if k in merged:
                held = merged[k][1] | {got.mine}
                merged[k] = (merged[k][0], shared.setdefault(held, held))
            else:
                held = frozenset((got.mine,))
                merged[k] = (row, shared.setdefault(held, held))
                order.append(k)

    def norm(v):
        return (v is None, (0, v) if isinstance(v, (int, float)) else (1, str(v)))
    items = [merged[k] for k in sorted(order, key=lambda k: tuple(norm(v) for v in k))]
    pairs = (sort or "").split()
    for path, direction in reversed(list(zip(pairs[0::2], pairs[1::2]))):
        if path in view:
            i = view.index(path)
            items.sort(key=lambda it, i=i: norm(it[0][i] if i < len(it[0]) else None),
                       reverse=direction.lower() == "desc")
    return items


def _breakdown(items, mines) -> list:
    """Rows per annotation, per mine, for a result keyed by fully qualified IDs: the
    complete picture a page of rows cannot give."""
    if not items:
        return []
    width = len(items[0][0])
    col = next((i for i in range(width)
                if sum(1 for row, _m in items if isinstance(row[i], str)
                       and _QUALIFIED.match(row[i])) * 2 > len(items)), None)
    if col is None:
        return []
    counts = {}
    for row, held in items:
        match = _QUALIFIED.match(str(row[col])) if isinstance(row[col], str) else None
        stem = f"{match.group('abbrev')}.{match.group('stem')}" if match else "(other)"
        for mine in held:
            counts.setdefault(stem, {}).setdefault(mine, 0)
            counts[stem][mine] += 1
    if len(counts) < 2 and len(mines) < 2:
        return []
    stems = sorted(counts, key=lambda s: (-sum(counts[s].values()), s))
    lines = [f"by annotation ({count_phrase(min(len(stems), 30), len(stems), 'annotation(s)')}):"]
    for stem in stems[:30]:
        lines.append(f"  {stem}: " + ", ".join(f"{m} {counts[stem].get(m, 0):,}"
                                               for m in mines))
    return lines


_MERGED: "OrderedDict" = OrderedDict()
_MERGED_LOCK = threading.Lock()
_PENDING: dict = {}
_POOL = ThreadPoolExecutor(max_workers=4, thread_name_prefix="legumista-mine")


def _store(key, compute):
    """Run a fetch-and-merge and cache what it found, bounded by MERGED_CACHE_ROWS
    rows. A result with any failed or incomplete mine is not cached, so a retry retries;
    it stays with its pending entry for the call that collects it."""
    value, cacheable = compute()
    if cacheable:
        with _MERGED_LOCK:
            _MERGED[key] = value
            while (len(_MERGED) > 1
                   and sum(len(v[1]) for v in _MERGED.values()) > MERGED_CACHE_ROWS):
                _MERGED.popitem(last=False)
            _PENDING.pop(key, None)
    return value


def _merged_result(key, compute):
    """The merged result for `key`, or None while its fetch is still running.

    A fetch runs in the background and the call waits up to TIME_BUDGET for it. One
    still running is left to finish: the next identical call joins it, or finds it done,
    rather than starting another."""
    with _MERGED_LOCK:
        if key in _MERGED:
            _MERGED.move_to_end(key)
            return _MERGED[key]
        future = _PENDING.get(key)
        if future is None:
            # A finished fetch whose caller never came back is dropped once there are
            # enough of them, so an abandoned large result does not stay in memory.
            done = [k for k, f in _PENDING.items() if f.done()]
            for stale in done[:max(0, len(done) - 8)]:
                _PENDING.pop(stale, None)
            future = _PENDING[key] = _POOL.submit(_store, key, compute)
    try:
        value = future.result(timeout=TIME_BUDGET)
    except FutureTimeout:
        return None
    except Exception:
        with _MERGED_LOCK:
            _PENDING.pop(key, None)
        raise
    with _MERGED_LOCK:
        if _PENDING.get(key) is future:
            _PENDING.pop(key, None)
    return value


def _execute(args, title, view, constraints, sort=None, assembly_col=1,
             subject_key="gene", subject_hint="a gene identifier such as 'Glyma.12G040000'",
             require_taxon=False, footer=None, on_empty=None, genus_hint="", holds=None,
             **_ignored):
    """Run one PathQuery against every mine that covers its subject (_plan), fetch each
    mine's whole result (_fetch_all), merge them (_merge), and render one page of the
    merged list with each mine's total, the overlap and a per-annotation breakdown.
    'offset' pages through the merged list, which is cached."""
    subject = (args.get(subject_key) or "").strip()
    if not subject:
        return f"error: missing {subject_key!r} — {subject_hint}."
    root = view[0].split(".", 1)[0]
    mines, taxon_cons, scope, err = _plan(args, root, require_taxon, genus_hint)
    if err:
        return err
    # `holds(mine)` names why a mine cannot answer this exact query (a family id it does
    # not have), so it is left out and the reply says why, rather than reporting its
    # certain zero as a finding.
    skipped = []
    if holds is not None and len(mines) > 1:
        for mine in list(mines):
            reason = holds(mine)
            if reason and len(mines) > 1:
                mines.remove(mine)
                skipped.append(f"{mine} was not queried: {reason}.")
    constraints = list(constraints) + taxon_cons
    if scope and scope not in title:
        title = f"{title} in {scope}"
    gene = subject if subject_key == "gene" else ""

    def compute():
        if len(mines) == 1:
            fetched = [_fetch_all(mines[0], view, constraints, sort, gene)]
        else:
            with ThreadPoolExecutor(max_workers=len(mines)) as pool:
                fetched = list(pool.map(
                    lambda m: _fetch_all(m, view, constraints, sort, gene), mines))
        ok = [f for f in fetched if not f.error]
        genera = {g for g in (_gene_genus(gene), (scope.split() or [""])[0]) if g}
        for f in ok:
            for row in f.rows[:50]:
                for cell in row:
                    if isinstance(cell, str) and _QUALIFIED.match(cell):
                        g = _gene_genus(cell)
                        if g:
                            genera.add(g)
        items = _merge(ok, _spelling_tokens(genera), view, sort)
        cacheable = all(not f.error and not f.incomplete for f in fetched)
        return (fetched, items), cacheable

    key = ("merged", tuple(mines), _pathquery(view, constraints, sort), gene)
    result = _merged_result(key, compute)
    if result is None:
        return (f"{title} — {subject} [mines: {', '.join(mines)}]: STILL FETCHING. This "
                f"result is large and the mines took longer than {TIME_BUDGET:g} s to "
                "return it. The fetch carries on here: make the same call again to get the "
                "result. This is not an empty result and not an error.")
    fetched, items = result
    return _reply(args, title, subject, fetched, items, assembly_col=assembly_col,
                  footer=footer, on_empty=on_empty, head_extra=skipped)


def _reply(args, title, subject, fetched, items, assembly_col=None, footer=None,
           on_empty=None, head_extra=(), tail_extra=()):
    """One page of a merged result: which mines answered, each one's total and the
    overlap, the rows with the mine(s) that hold each, and the breakdown and footers
    computed over the whole result. 'offset' and 'max_results' choose the page."""
    ok = [f for f in fetched if not f.error]
    failed = [f for f in fetched if f.error]
    if not ok:
        return failed[0].error if len(failed) == 1 else (
            "error: every mine failed — " + "; ".join(f"{f.mine}: {f.error}" for f in failed))
    labels = [f.mine for f in ok]
    multi = len(fetched) > 1

    head = []
    if failed:
        head.append("PARTIAL RESULTS — " + "; ".join(f"{f.mine} FAILED ({f.error})"
                                                      for f in failed)
                    + f". The rows below come only from {', '.join(labels)}; anything "
                    "the failed mine holds is unverified, not absent.")
    for f in ok:
        if f.respelled:
            head.append(f"{f.mine} spells {subject} as {f.respelled}; its rows are for that "
                        "ID.")
        if f.incomplete:
            head.append(f"INCOMPLETE — {f.mine}: {f.incomplete}.")
    if multi or failed:
        per = [f"{f.mine} {count_phrase(len(f.rows), f.total, 'row(s)')}" for f in ok]
        line = "sources — " + "; ".join(per)
        if len(ok) > 1:
            both = sum(1 for _r, held in items if len(held) == len(ok))
            only = [(f.mine, sum(1 for _r, held in items if held == {f.mine})) for f in ok]
            line += (f". Merged: {len(items):,} distinct row(s): {both:,} in both"
                     + "".join(f", {n:,} {m} only" for m, n in only))
        head.append(line)
    head += list(head_extra)

    if not items:
        if on_empty is not None:
            out = "\n".join(on_empty(f.mine, subject) for f in ok)
        else:
            out = (f"{title} — {subject} [{'mines' if multi else 'mine'}: "
                   f"{', '.join(labels)}]: no matches. The query was valid and returned zero "
                   "rows.")
        return _cap("\n".join(head + [out] + list(tail_extra)))

    offset = max(0, int(args.get("offset") or 0))
    size = max(1, min(int(args.get("max_results") or PAGE_MAX), PAGE_MAX))
    if offset >= len(items):
        return (f"{title} — {subject} [mines: {', '.join(labels)}]: no rows at "
                f"offset={offset}; the merged result has {len(items):,} row(s).")
    cols = list(next((f.cols for f in ok if f.cols), []))
    rows = [list(row) for row, _held in items]
    if len(ok) > 1:
        cols.append("in")
        page = [list(row) + ["both" if len(held) == len(ok) else ", ".join(sorted(held))]
                for row, held in items[offset:offset + size]]
    else:
        page = [list(row) for row, _held in items[offset:offset + size]]
    note = _assembly_note(rows, assembly_col) if assembly_col is not None else ""
    mine_label = ", ".join(labels)
    out = _render(title, mine_label, subject, page, cols, len(items), size, note,
                  capped=False, offset=offset, pageable=True, size_max=PAGE_MAX)
    if multi:
        out = out.replace(f"[mine: {mine_label}]", f"[mines: {mine_label}]", 1)
    first, _sep, rest = out.partition("\n")
    out = "\n".join([first] + head + [rest])
    extras = _breakdown(items, labels) if (multi or len(items) > len(page)) else []
    if footer:
        text = footer(rows)
        if text:
            extras.append(text)
    extras += list(tail_extra)
    if extras:
        out = _cap(out + "\n\n" + "\n".join(extras))
    return out


# --- the four tools -------------------------------------------------------------------
def _gene_proteins(args) -> str:
    return _execute(
        args, "Proteins",
        ["Gene.name", "Gene.assemblyVersion", "Gene.annotationVersion",
         "Gene.proteins.primaryIdentifier", "Gene.proteins.length",
         "Gene.proteins.molecularWeight"],
        _gene_constraints(args), on_empty=_explain_empty(args, "protein records"))


def _gene_families(args) -> str:
    return _execute(
        args, "Gene families",
        ["Gene.name", "Gene.assemblyVersion",
         "Gene.geneFamilyAssignments.geneFamily.primaryIdentifier",
         "Gene.geneFamilyAssignments.geneFamily.size",
         "Gene.geneFamilyAssignments.geneFamily.description"],
        _gene_constraints(args), on_empty=_explain_empty(args, "gene family assignments"))


def _gene_ontology(args) -> str:
    return _execute(
        args, "Ontology annotations",
        ["Gene.name", "Gene.assemblyVersion",
         "Gene.ontologyAnnotations.ontologyTerm.identifier",
         "Gene.ontologyAnnotations.ontologyTerm.name",
         "Gene.ontologyAnnotations.ontologyTerm.ontology.name"],
        _gene_constraints(args), on_empty=_explain_empty(args, "ontology annotations"))


# Column positions in the expression view below; fixed by that view.
_EXPR_SOURCE, _EXPR_UNIT = 3, 4


def _expression_footer(rows) -> str:
    """Name the studies (and their units) the rows came from. Values are only comparable
    within one study: studies differ in unit (TPM, FPKM, ...) and normalisation."""
    studies = {}
    for r in rows:
        if len(r) > _EXPR_UNIT:
            key = (str(r[_EXPR_SOURCE]), str(r[_EXPR_UNIT] or "unit not stated"))
            studies[key] = studies.get(key, 0) + 1
    if len(studies) <= 1:
        return ""
    units = sorted({u for _s, u in studies})
    lines = [f"NOTE: these rows mix {len(studies)} studies"
             + (f" in different units ({', '.join(units)})" if len(units) > 1 else "")
             + ". Rank and compare values within one study only; pass 'source' to see "
               "one study's samples."]
    lines += [f"  {s} [{u}]: {n} row(s)" for (s, u), n in sorted(studies.items())]
    return "\n".join(lines)


def _gene_expression(args) -> str:
    """Rooted at ExpressionValue: Gene has no expression collection, so the gene is
    reached through ExpressionValue.feature. Sorted high-to-low because one gene can carry
    hundreds of values and the top-expressing samples are the informative ones. The
    feature's primaryIdentifier (which names the assembly and annotation) and the study's
    unit are both in the view, because a bare gene name can match several assemblies and
    studies report in different units."""
    constraints = [("ExpressionValue.feature", "LOOKUP", (args.get("gene") or "").strip())]
    if (args.get("source") or "").strip():
        constraints.append(("ExpressionValue.sample.source.primaryIdentifier", "=",
                            args["source"].strip()))
    return _execute(
        args, "Expression values",
        ["ExpressionValue.feature.primaryIdentifier",
         "ExpressionValue.sample.primaryIdentifier",
         "ExpressionValue.sample.description",
         "ExpressionValue.sample.source.primaryIdentifier",
         "ExpressionValue.sample.source.unit", "ExpressionValue.value"],
        constraints, sort="ExpressionValue.value desc", assembly_col=None,
        footer=_expression_footer, on_empty=_explain_empty(args, "expression values"))


def _abbrev_for_taxon(ctl, taxon: str) -> str:
    """Datastore abbreviation ('glyma') for a taxon, or "" — used only to scope a symbol
    lookup, so a miss is harmless (the search just stays cross-species). Common names
    and abbreviations resolve through the catalog like everywhere else."""
    match = tools_catalog.resolve_taxon(taxon, ctl)
    if not (match.ok and match.species):
        return ""
    meta = (ctl.document.get("taxa") or {}).get(f"{match.genus}/{match.species}") or {}
    return str(meta.get("abbrev") or "")


def _catalog_symbol(args) -> str:
    """Curated symbol -> gene from the resident catalog, or "" on a miss.

    Checked BEFORE the mine because it is instant, offline, and has no ID-namespace
    problem: it stores the fully-qualified gene id, whereas the mine answer still has to
    be disambiguated across assemblies. The mine keeps the fall-through because its
    curation is broader (344 symbols here) and it carries every publication, not one DOI.
    """
    ctl = tools_catalog.controller()
    if ctl is None:
        return ""
    symbol = (args.get("symbol") or "").strip()
    taxon = (args.get("taxon") or args.get("genus") or "").strip()
    try:
        hits = ctl.resolve_symbol(symbol, _abbrev_for_taxon(ctl, taxon))
    except Exception:  # noqa: BLE001 - a catalog miss must never sink the mine query
        return ""
    if not hits:
        return ""
    lines = [f"Gene symbol — {symbol} [source: curated LIS catalog, NOT a mine query]",
             f"{len(hits)} curated record(s)  {tools_catalog.catalog_stamp(ctl)}",
             "  species | gene | doi | synopsis"]
    for hit in hits:
        lines.append("  " + " | ".join(str(hit.get(k) or "")
                                       for k in ("abbrev", "gene", "doi", "synopsis")))
    lines.append("This is the catalog's primary DOI for the symbol; the mine rows below "
                 "list every publication behind it.")
    return _cap("\n".join(lines))


def _gene_symbol(args) -> str:
    """Resolve a gene SYMBOL to its gene ID(s) — the lookup `lis_gene` cannot do.

    The catalog's curated symbols answer first, offline and fully qualified; the mines
    answer too, since their curation is broader and they carry every publication, not one
    DOI. `=` is case-insensitive in InterMine (GmNARK / gmnark / GMNARK all match). One row
    per publication, because the DOIs are the point: they hand the literature tools a
    citation for the functional claim."""
    symbol = (args.get("symbol") or "").strip()
    curated = _catalog_symbol(args) if symbol else ""
    hint = ""
    if curated:
        genera = {_gene_genus(line.split(" | ")[1].strip())
                  for line in curated.splitlines()[3:] if line.count(" | ") >= 3}
        hint = genera.pop() if len(genera) == 1 else ""
    mined = _execute(
        args, "Gene symbol",
        ["GeneFunction.symbol", "GeneFunction.symbolLong",
         "GeneFunction.gene.name", "GeneFunction.gene.primaryIdentifier",
         "GeneFunction.synopsis", "GeneFunction.publications.doi"],
        [("GeneFunction.symbol", "=", symbol)],
        assembly_col=None, subject_key="symbol",
        subject_hint="a gene symbol such as 'GmNARK' or 'PvSYMRK'", genus_hint=hint)
    if not curated:
        return mined
    return _cap(curated + "\n\n" + mined)


_FAMILY_CAVEAT = (
    "Family co-membership shows homology (shared ancestry), not orthology: a family holds "
    "paralogs too, so several members per species are expected — especially in "
    "polyploid lineages such as Glycine. To support an orthology claim, use the family's "
    "phylogeny or synteny (lis_synteny), and say which evidence you used.")


def _gene_family_members(args) -> str:
    """Members of a gene's family, from every mine that covers the subject.

    Accepts a gene (whose family is resolved first) or a family identifier directly.
    legumemine answers always, since its families span genera; with 'taxon' (or a gene
    naming one genus) that genus's own mine answers too, and 'taxon' keeps only that
    species' or genus's members.

    'assembly'/'annotation' pick the gene's copy, as in every other gene tool;
    'member_assembly'/'member_annotation' narrow the members listed. They are separate
    because they mean different genomes: a soybean gene's bean homologs need 'gnm4' for
    the gene and nothing at all for the bean. 'assembly' with 'family' has no gene to
    narrow, so it is refused rather than dropped without a word, as it once was."""
    family = (args.get("family") or "").strip()
    gene = (args.get("gene") or "").strip()
    _g, _s, taxon_label, terr, _u = _taxon_scope(args.get("taxon") or "")
    if terr:
        return terr
    gene_narrowing = [k for k in ("assembly", "annotation") if (args.get(k) or "").strip()]
    if family and gene_narrowing:
        return (f"error: {' and '.join(map(repr, gene_narrowing))} "
                f"{'pick' if len(gene_narrowing) > 1 else 'picks'} which copy of "
                "'gene' to look up, and with 'family' given no gene is looked up. To list "
                "only one genome's members, use 'member_assembly'/'member_annotation' "
                "(with 'taxon').")
    version_cons = [(f"Gene.{field}Version", "=", args[f"member_{field}"].strip())
                    for field in ("assembly", "annotation")
                    if (args.get(f"member_{field}") or "").strip()]
    versions = ", ".join(f"{field} {args[f'member_{field}'].strip()}"
                         for field in ("assembly", "annotation")
                         if (args.get(f"member_{field}") or "").strip())
    scope = ", ".join(x for x in (taxon_label, versions) if x)
    title = ("Gene family members (homologs; not an orthology call)"
             + (f" in {scope}" if scope else ""))
    if not family:
        if not gene:
            return ("error: provide 'gene' (e.g. 'Glyma.12G040000') or 'family' "
                    "(e.g. 'Legume.fam3.10524').")
        explicit = (args.get("mine") or "").strip()
        candidates = [explicit] if explicit else [MINE] + [
            m for m in [_genus_mine(_gene_genus(gene), "Gene")] if m and m != MINE]
        by_family, looked = {}, []
        for mine in candidates:
            for name in [gene] + _other_spellings(gene):
                xml = _pathquery(["Gene.primaryIdentifier",
                                  "Gene.geneFamilyAssignments.geneFamily.primaryIdentifier"],
                                 _gene_constraints({**args, "gene": name}))
                rows, _cols, err = _run(mine, xml, 20)
                if err:
                    return err
                for gene_id, fam in (r for r in rows or [] if len(r) > 1 and r[1]):
                    by_family.setdefault(fam, []).append(gene_id)
                looked.append(mine)
                if by_family:
                    break
            if by_family:
                break
        if not by_family:
            return (_render_empty(title, candidates[0], args, gene, "gene family assignment")
                    + "\n\n" + _FAMILY_CAVEAT)
        family = sorted(by_family, key=lambda f: (-len(by_family[f]), f))[0]
        others = [f for f in sorted(by_family) if f != family]
        prefix = (f"{gene} ({', '.join(sorted(by_family[family]))}) is in gene family "
                  f"{family}"
                  + (f"; other matches of this name are in: "
                     + ", ".join(f"{f} ({', '.join(by_family[f])})" for f in others)
                     if others else "") + "\n")
    else:
        prefix = ""
    constraints = ([("Gene.geneFamilyAssignments.geneFamily.primaryIdentifier", "=", family)]
                   + version_cons)

    def explain(mine_name, fam):
        overall = _count(mine_name, _pathquery(
            ["Gene.primaryIdentifier"],
            [("Gene.geneFamilyAssignments.geneFamily.primaryIdentifier", "=", fam)]))
        if overall == 0 or (overall is None and not scope):
            return (f"{title}: family {fam!r} has no members in {mine_name} — check the "
                    "family identifier.")
        if overall is None:
            return (f"{title}: no members of {fam} from {scope} in {mine_name}; "
                    "the family's overall size could not be checked, so confirm the "
                    "family identifier before treating this as a real zero.")
        hint = (" member_assembly/member_annotation are matched exactly ('gnm2', 'ann1'); "
                "drop them to see which versions the family has."
                if version_cons else "")
        return (f"{title}: family {fam} exists in {mine_name} with {overall:,} members, "
                f"none of them from {scope}. That is a real zero for this family "
                f"in this mine, not a failed lookup.{hint}")
    def holds(mine):
        n = _count(mine, _pathquery(["GeneFamily.primaryIdentifier"],
                                    [("GeneFamily.primaryIdentifier", "=", family)]))
        return f"it has no gene family {family}" if n == 0 else ""
    out = _execute(
        {**args, "family": family}, title,
        ["Gene.geneFamilyAssignments.geneFamily.primaryIdentifier", "Gene.primaryIdentifier",
         "Gene.organism.genus", "Gene.organism.species", "Gene.assemblyVersion"],
        constraints, sort="Gene.organism.genus asc Gene.primaryIdentifier asc",
        assembly_col=None, subject_key="family",
        subject_hint="a gene family identifier such as 'Legume.fam3.10524'",
        on_empty=explain, genus_hint=_gene_genus(gene), holds=holds)
    return prefix + out + "\n\n" + _FAMILY_CAVEAT


# --- search by description -------------------------------------------------------------
_SEARCH_CAVEAT = (
    "A description is automated text transferred from a homolog: a bracketed species "
    "('[Glycine max]') names that homolog's species, not the gene's (the genus/species "
    "columns). A match means the gene resembles one, not that its function is shown. Close "
    "paralogs share descriptions, so a description never settles which paralog a gene "
    "is. Only descriptions are "
    "searched: letters inside a gene ID say nothing about function.")
_SEARCH_MIN = 3


def _whole_word_note(rows, query, col):
    """Count rows where `query` occurs only inside a longer word. CONTAINS matches
    substrings, so 'CHS' finds 'Roseibium sp. TrichSKD4'."""
    word = re.compile(r"(?<![A-Za-z0-9])" + re.escape(query) + r"(?![A-Za-z0-9])", re.I)
    partial = [r for r in rows if len(r) > col and r[col] and not word.search(str(r[col]))]
    if not partial:
        return ""
    return (f"{len(partial)} of the {len(rows):,} rows contain {query!r} only inside a "
            f"longer word (e.g. {str(partial[0][0])}: {str(partial[0][col])[:80]!r}) — "
            "they do not match the term. Prefer full product names.")


def _gene_search(args) -> str:
    """Genes or gene families whose description contains a phrase: the way in for "find
    the chalcone synthases", which no identifier lookup can answer. Without it an agent
    grepped gene IDs for 'CHS' and reported a dynamin."""
    query = (args.get("query") or "").strip()
    if len(query) < _SEARCH_MIN:
        return (f"error: 'query' needs at least {_SEARCH_MIN} characters of description "
                "text, e.g. 'receptor kinase'.")
    kind = (args.get("search") or "genes").strip().lower()
    if kind not in ("genes", "families"):
        return "error: 'search' must be 'genes' or 'families'."
    if kind == "families":
        view = ["GeneFamily.primaryIdentifier", "GeneFamily.size",
                "GeneFamily.description"]
        constraints = [("GeneFamily.description", "CONTAINS", query)]
        # Largest first: the broad family is usually the one wanted. The identifier
        # breaks ties so paging is stable.
        sort, col = "GeneFamily.size desc GeneFamily.primaryIdentifier asc", 2
        nxt = ("A family's members in one annotation, with loci: lis_gene(genes="
               "{'family': <id>, 'collection': <annotation>}); across species: "
               "mine_gene_family_members.")
    else:
        view = ["Gene.primaryIdentifier", "Gene.organism.genus", "Gene.organism.species",
                "Gene.description"]
        constraints = [("Gene.description", "CONTAINS", query)]
        sort, col = "Gene.primaryIdentifier asc", 3
        nxt = ""
    title = f"Gene {'families' if kind == 'families' else 'search'} by description"

    def empty(mine_name, subject):
        return (f"{title}: no {kind} in {mine_name} with a description containing "
                f"{subject!r}. Descriptions spell out product names, not abbreviations: "
                "try the full name or a shorter phrase"
                + (", or search='families'" if kind == "genes" else "") + ".")

    def footer(rows):
        return "\n".join(x for x in (_whole_word_note(rows, query, col), _SEARCH_CAVEAT,
                                      nxt) if x)

    return _execute(args, title, view, constraints, sort=sort,
                    assembly_col=None, subject_key="query",
                    subject_hint="description text such as 'receptor kinase'",
                    footer=footer, on_empty=empty)


# --- a mine's own keyword search -----------------------------------------------------
SEARCH_PAGE_MAX = 100        # the service returns at most 100 results per request
_BRACKET_RE = re.compile(r"\[[A-Z][a-z]+ [a-z]+\]")
_BRACKET_NOTE = ("A bracketed species in a description ('[Glycine max]') names the "
                 "species of the homolog the description was transferred from, not this "
                 "gene's species: that is the organism column.")


KEYWORD_FETCH_MAX = int(os.environ.get("LEGUMISTA_MINE_SEARCH_MAX", "30000"))


def _search_request(mine, params):
    """One page of a mine's keyword search. Returns (doc, error)."""
    url = f"{_service(mine)}/search?" + urllib.parse.urlencode(params)
    try:
        _validate_url(url)
        doc = _get(url, accept="application/json")
    except Exception as e:  # noqa: BLE001
        return None, f"error: {mine} search failed: {type(e).__name__}: {e}"
    if not isinstance(doc, dict) or not doc.get("wasSuccessful", False):
        detail = (doc.get("error") if isinstance(doc, dict) else "") or "no reason given"
        return None, f"error: {mine} rejected the search: {detail}"
    return doc, ""


def _search_row(hit):
    f = hit.get("fields") or {}
    ident = f.get("primaryIdentifier") or f.get("identifier") or f.get("name") or ""
    name = f.get("symbol") or f.get("name") or ""
    organism_name = f.get("organism.name") or f.get("organism.shortName") or ""
    if f.get("strain.identifier"):
        organism_name += f" ({f['strain.identifier']})"
    version = ".".join(v for v in (f.get("assemblyVersion"), f.get("annotationVersion")) if v)
    desc = str(f.get("description") or "")
    return [hit.get("type", ""), ident, "" if name == ident else name, organism_name,
            version, desc[:160] + ("…" if len(desc) > 160 else "")]


def _search_all(mine, base, organisms):
    """Every hit of one mine's keyword search, over each organism filter in `organisms`
    ([None] for none), in pages of SEARCH_PAGE_MAX run in parallel up to
    KEYWORD_FETCH_MAX per filter. The counts by facet are summed over the filters."""
    got = _Fetched(mine)
    got.facets, got.total = {}, 0
    for organism in organisms:
        params = dict(base, size=str(SEARCH_PAGE_MAX), start="0")
        if organism:
            params["facet_organism.shortName"] = organism
        doc, err = _search_request(mine, params)
        if err:
            got.error = err
            return got
        total = int(doc.get("totalHits") or 0)
        got.total += total
        for facet, values in (doc.get("facets") or {}).items():
            bucket = got.facets.setdefault(facet, {})
            for value, n in (values or {}).items():
                bucket[value] = bucket.get(value, 0) + int(n)
        got.rows += [_search_row(h) for h in doc.get("results") or []]
        limit = min(total, KEYWORD_FETCH_MAX)
        starts = list(range(SEARCH_PAGE_MAX, limit, SEARCH_PAGE_MAX))
        with ThreadPoolExecutor(max_workers=max(1, FETCH_WORKERS)) as pool:
            pages = list(pool.map(
                lambda st: _search_request(mine, dict(params, start=str(st))), starts))
        for start, (page, perr) in zip(starts, pages):
            if perr:
                got.incomplete = f"results from {start:,} on could not be fetched ({perr})"
                return got
            got.rows += [_search_row(h) for h in page.get("results") or []]
        if total > KEYWORD_FETCH_MAX:
            got.incomplete = (f"fetched the first {KEYWORD_FETCH_MAX:,} of {total:,} results"
                              + (f" for {organism}" if organism else "")
                              + ", the search fetch limit (LEGUMISTA_MINE_SEARCH_MAX)")
    got.cols = ["type", "identifier", "name", "organism", "version", "description"]
    return got


def _genus_short_names(genus: str):
    """The organism names a keyword search counts by ('A. hypogaea') for a genus's
    species, from the catalog, or None without one. Matching on the initial alone would
    take other genera's species: legumemine counts 'A. evenia' (Aeschynomene) beside the
    Arachis species."""
    ctl = tools_catalog.controller()
    if ctl is None:
        return None
    return sorted({f"{genus[0]}. {key.split('/')[1]}"
                   for key in (ctl.document.get("taxa") or {})
                   if key.split("/")[0] == genus and "/" in key})


def _keyword_search(args) -> str:
    """What a mine's search box does: InterMine's keyword search over every indexed class
    and field, with the counts by category and organism its results page shows; run in
    legumemine and, for one species or genus, that genus's own mine, each in full, then
    merged.

    Not mine_gene_search's substring match on descriptions. Keyword search matches
    whole words, so a count can differ from the substring count for the same phrase;
    the reply says which one ran. The search can filter only by organism, so a genus
    in legumemine is searched once per species of it."""
    query = (args.get("query") or "").strip()
    if not query:
        return "error: missing 'query' — keywords, a quoted phrase, OR, AND NOT, or dros*."
    genus, species, label, err, unresolved = _taxon_scope(args.get("taxon") or "")
    if err:
        return err
    explicit = (args.get("mine") or "").strip()
    if explicit:
        mines = [explicit]
    else:
        mines = [MINE]
        other = _genus_mine(genus, "Gene") if genus else ""
        if other:
            mines.append(other)
        elif unresolved:
            return f"error: taxon: {unresolved}"
    category = (args.get("category") or "").strip()
    base = {"q": query}
    if category:
        base["facet_Category"] = category
    notes = []

    def organisms_for(mine):
        if not genus:
            return [None]
        if species:
            return [f"{genus[0]}. {species}"]
        if mine == genus.lower() + "mine":
            return [None]
        names = _genus_short_names(genus)
        if names is None:
            notes.append(f"{mine}'s results are not narrowed to {genus}: without a catalog "
                         "its species cannot be told from other genera's.")
            return [None]
        return names
    scope = ", ".join(x for x in (f"category {category}" if category else "", label) if x)
    title = f"Keyword search{' (' + scope + ')' if scope else ''}"

    def compute():
        plans = [(m, organisms_for(m)) for m in mines]
        with ThreadPoolExecutor(max_workers=len(plans)) as pool:
            fetched = list(pool.map(lambda p: _search_all(p[0], base, p[1]), plans))
        ok = [f for f in fetched if not f.error]
        tokens = _spelling_tokens({genus} if genus else set())
        items = _merge(ok, tokens, ["type", "identifier"], None, key_cols=(0, 1))
        return (fetched, items), all(not f.error and not f.incomplete for f in fetched)

    key = ("search", tuple(mines), query, category, genus, species)
    result = _merged_result(key, compute)
    if result is None:
        return (f"{title} — {query} [mines: {', '.join(mines)}]: STILL FETCHING. This "
                f"result is large and the mines took longer than {TIME_BUDGET:g} s to "
                "return it. The fetch carries on here: make the same call again to get the "
                "result. This is not an empty result and not an error.")
    fetched, items = result
    ok = [f for f in fetched if not f.error]
    counts = []
    for facet, name in (("Category", "by category"), ("organism.shortName", "by organism")):
        per = []
        for f in ok:
            values = (getattr(f, "facets", {}) or {}).get(facet) or {}
            if values:
                per.append((f"{f.mine}: " if len(ok) > 1 else "") + ", ".join(
                    f"{k} {v:,}" for k, v in sorted(values.items(), key=lambda kv: -kv[1])))
        if per:
            counts.append(f"{name} — " + "; ".join(per))
    tail = counts + ["Whole words in any indexed field: 'kinase-like' is another word, so "
                     "this can count fewer than mine_gene_search's substring match."]
    if any(_BRACKET_RE.search(str(row[5])) for row, _held in items):
        tail.append(_BRACKET_NOTE)
    return _reply(args, title, query, fetched, items, head_extra=notes, tail_extra=tail)


def _trait_qtls(args) -> str:
    """Trait -> QTLs. Per-species mine only (see _resolve_mine)."""
    return _execute(
        args, "QTLs",
        ["QTL.trait.name", "QTL.name", "QTL.linkageGroup.name", "QTL.lod",
         "QTL.markerR2", "QTL.qtlStudy.primaryIdentifier"],
        [("QTL.trait.name", "CONTAINS", (args.get("trait") or "").strip())],
        assembly_col=None, subject_key="trait", require_taxon=True,
        subject_hint="a trait name or fragment such as 'seed protein'")


def _catalog_gwas_ids():
    """Ids of every gwas collection in the catalog, or None when none is loaded."""
    with _MINES_LOCK:
        if "gwas" in _KNOWN:
            return _KNOWN["gwas"]
    ctl = tools_catalog.controller()
    ids = None
    if ctl is not None:
        ids = {c["id"] for c in ctl.collections if c.get("type") == "gwas" and c.get("id")}
    with _MINES_LOCK:
        _KNOWN["gwas"] = ids
    return ids


def _gwas_bridge(rows) -> str:
    """Tell the caller the study identifiers they are holding are lis_files handles.

    The mine's `gwas.primaryIdentifier` and the Data Store's gwas/ collection names are
    the same strings, so the association result and the underlying data are one call
    apart — but only if someone says so. Where the catalog is loaded each identifier is
    CHECKED against it, so a present one is stated as fact and an absent one is not
    claimed at all."""
    # Column 3 is GWASResult.gwas.primaryIdentifier — fixed by the view above.
    studies = sorted({str(r[3]) for r in rows if len(r) > 3 and r[3]})
    if not studies:
        return ""
    known = _catalog_gwas_ids()
    if known is None:
        return ("The study identifier(s) above (" + ", ".join(studies[:5])
                + (f", +{len(studies) - 5} more" if len(studies) > 5 else "") + ") are "
                "formatted like LIS Data Store gwas/ collection names, so "
                f"lis_files(collection={studies[0]!r}) may reach the underlying data. "
                "Not verified — no catalog is loaded.")
    present = [x for x in studies if x in known]
    missing = [x for x in studies if x not in known]
    lines = []
    if present:
        lines.append("These studies ARE LIS Data Store gwas/ collections — pass the "
                     "identifier to lis_files for the underlying data:")
        lines += [f"  lis_files(collection={x!r})" for x in present[:10]]
        if len(present) > 10:
            lines.append(f"  ... and {len(present) - 10} more")
    if missing:
        lines.append("No gwas/ collection in the catalog is named "
                     + ", ".join(repr(x) for x in missing[:5])
                     + (f" (+{len(missing) - 5} more)" if len(missing) > 5 else "")
                     + " — the mine holds the result, the Data Store does not hold the "
                       "study files.")
    return "\n".join(lines)


def _trait_gwas(args) -> str:
    """Trait -> GWAS associations, most significant first. Per-species mine only.

    The gwas identifiers match the Data Store's gwas/ collection names exactly (e.g.
    mixed.gwas.Bandillo_Jarquin_2015), so lis_files can serve the underlying data —
    `_gwas_bridge` puts that in the output, where the caller is holding the values."""
    return _execute(
        args, "GWAS associations",
        ["GWASResult.trait.name", "GWASResult.markerName", "GWASResult.pValue",
         "GWASResult.gwas.primaryIdentifier"],
        [("GWASResult.trait.name", "CONTAINS", (args.get("trait") or "").strip())],
        sort="GWASResult.pValue asc", assembly_col=None, subject_key="trait",
        require_taxon=True, footer=_gwas_bridge,
        subject_hint="a trait name or fragment such as 'seed protein'")


def _marker_position(args) -> str:
    """Marker -> physical position on every assembly that carries it.

    Multi-assembly output is the point: a breeder needs the coordinate on THEIR reference,
    and one marker sits at a different position in each (gnm1/gnm2/gnm4 all differ)."""
    return _execute(
        args, "Marker positions",
        ["GeneticMarker.name", "GeneticMarker.type",
         "GeneticMarker.chromosome.primaryIdentifier",
         "GeneticMarker.chromosomeLocation.start", "GeneticMarker.chromosomeLocation.end"],
        [("GeneticMarker", "LOOKUP", (args.get("marker") or "").strip())],
        assembly_col=None, subject_key="marker", require_taxon=True,
        subject_hint="a marker name such as 'ss715614263'")


# --- registry -------------------------------------------------------------------------
# Every mine tool answers from every mine that covers its subject: legumemine, and the
# subject genus's own mine where one exists and holds the data (see _plan). 'taxon' names
# the species or genus asked about: it filters the rows and brings in its genus mine.
# 'mine' queries one mine alone. Results are fetched whole, merged with each row's
# source, and paged with 'offset'.
_MINE_ARGS = {
    "taxon": {"type": "string",
              "description": "The species or genus asked about: Latin name, common name or "
                             "abbreviation ('Arachis hypogaea', 'peanut', 'arahy'). Keeps "
                             "only its rows, and adds its genus's own mine to legumemine."},
    "mine": {"type": "string",
             "description": "Query only this mine ('legumemine', 'arachismine', …)."},
}
_PAGE_ARGS = {
    "max_results": {"type": "integer",
                    "description": "Rows per page, 1-500 (default as many as fit)."},
    "offset": {"type": "integer",
               "description": "Rows of the merged result to skip; the reply names the "
                              "offset that continues it."},
}
_GENE_ARG = {
    "gene": {"type": "string",
             "description": "Gene ID, bare ('Glyma.12G040000') or fully qualified."},
    **_MINE_ARGS,
    **_PAGE_ARGS,
}
_ASSEMBLY_ARGS = {
    "assembly": {"type": "string",
                 "description": "Only the gene's copy on this assembly, e.g. 'gnm4': a bare "
                                "name matches every assembly that has it."},
    "annotation": {"type": "string",
                   "description": "Only the gene's copy in this annotation, e.g. 'ann1'."},
}


_TAXON_ARG = {
    "taxon": {"type": "string",
              "description": "The species or genus asked about: Latin name, common name or "
                             "abbreviation ('Glycine max', 'soybean', 'glyma'). Its genus "
                             "mine answers; this data is in no other."},
    "mine": {"type": "string", "description": "Query only this mine ('glycinemine')."},
    **_PAGE_ARGS,
}


def _mk(name, description, props, sync_fn, required=("gene",)):
    async def run(a, _f=sync_fn):
        return await asyncio.to_thread(_f, a)
    return Tool(name=name, description=description, read_only=True, run=run,
                parameters={"type": "object", "properties": props,
                            "required": list(required), "additionalProperties": False})


def mine_tools() -> list:
    """Read-only, per-question tools over a LIS InterMine instance."""
    return [
        _mk("mine_gene_proteins",
            "A gene's protein records: identifier, length (aa), molecular weight. For "
            "the sequence itself, use lis_gene's fasta_fetch call.",
            {**_GENE_ARG, **_ASSEMBLY_ARGS}, _gene_proteins),
        _mk("mine_gene_families",
            "A gene's family assignments (legume.fam3, legfed_v1_0), with family size "
            "and description. Families hold paralogs: membership is homology, not "
            "orthology.",
            {**_GENE_ARG, **_ASSEMBLY_ARGS}, _gene_families),
        _mk("mine_gene_ontology",
            "A gene's ontology annotations (GO and others): term ID, name, ontology.",
            {**_GENE_ARG, **_ASSEMBLY_ARGS}, _gene_ontology),
        _mk("mine_gene_expression",
            "A gene's expression values across samples, highest first: sample, its "
            "description, the study, and that study's unit. Compare values only within "
            "one study ('source' restricts to one). Capped, with the total.",
            {**_GENE_ARG,
             "source": {"type": "string",
                        "description": "Restrict to one expression study (the source "
                                       "identifier shown in a previous result)."}},
            _gene_expression),
        _mk("mine_gene_symbol",
            "A gene symbol (GmNARK, PvSYMRK) to its gene IDs, full name, synopsis and "
            "DOIs: the catalog's curated record when it has the symbol, and every mine "
            "that holds symbols for the genus, merged. The other gene tools match "
            "identifiers, not symbols.",
            {"symbol": {"type": "string",
                        "description": "Gene symbol, e.g. 'GmNARK'. Case-insensitive, exact."},
             **_MINE_ARGS,
             **_PAGE_ARGS},
            _gene_symbol, required=("symbol",)),
        _mk("mine_gene_family_members",
            "A gene family's members across legumes, given a gene or a family, from "
            "legumemine and, for one genus, that genus's own mine, merged. taxon keeps "
            "one species' or genus's members, member_assembly/member_annotation one "
            "genome; assembly/annotation pick the gene's own copy. Members are "
            "homologs, paralogs included: not an orthology call.",
            {"gene": {"type": "string", "description": "Gene identifier, e.g. 'Glyma.12G040000'."},
             "family": {"type": "string",
                        "description": "Gene family identifier, e.g. 'Legume.fam3.10524'. "
                                       "Skips the gene->family step."},
             **_MINE_ARGS,
             **_PAGE_ARGS,
             "member_assembly": {"type": "string",
                                 "description": "Only list members on this assembly "
                                                "version, e.g. 'gnm2'. Versions repeat "
                                                "across species, so pair it with "
                                                "'taxon'."},
             "member_annotation": {"type": "string",
                                   "description": "Only list members from this "
                                                  "annotation version, e.g. 'ann1'."},
             **_ASSEMBLY_ARGS},
            _gene_family_members, required=()),
        _mk("mine_gene_search",
            "Genes, or with search='families' gene families, whose description "
            "contains a phrase as a substring, so 'receptor kinase' also takes "
            "'receptor kinase-like': the way from a function to genes. Answers from "
            "legumemine and, for one genus, its own mine, merged. Descriptions only, "
            "never IDs. Descriptions are transferred from homologs, so a hit is a "
            "candidate, and close paralogs share them.",
            {"query": {"type": "string",
                       "description": "Description text, matched case-insensitively as a "
                                      "substring, e.g. 'receptor kinase'. Use full "
                                      "product names, not abbreviations."},
             "search": {"type": "string", "enum": ["genes", "families"],
                        "description": "'genes' (default) or 'families'."},
             **_MINE_ARGS,
             **_PAGE_ARGS},
            _gene_search, required=("query",)),
        _mk("mine_search",
            "A mine's own keyword search, as its search box runs it: whole words in "
            "every indexed class and field (genes, proteins, families, QTL, ontology "
            "terms…), so 'receptor kinase' does not take 'receptor kinase-like'. "
            "Runs in legumemine and, for one species or genus, its own mine, each in "
            "full, merged; gives each mine's total and counts by category and organism, "
            "as a mine's results page does. category narrows to one category.",
            {"query": {"type": "string",
                       "description": "Keywords: a quoted phrase, OR, AND NOT, or a "
                                      "trailing * (\"receptor kinase\")."},
             **_MINE_ARGS,
             "category": {"type": "string",
                          "description": "One category from the counts, e.g. 'Gene', "
                                         "'Protein', 'GeneFamily', 'QTL'."},
             **_PAGE_ARGS},
            _keyword_search, required=("query",)),
        _mk("mine_trait_qtls",
            "QTLs mapped for a trait: QTL, linkage group, LOD, marker R2, study. Needs "
            "taxon: QTL data lives only in genus mines.",
            {"trait": {"type": "string",
                       "description": "Trait name or fragment, e.g. 'seed protein', "
                                      "'days to maturity'. Substring match."},
             **_TAXON_ARG}, _trait_qtls, required=("trait",)),
        _mk("mine_trait_gwas",
            "GWAS associations for a trait, most significant first: marker, p-value, "
            "study. Study ids match Data Store gwas collections, which lis_files "
            "reads. Needs taxon.",
            {"trait": {"type": "string",
                       "description": "Trait name or fragment, e.g. 'seed protein'."},
             **_TAXON_ARG}, _trait_gwas, required=("trait",)),
        _mk("mine_marker_position",
            "A marker's physical position on each assembly that carries it; positions "
            "differ between assemblies. Needs taxon.",
            {"marker": {"type": "string",
                        "description": "Marker name, e.g. 'ss715614263'."},
             **_TAXON_ARG}, _marker_position, required=("marker",)),
    ]
