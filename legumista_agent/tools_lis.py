#!/usr/bin/env python3
"""LIS Data Store tools — systematic access to https://data.legumeinfo.org.

The Legume Information System publishes a formally specified data store
(github.com/legumeinfo/datastore-specifications): every "collection" (one directory of
related files) carries four layers of machine-readable metadata —

  description_{Genus}_{species}.yml   taxid, abbreviation, common name  (per species)
  README.{collection}.yml             genotype, synopsis, provenance, publication_doi
  MANIFEST.{collection}.yml           a description per data file
  CHECKSUM.{collection}.md5           the authoritative file list

— and `publication_doi` is REQUIRED by that spec, so every dataset points at its own
paper. These tools surface all of it from the live service, which is the public access
path (the datastore-metadata git repo is the upstream source of truth, but is not what
users are meant to query).

Three problems make the store hard for an agent to use unaided, and these tools exist to
solve exactly those:

1. **Collection keys are unguessable.** A collection is `Wm82.gnm4.ann1.T8TQ` — the
   trailing four characters are arbitrary. No agent derives that from "soybean Williams 82
   annotation v4", so `lis_find` discovers it.
2. **Random access is invisible.** The HTML directory listing omits the `.fai`/`.tbi`/
   `.gzi` index siblings, so nothing tells an agent that `protein_primary.faa.gz` can be
   addressed by gene ID while `legume.fam3.*.gfa.tsv.gz` cannot be read at all. The
   CHECKSUM file does list them, so `lis_files` reports each file's access mode.
3. **There is no gene-name lookup.** `tabix_query` needs coordinates, so `lis_gene`
   resolves an exact gene/mRNA ID to its locus and sequence handles.

Design: these tools **resolve, they do not retrieve**. They return URLs and sequence names
that the existing toolset consumes — `fasta_fetch`, `tabix_query`, `bcftools`, `samtools`
for data; `openalex_by_doi`, `read_paper` for the publication. Nothing here duplicates a
data path that already exists.

State: none on disk. Like `read_paper`, everything is fetched into memory; a small
process-lifetime cache keeps repeat lookups (notably the ~0.9 MB BED backing `lis_gene`)
free within a session without introducing the toolset's first persistent state.
"""
import asyncio
import gzip
import io
import os
import re
import threading
from concurrent.futures import ThreadPoolExecutor

from .tool import Tool
from .tools_native import BlockedURLError, MAX_CHARS, _cap, _get, _get_bytes, _validate_url

BASE_URL = os.environ.get("LEGUMISTA_LIS_BASE_URL", "https://data.legumeinfo.org").rstrip("/")
MAX_RESULTS = int(os.environ.get("LEGUMISTA_LIS_MAX_RESULTS", "10"))
BED_MAX_BYTES = int(os.environ.get("LEGUMISTA_LIS_BED_MAX_BYTES", str(64_000_000)))

# Collection directories are "<strain>.<gnmN>[.annN].<KEY>" or "<...>.<type>.<Author_Year>".
# The genome/annotation forms are what `lis_gene` can parse a gene ID against.
_COLLECTION_RE = re.compile(r"^[A-Za-z0-9_.-]+\.(?:gnm\d+|gen)\b")
# A fully-qualified LIS gene ID is "<abbrev>.<strain>.<gnmN>.<annN>.<GeneName>[.N]" —
# note it carries the annotation STEM but NOT the collection's 4-character key, so the
# key still has to be recovered by listing the annotations directory.
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

_CACHE: dict = {}
_CACHE_LOCK = threading.Lock()


def _cached(key: str, produce):
    """Memoize for the life of the process. The store is versioned and effectively
    immutable (a revision is a new collection), so there is nothing to invalidate."""
    with _CACHE_LOCK:
        if key in _CACHE:
            return _CACHE[key]
    value = produce()
    with _CACHE_LOCK:
        _CACHE[key] = value
    return value


def _url(*parts: str) -> str:
    return "/".join([BASE_URL] + [str(p).strip("/") for p in parts if str(p).strip("/")])


def _fetch_text(url: str, limit: int = 4_000_000) -> str:
    """GET a datastore text file, or "" when it is absent. Metadata files are optional in
    practice (an older collection may lack a MANIFEST), so a miss must not be fatal."""
    def produce():
        try:
            _validate_url(url)
            return _get_bytes(url, limit).decode("utf-8", "replace")
        except Exception:  # noqa: BLE001 - absent/unreadable metadata degrades, never raises
            return ""
    return _cached(f"text:{url}", produce)


def _list_dir(path: str):
    """List one datastore directory. The service renders an h5ai index whose no-JS
    fallback is a plain <a href> table, which is what we parse. Returns (dirs, files)."""
    def produce():
        url = _url(path) + "/"
        try:
            _validate_url(url)
            html = _get(url, accept="text/html")
        except (BlockedURLError, Exception):  # noqa: BLE001
            return [], []
        prefix = "/" + path.strip("/") + "/" if path.strip("/") else "/"
        dirs, files = [], []
        for href in re.findall(r'href="([^"]+)"', html):
            if not href.startswith(prefix) or href == prefix:
                continue
            rest = href[len(prefix):]
            if not rest or "/" in rest.rstrip("/"):
                continue          # a grandchild or the parent link, not a direct child
            (dirs if rest.endswith("/") else files).append(rest.rstrip("/"))
        return sorted(set(dirs)), sorted(set(files))
    return _cached(f"dir:{path}", produce)


def _yaml_docs(text: str):
    """Parse a datastore YAML file into dicts. PyYAML is already a dependency."""
    if not text.strip():
        return []
    try:
        import yaml
        return [d for d in yaml.safe_load_all(text) if isinstance(d, dict)]
    except Exception:  # noqa: BLE001 - a malformed README must not sink the whole listing
        return []


def _readme(coll_path: str, identifier: str) -> dict:
    docs = _yaml_docs(_fetch_text(_url(coll_path, f"README.{identifier}.yml")))
    return docs[0] if docs else {}


def _checksum_files(coll_path: str, identifier: str):
    """The authoritative file list. Unlike the HTML index this DOES include the
    .fai/.tbi/.gzi index siblings, which is what makes access modes knowable."""
    text = _fetch_text(_url(coll_path, f"CHECKSUM.{identifier}.md5"))
    names = []
    for line in text.splitlines():
        parts = line.split(None, 1)
        if len(parts) == 2:
            names.append(parts[1].strip().lstrip("./"))
    return names


def _manifest_map(coll_path: str, identifier: str) -> dict:
    """MANIFEST files are a YAML *list* of {name, description} - safe_load_all yields the
    list as one document, so flatten whichever shape we get."""
    text = _fetch_text(_url(coll_path, f"MANIFEST.{identifier}.yml"))
    if not text.strip():
        return {}
    try:
        import yaml
        loaded = list(yaml.safe_load_all(text))
    except Exception:  # noqa: BLE001
        return {}
    out = {}
    for doc in loaded:
        entries = doc if isinstance(doc, list) else [doc]
        for e in entries:
            if isinstance(e, dict) and e.get("name"):
                out[str(e["name"])] = str(e.get("description") or "").strip()
    return out


def _access_mode(name: str, present: set):
    """How can the existing toolset read this file? Returns (tool, how) or (None, why not).

    Decided by which index siblings the collection actually ships, which is the only
    honest signal — extension alone would promise random access the server cannot serve."""
    for suffix, (tool, how) in _ACCESS_BY_INDEX.items():
        if name + suffix in present:
            if suffix in (".tbi", ".csi") and name.endswith(_VARIANT_EXT):
                return "bcftools", "bcftools(args=['view','-H','-r','contig:start-end',URL])"
            return tool, how
    return None, "no index published - not randomly accessible; whole-file download only"


# --- lis_find -------------------------------------------------------------------------
def _species_description(genus: str, species: str) -> dict:
    docs = _yaml_docs(_fetch_text(
        _url(genus, species, "about_this_collection", f"description_{genus}_{species}.yml")))
    return docs[0] if docs else {}


def _find(args) -> str:
    genus = (args.get("genus") or "").strip()
    species = (args.get("species") or "").strip()
    taxon = (args.get("taxon") or "").strip()
    ctype = (args.get("type") or "").strip()
    query = (args.get("query") or "").strip().lower()
    limit = max(1, min(int(args.get("max_results") or MAX_RESULTS), 25))

    if taxon and not genus:
        bits = taxon.replace("_", " ").split()
        if bits:
            genus = bits[0].capitalize()
        if len(bits) > 1 and not species:
            species = bits[1].lower()

    if not genus:
        genera, _ = _list_dir("")
        genera = [g for g in genera if g[:1].isupper()]
        return ("LIS Data Store genera (pass one as 'genus', or a full 'taxon' like "
                f"'Glycine max'):\n  " + ", ".join(genera))

    if not species:
        specs, _ = _list_dir(genus)
        return (f"{genus}: {len(specs)} species/collection group(s):\n  " + ", ".join(specs)
                + "\n\nPass one as 'species' to see its data types.")

    if not ctype:
        types, _ = _list_dir(f"{genus}/{species}")
        desc = _species_description(genus, species)
        head = f"{genus} {species}"
        if desc.get("commonName"):
            head += f" ({desc['commonName']})"
        if desc.get("taxid"):
            head += f"  taxid:{desc['taxid']}"
        if desc.get("abbrev"):
            head += f"  abbrev:{desc['abbrev']}"
        return (head + f"\n{len(types)} data type(s):\n  " + ", ".join(types)
                + "\n\nPass one as 'type' to list its collections (with publication DOIs).")

    base = f"{genus}/{species}/{ctype}"
    colls, _ = _list_dir(base)
    if not colls:
        types, _ = _list_dir(f"{genus}/{species}")
        return (f"no collections under {base}. Available types for {genus} {species}: "
                + ", ".join(types))
    if query:
        colls = [c for c in colls if query in c.lower()]
        if not colls:
            return f"no collection under {base} matching {query!r}."
    shown, total = colls[:limit], len(colls)

    # READMEs are one fetch each; bounded by `limit` and fetched concurrently.
    with ThreadPoolExecutor(max_workers=6) as pool:
        readmes = list(pool.map(lambda c: _readme(f"{base}/{c}", c), shown))

    lines = [f"{total} collection(s) under {base}"
             + (f" matching {query!r}" if query else "")
             + (f"; showing {len(shown)}" if total > len(shown) else "") + ":"]
    for coll, rm in zip(shown, readmes):
        lines.append(f"\n  {coll}")
        lines.append(f"    path: {base}/{coll}")
        if rm.get("synopsis"):
            lines.append(f"    synopsis: {rm['synopsis']}")
        geno = rm.get("genotype")
        if geno:
            lines.append(f"    genotype: {', '.join(map(str, geno)) if isinstance(geno, list) else geno}")
        if rm.get("publication_doi"):
            lines.append(f"    publication_doi: {rm['publication_doi']}"
                         "   -> openalex_by_doi / read_paper")
        if rm.get("source"):
            lines.append(f"    source: {rm['source']}")
    lines.append("\nPass a 'path' above to lis_files to see what is randomly accessible.")
    return _cap("\n".join(lines))


# --- lis_files ------------------------------------------------------------------------
def _normalize_collection(spec: str):
    """Accept a datastore path, or a full URL under the datastore, and return
    (path, identifier) or (None, error)."""
    spec = (spec or "").strip().rstrip("/")
    if not spec:
        return None, ("error: missing 'collection' — a datastore path like "
                      "'Glycine/max/annotations/Wm82.gnm4.ann1.T8TQ' (lis_find prints these).")
    if spec.startswith("http://") or spec.startswith("https://"):
        if not spec.startswith(BASE_URL):
            return None, f"error: only URLs under {BASE_URL} are supported."
        spec = spec[len(BASE_URL):].strip("/")
    parts = [p for p in spec.split("/") if p]
    if len(parts) < 2:
        return None, (f"error: {spec!r} is not a collection path. Expected "
                      "'<Genus>/<species>/<type>/<collection>'.")
    return "/".join(parts), parts[-1]


def _files(args) -> str:
    path, identifier = _normalize_collection(args.get("collection"))
    if path is None:
        return identifier
    names = _checksum_files(path, identifier)
    if not names:
        return (f"error: no CHECKSUM.{identifier}.md5 under {path} — check the path "
                "(lis_find prints exact paths).")
    present = set(names)
    manifest = _manifest_map(path, identifier)
    rm = _readme(path, identifier)

    index_suffixes = tuple(_ACCESS_BY_INDEX) + (".gzi", ".md5")
    data = [n for n in names
            if not n.endswith(index_suffixes)
            and not os.path.basename(n).startswith(("README.", "MANIFEST.", "CHANGES.",
                                                    "CHECKSUM."))]
    head = [f"collection: {identifier}", f"path: {path}"]
    if rm.get("synopsis"):
        head.append(f"synopsis: {rm['synopsis']}")
    if rm.get("publication_doi"):
        head.append(f"publication_doi: {rm['publication_doi']}   -> openalex_by_doi / read_paper")
    head.append(f"{len(data)} data file(s); base URL {_url(path)}/")

    addressable, plain = [], []
    for name in sorted(data):
        tool, how = _access_mode(name, present)
        desc = manifest.get(os.path.basename(name), "")
        (addressable if tool else plain).append((name, tool, how, desc))

    lines = head + ["", f"RANDOMLY ACCESSIBLE ({len(addressable)}) — stream a region "
                        "without downloading the file:"]
    for name, tool, how, desc in addressable:
        lines.append(f"  {name}")
        if desc and desc != "MISSING":
            lines.append(f"      {desc}")
        lines.append(f"      via {tool}: {how}")
        lines.append(f"      url {_url(path, name)}")
    lines.append("")
    lines.append(f"NOT INDEXED ({len(plain)}) — no .fai/.tbi published, so these cannot be "
                 "region-queried or read through this toolset; they are whole-file "
                 "downloads only:")
    for name, _t, _h, desc in plain:
        lines.append(f"  {name}" + (f"   — {desc}" if desc and desc != "MISSING" else ""))
    return _cap("\n".join(lines))


# --- lis_gene -------------------------------------------------------------------------
def _resolve_gene_collection(gene: str, collection: str):
    """Work out which annotation collection to search. An explicit collection wins; else
    derive it from a fully-qualified gene ID.

    LIS abbreviations are the first three letters of the genus plus the first two of the
    species (glyma = Glycine max, phavu = Phaseolus vulgaris), so the species resolves
    with two directory listings rather than a scan of every description file."""
    if collection:
        return _normalize_collection(collection)
    m = _QUALIFIED_GENE_RE.match(gene)
    if not m:
        return None, ("error: pass 'collection' (a path from lis_find), or a fully "
                      "qualified gene ID like 'glyma.Wm82.gnm4.ann1.Glyma.12G040000'.")
    abbrev, stem = m.group("abbrev"), m.group("stem")
    genera, _ = _list_dir("")
    for genus in genera:
        if not genus[:1].isupper() or not abbrev.startswith(genus[:3].lower()):
            continue
        species_list, _ = _list_dir(genus)
        for sp in species_list:
            if abbrev != (genus[:3] + sp[:2]).lower():
                continue
            colls, _ = _list_dir(f"{genus}/{sp}/annotations")
            for c in colls:
                if c.startswith(stem + "."):
                    return f"{genus}/{sp}/annotations/{c}", c
            return None, (f"error: no annotation collection {stem}.* under "
                          f"{genus}/{sp}/annotations.")
    return None, (f"error: could not map abbreviation {abbrev!r} to a species. Pass "
                  "'collection' explicitly (lis_find prints paths).")



def _bed_url(path: str, identifier: str, names) -> str:
    for n in names:
        if n.endswith(".gene_models_main.bed.gz"):
            return _url(path, n)
    return ""


def _gene(args) -> str:
    gene = (args.get("gene") or "").strip()
    if not gene:
        return "error: missing 'gene' — an exact gene or mRNA ID, e.g. 'Glyma.12G040000'."
    path, identifier = _resolve_gene_collection(gene, (args.get("collection") or "").strip())
    if path is None:
        return identifier

    names = _checksum_files(path, identifier)
    if not names:
        return f"error: no CHECKSUM.{identifier}.md5 under {path} — check the collection."
    bed_url = _bed_url(path, identifier, names)
    if not bed_url:
        return (f"error: {identifier} publishes no gene_models_main.bed.gz, so gene "
                "lookup is unavailable for this collection.")

    def produce():
        try:
            _validate_url(bed_url)
            raw = _get_bytes(bed_url, BED_MAX_BYTES)
            return gzip.GzipFile(fileobj=io.BytesIO(raw)).read().decode("utf-8", "replace")
        except Exception as e:  # noqa: BLE001
            return f"\x00error {type(e).__name__}: {e}"
    bed = _cached(f"bed:{bed_url}", produce)
    if bed.startswith("\x00"):
        return f"error: could not read {os.path.basename(bed_url)}: {bed[1:]}"

    # Exact-ID matching only: the ID must equal the mRNA (col 4) or gene (col 7) field, or
    # the same with the collection prefix stripped. Substring matching would silently
    # return Glyma.12G0400001 for Glyma.12G040000.
    hits = []
    for line in bed.splitlines():
        f = line.split("\t")
        if len(f) < 6:
            continue
        mrna, geneid = f[3], (f[6] if len(f) > 6 else "")
        cands = {mrna, geneid}
        cands |= {_GENE_PREFIX_RE.sub("", c) for c in (mrna, geneid) if c}
        if gene in cands:
            hits.append((f[0], int(f[1]) + 1, int(f[2]), f[5], mrna, geneid))
    if not hits:
        return (f"no exact match for {gene!r} in {identifier}. This tool matches exact "
                "gene/mRNA IDs only (symbols like 'GmNARK' are not resolved yet) — check "
                "the ID, or try the unprefixed form (e.g. 'Glyma.12G040000').")

    contig, start, end, strand = hits[0][0], min(h[1] for h in hits), max(h[2] for h in hits), hits[0][3]
    seq_names = sorted({h[4] for h in hits})
    # Bounds come from gene_models_main.bed, which lists mRNA rows; they can sit just
    # inside the GFF's `gene` feature. Say so rather than implying an exact gene extent —
    # the GFF call below is the authority on feature boundaries.
    lines = [f"{gene} in {identifier}",
             f"  locus:  {contig}:{start:,}-{end:,} ({strand})   "
             f"[mRNA extent from gene_models_main.bed; "
             f"{len(hits)} model(s): {', '.join(seq_names)}]",
             f"  region string for tabix_query/samtools: {contig}:{start}-{end}"]
    for label, suffix in (("protein", ".protein_primary.faa.gz"),
                          ("CDS", ".cds_primary.fna.gz")):
        hit = next((n for n in names if n.endswith(suffix)), None)
        if hit and f"{hit}.fai" in set(names):
            lines.append(f"  {label}: fasta_fetch(path='{_url(path, hit)}', "
                         f"region='{seq_names[0]}')")
    gff = next((n for n in names if n.endswith(".gene_models_main.gff3.gz")), None)
    if gff and f"{gff}.tbi" in set(names):
        lines.append(f"  models: tabix_query(path='{_url(path, gff)}', "
                     f"region='{contig}:{start}-{end}')")
    rm = _readme(path, identifier)
    if rm.get("publication_doi"):
        lines.append(f"  publication_doi: {rm['publication_doi']}   -> openalex_by_doi")
    return _cap("\n".join(lines))


# --- registry -------------------------------------------------------------------------
def _mk(name, description, params, sync_fn):
    async def run(a, _f=sync_fn):
        return await asyncio.to_thread(_f, a)
    return Tool(name=name, description=description, parameters=params,
                read_only=True, run=run)


def lis_tools() -> list:
    """Read-only tools for the LIS Data Store (https://data.legumeinfo.org)."""
    return [
        _mk("lis_find",
            "Discover data in the LIS Data Store (legume genomes/annotations/diversity/"
            "GWAS/…). Drills down: no args lists genera; {taxon} lists that species' data "
            "types; {taxon, type} lists collections with their synopsis, genotype and "
            "publication DOI. Use this first — collection names end in an arbitrary "
            "4-character key ('Wm82.gnm4.ann1.T8TQ') that cannot be guessed. "
            "Args: {taxon?, genus?, species?, type?, query?, max_results?}.",
            {"type": "object",
             "properties": {
                 "taxon": {"type": "string",
                           "description": "Species name, e.g. 'Glycine max' or 'Phaseolus vulgaris'."},
                 "genus": {"type": "string", "description": "Genus alone, e.g. 'Glycine'."},
                 "species": {"type": "string", "description": "Species epithet, e.g. 'max'."},
                 "type": {"type": "string",
                          "description": "Data type: genomes, annotations, diversity, gwas, "
                                         "expression, markers, qtl, synteny, …"},
                 "query": {"type": "string",
                           "description": "Substring filter on collection names, e.g. 'Wm82' or '2021'."},
                 "max_results": {"type": "integer", "description": "Collections to detail, 1–25 (default 10)."}},
             "additionalProperties": False}, _find),
        _mk("lis_files",
            "List a LIS collection's files and — the important part — which ones are "
            "RANDOMLY ACCESSIBLE over HTTP, with the exact call to read them. The store's "
            "directory listing hides the .fai/.tbi index siblings, so this is the only way "
            "to know that a 286 MB genome can be region-queried without downloading it. "
            "Also reports files that are NOT indexed and therefore unreadable through this "
            "toolset. Args: {collection} — a path from lis_find, e.g. "
            "'Glycine/max/annotations/Wm82.gnm4.ann1.T8TQ'.",
            {"type": "object",
             "properties": {"collection": {"type": "string",
                                           "description": "Datastore path '<Genus>/<species>/<type>/"
                                                          "<collection>', or a full URL under the store."}},
             "required": ["collection"], "additionalProperties": False}, _files),
        _mk("lis_gene",
            "Look up an exact gene or mRNA ID in a LIS annotation and return its locus "
            "plus ready-to-use calls for its protein/CDS sequence and gene models. Bridges "
            "the gap that tabix_query needs coordinates, not names. Exact IDs only "
            "(e.g. 'Glyma.12G040000'); gene symbols like 'GmNARK' are not resolved. "
            "Args: {gene, collection?} — collection may be omitted if the ID is fully "
            "qualified ('glyma.Wm82.gnm4.ann1.Glyma.12G040000').",
            {"type": "object",
             "properties": {"gene": {"type": "string",
                                     "description": "Exact gene or mRNA ID, prefixed or not."},
                            "collection": {"type": "string",
                                           "description": "Annotation collection path from lis_find."}},
             "required": ["gene"], "additionalProperties": False}, _gene),
    ]
