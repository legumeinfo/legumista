#!/usr/bin/env python3
"""`browser_link` — links into LIS's hosted JBrowse 2 instances.

Builds a URL and nothing else: it writes nothing, serves nothing, fetches no sequence.
LIS assembly names are genome prefixes (`glyma.Wm82.gnm4`) and contig names carry the
same prefix, exactly what the selector and `lis_gene` return.

Where the names come from:

- **The catalog's placements**, when it has them. `populate-catalog` reads each
  instance's deployed `config.json` and records, per collection, which instance serves
  it, under which assembly name and track IDs (schema: JBROWSE_IDS_IN_CATALOG.md):

      "jbrowse_instances": {"all-genera": {"url": ..., "status": "ok" | "unavailable"}}
      collection["jbrowse"] = [{"instance", "assemblies": [...],
                                "tracks": [{"id", "type", "file", "index"}]}]

  Then a link names only what an instance actually serves, and "not served" is a
  finding: it says which instances were checked, and an instance that was unavailable
  at build time is reported as unchecked, never as absent.

- **Prediction**, for a catalog built before placements existed: the file name minus
  `.gz`, the rule `jbrowse add-track` applies by default. Every such reply says so.

What the deployed builds accept (read from their bundles, 2026-10-05): `assembly`,
`loc`, `tracks`, `highlight` and `sessionTracks` as plain query parameters, and a
`spec-` session for anything else, which here is only the dotplot.

One known defect is worked around: a gene-model GFF3 published with only a CSI index is
added as a session track with its CSI when the instance's config names a `.tbi` (or,
without placements, whenever the GFF3 is CSI-only).
"""
import json
import os
import re
import urllib.parse

from . import genes as G
from . import tools_lis
from .results import count_phrase, fail
from .tool import Tool
from .tools_catalog import catalog_stamp, controller

JBROWSE_URL = os.environ.get("LEGUMISTA_JBROWSE_URL",
                             "https://all-genera.lis.ncgr.org/tools/jbrowse2/")
PREFERRED_INSTANCE = os.environ.get("LEGUMISTA_JBROWSE_INSTANCE", "all-genera")
DATA_HOST = "https://data.legumeinfo.org/"
MAX_URL_CHARS = 8000
MAX_MARKS = 200
MAX_LINKS = 20
WINDOW_BP = 2_000_000
PAD_BP = 1000
_TRACK_ID_RE = re.compile(r"^[A-Za-z0-9_.\-]{1,200}$")
_GENE_MODELS = ".gene_models_main.gff3.gz"


def _assembly_of(record):
    return tools_lis._genome_of(f"{record.get('scientific_name_abbrev', '')}.{record['id']}")


# --- the catalog's placements ---------------------------------------------------------
def _instances(ctl):
    """{instance_id: {"url", "status", ...}} from the catalog, or None when the catalog
    predates placements (then every name is predicted)."""
    found = (ctl.document.get("jbrowse_instances") if ctl is not None else None)
    return found if isinstance(found, dict) and found else None


def _placements(record, instance=None):
    places = [p for p in record.get("jbrowse") or [] if isinstance(p, dict)]
    return [p for p in places if instance is None or p.get("instance") == instance]


def _serving(ctl, assembly):
    """Instance IDs whose placements include this assembly (through its genome)."""
    out = set()
    for record in ctl.collections:
        if record.get("type") == "genomes" and _assembly_of(record) == assembly:
            for place in _placements(record):
                if assembly in (place.get("assemblies") or []):
                    out.add(place.get("instance"))
    return out


def _all_track_ids(ctl, instance, assembly):
    ids = set()
    for record in ctl.collections:
        for place in _placements(record, instance):
            if assembly in (place.get("assemblies") or []):
                ids |= {t.get("id") for t in place.get("tracks") or []}
    return ids


def _choose_instance(ctl, instances, assembly, wanted_tracks, explicit):
    """(instance_id, notes) or (None, reason). The reason distinguishes "no instance
    serves it" (a finding) from "an instance that might was unavailable" (unchecked)."""
    ok = {i for i, meta in instances.items() if (meta or {}).get("status") == "ok"}
    down = sorted(set(instances) - ok)
    serving = _serving(ctl, assembly) & ok
    if explicit:
        if explicit not in instances:
            return None, (f"no JBrowse instance {explicit!r}; the catalog knows "
                          f"{', '.join(sorted(instances))}.")
        if explicit in down:
            return None, (f"{explicit} was unavailable when the catalog was built, so "
                          "whether it serves this assembly was NOT CHECKED.")
        if explicit not in serving:
            return None, (f"{explicit} does not serve {assembly}"
                          + (f"; {', '.join(sorted(serving))} does" if serving else "") + ".")
        return explicit, []
    if not serving:
        checked = ", ".join(sorted(ok)) or "none"
        reason = f"no LIS JBrowse instance serves {assembly} (checked: {checked})"
        if down:
            reason += (f"; NOT CHECKED: {', '.join(down)} (unavailable when the catalog "
                       "was built), so it may be served there")
        return None, reason + "."
    order = sorted(serving, key=lambda i: (i != PREFERRED_INSTANCE, i))
    if wanted_tracks:
        for candidate in order:
            if set(wanted_tracks) <= _all_track_ids(ctl, candidate, assembly):
                return candidate, []
    return order[0], []


def _gene_track_placed(record, instance):
    """(track_id, session_track_or_None, note) for an annotation's gene models on an
    instance, from its placement. None track when the instance does not serve it."""
    entry = next((f for f in record.get("files", []) if f["n"].endswith(_GENE_MODELS)), None)
    for place in _placements(record, instance):
        for track in place.get("tracks") or []:
            if str(track.get("file", "")).endswith(_GENE_MODELS):
                published = set((entry or {}).get("i") or [])
                if (str(track.get("index", "")).upper() == "TBI"
                        and ".csi" in published and ".tbi" not in published):
                    session = _csi_session_track(record, entry)
                    if session:
                        return (session["trackId"], session,
                                f"{instance}'s config points {record['id']}'s gene models "
                                "at a .tbi index that is not published; added as a session "
                                "track with its .csi instead.")
                return track.get("id"), None, ""
    return None, None, ""


# --- prediction, for catalogs without placements --------------------------------------
def _gene_track_predicted(record):
    """(track_id, session_track_or_None, note) from file names."""
    entry = next((f for f in record.get("files", []) if f["n"].endswith(_GENE_MODELS)), None)
    if entry is None:
        return None, None, ""
    indexes = set(entry.get("i") or [])
    if ".csi" in indexes and ".tbi" not in indexes:
        session = _csi_session_track(record, entry)
        if session:
            return (session["trackId"], session,
                    f"{record['id']} publishes only a CSI index for its gene models, so "
                    "they are added as a session track with that index.")
    return entry["n"][:-3], None, ""


def _csi_session_track(record, entry):
    url = tools_lis._file_url(record, entry["n"])
    if not url.startswith(DATA_HOST):
        return None
    session_id = f"legumista-{record['id']}-genes"
    return {"type": "FeatureTrack", "trackId": session_id,
            "name": f"{record['id']} gene models (CSI index)",
            "assemblyNames": [_assembly_of(record)],
            "adapter": {"type": "Gff3TabixAdapter",
                        "gffGzLocation": {"uri": url, "locationType": "UriLocation"},
                        "index": {"indexType": "CSI",
                                  "location": {"uri": url + ".csi",
                                               "locationType": "UriLocation"}}}}


# --- URLs -----------------------------------------------------------------------------
def _clusters(genes):
    """Group genes into views: one contig, at most WINDOW_BP wide, in genome order."""
    out = []
    for gene in G.genome_order(genes):
        last = out[-1] if out else None
        if (last and last[0].contig == gene.contig
                and gene.end - last[0].start <= WINDOW_BP):
            last.append(gene)
        else:
            out.append([gene])
    return out


def _url(base, params):
    return base + "?" + urllib.parse.urlencode(params, quote_via=urllib.parse.quote)


def _linear_link(base, assembly, contig, lo, hi, tracks, session_tracks, marks, mark):
    """One linear-view URL; marks shrink until it fits. Returns (url, marks_used)."""
    shown = list(marks)[:MAX_MARKS]
    while True:
        params = {"config": "config.json", "assembly": assembly,
                  "loc": f"{contig}:{max(1, lo)}-{hi}"}
        st = list(session_tracks)
        track_ids = list(tracks)
        if mark == "features" and shown:
            st.append({"type": "FeatureTrack", "trackId": "legumista-selection",
                       "name": "Legumista selection", "assemblyNames": [assembly],
                       "adapter": {"type": "FromConfigAdapter", "features": [
                           # JBrowse features are 0-based, half-open: a gene at
                           # 2,875,801-2,879,231 is start 2875800, end 2879231.
                           {"uniqueId": g.id, "refName": g.contig, "start": g.start - 1,
                            "end": g.end, "name": g.name,
                            "strand": -1 if g.strand == "-" else 1} for g in shown]}})
            track_ids.append("legumista-selection")
        elif mark == "highlight" and shown:
            params["highlight"] = " ".join(f"{g.contig}:{g.start}-{g.end}" for g in shown)
        if track_ids:
            params["tracks"] = ",".join(track_ids)
        if st:
            params["sessionTracks"] = json.dumps(st, separators=(",", ":"))
        url = _url(base, params)
        if len(url) <= MAX_URL_CHARS or len(shown) <= 1:
            return url, len(shown)
        shown = shown[:max(1, len(shown) // 2)]


def _other_assembly(ctl, compare):
    for record in ctl.collections if ctl else []:
        if record["id"] == compare and record.get("type") == "genomes":
            return _assembly_of(record)
    return compare


def _dotplot_placed(ctl, instance, assembly, other):
    """(track_id, collection_id) of a synteny track on `instance` naming both."""
    for record in ctl.collections:
        for place in _placements(record, instance):
            if {assembly, other} <= set(place.get("assemblies") or []):
                for track in place.get("tracks") or []:
                    if track.get("type") == "SyntenyTrack":
                        return track.get("id"), record["id"]
    return None, None


def _dotplot_predicted(ctl, assembly, other):
    """(track_id, collection_id) from the catalog's PAF file names."""
    for record in ctl.collections if ctl else []:
        if record.get("type") != "genome_alignments":
            continue
        for entry in record.get("files", []):
            name = entry["n"]
            if not name.endswith(".paf.gz"):
                continue
            stem = name[:-len(".paf.gz")] + "."
            if ".x." in stem and f"{assembly}." in stem and f"{other}." in stem:
                return name[:-3], record["id"]
    return None, None


def _dotplot_url(base, assembly, other, track_id):
    spec = {"views": [{"type": "DotplotView",
                       "views": [{"assembly": assembly}, {"assembly": other}],
                       "tracks": [track_id]}]}
    return _url(base, {"config": "config.json",
                       "session": "spec-" + json.dumps(spec, separators=(",", ":"))})


# --- the tool -------------------------------------------------------------------------
def _browser_link(args):  # noqa: C901 - one branch per input form and view
    ctl = controller()
    mark = (args.get("mark") or "highlight").strip().lower()
    view = (args.get("view") or "linear").strip().lower()
    if mark not in ("highlight", "features"):
        return fail("'mark' must be 'highlight' or 'features'.")
    if view not in ("linear", "dotplot"):
        return fail("'view' must be 'linear' or 'dotplot'.")
    user_tracks = args.get("tracks") or []
    if not isinstance(user_tracks, list) or not all(
            isinstance(t, str) and _TRACK_ID_RE.match(t) for t in user_tracks):
        return fail("'tracks' must be a list of JBrowse track IDs (letters, digits, '.', "
                    "'_', '-'); a URL is never accepted, so a link cannot load outside "
                    "data.")
    if bool(args.get("genes")) == bool(args.get("region")):
        return fail("pass exactly one of 'genes' (a selector) or 'region'.")

    lines, genes, records = [], [], []
    if args.get("genes"):
        sel = G.resolve(args["genes"])
        if sel.error:
            return fail(sel.error)
        if not sel.genes:
            return sel.summary()
        genes, records = sel.genes, [sel.record]
        assembly = _assembly_of(sel.record)
        lines.append(sel.summary(int(args["genes"].get("offset") or 0)))
    else:
        contig, lo, hi, err = tools_lis._parse_region(str(args["region"]))
        if err:
            return fail(err)
        match = G._GENOME_PREFIX_RE.match(contig or "")
        if not match or lo is None:
            return fail("'region' must be 'contig:start-end' with an LIS contig name, e.g. "
                        "'glyma.Wm82.gnm4.Gm12:2870000-2890000'.")
        assembly = match.group(1)
        records = G._annotations_for_genome(assembly)
        if ctl is not None and not records and not any(
                c.get("type") == "genomes" and _assembly_of(c) == assembly
                for c in ctl.collections):
            return fail(f"no genome {assembly!r} in the catalog. {catalog_stamp(ctl)}")

    instances = _instances(ctl)
    explicit = (args.get("instance") or "").strip()
    if instances is None:
        if explicit:
            return fail("this catalog records no JBrowse instances, so 'instance' cannot "
                        "be chosen; leave it out to use LEGUMISTA_JBROWSE_URL.")
        instance, base = None, JBROWSE_URL
        provenance = ("Assembly and track names are PREDICTED from the catalog's file "
                      "names (this catalog has no JBrowse placements yet), not read from "
                      "the instance's config: a genome the instance does not serve opens "
                      "an empty view.")
    else:
        instance, reason = _choose_instance(ctl, instances, assembly, user_tracks, explicit)
        if instance is None:
            return "\n".join(lines + [reason, catalog_stamp(ctl)])
        base = instances[instance].get("url") or JBROWSE_URL
        fetched = instances[instance].get("fetched_at", "")
        provenance = (f"Assembly and track names are from {instance}'s deployed config, as "
                      f"the catalog recorded it" + (f" ({fetched})" if fetched else "") + ".")

    if view == "dotplot":
        compare = (args.get("compare") or "").strip()
        if not compare:
            return fail("a dotplot needs 'compare': the other genome (an assembly name like "
                        "'phavu.G19833.gnm2' or a genome collection id).")
        other = _other_assembly(ctl, compare)
        track_id, source = (_dotplot_placed(ctl, instance, assembly, other) if instance
                            else _dotplot_predicted(ctl, assembly, other))
        if track_id is None:
            where = f"on {instance}" if instance else "in the catalog's genome alignments"
            return fail(f"no synteny track between {assembly} and {other} {where}. "
                        f"{catalog_stamp(ctl)}")
        return "\n".join(lines + [f"dotplot of {assembly} against {other}, track {track_id} "
                                  f"(from {source}):", _dotplot_url(base, assembly, other,
                                                                    track_id),
                                  provenance, catalog_stamp(ctl)])

    track_ids, session_tracks = list(user_tracks), []
    if user_tracks and instance:
        unplaced = sorted(set(user_tracks) - _all_track_ids(ctl, instance, assembly))
        if unplaced:
            lines.append(f"not in the catalog's placements for {instance}: "
                         f"{', '.join(unplaced)} (a track served from outside the Data "
                         "Store is not recorded there; check the ID).")
    if not user_tracks:
        for record in records:
            track_id, session_track, note = (_gene_track_placed(record, instance) if instance
                                             else _gene_track_predicted(record))
            if track_id:
                # A session track is defined by sessionTracks but shown only when
                # `tracks` lists it too, so its ID goes in either way.
                track_ids.append(track_id)
            if session_track:
                session_tracks.append(session_track)
            if note:
                lines.append(note)
    links = []
    if genes:
        clusters = _clusters(genes)
        for cluster in clusters[:MAX_LINKS]:
            lo = min(g.start for g in cluster) - PAD_BP
            hi = max(g.end for g in cluster) + PAD_BP
            url, used = _linear_link(base, assembly, cluster[0].contig, lo, hi, track_ids,
                                     session_tracks, cluster, mark)
            note = (f" (marks {used} of {len(cluster)} genes; the URL limit cut the rest)"
                    if used < len(cluster) else "")
            links.append(f"{cluster[0].contig}:{max(1, lo)}-{hi} — "
                         f"{len(cluster)} gene(s){note}\n  {url}")
        if len(clusters) > MAX_LINKS:
            lines.append(count_phrase(MAX_LINKS, len(clusters), "views") +
                         " (one per locus cluster); page the selector with 'offset' or "
                         "narrow it for the rest.")
    else:
        contig, lo, hi, _err = tools_lis._parse_region(str(args["region"]))
        url, _used = _linear_link(base, assembly, contig, lo, hi, track_ids, session_tracks,
                                  [], mark)
        links.append(f"{contig}:{lo}-{hi}\n  {url}")
    lines.append(f"JBrowse 2 ({instance or 'configured instance'}: {base}), assembly "
                 f"{assembly}, tracks: {', '.join(track_ids) or '(instance default)'}")
    lines += links
    lines += [provenance, catalog_stamp(ctl)]
    return "\n".join(lines)


def browser_tools() -> list:
    params = {
        "type": "object",
        "properties": {
            "genes": G.SELECTOR_SCHEMA,
            "region": {"type": "string",
                       "description": "Or a region instead of genes: "
                                      "'glyma.Wm82.gnm4.Gm12:2870000-2890000' (1-based)."},
            "tracks": {"type": "array", "items": {"type": "string"},
                       "description": "JBrowse track IDs to show; default: the "
                                      "annotation's gene models."},
            "mark": {"type": "string", "enum": ["highlight", "features"],
                     "description": "Mark genes as highlighted bands (default) or as an "
                                    "inline track."},
            "view": {"type": "string", "enum": ["linear", "dotplot"],
                     "description": "linear (default), or dotplot against 'compare'."},
            "compare": {"type": "string",
                        "description": "For a dotplot: the other genome (assembly name or "
                                       "genome collection id)."},
            "instance": {"type": "string",
                         "description": "A JBrowse instance from the catalog (e.g. "
                                        "'all-genera', 'cicer'); default: one that serves "
                                        "the assembly, all-genera first."},
        },
    }

    async def run(args):
        import asyncio
        return await asyncio.to_thread(_browser_link, args)

    return [Tool(
        name="browser_link",
        description=(
            "A link that opens genes or a region in an LIS JBrowse 2 instance, with the "
            "gene models track and the genes highlighted (or as an inline track); or a "
            "dotplot of two genomes. Takes a selector ({ids}, {region}, {family}) or a "
            "'region'. Says when no instance serves the assembly. Builds a URL only."),
        parameters=params, read_only=True, run=run)]
