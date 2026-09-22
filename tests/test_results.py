"""The shared result conventions: failure classification, count phrasing, source reports."""
import pytest

from legumista_agent.results import (SourceReport, SourceSummary, ToolOutput, coerce,
                                     count_phrase, fail)


@pytest.mark.parametrize("text", [
    "error: missing 'doi'",
    "[exit 1] something broke",
    "[esearch exit 2] bad query",
    "[datasets exit 1] (no output)",
    "[samtools error] truncated file",
    "[bcftools error] no index",
])
def test_legacy_failure_prefixes_are_errors(text):
    assert coerce(text).is_error is True


@pytest.mark.parametrize("text", [
    "No results.",
    "no collections for 'Glycine soja' in the catalog.",
    "No matches for /N50/ in https://x/y.pdf (12 pages).",
    "(samtools view: completed with no stdout)",
    "Proteins — Glyma.12G040000 [mine: legumemine]",
])
def test_valid_answers_including_empty_ones_are_not_errors(text):
    assert coerce(text).is_error is False


def test_explicit_outputs_pass_through_and_fail_adds_the_prefix():
    out = ToolOutput("anything", is_error=True)
    assert coerce(out) is out
    assert fail("OpenAlex timed out").text == "error: OpenAlex timed out"
    assert fail("error: already prefixed").text == "error: already prefixed"


def test_count_phrase_never_presents_a_cap_as_a_total():
    assert count_phrase(12, 12, "genes") == "12 genes"
    assert count_phrase(40, 46, "species") == "showing 40 of 46 species"
    capped = count_phrase(50, None, "rows", capped=True)
    assert capped.startswith("showing the first 50 rows") and "total is unavailable" in capped
    assert count_phrase(7, None, "rows") == "7 rows"


def test_source_summary_labels_partial_and_total_failure():
    s = SourceSummary()
    s.add(SourceReport("OpenAlex", hits=8))
    s.add(SourceReport.failed("Crossref", TimeoutError("timed out")))
    assert s.header().startswith("PARTIAL RESULTS — Crossref FAILED (TimeoutError: timed out)")
    assert "OpenAlex: 8 hit(s)" in s.footer() and "Crossref: failed" in s.footer()
    both = SourceSummary([SourceReport.failed("OpenAlex", OSError("down")),
                          SourceReport.failed("Crossref", OSError("down"))])
    assert both.all_failed() and both.header() == ""
    assert both.failure_text("paper search").startswith("error: paper search failed at every source")
