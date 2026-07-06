#!/usr/bin/env python3
"""The agent loop — a bounded tool-calling loop over the chat-completions API.

    messages + tools -> /chat/completions
      -> if the reply has tool_calls: run each (permission-gated), append a
         {role:"tool", tool_call_id, content} message per call, loop
      -> else: return the final assistant text

Bounded by `max_turns`. The LLM call (`llm.complete`, blocking urllib) runs in a
worker thread so it doesn't block the asyncio loop that owns the MCP sessions.
"""
import asyncio
import json

import llm

from .permissions import Permissions


async def run_agent(*, user, tools, system=None, model=None, max_turns=8,
                    permissions=None, temperature=None, timeout=None, on_event=None,
                    final_instruction=None):
    """Run the tool loop to a final answer. Returns a result dict:
       {ok, content, messages, meta, turns}  or  {ok: False, error, messages}."""
    permissions = permissions or Permissions(mode="read_only")
    by_name = {t.name: t for t in tools}
    specs = [t.to_openai() for t in tools]

    messages = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": user})

    last_meta = {}
    for turn in range(1, max_turns + 1):
        message, meta = await asyncio.to_thread(
            llm.complete, messages, tools=specs, model=model,
            temperature=temperature, timeout=timeout)
        last_meta = meta
        if message is None:                       # infra/HTTP failure
            # H3: the endpoint may reject requests carrying `tools` (no function-calling
            # support). On the FIRST turn, retry once tool-free so a plain model still
            # yields an answer instead of failing the whole run.
            if turn == 1 and specs and str((meta or {}).get("error", "")).startswith("HTTP"):
                message, meta = await asyncio.to_thread(
                    llm.complete, messages, tools=None, model=model,
                    temperature=temperature, timeout=timeout)
                if message is not None:
                    messages.append(_assistant_msg(message, []))
                    if on_event:
                        on_event({"type": "assistant", "turn": turn,
                                  "content": message.get("content"), "tool_calls": []})
                    return {"ok": True, "content": message.get("content") or "",
                            "messages": messages, "meta": meta, "turns": turn}
            return {"ok": False, "error": meta, "messages": messages, "turns": turn}

        tool_calls = message.get("tool_calls") or []
        # M4: some providers omit tool_call ids. Synthesize a stable one and reuse it in
        # BOTH the echoed assistant tool_calls and the matching tool-result message.
        for i, tc in enumerate(tool_calls):
            if isinstance(tc, dict) and not tc.get("id"):
                tc["id"] = f"call_{turn}_{i}"
        messages.append(_assistant_msg(message, tool_calls))
        if on_event:
            on_event({"type": "assistant", "turn": turn,
                      "content": message.get("content"),
                      "tool_calls": [(_tc_name(tc)) for tc in tool_calls]})

        if not tool_calls:                        # final answer
            return {"ok": True, "content": message.get("content") or "",
                    "messages": messages, "meta": meta, "turns": turn}

        for tc in tool_calls:
            result = await _run_tool_call(tc, by_name, permissions, on_event)
            messages.append({"role": "tool", "tool_call_id": tc.get("id"),
                             "content": result})

    # Turn ceiling hit while the model was still calling tools. Rather than discard
    # everything it gathered, make one final call with tools disabled so it must
    # synthesize an answer from the tool output collected so far — the phase gets a
    # usable result instead of a failure. Callers can pass `final_instruction` to keep
    # a required output format (e.g. a JSON verdict) on this forced last turn.
    messages.append({"role": "user", "content": final_instruction or
                     "You have reached the tool-call budget. Do not call any more tools — "
                     "give your best final answer now, using what you have gathered."})
    message, meta = await asyncio.to_thread(
        llm.complete, messages, tools=None, model=model,
        temperature=temperature, timeout=timeout)
    if message is None:
        return {"ok": False, "error": meta, "content": None,
                "messages": messages, "turns": max_turns}
    messages.append(_assistant_msg(message, []))
    return {"ok": True, "content": message.get("content") or "",
            "messages": messages, "meta": meta, "turns": max_turns}


def _assistant_msg(message, tool_calls):
    """The assistant message to send back on the next request: content (empty string
    if null, which every endpoint accepts) plus any tool_calls verbatim."""
    content = message.get("content")
    out = {"role": "assistant", "content": content if content is not None else ""}
    if tool_calls:
        out["tool_calls"] = tool_calls
    return out


def _tc_name(tc):
    return ((tc or {}).get("function") or {}).get("name") or "?"


async def _run_tool_call(tc, by_name, permissions, on_event):
    fn = (tc or {}).get("function") or {}
    name = fn.get("name") or ""
    # Arguments usually arrive as a JSON string, but some providers return an
    # already-parsed dict — json.loads on a dict raises TypeError, so handle both.
    raw = fn.get("arguments")
    try:
        args = raw if isinstance(raw, dict) else json.loads(raw or "{}")
        if not isinstance(args, dict):
            args = {}
    except (TypeError, ValueError):
        args = {}

    tool = by_name.get(name)
    if tool is None:
        result = f"error: unknown tool '{name}'"
    else:
        decision = permissions.check(tool, args)
        if not decision.allowed:
            result = f"error: permission denied ({decision.reason})"
        else:
            try:
                result = await tool.run(args)
            except Exception as e:  # noqa: BLE001 - a bad tool call must not kill the loop
                result = f"error: tool raised {type(e).__name__}: {e}"

    if not isinstance(result, str):
        result = json.dumps(result, ensure_ascii=False)
    if on_event:
        on_event({"type": "tool_result", "tool": name, "args": args, "chars": len(result)})
    return result
