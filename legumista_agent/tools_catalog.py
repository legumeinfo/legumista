#!/usr/bin/env python3
"""LIS catalog tools — the whole datastore, resident in memory.

This module owns the catalog: it loads the document produced by
`lis-autocontent populate-catalog` — every collection in the store, built offline
from the datastore-metadata mirror — and holds it in memory for the life of the
process.

`tools_lis.py` reads through it too; between them the pair replaced the previous
approach of crawling https://data.legumeinfo.org for directory listings and
per-collection metadata. Beyond making discovery instant, a resident catalog can
answer what crawling structurally cannot: *absence* has no link to follow, so
"which species lack expression data" is only answerable from a complete catalog.

The tools here are the whole-store half — coverage, co-availability, provenance —
while `tools_lis.py` addresses individual collections and genes.

**We use DSCensor's own data structures rather than its HTTP API.** The catalog is
DSCensor's format, so `dscensor.catalog.CatalogController` is the authoritative
reader for it; re-implementing that parsing here would fork the schema contract
between two codebases that must agree. Importing it in-process also avoids
standing up a service just to read a local file. `dscensor.catalog` is pure
stdlib at import time (no aiohttp, no uvloop), so the coupling costs nothing at
runtime — see NOTE on packaging below.

NOTE (packaging): the `dscensor` distribution declares aiohttp/aiohttp-cors/uvloop
because it also ships an HTTP server we do not use. Nothing here imports them, but
`pip install dscensor` would. Until DSCensor splits a server-free core package,
legumista treats it as an OPTIONAL dependency: when it is absent these tools
report how to enable them and every other tool is unaffected.

The catalog is a build artifact of ``lis-autocontent populate-catalog``, not source. It
is no longer vendored in the repository: it is fetched from a published URL and cached on
disk (see ``catalog_source``), so a rebuilt catalog reaches a running server without a
code release. ``LEGUMISTA_CATALOG_PATH`` names a local file that wins over the cache,
which is the pin/offline path.

Every catalog carries the datastore-metadata commit it was built from, and every tool
answer repeats it, so a stale copy announces itself rather than being discovered.

Reloading is a hot swap: ``refresh()`` validates a download before building a new
controller, and only then rebinds it under the lock. Readers hold the old controller for
the length of one call, so an in-flight tool never sees a half-swapped catalog.

    LEGUMISTA_CATALOG_PATH   optional: pin the server to this catalog.json (nothing is
                             downloaded, polling is off, the webhook reports "pinned")
    LEGUMISTA_DSCENSOR_PATH  optional: a dscensor source checkout, for running against
                             a working tree instead of an installed package
"""
import asyncio
import difflib
import os
import re
import sys
import threading
from dataclasses import dataclass, field

from . import catalog_source
from .tool import Tool
from .tools_native import _cap

# A pinned catalog is operator configuration, so it comes from the environment and is
# fixed when the server starts. It used to be discovered — a `catalog.json` in the working
# directory or the checkout root — but both of those are, by default, the WORKSPACE that
# the genomics tools may write into: one file written there silently repointed every
# lis_* tool at content a tool call chose, switched off the refresh that would have
# replaced it, and survived a restart. Nothing a tool can write may change the server's
# mode, so nothing is looked for; `_sandbox_path` also refuses to address this file.
CATALOG_PATH = (os.path.abspath(os.environ["LEGUMISTA_CATALOG_PATH"])
                if os.environ.get("LEGUMISTA_CATALOG_PATH") else "")
DSCENSOR_PATH = os.environ.get("LEGUMISTA_DSCENSOR_PATH", "")

_STATE = {"controller": None, "error": None, "loaded": False, "path": ""}
_LOCK = threading.Lock()

def _unavailable_text() -> str:
    return (
        "error: no LIS catalog is loaded. Every lis_* tool reads the catalog, so none "
        "of them can answer until one is present.\n"
        f"The catalog is normally downloaded at startup from {catalog_source.catalog_url()} "
        f"and cached at {catalog_source.cache_path()}; a failed download leaves the tools "
        "unavailable rather than serving stale data.\n"
        "To fix: check network access to that URL, or set LEGUMISTA_CATALOG_PATH to a "
        "catalog.json to pin one explicitly."
    )


def _resolve_path() -> str:
    """Where to load from: the pinned file, then the cache."""
    if CATALOG_PATH:
        return CATALOG_PATH
    cached = catalog_source.cache_path()
    return str(cached) if cached.is_file() else ""


def _import_controller():
    """Import DSCensor's CatalogController, honouring a source checkout if given."""
    if DSCENSOR_PATH and DSCENSOR_PATH not in sys.path:
        sys.path.insert(0, DSCENSOR_PATH)
    from dscensor.catalog import CatalogController  # noqa: PLC0415
    return CatalogController


def controller():
    """The loaded catalog controller, or None with the reason recorded.

    Loading is lazy and attempted once: a missing catalog is an expected
    configuration, not an error worth retrying on every call.
    """
    with _LOCK:
        if _STATE["loaded"]:
            return _STATE["controller"]
        _STATE["loaded"] = True
        path = _resolve_path()
        if not path:
            _STATE["error"] = (
                "LEGUMISTA_CATALOG_PATH is unset and nothing is cached at "
                f"{catalog_source.cache_path()}"
            )
            return None
        ctl, err = _build(path)
        _STATE["controller"], _STATE["error"] = ctl, err
        _STATE["path"] = path if ctl is not None else ""
        return ctl


def _build(path: str):
    """Construct a controller from `path`. Returns (controller, error_text).

    Split out of `controller()` because a reload needs exactly this and nothing else:
    build first, and only rebind the live controller if the build succeeded.
    """
    try:
        catalog_controller = _import_controller()
    except ImportError as e:
        return None, (
            f"the 'dscensor' package is not importable ({e}). Install it, or set "
            "LEGUMISTA_DSCENSOR_PATH to a dscensor source checkout."
        )
    try:
        return catalog_controller(path), None
    except Exception as e:  # noqa: BLE001 - CatalogError, OSError, anything
        return None, f"{type(e).__name__}: {e}"


def source_path() -> str:
    """The file the loaded catalog came from, or "" if none is loaded."""
    return _STATE.get("path") or ""


def is_pinned() -> bool:
    """True when LEGUMISTA_CATALOG_PATH pins a local catalog, so downloads do not apply."""
    return bool(CATALOG_PATH)


def refresh(*, force: bool = False) -> dict:
    """Fetch the published catalog and, if it changed, swap it in.

    This is what the webhook and the background poller call. The swap is the last step
    and happens only after the download has been validated and a controller built from
    it, so a bad publish leaves the running server on its previous catalog rather than
    taking the tools down.

    Returns the fetch report with `reloaded` and `stamp` added.
    """
    if is_pinned():
        return {"status": "pinned", "reloaded": False,
                "detail": f"LEGUMISTA_CATALOG_PATH pins {_resolve_path()}; unset it "
                          "to serve the published catalog",
                "url": catalog_source.catalog_url()}

    report = catalog_source.fetch(force=force)
    report["reloaded"] = False
    if report["status"] != "updated":
        return report

    ctl, err = _build(str(catalog_source.cache_path()))
    if ctl is None:
        # The download validated as a catalog document but DSCensor would not read it.
        # Keep serving whatever is already loaded and say so.
        report["status"] = "error"
        report["detail"] = f"downloaded catalog could not be loaded: {err}"
        return report

    with _LOCK:
        _STATE.update({"controller": ctl, "error": None, "loaded": True,
                       "path": str(catalog_source.cache_path())})
    report["reloaded"] = True
    report["stamp"] = catalog_stamp(ctl)
    return report


def startup() -> dict:
    """Ensure a catalog is available when the server starts.

    A pinned local file short-circuits. Otherwise fetch — conditionally, so a warm cache
    costs one 304 — and fall back to whatever is cached when the network is unavailable.
    Never fatal: a server with no catalog still serves its other 24 tools.
    """
    if is_pinned():
        return {"status": "pinned", "detail": _resolve_path(), "reloaded": False}

    report = catalog_source.fetch()
    report["reloaded"] = False
    if report["status"] == "error" and catalog_source.cache_path().is_file():
        report["detail"] += " — falling back to the cached catalog"
    reset()
    ctl = controller()
    report["reloaded"] = ctl is not None
    if ctl is not None:
        report["stamp"] = catalog_stamp(ctl)
    return report


def reset():
    """Forget the loaded catalog, so the next read re-resolves and reloads."""
    with _LOCK:
        _STATE.update({"controller": None, "error": None, "loaded": False, "path": ""})


def catalog_stamp(ctl) -> str:
    """One line naming the catalog that produced an answer.

    A cached catalog is only as good as its last build, so every answer says
    which build it came from rather than leaving staleness to be discovered.
    """
    prov = ctl.provenance()
    commit = (prov.get("source_commit") or "unknown")[:8]
    return (
        f"[catalog built {prov.get('built_at') or '?'} from datastore-metadata "
        f"{commit}; {prov.get('stats', {}).get('collections', '?')} collections]"
    )


def catalog_unavailable() -> str:
    reason = _STATE.get("error")
    return _unavailable_text() + (f"\n(reason: {reason})" if reason else "")


# --- taxon resolution -----------------------------------------------------------------
# Models say "soybean", "glyma", "Glycine Max" and "glycine_max" as often as "Glycine
# max". Every tool that takes a taxon resolves it here, so an unrecognised NAME is never
# reported as absent DATA ("no collections for genus 'Soybean'"). Only exact matches on a
# Latin name, a datastore abbreviation or a common name are applied; near misses are
# offered as suggestions and never silently substituted.
@dataclass
class TaxonMatch:
    query: str
    genus: str = ""
    species: str = ""            # "" when only a genus was named or resolved
    via: str = ""                # "name" | "abbreviation" | "common name"
    candidates: list = field(default_factory=list)    # ambiguous: ["Genus species", ...]
    suggestions: list = field(default_factory=list)   # near misses, never auto-applied
    unknown_species: str = ""    # genus resolved but this epithet is not in it

    @property
    def ok(self) -> bool:
        return bool(self.genus) and not self.unknown_species and not self.candidates

    @property
    def label(self) -> str:
        return f"{self.genus} {self.species}".strip()

    def note(self) -> str:
        """One line to echo when the input was reinterpreted, "" otherwise."""
        if self.ok and self.via in ("abbreviation", "common name"):
            return f"(interpreting {self.query!r} as {self.label} — {self.via})"
        return ""

    def problem(self) -> str:
        """Why this did not resolve, phrased as a statement about the NAME, not the data."""
        if self.candidates:
            return (f"{self.query!r} matches several taxa in the LIS catalog: "
                    + ", ".join(self.candidates) + ". Pass one of them.")
        if self.unknown_species:
            known = ", ".join(self.suggestions) or "(none)"
            return (f"the LIS catalog has no species {self.unknown_species!r} in genus "
                    f"{self.genus}. {self.genus} species in the catalog, closest first: "
                    f"{known}. If you meant a species not listed, LIS holds no data for it.")
        tail = (" Closest names: " + ", ".join(self.suggestions) + "."
                if self.suggestions else "")
        return (f"{self.query!r} does not match any taxon in the LIS catalog (checked Latin "
                f"names, datastore abbreviations and common names).{tail} If you meant a "
                "species not listed, LIS holds no data for it.")


_TAXON_INDEX: dict = {}


def _taxon_index(ctl) -> dict:
    """Name tables for resolution, built once per loaded controller (a hot swap changes
    `ctl`, which rebuilds the index)."""
    cached = _TAXON_INDEX.get("index")
    if cached is not None and _TAXON_INDEX.get("ctl") is ctl:
        return cached
    binomials, genera, species_of = {}, {}, {}
    for coll in ctl.collections:
        genus, species = coll["genus"], coll["species"]
        genera[genus.lower()] = genus
        binomials[(genus.lower(), species.lower())] = (genus, species)
        species_of.setdefault(genus, set()).add(species)
    abbrevs, common = {}, {}
    for key, meta in (ctl.document.get("taxa") or {}).items():
        if not isinstance(meta, dict):
            continue
        genus, _, species = key.partition("/")
        # A taxon can be described without owning collections; it is still a real name.
        genera.setdefault(genus.lower(), genus)
        if species:
            binomials.setdefault((genus.lower(), species.lower()), (genus, species))
            species_of.setdefault(genus, set()).add(species)
        if meta.get("abbrev") and species:
            abbrevs[str(meta["abbrev"]).lower()] = (genus, species)
        for name in _common_names(meta.get("commonName")):
            common.setdefault(name, set()).add((genus, species))
    index = {"binomials": binomials, "genera": genera, "abbrevs": abbrevs,
             "common": common, "species_of": species_of}
    _TAXON_INDEX.update({"ctl": ctl, "index": index})
    return index


def _common_names(raw) -> list:
    """'antaque, banner bean, hyacinth bean' -> each name; 'amendoim silvestre (wild
    peanut)' -> the whole string, the part before the bracket, and the bracketed part."""
    out = set()
    for part in str(raw or "").split(","):
        part = " ".join(part.lower().split())
        if not part or part in ("etc.", "etc"):
            continue
        out.add(part)
        bracket = re.match(r"^(.*?)\s*\((.+)\)$", part)
        if bracket:
            out.update(p.strip() for p in bracket.groups() if p.strip())
    return sorted(out)


def resolve_taxon(text: str, ctl=None) -> TaxonMatch:
    """Resolve free text to a catalog taxon. See TaxonMatch for the result contract."""
    ctl = ctl or controller()
    query = (text or "").strip()
    match = TaxonMatch(query=query)
    if ctl is None or not query:
        return match
    idx = _taxon_index(ctl)
    norm = " ".join(query.replace("_", " ").lower().rstrip(".").split())
    tokens = norm.split()

    if len(tokens) >= 2 and (tokens[0], tokens[1]) in idx["binomials"]:
        match.genus, match.species = idx["binomials"][(tokens[0], tokens[1])]
        match.via = "name"
        return match
    if len(tokens) == 1 and tokens[0] in idx["genera"]:
        match.genus, match.via = idx["genera"][tokens[0]], "name"
        return match
    if len(tokens) == 1 and tokens[0] in idx["abbrevs"]:
        match.genus, match.species = idx["abbrevs"][tokens[0]]
        match.via = "abbreviation"
        return match
    targets = idx["common"].get(norm)
    if targets:
        species_level = sorted(t for t in targets if t[1])   # prefer species over genus
        chosen = species_level or sorted(targets)
        if len(chosen) == 1:
            match.genus, match.species = chosen[0]
            match.via = "common name"
        else:
            match.candidates = [f"{g} {s}".strip() for g, s in chosen]
        return match
    if len(tokens) >= 2 and tokens[0] in idx["genera"]:
        genus = idx["genera"][tokens[0]]
        match.genus, match.unknown_species = genus, tokens[1]
        # Closest epithets first, then the rest; the GENUS pseudo-species (genus-level
        # collections) is not a species and is not offered.
        epithets = sorted(s for s in idx["species_of"].get(genus, ()) if s != "GENUS")
        close = difflib.get_close_matches(tokens[1], epithets, n=3, cutoff=0.6)
        match.suggestions = close + [s for s in epithets if s not in close]
        return match
    names = ([f"{g} {s}" for g, s in idx["binomials"].values()]
             + list(idx["genera"].values()) + list(idx["common"]) + list(idx["abbrevs"]))
    lowered = {n.lower(): n for n in names}
    close = difflib.get_close_matches(norm, list(lowered), n=5, cutoff=0.75)
    match.suggestions = [lowered[c] for c in close]
    return match


# --- lis_survey ----------------------------------------------------------------------
def _survey(args) -> str:
    ctl = controller()
    if ctl is None:
        return catalog_unavailable()
    taxon = (args.get("taxon") or "").strip()
    genus, species, note = "", "", ""
    if taxon:
        match = resolve_taxon(taxon, ctl)
        if not match.ok:
            return _cap(f"{catalog_stamp(ctl)}\n\n{match.problem()}")
        genus, species, note = match.genus, match.species, match.note()
    needs = [t.strip() for t in (args.get("needs") or []) if str(t).strip()]

    lines = [catalog_stamp(ctl)] + ([note] if note else [])

    if needs:
        # A misspelled type ("expressions") would otherwise yield a confident zero. A
        # documented type that no collection happens to have ("gwas" in a small catalog)
        # is a genuine zero and stays an answer.
        known_types = {c["type"] for c in ctl.collections} | set(_TYPE_GLOSS)
        unknown = [t for t in needs if t not in known_types]
        if unknown:
            return _cap("\n".join(lines + [
                f"\nunknown data type(s): {', '.join(map(repr, unknown))}. "
                f"Valid types: {', '.join(sorted(known_types))}."]))
        # The co-availability question. A crawl cannot answer it: it requires
        # knowing the whole store at once, and absence has no URL to follow.
        matches = ctl.species_with(needs)
        lines.append(
            f"\n{len(matches)} species hold ALL of: {', '.join(needs)}"
        )
        # Bounded by the number of species in the store (~60), so never truncated.
        for row in matches:
            lines.append(f"  {row['genus']} {row['species']}")
        if not matches:
            lines.append("  (none — this is a real answer, not a lookup failure)")
        return _cap("\n".join(lines))

    if not genus:
        counts = {}
        for coll in ctl.collections:
            counts[coll["genus"]] = counts.get(coll["genus"], 0) + 1
        lines.append(f"\n{len(counts)} genera, {len(ctl.collections)} collections:")
        for name in sorted(counts):
            lines.append(f"  {name:<18} {counts[name]:>5}")
        lines.append("\nPass 'taxon' for one species, or 'needs' to ask which species "
                     "hold several data types at once.")
        return _cap("\n".join(lines))

    types = ctl.list_types(genus=genus, species=species)
    scope = f"{genus} {species}".strip()
    if not types:
        lines.append(f"\nno collections for {scope!r} in the catalog.")
        return _cap("\n".join(lines))
    total = sum(types.values())
    lines.append(f"\n{scope}: {total} collections across {len(types)} data types")
    for name, count in types.items():
        lines.append(f"  {name:<20} {count:>5}")
    lines.append("\nUse lis_find/lis_files for the collections themselves.")
    return _cap("\n".join(lines))


# --- lis_lineage ---------------------------------------------------------------------
def _lineage(args) -> str:
    ctl = controller()
    if ctl is None:
        return catalog_unavailable()
    identifier = (args.get("collection") or "").strip()
    if not identifier:
        return ("error: missing 'collection' — a collection id such as "
                "'Wm82.gnm4.ann1.T8TQ' (lis_find prints these).")
    # Accept a full path as well as a bare id; agents carry paths around.
    if "/" in identifier:
        identifier = identifier.rstrip("/").split("/")[-1]

    result = ctl.lineage(identifier)
    if not result["found"]:
        return (f"no collection with id {identifier!r} in the catalog. "
                f"{catalog_stamp(ctl)}")

    lines = [catalog_stamp(ctl), f"\nlineage of {identifier}:"]
    for step, entry in enumerate(result["chain"]):
        arrow = "" if step == 0 else "  derived from "
        lines.append(f"  {arrow}{entry['id']}  ({entry['type']})")
        if entry.get("publication_title"):
            lines.append(f"      {entry['publication_title']}")
        if entry.get("publication_doi"):
            lines.append(f"      doi: {entry['publication_doi']}")
        if entry.get("license"):
            lines.append(f"      license: {entry['license']}")
    if result["dois"]:
        from . import pubstatus  # lazy: keeps the catalog import-light
        lines.append(
            f"\nciting this result means citing {len(result['dois'])} publication(s):"
        )
        published = ctl.document.get("publications") or {}   # build-time status, if any
        for doi in result["dois"]:
            status = published.get(doi) or pubstatus.check_doi(doi)
            flag = pubstatus.describe(status)
            lines.append(f"  {doi}" + (f"   {flag}" if flag else "")
                         + "   -> openalex_by_doi / read_paper")
    else:
        lines.append("\nno publication DOI is published for anything in this chain.")
    return _cap("\n".join(lines))


def _mk(name, description, params, sync_fn):
    async def run(a, _f=sync_fn):
        return await asyncio.to_thread(_f, a)
    return Tool(name=name, description=description, parameters=params,
                read_only=True, run=run)


def catalog_tools() -> list:
    """Whole-store tools, available only when a catalog is configured."""
    return [
        _mk("lis_survey",
            "Survey what exists across the WHOLE LIS Data Store from a resident "
            "catalog: genera and their collection counts, the data types available "
            "for a species, or — with 'needs' — which species hold several data "
            "types at once. Use this for questions about coverage and absence "
            "('which species have both diversity and expression data?'), which "
            "lis_find cannot answer because crawling only finds what is present. "
            "Names are resolved (Latin, common name or abbreviation), and an unknown "
            "'needs' type is rejected rather than answered with a zero. "
            "Args: {taxon?, needs?}.",
            {"type": "object",
             "properties": {
                 "taxon": {"type": "string",
                           "description": "Species or genus: Latin name, common name "
                                          "or abbreviation, e.g. 'Glycine max', "
                                          "'soybean', 'glyma'. Omit to list all genera."},
                 "needs": {"type": "array", "items": {"type": "string"},
                           "description": "Collection types that must ALL be present, "
                                          "e.g. ['diversity','expression']."}},
             "additionalProperties": False}, _survey),
        _mk("lis_lineage",
            "Trace a LIS collection back through what it was derived from, and "
            "return every publication the result depends on. An annotation carries "
            "its own DOI and its genome's; this walks the chain and de-duplicates, "
            "so 'cite everything this rests on' is one call. Each DOI carries its "
            "retraction status. "
            "Args: {collection} — an id like 'Wm82.gnm4.ann1.T8TQ' or a full path.",
            {"type": "object",
             "properties": {"collection": {"type": "string",
                                           "description": "Collection id or path."}},
             "required": ["collection"], "additionalProperties": False}, _lineage),
    ]


# --- resident map ---------------------------------------------------------------------
# A compact projection of the catalog, injected into the MCP server's instructions so the
# model starts oriented instead of spending turns on exploratory calls ("which genera
# exist?", "what does Glycine max have?", "what is soybean called here?").
#
# Sizing, measured: the full catalog is ~557,000 tokens and one species' full records is
# ~256,000 -- neither can be resident. This projection is ~1,400 tokens because it carries
# only facts of BOUNDED cardinality: one line per species, changing when LIS adds a
# species. Anything unbounded (collection names, file lists, loci) stays a tool call.
#
# Descriptions of the datastore's collection types. Prompt text, not data -- an unlisted
# type still appears in the map, just without a gloss.
_TYPE_GLOSS = {
    "annotations": "gene models, CDS/protein FASTA",
    "diversity": "variant panels (VCF)",
    "expression": "expression matrices",
    "gene_functions": "curated gene-trait links",
    "genefamilies": "gene family sets",
    "genome_alignments": "whole-genome alignments",
    "genomes": "assembled sequence (FASTA)",
    "gwas": "association results",
    "maps": "genetic maps",
    "markers": "marker positions",
    "methylation": "methylation tracks",
    "mstmap": "MSTmap linkage output",
    "pangenes": "pangene sets",
    "pangenomes": "pangenome sets",
    "qtl": "linkage QTL studies",
    "repeats": "repeat annotations",
    "sequence_feature": "misc. feature tracks",
    "supplements": "supplementary files",
    "synteny": "pairwise syntenic blocks",
    "traits": "trait ontology links",
    "transcriptomes": "assembled transcripts",
}


def catalog_map() -> str:
    """The resident map, or "" when no catalog is loaded.

    Three lines of the preamble are load-bearing and should not be trimmed:

    * that an absent type means ZERO -- otherwise the model reads a short line as an
      incomplete map rather than as evidence of absence, which is the one thing a
      catalog can tell you that crawling cannot;
    * that the type names are verbatim `lis_find(type=...)` values -- otherwise the map
      generates malformed calls;
    * that collection names, file lists and loci are NOT here -- without it the model
      confabulates collection ids, having just been shown a confident inventory.
    """
    ctl = controller()
    if ctl is None:
        return ""
    census: dict = {}
    for collection in ctl.collections:
        key = (collection["genus"], collection["species"])
        census.setdefault(key, {})
        ctype = collection["type"]
        census[key][ctype] = census[key].get(ctype, 0) + 1
    if not census:
        return ""

    taxa = ctl.document.get("taxa", {})
    seen_types = sorted({t for types in census.values() for t in types})
    gloss = "; ".join(
        f"{t} = {_TYPE_GLOSS[t]}" for t in seen_types if t in _TYPE_GLOSS
    )

    lines = [
        f"## LIS Data Store map  [{catalog_stamp(ctl).strip('[]')}]",
        "",
        "Every species in the store, with its common name, datastore abbreviation, and",
        "how many collections of each data type it holds. A type absent from a line means",
        "ZERO collections of that type — that is a fact, not a gap in this map.",
        "",
        "Type names are the exact values `lis_find(type=...)` accepts. This map holds no",
        "collection names, no file lists and no gene loci; use the tools for those.",
        "",
        f"Types: {gloss}",
        "",
    ]
    for (genus, species), types in sorted(census.items()):
        meta = taxa.get(f"{genus}/{species}", {})
        tags = [x for x in (meta.get("commonName"), meta.get("abbrev")) if x]
        label = f"{genus} {species}" + (f" ({', '.join(tags)})" if tags else "")
        counts = " ".join(f"{t}={n}" for t, n in sorted(types.items()))
        lines.append(f"{label}: {counts}")
    return "\n".join(lines) + "\n"
