#!/usr/bin/env python3
"""Tool interface for the agent harness.

A tool is a name + description + JSON schema for its arguments, a read-only flag (for the
permission gate), and an async `run(args) -> str`. `to_openai()` renders the OpenAI
function-tool spec sent in the request's `tools` array (OpenAI spec
https://github.com/openai/openai-openapi: ChatCompletionTool / FunctionObject).

Naming convention: tool names are **snake_case** throughout. This is what the
OpenAI-compatible ecosystem uses (every example in the OpenAI function-calling guide and
Anthropic tool-use docs is snake_case), it satisfies the tool-name grammar
`^[a-zA-Z0-9_-]{1,128}$`, it tokenizes on clean word boundaries, and it matches the models'
training distribution — all of which improve name→intent recognition and reliable
selection.
"""
from dataclasses import dataclass
from typing import Awaitable, Callable, Optional

# args (dict) -> result text (awaited)
ToolRun = Callable[[dict], Awaitable[str]]
# args (dict) -> True if THIS specific call writes (mutates disk/state). Optional: for
# tools whose read/write nature depends on the arguments — e.g. a `samtools` dispatcher
# where `view` reads but `sort` writes. The permission gate consults it per call.
WritesFn = Callable[[dict], bool]


@dataclass
class Tool:
    name: str
    description: str
    parameters: dict          # JSON Schema, type "object"
    read_only: bool
    run: ToolRun
    # Per-call write classifier. When None, `read_only` is authoritative for the whole
    # tool. When set, the gate treats a call as a write iff `writes(args)` is True, so one
    # tool can expose both read and write operations under a single schema.
    writes: Optional[WritesFn] = None

    def to_openai(self) -> dict:
        """Render the OpenAI function-tool spec.

        We normalise the parameter schema for reliable function calling:
        - guarantee a well-formed `{"type": "object", "properties": {...}}` envelope, and
        - set `additionalProperties: false` so the model is discouraged from inventing
          argument keys the tool does not accept.

        We deliberately do *not* emit `strict: true`. Strict mode requires every property
        to be listed in `required` (optionals modelled as nullable) and is not uniformly
        supported across the OpenAI-compatible endpoints legumista targets (local ollama,
        small llama models, arbitrary hosted servers). `additionalProperties: false` plus
        clear, imperative descriptions gives most of the reliability benefit while staying
        portable.
        """
        params = dict(self.parameters or {"type": "object", "properties": {}})
        params.setdefault("type", "object")
        params.setdefault("properties", {})
        params.setdefault("additionalProperties", False)
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description or "",
                "parameters": params,
            },
        }
