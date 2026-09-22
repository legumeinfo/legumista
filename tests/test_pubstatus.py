"""Publication-status tests — Crossref notices, merging, wording, and the per-DOI cache.

No network: `pubstatus._get` is stubbed, so these pin how a status is derived and worded,
not what Crossref currently says about any real DOI."""
import urllib.error

import pytest

from legumista_agent import pubstatus as P


@pytest.fixture(autouse=True)
def empty_cache(monkeypatch):
    monkeypatch.setattr(P, "_CACHE", {})


def _notice(source, kind="retraction", doi="10.1/notice", date=(2023, 4, 22)):
    return {"DOI": doi, "type": kind, "source": source,
            "updated": {"date-parts": [list(date)]}}


def test_one_notice_reported_by_two_sources_is_described_once():
    """Crossref lists a retraction once per source (the publisher's own notice and the
    Retraction Watch record). The model should see one notice, attributed to both."""
    status = P.from_crossref({"updated-by": [_notice("publisher"),
                                             _notice("retraction-watch")]})
    assert status["status"] == "retracted"
    assert P.describe(status) == ("RETRACTED (retraction 10.1/notice, 2023-04-22, via "
                                  "publisher+retraction-watch)")


def test_a_notice_without_a_full_date_is_still_described():
    status = P.from_crossref({"updated-by": [_notice("publisher", date=(2021,))]})
    assert "retraction 10.1/notice, 2021, via publisher" in P.describe(status)
    undated = P.from_crossref({"updated-by": [{"DOI": "10.1/n", "type": "retraction",
                                               "updated": {"date-parts": [[None]]}}]})
    assert "None" not in P.describe(undated)


def test_the_more_severe_status_wins_a_merge():
    corrected = P.from_crossref({"updated-by": [_notice("publisher", kind="correction",
                                                        doi="10.1/c")]})
    retracted = P.from_openalex({"is_retracted": True})
    assert P.merge(corrected, retracted)["status"] == "retracted"
    assert P.merge(retracted, corrected)["status"] == "retracted"
    assert P.merge(corrected, retracted)["checked"] == "crossref+openalex"


def test_a_failed_check_is_never_described_as_clean():
    assert P.describe({"status": "ok"}) == ""
    unknown = P.describe({"status": "unknown", "error": "TimeoutError: timed out"})
    assert unknown.startswith("STATUS UNKNOWN") and "timed out" in unknown


def _registry(calls):
    """Crossref knows 10.1/ok; doi.org also knows the DataCite DOI 10.5281/zenodo.1."""
    def fake_get(url, accept="application/json"):
        calls.append(url)
        if "10.1/flaky" in url:
            raise TimeoutError("timed out")
        if url.startswith("https://doi.org/api/handles/"):
            if "10.5281/zenodo.1" in url:
                return {"responseCode": 1, "handle": "10.5281/zenodo.1", "values": []}
            raise urllib.error.HTTPError(url, 404, "Not Found", {}, None)
        if "10.1/ok" in url:
            return {"message": {"title": ["A paper"], "updated-by": []}}
        raise urllib.error.HTTPError(url, 404, "Not Found", {}, None)   # not in Crossref
    return fake_get


def test_check_doi_caches_answers_but_not_failures(monkeypatch):
    calls = []
    monkeypatch.setattr(P, "_get", _registry(calls))
    assert P.check_doi("10.1/OK")["status"] == "ok"
    assert P.check_doi("10.1/ok")["title"] == "A paper"        # case-folded, cached
    assert P.check_doi("10.1/gone")["status"] == "not_found"    # Crossref 404, doi.org 404
    P.check_doi("10.1/gone")                                     # an answer: cached
    assert P.check_doi("10.1/flaky")["status"] == "unknown"
    P.check_doi("10.1/flaky")                                    # a failure is retried
    assert len(calls) == 1 + 2 + 2


def test_a_doi_crossref_lacks_is_checked_at_doi_org_before_it_is_called_missing(monkeypatch):
    """Dataset and software DOIs (Zenodo, Dryad, figshare) are DataCite DOIs: Crossref
    returns 404 for them. Calling that "not found" would brand a real citation fake."""
    calls = []
    monkeypatch.setattr(P, "_get", _registry(calls))
    status = P.check_doi("10.5281/zenodo.1")
    assert status["status"] == "non_crossref"
    assert "NOT A CROSSREF DOI" in P.describe(status)
    assert P.merge(P.from_openalex({}), status)["checked"] == "openalex"   # not a check
