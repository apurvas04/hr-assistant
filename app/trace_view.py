"""Read traces.

    python -m app.trace_view                 last 5 traces as trees
    python -m app.trace_view --last 20
    python -m app.trace_view --id 3fa9       one trace (id prefix)
    python -m app.trace_view --errors        traces with an error or a failed model call
    python -m app.trace_view --summary       aggregate numbers (latency, cache, routing, tokens, failures)
    python -m app.trace_view --dir traces/eval --summary
"""
import argparse
import json
from collections import Counter
from pathlib import Path

from app.tracing import trace_dir


def load(directory: Path) -> list:
    traces = []
    for f in sorted(Path(directory).glob("*.jsonl")):
        for line in open(f, encoding="utf-8"):
            if line.strip():
                traces.append(json.loads(line))
    return traces


def pct(values, p):
    if not values:
        return 0.0
    v = sorted(values)
    return v[min(len(v) - 1, int(round(p / 100 * (len(v) - 1))))]


def summarize(traces: list) -> dict:
    n = len(traces)
    oc = [t.get("outcome", {}) for t in traces]
    spans = [s for t in traces for s in t["spans"]]
    llm = [s for s in spans if s["name"] == "llm.call"]
    ok_llm = [s for s in llm if s["status"] == "ok"]
    by_name = {}
    for s in spans:
        by_name.setdefault(s["name"], []).append(s["duration_ms"])
    lat = [t["duration_ms"] for t in traces]
    hit = [t["duration_ms"] for t, o in zip(traces, oc) if str(o.get("cache", "")).startswith(("exact", "semantic"))]
    miss = [t["duration_ms"] for t, o in zip(traces, oc) if o.get("cache") in ("miss", "bypass")]
    return {
        "traces": n,
        "errors": sum(t["status"] == "error" for t in traces),
        "blocked": sum(bool(o.get("blocked")) for o in oc),
        "cache": dict(Counter(str(o.get("cache")) for o in oc)),
        "tiers": dict(Counter(o.get("tier") for o in oc if o.get("tier"))),
        "escalated": sum(bool(o.get("escalated")) for o in oc),
        "repaired": sum(bool(o.get("repaired")) for o in oc),
        "output_actions": dict(Counter(a for o in oc for a in (o.get("output_actions") or []))),
        "latency_ms": {"p50": pct(lat, 50), "p95": pct(lat, 95),
                       "cache_hit_p50": pct(hit, 50), "cache_miss_p50": pct(miss, 50)},
        "stage_avg_ms": {k: round(sum(v) / len(v), 1) for k, v in sorted(by_name.items())},
        "llm_calls": len(llm),
        "llm_failed": dict(Counter(str(s["attrs"].get("status_code", s["attrs"].get("error")))
                                   for s in llm if s["status"] == "error")),
        "tokens_prompt": sum(s["attrs"].get("prompt_tokens") or 0 for s in ok_llm),
        "tokens_completion": sum(s["attrs"].get("completion_tokens") or 0 for s in ok_llm),
        "by_model": dict(Counter(s["attrs"].get("model") for s in ok_llm)),
    }


def render(trace: dict) -> str:
    o = trace.get("outcome", {})
    u = trace["user"]
    lines = [f'{trace["trace_id"]}  {trace["ts"]}  {trace["status"].upper()}  {trace["duration_ms"]:.0f} ms  '
             f'{u["id"]} ({u["role"]}, group={u["group"]})',
             f'  question: {trace["question"]}',
             f'  outcome:  {json.dumps(o, ensure_ascii=False)}']
    children = {}
    for s in trace["spans"]:
        children.setdefault(s["parent"], []).append(s)

    def walk(parent, depth):
        for s in children.get(parent, []):
            attrs = {k: v for k, v in s["attrs"].items()}
            mark = "" if s["status"] == "ok" else "  !! ERROR"
            lines.append(f'  {"  " * depth}- {s["name"]:<18} {s["duration_ms"]:>8.1f} ms{mark}  '
                         f'{json.dumps(attrs, ensure_ascii=False) if attrs else ""}')
            walk(s["id"], depth + 1)

    walk(None, 0)
    return "\n".join(lines)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir", default=None)
    ap.add_argument("--last", type=int, default=5)
    ap.add_argument("--id")
    ap.add_argument("--errors", action="store_true")
    ap.add_argument("--summary", action="store_true")
    args = ap.parse_args()

    traces = load(Path(args.dir) if args.dir else trace_dir())
    if not traces:
        print("No traces found. Ask a question first (python -m app.answer ...).")
        return
    if args.summary:
        s = summarize(traces)
        print(f'{s["traces"]} traces  |  errors {s["errors"]}  blocked {s["blocked"]}  '
              f'escalated {s["escalated"]}  repaired {s["repaired"]}')
        print(f'cache: {s["cache"]}')
        print(f'tiers: {s["tiers"]}   models used: {s["by_model"]}')
        print(f'output guard actions: {s["output_actions"]}')
        print(f'latency ms: {s["latency_ms"]}')
        print(f'llm calls: {s["llm_calls"]}   failed attempts by status: {s["llm_failed"]}')
        print(f'tokens: prompt {s["tokens_prompt"]}  completion {s["tokens_completion"]}')
        print("average ms per stage:")
        for k, v in s["stage_avg_ms"].items():
            print(f"  {k:<18} {v:>9.1f}")
        return
    if args.id:
        picked = [t for t in traces if t["trace_id"].startswith(args.id)]
    elif args.errors:
        picked = [t for t in traces if t["status"] == "error"
                  or any(s["status"] == "error" for s in t["spans"])]
    else:
        picked = traces[-args.last:]
    for t in picked:
        print(render(t) + "\n")
    if not picked:
        print("Nothing matched.")


if __name__ == "__main__":
    main()
