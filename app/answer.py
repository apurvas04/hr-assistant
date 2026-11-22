"""Entry point kept stable: answer(question, user). The work is in app/orchestrator.py."""
from app.orchestrator import answer  # noqa: F401

if __name__ == "__main__":
    import argparse
    import time
    from app.identity import get_user

    ap = argparse.ArgumentParser()
    ap.add_argument("--user", default="E001", help="employee id from employees.json")
    ap.add_argument("question", nargs="+")
    args = ap.parse_args()
    user = get_user(args.user)
    t0 = time.time()
    out = answer(" ".join(args.question), user)
    print(f"[asking as {user.name} ({user.role})]  answered in {time.time() - t0:.1f}s\n")
    print(out["answer"])
    if out["tool_calls"]:
        print("\nTools used:")
        for t in out["tool_calls"]:
            print(f'  {t["name"]}({t["args"]}) -> {t["result"][:120]}')
    g = out.get("guardrails", {})
    if g.get("blocked") or g.get("output_actions"):
        print(f"\nGuardrails: blocked={g.get('blocked')} reason={g.get('input_reason')} "
              f"output_actions={g.get('output_actions')} repaired={g.get('repaired')}")
        if out.get("raw_answer") and out["raw_answer"] != out["answer"]:
            print(f"\nRaw model answer (before guard):\n{out['raw_answer']}")
    rt = out.get("route")
    if rt:
        print(f"\nRoute: {rt['tier']} -> {rt['model']}  reasons={rt['reasons']}"
              + ("  ESCALATED" if rt.get("escalated") else ""))
    print(f"Cache: {out.get('cache')}")
    if out.get("trace_id"):
        print(f"Trace: {out['trace_id']}   (python -m app.trace_view --id {out['trace_id'][:6]})")
    print("\nSources:")
    for i, s in enumerate(out["sources"], 1):
        print(f'  [{i}] {s["title"]} > {s["section"]}  ({s["status"]}, {s["access"]})')
