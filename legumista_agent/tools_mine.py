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
4. **Unbounded result sets.** One gene has 639 expression values, so queries are capped and
   the total is reported via a `format=count` pre-flight rather than truncating silently.
5. **A mine that does not exist.** Only ~10 genera have a mine; the store holds ~48 species
   that have none. Routing them by name produced a 404 the model reads as an outage, so
   the catalog's published mines are checked BEFORE any request (see `_known_mines`).

Successful responses are memoized for the life of the process (`_cached`): an agent working
one gene re-issues the same PathQuery — `legumemine_gene_family_members` re-derives the
family `legumemine_gene_families` just fetched. Errors are never cached, so a retry retries.

Mine selection: `MINE` (env `LEGUMISTA_LIS_MINE`) with a per-call `mine` override. The
default is `legumemine`, the pan-legume mine — it spans 55 organisms, so its gene families
are cross-species (Legume.fam3.10524 has 347 members there vs 195 in the genus-scoped
glycinemine). The PathQueries are mine-agnostic, so a per-genus mine ('glycinemine',
'phaseolusmine', ...) is a one-argument switch when a genus-scoped answer is wanted. Every
result names the mine that answered, so an agent can never misattribute.
"""
import asyncio
import os
import re
import threading
import urllib.parse
from xml.sax.saxutils import quoteattr

from . import tools_catalog
from .results import count_phrase
from .tool import Tool
from .tools_native import MAX_CHARS, _cap, _get, _validate_url

MINES_BASE = os.environ.get("LEGUMISTA_LIS_MINES_BASE",
                            "https://mines.legumeinfo.org").rstrip("/")
MINE = os.environ.get("LEGUMISTA_LIS_MINE", "legumemine")
MAX_ROWS = int(os.environ.get("LEGUMISTA_MINE_MAX_ROWS", "50"))
# Pre-flight counts cost a round trip; skip them for queries that cannot run away.
COUNT_THRESHOLD = int(os.environ.get("LEGUMISTA_MINE_COUNT_THRESHOLD", "200"))
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
# a gene's family members re-derives the gene->family mapping `legumemine_gene_families`
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
            pageable=False):
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
            if not cut:
                shown += " (or raise 'max_results', up to 500)"
        elif cut:
            shown += (f" — the reply's size limit stopped the list at {len(body)} of the "
                      f"{len(rows)} rows fetched; narrow the query to see the rest")
        else:
            shown += f" — capped at {size}; raise 'max_results' (up to 500) to see more"
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


def _execute(args, title, view, constraints, sort=None, assembly_col=1,
             subject_key="gene", subject_hint="a gene identifier such as 'Glyma.12G040000'",
             require_taxon=False, footer=None, on_empty=None, pageable=False):
    """Run one PathQuery and render it. A `pageable` tool honours args['offset'], so
    `sort` must then order the rows completely: a tie lets a row move between pages."""
    mine, mine_err = _resolve_mine(args, require_taxon)
    if mine_err:
        return mine_err
    subject = (args.get(subject_key) or "").strip()
    if not subject:
        return f"error: missing {subject_key!r} — {subject_hint}."
    size = max(1, min(int(args.get("max_results") or MAX_ROWS), 500))
    offset = max(0, int(args.get("offset") or 0)) if pageable else 0
    xml = _pathquery(view, constraints, sort)
    rows, cols, err = _run(mine, xml, size, offset)
    if err:
        return err
    if not rows and offset:
        total = _count(mine, xml)
        return (f"{title} — {subject} [mine: {mine}]: no rows at offset={offset}; "
                + (f"the query has {total:,} row(s) in all." if total is not None else
                   "the query's total could not be checked."))
    if not rows and on_empty is not None:
        # The caller explains its own zero (see _render_empty) instead of the generic text.
        return on_empty(mine, subject)
    capped = len(rows) >= size
    total = (_count(mine, xml) if len(rows) >= min(size, COUNT_THRESHOLD)
             else offset + len(rows))
    note = _assembly_note(rows, assembly_col) if assembly_col is not None else ""
    out = _render(title, mine, subject, rows, cols, total, size, note, capped=capped,
                  offset=offset, pageable=pageable)
    # A footer names the next tool the rows unlock. It goes in the OUTPUT rather than a
    # docstring because the hand-off is only discoverable once you are holding the values.
    if footer and rows:
        extra = footer(rows)
        if extra:
            out = _cap(out + "\n\n" + extra)
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
    lines.append("The gene id is fully qualified — pass it straight to lis_gene, "
                 "legumemine_gene_families or fasta_fetch. For every publication behind "
                 "the symbol rather than the primary DOI, re-run with an explicit 'mine'.")
    return _cap("\n".join(lines))


def _gene_symbol(args) -> str:
    """Resolve a gene SYMBOL to its gene ID(s) — the lookup `lis_gene` cannot do.

    The catalog is consulted first (see `_catalog_symbol`); the mine query below is the
    fall-through. `=` is case-insensitive in InterMine (GmNARK / gmnark / GMNARK all
    match), so exact matching is safe here. One row per publication, because the DOIs are
    the point: they hand the literature tools a citation for the functional claim."""
    if (args.get("symbol") or "").strip():
        curated = _catalog_symbol(args)
        if curated:
            return curated
    return _execute(
        args, "Gene symbol",
        ["GeneFunction.symbol", "GeneFunction.symbolLong",
         "GeneFunction.gene.name", "GeneFunction.gene.primaryIdentifier",
         "GeneFunction.synopsis", "GeneFunction.publications.doi"],
        [("GeneFunction.symbol", "=", (args.get("symbol") or "").strip())],
        assembly_col=None, subject_key="symbol",
        subject_hint="a gene symbol such as 'GmNARK' or 'PvSYMRK'")


_FAMILY_CAVEAT = (
    "Family co-membership shows homology (shared ancestry), not orthology: a family holds "
    "paralogs too, so several members per species are expected — especially in "
    "polyploid lineages such as Glycine. To support an orthology claim, use the family's "
    "phylogeny or synteny (lis_synteny), and say which evidence you used.")


def _target_constraints(target: str):
    """(constraints, label, error) restricting family members to one taxon."""
    target = (target or "").strip()
    if not target:
        return [], "", None
    if tools_catalog.controller() is None:
        # Without the catalog, names cannot be resolved: accept only a Latin genus or
        # binomial, so "chickpea" is not silently queried as a genus called "Chickpea".
        bits = target.replace("_", " ").split()
        if not bits[0][:1].isupper() or len(bits) > 2:
            return None, "", ("error: target_taxon: no LIS catalog is loaded, so only a "
                              "Latin genus or binomial is accepted (e.g. 'Cicer arietinum').")
        genus, species = bits[0], (bits[1].lower() if len(bits) > 1 else "")
    else:
        match = tools_catalog.resolve_taxon(target)
        if not match.ok:
            return None, "", f"error: target_taxon: {match.problem()}"
        genus, species = match.genus, match.species
    cons = [("Gene.organism.genus", "=", genus)]
    if species and species != "GENUS":
        cons.append(("Gene.organism.species", "=", species))
    return cons, f"{genus} {species}".strip(), None


def _gene_family_members(args) -> str:
    """Members of a gene's family, optionally restricted to one target species.

    Accepts a gene (whose family is resolved first) or a family identifier directly.
    Always queries the pan-legume mine unless 'mine' is given: families are cross-species
    there (Legume.fam3.10524 has 347 members) but genus-scoped in a per-genus mine (195
    in glycinemine), where another genus's gene is simply absent.

    'assembly' and 'annotation' narrow the MEMBERS listed, not the gene: they once
    narrowed only the gene->family step, so with 'family' given they were dropped
    without a word and the agent read the whole family as the narrowed list."""
    mine = (args.get("mine") or "").strip() or MINE
    family = (args.get("family") or "").strip()
    gene = (args.get("gene") or "").strip()
    target_cons, target_label, terr = _target_constraints(args.get("target_taxon"))
    if terr:
        return terr
    version_cons = [(f"Gene.{field}Version", "=", args[key].strip())
                    for key, field in (("assembly", "assembly"), ("annotation", "annotation"))
                    if (args.get(key) or "").strip()]
    scope = ", ".join(([target_label] if target_label else [])
                      + [f"{key} {args[key].strip()}" for key in ("assembly", "annotation")
                         if (args.get(key) or "").strip()])
    title = ("Gene family members (homologs; not an orthology call)"
             + (f" in {scope}" if scope else ""))
    if not family:
        if not gene:
            return ("error: provide 'gene' (e.g. 'Glyma.12G040000') or 'family' "
                    "(e.g. 'Legume.fam3.10524').")
        # Step 1: gene -> family, per assembly (a bare name can match several).
        xml = _pathquery(["Gene.primaryIdentifier",
                          "Gene.geneFamilyAssignments.geneFamily.primaryIdentifier"],
                         _gene_constraints({"gene": gene}))
        rows, _cols, err = _run(mine, xml, 20)
        if err:
            return err
        by_family = {}
        for gene_id, fam in (r for r in rows or [] if len(r) > 1 and r[1]):
            by_family.setdefault(fam, []).append(gene_id)
        if not by_family:
            return (_render_empty(title, mine, {"gene": gene}, gene,
                                  "gene family assignment")
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
                   + target_cons + version_cons)
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
        hint = (" Assembly and annotation versions are matched exactly ('gnm2', 'ann1'); "
                "drop them to see which versions the family has."
                if version_cons else "")
        return (f"{title}: family {fam} exists in {mine_name} with {overall:,} members, "
                f"none of them from {scope}. That is a real zero for this family "
                f"in this mine, not a failed lookup.{hint}")
    out = _execute(
        {**args, "family": family, "mine": mine}, title,
        ["Gene.geneFamilyAssignments.geneFamily.primaryIdentifier", "Gene.primaryIdentifier",
         "Gene.organism.genus", "Gene.organism.species", "Gene.assemblyVersion"],
        constraints, sort="Gene.organism.genus asc Gene.primaryIdentifier asc",
        assembly_col=None, subject_key="family",
        subject_hint="a gene family identifier such as 'Legume.fam3.10524'",
        on_empty=explain, pageable=True)
    return prefix + out + "\n\n" + _FAMILY_CAVEAT


# --- search by description -------------------------------------------------------------
_SEARCH_CAVEAT = (
    "A description is automated text transferred from a homolog (often ending '[Glycine "
    "max]'): a match means the gene resembles one, not that its function is shown. Close "
    "paralogs share descriptions — peanut's stilbene synthases read 'chalcone synthase' "
    "— so a description never settles which paralog a gene is. Only descriptions are "
    "searched: letters inside a gene ID say nothing about function.")
_SEARCH_MIN = 3


def _whole_word_note(rows, query, col):
    """Count rows where `query` occurs only inside a longer word. CONTAINS matches
    substrings, so 'CHS' finds 'Roseibium sp. TrichSKD4'."""
    word = re.compile(r"(?<![A-Za-z0-9])" + re.escape(query) + r"(?![A-Za-z0-9])", re.I)
    partial = [r for r in rows if len(r) > col and r[col] and not word.search(str(r[col]))]
    if not partial:
        return ""
    return (f"{len(partial)} of the {len(rows)} rows shown contain {query!r} only inside a "
            f"longer word (e.g. {str(partial[0][0])}: {str(partial[0][col])[:80]!r}) — "
            "they do not match the term. Prefer full product names.")


def _gene_search(args) -> str:
    """Genes or gene families whose description contains a phrase: the way in for "find
    the chalcone synthases", which no identifier lookup can answer. Without it an agent
    grepped gene IDs for 'CHS' and reported a dynamin."""
    query = (args.get("query") or "").strip()
    if len(query) < _SEARCH_MIN:
        return (f"error: 'query' needs at least {_SEARCH_MIN} characters of description "
                "text, e.g. 'chalcone synthase'.")
    kind = (args.get("search") or "genes").strip().lower()
    if kind not in ("genes", "families"):
        return "error: 'search' must be 'genes' or 'families'."
    mine = (args.get("mine") or "").strip() or MINE
    if kind == "families":
        if (args.get("target_taxon") or "").strip():
            return ("error: a gene family spans species, so 'target_taxon' does not apply "
                    "to search='families'. Find the family here, then list one species' "
                    "members with legumemine_gene_family_members(target_taxon=...), or "
                    "one annotation's with lis_gene(genes={'family': ..., 'collection': "
                    "...}).")
        view = ["GeneFamily.primaryIdentifier", "GeneFamily.size",
                "GeneFamily.description"]
        constraints = [("GeneFamily.description", "CONTAINS", query)]
        # Largest first: the broad family is usually the one wanted. The identifier
        # breaks ties so paging is stable.
        sort, col, scope = "GeneFamily.size desc GeneFamily.primaryIdentifier asc", 2, ""
        nxt = ("Next: lis_gene(genes={'family': <id>, 'collection': <annotation>}) lists a "
               "family's members in one annotation with loci and descriptions; "
               "legumemine_gene_family_members lists them across species.")
    else:
        target_cons, scope, terr = _target_constraints(args.get("target_taxon"))
        if terr:
            return terr
        view = ["Gene.primaryIdentifier", "Gene.organism.genus", "Gene.organism.species",
                "Gene.description"]
        constraints = [("Gene.description", "CONTAINS", query)] + target_cons
        sort, col = "Gene.primaryIdentifier asc", 3
        nxt = ("Next: lis_gene(gene=<id>) for a gene's locus and full description; "
               "legumemine_gene_families for its family. To list every member of a family "
               "in one annotation, search='families' or lis_gene with a family selector.")
    title = (f"Gene {'families' if kind == 'families' else 'search'} by description"
             + (f" in {scope}" if scope else ""))

    def empty(mine_name, subject):
        return (f"{title}: no {kind} in {mine_name} with a description containing "
                f"{subject!r}. Descriptions spell out product names ('chalcone synthase', "
                "not 'CHS'): try the full name or a shorter phrase"
                + (", or search='families'" if kind == "genes" else "") + ".")

    def footer(rows):
        return "\n".join(x for x in (_whole_word_note(rows, query, col), _SEARCH_CAVEAT,
                                      nxt) if x)

    return _execute({**args, "mine": mine}, title, view, constraints, sort=sort,
                    assembly_col=None, subject_key="query",
                    subject_hint="description text such as 'chalcone synthase'",
                    footer=footer, on_empty=empty, pageable=True)


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
_GENE_ARG = {
    "gene": {"type": "string",
             "description": "Gene identifier, e.g. 'Glyma.12G040000' or the qualified "
                            "'glyma.Wm82.gnm4.ann1.Glyma.12G040000'. Matched with "
                            "InterMine LOOKUP, so either form works."},
    "mine": {"type": "string",
             "description": f"Mine to query (default {MINE!r}), e.g. 'glycinemine', "
                            "'phaseolusmine', 'legumemine'."},
    "max_results": {"type": "integer", "description": f"Row cap, 1-500 (default {MAX_ROWS})."},
}
_ASSEMBLY_ARGS = {
    "assembly": {"type": "string",
                 "description": "Narrow to one assembly, e.g. 'gnm4'. A bare gene name "
                                "matches every assembly (gnm2/gnm4/gnm6) — different loci."},
    "annotation": {"type": "string", "description": "Narrow to one annotation, e.g. 'ann1'."},
}


_TAXON_ARG = {
    "taxon": {"type": "string",
              "description": "Species whose mine to query, e.g. 'Glycine max', "
                             "'soybean' or 'phavu' (common names and abbreviations are "
                             "resolved through the catalog). Routes to <genus>mine; a "
                             "species whose genus has no mine is reported as such "
                             "without a request."},
    "mine": {"type": "string", "description": "Explicit mine name; overrides 'taxon'."},
    "max_results": {"type": "integer", "description": f"Row cap, 1-500 (default {MAX_ROWS})."},
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
        _mk("legumemine_gene_proteins",
            "Protein records for a gene from the LIS mine: protein identifier, length "
            "(aa) and molecular weight. Use when you need the protein a gene encodes, or "
            "to confirm a sequence length. For the actual sequence, use lis_gene + "
            "fasta_fetch against the Data Store instead.",
            {**_GENE_ARG, **_ASSEMBLY_ARGS}, _gene_proteins),
        _mk("legumemine_gene_families",
            "Gene family assignments for a gene (e.g. 'Legume.fam3.10524'), with family "
            "size and description. Families group homologs across species, paralogs "
            "included, so membership is not an orthology call; list a family's members "
            "with legumemine_gene_family_members. The Data Store ships family "
            "assignments as an UNINDEXED .gfa.tsv.gz that no tool can read, so the mine "
            "is the only way to get them.",
            {**_GENE_ARG, **_ASSEMBLY_ARGS}, _gene_families),
        _mk("legumemine_gene_ontology",
            "Ontology annotations for a gene — GO terms and other ontologies — as "
            "identifier, term name and source ontology. Use for 'what does this gene do?' "
            "when you want curated terms rather than a free-text description.",
            {**_GENE_ARG, **_ASSEMBLY_ARGS}, _gene_ontology),
        _mk("legumemine_gene_expression",
            "Expression values for a gene across samples, highest first, with the sample, "
            "its description (tissue/treatment), the source study and that study's unit "
            "(TPM, FPKM, ...). Values are comparable only within one study; pass 'source' "
            "to restrict to one. One gene can have hundreds of values, so results are "
            "capped and the total is reported.",
            {**_GENE_ARG,
             "source": {"type": "string",
                        "description": "Restrict to one expression study (the source "
                                       "identifier shown in a previous result)."}},
            _gene_expression),
        _mk("legumemine_gene_symbol",
            "Resolve a gene SYMBOL (e.g. 'GmNARK', 'PvSYMRK') to its gene identifier, "
            "full name, functional synopsis and the DOIs behind the claim. Answered from "
            "the resident curated catalog when it knows the symbol (instant, offline, "
            "fully-qualified id) and from the mine otherwise; the reply says which. Use "
            "this FIRST when you have a symbol rather than an ID — lis_gene and the other "
            "mine tools match identifiers only. Feed the DOIs to openalex_by_doi / "
            "read_paper.",
            {"symbol": {"type": "string",
                        "description": "Gene symbol, e.g. 'GmNARK'. Case-insensitive, exact."},
             **_TAXON_ARG}, _gene_symbol, required=("symbol",)),
        _mk("legumemine_gene_family_members",
            "Members of a gene's family across legume species — the homologs of a gene, "
            "for 'does my crop have a counterpart of this gene?'. Give 'gene' (its family "
            "is looked up first) or 'family'; add 'target_taxon' (e.g. 'Cicer arietinum' "
            "or 'chickpea') to list only that species' members, and 'assembly'/"
            "'annotation' to list only one genome's. A long list is paged: the reply "
            "names the 'offset' that continues it. Family membership is evidence of "
            "homology, NOT orthology: families include paralogs. Queries the pan-legume "
            "mine, where families span genera.",
            {"gene": {"type": "string", "description": "Gene identifier, e.g. 'Glyma.12G040000'."},
             "family": {"type": "string",
                        "description": "Gene family identifier, e.g. 'Legume.fam3.10524'. "
                                       "Skips the gene->family step."},
             "target_taxon": {"type": "string",
                              "description": "Only list members from this species or genus "
                                             "(Latin name, abbreviation or common name)."},
             "mine": {"type": "string",
                      "description": f"Mine to query (default {MINE!r}). A per-genus mine "
                                     "only holds its own genus's genes."},
             "max_results": {"type": "integer",
                             "description": f"Row cap, 1-500 (default {MAX_ROWS})."},
             "offset": {"type": "integer",
                        "description": "Rows to skip, to continue a list the reply cut "
                                       "short (it names the offset to use)."},
             "assembly": {"type": "string",
                          "description": "Only list members on this assembly version, "
                                         "e.g. 'gnm2'. Versions repeat across species, so "
                                         "pair it with 'target_taxon'."},
             "annotation": {"type": "string",
                            "description": "Only list members from this annotation "
                                           "version, e.g. 'ann1'."}},
            _gene_family_members, required=()),
        _mk("legumemine_gene_search",
            "Find genes, or gene families, by what their DESCRIPTION says — 'chalcone "
            "synthase', 'nodulation receptor kinase' — the way in when you know a "
            "function but no gene ID. Matches description text only, never IDs. "
            "search='families' returns family IDs for a family selector or "
            "legumemine_gene_family_members. Descriptions are automated and transferred "
            "from homologs, so a hit is a candidate, not a function; close paralogs "
            "share them. Queries the pan-legume mine.",
            {"query": {"type": "string",
                       "description": "Description text, matched case-insensitively as a "
                                      "substring, e.g. 'chalcone synthase'. Use full "
                                      "product names, not abbreviations."},
             "search": {"type": "string", "enum": ["genes", "families"],
                        "description": "'genes' (default) or 'families'."},
             "target_taxon": {"type": "string",
                              "description": "Genes only: this species or genus (Latin "
                                             "name, abbreviation or common name)."},
             "mine": {"type": "string",
                      "description": f"Mine to query (default {MINE!r})."},
             "max_results": {"type": "integer",
                             "description": f"Row cap, 1-500 (default {MAX_ROWS})."},
             "offset": {"type": "integer",
                        "description": "Rows to skip, to continue a list the reply cut "
                                       "short (it names the offset to use)."}},
            _gene_search, required=("query",)),
        _mk("lis_trait_qtls",
            "QTLs mapped for a trait: QTL name, linkage group, LOD, marker R2 and the "
            "study it came from. The breeder's entry point for 'what's known about the "
            "genetics of trait X?'. REQUIRES 'taxon' — QTL data exists only in the "
            "per-species mines, not the pan-legume one.",
            {"trait": {"type": "string",
                       "description": "Trait name or fragment, e.g. 'seed protein', "
                                      "'days to maturity'. Substring match."},
             **_TAXON_ARG}, _trait_qtls, required=("trait",)),
        _mk("lis_trait_gwas",
            "GWAS associations for a trait, most significant first: marker name, p-value "
            "and source study. Use alongside lis_trait_qtls for association evidence. The "
            "study identifiers match the Data Store's gwas/ collections, so lis_files can "
            "fetch the underlying data. REQUIRES 'taxon'.",
            {"trait": {"type": "string",
                       "description": "Trait name or fragment, e.g. 'seed protein'."},
             **_TAXON_ARG}, _trait_gwas, required=("trait",)),
        _mk("lis_marker_position",
            "Physical position of a genetic marker on each assembly that carries it — for "
            "marker-assisted selection, where the coordinate must match the breeder's own "
            "reference. One marker sits at different positions in gnm1/gnm2/gnm4. "
            "REQUIRES 'taxon'.",
            {"marker": {"type": "string",
                        "description": "Marker name, e.g. 'ss715614263'."},
             **_TAXON_ARG}, _marker_position, required=("marker",)),
    ]
