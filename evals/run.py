"""Run the eval cases: a Claude model answers each question with legumista's tools.

The server runs in-process (FastMCP's in-memory client), so a case can make hosts
unreachable by patching DNS for this process only (`inject.block_hosts`), and every tool
result, isError flag and token count is recorded for score.py. Nothing here runs in CI:
it needs an API key and the network, and it costs money.

Usage:
    pip install -e ".[evals]"
    ANTHROPIC_API_KEY=... python -m evals.run --model <model-id> \\
        --catalog /path/to/catalog.json --repeats 3 --out runs/$(date +%F).jsonl
    python -m evals.score --run runs/<date>.jsonl [--baseline runs/<older>.jsonl]
"""
import argparse
import asyncio
import contextlib
import hashlib
import json
import os
import socket
import sys

from evals.score import load_cases

HERE = os.path.dirname(os.path.abspath(__file__))

JUDGE_PROMPT = """You are grading one answer from a research assistant. Judge ONLY against
the rubric and the answer text. Do not use your own knowledge of the facts.

Question:
{question}

Answer:
{answer}

Rubric:
{rubric}

Return a JSON object and nothing else:
{{"stance": "abstains" if the answer declines to give the requested facts, or says they
could not be found or verified; otherwise "asserts",
 "verdict": {verdict_spec}}}"""

VERDICT_SPEC = ('"correct" if the answer does what the rubric calls correct, "incorrect" if '
                'it does what the rubric calls wrong, otherwise "neither"')


@contextlib.contextmanager
def blocked_hosts(hosts):
    """Make `hosts` unresolvable for this process, the way a DNS outage would. Every
    legumista request resolves its host in the SSRF guard (tools_native._validate_url)
    before connecting, even behind an HTTP proxy and even for htslib reads, so this cuts
    the listed hosts off for every tool. Never list the model API's host."""
    hosts = {h.lower() for h in hosts or ()}
    real = socket.getaddrinfo

    def guarded(host, *args, **kwargs):
        if str(host).lower() in hosts:
            raise socket.gaierror(socket.EAI_NONAME, f"eval: {host} is blocked")
        return real(host, *args, **kwargs)

    socket.getaddrinfo = guarded
    try:
        yield
    finally:
        socket.getaddrinfo = real


def pin_catalog(path: str, allow_drift: bool = False) -> None:
    """Load exactly the catalog the case expectations were read from."""
    lock = json.load(open(os.path.join(HERE, "catalog.lock.json"), encoding="utf-8"))
    digest = hashlib.sha256(open(path, "rb").read()).hexdigest()
    if digest != lock["sha256"] and not allow_drift:
        sys.exit(f"{path} is not the pinned catalog (sha256 {digest[:12]}…, expected "
                 f"{lock['sha256'][:12]}…, built {lock['built_at']} from "
                 f"{lock['source_commit'][:8]}). Pass --allow-catalog-drift to run anyway; "
                 "catalog cases may then fail for reasons unrelated to the change.")
    from legumista_agent import tools_catalog
    tools_catalog.CATALOG_PATH = path
    tools_catalog.reset()


def reset_process_caches() -> None:
    """Cases must not see each other's cached answers: an outage case would otherwise be
    served a success cached by an earlier case."""
    from legumista_agent import tools_mine
    tools_mine.reset_cache()
    try:
        from legumista_agent import pubstatus
    except ImportError:              # a baseline run on a commit that predates R-01
        return
    with pubstatus._LOCK:
        pubstatus._CACHE.clear()


def to_anthropic_tools(mcp_tools) -> list:
    return [{"name": t.name, "description": t.description or "",
             "input_schema": getattr(t, "input_schema", None) or getattr(t, "inputSchema")}
            for t in mcp_tools]


async def run_case(ai, mcp, tools, system, case, model, max_turns=25):
    """One question, answered with tools. Returns the record score.py reads."""
    record = {"id": case["id"], "tool_calls": [], "tool_texts": [],
              "usage": {"input_tokens": 0, "output_tokens": 0}, "answer": ""}
    messages = [{"role": "user", "content": case["question"]}]
    with blocked_hosts((case.get("inject") or {}).get("block_hosts")):
        for _turn in range(max_turns):
            resp = await ai.messages.create(model=model, max_tokens=4096, system=system,
                                            tools=tools, messages=messages)
            record["usage"]["input_tokens"] += resp.usage.input_tokens
            record["usage"]["output_tokens"] += resp.usage.output_tokens
            messages.append({"role": "assistant", "content": resp.content})
            if resp.stop_reason != "tool_use":
                record["answer"] = "".join(b.text for b in resp.content if b.type == "text")
                break
            results = []
            for block in resp.content:
                if block.type != "tool_use":
                    continue
                res = await mcp.call_tool(block.name, block.input, raise_on_error=False)
                text = "".join(c.text for c in res.content if getattr(c, "type", "") == "text")
                record["tool_calls"].append({"name": block.name, "is_error": bool(res.is_error),
                                             "chars": len(text)})
                record["tool_texts"].append(text)
                results.append({"type": "tool_result", "tool_use_id": block.id,
                                "content": text, "is_error": bool(res.is_error)})
            messages.append({"role": "user", "content": results})
        else:
            record["stopped"] = f"no final answer after {max_turns} turns"
    return record


async def judge(ai, model, case, answer) -> dict:
    rubric = (case.get("expect") or {}).get("rubric") or ""
    prompt = JUDGE_PROMPT.format(question=case["question"], answer=answer or "(no answer)",
                                 rubric=rubric or "(none)",
                                 verdict_spec=VERDICT_SPEC if rubric else '"n/a"')
    resp = await ai.messages.create(model=model, max_tokens=200, temperature=0,
                                    messages=[{"role": "user", "content": prompt}])
    text = "".join(b.text for b in resp.content if b.type == "text").strip()
    start, end = text.find("{"), text.rfind("}")
    return json.loads(text[start:end + 1])


async def run_all(args, ai=None) -> list:
    from fastmcp import Client
    from legumista_agent.mcp_server import build_server
    if args.catalog:
        pin_catalog(args.catalog, args.allow_catalog_drift)
    cases = load_cases(args.cases)
    if args.only:
        cases = {k: v for k, v in cases.items() if k in set(args.only.split(","))}
    if ai is None:
        from anthropic import AsyncAnthropic  # evals-only dependency
        ai = AsyncAnthropic()
    server = build_server()
    records = []
    async with Client(server) as mcp:
        tools = to_anthropic_tools(await mcp.list_tools())
        for case in cases.values():
            for repeat in range(args.repeats):
                reset_process_caches()
                record = await run_case(ai, mcp, tools, server.instructions, case, args.model)
                record["repeat"] = repeat
                record["judge"] = await judge(ai, args.judge_model or args.model, case,
                                              record["answer"])
                records.append(record)
                print(f"{case['id']} #{repeat}: {len(record['tool_calls'])} call(s), "
                      f"judge={record['judge']}", file=sys.stderr)
    return records


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--model", required=True, help="model id to evaluate")
    parser.add_argument("--judge-model", help="model id for the stance/rubric judge "
                                              "(default: --model)")
    parser.add_argument("--cases", default=os.path.join(HERE, "cases"))
    parser.add_argument("--catalog", help="path to the pinned catalog.json")
    parser.add_argument("--allow-catalog-drift", action="store_true")
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--only", help="comma-separated case ids")
    parser.add_argument("--out", required=True)
    args = parser.parse_args(argv)
    records = asyncio.run(run_all(args))
    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as handle:
        for record in records:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")
    print(f"wrote {len(records)} record(s) to {args.out}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
