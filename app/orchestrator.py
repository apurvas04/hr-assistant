"""The orchestrator. Pipeline (matches the architecture diagram):

  input guard -> cache lookup -> retrieve (access-filtered) -> router -> tool loop (MCP)
              -> output guard (+ one repair) -> escalate small->large if still unverified -> cache store
"""
import asyncio
import json
import os
import sys
import time
from pathlib import Path

import groq
from dotenv import load_dotenv
from groq import Groq
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from app import cache, prompts, tracing
from app.guardrails import check_input, finalize
from app.identity import User
from app.retrieve import retrieve
from app.router import models_for, route

load_dotenv()
client = Groq(api_key=os.getenv("GROQ_API_KEY"), timeout=60)
MAX_STEPS = 4
PROJECT_ROOT = Path(__file__).resolve().parent.parent

def chat(messages: list, tools, models: list):
    """One model call with retries; falls through `models` in order. Returns (message, model_used)."""
    last = None
    for model in models:
        for attempt in range(2):
            try:
                kwargs = dict(model=model, messages=messages, temperature=0, max_tokens=2000)
                if tools:
                    kwargs.update(tools=tools, tool_choice="auto")
                with tracing.span("llm.call", model=model, attempt=attempt + 1) as sp:
                    resp = client.chat.completions.create(**kwargs)
                    usage = getattr(resp, "usage", None)
                    sp.set(prompt_tokens=getattr(usage, "prompt_tokens", None),
                           completion_tokens=getattr(usage, "completion_tokens", None),
                           tool_calls=len(resp.choices[0].message.tool_calls or []))
                return resp.choices[0].message, model
            except groq.APIStatusError as e:
                last = e
                print(f"[{model}] attempt {attempt + 1} failed: {e.status_code}")
                if e.status_code in (400, 404):   # bad model or malformed tool call: next model
                    break
                time.sleep(2)
            except groq.APIConnectionError as e:
                last = e
                print(f"[{model}] attempt {attempt + 1} connection error")
                time.sleep(2)
    raise last


def _text(result) -> str:
    return "".join(c.text for c in result.content if getattr(c, "type", "") == "text")


async def _run(question: str, user: User, system: str, force_tier: str | None = None) -> dict:
    hits = retrieve(question, user)
    with tracing.span("router") as sp:
        decision = route(question, hits)
        if force_tier:
            decision = {"tier": force_tier, "reasons": decision["reasons"] + [f"forced_{force_tier}"]}
        sp.set(tier=decision["tier"], reasons=decision["reasons"])
    models = models_for(decision["tier"])

    context = "\n\n".join(
        f'[{i}] {h["title"]} > {h["section"]}\n{h["text"]}' for i, h in enumerate(hits, 1))
    messages = [{"role": "system", "content": system},
                {"role": "user", "content": f"Context:\n{context}\n\nQuestion: {question}"}]

    server = StdioServerParameters(
        command=sys.executable, args=["-m", "app.mcp_server"],
        env={**os.environ, "HR_USER_ID": user.id},      # identity travels HERE, not in tool args
        cwd=str(PROJECT_ROOT))
    trace, final, used_model = [], None, None
    t_mcp = time.perf_counter()

    async with stdio_client(server) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            tracing.record("mcp.start", (time.perf_counter() - t_mcp) * 1000)
            listed = (await session.list_tools()).tools
            tool_defs = [{"type": "function", "function": {
                "name": t.name, "description": t.description or "",
                "parameters": t.inputSchema}} for t in listed]

            for _ in range(MAX_STEPS):
                msg, used_model = chat(messages, tool_defs, models)
                if not msg.tool_calls:
                    final = (msg.content or "").strip()
                    break
                messages.append({"role": "assistant", "content": msg.content or "",
                                 "tool_calls": [{"id": tc.id, "type": "function",
                                                 "function": {"name": tc.function.name,
                                                              "arguments": tc.function.arguments}}
                                                for tc in msg.tool_calls]})
                for tc in msg.tool_calls:
                    args = json.loads(tc.function.arguments or "{}")
                    with tracing.span(f"tool.{tc.function.name}", args=args) as sp:
                        result = _text(await session.call_tool(tc.function.name, args))
                        sp.set(ok='"error"' not in result, result_chars=len(result))   # never the result itself
                    trace.append({"name": tc.function.name, "args": args, "result": result})
                    messages.append({"role": "tool", "tool_call_id": tc.id, "content": result})

    if not final:
        final = "I wasn't able to finish answering that. Please try rephrasing, or contact HR."
    return {"answer": final, "sources": hits, "user": user.id, "tool_calls": trace,
            "route": {**decision, "model": used_model, "escalated": False},
            "_messages": messages, "_models": models}


def _answer_once(question: str, user: User, system, repair_prompt, force_tier: str | None = None) -> dict:
    out = asyncio.run(_run(question, user, system.text, force_tier))
    messages, models = out.pop("_messages"), out.pop("_models")
    raw = out["answer"]

    def repair(bad: str):
        # only reached for answers that used no tools, so the history is plain text
        with tracing.span("repair"):
            try:
                msg, _ = chat(messages + [{"role": "assistant", "content": bad},
                                          {"role": "user", "content": repair_prompt.text}], None, models)
                return (msg.content or "").strip()
            except Exception:                  # noqa: BLE001
                return None

    with tracing.span("output_guard") as sp:
        final, actions, repaired = finalize(raw, out["sources"], out["tool_calls"], repair)
        sp.set(actions=actions, repaired=repaired, answer_chars=len(final))
    out["answer"], out["raw_answer"] = final, raw
    out["guardrails"] = {"blocked": False, "input_reason": None,
                         "output_actions": actions, "repaired": repaired}
    return out


def _finish(out: dict) -> dict:
    g, rt = out.get("guardrails", {}), out.get("route") or {}
    tracing.outcome(blocked=g.get("blocked"), cache=(out.get("cache") or {}).get("status"),
                    tier=rt.get("tier"), model=rt.get("model"), escalated=rt.get("escalated"),
                    output_actions=g.get("output_actions"), repaired=g.get("repaired"),
                    tools=[t["name"] for t in out.get("tool_calls", [])],
                    answer_chars=len(out.get("answer", "")))
    return out


def _answer(question: str, user: User) -> dict:
    system, repair_prompt = prompts.get("system"), prompts.get("repair")
    tracing.outcome(prompts={"system": f"{system.version}@{system.sha}",
                             "repair": f"{repair_prompt.version}@{repair_prompt.sha}"})

    # 1. input guard: blocked questions never reach the cache, retrieval, the model or any tool
    with tracing.span("input_guard") as sp:
        verdict = check_input(question)
        sp.set(allowed=verdict.allowed, reason=verdict.reason)
    if not verdict.allowed:
        return _finish({"answer": verdict.message, "raw_answer": None, "sources": [], "user": user.id,
                        "tool_calls": [], "route": None, "cache": {"status": "n/a"},
                        "guardrails": {"blocked": True, "input_reason": verdict.reason,
                                       "output_actions": [], "repaired": False}})

    # 2. cache: keyed by access group + corpus/prompt version (see app/cache.py)
    ver = cache.version(system.text) if os.getenv("CACHE_ENABLED", "1") == "1" else None
    if ver:
        with tracing.span("cache.lookup") as sp:
            hit = cache.lookup(verdict.text, user, ver)
            sp.set(hit_kind=hit[1] if hit else "miss", distance=hit[2] if hit else None)
        if hit:
            payload, kind, dist = hit
            return _finish({"answer": payload["answer"], "raw_answer": None, "sources": payload["sources"],
                            "user": user.id, "tool_calls": [], "route": None,
                            "cache": {"status": kind, "distance": dist},
                            "guardrails": {"blocked": False, "input_reason": None,
                                           "output_actions": [], "repaired": False}})

    # 3. answer; if the small model's answer cannot be verified, retry once on the large one
    out = _answer_once(verdict.text, user, system, repair_prompt)
    if out["route"]["tier"] == "small" and "repair_failed" in out["guardrails"]["output_actions"]:
        with tracing.span("escalate", to="large"):
            out = _answer_once(verdict.text, user, system, repair_prompt, force_tier="large")
        out["route"]["escalated"] = True

    out["cache"] = {"status": "miss" if ver else "bypass"}
    if ver and cache.cacheable(verdict.text, out):
        with tracing.span("cache.store"):
            cache.store(verdict.text, user, ver, out)
    return _finish(out)


def answer(question: str, user: User) -> dict:
    with tracing.start_trace(user, question) as tr:
        out = _answer(question, user)
        out["trace_id"] = tr.trace_id if tr else None
        return out
