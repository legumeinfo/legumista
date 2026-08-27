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

Mine selection: `MINE` (env `LEGUMISTA_LIS_MINE`) with a per-call `mine` override. The
default is `legumemine`, the pan-legume mine — it spans 55 organisms, so its gene families
are cross-species (Legume.fam3.10524 has 347 members there vs 195 in the genus-scoped
glycinemine). The PathQueries are mine-agnostic, so a per-genus mine ('glycinemine',
'phaseolusmine', ...) is a one-argument switch when a genus-scoped answer is wanted. Every
result names the mine that answered, so an agent can never misattribute.
"""
import asyncio
import os
import urllib.parse
from xml.sax.saxutils import quoteattr

from .tool import Tool
from .tools_native import _cap, _get, _validate_url

MINES_BASE = os.environ.get("LEGUMISTA_LIS_MINES_BASE",
                            "https://mines.legumeinfo.org").rstrip("/")
MINE = os.environ.get("LEGUMISTA_LIS_MINE", "legumemine")
MAX_ROWS = int(os.environ.get("LEGUMISTA_MINE_MAX_ROWS", "50"))
# Pre-flight counts cost a round trip; skip them for queries that cannot run away.
COUNT_THRESHOLD = int(os.environ.get("LEGUMISTA_MINE_COUNT_THRESHOLD", "200"))


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


def _url(mine: str, xml: str, fmt: str, size: int = None) -> str:
    params = {"query": xml, "format": fmt}
    if size is not None:
        params["size"] = str(size)
    return f"{_service(mine)}/query/results?" + urllib.parse.urlencode(params)


def _count(mine: str, xml: str):
    """Total matching rows, or None if the count itself failed (never fatal — a missing
    count only costs the caller the 'showing N of M' line)."""
    try:
        url = _url(mine, xml, "count")
        _validate_url(url)
        text = _get(url, accept="text/plain").strip()
        return int(text) if text.isdigit() else None
    except Exception:  # noqa: BLE001
        return None


def _run(mine: str, xml: str, size: int):
    """Execute a PathQuery. Returns (rows, columns, error_text).

    The whole point: InterMine reports failure inside a 200 body, so `wasSuccessful` is
    checked before `results` is trusted."""
    try:
        url = _url(mine, xml, "json", size)
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


def _render(title, mine, gene, rows, cols, total, size, note=""):
    if not rows:
        return (f"{title}: no matches for {gene!r} in {mine}. The query was valid and "
                "returned zero rows — check the identifier, or widen 'assembly'/"
                "'annotation'.")
    head = f"{title} — {gene} [mine: {mine}]"
    shown = f"{len(rows)} row(s)"
    if total and total > len(rows):
        shown += f" of {total} (capped at {size}; raise 'max_results' to see more)"
    lines = [head, shown + (f"  {note}" if note else ""), "  " + " | ".join(cols)]
    for r in rows:
        lines.append("  " + " | ".join("" if v is None else str(v) for v in r))
    return _cap("\n".join(lines))


def _assembly_note(rows, idx):
    """Warn when one bare gene name resolved to several assemblies — those are different
    loci, and silently mixing them is a correctness bug in the caller's analysis."""
    seen = {r[idx] for r in rows if len(r) > idx and r[idx]}
    if len(seen) > 1:
        return (f"NOTE: matched {len(seen)} assemblies ({', '.join(sorted(map(str, seen)))})"
                " — pass 'assembly' to pick one.")
    return ""


# Every LIS mine is named "<genus>mine" in lower case — aeschynomenemine, arachismine,
# cajanusmine, cicermine, glycinemine, lensmine, lupinusmine, medicagomine, phaseolusmine,
# vignamine — so a taxon routes to its mine without a lookup table.
def _mine_for_taxon(taxon: str) -> str:
    return (taxon or "").replace("_", " ").split()[0].lower() + "mine"


def _resolve_mine(args, require_taxon=False):
    """Pick the mine: explicit `mine` wins, else route from `taxon`/`genus`.

    `require_taxon` is set by the breeding tools because QTL/GWAS/marker/map classes exist
    ONLY in the per-species mines — legumemine has no such classes, so defaulting there
    would raise a model error rather than return an honest empty result."""
    explicit = (args.get("mine") or "").strip()
    if explicit:
        return explicit, None
    taxon = (args.get("taxon") or args.get("genus") or "").strip()
    if taxon:
        return _mine_for_taxon(taxon), None
    if require_taxon:
        return None, ("error: missing 'taxon' — QTL/GWAS/marker data lives only in the "
                      "per-species mines (legumemine has none of it), so name the species, "
                      "e.g. taxon='Glycine max' or taxon='Phaseolus vulgaris'.")
    return MINE, None


def _execute(args, title, view, constraints, sort=None, assembly_col=1,
             subject_key="gene", subject_hint="a gene identifier such as 'Glyma.12G040000'",
             require_taxon=False):
    mine, mine_err = _resolve_mine(args, require_taxon)
    if mine_err:
        return mine_err
    subject = (args.get(subject_key) or "").strip()
    if not subject:
        return f"error: missing {subject_key!r} — {subject_hint}."
    size = max(1, min(int(args.get("max_results") or MAX_ROWS), 500))
    xml = _pathquery(view, constraints, sort)
    rows, cols, err = _run(mine, xml, size)
    if err:
        return err
    total = _count(mine, xml) if len(rows) >= min(size, COUNT_THRESHOLD) else len(rows)
    note = _assembly_note(rows, assembly_col) if assembly_col is not None else ""
    return _render(title, mine, subject, rows, cols, total, size, note)


# --- the four tools -------------------------------------------------------------------
def _gene_proteins(args) -> str:
    return _execute(
        args, "Proteins",
        ["Gene.name", "Gene.assemblyVersion", "Gene.annotationVersion",
         "Gene.proteins.primaryIdentifier", "Gene.proteins.length",
         "Gene.proteins.molecularWeight"],
        _gene_constraints(args))


def _gene_families(args) -> str:
    return _execute(
        args, "Gene families",
        ["Gene.name", "Gene.assemblyVersion",
         "Gene.geneFamilyAssignments.geneFamily.primaryIdentifier",
         "Gene.geneFamilyAssignments.geneFamily.size",
         "Gene.geneFamilyAssignments.geneFamily.description"],
        _gene_constraints(args))


def _gene_ontology(args) -> str:
    return _execute(
        args, "Ontology annotations",
        ["Gene.name", "Gene.assemblyVersion",
         "Gene.ontologyAnnotations.ontologyTerm.identifier",
         "Gene.ontologyAnnotations.ontologyTerm.name",
         "Gene.ontologyAnnotations.ontologyTerm.ontology.name"],
        _gene_constraints(args))


def _gene_expression(args) -> str:
    """Rooted at ExpressionValue: Gene has no expression collection, so the gene is
    reached through ExpressionValue.feature. Sorted high-to-low because one gene can carry
    hundreds of values and the top-expressing samples are the informative ones."""
    return _execute(
        args, "Expression values",
        ["ExpressionValue.feature.name", "ExpressionValue.sample.primaryIdentifier",
         "ExpressionValue.sample.description",
         "ExpressionValue.sample.source.primaryIdentifier", "ExpressionValue.value"],
        [("ExpressionValue.feature", "LOOKUP", (args.get("gene") or "").strip())],
        sort="ExpressionValue.value desc",
        assembly_col=None)


def _gene_symbol(args) -> str:
    """Resolve a gene SYMBOL to its gene ID(s) — the lookup `lis_gene` cannot do.

    `=` is case-insensitive in InterMine (GmNARK / gmnark / GMNARK all match), so exact
    matching is safe here. One row per publication, because the DOIs are the point: they
    hand the literature tools a citation for the functional claim."""
    return _execute(
        args, "Gene symbol",
        ["GeneFunction.symbol", "GeneFunction.symbolLong",
         "GeneFunction.gene.name", "GeneFunction.gene.primaryIdentifier",
         "GeneFunction.synopsis", "GeneFunction.publications.doi"],
        [("GeneFunction.symbol", "=", (args.get("symbol") or "").strip())],
        assembly_col=None, subject_key="symbol",
        subject_hint="a gene symbol such as 'GmNARK' or 'PvSYMRK'")


def _gene_orthologs(args) -> str:
    """Family members across species — 'does my crop have this gene?'.

    Accepts a gene (whose family is resolved first) or a family identifier directly.
    Defaults to legumemine: families are cross-species there (Legume.fam3.10524 has 347
    members) but genus-scoped in a per-species mine (195 in glycinemine)."""
    mine, mine_err = _resolve_mine(args)
    if mine_err:
        return mine_err
    family = (args.get("family") or "").strip()
    gene = (args.get("gene") or "").strip()
    if not family:
        if not gene:
            return ("error: provide 'gene' (e.g. 'Glyma.12G040000') or 'family' "
                    "(e.g. 'Legume.fam3.10524').")
        # Step 1: gene -> family. Without this the caller would have to already know the
        # family id, which is exactly the thing they are asking us for.
        xml = _pathquery(["Gene.geneFamilyAssignments.geneFamily.primaryIdentifier"],
                         _gene_constraints(args))
        rows, _cols, err = _run(mine, xml, 5)
        if err:
            return err
        fams = [r[0] for r in (rows or []) if r and r[0]]
        if not fams:
            return (f"no gene family assignment for {gene!r} in {mine} — the query was "
                    "valid and returned zero rows, so orthologs cannot be derived.")
        family = fams[0]
        prefix = (f"{gene} is in gene family {family}"
                  + (f" (also: {', '.join(fams[1:])})" if len(fams) > 1 else "") + "\n")
    else:
        prefix = ""
    out = _execute(
        {**args, "family": family}, "Family members", 
        ["Gene.geneFamilyAssignments.geneFamily.primaryIdentifier", "Gene.name",
         "Gene.organism.genus", "Gene.organism.species", "Gene.assemblyVersion"],
        [("Gene.geneFamilyAssignments.geneFamily.primaryIdentifier", "=", family)],
        sort="Gene.organism.genus asc", assembly_col=None, subject_key="family",
        subject_hint="a gene family identifier such as 'Legume.fam3.10524'")
    return prefix + out


def _trait_qtls(args) -> str:
    """Trait -> QTLs. Per-species mine only (see _resolve_mine)."""
    return _execute(
        args, "QTLs",
        ["QTL.trait.name", "QTL.name", "QTL.linkageGroup.name", "QTL.lod",
         "QTL.markerR2", "QTL.qtlStudy.primaryIdentifier"],
        [("QTL.trait.name", "CONTAINS", (args.get("trait") or "").strip())],
        assembly_col=None, subject_key="trait", require_taxon=True,
        subject_hint="a trait name or fragment such as 'seed protein'")


def _trait_gwas(args) -> str:
    """Trait -> GWAS associations, most significant first. Per-species mine only.

    The qtlStudy/gwas identifiers match the Data Store's gwas/ collection names exactly
    (e.g. mixed.gwas.Bandillo_Jarquin_2015), so lis_files can serve the underlying data."""
    return _execute(
        args, "GWAS associations",
        ["GWASResult.trait.name", "GWASResult.markerName", "GWASResult.pValue",
         "GWASResult.gwas.primaryIdentifier"],
        [("GWASResult.trait.name", "CONTAINS", (args.get("trait") or "").strip())],
        sort="GWASResult.pValue asc", assembly_col=None, subject_key="trait",
        require_taxon=True,
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
              "description": "Species whose mine to query, e.g. 'Glycine max' or "
                             "'Phaseolus vulgaris'. Routes to <genus>mine."},
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
            "size and description. This is the cross-species ortholog bridge: the Data "
            "Store ships family assignments as an UNINDEXED .gfa.tsv.gz that no tool can "
            "read, so the mine is the only way to get them.",
            {**_GENE_ARG, **_ASSEMBLY_ARGS}, _gene_families),
        _mk("legumemine_gene_ontology",
            "Ontology annotations for a gene — GO terms and other ontologies — as "
            "identifier, term name and source ontology. Use for 'what does this gene do?' "
            "when you want curated terms rather than a free-text description.",
            {**_GENE_ARG, **_ASSEMBLY_ARGS}, _gene_ontology),
        _mk("legumemine_gene_expression",
            "Expression values for a gene across samples, highest first, with the sample "
            "identifier, its description (tissue/treatment) and the source study. One "
            "gene can have hundreds of values, so results are capped and the total is "
            "reported.",
            {**_GENE_ARG}, _gene_expression),
        _mk("legumemine_gene_symbol",
            "Resolve a gene SYMBOL (e.g. 'GmNARK', 'PvSYMRK') to its gene identifier, "
            "full name, functional synopsis and the DOIs behind the claim. Use this FIRST "
            "when you have a symbol rather than an ID — lis_gene and the other mine tools "
            "match identifiers only. Feed the DOIs to openalex_by_doi / read_paper.",
            {"symbol": {"type": "string",
                        "description": "Gene symbol, e.g. 'GmNARK'. Case-insensitive, exact."},
             **_TAXON_ARG}, _gene_symbol, required=("symbol",)),
        _mk("legumemine_gene_orthologs",
            "Find a gene's counterparts in other legume species via its gene family — "
            "'does my crop have this gene?'. Give 'gene' (its family is looked up first) "
            "or 'family' directly. Defaults to the pan-legume mine, where families span "
            "genera; a per-species 'mine' narrows them to one genus.",
            {"gene": {"type": "string", "description": "Gene identifier, e.g. 'Glyma.12G040000'."},
             "family": {"type": "string",
                        "description": "Gene family identifier, e.g. 'Legume.fam3.10524'. "
                                       "Skips the gene->family step."},
             **_ASSEMBLY_ARGS, **_TAXON_ARG}, _gene_orthologs, required=()),
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
