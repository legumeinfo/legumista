#!/usr/bin/env python3
"""
Minimal OpenAI-compatible Chat Completions client (stdlib only).

The pipeline talks the OpenAI /v1/chat/completions API directly — to ollama
(http://localhost:11434/v1), OpenRouter (https://openrouter.ai/api/v1), or any
OpenAI-compatible endpoint — so there is no proxy or external runtime in the model
path. Request/response shapes follow the official OpenAI spec
(https://github.com/openai/openai-openapi):

  request  POST {base_url}/chat/completions
           { "model", "messages":[...], "temperature", "max_completion_tokens"?,
             "response_format"?, "tools"?, "tool_choice"?, "stream": false }
  response { "choices":[{"message":{"content", "tool_calls"?}, "finish_reason"}], "usage" }

Two entry points:
  - `complete(messages, tools=...)` — full control; returns the assistant *message*
    dict (content + any tool_calls). The agentic harness (legumista_agent) uses this.
  - `chat(prompt, system=...)` — single-turn text convenience for the pipeline;
    returns just the content string.

Endpoint/model/key come from config.llm() (legumista.yml `llm` + LLM_* env).
"""
import json
import urllib.error
import urllib.request

import config


def complete(messages: list, *, tools: list = None, tool_choice=None,
             model: str = None, small: bool = False, json_mode: bool = None,
             temperature: float = None, timeout: int = None):
    """One chat completion over a full message list. Returns (message, meta):
      message — the assistant message dict (`{"role","content","tool_calls"?}`),
                or None on any failure
      meta    — {model, usage, finish_reason} on success; {error, detail, model}
                on failure
    Never raises for network/HTTP/parse issues — the caller decides how to react."""
    c = config.llm()
    url = f"{(c['base_url'] or '').rstrip('/')}/chat/completions"
    mdl = model or (c["small_model"] if small and c["small_model"] else c["model"])

    body = {
        "model": mdl,
        "messages": messages,
        "temperature": c["temperature"] if temperature is None else temperature,
        "stream": False,
    }
    if c["max_tokens"]:            # spec: max_completion_tokens (max_tokens is deprecated)
        body["max_completion_tokens"] = c["max_tokens"]
    # json_mode and tools don't mix: many endpoints reject the combination or suppress
    # tool_calls when response_format is forced, so only request JSON when NOT tool-calling.
    if (c["json_mode"] if json_mode is None else json_mode) and not tools:
        body["response_format"] = {"type": "json_object"}
    if tools:
        body["tools"] = tools
        body["tool_choice"] = tool_choice or "auto"

    headers = {"Content-Type": "application/json"}
    key = config.llm_api_key()
    if key:
        headers["Authorization"] = f"Bearer {key}"
    headers.update(c["headers"])   # e.g. OpenRouter HTTP-Referer / X-Title

    req = urllib.request.Request(url, data=json.dumps(body).encode("utf-8"),
                                 headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout or c["timeout"]) as resp:
            payload = json.load(resp)
    except urllib.error.HTTPError as e:
        detail = ""
        try:
            detail = e.read().decode("utf-8", "replace")[:300]
        except Exception:  # noqa: BLE001
            pass
        return None, {"error": f"HTTP {e.code}", "detail": detail, "model": mdl}
    except Exception as e:  # noqa: BLE001 - network is best-effort
        return None, {"error": type(e).__name__, "detail": str(e)[:300], "model": mdl}

    try:
        message = payload["choices"][0]["message"]
    except (KeyError, IndexError, TypeError):
        return None, {"error": "malformed response",
                      "detail": json.dumps(payload)[:300], "model": mdl}
    meta = {"model": payload.get("model", mdl), "usage": payload.get("usage"),
            "finish_reason": (payload.get("choices") or [{}])[0].get("finish_reason")}
    return message, meta


def chat(prompt: str, *, system: str = None, model: str = None, small: bool = False,
         json_mode: bool = None, temperature: float = None, timeout: int = None):
    """Single-turn text convenience (no tools) for the pipeline. Returns
    (content, meta) where content is the assistant text (str) or None on failure or
    empty/refusal reply."""
    messages = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": prompt})
    message, meta = complete(messages, model=model, small=small, json_mode=json_mode,
                             temperature=temperature, timeout=timeout)
    if message is None:
        return None, meta
    content = message.get("content")
    if content is None:            # spec allows content: null (e.g. refusal / tool-only)
        return None, {"error": "empty content", "detail": json.dumps(message)[:300],
                      "model": meta.get("model")}
    return content, meta
