"""Lightweight tracing: one trace per request, one span per pipeline stage.

Spans nest (request > retrieve > vector_search ...) and record duration and status.
Finished traces are appended to  traces/<date>.jsonl  (override with TRACE_DIR).
View them with:  python -m app.trace_view

PRIVACY RULES, enforced here so careless instrumentation cannot break them:
  * Trace data can leak exactly what the pipeline protects, so by default it holds IDs,
    counts, timings and tool names, NOT text. Attributes named text/result/answer/
    raw_answer/context/content/messages/prompt are withheld unless TRACE_CONTENT=1.
  * Personal data (email, phone, Aadhaar, PAN, card) is redacted from everything, always.
  * Strings are cut to 300 characters; the question is stored redacted and cut to 200.
  * Every trace records WHO asked (id, role, access group), like every other box in the diagram.
Tracing never raises into the app: a failure to write a trace is swallowed.
Settings: TRACE_ENABLED (1), TRACE_CONTENT (0), TRACE_DIR.
"""
import contextvars
import json
import math
import os
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from app.guardrails import _PII
from app.identity import allowed_access

_TRACE = contextvars.ContextVar("trace", default=None)
_PARENT = contextvars.ContextVar("parent_span", default=None)

DENY_KEYS = {"text", "result", "answer", "raw_answer", "context", "content", "messages", "prompt"}
MAX_STR = 300


def _enabled() -> bool:
    return os.getenv("TRACE_ENABLED", "1") == "1"


def _content_on() -> bool:
    return os.getenv("TRACE_CONTENT", "0") == "1"


def trace_dir() -> Path:
    return Path(os.getenv("TRACE_DIR") or Path(__file__).resolve().parent.parent / "traces")


def redact(text: str) -> str:
    for kind, rx in _PII.items():
        text = rx.sub(f"[{kind}]", text)
    return text


def _clean(key: str, value):
    if key in DENY_KEYS and not _content_on():
        return "[withheld]"
    if value is None or isinstance(value, (bool, int)):
        return value
    if isinstance(value, float):
        return None if math.isnan(value) or math.isinf(value) else round(value, 4)
    if isinstance(value, str):
        return redact(value)[:MAX_STR]
    if isinstance(value, (list, tuple, set)):
        return [_clean(key, v) for v in list(value)[:20]]
    if isinstance(value, dict):
        return {str(k): _clean(str(k), v) for k, v in list(value.items())[:20]}
    return redact(str(value))[:MAX_STR]


class _NoSpan:
    def set(self, **attrs):
        pass


_NOOP = _NoSpan()


class Span:
    def __init__(self, trace, name, parent, attrs):
        self.id = uuid.uuid4().hex[:8]
        self.trace, self.name, self.parent = trace, name, parent
        self.start_ms = round((time.perf_counter() - trace.t0) * 1000, 1)
        self.duration_ms = 0.0
        self.status = "ok"
        self.attrs = {}
        self.set(**attrs)

    def set(self, **attrs):
        for k, v in attrs.items():
            self.attrs[k] = _clean(k, v)

    def to_dict(self):
        return {"id": self.id, "parent": self.parent, "name": self.name, "start_ms": self.start_ms,
                "duration_ms": self.duration_ms, "status": self.status, "attrs": self.attrs}


class Trace:
    def __init__(self, user, question):
        self.trace_id = uuid.uuid4().hex[:12]
        self.t0 = time.perf_counter()
        self.ts = datetime.now(timezone.utc).isoformat(timespec="seconds")
        try:
            group = "_".join(allowed_access(user))
        except Exception:                                  # noqa: BLE001
            group = "unknown"
        self.user = {"id": user.id, "role": user.role, "group": group}
        self.question = redact(question)[:200]
        self.spans, self.outcome = [], {}
        self.duration_ms, self.status = 0.0, "ok"

    def to_dict(self):
        return {"trace_id": self.trace_id, "ts": self.ts, "user": self.user, "question": self.question,
                "duration_ms": self.duration_ms, "status": self.status, "outcome": self.outcome,
                "spans": sorted(self.spans, key=lambda s: s["start_ms"])}

    def write(self):
        d = trace_dir()
        d.mkdir(parents=True, exist_ok=True)
        day = self.ts[:10]
        with open(d / f"{day}.jsonl", "a", encoding="utf-8") as f:
            f.write(json.dumps(self.to_dict(), ensure_ascii=False) + "\n")


@contextmanager
def span(name: str, **attrs):
    tr = _TRACE.get()
    if tr is None:
        yield _NOOP
        return
    sp = Span(tr, name, _PARENT.get(), attrs)
    token = _PARENT.set(sp.id)
    t = time.perf_counter()
    try:
        yield sp
    except BaseException as e:
        sp.status = "error"
        sp.attrs["error"] = type(e).__name__
        code = getattr(e, "status_code", None)
        if code is not None:
            sp.attrs["status_code"] = code
        raise
    finally:
        sp.duration_ms = round((time.perf_counter() - t) * 1000, 1)
        _PARENT.reset(token)
        tr.spans.append(sp.to_dict())


def record(name: str, duration_ms: float, **attrs):
    """Add an already-finished span (for work that cannot sit inside a `with` block)."""
    tr = _TRACE.get()
    if tr is None:
        return
    sp = Span(tr, name, _PARENT.get(), attrs)
    sp.start_ms = round(max(0.0, sp.start_ms - duration_ms), 1)
    sp.duration_ms = round(duration_ms, 1)
    tr.spans.append(sp.to_dict())


def outcome(**kv):
    tr = _TRACE.get()
    if tr is not None:
        for k, v in kv.items():
            tr.outcome[k] = _clean(k, v)


@contextmanager
def start_trace(user, question):
    if not _enabled():
        yield None
        return
    tr = Trace(user, question)
    token = _TRACE.set(tr)
    t0 = time.perf_counter()
    try:
        with span("request"):
            yield tr
    except BaseException:
        tr.status = "error"
        raise
    finally:
        tr.duration_ms = round((time.perf_counter() - t0) * 1000, 1)
        _TRACE.reset(token)
        try:
            tr.write()
        except Exception:                                  # noqa: BLE001
            pass
