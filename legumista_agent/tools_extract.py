#!/usr/bin/env python3
"""`extract_features` — strand-correct sequence for a gene selection.

Turns a selection into sequence without the agent doing coordinate arithmetic. Strand,
1-based versus 0-based coordinates and isoform choice are where hand-chained
`fasta_fetch`/`tabix_query` calls fail quietly; here they are code with tests.

Where each feature comes from:

    protein, cds, mrna      the annotation's *_primary FASTA (else the full FASTA), by
                            model name: no coordinates involved
    gene                    the genome FASTA, over the gene's span in the GFF3
    upstream, downstream    the genome FASTA, flanking the gene (or its start codon)
    utr5, utr3              the genome FASTA, over the model's UTR features, stitched
                            in transcript order

Spans come from `gene_models_main.gff3`, not the BED: the BED records the coding
extent, so a flank measured from it would start inside the 5' UTR. On the minus strand
upstream lies at higher coordinates, and every piece is reverse-complemented.

A call costs at most two worker processes, however many genes it covers: one to read
feature rows, sequence lengths and primary models, one to fetch the sequence.
"""
import asyncio
import hashlib
import os

from . import genes as G
from .results import count_phrase, fail
from .tool import Tool
from .tools_catalog import controller
from .tools_native import _cap, _sandbox_path
from .tools_pysam import (MAX_FILE_BYTES, MAX_SEQ, READ_FILE_BYTES, _run_worker,
                          _worker_error)

INLINE_CHARS = int(os.environ.get("LEGUMISTA_EXTRACT_INLINE_CHARS", "15000"))
MAX_TOTAL_BP = int(os.environ.get("LEGUMISTA_EXTRACT_MAX_BP", "2000000"))
MAX_FLANK = 10_000
FEATURES = ("protein", "cds", "mrna", "gene", "upstream", "downstream", "utr5", "utr3")
_SEQUENCE_FEATURES = {"protein": "protein", "cds": "cds", "mrna": "mrna"}
_EXT = {"protein": "faa.gz", "cds": "fna.gz", "mrna": "fna.gz"}
_UTR_TYPES = {"utr5": "five_prime_UTR", "utr3": "three_prime_UTR"}
_COMPLEMENT = str.maketrans("ACGTRYKMBDHVNacgtrykmbdhvn", "TGCAYRMKVHDBNtgcayrmkvhdbn")
_STOPS = {"TAA", "TAG", "TGA"}


def _revcomp(seq: str) -> str:
    return seq.translate(_COMPLEMENT)[::-1]


def _indexed_file(record, suffix):
    """Name of the collection file ending in `.<suffix>` that has a .fai, or None."""
    return next((f["n"] for f in record.get("files", [])
                 if f["n"].endswith("." + suffix) and ".fai" in (f.get("i") or [])), None)


def _genome_for(annotation):
    ctl = controller()
    for genome_id in annotation.get("derived_from") or []:
        for record in ctl.collections:
            if record["id"] == genome_id and record.get("type") == "genomes":
                return record
    return None


def _gff_for(annotation):
    """(name, has_index) for the annotation's gene_models_main.gff3.gz."""
    entry = next((f for f in annotation.get("files", [])
                  if f["n"].endswith(".gene_models_main.gff3.gz")), None)
    if entry is None:
        return None
    return entry["n"] if {".tbi", ".csi"} & set(entry.get("i") or []) else None


def _attrs(column):
    out = {}
    for part in column.split(";"):
        if "=" in part:
            key, _, value = part.partition("=")
            out[key.strip()] = value.strip()
    return out


def _parse_gff(rows):
    feats = []
    for row in rows:
        cols = row.split("\t")
        if len(cols) < 9:
            continue
        feats.append({"type": cols[2], "start": int(cols[3]), "end": int(cols[4]),
                      "strand": cols[6], "attrs": _attrs(cols[8])})
    return feats


def _cds_notes(seq: str):
    """Checks on a CDS. Information, not a verdict: partial models are legitimate."""
    s = seq.upper()
    notes = []
    if len(s) % 3:
        notes.append("length_not_multiple_of_3")
    if not s.startswith("ATG"):
        notes.append("no_ATG_start")
    if s[-3:] not in _STOPS:
        notes.append("no_terminal_stop")
    codons = [s[i:i + 3] for i in range(0, len(s) - 3, 3)]
    if any(c in _STOPS for c in codons):
        notes.append("internal_stop")
    return notes


def _wrap(seq: str) -> str:
    return "\n".join(seq[i:i + 70] for i in range(0, len(seq), 70)) or ""


class _Plan:
    """What to fetch for one record, and how to report it."""

    def __init__(self, label, gene):
        self.label, self.gene = label, gene
        self.src_label = ""      # the collection(s) the sequence came from
        self.items = []          # worker batch items, in transcript order
        self.revcomp = False
        self.loc = ""
        self.notes = []
        self.bed = None          # (contig, start0, end, strand)
        self.bases = 0


def _flank(gene_feature, gene, feats, args, ref_len, cds):
    """(lo, hi, notes) for an upstream/downstream flank, 1-based inclusive."""
    feature, flank, notes = args["feature"], args["flank"], []
    plus = gene.strand != "-"
    if feature == "upstream" and args["anchor"] == "start_codon":
        if not cds:
            return None, None, ["no CDS rows for the chosen model; cannot anchor on the "
                                "start codon"]
        anchor_lo, anchor_hi = min(c["start"] for c in cds), max(c["end"] for c in cds)
    else:
        anchor_lo, anchor_hi = gene_feature
    goes_left = plus == (feature == "upstream")
    lo, hi = (anchor_lo - flank, anchor_lo - 1) if goes_left else (anchor_hi + 1,
                                                                    anchor_hi + flank)
    if args["stop_at_neighbor"]:
        others = [f for f in feats if f["type"] == "gene" and f["attrs"].get("ID") != gene.id
                  and f["start"] <= hi and f["end"] >= lo]
        if others:
            if goes_left:
                new_lo = max(f["end"] for f in others) + 1
                blocker = max(others, key=lambda f: f["end"])
                lo = new_lo
            else:
                new_hi = min(f["start"] for f in others) - 1
                blocker = min(others, key=lambda f: f["start"])
                hi = new_hi
            notes.append(f"stopped_at_neighbor={blocker['attrs'].get('ID', '?')}")
    if lo < 1:
        lo = 1
        notes.append("clipped_at_contig_start")
    if ref_len is not None and hi > ref_len:
        hi = ref_len
        notes.append("clipped_at_contig_end")
    return lo, hi, notes


def _plan_genomic(sel, args, phase1, genome_file):  # noqa: C901 - one branch per feature
    """Turn feature rows into fetch plans. Returns (plans, problems)."""
    plans, problems = [], []
    for gene, (gff, picked, ref_len) in zip(sel.genes, phase1):
        if "error" in gff:
            problems.append(f"{gene.id}: could not read its GFF3 rows "
                            f"({gff.get('message') or gff['error']})")
            continue
        feats = _parse_gff(gff["rows"])
        row = next((f for f in feats if f["type"] == "gene"
                    and f["attrs"].get("ID") == gene.id), None)
        gene_span = (row["start"], row["end"]) if row else (gene.start, gene.end)
        base_notes = [] if row else ["span=coding_extent (no gene row in the GFF3)"]
        mrnas = [f["attrs"].get("ID") for f in feats if f["type"] == "mRNA"
                 and f["attrs"].get("Parent") == gene.id]
        if args["isoforms"] == "all":
            models = mrnas or gene.models
        else:
            models = [picked.get("name") or next(
                (f["attrs"]["ID"] for f in feats if f["type"] == "mRNA"
                 and f["attrs"].get("Parent") == gene.id
                 and f["attrs"].get("longest") == "1"), None) or (mrnas or gene.models)[0]]
        per_model = args["feature"] in ("utr5", "utr3") or (
            args["feature"] == "upstream" and args["anchor"] == "start_codon")
        for model in models if per_model else [None]:
            label = model or gene.id
            plan = _Plan(label, gene)
            plan.notes = list(base_notes)
            plus = gene.strand != "-"
            plan.revcomp = not plus
            cds = [f for f in feats if f["type"] == "CDS" and f["attrs"].get("Parent") == model]
            if args["feature"] == "gene":
                pieces = [gene_span]
            elif args["feature"] in ("upstream", "downstream"):
                lo, hi, notes = _flank(gene_span, gene, feats, args, ref_len, cds)
                plan.notes += notes
                if lo is None:
                    problems.append(f"{label}: {notes[0]}")
                    continue
                pieces = [(lo, hi)] if hi >= lo else []
            else:
                utrs = [(f["start"], f["end"]) for f in feats
                        if f["type"] == _UTR_TYPES[args["feature"]]
                        and f["attrs"].get("Parent") == model]
                pieces = sorted(utrs, reverse=not plus)
                if not pieces:
                    plan.notes.append(f"no annotated {_UTR_TYPES[args['feature']]} "
                                      "in the GFF3")
            if not pieces:
                # Nothing to fetch is a fact about this gene, not an empty sequence.
                problems.append(f"{label}: " + (" ".join(plan.notes)
                                                 or "the flank is empty (an overlapping "
                                                    "gene)"))
                continue
            for lo, hi in pieces:
                plan.items.append({"kind": "fasta", "path": genome_file, "name": gene.contig,
                                   "start": lo - 1, "end": hi, "max_len": MAX_SEQ})
                plan.bases += hi - lo + 1
            lo, hi = min(p[0] for p in pieces), max(p[1] for p in pieces)
            plan.loc = f"{gene.contig}:{lo}-{hi}({gene.strand})"
            plan.bed = (gene.contig, lo - 1, hi, gene.strand)
            if len(pieces) > 1:
                plan.notes.append(f"pieces={len(pieces)}")
            plans.append(plan)
    return plans, problems


async def _extract(args, allow_write: bool):
    feature = (args.get("feature") or "").strip().lower()
    if feature not in FEATURES:
        return fail(f"'feature' must be one of {', '.join(FEATURES)}.")
    try:
        flank = int(args.get("flank") if args.get("flank") is not None else 2000)
    except (TypeError, ValueError):
        return fail("'flank' must be a whole number of bases.")
    if not 0 < flank <= MAX_FLANK:
        return fail(f"'flank' must be between 1 and {MAX_FLANK:,} bp.")
    anchor = (args.get("anchor") or "gene_start").strip().lower()
    isoforms = (args.get("isoforms") or "primary").strip().lower()
    if anchor not in ("gene_start", "start_codon"):
        return fail("'anchor' must be 'gene_start' or 'start_codon'.")
    if isoforms not in ("primary", "all"):
        return fail("'isoforms' must be 'primary' or 'all'.")
    opts = {"feature": feature, "flank": flank, "anchor": anchor, "isoforms": isoforms,
            "stop_at_neighbor": bool(args.get("stop_at_neighbor"))}

    sel = await asyncio.to_thread(G.resolve, args.get("genes"))
    if sel.error:
        return fail(sel.error)
    offset = int((args.get("genes") or {}).get("offset") or 0)
    if not sel.genes:
        return sel.summary(offset)
    record = sel.record
    plans, problems = await asyncio.to_thread(_plan_all, sel, opts)
    if isinstance(plans, str):
        return fail(plans)

    total = sum(p.bases for p in plans)
    head = [f"extract_features: feature={feature}"
            + (f" flank={flank} anchor={anchor}" if feature in ("upstream", "downstream")
               else "") + f" isoforms={isoforms} from {record['id']}",
            sel.summary(offset)]
    if args.get("dry_run"):
        lines = head + [f"dry run: {len(plans)} record(s), {total:,} bp; nothing fetched."]
        lines += [f"  {p.label}\t{p.bases:,} bp\t{p.loc or '(by name)'}\t{' '.join(p.notes)}"
                  for p in plans]
        lines += [f"  problem: {x}" for x in problems]
        return _cap("\n".join(lines))
    if total > MAX_TOTAL_BP:
        return fail(f"this selection asks for {total:,} bp, over the {MAX_TOTAL_BP:,} bp "
                    "limit per call. Narrow it, or page with the selector's 'offset' "
                    "(dry_run=true shows the size per record).")

    items = [item for p in plans for item in p.items]
    res = await asyncio.to_thread(_run_worker, {"op": "batch", "items": items},
                                  READ_FILE_BYTES)
    if res.get("error"):
        return fail(_worker_error(res, "extract_features", record["id"]))
    results = iter(res["items"])
    records, qc_flagged = [], 0
    for plan in plans:
        parts, failed = [], ""
        for _item in plan.items:
            got = next(results)
            if "error" in got:
                failed = got.get("message") or got["error"]
                continue
            parts.append(got["seq"])
            if got.get("end", 0) - got.get("start", 0) >= MAX_SEQ:
                plan.notes.append(f"truncated_to={MAX_SEQ}")
        if failed:
            problems.append(f"{plan.label}: {failed}")
            continue
        seq = "".join(_revcomp(s) for s in parts) if plan.revcomp else "".join(parts)
        if feature == "cds":
            notes = _cds_notes(seq)
            if notes:
                qc_flagged += 1
                plan.notes.append("cds_notes=" + ",".join(notes))
        header = (f">{plan.label} feature={feature}"
                  + (f" loc={plan.loc}" if plan.loc else "") + f" src={plan.src_label}"
                  + "".join(f" {n}" for n in plan.notes))
        records.append((header, seq, plan))

    unit = "residues" if feature == "protein" else "bp"
    summary = head + [f"{count_phrase(len(records), len(plans), 'record(s)')}, "
                      f"{sum(len(s) for _h, s, _p in records):,} {unit}"]
    if feature == "cds" and qc_flagged:
        summary.append(f"{qc_flagged} CDS record(s) carry cds_notes (no ATG start, no "
                       "terminal stop, internal stop or a length not divisible by 3). "
                       "Partial gene models are common; these are notes, not defects.")
    summary += [f"problem: {x}" for x in problems]
    fasta = "\n".join(f"{h}\n{_wrap(s)}" for h, s, _p in records)
    text = "\n".join(summary) + "\n\n" + fasta
    if len(text) <= INLINE_CHARS:
        return text
    if allow_write:
        return await asyncio.to_thread(_write_files, summary, fasta, records, feature)
    shown, used = [], len("\n".join(summary)) + 200
    for h, s, _p in records:
        block = f"{h}\n{_wrap(s)}"
        if used + len(block) > INLINE_CHARS - 600:
            break
        shown.append(block)
        used += len(block) + 1
    return ("\n".join(summary)
            + f"\n{count_phrase(len(shown), len(records), 'record(s)')} inline — the rest "
              "exceed this reply's size. The server was started without --allow-write, so "
              "it cannot write them to a file: narrow the selection, or page it with the "
              "selector's 'offset'.\n\n" + "\n".join(shown))


def _plan_all(sel, opts):
    """Phase 1: read what the plans need (one worker), then build them."""
    record, feature = sel.record, opts["feature"]
    if feature in _SEQUENCE_FEATURES:
        kind = _SEQUENCE_FEATURES[feature]
        primary = _indexed_file(record, f"{kind}_primary.{_EXT[kind]}")
        full = _indexed_file(record, f"{kind}.{_EXT[kind]}")
        if opts["isoforms"] == "primary" and primary:
            path, rule = primary, "present"
        elif full:
            path, rule = full, ("longest" if opts["isoforms"] == "primary" else None)
        else:
            return (f"{record['id']} publishes no indexed {kind} FASTA, so this feature "
                    "cannot be extracted."), []
        url = G.tools_lis._file_url(record, path)
        if rule is None:
            plans = []
            for gene in sel.genes:
                for model in gene.models:
                    plan = _Plan(model, gene)
                    plan.items.append({"kind": "fasta", "path": url, "name": model,
                                       "max_len": MAX_SEQ})
                    plans.append(plan)
            items = [{"kind": "pick", "path": url, "names": [p.label], "rule": "present"}
                     for p in plans]
        else:
            plans = None
            items = [{"kind": "pick", "path": url, "names": g.models, "rule": rule}
                     for g in sel.genes]
        res = _run_worker({"op": "batch", "items": items}, READ_FILE_BYTES)
        if res.get("error"):
            return _worker_error(res, "extract_features", record["id"]), []
        picks, problems = res["items"], []
        if plans is None:
            plans = []
            for gene, pick in zip(sel.genes, picks):
                if pick.get("error"):
                    problems.append(f"{gene.id}: could not read {path} "
                                    f"({pick.get('message') or pick['error']})")
                    continue
                if not pick.get("name"):
                    problems.append(f"{gene.id}: no model of it is in {path}")
                    continue
                plan = _Plan(pick["name"], gene)
                plan.items.append({"kind": "fasta", "path": url, "name": pick["name"],
                                   "max_len": MAX_SEQ})
                plan.bases = min(pick["length"], MAX_SEQ)
                if rule == "longest":
                    plan.notes.append("chosen=longest_model(no_primary_file)")
                plans.append(plan)
        else:
            kept = []
            for plan, pick in zip(plans, picks):
                if pick.get("name"):
                    plan.bases = min(pick["length"], MAX_SEQ)
                    kept.append(plan)
                else:
                    problems.append(f"{plan.label}: not in {path}")
            plans = kept
        for plan in plans:
            plan.src_label = record["id"]
            plan.bed = (plan.gene.contig, plan.gene.start - 1, plan.gene.end,
                        plan.gene.strand)
        return plans, problems

    genome = _genome_for(record)
    gff = _gff_for(record)
    genome_file = _indexed_file(genome, "genome_main.fna.gz") if genome else None
    if not (genome and genome_file and gff):
        missing = [what for what, ok in (("an indexed genome_main FASTA in its genome "
                                          "collection", genome and genome_file),
                                         ("an indexed gene_models_main GFF3", gff)) if not ok]
        return f"{record['id']} lacks {' and '.join(missing)}, so this feature cannot be " \
               "extracted.", []
    genome_url = G.tools_lis._file_url(genome, genome_file)
    gff_url = G.tools_lis._file_url(record, gff)
    cds_primary = _indexed_file(record, "cds_primary.fna.gz")
    cds_full = _indexed_file(record, "cds.fna.gz")
    pick_path = cds_primary or cds_full
    margin = opts["flank"] if feature in ("upstream", "downstream") else 0
    items, contigs = [], sorted({g.contig for g in sel.genes})
    for gene in sel.genes:
        items.append({"kind": "tabix", "path": gff_url, "contig": gene.contig,
                      # UTRs reach past the coding extent the BED records, so a flank
                      # anchored on the gene's start needs to see a little further.
                      "start": max(0, gene.start - 1 - margin - 10_000),
                      "end": gene.end + margin + 10_000, "limit": 20_000})
        if pick_path:
            items.append({"kind": "pick", "path": G.tools_lis._file_url(record, pick_path),
                          "names": gene.models,
                          "rule": "present" if cds_primary else "longest"})
        else:
            items.append({"kind": "noop"})
    items += [{"kind": "fasta", "path": genome_url, "name": c, "start": 0, "end": 0}
              for c in contigs]
    res = _run_worker({"op": "batch", "items": items}, READ_FILE_BYTES)
    if res.get("error"):
        return _worker_error(res, "extract_features", record["id"]), []
    got = res["items"]
    ref_lens = {c: got[2 * len(sel.genes) + i].get("ref_len")
                for i, c in enumerate(contigs)}
    phase1 = [(got[2 * i], got[2 * i + 1] if "error" not in got[2 * i + 1] else {},
               ref_lens.get(g.contig)) for i, g in enumerate(sel.genes)]
    plans, problems = _plan_genomic(sel, opts, phase1, genome_url)
    for plan in plans:
        plan.src_label = f"{genome['id']}+{record['id']}"
    return plans, problems


def _write_files(summary, fasta, records, feature):
    digest = hashlib.sha256(fasta.encode()).hexdigest()[:10]
    base = f"extract_{feature}_{digest}"
    fa_path, err = _sandbox_path(base + ".fa")
    bed_path, err2 = _sandbox_path(base + ".bed")
    if err or err2:
        return fail(err or err2)
    bed = "".join(f"{c}\t{s}\t{e}\t{p.label}\t0\t{st}\n" for _h, _s, p in records
                  if p.bed for c, s, e, st in [p.bed])
    if MAX_FILE_BYTES and len(fasta) > MAX_FILE_BYTES:
        return fail(f"the FASTA would be {len(fasta):,} bytes, over this server's "
                    f"{MAX_FILE_BYTES:,}-byte limit per file.")
    try:
        with open(fa_path, "w", encoding="utf-8") as fh:
            fh.write(fasta + "\n")
        with open(bed_path, "w", encoding="utf-8") as fh:
            fh.write(bed)
    except OSError as e:
        return fail(f"could not write {base}.fa: {e}")
    return ("\n".join(summary) + f"\nwrote {len(records)} record(s) to {base}.fa and their "
            f"coordinates to {base}.bed (0-based starts) in the workspace.\n"
            f"Read them with fasta_fetch / tabix_query, or "
            f"samtools(args=['faidx', '{base}.fa']) to index the FASTA.")


def extract_tools(allow_write: bool = False) -> list:
    params = {
        "type": "object",
        "properties": {
            "genes": G.SELECTOR_SCHEMA,
            "feature": {"type": "string", "enum": list(FEATURES),
                        "description": "What to extract for each gene."},
            "flank": {"type": "integer",
                      "description": f"upstream/downstream length in bp (default 2,000, "
                                     f"max {MAX_FLANK:,})."},
            "anchor": {"type": "string", "enum": ["gene_start", "start_codon"],
                       "description": "upstream measured from the transcription start "
                                      "(default) or the start codon."},
            "stop_at_neighbor": {"type": "boolean",
                                 "description": "Truncate a flank at the nearest gene."},
            "isoforms": {"type": "string", "enum": ["primary", "all"],
                         "description": "One model per gene (default) or every model."},
            "dry_run": {"type": "boolean",
                        "description": "Report record count and bases; fetch nothing."},
        },
        "required": ["genes", "feature"],
    }

    async def run(args):
        return await _extract(args, allow_write)

    return [Tool(
        name="extract_features",
        description=(
            "Strand-correct sequence for a gene selection: protein, cds, mrna, gene, "
            "upstream, downstream, utr5 or utr3. Takes a selector ({ids}, {region} or "
            "{family}; up to 200 genes). Handles strand, coordinates and isoforms; headers "
            "carry locus and source. Large output needs --allow-write (written to a "
            "workspace FASTA + BED); otherwise the reply says how much it left out."),
        parameters=params, read_only=not allow_write, run=run,
        writes=lambda a: allow_write and not a.get("dry_run"))]
