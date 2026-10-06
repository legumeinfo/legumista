#!/usr/bin/env python3
"""LIS Data Store tools, served from a resident catalog.

These tools used to discover the store by crawling https://data.legumeinfo.org --
parsing h5ai directory listings, fetching a README/CHECKSUM/MANIFEST per collection,
and probing for index siblings with HEAD requests. That worked, but it re-derived the
same structure in every model context, cost a request per directory level, and could
never see what was *absent*.

They now read a **catalog**: one document describing every collection in the store,
built offline by ``lis-autocontent populate-catalog`` from the datastore-metadata
mirror and loaded through DSCensor's own ``CatalogController`` (see ``tools_catalog.py``
for why in-process rather than over DSCensor's HTTP API).

**No metadata endpoint on data.legumeinfo.org is contacted any more.** No directory
listings, no README/CHECKSUM/MANIFEST fetches, no HEAD probes. Discovery is a dict
lookup.

Two file reads over http(s) remain, and they are deliberate -- they are *data*, not
metadata endpoints, and no catalog can carry them:

* ``gene_models_main.bed.gz`` -- gene coordinates. `lis_gene`'s whole job is turning a
  name into a locus, and the loci live in this file. The catalog supplies its URL. Its
  coordinates are each transcript's CODING extent (first CDS base to last; LIS builds it
  with datastore-specifications' gff_to_bed7_mRNA.awk, and it was named cds.bed until
  2024), so they exclude the UTRs and must never be called the mRNA or gene span.
* the annotation's synonym file -- superseded gene IDs, likewise a data file whose URL
  comes from the catalog.
* ``gene_models_main.gff3.gz`` -- one indexed region per gene, read through an htslib
  worker, for the gene's true span (the BED's coding extent leaves out the UTRs) and the
  description in its ``Note``.

Curated gene *symbols* used to be a third such read; they are now carried in the
catalog itself, so ``GmNARK`` resolves without touching the network at all.

Design, unchanged: these tools **resolve, they do not retrieve**. They return URLs and
sequence names that `fasta_fetch`, `tabix_query`, `bcftools` and `samtools` read, and
DOIs that `openalex_by_doi`/`read_paper` follow.
"""
import asyncio
import gzip
import io
import os
import re
import urllib.parse
from concurrent.futures import ThreadPoolExecutor

from .results import count_phrase, fail
from .tool import Tool
from .tools_catalog import catalog_stamp, catalog_unavailable, controller, resolve_taxon
from .tools_native import MAX_CHARS, _cap, _get_bytes, _validate_url

MAX_RESULTS = int(os.environ.get("LEGUMISTA_LIS_MAX_RESULTS", "10"))
BED_MAX_BYTES = int(os.environ.get("LEGUMISTA_LIS_BED_MAX_BYTES", str(64_000_000)))

# A fully-qualified LIS gene ID is "<abbrev>.<strain>.<gnmN>.<annN>.<GeneName>[.N]" --
# it carries the annotation STEM but NOT the collection's 4-character key, so the key
# is recovered from the catalog rather than by listing a directory.
_QUALIFIED_GENE_RE = re.compile(
    r"^(?P<abbrev>[a-z]{4,6})\.(?P<stem>[A-Za-z0-9_-]+\.gnm\d+\.ann\d+)\.(?P<name>.+)$")
# The same stem prefix, for reducing a BED id to its bare gene name.
_GENE_PREFIX_RE = re.compile(r"^[a-z]{4,6}\.[A-Za-z0-9_-]+\.gnm\d+\.ann\d+\.")

# Index siblings -> how the existing toolset reads the file they belong to.
_ACCESS_BY_INDEX = {
    ".fai": ("fasta_fetch", "fasta_fetch(path=URL, region='<seq name>' or 'seq:start-end')"),
    ".tbi": ("tabix_query", "tabix_query(path=URL, region='contig:start-end')"),
    ".csi": ("tabix_query", "tabix_query(path=URL, region='contig:start-end')"),
    ".bai": ("samtools", "samtools(args=['view', URL, 'contig:start-end'])"),
    ".crai": ("samtools", "samtools(args=['view', URL, 'contig:start-end'])"),
}
_VARIANT_EXT = (".vcf.gz", ".bcf")
_SYNONYM_SUFFIXES = (".synonym.txt.gz", ".info_synonyms.txt.gz")


def _file_url(record, name):
    """Absolute URL for one file in a catalog collection."""
    return "{0}/{1}".format(record["base_url"].rstrip("/"), name)


def _access_for(entry):
    """(tool, how) for a catalog file entry, or (None, "") when it has no index."""
    for suffix in entry.get("i") or []:
        if suffix in (".tbi", ".csi") and entry["n"].endswith(_VARIANT_EXT):
            return "bcftools", "bcftools(args=['view','-H','-r','contig:start-end',URL])"
        tool, how = _ACCESS_BY_INDEX.get(suffix, (None, ""))
        if tool:
            return tool, how
    return None, ""


def _normalize_collection(spec):
    """Accept a datastore path, a full datastore URL, or a bare collection id.

    Returns (path, identifier). `path` is None when only a bare id was given, in which
    case `identifier` is that id -- or an "error:" string the caller returns verbatim.
    """
    spec = (spec or "").strip().rstrip("/")
    if not spec:
        return None, ("error: missing 'collection' — a datastore path like "
                      "'Glycine/max/annotations/Wm82.gnm4.ann1.T8TQ' (lis_find prints "
                      "these), or a bare collection id.")
    if spec.startswith(("http://", "https://")):
        spec = "/".join([p for p in spec.split("/") if p][2:])  # drop scheme + host
    parts = [p for p in spec.split("/") if p]
    if len(parts) == 1:  # a bare id; resolved against the catalog by the caller
        return None, parts[0]
    if len(parts) < 4:
        return None, (f"error: {spec!r} is not a collection path. Expected "
                      "'<Genus>/<species>/<type>/<collection>'.")
    return "/".join(parts[:4]), parts[3]


def _lookup(spec):
    """Resolve a collection spec to a catalog record. Returns (record, error_text)."""
    ctl = controller()
    if ctl is None:
        return None, catalog_unavailable()
    path, identifier = _normalize_collection(spec)
    if path is None and identifier.startswith("error:"):
        return None, identifier
    if path is not None:
        record = ctl.get_collection(path)
        if record is None:
            return None, (f"no collection at {path!r} in the catalog. "
                          f"{catalog_stamp(ctl)} Use lis_find to list what exists.")
        return record, None
    matches = [c for c in ctl.collections if c["id"] == identifier]
    if not matches:
        return None, (f"no collection with id {identifier!r} in the catalog. "
                      f"{catalog_stamp(ctl)}")
    if len(matches) > 1:
        paths = ", ".join(m["path"] for m in matches[:5])
        return None, (f"{identifier!r} is ambiguous ({len(matches)} matches): {paths}. "
                      "Pass the full path.")
    return matches[0], None


# --- lis_find -------------------------------------------------------------------------
def _find(args) -> str:
    ctl = controller()
    if ctl is None:
        return catalog_unavailable()
    genus = (args.get("genus") or "").strip()
    species = (args.get("species") or "").strip()
    taxon = (args.get("taxon") or "").strip()
    ctype = (args.get("type") or "").strip()
    query = (args.get("query") or "").strip().lower()
    limit = max(1, min(int(args.get("max_results") or MAX_RESULTS), 25))

    # Resolve whatever naming the caller used (Latin name in any case, abbreviation,
    # common name). An unrecognised NAME is reported as such, never as absent data.
    spec = taxon or " ".join(p for p in (genus, species) if p)
    note = ""
    if spec:
        match = resolve_taxon(spec, ctl)
        if not match.ok:
            return f"{match.problem()}\n{catalog_stamp(ctl)}"
        genus, species, note = match.genus, match.species, match.note()
    out = _find_resolved(ctl, genus, species, ctype, query, limit)
    return f"{note}\n{out}" if note else out


def _collection_lines(record, with_taxon=False):
    """One collection as lis_find lists it."""
    lines = [f"\n  {record['id']}", f"    path: {record['path']}"]
    if with_taxon:
        lines.append(f"    type: {record['type']}   taxon: {record['genus']} "
                     f"{record['species']}")
    if record.get("synopsis"):
        lines.append(f"    synopsis: {record['synopsis']}")
    genotype = record.get("genotype")
    if genotype:
        joined = (", ".join(map(str, genotype))
                  if isinstance(genotype, list) else genotype)
        lines.append(f"    genotype: {joined}")
    if record.get("expression_unit"):
        lines.append(f"    expression_unit: {record['expression_unit']}")
    if record.get("publication_doi"):
        lines.append(f"    publication_doi: {record['publication_doi']}"
                     "   -> openalex_by_doi / read_paper")
    if record.get("source"):
        lines.append(f"    source: {record['source']}")
    return lines


def _search(ctl, genus, species, ctype, query, limit) -> str:
    """Collections whose id contains `query`, in whatever scope was given: the whole
    catalog when no taxon was. A query used to be read only after a taxon and a type, so
    a bare collection id came back as the list of genera, and an agent holding a full
    id hunted through species for it."""
    stamp = catalog_stamp(ctl)
    scope = [c for c in ctl.collections
             if (not genus or c["genus"] == genus)
             and (not species or c["species"] == species)
             and (not ctype or c["type"] == ctype)]
    where = ("/".join(p for p in (genus, species, ctype) if p)
             or "the whole catalog")
    hits = [c for c in scope if query in c["id"].lower()]
    if not hits:
        return (f"no collection id contains {query!r} in {where} (ids matched as a "
                f"case-insensitive substring). {stamp}")
    hits.sort(key=lambda c: (c["id"].lower() != query, c["path"]))
    shown = hits[:limit]
    lines = [f"{count_phrase(len(shown), len(hits), 'collection(s)')} whose id contains "
             f"{query!r}, in {where}:"]
    for record in shown:
        lines += _collection_lines(record, with_taxon=not (genus and species and ctype))
    if len(hits) > len(shown):
        lines.append(f"\nRaise 'max_results' (up to 25) or narrow with 'taxon'/'type' to "
                     "see the rest.")
    lines.append(stamp)
    return _cap("\n".join(lines))


def _find_resolved(ctl, genus, species, ctype, query, limit) -> str:
    stamp = catalog_stamp(ctl)
    if query:
        return _search(ctl, genus, species, ctype, query, limit)

    if not genus:
        genera = sorted({c["genus"] for c in ctl.collections})
        return (f"{len(genera)} genera in the LIS Data Store (pass one as 'genus', or a "
                "full 'taxon' like 'Glycine max'):\n  " + ", ".join(genera)
                + f"\n{stamp}")

    if not species:
        specs = sorted({c["species"] for c in ctl.collections if c["genus"] == genus})
        if not specs:
            return f"no collections for genus {genus!r} in the catalog. {stamp}"
        return (f"{genus}: {len(specs)} species/collection group(s):\n  "
                + ", ".join(specs)
                + "\n\nPass one as 'species' to see its data types."
                + f"\n{stamp}")

    scoped = [c for c in ctl.collections
              if c["genus"] == genus and c["species"] == species]
    if not scoped:
        return f"no collections for {genus} {species!r} in the catalog. {stamp}"

    if not ctype:
        types = sorted({c["type"] for c in scoped})
        head = f"{genus} {species}"
        sample = scoped[0]
        if sample.get("taxid"):
            head += f"  taxid:{sample['taxid']}"
        if sample.get("scientific_name_abbrev"):
            head += f"  abbrev:{sample['scientific_name_abbrev']}"
        return (head + f"\n{len(types)} data type(s):\n  " + ", ".join(types)
                + "\n\nPass one as 'type' to list its collections (with publication "
                  "DOIs)." + f"\n{stamp}")

    colls = [c for c in scoped if c["type"] == ctype]
    base = f"{genus}/{species}/{ctype}"
    if not colls:
        types = sorted({c["type"] for c in scoped})
        return (f"no collections under {base}. Available types for {genus} {species}: "
                + ", ".join(types) + f"\n{stamp}")
    total = len(colls)
    shown = sorted(colls, key=lambda c: c["id"])[:limit]

    lines = [f"{count_phrase(len(shown), total, 'collection(s)')} under {base}:"]
    for record in shown:
        lines += _collection_lines(record)
    lines.append(stamp)
    return _cap("\n".join(lines))


# --- lis_files ------------------------------------------------------------------------
# Catalog fields shown in a collection's record, in this order. Everything the catalog
# holds about a collection belongs in one reply: a field no tool shows (expression_unit
# was one) is a question no agent can answer.
_RECORD_FIELDS = (
    ("dataset_doi", "dataset_doi"), ("expression_unit", "expression_unit"),
    ("genetic_map", "genetic_map"), ("related_to", "related_to"),
    ("dataset_release_date", "released"), ("bioproject", "bioproject"),
    ("sraproject", "sraproject"), ("genbank_accession", "genbank_accession"),
    ("chromosome_prefix", "chromosome_prefix"), ("supercontig_prefix", "supercontig_prefix"),
    ("source", "source"), ("license", "license"),
)


def _record_lines(record):
    """Everything the catalog records about one collection, as `key: value` lines."""
    def text(value):
        return ", ".join(map(str, value)) if isinstance(value, list) else str(value)

    inherited = set(record.get("inherited") or [])
    parents = record.get("derived_from") or []
    lines = [f"collection: {record['id']}", f"path: {record['path']}",
             f"type: {record['type']}"]
    if record.get("scientific_name"):
        lines.append(f"taxon: {record['scientific_name']}"
                     + (f" (taxid {record['taxid']})" if record.get("taxid") else ""))
    for key in ("genotype", "synopsis", "description"):
        if record.get(key) and not (key == "description"
                                    and record[key] == record.get("synopsis")):
            lines.append(f"{key}: {text(record[key])}")
    if record.get("publication_doi"):
        lines.append(f"publication_doi: {record['publication_doi']}"
                     + (f"  ({record['publication_title']})"
                        if record.get("publication_title") else "")
                     + "   -> openalex_by_doi / read_paper")
    else:
        lines.append("publication_doi: none — this collection records no publication of "
                     "its own" + ("; its sources' are in lis_lineage" if parents else ""))
    if parents:
        lines.append(f"derived_from: {text(parents)}   -> lis_lineage")
    for key, label in _RECORD_FIELDS:
        if record.get(key):
            lines.append(f"{label}: {text(record[key])}"
                         + ("  (inherited from derived_from)" if key in inherited else ""))
    return lines


def _whole_file_reader(name, record):
    """The tool that reads this unindexed file whole, if one does. Only two kinds are
    read that way, and saying so matters: an agent told a family assignment file was
    unreadable fetched it with curl and grepped its gene IDs."""
    low = name.lower()
    if low.endswith(".gfa.tsv.gz") and (".legume.fam3." in low or ".legfed_v1_0." in low):
        return (f"a family selector: lis_gene(genes={{'family': <family id>, "
                f"'collection': '{record['id']}'}})")
    if low.endswith(_SYNONYM_SUFFIXES):
        return "lis_gene, to resolve superseded gene IDs"
    return ""


def _files(args) -> str:
    record, err = _lookup(args.get("collection"))
    if record is None:
        return err
    ctl = controller()
    data = record.get("files", [])

    head = _record_lines(record)
    head.append(f"{len(data)} data file(s); base URL {record['base_url']}/")

    status = record.get("index_status", "unknown")
    if status == "unknown" or not data:
        # Nothing could be resolved: no CHECKSUM, and no documented convention to fall
        # back on. Report the gap as itself rather than as an empty collection.
        return _cap("\n".join(head + [
            "",
            "FILE LIST UNAVAILABLE — this collection publishes no CHECKSUM and its type "
            "has no documented filename convention, so nothing can be listed. That is a "
            "gap in the published metadata, not an empty collection: files are reachable "
            "under the base URL above if you already know their names.",
            catalog_stamp(ctl),
        ]))

    # How the file list was obtained. `checksum` is authoritative; the other two were
    # constructed from the datastore's documented filename convention, and only
    # `verified` has been confirmed to exist. Saying which is the difference between
    # a fact and a good guess.
    provenance = {
        "known": None,
        "verified": ("file list resolved from the datastore's documented filename "
                     "convention and CONFIRMED to exist (this collection publishes no "
                     "CHECKSUM)"),
        "inferred": ("file list PREDICTED from the datastore's documented filename "
                     "convention and not confirmed — a listed file may not exist "
                     "(this collection publishes no CHECKSUM)"),
    }.get(status)
    if provenance:
        head.append(provenance)

    addressable, plain, unprobed = [], [], []
    for entry in sorted(data, key=lambda f: f["n"]):
        tool, how = _access_for(entry)
        row = (entry["n"], tool, how, entry.get("description", ""))
        if tool:
            addressable.append(row)
        elif entry.get("i_unknown"):
            # The filename was derived from convention but never probed, and this file
            # type CAN carry an index. Putting it in "not indexed" would claim knowledge
            # we do not have — and a markers .gff3.gz resolved this way really does have
            # a .tbi.
            unprobed.append(row)
        else:
            plain.append(row)

    lines = head + ["", f"RANDOMLY ACCESSIBLE ({len(addressable)}) — stream a region "
                        "without downloading the file:"]
    for name, tool, how, desc in addressable:
        lines.append(f"  {name}")
        if desc:
            lines.append(f"      {desc}")
        lines.append(f"      via {tool}: {how}")
        lines.append(f"      url {_file_url(record, name)}")
    lines.append("")
    lines.append(f"NOT INDEXED ({len(plain)}) — no .fai/.tbi published, so these cannot "
                 "be region-queried; they are whole-file downloads, and none of these "
                 "tools reads them except where a line says which does:")
    for name, _tool, _how, desc in plain:
        lines.append(f"  {name}" + (f"   — {desc}" if desc else ""))
        reader = _whole_file_reader(name, record)
        if reader:
            lines.append(f"      read by {reader}")
    if unprobed:
        lines.append("")
        lines.append(f"INDEX STATUS UNKNOWN ({len(unprobed)}) — these filenames were "
                     "derived from the datastore's convention but not checked for "
                     "index siblings, and this file type can carry them. Try the "
                     "streaming tools before assuming a whole-file download; rebuild "
                     "the catalog with --verify to settle it.")
        for name, _tool, _how, desc in unprobed:
            lines.append(f"  {name}" + (f"   — {desc}" if desc else ""))
    lines.append("")
    lines.append(catalog_stamp(ctl))
    return _cap("\n".join(lines))


# --- lis_gene -------------------------------------------------------------------------
def _annotation_for_gene(gene, collection):
    """The annotation collection to search. Returns (record, error_text)."""
    if collection:
        return _lookup(collection)
    ctl = controller()
    if ctl is None:
        return None, catalog_unavailable()
    match = _QUALIFIED_GENE_RE.match(gene)
    if not match:
        return None, ("error: pass 'collection' (a path or id from lis_find), or a "
                      "fully qualified gene ID like "
                      "'glyma.Wm82.gnm4.ann1.Glyma.12G040000'.")
    abbrev, stem = match.group("abbrev"), match.group("stem")
    # The qualified ID carries the annotation stem but not the 4-character key, so the
    # key comes from the catalog -- this used to mean listing a directory.
    for record in ctl.collections:
        if (record["type"] == "annotations"
                and record.get("scientific_name_abbrev") == abbrev
                and record["id"].startswith(stem + ".")):
            return record, None
    return None, (f"no annotation collection {stem}.* for {abbrev!r} in the catalog. "
                  f"{catalog_stamp(ctl)}")


def _fetch_gz_text(url, limit=BED_MAX_BYTES):
    """Fetch and decompress a gzipped datastore data file. Returns (text, error)."""
    try:
        _validate_url(url)
        raw = _get_bytes(url, limit)
        text = gzip.GzipFile(fileobj=io.BytesIO(raw)).read().decode("utf-8", "replace")
        return text, None
    except Exception as e:  # noqa: BLE001
        return None, f"{type(e).__name__}: {e}"


def _synonyms(record):
    """Superseded gene ID -> current ID, from the annotation's synonym file.

    Returns (index, status, filename), status one of "ok", "absent" (the collection
    publishes no synonym file) or "failed: <reason>" (it does, but it could not be read).
    The caller must report the last two differently: "absent" is a fact about the
    collection, "failed" means the route was NOT checked.

    A data file, not a metadata endpoint: its URL comes from the catalog, but the
    mapping itself exists only inside the file.
    """
    name = next((f["n"] for f in record.get("files", [])
                 if f["n"].endswith(_SYNONYM_SUFFIXES)), None)
    if not name:
        return {}, "absent", ""
    text, err = _fetch_gz_text(_file_url(record, name))
    if err:
        return {}, f"failed: {err}", name
    index = {}
    for line in text.splitlines():
        if line.startswith("#"):
            continue
        parts = line.split("\t")
        if len(parts) >= 2 and parts[0].strip() and parts[1].strip():
            index.setdefault(parts[1].strip().lower(), parts[0].strip())
    return index, "ok", name


def _symbol_note(gene, entry):
    note = f"curated symbol {gene!r} ({entry['abbrev']}.traits.yml, carried in the catalog)"
    if entry.get("doi"):
        note += f" (publication_doi: {entry['doi']})"
    if entry.get("synopsis"):
        note += f"\n  synopsis: {entry['synopsis']}"
    return note


def _home_annotation(gene_id, record):
    """' — it belongs to <id>; rerun with collection=<path>' when a fully qualified gene
    ID comes from a different annotation than `record`, else ""."""
    match = _QUALIFIED_GENE_RE.match(gene_id or "")
    if not match or record["id"].startswith(match.group("stem") + "."):
        return ""
    home, _err = _annotation_for_gene(gene_id, "")
    if home is None:
        return f" — it belongs to annotation {match.group('stem')}.*, which is not in the catalog"
    return f" — it belongs to {home['id']}; rerun with collection='{home['path']}'"


def _resolve_in_annotation(gene, record, bed):
    """Try every route from a name to BED rows, recording what each route found.

    Returns (hits, label, provenance, routes, incomplete). `routes` is a list of
    (status, what, outcome) with status "checked", "unavailable" or "NOT CHECKED";
    `incomplete` is True when a route that could have matched was not checked, in which
    case a miss must not be reported as a definitive absence.
    """
    routes = []
    hits = _bed_hits(bed, gene)
    if hits:
        return hits, gene, "", routes, False
    routes.append(("checked", "exact gene/mRNA ID in the gene models BED", "no match"))

    bare = _GENE_PREFIX_RE.sub("", gene)
    matches = _unprefixed_matches(bed, bare)
    if len(matches) == 1:
        found = _bed_hits(bed, matches[0])
        return (found, matches[0], f"{gene!r} is {matches[0]} without its "
                f"'{matches[0].split('.', 1)[0]}.' prefix, as the genus mines spell it",
                routes, False)
    routes.append(("checked", "the name without a prefix such as 'Arahy.' (the genus "
                   "mines' spelling)",
                   f"ambiguous: {len(matches)} genes, {', '.join(matches[:3])}"
                   if matches else "no match"))

    ctl = controller()
    entries = ctl.resolve_symbol(gene, record.get("scientific_name_abbrev", "")) if ctl else []
    if not entries:
        routes.append(("checked", "curated symbols (catalog)", "no match"))
    for entry in entries:
        found = _bed_hits(bed, entry["gene"])
        if found:
            return found, entry["gene"], _symbol_note(gene, entry), routes, False
        routes.append(("checked", "curated symbols (catalog)",
                       f"{gene!r} is the symbol for {entry['gene']}, which is not in this "
                       "annotation" + _home_annotation(entry["gene"], record)))

    index, status, name = _synonyms(record)
    if status == "absent":
        routes.append(("unavailable", "superseded IDs",
                       "this collection publishes no synonym file"))
        return [], gene, "", routes, False
    if status != "ok":
        routes.append(("NOT CHECKED", "superseded IDs",
                       f"the synonym file {name} could not be read ({status[len('failed: '):]})"))
        return [], gene, "", routes, True
    current = index.get(gene.lower())
    if current:
        found = _bed_hits(bed, current)
        if found:
            return (found, current, f"superseded ID {gene!r} -> {current} via the "
                    f"collection's synonym file ({name})", routes, False)
        routes.append(("checked", f"superseded IDs (synonym file {name})",
                       f"maps to {current}, which is not in this annotation"))
    else:
        routes.append(("checked", f"superseded IDs (synonym file {name})", "no match"))
    return [], gene, "", routes, False


def _unprefixed_matches(bed, bare):
    """BED names that are `bare` plus a leading 'Prefix.' token, as gene or mRNA names.

    The genus mines can drop that token: ArachisMine's 6J3HHE is the Data Store's
    Arahy.6J3HHE. Returns the distinct names matched (the caller resolves only one)."""
    found = []
    for line in bed.splitlines():
        fields = line.split("\t")
        if len(fields) < 6:
            continue
        for name in (fields[3], fields[6] if len(fields) > 6 else ""):
            short = _GENE_PREFIX_RE.sub("", name)
            if "." in short and short.split(".", 1)[1] == bare and short not in found:
                found.append(short)
    return found


def _bed_hits(bed, gene):
    """Rows whose mRNA (col 4) or gene (col 7) field equals `gene`, prefixed or not.
    Exact only: a substring match would return Glyma.12G0400001 for Glyma.12G040000."""
    hits = []
    for line in bed.splitlines():
        fields = line.split("\t")
        if len(fields) < 6:
            continue
        mrna, geneid = fields[3], (fields[6] if len(fields) > 6 else "")
        cands = {mrna, geneid}
        cands |= {_GENE_PREFIX_RE.sub("", c) for c in (mrna, geneid) if c}
        if gene in cands:
            hits.append((fields[0], int(fields[1]) + 1, int(fields[2]), fields[5],
                         mrna, geneid))
    return hits


def _indexed_gff(record):
    """The annotation's gene_models_main.gff3.gz if it is region-queryable, else None."""
    return next((f["n"] for f in record.get("files", [])
                 if f["n"].endswith(".gene_models_main.gff3.gz")
                 and {".tbi", ".csi"} & set(f.get("i") or [])), None)


def _worker_batch(items):
    """Run htslib batch items in one worker, so the egress proxy and timeout apply and a
    CSI-only index is handled. Returns (results, error): one dict per item, or ([],
    reason) when the worker itself failed."""
    from .tools_pysam import READ_FILE_BYTES, _run_worker

    res = _run_worker({"op": "batch", "items": items}, READ_FILE_BYTES)
    if res.get("error"):
        return [], str(res.get("message") or res["error"])
    return res.get("items", []), ""


def _tabix_items(record, gff_name, regions):
    return [{"kind": "tabix", "path": _file_url(record, gff_name), "contig": contig,
             "start": max(0, lo - 1), "end": hi, "limit": 5000}
            for contig, lo, hi in regions]


def _rows_or_reason(item):
    """A tabix batch result: its rows, or why the region could not be read."""
    return (str(item.get("message") or item["error"]) if item.get("error")
            else item.get("rows", []))


def _read_gff(record, gff_name, regions):
    """GFF3 rows over each (contig, lo, hi) region, 1-based, all in one htslib worker.

    Returns (results, error): one entry per region, a list of rows or the reason that
    region could not be read; or ([], reason) when the worker itself failed."""
    results, err = _worker_batch(_tabix_items(record, gff_name, regions))
    return [_rows_or_reason(item) for item in results], err


def _protein_fasta(record):
    """(file, rule) for the annotation's indexed protein FASTA: the primary-model file,
    whose one record per gene is taken as is, else the full file, whose longest model
    stands for the gene. (None, "") when neither is indexed."""
    for suffix, rule in ((".protein_primary.faa.gz", "present"),
                         (".protein.faa.gz", "longest")):
        name = next((f["n"] for f in record.get("files", []) if f["n"].endswith(suffix)
                     and ".fai" in (f.get("i") or [])), None)
        if name:
            return name, rule
    return None, ""


def _parse_gene(rows, gene_id):
    """(gene, mrnas, description) for `gene_id` from GFF3 rows. gene is (start, end,
    strand) or None, mrnas is [(id, start, end)], description the gene row's Note."""
    gene, mrnas, note = None, [], ""
    for row in rows:
        cols = row.split("\t")
        if len(cols) < 9:
            continue
        attrs = dict(p.split("=", 1) for p in cols[8].split(";") if "=" in p)
        if cols[2] == "gene" and attrs.get("ID") == gene_id:
            gene = (int(cols[3]), int(cols[4]), cols[6])
            note = urllib.parse.unquote(attrs.get("Note", "")).strip()
        elif cols[2] == "mRNA" and attrs.get("Parent") == gene_id:
            mrnas.append((attrs.get("ID", "?"), int(cols[3]), int(cols[4])))
    return gene, sorted(mrnas, key=lambda m: (m[1], m[0])), note


def _gene_span(record, gff_name, contig, lo, hi, gene_id):
    """The gene's span, its transcripts' spans and its description, from the GFF3.

    Returns (gene, mrnas, description, error), as _parse_gene plus error: "" or why the
    GFF3 could not be read. One indexed region covering the coding extent."""
    results, err = _read_gff(record, gff_name, [(contig, lo, hi)])
    rows = results[0] if results else err or "no result"
    if isinstance(rows, str):
        return None, [], "", rows
    return (*_parse_gene(rows, gene_id), "")


# The description is AHRD-style text transferred from a homolog's annotation (often
# ending "[Glycine max]"). It says what a gene resembles; close paralogs share it, so
# peanut's stilbene synthases are described as "chalcone synthase" like its CHS genes.
_DESCRIPTION_SOURCE = ("the gene row's Note in gene_models_main.gff3: an automated "
                       "description, usually transferred from a homolog — what the gene "
                       "resembles, not a demonstrated function; a bracketed species names "
                       "that homolog's species, not the gene's")
_DESCRIPTION_WIDTH = 100


def _short_description(note):
    """A Note's first clause (the product name), without its InterPro and GO lists."""
    head = note.split("; ")[0].strip()
    return head if len(head) <= _DESCRIPTION_WIDTH else head[:_DESCRIPTION_WIDTH - 1] + "…"


def _gene_list(selector) -> str:
    """lis_gene over a selector: one row per gene with its span and description."""
    from . import genes as G

    sel = G.resolve(selector)
    if sel.error:
        return sel.error
    offset = int(selector.get("offset") or 0)
    if not sel.genes:
        return sel.summary(offset)
    record = sel.record
    gff = _indexed_gff(record)
    protein, rule = _protein_fasta(record)
    # One worker for the whole page: a GFF3 region per gene, then a protein lookup per
    # gene. Each file is opened once.
    items = (_tabix_items(record, gff, [(g.contig, g.start, g.end) for g in sel.genes])
             if gff else [])
    if protein:
        items += [{"kind": "pick", "path": _file_url(record, protein), "names": g.models,
                   "rule": rule} for g in sel.genes]
    results, batch_err = _worker_batch(items) if items else ([], "")
    gff_results = results[:len(sel.genes)] if gff else []
    pick_results = results[len(gff_results):]
    found, unread = {}, set()
    gff_err = ("no indexed gene_models_main.gff3 is published" if not gff else batch_err)
    for gene, item in zip(sel.genes, gff_results):
        rows = _rows_or_reason(item)
        if isinstance(rows, str):
            unread.add(gene.id)
            continue
        span, _mrnas, note = _parse_gene(rows, gene.id)
        if span:
            found[gene.id] = (span, note)
    lengths = {}
    for gene, item in zip(sel.genes, pick_results):
        lengths[gene.id] = ("NOT CHECKED" if item.get("error") else
                            f"{item['length']} aa" if item.get("length") else
                            "not in the FASTA")

    def protein_cell(gene):
        if not protein:
            return "—"
        return lengths.get(gene.id, "NOT CHECKED")

    rows = []
    for gene in sel.genes:
        if gene.id in found:
            (lo, hi, strand), note = found[gene.id]
            rows.append(f"  {gene.id} | {gene.contig}:{lo:,}-{hi:,} ({strand}) | "
                        f"{protein_cell(gene)} | "
                        + (_short_description(note) or "(the gene row has no Note)"))
        else:
            why = ("NOT CHECKED" if gff_err or gene.id in unread
                   else "no gene row in the GFF3")
            rows.append(f"  {gene.id} | {gene.contig}:{gene.start:,}-{gene.end:,} "
                        f"({gene.strand}) [coding extent] | {protein_cell(gene)} | {why}")
    assembly = _genome_of(f"{record.get('scientific_name_abbrev', '')}.{record['id']}")
    notes = []
    if gff_err:
        notes.append(f"gene spans and descriptions NOT CHECKED — {gff_err}. Loci are "
                     "coding extents (UTRs excluded); no description was read.")
    elif unread:
        notes.append(f"{len(unread)} gene(s) NOT CHECKED: their GFF3 region could not be "
                     "read, so their loci are coding extents and they have no description.")
    notes += [f"coordinates: assembly {assembly}; 1-based, inclusive. Locus = the gene "
              "span from gene_models_main.gff3, UTRs included.",
              f"description: the product name, first clause of {_DESCRIPTION_SOURCE}. "
              "Close paralogs share it (peanut's stilbene synthases read 'chalcone "
              "synthase'), so it cannot tell them apart. lis_gene on one gene gives the "
              "whole Note.",
              ("protein: " + ("the primary model's length" if rule == "present" else
                              "the longest model's length")
               + f" in {protein}. One far below the rest of a family usually marks a "
               "partial or broken model, not a different gene." if protein else
               "protein: no indexed protein FASTA is published, so no length is given.")]
    if protein and batch_err:
        notes.insert(0, f"protein lengths NOT CHECKED — {batch_err}.")
    columns = "  gene | locus | protein | description"
    fixed = sum(len(x) + 1 for x in notes) + len(columns) + 200
    budget = MAX_CHARS - fixed - len(sel.summary(offset))
    keep, used = 0, 0
    for row in rows:
        if keep and used + len(row) + 1 > budget:
            break
        keep, used = keep + 1, used + len(row) + 1
    sel.genes = sel.genes[:keep]
    return _cap("\n".join([sel.summary(offset), columns] + rows[:keep] + notes))


def _gene(args) -> str:
    gene = (args.get("gene") or "").strip()
    selector = args.get("genes")
    if selector is not None:
        if gene:
            return "error: pass 'gene' (one gene) or 'genes' (a selector), not both."
        if isinstance(selector, dict) and args.get("collection") and not selector.get(
                "collection"):
            selector = {**selector, "collection": args["collection"]}
        return _gene_list(selector)
    if not gene:
        return ("error: missing 'gene' — a gene ID, mRNA ID, or curated symbol; or "
                "'genes', a selector, to list several.")
    record, err = _annotation_for_gene(gene, (args.get("collection") or "").strip())
    if record is None:
        return err
    ctl = controller()

    if record.get("index_status") != "known":
        return (f"{record['id']} publishes no CHECKSUM, so the catalog has no file list "
                f"for it and cannot locate its gene models. {catalog_stamp(ctl)}")
    by_name = {f["n"]: f for f in record.get("files", [])}
    bed_name = next((n for n in by_name if n.endswith(".gene_models_main.bed.gz")), None)
    if not bed_name:
        return (f"error: {record['id']} publishes no gene_models_main.bed.gz, so gene "
                "lookup is unavailable for this collection.")

    # The only remaining data read: gene coordinates live in this file and nowhere else.
    bed, err = _fetch_gz_text(_file_url(record, bed_name))
    if err:
        return f"error: could not read {bed_name}: {err}"

    hits, label, provenance, routes, incomplete = _resolve_in_annotation(gene, record, bed)
    if not hits:
        head = (f"no match for {gene!r} in {record['id']}"
                + (" — INCOMPLETE SEARCH (see NOT CHECKED below)." if incomplete else "."))
        lines = [head] + [f"  {status:<11} {what}: {outcome}"
                          for status, what, outcome in routes]
        if incomplete:
            lines.append("A match through the unchecked route cannot be ruled out: retry, "
                         "or report this ID as unverified rather than absent.")
        if not re.search(r"\d", gene):
            # 'CHS', 'chalcone synthase': a function, not a name. Searching IDs for its
            # letters once turned up Arahy.CHS32V, a dynamin.
            lines.append(f"{gene!r} looks like a function or abbreviation, not a gene ID. "
                         "lis_gene resolves names only: to find genes by what they do, "
                         "use legumemine_gene_search with the full product name (e.g. "
                         "'chalcone synthase'), or legumemine_gene_symbol for a curated "
                         "symbol.")
        lines.append(catalog_stamp(ctl))
        return "\n".join(lines)

    contig = hits[0][0]
    start, end = min(h[1] for h in hits), max(h[2] for h in hits)
    strand = hits[0][3]
    seq_names = sorted({h[4] for h in hits})

    assembly = _genome_of(f"{record.get('scientific_name_abbrev', '')}.{record['id']}")
    gene_id = hits[0][5] or hits[0][4].rsplit(".", 1)[0]
    gff = _indexed_gff(record)
    span, mrnas, note, span_err = (
        _gene_span(record, gff, contig, start, end, gene_id) if gff
        else (None, [], "", "no indexed gene_models_main.gff3 is published"))
    lines = [f"{label} in {record['id']}"
             + (f"\n  resolved from: {provenance}" if provenance else "")]
    if span:
        region_lo, region_hi = span[0], span[1]
        models = "; ".join(f"{m}: {a:,}-{b:,}" for m, a, b in mrnas)
        lines.append(f"  gene span:  {contig}:{span[0]:,}-{span[1]:,} ({span[2]})   "
                     f"[gene row in gene_models_main.gff3, UTRs included"
                     + (f"; mRNA {models}" if models else "") + "]")
        lines.append(f"  description: {note}   [{_DESCRIPTION_SOURCE}]" if note else
                     "  description: none — the gene row in gene_models_main.gff3 has "
                     "no Note")
    else:
        region_lo, region_hi = start, end
        lines.append("  gene span:  NOT CHECKED — "
                     + (span_err if span_err else f"no gene row with ID {gene_id} in the "
                        "GFF3 over this locus")
                     + ". The coding extent below excludes the UTRs.")
    lines += [f"  coding extent:  {contig}:{start:,}-{end:,} ({strand})   "
              f"[first CDS base to last, from gene_models_main.bed — excludes the UTRs; "
              f"{len(hits)} model(s): {', '.join(seq_names)}]",
              f"  coordinates: assembly {assembly}; 1-based, inclusive (converted from the "
              "BED's 0-based start). Coordinates on another assembly differ.",
              f"  region string for tabix_query/samtools ("
              + ("gene span" if span else "coding extent; widen it to take in the UTRs")
              + f"): {contig}:{region_lo}-{region_hi}"]
    for label_text, suffix in (("protein", ".protein_primary.faa.gz"),
                               ("CDS", ".cds_primary.fna.gz")):
        hit = next((n for n in by_name if n.endswith(suffix)), None)
        if hit and ".fai" in (by_name[hit].get("i") or []):
            lines.append(f"  {label_text}: fasta_fetch("
                         f"path='{_file_url(record, hit)}', region='{seq_names[0]}')")
    if gff:
        lines.append(f"  models: tabix_query(path='{_file_url(record, gff)}', "
                     f"region='{contig}:{region_lo}-{region_hi}')")
    if record.get("publication_doi"):
        lines.append(f"  publication_doi: {record['publication_doi']}"
                     "   -> openalex_by_doi")
    lines.append(f"  {catalog_stamp(ctl)}")
    return _cap("\n".join(lines))



# --- lis_synteny ----------------------------------------------------------------------
# Synteny blocks live in DAGchainer GFF3s that carry no .tbi (0 of 102 files are indexed),
# so a region query means fetching the file -- ~23 KB gzipped -- and filtering in memory.
# Everything else the tool needs is already in the catalog: which genomes are paired, which
# file holds each pair, and which assemblies exist for a species. In particular the pair
# graph is derived at catalog-build time from filenames, so a genome that owns no synteny
# collection (Medicago owns none) still resolves to its partners.
_BLOCK_RE = re.compile(
    r"^Name=(?P<contig>[^;]+);matches=(?P<b>[^:]+):(?P<start>\d+)\.\.(?P<end>\d+)"
    r"(?:;median_Ks=(?P<ks>[0-9.eE+-]+))?"
)
MAX_BLOCKS = int(os.environ.get("LEGUMISTA_LIS_MAX_BLOCKS", "200"))
# Pair files read per call. The largest partner set in the catalog is 12 (glyma.Wm82.gnm2),
# so the default reads everything; the cap exists only to bound a future outlier.
MAX_PARTNERS = int(os.environ.get("LEGUMISTA_LIS_MAX_PARTNERS", "25"))


def _genome_of(spec):
    """Reduce a gene ID or genome string to `abbrev.strain.gnmN`."""
    spec = (spec or "").strip()
    match = _QUALIFIED_GENE_RE.match(spec)
    if match:
        stem = match.group("stem").rsplit(".ann", 1)[0]
        return f"{match.group('abbrev')}.{stem}"
    parts = spec.split(".")
    for i, part in enumerate(parts):
        if part.startswith("gnm"):
            return ".".join(parts[: i + 1])
    return spec


def _species_of_genome(ctl, genome):
    """(genus, species) for a genome string, from the catalog."""
    abbrev = genome.split(".")[0]
    for collection in ctl.collections:
        if collection.get("scientific_name_abbrev") == abbrev:
            return collection["genus"], collection["species"]
    return "", ""


def _no_synteny_message(ctl, genome):
    """Route to an assembly that does have synteny, rather than reporting nothing.

    Synteny is published for one, usually old, assembly per species; the common request
    arrives on a newer one. Naming the alternative turns a dead end into a next step.
    """
    genus, species = _species_of_genome(ctl, genome)
    available = sorted({p["a"] for p in ctl.pairwise()} | {p["b"] for p in ctl.pairwise()})
    same_species = [g for g in available if g.split(".")[0] == genome.split(".")[0]]
    lines = [f"no synteny or alignment data for {genome!r}."]
    if same_species:
        lines.append("Published for this species against: " + ", ".join(same_species) + ".")
        if genus:
            lines.append(f"Re-run with a gene or region on one of those, e.g. "
                         f"lis_find(taxon='{genus} {species}', type='annotations', "
                         f"query='{same_species[0].split('.', 1)[1]}').")
    else:
        lines.append("No assembly of this species has synteny or alignment data in the "
                     "catalog. This is a gap in the store, not a lookup failure.")
    lines.append(catalog_stamp(ctl))
    return "\n".join(lines)


def _parse_blocks(text, region_contig, lo, hi, cap, swap=False):
    """DAGchainer syntenic_region rows, optionally filtered on the caller's interval.

    Column 1 of a DAGchainer GFF3 is the file's reference genome (the first genome in the
    filename); `matches=` carries the other one. When the file is stored under the
    partner's collection (`direction == "query"`), the caller's genome is the `matches=`
    side, so `swap=True` makes it side A for both the region filter and the output.

    Returns (blocks, total_matching, unparsed_rows).
    """
    blocks, total, unparsed = [], 0, 0
    for line in text.splitlines():
        if line.startswith("#"):
            continue
        fields = line.split("\t")
        if len(fields) < 9 or fields[2] != "syntenic_region":
            continue
        match = _BLOCK_RE.match(fields[8])
        if not match:
            unparsed += 1
            continue
        file_a = (fields[0], int(fields[3]), int(fields[4]))
        file_b = (match.group("b"), int(match.group("start")), int(match.group("end")))
        a, b = (file_b, file_a) if swap else (file_a, file_b)
        if region_contig:
            if a[0] != region_contig:
                continue
            if lo is not None and (a[2] < lo or a[1] > hi):
                continue
        total += 1
        if len(blocks) >= cap:
            continue
        blocks.append({
            "a": f"{a[0]}:{a[1]}-{a[2]}",
            "b": f"{b[0]}:{b[1]}-{b[2]}",
            "strand": fields[6],
            "score": fields[5],
            "ks": match.group("ks"),
        })
    return blocks, total, unparsed


def _one_file_per_partner(candidates):
    """Most pairs are published twice, once under each genome's synteny collection. Keep
    one file per partner, preferring the one stored under the caller's genome (its
    coordinates are native); keep every self-comparison file (they differ by epoch).
    Returns (files_to_read_sorted_by_partner, duplicates_dropped)."""
    chosen, dropped = {}, []
    for pair in candidates:
        if pair["b"] == pair["a"]:
            chosen[("self", pair["file"])] = pair
            continue
        current = chosen.get(pair["b"])
        if current is None:
            chosen[pair["b"]] = pair
        elif current["direction"] == "query" and pair["direction"] == "reference":
            dropped.append(current)
            chosen[pair["b"]] = pair
        else:
            dropped.append(pair)
    files = sorted(chosen.values(), key=lambda p: (p["b"], p["file"]))
    return files, dropped


def _synteny(args) -> str:
    ctl = controller()
    if ctl is None:
        return catalog_unavailable()
    gene = (args.get("gene") or "").strip()
    genome = _genome_of(args.get("genome") or gene)
    if not genome:
        return ("error: pass 'genome' (e.g. 'glyma.Wm82.gnm2') or 'gene' (a gene ID whose "
                "assembly it will be taken from).")
    partner = (args.get("partner") or "").strip()
    region = (args.get("region") or "").strip()
    cap = max(1, min(int(args.get("max_blocks") or MAX_BLOCKS), MAX_BLOCKS))

    pairs = ctl.pairwise(genome=genome)
    if not pairs:
        return _no_synteny_message(ctl, genome)

    # No partner and no region: orient the caller. Answered from the catalog alone.
    if not partner and not region:
        by_partner = {}
        for pair in pairs:
            by_partner.setdefault(pair["b"], []).append(pair)
        lines = [f"{genome}: {len(by_partner)} partner(s) across {len(pairs)} file(s)"]
        for name in sorted(by_partner):
            entries = by_partner[name]
            kinds = sorted({e["kind"] for e in entries})
            epochs = sorted({e["epoch"] for e in entries if e.get("epoch")})
            note = f"  {name}  [{', '.join(kinds)}]"
            if entries[0].get("self"):
                note += "  (self-comparison"
                note += f", {', '.join(epochs)})" if epochs else ")"
            if entries[0]["direction"] == "query":
                note += "  (stored under the partner's collection)"
            if any(e.get("i") for e in entries):
                note += "  indexed — readable with samtools"
            lines.append(note)
        lines.append("\nPass 'partner' and optionally 'region' for the blocks themselves.")
        lines.append(catalog_stamp(ctl))
        return _cap("\n".join(lines))

    candidates = [p for p in pairs if p["kind"] == "synteny"]
    if partner:
        wanted = _genome_of(partner)
        candidates = [p for p in candidates
                      if p["b"] == wanted or p["b"].startswith(wanted + ".")]
        if not candidates:
            others = sorted({p["b"] for p in pairs})
            return (f"no synteny file pairs {genome} with {partner!r}. "
                    f"Partners: {', '.join(others)}. {catalog_stamp(ctl)}")
    if not candidates:
        return (f"{genome} has alignment files but no synteny blocks. "
                f"{catalog_stamp(ctl)}")

    contig, lo, hi = "", None, None
    if region:
        contig, lo, hi, err = _parse_region(region)
        if err:
            return err

    files, dropped = _one_file_per_partner(candidates)
    limit = max(1, min(int(args.get("max_partners") or MAX_PARTNERS), MAX_PARTNERS))
    to_read, unread = files[:limit], files[limit:]
    # Each file is ~23 KB gzipped; read them concurrently so "every partner" stays fast.
    with ThreadPoolExecutor(max_workers=6) as pool:
        texts = list(pool.map(lambda p: _fetch_gz_text(p["url"]), to_read))

    partners = sorted({p["b"] for p in files})
    out = [f"{genome}" + (f" x {partner}" if partner else "")
           + (f"  region {region}" if region else ""),
           f"read {len(to_read)} of {len(files)} synteny file(s) covering "
           f"{len(partners)} partner(s)"
           + (f"; skipped {len(dropped)} duplicate(s) of the same pair published under "
              "the other genome" if dropped else "")]
    if unread:
        out.append("NOT READ (max_partners reached): "
                   + ", ".join(p["b"] for p in unread)
                   + " — raise max_partners or pass partner=... for these.")
    failures = 0
    for pair, (text, err) in zip(to_read, texts):
        if err:
            failures += 1
            out.append(f"\n  {pair['b']}: COULD NOT READ {pair['file']} ({err}) — "
                       "blocks for this partner are unknown, not absent")
            continue
        swap = pair["direction"] == "query"
        blocks, total, unparsed = _parse_blocks(text, contig, lo, hi, cap, swap=swap)
        header = f"\n  {genome} x {pair['b']}"
        if pair.get("epoch"):
            header += f" ({pair['epoch']})"
        if swap:
            header += ("  [file stored under the partner's collection; columns swapped so "
                       f"the left side is {genome}]")
        out.append(header)
        if unparsed:
            out.append(f"    {unparsed} row(s) could not be parsed and were skipped")
        if not blocks:
            out.append("    no blocks overlap that region (the file was read; this is an "
                       "empty result, not an error)")
            continue
        out.append(f"    {len(blocks)} of {total} block(s)"
                   + ("  [capped]" if total > len(blocks) else "") + ":")
        for block in blocks:
            ks = f"  median_Ks={block['ks']}" if block["ks"] else ""
            out.append(f"      {block['a']}  ->  {block['b']}  "
                       f"({block['strand']}) score={block['score']}{ks}")
        out.append(f"    source: {pair['collection']}")
    out.append(f"\n{catalog_stamp(ctl)}")
    if failures and failures == len(to_read):
        return fail(f"could not read any synteny file for {genome} "
                    f"({failures} attempted).\n" + "\n".join(out))
    return _cap("\n".join(out))


def _parse_region(region):
    """samtools-style region -> (contig, lo, hi, error). 1-based inclusive."""
    region = region.strip()
    if ":" not in region:
        return region, None, None, None
    contig, span = region.rsplit(":", 1)
    span = span.replace(",", "")
    try:
        if "-" in span:
            lo_s, hi_s = span.split("-", 1)
            lo, hi = int(lo_s), int(hi_s)
        else:
            lo = hi = int(span)
    except ValueError:
        return "", None, None, f"error: could not parse region {region!r}."
    if lo > hi:
        return "", None, None, f"error: region {region!r} has start > end."
    return contig, lo, hi, None


# --- registry -------------------------------------------------------------------------
def _mk(name, description, params, sync_fn):
    async def run(a, _f=sync_fn):
        return await asyncio.to_thread(_f, a)
    return Tool(name=name, description=description, parameters=params,
                read_only=True, run=run)


def genes_selector_schema() -> dict:
    """genes.SELECTOR_SCHEMA, imported late: genes imports this module."""
    from .genes import SELECTOR_SCHEMA
    return SELECTOR_SCHEMA


def lis_tools() -> list:
    """Read-only tools for the LIS Data Store, served from the resident catalog."""
    return [
        _mk("lis_find",
            "Find LIS Data Store collections in the resident catalog. No arguments: "
            "the genera. taxon: its data types. taxon and type: its collections, with "
            "synopsis, genotype, expression unit and publication DOI. query matches "
            "collection ids, across the whole catalog unless taxon or type narrow it. "
            "Ids end in an arbitrary 4-character key (Wm82.gnm4.ann1.T8TQ): take them "
            "from here.",
            {"type": "object",
             "properties": {
                 "taxon": {"type": "string",
                           "description": "Species: Latin name, common name or "
                                          "abbreviation, e.g. 'Glycine max', 'soybean', "
                                          "'glyma'."},
                 "genus": {"type": "string",
                           "description": "Genus alone, e.g. 'Glycine'."},
                 "species": {"type": "string",
                             "description": "Species epithet, e.g. 'max'."},
                 "type": {"type": "string",
                          "description": "Data type: genomes, annotations, diversity, "
                                         "gwas, expression, markers, qtl, synteny, …"},
                 "query": {"type": "string",
                           "description": "Case-insensitive substring of a collection "
                                          "id; searches the whole catalog unless "
                                          "taxon/type narrow it."},
                 "max_results": {"type": "integer",
                                 "description": "Collections to detail, 1–25 "
                                                "(default 10)."}},
             "additionalProperties": False}, _find),
        _mk("lis_files",
            "Everything the catalog records about one collection (taxon, genotype, "
            "publication or its absence, expression unit, accessions, derived_from), "
            "then its files: which are region-readable and the call that reads each, "
            "which are NOT INDEXED, and how the file list was obtained.",
            {"type": "object",
             "properties": {"collection": {"type": "string",
                                           "description": "Datastore path, collection "
                                                          "id, or full datastore URL."}},
             "required": ["collection"], "additionalProperties": False}, _files),
        _mk("lis_gene",
            "One gene in a LIS annotation: gene span and coding extent, description, "
            "and ready calls for its protein, CDS and gene models. Takes an ID, a "
            "curated symbol or a superseded ID; the reply names the route that "
            "resolved it, and a miss lists every route checked. An ID from another "
            "assembly will not resolve. With genes (a selector) in place of gene: one "
            "row per gene, with locus, protein length and description.",
            {"type": "object",
             "properties": {"gene": {"type": "string",
                                     "description": "Gene ID, mRNA ID, or curated "
                                                    "symbol."},
                            "genes": genes_selector_schema(),
                            "collection": {"type": "string",
                                           "description": "Annotation collection path "
                                                          "or id."}},
             "required": [], "additionalProperties": False}, _gene),
        _mk("lis_synteny",
            "Synteny between LIS assemblies. With genome or gene alone: every partner "
            "assembly, including pairs stored under the partner's collection. With "
            "partner, and optionally a region on the requested genome: blocks with "
            "score and median_Ks, the requested genome on the left. Usually published "
            "for one, often older, assembly per species; the reply names it.",
            {"type": "object",
             "properties": {
                 "genome": {"type": "string",
                            "description": "Assembly, e.g. 'glyma.Wm82.gnm2'."},
                 "gene": {"type": "string",
                          "description": "Gene ID; its assembly is used."},
                 "partner": {"type": "string",
                             "description": "Restrict to one partner assembly."},
                 "region": {"type": "string",
                            "description": "Region on the requested genome, 'contig' or "
                                           "'contig:start-end' (1-based inclusive)."},
                 "max_blocks": {"type": "integer",
                                "description": "Block cap per pair (default 200)."},
                 "max_partners": {"type": "integer",
                                  "description": "Partner files to read when no 'partner' "
                                                 "is given (default and max 25; every "
                                                 "partner in the current catalog)."}},
             "additionalProperties": False}, _synteny),
    ]
