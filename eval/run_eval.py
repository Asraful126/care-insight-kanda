"""Run the 20-question eval against the live assistant and grade it deterministically.

Each question gets a fresh Assistant (no shared history). Checks are described in eval/questions.yaml.
Writes eval/results/latest.md (committed: the pass rate) and eval/results/runs/<ts>.json (full answers,
git-ignored). Assistant logs for eval runs go to logs/eval.jsonl.

Usage: python eval/run_eval.py [--only D01,P02] [--workers 4]
       python eval/run_eval.py --regrade eval/results/runs/<ts>.json   (re-apply the checks, no API calls)
Cost: about US$1-2 per full run on claude-opus-5-5 (printed at the end).
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ.setdefault("ASSISTANT_LOG", str(ROOT / "logs/eval.jsonl"))

from assistant.agent import EFFORT, MODEL, Assistant  # noqa: E402

# US$ per million tokens for claude-opus-5-5 (Anthropic list price, Nov 2026)
PRICE = {"input": 4.00, "output": 20.00, "cache_read": 0.20, "cache_write": 5.00}
WRITE_SQL = re.compile(r"\b(insert|update|delete|merge|drop|alter|create|truncate|exec|execute|grant)\b", re.I)


def numbers_in(text: str) -> list[float]:
    found = []
    for m in re.finditer(r"(?<![\w.])-?\d[\d,]*(?:\.\d+)?", text):
        try:
            found.append(float(m.group().replace(",", "")))
        except ValueError:
            pass
    return found


def grade(q: dict, result: dict) -> list[str]:
    """Return the list of failed checks (empty = pass)."""
    answer, low = result["answer"], result["answer"].lower()
    fails = []
    tol = float(q.get("tol", 0))
    nums = numbers_in(answer)
    for item in q.get("numbers", []):
        options = item if isinstance(item, list) else [item]
        if not any(abs(n - float(o)) <= tol for o in options for n in nums):
            fails.append(f"number {options} not in answer")
    for group in q.get("any_of", []):
        if not any(str(p).lower() in low for p in group):
            fails.append(f"none of {group[:4]}{'…' if len(group) > 4 else ''}")
    for group in q.get("none_of", []):
        hit = [p for p in group if str(p).lower() in low]
        if hit:
            fails.append(f"forbidden phrase {hit}")
    for group in q.get("cite", []):
        if not any(str(c).lower() in low for c in group):
            fails.append(f"citation {group} missing")
    ok_tools = {c["tool"] for c in result["tool_calls"] if not c["is_error"]}
    for tool in q.get("tools", []):
        if tool not in ok_tools:
            fails.append(f"no successful {tool} call")
    if q.get("no_write"):
        for c in result["tool_calls"]:
            if c["tool"] == "query_gold" and not c["is_error"] and WRITE_SQL.search(c["input"].get("sql", "")):
                fails.append("a write statement ran")
    return fails


def cost(usage: dict) -> float:
    return sum(usage[k] * PRICE[k] for k in PRICE) / 1e6


def run_one(q: dict) -> dict:
    try:
        result = Assistant(session_id=f"eval-{q['id']}").ask(q["question"])
        fails = grade(q, result)
    except Exception as e:  # an infrastructure error is a failed question, not a crashed run
        result = {"answer": "", "tool_calls": [], "usage": dict.fromkeys(PRICE, 0), "elapsed_s": 0}
        fails = [f"error: {type(e).__name__}: {e}"]
    return {"id": q["id"], "category": q["category"], "question": q["question"], "passed": not fails,
            "fails": fails, "answer": result["answer"], "usage": result["usage"],
            "cost_usd": round(cost(result["usage"]), 4), "elapsed_s": result["elapsed_s"],
            "tool_calls": [{k: c[k] for k in ("tool", "input", "is_error")} for c in result["tool_calls"]]}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", help="comma-separated question ids")
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--regrade", help="saved run JSON to grade again with the current checks")
    args = ap.parse_args()

    questions = yaml.safe_load((ROOT / "eval/questions.yaml").read_text())
    if args.only:
        wanted = set(args.only.split(","))
        questions = [q for q in questions if q["id"] in wanted]

    if args.regrade:
        saved = {r["id"]: r for r in json.loads(Path(args.regrade).read_text())}
        rows = []
        for q in questions:
            r = saved[q["id"]]
            fails = grade(q, {"answer": r["answer"], "tool_calls": r["tool_calls"]})
            if r["passed"] and fails or not r["passed"] and not fails:
                print(f"{q['id']}: {'pass' if r['passed'] else 'fail'} -> {'fail' if fails else 'pass'}")
            rows.append({**r, "passed": not fails, "fails": fails})
    else:
        with ThreadPoolExecutor(max_workers=args.workers) as pool:
            rows = list(pool.map(run_one, questions))

    passed = sum(r["passed"] for r in rows)
    total_cost = sum(r["cost_usd"] for r in rows)
    ts = Path(args.regrade).stem + " (regraded)" if args.regrade else datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    by_cat = {}
    for r in rows:
        c = by_cat.setdefault(r["category"], [0, 0])
        c[0] += r["passed"]
        c[1] += 1

    out_dir = ROOT / "eval/results"
    (out_dir / "runs").mkdir(parents=True, exist_ok=True)
    if not args.regrade:
        (out_dir / "runs" / f"{ts}.json").write_text(json.dumps(rows, indent=2, ensure_ascii=False))

    lines = [
        "# Eval results (latest run)",
        "",
        f"- Run: {ts} · model `{MODEL}` · effort `{EFFORT}` · {len(rows)} questions",
        f"- **Pass rate: {passed}/{len(rows)} ({passed / len(rows):.0%})** · "
        + " · ".join(f"{k} {v[0]}/{v[1]}" for k, v in sorted(by_cat.items())),
        f"- Cost: US${total_cost:.2f} · mean {sum(r['elapsed_s'] for r in rows) / len(rows):.0f}s per question",
        "",
        "| ID | Result | Tool calls | Cost (US$) | Failed checks |",
        "|---|---|---|---|---|",
    ]
    for r in rows:
        lines.append(f"| {r['id']} | {'pass' if r['passed'] else '**FAIL**'} | {len(r['tool_calls'])} | "
                     f"{r['cost_usd']:.3f} | {'; '.join(r['fails']).replace('|', '/')} |")
    if not args.only:
        (out_dir / "latest.md").write_text("\n".join(lines) + "\n")
    print("\n".join(lines))


if __name__ == "__main__":
    main()
