"""Agent-loop tests — `legumista_agent.loop.run_agent`, the heart of the tool-using
harness. The model endpoint is faked (a scripted `llm.complete`), so these are fully
deterministic: no network, no real completions. Each test drives one branch of the loop
(final answer, tool call + result, permission denial, unknown tool, tool exception, bad
arguments, id synthesis, the max-turns forced synthesis, and the two infra-failure paths)."""
import asyncio

import pytest

import llm
from legumista_agent.loop import run_agent
from legumista_agent.permissions import Permissions
from legumista_agent.tool import Tool


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def _tool(name, fn, *, read_only=True, writes=None):
    async def run(args, _fn=fn):
        return _fn(args)
    return Tool(name=name, description=name, read_only=read_only, run=run, writes=writes,
                parameters={"type": "object", "properties": {}})


def _script(monkeypatch, *responses):
    """Fake `llm.complete`: return `responses[i]` on the i-th call, recording each call's
    kwargs so tests can assert e.g. that the retry disabled tools."""
    calls = []

    def complete(messages, tools=None, model=None, temperature=None, timeout=None):
        calls.append({"tools": tools, "messages": [dict(m) for m in messages]})
        return responses[len(calls) - 1]

    monkeypatch.setattr(llm, "complete", complete)
    return calls


def _assistant(content=None, tool_calls=None):
    msg = {"content": content}
    if tool_calls is not None:
        msg["tool_calls"] = tool_calls
    return (msg, {"model": "fake"})


def _call(name, arguments, tid=None):
    tc = {"function": {"name": name, "arguments": arguments}}
    if tid is not None:
        tc["id"] = tid
    return tc


def test_final_answer_no_tools(monkeypatch):
    _script(monkeypatch, _assistant("hello", tool_calls=[]))
    res = _run(run_agent(user="q", tools=[]))
    assert res["ok"] and res["content"] == "hello" and res["turns"] == 1


def test_tool_call_then_answer(monkeypatch):
    echo = _tool("echo", lambda a: f"echoed:{a.get('x')}")
    _script(monkeypatch,
            _assistant(None, [_call("echo", '{"x": "hi"}', tid="c1")]),
            _assistant("done", tool_calls=[]))
    res = _run(run_agent(user="q", tools=[echo], max_turns=5))
    assert res["ok"] and res["content"] == "done" and res["turns"] == 2
    tool_msgs = [m for m in res["messages"] if m["role"] == "tool"]
    assert tool_msgs[0]["content"] == "echoed:hi"
    assert tool_msgs[0]["tool_call_id"] == "c1"          # id echoed back on the result


def test_permission_denied_is_reported_not_run(monkeypatch):
    ran = {"n": 0}

    def body(a):
        ran["n"] += 1
        return "wrote"
    writer = _tool("writer", body, read_only=False)      # a write tool
    _script(monkeypatch,
            _assistant(None, [_call("writer", "{}", tid="c1")]),
            _assistant("ok", tool_calls=[]))
    res = _run(run_agent(user="q", tools=[writer],
                         permissions=Permissions(mode="read_only")))
    tool_msg = next(m for m in res["messages"] if m["role"] == "tool")
    assert tool_msg["content"].startswith("error: permission denied")
    assert ran["n"] == 0                                  # denied before the tool body runs


def test_unknown_tool(monkeypatch):
    _script(monkeypatch,
            _assistant(None, [_call("ghost", "{}", tid="c1")]),
            _assistant("ok", tool_calls=[]))
    res = _run(run_agent(user="q", tools=[]))
    tool_msg = next(m for m in res["messages"] if m["role"] == "tool")
    assert tool_msg["content"] == "error: unknown tool 'ghost'"


def test_tool_exception_is_caught(monkeypatch):
    def boom(a):
        raise RuntimeError("kaboom")
    _script(monkeypatch,
            _assistant(None, [_call("bomb", "{}", tid="c1")]),
            _assistant("recovered", tool_calls=[]))
    res = _run(run_agent(user="q", tools=[_tool("bomb", boom)]))
    tool_msg = next(m for m in res["messages"] if m["role"] == "tool")
    assert tool_msg["content"].startswith("error: tool raised RuntimeError")
    assert res["ok"] and res["content"] == "recovered"   # loop survives a bad tool


def test_malformed_arguments_become_empty_dict(monkeypatch):
    seen = {}
    _script(monkeypatch,
            _assistant(None, [_call("echo", "{not valid json", tid="c1")]),
            _assistant("ok", tool_calls=[]))

    def capture(a):
        seen["args"] = a
        return "ran"
    _run(run_agent(user="q", tools=[_tool("echo", capture)]))
    assert seen["args"] == {}                             # bad JSON -> {}, no crash


def test_missing_tool_call_id_is_synthesized(monkeypatch):
    _script(monkeypatch,
            _assistant(None, [_call("echo", "{}")]),      # no id
            _assistant("ok", tool_calls=[]))
    res = _run(run_agent(user="q", tools=[_tool("echo", lambda a: "r")]))
    assistant = next(m for m in res["messages"]
                     if m["role"] == "assistant" and m.get("tool_calls"))
    tool_msg = next(m for m in res["messages"] if m["role"] == "tool")
    synth = assistant["tool_calls"][0]["id"]
    assert synth == "call_1_0"
    assert tool_msg["tool_call_id"] == synth             # echoed + result agree on the id


def test_max_turns_forces_final_synthesis(monkeypatch):
    calls = _script(monkeypatch,
                    _assistant(None, [_call("t", "{}", tid="a")]),   # turn 1: tool call
                    _assistant(None, [_call("t", "{}", tid="b")]),   # turn 2: tool call
                    _assistant("synthesized", tool_calls=[]))        # forced final, tools off
    res = _run(run_agent(user="q", tools=[_tool("t", lambda a: "x")],
                         max_turns=2, final_instruction="WRAP UP NOW"))
    assert res["ok"] and res["content"] == "synthesized" and res["turns"] == 2
    assert calls[-1]["tools"] is None                    # final synthesis call disables tools
    assert res["messages"][-2]["content"] == "WRAP UP NOW"   # custom final instruction used


def test_http_failure_on_first_turn_retries_tool_free(monkeypatch):
    calls = _script(monkeypatch,
                    (None, {"error": "HTTP 400 bad tools"}),          # tools rejected
                    _assistant("plain answer", tool_calls=[]))        # retry without tools
    res = _run(run_agent(user="q", tools=[_tool("t", lambda a: "x")]))
    assert res["ok"] and res["content"] == "plain answer" and res["turns"] == 1
    assert calls[1]["tools"] is None                     # retry disabled tools


def test_non_http_infra_failure_returns_error(monkeypatch):
    _script(monkeypatch, (None, {"error": "timeout"}))
    res = _run(run_agent(user="q", tools=[_tool("t", lambda a: "x")]))
    assert res["ok"] is False and res["error"] == {"error": "timeout"}
