"""Show which model tier every eval question would use (retrieval + router, NO LLM calls):

    python -m eval.run_router
"""
import json
from collections import Counter
from pathlib import Path

from app.guardrails import check_input
from app.identity import get_user
from app.retrieve import retrieve
from app.router import route

EVAL = Path(__file__).parent
USER_FOR = {None: "E001", "managers": "E002", "hr": "E003"}


def main():
    items = []
    for line in open(EVAL / "questions.jsonl", encoding="utf-8"):
        if line.strip():
            q = json.loads(line)
            items.append((q["id"], q["category"], q["question"], USER_FOR[q.get("access_needed")]))
    for line in open(EVAL / "tool_questions.jsonl", encoding="utf-8"):
        if line.strip():
            q = json.loads(line)
            items.append((q["id"], "tool", q["question"], q["user"]))

    tiers, by_reason = Counter(), Counter()
    print(f'{"id":4} {"tier":5} {"category":13} reasons / question')
    for qid, cat, text, uid in items:
        if not check_input(text).allowed:
            continue                                   # attacks never reach the router
        d = route(text, retrieve(text, get_user(uid)))
        tiers[d["tier"]] += 1
        by_reason.update(d["reasons"])
        print(f'{qid:4} {d["tier"]:5} {cat:13} {",".join(d["reasons"])}  |  {text[:60]}')
    total = sum(tiers.values())
    print(f"\nsmall: {tiers['small']}/{total}   large: {tiers['large']}/{total}")
    print("reasons:", dict(by_reason))


if __name__ == "__main__":
    main()
