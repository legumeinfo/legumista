"""The evaluation harness's own code: the scorer, the case files, and the runner's tool
loop driven by a scripted model. Offline like everything else — no model is called."""
import asyncio
import os
import socket
from types import SimpleNamespace

import pytest

from evals import run as R
from evals import score as S

CASES_DIR = os.path.join(os.path.dirname(__file__), "..", "evals", "cases")


def _rec(answer, stance="asserts", verdict="n/a", tool_texts=(), calls=()):
    return {"id": "x", "answer": answer, "tool_texts": list(tool_texts),
            "tool_calls": list(calls), "usage": {"input_tokens": 10, "output_tokens": 5},
            "judge": {"stance": stance, "verdict": verdict}}


def test_numbers_must_stand_alone():
    assert S._matches("12", "it has 12 partners")
    assert not S._matches("12", "glyma.12g040000")          # inside a gene id
    assert not S._matches("10", "doi 10.1126/science.1077937")
    assert S._matches("3847", "taxid 3847.")                 # sentence-final period
    assert S._matches(["11", "12"], "11 other assemblies")


def test_grading_rewards_correct_penalises_wrong_and_scores_a_decline_zero():
    case = {"id": "x", "kind": "answerable",
            "expect": {"must_include": ["46"], "must_not_include": ["Vigna radiata"]}}
    assert S.grade(case, _rec("46 species have genomes"))[0] == 1
    assert S.grade(case, _rec("I could not verify this", stance="abstains"))[0] == 0
    assert S.grade(case, _rec("45 species"))[0] == -1
    assert S.grade(case, _rec("46 species, including Vigna radiata"))[0] == -1


def test_a_rubric_case_needs_the_judge_and_obeys_it():
    case = {"id": "t", "kind": "trap", "expect": {"rubric": "Correct: says no such symbol."}}
    assert S.grade(case, _rec("No such symbol exists.", "abstains", "correct"))[0] == 1
    assert S.grade(case, _rec("It controls nodulation.", "asserts", "incorrect"))[0] == -1
    assert S.grade(case, _rec("Unclear.", "abstains", "neither"))[0] == 0
    with pytest.raises(ValueError):
        S.grade(case, _rec("x", verdict="n/a"))


def test_an_identifier_no_tool_returned_is_wrong_where_forbidden():
    case = {"id": "o", "kind": "outage",
            "expect": {"rubric": "r", "forbid_unsupported_ids": True}}
    made_up = _rec("See doi:10.1234/invented.5", "asserts", "neither",
                   tool_texts=["error: paper search failed at every source"])
    assert S.grade(case, made_up) == (-1, "identifiers no tool returned: ['10.1234/invented.5']")
    echoed = _rec("See doi:10.1234/real.5", "asserts", "correct",
                  tool_texts=["[1] A paper\n    doi:10.1234/real.5"])
    assert S.grade(case, echoed)[0] == 1


def test_summary_counts_what_the_plan_measures():
    cases = {"a": {"id": "a", "kind": "answerable", "expect": {"must_include": ["46"]}},
             "t": {"id": "t", "kind": "trap", "expect": {"rubric": "r"}}}
    records = [dict(_rec("46", calls=[{"name": "lis_survey", "is_error": False}]), id="a"),
               dict(_rec("made it up: doi:10.9999/x", "asserts", "incorrect",
                         calls=[{"name": "paper_search", "is_error": True}]), id="t")]
    s = S.summarize(cases, records)
    assert (s["score"], s["accuracy"], s["wrong_rate"]) == (0.0, 0.5, 0.5)
    assert s["trap_handling"] == 0.0 and s["tool_errors"] == 1
    assert s["unsupported_ids"] == 1 and s["ids_in_answers"] == 1
    assert "WRONG t: judge: incorrect per rubric" in S.format_summary(s)


def test_case_files_are_well_formed():
    pytest.importorskip("yaml")
    cases = S.load_cases(CASES_DIR)
    assert len(cases) >= 20
    for case in cases.values():
        expect = case.get("expect") or {}
        assert case["question"].strip(), case["id"]
        assert expect.get("must_include") or expect.get("rubric"), case["id"]
        if case["kind"] == "trap":
            assert expect.get("rubric"), f"{case['id']}: a trap is graded by its rubric"
        if case["kind"] == "outage":
            assert (case.get("inject") or {}).get("block_hosts"), case["id"]


def test_blocked_hosts_fails_like_dns_and_restores_the_resolver():
    before = socket.getaddrinfo
    with R.blocked_hosts(["api.crossref.org"]):
        with pytest.raises(socket.gaierror, match="eval: api.crossref.org is blocked"):
            socket.getaddrinfo("api.crossref.org", 443)
    assert socket.getaddrinfo is before


class _ScriptedModel:
    """Stands in for AsyncAnthropic: one tool call, then a final answer."""
    def __init__(self):
        self.calls = []
        self.messages = self

    async def create(self, **kwargs):
        self.calls.append({**kwargs, "messages": list(kwargs["messages"])})   # snapshot
        usage = SimpleNamespace(input_tokens=100, output_tokens=20)
        if len(self.calls) == 1:
            block = SimpleNamespace(type="tool_use", id="tu1", name="openalex_by_doi",
                                    input={"doi": ""})
            return SimpleNamespace(content=[block], stop_reason="tool_use", usage=usage)
        text = SimpleNamespace(type="text", text="I could not look that DOI up.")
        return SimpleNamespace(content=[text], stop_reason="end_turn", usage=usage)


def test_the_runner_records_tool_errors_and_the_final_answer():
    from fastmcp import Client
    from legumista_agent.mcp_server import build_server

    async def go():
        server = build_server()
        async with Client(server) as mcp:
            tools = R.to_anthropic_tools(await mcp.list_tools())
            assert any(t["name"] == "verify_ids" and t["input_schema"]["type"] == "object"
                       for t in tools)
            model = _ScriptedModel()
            case = {"id": "demo", "question": "What is doi:?", "kind": "answerable"}
            record = await R.run_case(model, mcp, tools, server.instructions, case, "m")
            return record, model

    record, model = asyncio.new_event_loop().run_until_complete(go())
    assert record["tool_calls"] == [{"name": "openalex_by_doi", "is_error": True,
                                     "chars": len("error: missing 'doi'")}]
    assert record["answer"] == "I could not look that DOI up."
    assert record["usage"] == {"input_tokens": 200, "output_tokens": 40}
    tool_result = model.calls[1]["messages"][-1]["content"][0]
    assert tool_result["is_error"] is True and tool_result["tool_use_id"] == "tu1"
