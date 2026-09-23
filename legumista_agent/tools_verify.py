#!/usr/bin/env python3
"""`verify_ids` — check the identifiers in a draft answer against their sources.

The model calls this before it answers. It pulls DOIs, fully qualified LIS gene IDs,
LIS collection IDs and NCBI assembly accessions out of the text (or takes them as lists),
resolves each one against the authority for that kind of ID, and reports a verdict per
ID. It is deterministic: no LLM judges anything here.

Verdicts: FOUND, NOT FOUND, MISMATCH (a DOI that resolves to a different title than the
one claimed), RETRACTED / EXPRESSION OF CONCERN (found, but flagged), and UNCHECKED (the
lookup failed or no verifier exists for that kind — never read UNCHECKED as FOUND).
"""
import asyncio
import re

from . import pubstatus, tools_catalog, tools_mine
from .results import fail
from .tool import Tool
from .tools_native import _cap, _cli_raw

_DOI_RE = re.compile(r"\b10\.\d{4,9}/[^\s\"'<>]+")
_GENE_RE = re.compile(r"\b[a-z]{4,6}\.[A-Za-z0-9_-]+\.gnm\d+\.ann\d+\.[A-Za-z0-9_.-]+")
# Genome and annotation IDs end in a 4-character key. `annN`, `expr` and `metN` are parts
# of longer IDs (annotation, expression, methylation), so they are never taken as a key.
_COLLECTION_RE = re.compile(
    r"\b[A-Za-z0-9_-]+\.gnm\d+(?:\.ann\d+)?\.(?!(?:expr|met\d|ann\d+)\b)[A-Za-z0-9]{4}\b")
_ASSEMBLY_RE = re.compile(r"\bGC[AF]_\d{9}\.\d+\b")
_DOTTED_RE = re.compile(r"[\w-]+(?:\.[\w-]+)+")     # \w: IDs carry names like Nicolás
# A gene-shaped token that ends like a file (`glyma.Wm82.gnm4.ann1.T8TQ.gene_models_main.
# bed.gz`) is a file name, not a gene; the collection ID inside it is still extracted.
_FILE_SUFFIX_RE = re.compile(
    r"\.(?:gz|bgz|gzi|fai|tbi|csi|fa|fna|faa|fasta|gff3?|gtf|bed|tsv|txt|vcf|bcf|bam|cram|gfa)$",
    re.IGNORECASE)
_TRAILING = ".,;:)]}"
MAX_IDS = 40


def extract_ids(text: str, known_collections=None) -> dict:
    """{'dois': [...], 'genes': [...], 'collections': [...], 'assemblies': [...]}, in
    first-seen order, de-duplicated. Gene IDs are removed before collection matching,
    since a qualified gene ID contains a collection-like prefix.

    Collection IDs come in many shapes (`Wm82.gnm4.ann1.T8TQ`, `mixed.gwas.
    Bandillo_Jarquin_2015`, `Tifrunner.gnm2.ann2.expr.Tifrunner.Clevenger_2016`), and a
    loose pattern would cut a real ID short and report the fragment NOT FOUND. So only the
    genome/annotation shape is matched by pattern; any other ID is extracted when it is in
    `known_collections` (the loaded catalog), including when it is embedded in a file name.
    To check an unknown ID of another shape, pass it explicitly."""
    text = text or ""
    dois = _ordered(d.rstrip(_TRAILING).lower() for d in _DOI_RE.findall(text))

    def is_file(token):
        return bool(_FILE_SUFFIX_RE.search(token.rstrip(_TRAILING)))

    genes = _ordered(g.rstrip(_TRAILING) for g in _GENE_RE.findall(text) if not is_file(g))
    rest = _GENE_RE.sub(lambda m: m.group(0) if is_file(m.group(0)) else " ", text)

    found, spans = [], []                      # (position, id); spans of catalog IDs
    if known_collections:
        for m in _DOTTED_RE.finditer(rest):
            parts = m.group(0).split(".")
            offsets = [0]
            for part in parts[:-1]:
                offsets.append(offsets[-1] + len(part) + 1)
            i = 0
            while i < len(parts):
                for j in range(len(parts), i, -1):
                    candidate = ".".join(parts[i:j])
                    if candidate in known_collections:
                        start = m.start() + offsets[i]
                        spans.append((start, start + len(candidate)))
                        found.append((start, candidate))
                        i = j - 1
                        break
                i += 1
    for m in _COLLECTION_RE.finditer(rest):
        # Drop a pattern match that is only part of a longer catalog ID
        # (`Tifrunner.gnm2.ann2.expr` inside an expression collection's ID).
        if not any(a <= m.start() and m.end() <= b and (b - a) > (m.end() - m.start())
                   for a, b in spans):
            found.append((m.start(), m.group(0)))
    return {"dois": dois, "genes": genes,
            "collections": _ordered(cid for _pos, cid in sorted(found)),
            "assemblies": _ordered(_ASSEMBLY_RE.findall(text))}


def _ordered(items):
    seen, out = set(), []
    for item in items:
        if item and item not in seen:
            seen.add(item)
            out.append(item)
    return out


def _tokens(title: str) -> set:
    return set(re.sub(r"[^a-z0-9 ]+", " ", (title or "").lower()).split())


def title_similarity(a: str, b: str) -> float:
    """Token-set Jaccard similarity of two titles, 0..1."""
    ta, tb = _tokens(a), _tokens(b)
    return len(ta & tb) / len(ta | tb) if ta and tb else 0.0


# Thresholds for a claimed title against the registered one. Tune on a labelled sample
# (CiteCheck-style) before tightening; these err toward "check it" rather than "match".
MATCH_AT, PARTIAL_AT = 0.8, 0.5


# Each verifier returns (verdict, line). Verdicts: FOUND, NOT FOUND, MISMATCH, UNCHECKED.
def verify_doi(doi: str, claimed_title: str = ""):
    status = pubstatus.check_doi(doi)
    st = status.get("status")
    if st == "unknown":
        return "UNCHECKED", f"DOI {doi} — UNCHECKED ({status.get('error', 'lookup failed')})"
    if st == "not_found":
        return "NOT FOUND", (f"DOI {doi} — NOT FOUND: not registered at doi.org (wrong, "
                             "mistyped or fabricated)")
    if st == "non_crossref":
        return "FOUND", (f"DOI {doi} — FOUND at doi.org, but not a Crossref DOI (e.g. "
                         "DataCite); its title and retraction status were not checked")
    title = status.get("title") or ""
    verdict, detail = "FOUND", "FOUND"
    if claimed_title:
        sim = title_similarity(claimed_title, title)
        if sim < PARTIAL_AT:
            verdict = "MISMATCH"
            detail = f"MISMATCH (claimed title does not match; similarity {sim:.2f})"
        elif sim < MATCH_AT:
            detail = f"FOUND, title only partly matches (similarity {sim:.2f}) — check it"
    flag = pubstatus.describe(status)
    return verdict, (f"DOI {doi} — {detail}" + (f", {flag}" if flag else "")
                     + (f' — registered title: "{title}"' if title else ""))


def verify_gene(gene_id: str):
    ids, err = tools_mine._gene_presence(tools_mine.MINE, {"gene": gene_id})
    if err:
        return "UNCHECKED", f"gene {gene_id} — UNCHECKED ({err})"
    if gene_id in (ids or []):
        return "FOUND", f"gene {gene_id} — FOUND in {tools_mine.MINE}"
    if ids:
        return "NOT FOUND", (f"gene {gene_id} — NOT FOUND as written; {tools_mine.MINE} has "
                             + ", ".join(ids[:3]))
    return "NOT FOUND", f"gene {gene_id} — NOT FOUND in {tools_mine.MINE}"


def verify_collection(cid: str):
    ctl = tools_catalog.controller()
    if ctl is None:
        return "UNCHECKED", f"collection {cid} — UNCHECKED (no LIS catalog is loaded)"
    hits = [c["path"] for c in ctl.collections if c["id"] == cid]
    if hits:
        return "FOUND", f"collection {cid} — FOUND in the LIS catalog ({', '.join(hits)})"
    return "NOT FOUND", (f"collection {cid} — NOT FOUND in the LIS catalog "
                         f"{tools_catalog.catalog_stamp(ctl)}")


def verify_assembly(acc: str):
    out, err = _cli_raw(["datasets", "summary", "genome", "accession", acc,
                         "--as-json-lines"])
    if err and err.startswith("__missing__"):
        return "UNCHECKED", f"assembly {acc} — UNCHECKED (the NCBI datasets CLI is not installed)"
    if err:
        low = err.lower()
        if "no genome data" in low or "no assemblies" in low or "not found" in low:
            return "NOT FOUND", f"assembly {acc} — NOT FOUND in NCBI"
        return "UNCHECKED", f"assembly {acc} — UNCHECKED ({err[:120]})"
    if (out or "").strip():
        return "FOUND", f"assembly {acc} — FOUND in NCBI"
    return "NOT FOUND", f"assembly {acc} — NOT FOUND in NCBI"


def _verify(args):
    ctl = tools_catalog.controller()
    known = {c["id"] for c in ctl.collections} if ctl is not None else None
    ids = extract_ids(args.get("text") or "", known)
    for key in ("dois", "genes", "collections", "assemblies"):
        ids[key] = _ordered(ids[key] + [str(x).strip() for x in (args.get(key) or [])])
    claimed = {str(c.get("doi", "")).strip().lower(): str(c.get("title", ""))
               for c in (args.get("citations") or []) if isinstance(c, dict)}
    ids["dois"] = _ordered(ids["dois"] + [d for d in claimed if d])
    total = sum(len(v) for v in ids.values())
    if not total:
        return fail("no identifiers found — pass 'text' containing DOIs, LIS gene or "
                    "collection IDs, or GCA_/GCF_ accessions, or pass them as lists.")
    if total > MAX_IDS:
        return fail(f"{total} identifiers is more than one call checks ({MAX_IDS}); split "
                    "the draft.")
    results = ([verify_doi(d, claimed.get(d, "")) for d in ids["dois"]]
               + [verify_gene(g) for g in ids["genes"]]
               + [verify_collection(c) for c in ids["collections"]]
               + [verify_assembly(a) for a in ids["assemblies"]])
    lines = [line for _verdict, line in results]
    tally = {}
    for verdict, _line in results:
        tally[verdict] = tally.get(verdict, 0) + 1
    flagged = sum(1 for line in lines if "RETRACTED" in line or "CONCERN" in line)
    order = ("FOUND", "MISMATCH", "NOT FOUND", "UNCHECKED")
    summary = ", ".join(f"{tally[v]} {v}" for v in order if v in tally)
    head = (f"verify_ids — {total} identifier(s): {summary}"
            + (f"; {flagged} flagged by a retraction/concern notice" if flagged else ""))
    guidance = ("Do not cite anything NOT FOUND, MISMATCH or RETRACTED as support. Treat "
                "UNCHECKED as unverified and say so.")
    return _cap("\n".join([head] + [f"  {line}" for line in lines] + [guidance]))


def verify_tools() -> list:
    async def run(a):
        return await asyncio.to_thread(_verify, a)
    return [Tool(
        name="verify_ids",
        description=(
            "Check the identifiers in a draft answer before you send it: DOIs (exists? "
            "retracted? does the claimed title match?), fully qualified LIS gene IDs, LIS "
            "collection IDs, and NCBI GCA_/GCF_ assembly accessions. Pass the draft as "
            "'text' (IDs are extracted) and/or explicit lists; pass 'citations' as "
            "[{doi, title}] to catch a real DOI paired with the wrong paper. Returns one "
            "verdict per ID: FOUND, NOT FOUND, MISMATCH, RETRACTED or UNCHECKED."),
        read_only=True, run=run,
        parameters={"type": "object", "additionalProperties": False, "properties": {
            "text": {"type": "string", "description": "Draft text to extract IDs from."},
            "dois": {"type": "array", "items": {"type": "string"},
                     "description": "DOIs, bare or as https://doi.org/ links."},
            "genes": {"type": "array", "items": {"type": "string"},
                      "description": "Fully qualified LIS gene IDs."},
            "collections": {"type": "array", "items": {"type": "string"},
                            "description": "LIS collection IDs, e.g. "
                                           "'Wm82.gnm4.ann1.T8TQ'."},
            "assemblies": {"type": "array", "items": {"type": "string"},
                           "description": "NCBI assembly accessions, e.g. "
                                          "'GCF_000004515.6'."},
            "citations": {"type": "array",
                          "description": "Claimed citations as {doi, title}; a title "
                                         "that does not match the DOI's registered "
                                         "title is reported as MISMATCH.",
                          "items": {"type": "object", "properties": {
                              "doi": {"type": "string"}, "title": {"type": "string"}}}},
        }})]
