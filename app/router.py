"""Router: send easy questions to a small model, everything else to the large one.

Pure function of the question and the retrieved chunks (no LLM call), so it is cheap
and testable. A question goes to the SMALL model only if it has none of these signals:

  personal_or_tool  - about "my" data, balances, teams (tools must be called reliably)
  long / complex    - long, several questions, comparisons
  weak_retrieval    - best vector match is far away (the context may not hold the answer)
  multi_doc         - the top two chunks come from different documents (ambiguous)

If the small model's answer fails the output guard, the orchestrator escalates to the large model.
Settings are read at call time so .env values are picked up:
  ROUTER_ENABLED (1/0)  ROUTER_MAX_DISTANCE (0.45)  MODEL_SMALL  MODEL_LARGE
"""
import os
import re

PERSONAL = re.compile(r"\b(my|mine|me|myself|balance|remaining|left|reports?|team|manager|boss)\b", re.I)
COMPLEX = re.compile(r"\b(compare|difference|versus|vs|both|step by step|explain why|pros and cons|what if)\b", re.I)
MAX_CHARS_SMALL = 160


def is_personal(question: str) -> bool:
    return bool(PERSONAL.search(question))


def route(question: str, hits: list) -> dict:
    if os.getenv("ROUTER_ENABLED", "1") != "1":
        return {"tier": "large", "reasons": ["router_disabled"]}
    reasons = []
    if is_personal(question):
        reasons.append("personal_or_tool")
    if len(question) > MAX_CHARS_SMALL:
        reasons.append("long")
    if question.count("?") >= 2 or COMPLEX.search(question):
        reasons.append("complex")
    if not hits:
        reasons.append("no_context")
    else:
        best = min(h["distance"] for h in hits)
        if best > float(os.getenv("ROUTER_MAX_DISTANCE", "0.45")):
            reasons.append(f"weak_retrieval({best:.2f})")
        if len({h["doc_id"] for h in hits[:2]}) > 1:
            reasons.append("multi_doc")
    return {"tier": "large" if reasons else "small", "reasons": reasons or ["simple_single_doc"]}


def models_for(tier: str) -> list:
    small = os.getenv("MODEL_SMALL", "openai/gpt-oss-20b").strip()
    large = os.getenv("MODEL_LARGE", "openai/gpt-oss-120b").strip()
    order = [small, large] if tier == "small" else [large, small]
    extra = [m.strip() for m in os.getenv("ANSWER_MODELS", "").split(",") if m.strip()]
    out = []
    for m in order + extra:            # fallbacks after the two main models, no duplicates
        if m not in out:
            out.append(m)
    return out
