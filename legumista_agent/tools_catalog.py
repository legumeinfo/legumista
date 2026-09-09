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

The catalog is a top-level ``catalog.json`` -- looked for at the install root, then in
the working directory. It is a build artifact of ``lis-autocontent populate-catalog``,
not source, and carries the datastore-metadata commit it was built from so a stale copy
announces itself.

    LEGUMISTA_DSCENSOR_PATH  optional: a dscensor source checkout, for running against
                             a working tree instead of an installed package
"""
import asyncio
import os
import sys
import threading
from pathlib import Path

from .tool import Tool
from .tools_native import _cap

# The catalog ships alongside the code as a top-level `catalog.json`. Two locations are
# tried, in order, and nothing is configurable: the repo/install root (a source checkout
# or `pip install -e`) and then the working directory (which is what a container mount
# lands on). Absent from both, the lis_* tools report how to build one.
_CANDIDATES = (
    Path(__file__).resolve().parent.parent / "catalog.json",
    Path.cwd() / "catalog.json",
)
CATALOG_PATH = next((str(p) for p in _CANDIDATES if p.is_file()), "")
DSCENSOR_PATH = os.environ.get("LEGUMISTA_DSCENSOR_PATH", "")

_STATE = {"controller": None, "error": None, "loaded": False}
_LOCK = threading.Lock()

_UNAVAILABLE = (
    "error: no LIS catalog is loaded. Every lis_* tool reads the catalog, so none "
    "of them can answer until one is present.\n"
    "To enable: build one with `lis-autocontent populate-catalog --from_github "
    "./datastore-metadata --verify --catalog_out catalog.json` and put it at the "
    "top level of the legumista checkout (or the working directory)."
)


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
        path = CATALOG_PATH or next(
            (str(p) for p in _CANDIDATES if p.is_file()), ""
        )
        if not path:
            _STATE["error"] = "no catalog.json at " + " or ".join(
                str(p) for p in _CANDIDATES
            )
            return None
        try:
            catalog_controller = _import_controller()
        except ImportError as e:
            _STATE["error"] = (
                f"the 'dscensor' package is not importable ({e}). Install it, or set "
                "LEGUMISTA_DSCENSOR_PATH to a dscensor source checkout."
            )
            return None
        try:
            _STATE["controller"] = catalog_controller(path)
        except Exception as e:  # noqa: BLE001 - CatalogError, OSError, anything
            _STATE["error"] = f"{type(e).__name__}: {e}"
            return None
        return _STATE["controller"]


def reset():
    """Forget the loaded catalog. For tests, and for a future reload command."""
    with _LOCK:
        _STATE.update({"controller": None, "error": None, "loaded": False})


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
    return _UNAVAILABLE + (f"\n(reason: {reason})" if reason else "")


# --- lis_survey ----------------------------------------------------------------------
def _survey(args) -> str:
    ctl = controller()
    if ctl is None:
        return catalog_unavailable()
    taxon = (args.get("taxon") or "").strip()
    genus, species = "", ""
    if taxon:
        bits = taxon.replace("_", " ").split()
        genus = bits[0].capitalize() if bits else ""
        species = bits[1].lower() if len(bits) > 1 else ""
    needs = [t.strip() for t in (args.get("needs") or []) if str(t).strip()]

    lines = [catalog_stamp(ctl)]

    if needs:
        # The co-availability question. A crawl cannot answer it: it requires
        # knowing the whole store at once, and absence has no URL to follow.
        matches = ctl.species_with(needs)
        lines.append(
            f"\n{len(matches)} species hold ALL of: {', '.join(needs)}"
        )
        for row in matches[:40]:
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
        lines.append(
            f"\nciting this result means citing {len(result['dois'])} publication(s):"
        )
        for doi in result["dois"]:
            lines.append(f"  {doi}   -> openalex_by_doi / read_paper")
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
            "Args: {taxon?, needs?}.",
            {"type": "object",
             "properties": {
                 "taxon": {"type": "string",
                           "description": "Species or genus, e.g. 'Glycine max'. "
                                          "Omit to list all genera."},
                 "needs": {"type": "array", "items": {"type": "string"},
                           "description": "Collection types that must ALL be present, "
                                          "e.g. ['diversity','expression']."}},
             "additionalProperties": False}, _survey),
        _mk("lis_lineage",
            "Trace a LIS collection back through what it was derived from, and "
            "return every publication the result depends on. An annotation carries "
            "its own DOI and its genome's; this walks the chain and de-duplicates, "
            "so 'cite everything this rests on' is one call. "
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
