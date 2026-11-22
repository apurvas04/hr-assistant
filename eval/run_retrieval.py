"""Retrieval eval.  Run from the project root:

    python -m eval.run_retrieval                 # retrieval only (fast, no LLM)
    python -m eval.run_retrieval --label baseline
    python -m eval.run_retrieval --answers       # also check final answers (calls the LLM)
"""
import argparse
import json
import os
import re
import statistics
import time
from collections import defaultdict
from pathlib import Path

os.environ.setdefault("CACHE_ENABLED", "0")   # measure the model, not the cache
os.environ.setdefault("TRACE_DIR", str(Path(__file__).resolve().parent.parent / "traces" / "eval"))

from app.identity import get_user
from app.retrieve import retrieve

EVAL_DIR = Path(__file__).parent
QUESTIONS = EVAL_DIR / "questions.jsonl"
RESULTS_DIR = EVAL_DIR / "results"
K = 5

# each question runs as the LEAST privileged user who is allowed to see its answer
USER_FOR = {None: "E001", "managers": "E002", "hr": "E003"}


def load_questions():
    with open(QUESTIONS, encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


def rank_of(hits, doc_id, section=None):
    """1-based rank of the first hit matching the doc (and section), else None."""
    for i, h in enumerate(hits, 1):
        if h["doc_id"] == doc_id and (section is None or h["section"] == section):
            return i
    return None


def evaluate(questions, mode="vector", include_archived=True):
    rows = []
    for q in questions:
        user = get_user(USER_FOR[q.get("access_needed")])
        hits = retrieve(q["question"], user, k=K, mode=mode, include_archived=include_archived)
        # leak check: the same question asked by an ordinary employee must never
        # return anything above access "all"
        emp_hits = retrieve(q["question"], get_user("E001"), k=K, mode=mode,
                            include_archived=include_archived)
        row = {
            "leak_as_employee": any(h["access"] != "all" for h in emp_hits),
            "id": q["id"], "category": q["category"], "question": q["question"],
            "top_distance": float(hits[0]["distance"]) if hits else None,
            "archived_in_topk": any(h["status"] == "archived" for h in hits),
            "restricted_in_topk": any(h["access"] != "all" for h in hits),
            "retrieved": [f'{h["doc_id"]} > {h["section"]}' for h in hits],
        }
        if q["expected_doc"]:
            row["doc_rank"] = rank_of(hits, q["expected_doc"])
            row["chunk_rank"] = rank_of(hits, q["expected_doc"], q["expected_section"])
            row["expected"] = f'{q["expected_doc"]} > {q["expected_section"]}'
            if q["category"] == "version_trap":
                cur = rank_of(hits, q["expected_doc"])
                arch = rank_of(hits, q["expected_doc"] + "-v1")
                row["current_beats_archived"] = cur is not None and (arch is None or cur < arch)
        rows.append(row)
    return rows


def pct(n, d):
    return f"{100 * n / d:5.1f}%" if d else "   n/a"


def summarize(rows):
    answerable = [r for r in rows if "doc_rank" in r]
    n = len(answerable)

    def hit(key, k):
        return sum(1 for r in answerable if r[key] is not None and r[key] <= k)

    mrr = statistics.mean(1 / r["chunk_rank"] if r["chunk_rank"] else 0 for r in answerable)
    summary = {
        "n_answerable": n,
        "doc_hit@1": hit("doc_rank", 1) / n, "doc_hit@3": hit("doc_rank", 3) / n,
        f"doc_hit@{K}": hit("doc_rank", K) / n,
        "chunk_hit@1": hit("chunk_rank", 1) / n, "chunk_hit@3": hit("chunk_rank", 3) / n,
        f"chunk_hit@{K}": hit("chunk_rank", K) / n,
        "chunk_mrr": mrr,
        "archived_in_topk_rate": sum(r["archived_in_topk"] for r in rows) / len(rows),
        "leaks_as_employee": sum(r["leak_as_employee"] for r in rows),
    }
    ver = [r for r in rows if r["category"] == "version_trap"]
    summary["version_current_beats_archived"] = (
        sum(r["current_beats_archived"] for r in ver) / len(ver) if ver else None)
    in_d = [r["top_distance"] for r in answerable if r["top_distance"] == r["top_distance"]]
    out_d = [r["top_distance"] for r in rows
             if r["category"] == "out_of_scope" and r["top_distance"] == r["top_distance"]]
    summary["avg_top_distance_in_scope"] = statistics.mean(in_d) if in_d else None
    summary["avg_top_distance_out_of_scope"] = statistics.mean(out_d) if out_d else None

    print("\n=== OVERALL (answerable questions: %d) ===" % n)
    print(f"  doc   hit@1 {pct(hit('doc_rank',1),n)}   hit@3 {pct(hit('doc_rank',3),n)}   hit@{K} {pct(hit('doc_rank',K),n)}")
    print(f"  chunk hit@1 {pct(hit('chunk_rank',1),n)}   hit@3 {pct(hit('chunk_rank',3),n)}   hit@{K} {pct(hit('chunk_rank',K),n)}   MRR {mrr:.3f}")
    print(f"  archived chunk in top-{K}, any question: {pct(sum(r['archived_in_topk'] for r in rows), len(rows))}")
    print(f"  LEAKS (restricted chunk returned to an ordinary employee): {summary['leaks_as_employee']} of {len(rows)} questions")
    if ver:
        print(f"  version traps where CURRENT policy ranks above archived: {sum(r['current_beats_archived'] for r in ver)}/{len(ver)}")
    if in_d:
        print(f"  avg top-1 vector distance: in-scope {summary['avg_top_distance_in_scope']:.3f}"
              + (f" | out-of-scope {summary['avg_top_distance_out_of_scope']:.3f}" if out_d else ""))

    print("\n=== BY CATEGORY (chunk hit@3) ===")
    cats = defaultdict(list)
    for r in answerable:
        cats[r["category"]].append(r)
    for cat, rs in sorted(cats.items()):
        ok = sum(1 for r in rs if r["chunk_rank"] and r["chunk_rank"] <= 3)
        print(f"  {cat:14} {ok}/{len(rs)}  {pct(ok, len(rs))}")

    misses = [r for r in answerable if not r["chunk_rank"] or r["chunk_rank"] > 1]
    if misses:
        print("\n=== NOT RANK 1 (expected chunk) ===")
        for r in misses:
            print(f"  {r['id']} [{r['category']}] rank={r['chunk_rank']}  {r['question']}")
            print(f"      expected: {r['expected']}")
            print(f"      top 3:    {r['retrieved'][:3]}")
    return summary


_WORDS = {"one": "1", "two": "2", "three": "3", "four": "4", "five": "5", "six": "6", "seven": "7",
          "eight": "8", "nine": "9", "ten": "10", "twelve": "12", "fifteen": "15", "eighteen": "18",
          "twenty": "20", "thirty": "30", "sixty": "60"}


def numbers_as_digits(text: str) -> str:
    """'three days' -> '3 days', so the eval accepts spelled-out numbers."""
    return re.sub(r"\b(" + "|".join(_WORDS) + r")\b", lambda m: _WORDS[m.group(1)], text)


def answer_check(questions):
    from app.answer import answer
    print("\n=== ANSWER CHECK (does the final answer contain the key fact?) ===")
    checked = ok = 0
    route_stats = {"small": 0, "large": 0, "escalated": 0, "repaired": 0}
    for q in questions:
        if not q["expected_doc"] or not q["answer_contains"]:
            continue
        try:
            out = answer(q["question"], get_user(USER_FOR[q.get("access_needed")]))
            text = out["answer"]
        except Exception as e:
            print(f"  {q['id']} ERROR {str(e)[:60]}")
            continue
        low = numbers_as_digits(text.lower())
        raw = out.get("raw_answer")
        rt = out.get("route") or {}
        if rt.get("tier") in route_stats:
            route_stats[rt["tier"]] += 1
        route_stats["escalated"] += bool(rt.get("escalated"))
        route_stats["repaired"] += bool(out.get("guardrails", {}).get("repaired"))
        passed = (all(s.lower() in low for s in q["answer_contains"])
                  and not any(s.lower() in low for s in q.get("answer_must_not_contain", [])))
        checked += 1
        ok += passed
        if not passed:
            print(f"  {q['id']} FAIL  {q['question']}\n      wanted {q['answer_contains']}"
                  f"  must-not {q.get('answer_must_not_contain', [])}\n      got    {text[:140]!r}"
                  f"\n      guard  {out.get('guardrails', {}).get('output_actions')}"
                  + (f"\n      RAW    {raw[:300]!r}" if raw and raw != text else ""))
        time.sleep(1)
    print(f"  answer pass rate: {ok}/{checked}")
    print(f"  routing: {route_stats['small']} small, {route_stats['large']} large, "
          f"{route_stats['escalated']} escalated small->large, {route_stats['repaired']} answers needed a citation repair")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--label", default="latest", help="name for the saved results file")
    ap.add_argument("--mode", choices=["vector", "vector_rerank"], default="vector_rerank")
    ap.add_argument("--include-archived", action="store_true",
                    help="keep archived policies in the index search (the Step 2 baseline)")
    ap.add_argument("--answers", action="store_true", help="also run the LLM answer check")
    args = ap.parse_args()

    questions = load_questions()
    from app import prompts
    pv = {n: f"{prompts.get(n).version}@{prompts.get(n).sha}" for n in ("system", "repair")}
    print(f"mode={args.mode}  include_archived={args.include_archived}  prompts={pv}")
    rows = evaluate(questions, args.mode, args.include_archived)
    summary = summarize(rows)

    RESULTS_DIR.mkdir(exist_ok=True)
    out = RESULTS_DIR / f"{args.label}.json"
    out.write_text(json.dumps({"prompts": pv, "mode": args.mode, "include_archived": args.include_archived,
                               "summary": summary, "rows": rows}, indent=2), encoding="utf-8")
    print(f"\nSaved {out}")

    if args.answers:
        answer_check(questions)


if __name__ == "__main__":
    main()
