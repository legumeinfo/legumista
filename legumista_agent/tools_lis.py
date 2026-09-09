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
  name into a locus, and the loci live in this file. The catalog supplies its URL.
* the annotation's synonym file -- superseded gene IDs, likewise a data file whose URL
  comes from the catalog.

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

from .tool import Tool
from .tools_catalog import catalog_stamp, catalog_unavailable, controller
from .tools_native import _cap, _get_bytes, _validate_url

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

    if taxon and not genus:
        bits = taxon.replace("_", " ").split()
        if bits:
            genus = bits[0].capitalize()
        if len(bits) > 1 and not species:
            species = bits[1].lower()

    stamp = catalog_stamp(ctl)

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
    if query:
        colls = [c for c in colls if query in c["id"].lower()]
        if not colls:
            return f"no collection under {base} matching {query!r}. {stamp}"
    total = len(colls)
    shown = sorted(colls, key=lambda c: c["id"])[:limit]

    lines = [f"{total} collection(s) under {base}"
             + (f" matching {query!r}" if query else "")
             + (f"; showing {len(shown)}" if total > len(shown) else "") + ":"]
    for record in shown:
        lines.append(f"\n  {record['id']}")
        lines.append(f"    path: {record['path']}")
        if record.get("synopsis"):
            lines.append(f"    synopsis: {record['synopsis']}")
        genotype = record.get("genotype")
        if genotype:
            joined = (", ".join(map(str, genotype))
                      if isinstance(genotype, list) else genotype)
            lines.append(f"    genotype: {joined}")
        if record.get("publication_doi"):
            lines.append(f"    publication_doi: {record['publication_doi']}"
                         "   -> openalex_by_doi / read_paper")
        if record.get("source"):
            lines.append(f"    source: {record['source']}")
    lines.append("\nPass a 'path' above to lis_files to see what is randomly accessible.")
    lines.append(stamp)
    return _cap("\n".join(lines))


# --- lis_files ------------------------------------------------------------------------
def _files(args) -> str:
    record, err = _lookup(args.get("collection"))
    if record is None:
        return err
    ctl = controller()
    data = record.get("files", [])

    head = [f"collection: {record['id']}", f"path: {record['path']}"]
    if record.get("synopsis"):
        head.append(f"synopsis: {record['synopsis']}")
    if record.get("publication_doi"):
        head.append(f"publication_doi: {record['publication_doi']}"
                    "   -> openalex_by_doi / read_paper")
    if record.get("license"):
        head.append(f"license: {record['license']}")
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

    addressable, plain = [], []
    for entry in sorted(data, key=lambda f: f["n"]):
        tool, how = _access_for(entry)
        (addressable if tool else plain).append(
            (entry["n"], tool, how, entry.get("description", "")))

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
                 "be region-queried or read through this toolset; they are whole-file "
                 "downloads only:")
    for name, _tool, _how, desc in plain:
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

    A data file, not a metadata endpoint: its URL comes from the catalog, but the
    mapping itself exists only inside the file.
    """
    name = next((f["n"] for f in record.get("files", [])
                 if f["n"].endswith(_SYNONYM_SUFFIXES)), None)
    if not name:
        return {}
    text, err = _fetch_gz_text(_file_url(record, name))
    if err:
        return {}
    index = {}
    for line in text.splitlines():
        if line.startswith("#"):
            continue
        parts = line.split("\t")
        if len(parts) >= 2 and parts[0].strip() and parts[1].strip():
            index.setdefault(parts[1].strip().lower(), parts[0].strip())
    return index


def _alias_candidates(gene, record):
    """Yield (candidate_id, how_it_was_found) for a query that matched nothing exactly."""
    ctl = controller()
    if ctl is not None:
        abbrev = record.get("scientific_name_abbrev", "")
        for entry in ctl.resolve_symbol(gene, abbrev):
            note = (f"curated symbol {gene!r} "
                    f"({entry['abbrev']}.traits.yml, carried in the catalog)")
            if entry.get("doi"):
                note += f" (publication_doi: {entry['doi']})"
            if entry.get("synopsis"):
                note += f"\n  synopsis: {entry['synopsis']}"
            yield entry["gene"], note
    current = _synonyms(record).get(gene.lower())
    if current:
        yield current, (f"superseded ID {gene!r} -> {current} via the collection's "
                        "synonym file")


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


def _gene(args) -> str:
    gene = (args.get("gene") or "").strip()
    if not gene:
        return "error: missing 'gene' — a gene ID, mRNA ID, or curated symbol."
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

    label, provenance = gene, ""
    hits = _bed_hits(bed, gene)
    if not hits:
        for candidate, note in _alias_candidates(gene, record):
            found = _bed_hits(bed, candidate)
            if found:
                hits, label, provenance = found, candidate, note
                break
    if not hits:
        return (f"no match for {gene!r} in {record['id']}. Consulted: exact gene/mRNA "
                "ID, the catalog's curated symbols, and the collection's synonym file. "
                "Note a name from a different assembly (e.g. an A17.gnm5 ID against a "
                "gnm4 annotation) is not a synonym and cannot be resolved here — use "
                f"lis_find to pick the matching collection. {catalog_stamp(ctl)}")

    contig = hits[0][0]
    start, end = min(h[1] for h in hits), max(h[2] for h in hits)
    strand = hits[0][3]
    seq_names = sorted({h[4] for h in hits})

    lines = [f"{label} in {record['id']}"
             + (f"\n  resolved from: {provenance}" if provenance else ""),
             f"  locus:  {contig}:{start:,}-{end:,} ({strand})   "
             f"[mRNA extent from gene_models_main.bed; "
             f"{len(hits)} model(s): {', '.join(seq_names)}]",
             f"  region string for tabix_query/samtools: {contig}:{start}-{end}"]
    for label_text, suffix in (("protein", ".protein_primary.faa.gz"),
                               ("CDS", ".cds_primary.fna.gz")):
        hit = next((n for n in by_name if n.endswith(suffix)), None)
        if hit and ".fai" in (by_name[hit].get("i") or []):
            lines.append(f"  {label_text}: fasta_fetch("
                         f"path='{_file_url(record, hit)}', region='{seq_names[0]}')")
    gff = next((n for n in by_name if n.endswith(".gene_models_main.gff3.gz")), None)
    if gff and ".tbi" in (by_name[gff].get("i") or []):
        lines.append(f"  models: tabix_query(path='{_file_url(record, gff)}', "
                     f"region='{contig}:{start}-{end}')")
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


def _parse_blocks(text, region_contig, lo, hi, cap):
    """DAGchainer syntenic_region rows, optionally filtered on the A-side interval."""
    blocks, total = [], 0
    for line in text.splitlines():
        if line.startswith("#"):
            continue
        fields = line.split("\t")
        if len(fields) < 9 or fields[2] != "syntenic_region":
            continue
        a_contig, a_start, a_end = fields[0], int(fields[3]), int(fields[4])
        if region_contig:
            if a_contig != region_contig:
                continue
            if lo is not None and (a_end < lo or a_start > hi):
                continue
        total += 1
        if len(blocks) >= cap:
            continue
        match = _BLOCK_RE.match(fields[8])
        if not match:
            continue
        blocks.append({
            "a": f"{a_contig}:{a_start}-{a_end}",
            "b": f"{match.group('b')}:{match.group('start')}-{match.group('end')}",
            "strand": fields[6],
            "score": fields[5],
            "ks": match.group("ks"),
        })
    return blocks, total


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

    out = [f"{genome}" + (f" x {partner}" if partner else "")
           + (f"  region {region}" if region else "")]
    for pair in candidates[:6]:
        text, err = _fetch_gz_text(pair["url"])
        if err:
            out.append(f"\n  {pair['b']}: error reading {pair['file']}: {err}")
            continue
        blocks, total = _parse_blocks(text, contig, lo, hi, cap)
        header = f"\n  {genome} x {pair['b']}"
        if pair.get("epoch"):
            header += f" ({pair['epoch']})"
        if pair["direction"] == "query":
            header += "  [stored under the partner's collection; A-side normalised]"
        out.append(header)
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


def lis_tools() -> list:
    """Read-only tools for the LIS Data Store, served from the resident catalog."""
    return [
        _mk("lis_find",
            "Discover data in the LIS Data Store (legume genomes/annotations/diversity/"
            "GWAS/…) from a resident catalog — no network. Drills down: no args lists "
            "genera; {taxon} lists that species' data types; {taxon, type} lists "
            "collections with their synopsis, genotype and publication DOI. Use this "
            "first — collection names end in an arbitrary 4-character key "
            "('Wm82.gnm4.ann1.T8TQ') that cannot be guessed. "
            "Args: {taxon?, genus?, species?, type?, query?, max_results?}.",
            {"type": "object",
             "properties": {
                 "taxon": {"type": "string",
                           "description": "Species name, e.g. 'Glycine max'."},
                 "genus": {"type": "string",
                           "description": "Genus alone, e.g. 'Glycine'."},
                 "species": {"type": "string",
                             "description": "Species epithet, e.g. 'max'."},
                 "type": {"type": "string",
                          "description": "Data type: genomes, annotations, diversity, "
                                         "gwas, expression, markers, qtl, synteny, …"},
                 "query": {"type": "string",
                           "description": "Substring filter on collection names."},
                 "max_results": {"type": "integer",
                                 "description": "Collections to detail, 1–25 "
                                                "(default 10)."}},
             "additionalProperties": False}, _find),
        _mk("lis_files",
            "List a LIS collection's files and — the important part — which ones are "
            "RANDOMLY ACCESSIBLE over HTTP, with the exact call to read them. Answers "
            "from the resident catalog, so it is instant and complete. Reports files "
            "that are NOT indexed and therefore unreadable through this toolset, and "
            "distinguishes both from a collection whose index status is UNKNOWN because "
            "it publishes no CHECKSUM. Args: {collection} — a path from lis_find, e.g. "
            "'Glycine/max/annotations/Wm82.gnm4.ann1.T8TQ', or a bare collection id.",
            {"type": "object",
             "properties": {"collection": {"type": "string",
                                           "description": "Datastore path, collection "
                                                          "id, or full datastore URL."}},
             "required": ["collection"], "additionalProperties": False}, _files),
        _mk("lis_gene",
            "Look up a gene in a LIS annotation and return its locus plus ready-to-use "
            "calls for its protein/CDS sequence and gene models. Bridges the gap that "
            "tabix_query needs coordinates, not names. Accepts an exact ID "
            "('Glyma.12G040000'), a curated gene symbol ('GmNARK', resolved from the "
            "catalog), or a superseded ID ('Glyma01g00210', resolved from the "
            "collection's synonym file); the reply says which route resolved it. A name "
            "from a DIFFERENT assembly is not a synonym and will not resolve — use "
            "lis_find to pick the right collection. Args: {gene, collection?}.",
            {"type": "object",
             "properties": {"gene": {"type": "string",
                                     "description": "Gene ID, mRNA ID, or curated "
                                                    "symbol."},
                            "collection": {"type": "string",
                                           "description": "Annotation collection path "
                                                          "or id."}},
             "required": ["gene"], "additionalProperties": False}, _gene),
        _mk("lis_synteny",
            "Syntenic blocks and whole-genome alignments between legume assemblies. With "
            "just {genome} or {gene}, lists every partner that assembly is paired with — "
            "including pairs stored under the OTHER genome's collection, which a "
            "directory listing would miss. Add {partner} and optionally {region} "
            "(A-side, samtools-style) for the blocks themselves, with score and "
            "median_Ks. Synteny is published for one, usually OLD, assembly per species "
            "(soybean: Wm82.gnm2, not gnm4); if you ask about an assembly without it, "
            "the reply names the one that has it. Args: {genome?, gene?, partner?, "
            "region?, max_blocks?}.",
            {"type": "object",
             "properties": {
                 "genome": {"type": "string",
                            "description": "Assembly, e.g. 'glyma.Wm82.gnm2'."},
                 "gene": {"type": "string",
                          "description": "Gene ID; its assembly is used."},
                 "partner": {"type": "string",
                             "description": "Restrict to one partner assembly."},
                 "region": {"type": "string",
                            "description": "A-side region, 'contig' or "
                                           "'contig:start-end' (1-based inclusive)."},
                 "max_blocks": {"type": "integer",
                                "description": "Block cap per pair (default 200)."}},
             "additionalProperties": False}, _synteny),
    ]
