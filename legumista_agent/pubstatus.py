"""Publication status — has a DOI been retracted, withdrawn, corrected or flagged?

Crossref's record for the ORIGINAL work carries an `updated-by` list naming each notice:
its type ("retraction", "expression_of_concern", "correction", ...), the notice's DOI, the
source ("publisher" or "retraction-watch"; Retraction Watch data has been in Crossref's
REST API since January 2025) and the date. OpenAlex exposes the headline fact as the
boolean `is_retracted`. Both APIs include these fields in their search results, so a
search can flag most hits without an extra request; `check_doi` is the per-DOI lookup for
everything else.

Status values: "retracted", "concern", "corrected", "ok" (the source was checked and
lists no notice), "non_crossref" (registered with another agency such as DataCite, so
Crossref holds no status for it), "not_found" (not registered anywhere: doi.org does not
know it) and "unknown" (a check itself failed — never report that as "ok").

A Crossref 404 alone proves nothing about existence: dataset and software DOIs (Zenodo,
Dryad, figshare) are DataCite DOIs. `check_doi` therefore asks the DOI proxy before it
reports "not_found".
"""
import threading
import time
import urllib.error
import urllib.parse

import config

from .tools_native import _get

_TTL_SECONDS = 24 * 3600
_CACHE: dict = {}
_LOCK = threading.Lock()


def classify(update_type: str) -> str:
    """Map a Crossref update type onto our status vocabulary. Unknown types are kept as
    'updated' so nothing is silently dropped; callers always print the raw type too."""
    t = (update_type or "").lower()
    if "retract" in t or "withdraw" in t or "removal" in t:
        return "retracted"
    if "concern" in t:
        return "concern"
    if "correct" in t or "errat" in t or "corrig" in t:
        return "corrected"
    return "updated"


_RANK = {"retracted": 4, "concern": 3, "corrected": 2, "updated": 1, "ok": 0}
# Statuses that mean a source was consulted and answered about notices. The others
# ("non_crossref", "not_found", "unknown") never count as a retraction check.
CHECKED = frozenset(_RANK)


def from_crossref(item: dict) -> dict:
    """Status from one Crossref work record (a /works/{doi} message or a search item)."""
    notices = []
    for upd in item.get("updated-by") or []:
        parts = [p for p in ((upd.get("updated") or {}).get("date-parts") or [[]])[0]
                 if isinstance(p, int)]
        date = "-".join(f"{p:02d}" if i else str(p) for i, p in enumerate(parts))
        notices.append({"type": upd.get("type") or "", "doi": upd.get("DOI") or "",
                        "source": upd.get("source") or "", "date": date,
                        "status": classify(upd.get("type"))})
    worst = max((n["status"] for n in notices), key=_RANK.get, default="ok")
    return {"status": worst, "notices": notices, "checked": "crossref"}


def from_openalex(work: dict) -> dict:
    """Status from an OpenAlex work: only the retraction flag is available there."""
    retracted = bool(work.get("is_retracted"))
    return {"status": "retracted" if retracted else "ok", "notices": [],
            "checked": "openalex"}


def merge(a: dict, b: dict) -> dict:
    """Combine two status reports; the more severe status wins, notices are unioned."""
    if not a:
        return b or {}
    if not b:
        return a
    worst = max((a.get("status", "ok"), b.get("status", "ok")),
                key=lambda s: _RANK.get(s, -1))
    notices = a.get("notices", []) + [n for n in b.get("notices", [])
                                      if n not in a.get("notices", [])]
    checked = "+".join(sorted({r.get("checked", "") for r in (a, b)
                               if r.get("status") in CHECKED} - {""}))
    return {"status": worst, "notices": notices, "checked": checked}


def doi_registered(doi: str):
    """Is this DOI registered with ANY agency (Crossref, DataCite, mEDRA, ...)?

    The DOI proxy's handle API answers HTTP 200 with responseCode 1 (or 200: the handle
    exists but has no values) for a registered DOI, and HTTP 404 with responseCode 100
    for an unregistered one. Returns True, False, or None when the check itself failed."""
    try:
        data = _get(f"https://doi.org/api/handles/{urllib.parse.quote(doi)}")
    except urllib.error.HTTPError as e:
        return False if e.code == 404 else None
    except Exception:  # noqa: BLE001
        return None
    return (data or {}).get("responseCode") in (1, 200)


def check_doi(doi: str) -> dict:
    """One DOI against Crossref, cached for a day. Failures are never cached."""
    doi = (doi or "").strip().lower()
    now = time.time()
    with _LOCK:
        hit = _CACHE.get(doi)
        if hit and now - hit[0] < _TTL_SECONDS:
            return hit[1]
    url = (f"https://api.crossref.org/works/{urllib.parse.quote(doi)}"
           f"?mailto={config.contact_email()}")
    try:
        item = (_get(url) or {}).get("message") or {}
    except urllib.error.HTTPError as e:
        if e.code == 404:
            registered = doi_registered(doi)
            if registered is None:
                return {"status": "unknown", "notices": [],
                        "error": "Crossref has no record, and doi.org could not be reached "
                                 "to check whether the DOI exists at all"}
            result = {"status": "non_crossref" if registered else "not_found",
                      "notices": [], "checked": "doi.org"}
            with _LOCK:
                _CACHE[doi] = (now, result)
            return result
        return {"status": "unknown", "notices": [], "error": f"HTTP {e.code}"}
    except Exception as e:  # noqa: BLE001
        return {"status": "unknown", "notices": [], "error": f"{type(e).__name__}: {e}"}
    result = from_crossref(item)
    result["title"] = ((item.get("title") or [""])[0] or "").strip()
    with _LOCK:
        _CACHE[doi] = (now, result)
    return result


def describe(status: dict) -> str:
    """'RETRACTED (retraction 10.x/y, 2023-04-22, via publisher+retraction-watch)', or ''
    for an unflagged work. 'unknown' is spelled out so it is never read as clean."""
    st = (status or {}).get("status", "")
    if st in ("", "ok"):
        return ""
    if st == "unknown":
        return f"STATUS UNKNOWN (retraction check failed: {status.get('error', '?')})"
    if st == "not_found":
        return "NOT FOUND (doi.org does not know this DOI: it is wrong or fabricated)"
    if st == "non_crossref":
        return ("NOT A CROSSREF DOI (registered with another agency, e.g. DataCite; its "
                "retraction status is not checked)")
    grouped = {}
    for n in status.get("notices", []):
        key = (n["type"], n["doi"], n["date"])
        grouped.setdefault(key, set()).add(n["source"] or "?")
    detail = "; ".join(f"{t or 'notice'} {d or '(no notice DOI)'}"
                       + (f", {dt}" if dt else "") + f", via {'+'.join(sorted(src))}"
                       for (t, d, dt), src in grouped.items())
    label = {"retracted": "RETRACTED", "concern": "EXPRESSION OF CONCERN",
             "corrected": "CORRECTED", "updated": "UPDATED"}.get(st, st.upper())
    return f"{label} ({detail})" if detail else label
