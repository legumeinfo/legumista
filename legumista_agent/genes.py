#!/usr/bin/env python3
"""Gene selectors: a list of genes named by a short, reproducible definition.

Tools that take genes (`lis_gene`, `extract_features`, `browser_link`) accept one
argument, a selector, in one of four forms:

    {"ids": ["GmNARK", "Glyma.12G040000"], "collection": "Wm82.gnm4.ann1.T8TQ"}
    {"region": "glyma.Wm82.gnm4.Gm12:2800000-3000000"}
    {"family": "legume.fam3.12584", "collection": "Wm82.gnm4.ann1.T8TQ"}
    {"ncbi": ["LOC112749796"], "collection": "Tifrunner.gnm2.ann2.PVFB"}

plus, on any form, `"translate_to": "<annotation>"` and `"offset": N`.

Nothing is stored. A selector is resolved afresh on every call, against the resident
catalog and the annotation's own data files, so it needs no session, survives reconnects
and restarts, and means the same thing when copied out of a month-old chat. (An earlier
design held gene sets server-side by handle; the definition is the better handle.)

Rules, on every resolution:

- **Canonical.** Each gene becomes its fully qualified LIS ID with its locus, strand and
  models, read from the annotation's `gene_models_main.bed.gz`.
- **Every input is accounted for.** Inputs are sorted into resolved, resolved through a
  symbol or a superseded ID, not found, and refused, naming the route that ran.
- **One selection, one annotation.** `translate_to` is the only way across.
- **Deterministic.** Genes come back in genome order, so `offset` paging is stable.

Two data files are cached in process (the gene-model BED, keyed by URL and catalog
commit) and one on disk (gene-family assignments, in SQLite under the cache directory),
because both are re-read constantly and change only with the catalog.
"""
import os
import re
import sqlite3
import threading
import urllib.parse
from collections import OrderedDict
from dataclasses import dataclass, field

import config

from . import tools_lis
from .results import count_phrase
from .tools_catalog import catalog_stamp, catalog_unavailable, controller
from .tools_native import _get, _get_bytes, _validate_url

MAX_GENES = int(os.environ.get("LEGUMISTA_SELECTOR_MAX", "200"))
FAMILY_CACHE_FILES = int(os.environ.get("LEGUMISTA_FAMILY_CACHE_MAX", "64"))
_BED_CACHE_ENTRIES = 4

_BED_SUFFIX = ".gene_models_main.bed.gz"
# Prefer the newer family set when a family id does not say which one it is from.
_FAMILY_SETS = ("legume.fam3", "legfed_v1_0")
_GENOME_PREFIX_RE = re.compile(r"^([a-z]{4,6}\.[A-Za-z0-9_-]+\.gnm\d+)\.")


@dataclass
class Gene:
    """One gene, canonical. Coordinates are 1-based inclusive and span the gene's CDS:
    `gene_models_main.bed` records the coding extent, not the UTRs."""
    id: str
    contig: str
    start: int
    end: int
    strand: str
    models: list = field(default_factory=list)

    @property
    def name(self) -> str:
        return tools_lis._GENE_PREFIX_RE.sub("", self.id)


@dataclass
class Selection:
    record: dict = None             # the annotation collection
    genes: list = field(default_factory=list)
    total: int = 0                  # genes the selector matched, before offset/cap
    lines: list = field(default_factory=list)   # the accounting, one line per input
    error: str = ""                 # set when nothing could be resolved at all
    incomplete: bool = False        # a route that could have matched did not run
    mapping: list = field(default_factory=list)  # translation rows
    mapping_title: str = "translation"
    direct: int = 0                 # inputs that matched a gene or model ID outright

    def summary(self, offset: int = 0, max_mapping: int = 50) -> str:
        """The selection and its accounting. Inputs that matched an ID outright are
        counted; every other outcome (symbol, synonym, miss, refusal) gets its own line."""
        shown = len(self.genes)
        span = (f"genes {offset + 1}–{offset + shown} of {self.total}"
                if self.total > shown else f"{self.total} gene(s)")
        head = f"selection: {span} in {self.record['id']}"
        if self.incomplete:
            head += " — INCOMPLETE (a route was NOT CHECKED; see below)"
        if offset + shown < self.total:
            head += f"; pass offset={offset + shown} for the next page"
        lines = [head]
        if self.direct:
            lines.append(f"  {self.direct} input(s) matched a gene or model ID directly")
        lines += [f"  {line}" for line in self.lines]
        if self.mapping:
            lines.append(f"  {self.mapping_title} ({count_phrase(min(len(self.mapping), max_mapping), len(self.mapping), 'source gene(s)')}):")
            lines += [f"    {src} -> {dst or '(none)'}   [{route}]"
                      for src, dst, route in self.mapping[:max_mapping]]
        return "\n".join(lines)


# --- the gene-model BED, indexed ------------------------------------------------------
_BED_CACHE: "OrderedDict[tuple, dict]" = OrderedDict()
_BED_LOCK = threading.Lock()


def _commit() -> str:
    ctl = controller()
    return (ctl.provenance().get("source_commit") if ctl is not None else "") or ""


def bed_index(record):
    """The annotation's gene models, indexed. Returns (index, error_text).

    index = {"genes": {gene_id: Gene}, "alias": {name_or_model: gene_id},
             "by_contig": {contig: [Gene, ...] in start order}}"""
    name = next((f["n"] for f in record.get("files", []) if f["n"].endswith(_BED_SUFFIX)),
                None)
    if not name:
        return None, (f"error: {record['id']} publishes no gene_models_main.bed.gz, so its "
                      "genes cannot be located.")
    url = tools_lis._file_url(record, name)
    key = (url, _commit())
    with _BED_LOCK:
        if key in _BED_CACHE:
            _BED_CACHE.move_to_end(key)
            return _BED_CACHE[key], None
    text, err = tools_lis._fetch_gz_text(url)
    if err:
        return None, f"error: could not read {name}: {err}"
    genes = {}
    for line in text.splitlines():
        fields = line.split("\t")
        if len(fields) < 6 or line.startswith(("#", "track", "browser")):
            continue
        contig, start, end, model, strand = (fields[0], int(fields[1]) + 1, int(fields[2]),
                                             fields[3], fields[5])
        gene_id = fields[6] if len(fields) > 6 and fields[6] else model.rsplit(".", 1)[0]
        gene = genes.get(gene_id)
        if gene is None:
            genes[gene_id] = Gene(gene_id, contig, start, end, strand, [model])
        else:
            gene.start, gene.end = min(gene.start, start), max(gene.end, end)
            gene.models.append(model)
    alias, by_contig, short = {}, {}, {}
    for gene in genes.values():
        bare_models = [tools_lis._GENE_PREFIX_RE.sub("", m) for m in gene.models]
        for key_name in [gene.id, gene.name] + gene.models + bare_models:
            alias.setdefault(key_name.lower(), gene.id)
        # The genus mines can drop the name's leading 'Prefix.' token (ArachisMine's
        # 6J3HHE is Arahy.6J3HHE). Kept only where unambiguous.
        for name in [gene.name] + bare_models:
            if "." in name:
                short.setdefault(name.split(".", 1)[1].lower(), set()).add(gene.id)
        by_contig.setdefault(gene.contig, []).append(gene)
    for contig_genes in by_contig.values():
        contig_genes.sort(key=lambda g: (g.start, g.end, g.id))
    index = {"genes": genes, "alias": alias, "by_contig": by_contig,
             "unprefixed": {k: next(iter(v)) for k, v in short.items() if len(v) == 1}}
    with _BED_LOCK:
        _BED_CACHE[key] = index
        while len(_BED_CACHE) > _BED_CACHE_ENTRIES:
            _BED_CACHE.popitem(last=False)
    return index, None


def reset_caches():
    """For tests."""
    with _BED_LOCK:
        _BED_CACHE.clear()
    with _DB_LOCK:
        for conn in _DB.values():
            conn.close()
        _DB.clear()


def genome_order(genes):
    return sorted(genes, key=lambda g: (g.contig, g.start, g.end, g.id))


# --- gene-family assignments, cached in SQLite ----------------------------------------
_DB: dict = {}
_DB_LOCK = threading.Lock()


def _db():
    from .catalog_source import cache_dir

    path = str(cache_dir() / "families.sqlite")
    conn = _DB.get(path)
    if conn is None:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        conn = sqlite3.connect(path, check_same_thread=False)
        conn.executescript(
            "CREATE TABLE IF NOT EXISTS files (url TEXT PRIMARY KEY, commit_id TEXT,"
            " used REAL);"
            "CREATE TABLE IF NOT EXISTS rows (url TEXT, gene TEXT, family TEXT);"
            "CREATE INDEX IF NOT EXISTS rows_gene ON rows (url, gene);"
            "CREATE INDEX IF NOT EXISTS rows_family ON rows (url, family);")
        _DB[path] = conn
    return conn


def _family_file(record, family_set):
    """The annotation's assignment file for one family set, or None."""
    marker = f".{family_set.lower()}."
    return next((f["n"] for f in record.get("files", [])
                 if f["n"].endswith(".gfa.tsv.gz") and marker in f["n"].lower()), None)


def _load_families(record, family_set):
    """Ensure one family file is in the cache. Returns (url, error_text)."""
    import time

    name = _family_file(record, family_set)
    if not name:
        return None, f"{record['id']} publishes no {family_set} assignment file"
    url = tools_lis._file_url(record, name)
    commit = _commit()
    with _DB_LOCK:
        conn = _db()
        row = conn.execute("SELECT commit_id FROM files WHERE url = ?", (url,)).fetchone()
        if row and row[0] == commit:
            conn.execute("UPDATE files SET used = ? WHERE url = ?", (time.time(), url))
            conn.commit()
            return url, None
    text, err = tools_lis._fetch_gz_text(url)
    if err:
        return None, f"could not read {name}: {err}"
    rows = []
    for line in text.splitlines():
        if line.startswith("#"):
            continue
        parts = line.split("\t")
        if len(parts) >= 2 and parts[0] and parts[1]:
            rows.append((url, parts[0].strip(), parts[1].strip()))
    with _DB_LOCK:
        conn = _db()
        conn.execute("DELETE FROM rows WHERE url = ?", (url,))
        conn.executemany("INSERT INTO rows VALUES (?, ?, ?)", rows)
        conn.execute("INSERT OR REPLACE INTO files VALUES (?, ?, ?)",
                     (url, commit, time.time()))
        stale = conn.execute("SELECT url FROM files ORDER BY used DESC LIMIT -1 OFFSET ?",
                             (FAMILY_CACHE_FILES,)).fetchall()
        for (old,) in stale:
            conn.execute("DELETE FROM rows WHERE url = ?", (old,))
            conn.execute("DELETE FROM files WHERE url = ?", (old,))
        conn.commit()
    return url, None


def _family_members(url, family):
    with _DB_LOCK:
        return [r[0] for r in _db().execute(
            "SELECT DISTINCT gene FROM rows WHERE url = ? AND lower(family) = lower(?)"
            " ORDER BY gene", (url, family))]


def _families_of(url, gene):
    with _DB_LOCK:
        return [r[0] for r in _db().execute(
            "SELECT DISTINCT family FROM rows WHERE url = ? AND gene = ? ORDER BY family",
            (url, gene))]


def _family_set_of(family):
    low = family.lower()
    return next((s for s in _FAMILY_SETS if low.startswith(s + ".")), None)


# --- resolution -----------------------------------------------------------------------
def _annotation(spec, hint_gene=""):
    """The annotation collection for a selector. Returns (record, error_text)."""
    spec = (spec or "").strip()
    if spec:
        record, err = tools_lis._lookup(spec)
    else:
        record, err = tools_lis._annotation_for_gene(hint_gene, "")
    if record is not None and record.get("type") != "annotations":
        return None, (f"error: {record['id']} is a {record.get('type')} collection; a "
                      "selector needs an annotation collection.")
    return record, err


def _genome_of_record(record):
    """The assembly (abbrev.strain.gnmN) an annotation collection's genes sit on."""
    return tools_lis._genome_of(f"{record.get('scientific_name_abbrev', '')}.{record['id']}")


def _annotations_for_genome(genome):
    """Annotation collections whose genes sit on `genome` (abbrev.strain.gnmN)."""
    ctl = controller()
    out = [record for record in (ctl.collections if ctl else [])
           if record.get("type") == "annotations" and _genome_of_record(record) == genome]
    return sorted(out, key=lambda r: r["id"])


def _resolve_ids(ids, record, index, sel):
    """Resolve each input name; record what happened to it."""
    ctl = controller()
    synonyms, syn_status, syn_name = None, "", ""
    found = {}
    for raw in ids:
        name = str(raw or "").strip()
        if not name:
            continue
        qualified = tools_lis._QUALIFIED_GENE_RE.match(name)
        if qualified and not record["id"].startswith(qualified.group("stem") + "."):
            sel.lines.append(f"refused  {name}: from annotation {qualified.group('stem')}.*, "
                             f"not {record['id']} — one annotation per selection; use "
                             "translate_to to cross")
            continue
        gene_id = index["alias"].get(name.lower())
        if gene_id:
            found.setdefault(gene_id, name)
            sel.direct += 1
            continue
        gene_id = index["unprefixed"].get(tools_lis._GENE_PREFIX_RE.sub("", name).lower())
        if gene_id:
            found.setdefault(gene_id, name)
            sel.lines.append(f"resolved {name} -> {gene_id} (the name without its "
                             "prefix, as the genus mines spell it)")
            continue
        symbol_hits = [e for e in (ctl.resolve_symbol(name, record.get(
            "scientific_name_abbrev", "")) if ctl else [])
            if index["alias"].get(e["gene"].lower())]
        if symbol_hits:
            gene_id = index["alias"][symbol_hits[0]["gene"].lower()]
            found.setdefault(gene_id, name)
            sel.lines.append(f"resolved {name} -> {gene_id} (curated symbol, catalog)")
            continue
        if synonyms is None:
            synonyms, syn_status, syn_name = tools_lis._synonyms(record)
        if syn_status == "ok":
            current = synonyms.get(name.lower())
            gene_id = index["alias"].get((current or "").lower())
            if gene_id:
                found.setdefault(gene_id, name)
                sel.lines.append(f"resolved {name} -> {gene_id} (superseded ID, {syn_name})")
                continue
            sel.lines.append(f"not found {name}: checked gene/model IDs, curated symbols, "
                             f"and superseded IDs ({syn_name})")
        elif syn_status == "absent":
            sel.lines.append(f"not found {name}: checked gene/model IDs and curated symbols; "
                             "this collection publishes no synonym file")
        else:
            sel.incomplete = True
            sel.lines.append(f"NOT CHECKED {name}: no gene/model ID or symbol matched, and "
                             f"the synonym file could not be read ({syn_status[8:]}) — "
                             "report it as unverified, not absent")
    return [index["genes"][g] for g in found]


def _resolve_region(region, record, index, sel):
    contig, lo, hi, err = tools_lis._parse_region(region)
    if err:
        sel.error = err
        return []
    genes = index["by_contig"].get(contig)
    if genes is None:
        sel.error = (f"error: no gene of {record['id']} is on {contig!r}. Contig names carry "
                     "the assembly prefix, e.g. 'glyma.Wm82.gnm4.Gm12'.")
        return []
    lo = 1 if lo is None else lo
    hi = max(g.end for g in genes) if hi is None else hi
    hits = [g for g in genes if g.start <= hi and g.end >= lo]
    sel.lines.append(f"region {contig}:{lo}-{hi}: {len(hits)} gene(s) whose coding extent "
                     "overlaps it (from gene_models_main.bed)")
    return hits


def _resolve_family(family, record, index, sel):
    family_set = _family_set_of(family)
    if not family_set:
        sel.error = (f"error: {family!r} is not a family id this server knows — expected a "
                     "'legume.fam3.' or 'legfed_v1_0.' family (mine_gene_families "
                     "lists a gene's families).")
        return []
    url, err = _load_families(record, family_set)
    if err:
        sel.error = f"error: {err}"
        return []
    members = _family_members(url, family)
    genes, missing = [], []
    for member in members:
        gene_id = index["alias"].get(member.lower())
        (genes.append(index["genes"][gene_id]) if gene_id else missing.append(member))
    sel.lines.append(f"family {family}: {len(members)} member(s) in {record['id']} "
                     f"({os.path.basename(url)})")
    if missing:
        sel.lines.append(f"{len(missing)} member(s) have no gene model in "
                         f"gene_models_main.bed: {', '.join(missing[:5])}"
                         + (" …" if len(missing) > 5 else ""))
    return genes


# --- NCBI genes, placed by locus ------------------------------------------------------
# NCBI Gene names a gene's location on its own RefSeq/GenBank sequence. Where that
# sequence IS the LIS assembly (NCBI annotates many LIS genomes directly: its NC_092050.1
# is titled "... arahy.Tifrunner.gnm2.J5K5"), the coordinates are directly comparable,
# and NCBI's names (which tell chalcone from stilbene synthase where the LIS description
# does not) carry over by overlap. Sameness is checked, never assumed: a sequence maps
# to the LIS contig of identical length, and to nothing otherwise.
EUTILS = os.environ.get("LEGUMISTA_EUTILS_URL",
                        "https://eutils.ncbi.nlm.nih.gov/entrez/eutils").rstrip("/")
_NCBI_ID_RE = re.compile(r"^(?:LOC|GeneID:)?(\d+)$", re.IGNORECASE)


def _esummary(db, ids):
    """NCBI esummary JSON for `ids` in `db`: {uid: record}. Raises on failure."""
    url = f"{EUTILS}/esummary.fcgi?" + urllib.parse.urlencode(
        {"db": db, "id": ",".join(ids), "retmode": "json", "tool": "legumista",
         "email": config.contact_email()})
    _validate_url(url)
    doc = _get(url)
    result = (doc or {}).get("result") or {}
    return {uid: result[uid] for uid in result.get("uids", []) if uid in result}


def _contig_lengths(annotation):
    """{contig: length} for the annotation's genome, from its genome_main .fai.
    Returns (lengths, genome_record, error_text)."""
    ctl = controller()
    genome = next((r for gid in annotation.get("derived_from") or []
                   for r in ctl.collections
                   if r["id"] == gid and r.get("type") == "genomes"), None)
    if genome is None:
        return None, None, f"the catalog names no genome collection for {annotation['id']}"
    name = next((f["n"] for f in genome.get("files", [])
                 if f["n"].endswith(".genome_main.fna.gz") and ".fai" in (f.get("i") or [])),
                None)
    if not name:
        return None, genome, f"{genome['id']} publishes no indexed genome_main FASTA"
    url = tools_lis._file_url(genome, name) + ".fai"
    try:
        _validate_url(url)
        text = _get_bytes(url, 50_000_000).decode("utf-8", "replace")
    except Exception as e:  # noqa: BLE001
        return None, genome, f"could not read {name}.fai: {type(e).__name__}: {e}"
    lengths = {}
    for line in text.splitlines():
        parts = line.split("\t")
        if len(parts) >= 2 and parts[1].isdigit():
            lengths[parts[0]] = int(parts[1])
    return lengths, genome, ""


def _resolve_ncbi(inputs, record, index, sel):
    """NCBI Gene IDs -> the genes of `record` they overlap, on a verified-same sequence."""
    wanted = {}
    for raw in inputs:
        name = str(raw or "").strip()
        match = _NCBI_ID_RE.match(name)
        if match:
            wanted.setdefault(match.group(1), name)
        elif name:
            sel.lines.append(f"not found {name}: not an NCBI Gene ID (LOC112749796, "
                             "112749796 or GeneID:112749796); edirect(db='gene') finds them")
    if not wanted:
        return []
    try:
        genes = _esummary("gene", list(wanted))
    except Exception as e:  # noqa: BLE001
        sel.error = (f"error: NCBI esummary (gene) failed: {type(e).__name__}: {e}. Nothing "
                     "was checked; retry.")
        return []
    places = {}
    for uid, name in wanted.items():
        rec = genes.get(uid) or {}
        locs = [g for g in rec.get("genomicinfo") or [] if g.get("chraccver")]
        if rec.get("error") or not rec:
            sel.lines.append(f"not found {name}: NCBI Gene has no record {uid}")
        elif not locs:
            sel.lines.append(f"not found {name}: NCBI gives no genomic location for it")
        else:
            places[uid] = (name, rec, locs)
    accessions = sorted({g["chraccver"] for _n, _r, locs in places.values() for g in locs})
    if not accessions:
        return []
    try:
        seqs = _esummary("nuccore", accessions)
    except Exception as e:  # noqa: BLE001
        sel.error = (f"error: NCBI esummary (nuccore) failed: {type(e).__name__}: {e}. "
                     "Nothing was checked; retry.")
        return []
    by_acc = {}
    for seq in seqs.values():
        for key in (seq.get("accessionversion"), seq.get("caption")):
            if key:
                by_acc[key] = seq
    lengths, genome, err = _contig_lengths(record)
    if err:
        sel.error = f"error: cannot compare NCBI's coordinates with {record['id']}: {err}."
        return []
    by_length = {}
    for contig, length in lengths.items():
        by_length.setdefault(length, []).append(contig)

    found = {}
    for uid, (name, rec, locs) in places.items():
        label = f"{name} [NCBI: {rec.get('description') or rec.get('name') or '?'}]"
        tried = []
        for loc in locs:
            acc = loc["chraccver"]
            seq = by_acc.get(acc) or by_acc.get(acc.split(".")[0]) or {}
            slen = int(seq.get("slen") or 0)
            contigs = [c for c in by_length.get(slen, []) if c in index["by_contig"]]
            if len(contigs) != 1:
                tried.append(f"{acc} ({slen:,} bp) " + (
                    f"matches {len(contigs)} contigs by length" if contigs else
                    f"is no contig of {genome['id']}"))
                continue
            contig = contigs[0]
            start, stop = int(loc["chrstart"]), int(loc["chrstop"])
            lo, hi, strand = min(start, stop) + 1, max(start, stop) + 1, \
                "-" if start > stop else "+"
            hits = []
            for cand in index["by_contig"][contig]:
                if cand.start > hi:
                    break
                if cand.end >= lo and cand.strand == strand:
                    shared = min(hi, cand.end) - max(lo, cand.start) + 1
                    hits.append((cand, round(100 * shared / max(hi - lo + 1,
                                                                cand.end - cand.start + 1))))
            for cand, _pct in hits:
                found.setdefault(cand.id, None)
            same = (f"{acc} is {contig}: identical length, {slen:,} bp"
                    + (f", and NCBI's title names {genome['id']}"
                       if genome["id"] in (seq.get("title") or "") else ""))
            sel.mapping.append((
                f"{label} {contig}:{lo:,}-{hi:,} ({strand})",
                ", ".join(c.id for c, _ in hits),
                "; ".join(f"{pct}% overlap" for _, pct in hits) + f"; {same}" if hits
                else f"no gene of {record['id']} overlaps it on the same strand; {same}"))
            break
        else:
            sel.lines.append(f"not placed {label}: NCBI's sequence for it is not this "
                             f"assembly — {'; '.join(tried)}. Its coordinates cannot be "
                             f"compared with {record['id']}.")
    sel.mapping_title = "NCBI placement"
    if places:
        sel.lines.append("NCBI genes are placed by locus: each maps to every gene on the "
                         "same strand that its NCBI span overlaps (the percentage is shared "
                         "bases over the longer, and NCBI's span includes UTRs)")
    return [index["genes"][g] for g in found]


def _translate_by_locus(genes, tindex, genome, sel):
    """Map genes between two annotations of one assembly by where they are.

    Their loci are directly comparable, and a re-annotation renames freely (peanut's
    Tifrunner gnm2 ann1 Arahy.UDJX6I is ann2's Ah03g097600, and ann2 publishes no synonym
    file), so names cannot be trusted to carry over. A gene maps to every target gene on
    its strand whose coding extent overlaps its own: usually one, more where models were
    split or merged."""
    out = {}
    for gene in genes:
        hits, opposite = [], []
        for cand in tindex["by_contig"].get(gene.contig, []):
            if cand.start > gene.end:
                break                       # by_contig is in start order
            if cand.end < gene.start:
                continue
            shared = min(gene.end, cand.end) - max(gene.start, cand.start) + 1
            pct = round(100 * shared / max(gene.end - gene.start + 1,
                                           cand.end - cand.start + 1))
            (hits if cand.strand == gene.strand else opposite).append((cand, pct))
        for cand, _pct in hits:
            out.setdefault(cand.id, None)
        if hits:
            sel.mapping.append((gene.id, ", ".join(c.id for c, _ in hits),
                                "; ".join(f"{pct}% overlap" for _, pct in hits)))
        else:
            sel.mapping.append((gene.id, "", "no gene overlaps it on the same strand"
                                + (" (opposite strand: " + ", ".join(
                                    c.id for c, _ in opposite) + ")" if opposite else "")))
    sel.lines.append(f"translated by locus: both annotations are on {genome}, so each gene "
                     "maps to every same-strand gene whose coding extent overlaps it; the "
                     "percentage is shared bases over the longer of the two")
    return [tindex["genes"][g] for g in out]


def _translate(genes, source, target_spec, sel):
    """Map genes from `source` to the annotation `target_spec`. Returns target genes."""
    target, err = _annotation(target_spec)
    if target is None:
        sel.error = err
        return [], None
    if target["id"] == source["id"]:
        return genes, target
    tindex, err = bed_index(target)
    if err:
        sel.error = err
        return [], None
    genome = _genome_of_record(source)
    if genome and genome == _genome_of_record(target):
        return _translate_by_locus(genes, tindex, genome, sel), target
    same_species = (source.get("scientific_name_abbrev")
                    and source.get("scientific_name_abbrev") == target.get(
                        "scientific_name_abbrev"))
    out = {}
    if same_species:
        # Within a species, gene names usually carry over between assemblies (soybean
        # gnm2 and gnm4 share Glyma.12G040000); the synonym files cover renames.
        tsyn, tstatus, tname = tools_lis._synonyms(target)
        ssyn, sstatus, sname = tools_lis._synonyms(source)
        back = {}
        if sstatus == "ok":   # source current -> older name; a target may use the older one
            for old, current in ssyn.items():
                back.setdefault(current.lower(), old)
        for gene in genes:
            route, hit = "", None
            if tindex["alias"].get(gene.name.lower()):
                hit, route = tindex["alias"][gene.name.lower()], "same gene name"
            if hit is None and tstatus == "ok":
                for key in [gene.name] + [tools_lis._GENE_PREFIX_RE.sub("", m)
                                          for m in gene.models]:
                    current = tsyn.get(key.lower())
                    if current and tindex["alias"].get(current.lower()):
                        hit, route = tindex["alias"][current.lower()], f"synonym file {tname}"
                        break
            if hit is None and back:
                for model in gene.models:
                    old = back.get(tools_lis._GENE_PREFIX_RE.sub("", model).lower())
                    if old and tindex["alias"].get(old.lower()):
                        hit, route = tindex["alias"][old.lower()], f"synonym file {sname}"
                        break
            if hit:
                out.setdefault(hit, None)
                sel.mapping.append((gene.id, hit, route))
            else:
                sel.mapping.append((gene.id, "", "no match by name or synonym"))
        if "failed" in (tstatus + sstatus):
            sel.incomplete = True
            sel.lines.append("a synonym file could not be read, so unmatched genes are "
                             "NOT CHECKED, not absent")
        return [tindex["genes"][g] for g in out], target
    # Across species, through shared gene families: one-to-many.
    for family_set in _FAMILY_SETS:
        surl, serr = _load_families(source, family_set)
        turl, terr = _load_families(target, family_set) if not serr else (None, serr)
        if serr or terr:
            continue
        for gene in genes:
            families = _families_of(surl, gene.id)
            members = [m for f in families for m in _family_members(turl, f)]
            hits = [tindex["alias"][m.lower()] for m in members if m.lower() in tindex["alias"]]
            for hit in hits:
                out.setdefault(hit, None)
            route = (f"{', '.join(families)} ({family_set})" if families
                     else f"in no {family_set} family")
            sel.mapping.append((gene.id, ", ".join(hits), route))
        sel.lines.append(f"translated through {family_set} families: one-to-many, so a "
                         "target gene can be a paralog, not an ortholog")
        return [tindex["genes"][g] for g in out], target
    sel.error = (f"error: no gene-family set is published by both {source['id']} and "
                 f"{target['id']}, so they cannot be translated across species.")
    return [], None


def _ambiguous_region(genome, candidates):
    """The refusal for a region on an assembly with several annotations.

    Picking one would answer about gene models the caller never chose, and a default
    such as "the newest" would change what an old selector means once LIS adds another.
    So the caller chooses, and the reply gives what the catalog knows to choose with."""
    lines = [f"error: pass 'collection' with a region selector — {len(candidates)} "
             f"annotations sit on {genome}, and their genes differ (IDs and models):"]
    for record in candidates:
        synopsis = (record.get("synopsis") or "").strip() or "no synopsis recorded"
        if len(synopsis) > 100:
            synopsis = synopsis[:99] + "…"
        released = record.get("dataset_release_date") or "release date not recorded"
        lines.append(f"  {record['id']}  ({released})  {synopsis}")
    lines.append("Choose the one whose gene IDs your other sources use: a fully qualified "
                 "ID names its annotation (e.g. '<abbrev>.<strain>.gnmN.ann1.<gene>' is "
                 "ann1). A selection made in one can be moved to another with "
                 "'translate_to'.")
    return "\n".join(lines)


def resolve(selector) -> Selection:
    """Resolve a selector. Returns a Selection; `error` is set when nothing resolved."""
    sel = Selection()
    if controller() is None:
        sel.error = catalog_unavailable()
        return sel
    if not isinstance(selector, dict):
        sel.error = ('error: "genes" must be a selector object: {"ids": [...]}, '
                     '{"region": "contig:start-end"}, {"family": "...", "collection": '
                     '"..."} or {"ncbi": ["LOC..."], "collection": "..."}.')
        return sel
    forms = [k for k in ("ids", "region", "family", "ncbi") if selector.get(k)]
    if len(forms) != 1:
        sel.error = ("error: a selector takes exactly one of 'ids', 'region', 'family' or "
                     f"'ncbi' (got {forms or 'none'}).")
        return sel
    form = forms[0]
    ids = selector.get(form) if form in ("ids", "ncbi") else None
    if ids is not None and (not isinstance(ids, list) or len(ids) > MAX_GENES):
        sel.error = (f"error: '{form}' must be a list of at most {MAX_GENES} names; use a "
                     "'region' or 'family' selector for more.")
        return sel

    collection = selector.get("collection") or ""
    if form == "region" and not collection:
        match = _GENOME_PREFIX_RE.match(str(selector["region"]))
        candidates = _annotations_for_genome(match.group(1)) if match else []
        if not candidates:
            sel.error = ("error: pass 'collection' with a region selector — the region's "
                         "contig names no genome in the catalog.")
            return sel
        if len(candidates) > 1:
            sel.error = _ambiguous_region(match.group(1), candidates)
            return sel
        collection = candidates[0]["path"]
    hint = next((str(i) for i in (ids if form == "ids" else None) or []
                 if tools_lis._QUALIFIED_GENE_RE.match(str(i))), "")
    record, err = _annotation(collection, hint)
    if record is None:
        sel.error = err if form != "ids" or collection or hint else (
            "error: pass 'collection' (an annotation path or id from lis_find), or at "
            "least one fully qualified gene ID.")
        return sel
    if record.get("index_status") != "known":
        sel.error = (f"{record['id']} publishes no CHECKSUM, so the catalog has no file "
                     f"list for it. {catalog_stamp(controller())}")
        return sel
    index, err = bed_index(record)
    if err:
        sel.error = err
        return sel
    sel.record = record
    if form == "ids":
        genes = _resolve_ids(ids, record, index, sel)
    elif form == "ncbi":
        genes = _resolve_ncbi(ids, record, index, sel)
    elif form == "region":
        genes = _resolve_region(str(selector["region"]), record, index, sel)
    else:
        genes = _resolve_family(str(selector["family"]).strip(), record, index, sel)
    if sel.error:
        return sel
    if selector.get("translate_to"):
        genes, target = _translate(genome_order(genes), record, selector["translate_to"], sel)
        if sel.error:
            return sel
        sel.record = target
    genes = genome_order(genes)
    sel.total = len(genes)
    try:
        offset = max(0, int(selector.get("offset") or 0))
    except (TypeError, ValueError):
        sel.error = "error: 'offset' must be a whole number."
        return sel
    # summary() names the page and the next offset from what is finally shown, so a
    # caller that shows fewer genes (lis_gene, to fit its reply) stays accurate.
    sel.genes = genes[offset:offset + MAX_GENES]
    if not sel.genes and not sel.error:
        sel.lines.append("no genes resolved")
    sel.lines.append(catalog_stamp(controller()))
    return sel


SELECTOR_SCHEMA = {
    "type": "object",
    "description": "A gene selector: exactly one of ids, region, family or ncbi.",
    "properties": {
        "ids": {"type": "array", "items": {"type": "string"},
                "description": "Up to 200 gene IDs, symbols or superseded IDs."},
        "region": {"type": "string",
                   "description": "'contig:start-end', 1-based; genes overlapping it."},
        "family": {"type": "string",
                   "description": "A legume.fam3 or legfed_v1_0 family id; needs "
                                  "collection."},
        "ncbi": {"type": "array", "items": {"type": "string"},
                 "description": "NCBI Gene IDs (LOC…), placed by locus; needs collection."},
        "collection": {"type": "string", "description": "The annotation collection."},
        "translate_to": {"type": "string",
                         "description": "Another annotation to move the selection to."},
        "offset": {"type": "integer", "description": "Genes to skip, for paging."},
    },
}
