"""Tool eval (calls the LLM, so it uses your Groq quota):  python -m eval.run_tools"""
import json
import os
import time
from pathlib import Path

os.environ.setdefault("CACHE_ENABLED", "0")
os.environ.setdefault("TRACE_DIR", str(Path(__file__).resolve().parent.parent / "traces" / "eval"))

from app.answer import answer
from app.identity import get_user

CASES = Path(__file__).parent / "tool_questions.jsonl"


def main():
    cases = [json.loads(l) for l in open(CASES, encoding="utf-8") if l.strip()]
    passed = 0
    for c in cases:
        try:
            out = answer(c["question"], get_user(c["user"]))
        except Exception as e:
            print(f'{c["id"]} ERROR {str(e)[:80]}')
            continue
        text = out["answer"].lower()
        used = sorted({t["name"] for t in out["tool_calls"]})
        problems = []
        problems += [f'missing "{s}"' for s in c.get("must_contain", []) if s.lower() not in text]
        problems += [f'LEAK "{s}"' for s in c.get("must_not_contain", []) if s.lower() in text]
        if "expect_tools" in c and used != sorted(c["expect_tools"]):
            problems.append(f'tools used {used}, expected {sorted(c["expect_tools"])}')
        g = out.get("guardrails", {})
        if "expect_blocked" in c and g.get("blocked") != c["expect_blocked"]:
            problems.append(f'blocked={g.get("blocked")} ({g.get("input_reason")}), expected {c["expect_blocked"]}')
        # the raw tool results must never contain data the asker may not see
        for s in c.get("must_not_contain", []):
            if any(s in t["result"] for t in out["tool_calls"]):
                problems.append(f'LEAK "{s}" in a tool result')
        status = "PASS" if not problems else "FAIL"
        passed += not problems
        print(f'{c["id"]} {status}  [{c["user"]}] {c["question"]}')
        for p in problems:
            print(f"      {p}")
        if problems:
            print(f"      answer: {out['answer'][:200]!r}")
        time.sleep(1.5)
    print(f"\n{passed}/{len(cases)} passed")


if __name__ == "__main__":
    main()
