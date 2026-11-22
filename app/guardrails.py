"""Guardrails: checks before the model runs and after it answers.

INPUT  (check_input)  - cheap, deterministic, runs before any LLM call:
    empty / too long, prompt-injection patterns, role-marker and encoded-payload tricks.
    Optional second layer: a Prompt Guard classifier on Groq (PROMPT_GUARD_MODEL in .env).
OUTPUT (check_output) - runs on the model's answer:
    personal data (email, phone, Aadhaar, PAN, card) that did NOT come from the
    retrieved policy text or tool results is redacted; answers that make claims
    without a valid [n] citation are replaced with a safe fallback.

These are a second layer. The first layer is access control (Steps 4 and 5):
restricted data never reaches the model in the first place.
"""
import os
import re
import unicodedata
from dataclasses import dataclass, field

MAX_CHARS = 1000

REFUSAL = ("I can't help with that request. I can answer questions about Nimbus HR "
           "policies and your own HR information, so please try rephrasing.")
TOO_LONG = f"That message is too long. Please ask a shorter question (under {MAX_CHARS} characters)."
EMPTY = "Please type a question."
UNVERIFIED = ("I couldn't verify that answer against the policy documents, so I'd rather "
              "not guess. Please check with HR directly.")

# ---------------------------------------------------------------- input rules
# Applied to NORMALISED text (lower-case, NFKC, no zero-width characters, no
# letter-spacing). Deliberately NOT blocked: claims like "I am from HR". Those
# are not attacks on their own, and access control already ignores them.
_RULES = {
    "override_instructions": r"\b(ignore|disregard|forget|bypass)\b[^.?!]{0,50}\b(instructions?|rules|guidelines|restrictions|guardrails|safeguards|filters|prompts?|everything (above|before)|previous|prior)\b",
    "reveal_prompt": r"\b(your|the)\s+(system|hidden|initial|original|developer)\s+(prompt|instructions?|message|rules)\b",
    "repeat_above": r"\b(repeat|print|output|show|copy)\b[^.?!]{0,20}\b(text|everything|words|content|message)\s+(above|before)\b",
    "persona_switch": r"\byou are now\b|\bpretend (to be|you are|that you)\b|\bact as (an? )?(unrestricted|unfiltered|jailbroken|dan|root|admin)\b|\bdo anything now\b|\bdeveloper mode\b|\bjailbreak(ed)?\b",
    "no_restrictions": r"\b(with no|without (any )?)(restrictions|filters|safeguards|guardrails)\b",
    "role_marker": r"(^|\n)\s*(system|assistant|developer)\s*:|<\|?(im_start|im_end|system)\|?>|\[/?(inst|sys)\]|###\s*system",
    "access_claim": r"\b(access|role|permissions?)\s*[:=]\s*(hr|admin|managers?|root)\b|\bsudo\b|\b(system|admin|debug)\s+override\b",
    "borrowed_credentials": r"\b(manager|someone else|another (person|employee)|his|her|their)(')?s?\s+(login|credentials|password)\b",
    "encoded_payload": r"[a-z0-9+/]{60,}={0,2}",
}
RULES = {name: re.compile(p) for name, p in _RULES.items()}


def _clean(text: str) -> str:
    """Remove zero-width / control characters but keep the user's wording."""
    return "".join(ch for ch in text
                   if ch in "\n\t" or unicodedata.category(ch) not in ("Cf", "Cc")).strip()


def _normalise(text: str) -> str:
    t = unicodedata.normalize("NFKC", _clean(text)).casefold()
    t = t.replace("\u2019", "'").replace("\u2018", "'")
    t = re.sub(r"\b(?:\w ){3,}\w\b", lambda m: m.group().replace(" ", ""), t)   # "i g n o r e"
    return re.sub(r"[ \t]+", " ", t)


@dataclass
class InputVerdict:
    allowed: bool
    text: str = ""
    reason: str | None = None
    message: str = ""
    flags: list = field(default_factory=list)


def _block(reason: str, message: str = REFUSAL) -> InputVerdict:
    return InputVerdict(False, reason=reason, message=message)


def classifier_score(text: str) -> float | None:
    """Optional Prompt Guard check. Off unless PROMPT_GUARD_MODEL is set in .env.
    Fails OPEN (returns None) if the service errors: the rules above, access
    control and the output checks still apply."""
    model = os.getenv("PROMPT_GUARD_MODEL", "").strip()
    if not model:
        return None
    try:
        from groq import Groq
        resp = Groq(api_key=os.getenv("GROQ_API_KEY"), timeout=20).chat.completions.create(
            model=model, messages=[{"role": "user", "content": text}])
        m = re.search(r"\d*\.?\d+", resp.choices[0].message.content or "")
        return float(m.group()) if m else None
    except Exception as e:                       # noqa: BLE001
        print(f"[guardrails] classifier unavailable ({type(e).__name__}); using rules only")
        return None


def check_input(question) -> InputVerdict:
    if not isinstance(question, str) or not question.strip():
        return _block("empty", EMPTY)
    if len(question) > MAX_CHARS:
        return _block("too_long", TOO_LONG)
    norm = _normalise(question)
    for name, rx in RULES.items():
        if rx.search(norm):
            return _block(f"rule:{name}")
    score = classifier_score(_clean(question))
    if score is not None and score >= float(os.getenv("PROMPT_GUARD_THRESHOLD", "0.5")):
        return _block("classifier")
    return InputVerdict(True, text=_clean(question))


# --------------------------------------------------------------- output checks
_PII = {
    "email": re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}"),
    "phone": re.compile(r"(?<!\d)(?:\+?91[\s-]?)?[6-9]\d{4}[\s-]?\d{5}(?!\d)"),
    "aadhaar": re.compile(r"(?<!\d)(?<!\d[ -])\d{4}[ -]\d{4}[ -]\d{4}(?![ -]?\d)"),
    "pan": re.compile(r"\b[A-Z]{5}\d{4}[A-Z]\b"),
    "card": re.compile(r"(?<!\d)\d(?:[ -]?\d){12,15}(?!\d)"),
}
_NUMS = r"\d+(?:\s*,\s*\d+)*"
_CITE_FORMS = [
    re.compile(rf"\[({_NUMS})\]"),                                  # [1]  [1, 2]
    re.compile(rf"\[(?:source|sources|src|ref)s?\s*:?\s*({_NUMS})\]", re.I),   # [Source 1]
    re.compile(rf"\((?:source|sources)\s*:?\s*({_NUMS})\)", re.I),            # (Source 1)
    re.compile(r"\u3010\s*(\d+)[^\u3011]*\u3011"),                          # 【1】 【1†L3-L5】
]


def cited_numbers(text: str) -> set:
    return {int(n) for rx in _CITE_FORMS for grp in rx.findall(text) for n in re.findall(r"\d+", grp)}
_REFUSAL = re.compile(
    r"can'?t find|cannot find|couldn'?t find|could not find|unable to find|can'?t access|"
    r"cannot access|not able to|wasn'?t able|contact hr|check with hr|don'?t know|do not know", re.I)


def _digits(s: str) -> str:
    return re.sub(r"\D", "", s)


_FULLWIDTH_CITE = re.compile(r"\u3010\s*(\d+)[^\u3011]*\u3011")   # 【1】 / 【1†L3-L5】 -> [1]
REFUSAL_RX = _REFUSAL


def _tidy(text: str) -> str:
    """Models sometimes emit narrow no-break spaces (U+202F), no-break spaces and
    non-breaking hyphens. Turn them into plain ones so the checks below (and any
    exact-match test) see what a human sees."""
    text = "".join(" " if unicodedata.category(ch) == "Zs" else ch for ch in text)
    return re.sub("[\u2010\u2011\u2212]", "-", text)


def check_output(answer: str, hits: list, tool_calls: list):
    """Return (final_answer, actions). `actions` lists what the guard did."""
    actions = []
    answer = _FULLWIDTH_CITE.sub(r"[\1]", _tidy(answer))
    allowed_text = "\n".join([h["text"] for h in hits] + [t["result"] for t in tool_calls])
    allowed_digits = _digits(allowed_text)

    for kind, rx in _PII.items():
        def sub(m, kind=kind):
            found = m.group()
            grounded = found in allowed_text or (
                kind != "email" and len(_digits(found)) >= 8 and _digits(found) in allowed_digits)
            if grounded:
                return found
            actions.append(f"pii:{kind}")
            return "[redacted]"
        answer = rx.sub(sub, answer)

    cited = cited_numbers(answer)
    if any(n < 1 or n > len(hits) for n in cited):
        return UNVERIFIED, actions + ["invalid_citation"]
    if not cited and not tool_calls and not _REFUSAL.search(answer):
        return UNVERIFIED, actions + ["no_citation"]
    return answer, actions


CITATION_ACTIONS = {"no_citation", "invalid_citation"}


def finalize(raw: str, hits: list, tool_calls: list, repair=None):
    """check_output, plus ONE repair attempt when the only problem is citations.
    `repair(raw)` should return a rewritten answer, or None. Returns
    (final_answer, actions, repaired)."""
    final, actions = check_output(raw, hits, tool_calls)
    if CITATION_ACTIONS & set(actions) and repair is not None:
        fixed = repair(raw)
        if fixed:
            final2, actions2 = check_output(fixed, hits, tool_calls)
            if not (CITATION_ACTIONS & set(actions2)):
                return final2, actions2, True
        return final, actions + ["repair_failed"], False
    return final, actions, False
