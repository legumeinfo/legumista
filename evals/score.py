"""Score a run: +1 correct, 0 declined, -1 wrong (plan rule R9).

A wrong answer costs more than a declined one on purpose. Without that penalty, a model
that always guesses outscores one that says "I could not verify this", which is the
behaviour the whole plan exists to remove.

Input is the JSONL that run.py writes (one record per case per repeat):

    {"id": ..., "repeat": 0, "answer": "...", "tool_calls": [{"name", "is_error", "chars"}],
     "tool_texts": ["...", ...], "usage": {"input_tokens", "output_tokens"},
     "judge": {"stance": "asserts" | "abstains",
               "verdict": "correct" | "incorrect" | "neither" | "n/a"}}

Grading, in order (first match wins):

1. -1 if the answer contains a `must_not_include` string, or (with
   `forbid_unsupported_ids`) an identifier that no tool returned, or the judge's rubric
   verdict is "incorrect".
2. +1 if every `must_include` item is present and, when the case has a rubric, the judge
   says "correct".
3. 0 if the judge says the answer abstains; otherwise -1 (it asserted something that was
   not the answer). A rubric verdict of "neither" (not the rubric's correct answer, not
   its named wrong one) lands here.

Usage:  python -m evals.score --cases evals/cases --run runs/<file>.jsonl [--baseline runs/<old>.jsonl]
"""
import argparse
import glob
import json
import os
import re
import sys

KINDS = ("answerable", "trap", "outage")


def load_cases(path: str) -> dict:
    import yaml  # evals-only dependency (pip install -e ".[evals]")
    files = sorted(glob.glob(os.path.join(path, "*.yaml"))) if os.path.isdir(path) else [path]
    cases = {}
    for name in files:
        for case in yaml.safe_load(open(name, encoding="utf-8")) or []:
            if case["id"] in cases:
                raise ValueError(f"duplicate case id {case['id']!r} in {name}")
            if case.get("kind") not in KINDS:
                raise ValueError(f"{case['id']}: kind must be one of {KINDS}")
            cases[case["id"]] = case
    return cases


def load_run(path: str) -> list:
    with open(path, encoding="utf-8") as handle:
        return [json.loads(line) for line in handle if line.strip()]


def _matches(item, text: str) -> bool:
    """Case-insensitive containment. A list means "any of". A bare number must stand
    alone, so "12" does not match inside "Glyma.12G040000" or "10" inside a DOI."""
    if isinstance(item, list):
        return any(_matches(alt, text) for alt in item)
    item = str(item).lower()
    if re.fullmatch(r"\d+", item):
        return re.search(rf"(?<![\w.]){item}(?!\w|\.\d)", text) is not None
    return item in text


def unsupported_ids(answer: str, tool_texts: list) -> list:
    """Identifiers in the answer that appear in no tool result of the same session: the
    cheapest deterministic hallucination check there is."""
    from legumista_agent.tools_verify import extract_ids
    seen = "\n".join(tool_texts).lower()
    found = extract_ids(answer or "")
    return [i for kind in ("dois", "genes", "collections", "assemblies")
            for i in found[kind] if i.lower() not in seen]


def grade(case: dict, record: dict) -> tuple:
    """(score, reason) for one record."""
    expect = case.get("expect") or {}
    answer = (record.get("answer") or "").lower()
    judge = record.get("judge") or {}
    rubric = bool(expect.get("rubric"))
    if rubric and judge.get("verdict") not in ("correct", "incorrect", "neither"):
        raise ValueError(f"{case['id']}: rubric case without a judge verdict")
    if judge.get("stance") not in ("asserts", "abstains"):
        raise ValueError(f"{case['id']}: judge stance missing")

    forbidden = [s for s in expect.get("must_not_include") or [] if _matches(s, answer)]
    if forbidden:
        return -1, f"contains {forbidden}"
    if expect.get("forbid_unsupported_ids"):
        extra = unsupported_ids(record.get("answer") or "", record.get("tool_texts") or [])
        if extra:
            return -1, f"identifiers no tool returned: {extra}"
    if rubric and judge["verdict"] == "incorrect":
        return -1, "judge: incorrect per rubric"
    missing = [s for s in expect.get("must_include") or [] if not _matches(s, answer)]
    if not missing and (not rubric or judge["verdict"] == "correct"):
        return 1, "correct"
    if judge["stance"] == "abstains":
        return 0, "declined" + (f" (missing {missing})" if missing else "")
    return -1, f"missing {missing}" if missing else "asserted, not correct"


def summarize(cases: dict, records: list) -> dict:
    graded = []
    for record in records:
        case = cases[record["id"]]
        score, reason = grade(case, record)
        graded.append({"id": record["id"], "kind": case["kind"], "score": score,
                       "reason": reason, "record": record})
    n = len(graded)

    def count(kind=None, score=None):
        return sum(1 for g in graded if (kind is None or g["kind"] == kind)
                   and (score is None or g["score"] == score))

    correct, wrong, declined = count(score=1), count(score=-1), count(score=0)
    calls = [c for g in graded for c in g["record"].get("tool_calls") or []]
    ids_in_answers = unsupported = 0
    from legumista_agent.tools_verify import extract_ids
    for g in graded:
        found = extract_ids(g["record"].get("answer") or "")
        ids_in_answers += sum(len(v) for v in found.values())
        unsupported += len(unsupported_ids(g["record"].get("answer") or "",
                                           g["record"].get("tool_texts") or []))
    usage = [g["record"].get("usage") or {} for g in graded]
    return {
        "records": n,
        "score": sum(g["score"] for g in graded) / n if n else 0.0,
        "accuracy": correct / n if n else 0.0,
        "precision": correct / (correct + wrong) if correct + wrong else 0.0,
        "wrong_rate": wrong / n if n else 0.0,
        "declined": declined,
        "over_abstention": (count("answerable", 0) / count("answerable")
                            if count("answerable") else 0.0),
        "trap_handling": count("trap", 1) / count("trap") if count("trap") else 0.0,
        "outage_honesty": count("outage", 1) / count("outage") if count("outage") else 0.0,
        "unsupported_ids": unsupported,
        "ids_in_answers": ids_in_answers,
        "tool_calls": len(calls),
        "tool_errors": sum(1 for c in calls if c.get("is_error")),
        "calls_per_record": len(calls) / n if n else 0.0,
        "input_tokens_per_record": sum(u.get("input_tokens", 0) for u in usage) / n if n else 0.0,
        "output_tokens_per_record": sum(u.get("output_tokens", 0) for u in usage) / n if n else 0.0,
        "wrong": [(g["id"], g["reason"]) for g in graded if g["score"] == -1],
    }


def format_summary(s: dict, baseline: dict = None) -> str:
    def delta(key, pct=True):
        if not baseline:
            return ""
        d = s[key] - baseline[key]
        return f"  ({'+' if d >= 0 else ''}{d * 100:.1f} pts)" if pct else f"  ({d:+.2f})"

    lines = [
        f"records: {s['records']}",
        f"score (+1/0/-1 mean): {s['score']:+.3f}{delta('score', pct=False)}",
        f"accuracy: {s['accuracy']:.1%}{delta('accuracy')}",
        f"precision (correct / attempted): {s['precision']:.1%}{delta('precision')}",
        f"wrong rate: {s['wrong_rate']:.1%}{delta('wrong_rate')}",
        f"over-abstention on answerable cases: {s['over_abstention']:.1%}",
        f"trap handling: {s['trap_handling']:.1%}   outage honesty: {s['outage_honesty']:.1%}",
        f"unsupported identifiers: {s['unsupported_ids']} of {s['ids_in_answers']} in answers",
        f"tool calls: {s['tool_calls']} ({s['calls_per_record']:.1f} per record), "
        f"isError: {s['tool_errors']}",
        f"tokens per record: {s['input_tokens_per_record']:.0f} in, "
        f"{s['output_tokens_per_record']:.0f} out",
    ]
    lines += [f"  WRONG {case}: {why}" for case, why in s["wrong"]]
    return "\n".join(lines)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--cases", default="evals/cases")
    parser.add_argument("--run", required=True)
    parser.add_argument("--baseline")
    args = parser.parse_args(argv)
    cases = load_cases(args.cases)
    current = summarize(cases, load_run(args.run))
    base = summarize(cases, load_run(args.baseline)) if args.baseline else None
    print(format_summary(current, base))
    return 0


if __name__ == "__main__":
    sys.exit(main())
