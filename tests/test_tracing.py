"""Tracing tests (no services):  python -m pytest tests/test_tracing.py -q"""
import json
import os
import tempfile
import time
from pathlib import Path

import pytest

from app import trace_view, tracing
from app.identity import get_user

EMP, HR = get_user("E001"), get_user("E003")


def _fresh(**env):
    d = tempfile.mkdtemp()
    for k in ("TRACE_ENABLED", "TRACE_CONTENT"):
        os.environ.pop(k, None)
    os.environ["TRACE_DIR"] = d
    os.environ.update(env)
    return Path(d)


def _read(d: Path):
    lines = [l for f in sorted(d.glob("*.jsonl")) for l in open(f, encoding="utf-8") if l.strip()]
    return [json.loads(l) for l in lines]


def _raw(d: Path) -> str:
    return "".join(open(f, encoding="utf-8").read() for f in d.glob("*.jsonl"))


def test_span_outside_a_trace_is_a_harmless_noop():
    with tracing.span("anything", a=1) as sp:
        sp.set(b=2)
    tracing.outcome(x=1)
    tracing.record("x", 5.0)


def test_spans_nest_and_time_themselves():
    d = _fresh()
    with tracing.start_trace(EMP, "q"):
        with tracing.span("retrieve"):
            with tracing.span("vector_search"):
                time.sleep(0.02)
    t = _read(d)[0]
    by = {s["name"]: s for s in t["spans"]}
    assert by["vector_search"]["parent"] == by["retrieve"]["id"]
    assert by["retrieve"]["parent"] == by["request"]["id"]
    assert by["request"]["parent"] is None
    assert by["vector_search"]["duration_ms"] >= 15
    assert by["request"]["duration_ms"] >= by["vector_search"]["duration_ms"]


def test_errors_are_recorded_and_still_raised():
    d = _fresh()
    with pytest.raises(ValueError):
        with tracing.start_trace(EMP, "q"):
            with tracing.span("llm.call"):
                raise ValueError("boom")
    t = _read(d)[0]
    assert t["status"] == "error"
    llm = next(s for s in t["spans"] if s["name"] == "llm.call")
    assert llm["status"] == "error" and llm["attrs"]["error"] == "ValueError"


def test_every_trace_says_who_asked():
    d = _fresh()
    with tracing.start_trace(EMP, "q"):
        pass
    with tracing.start_trace(HR, "q"):
        pass
    a, b = _read(d)
    assert a["user"] == {"id": "E001", "role": "employee", "group": "all"}
    assert b["user"] == {"id": "E003", "role": "hr", "group": "all_managers_hr"}


def test_text_and_results_are_withheld_by_default():
    d = _fresh()
    with tracing.start_trace(HR, "salary question"):
        with tracing.span("retrieve") as sp:
            sp.set(text="Salary band for L3 is 14 to 22 LPA", n_hits=3,
                   chunks=["salary-bands>Overview"])
        with tracing.span("tool.get_leave_balance") as sp:
            sp.set(result='{"earned": 16, "casual": 9}', answer="Priya has 16 earned days")
        tracing.outcome(raw_answer="14 to 22 LPA", tier="large")
    raw = _raw(d)
    for secret in ("14 to 22", '"earned": 16', "Priya has 16", "LPA"):
        assert secret not in raw, secret
    assert '"n_hits": 3' in raw and "salary-bands>Overview" in raw and "[withheld]" in raw


def test_content_logging_is_opt_in():
    d = _fresh(TRACE_CONTENT="1")
    with tracing.start_trace(EMP, "q"):
        with tracing.span("retrieve") as sp:
            sp.set(text="Casual leave is 8 days")
    assert "Casual leave is 8 days" in _raw(d)
    os.environ.pop("TRACE_CONTENT", None)


def test_personal_data_is_always_redacted_even_with_content_on():
    d = _fresh(TRACE_CONTENT="1")
    with tracing.start_trace(EMP, "My email is a.b@gmail.com and my phone is 9876543210, how many days?"):
        with tracing.span("x") as sp:
            sp.set(note="PAN ABCDE1234F, card 4111 1111 1111 1111", text="call 98765 43210")
    raw = _raw(d)
    for pii in ("a.b@gmail.com", "9876543210", "ABCDE1234F", "4111 1111 1111 1111", "98765 43210"):
        assert pii not in raw, pii


def test_long_strings_and_questions_are_truncated():
    d = _fresh()
    with tracing.start_trace(EMP, "x" * 1000):
        with tracing.span("s") as sp:
            sp.set(note="y" * 1000)
    t = _read(d)[0]
    assert len(t["question"]) == 200
    assert len(next(s for s in t["spans"] if s["name"] == "s")["attrs"]["note"]) == 300


def test_nan_becomes_null_so_the_json_stays_valid():
    d = _fresh()
    with tracing.start_trace(EMP, "q"):
        with tracing.span("s") as sp:
            sp.set(distance=float("nan"))
    assert next(s for s in _read(d)[0]["spans"] if s["name"] == "s")["attrs"]["distance"] is None


def test_tracing_can_be_switched_off():
    d = _fresh(TRACE_ENABLED="0")
    with tracing.start_trace(EMP, "q") as tr:
        assert tr is None
        with tracing.span("x"):
            pass
    assert _read(d) == []
    os.environ.pop("TRACE_ENABLED", None)


def test_an_unwritable_trace_directory_never_breaks_the_request():
    f = tempfile.NamedTemporaryFile(delete=False)         # a FILE where a directory is expected
    _fresh()
    os.environ["TRACE_DIR"] = f.name
    with tracing.start_trace(EMP, "q"):
        pass                                              # must not raise


def test_record_adds_a_finished_span():
    d = _fresh()
    with tracing.start_trace(EMP, "q"):
        time.sleep(0.01)
        tracing.record("mcp.start", 8.0, ok=True)
    s = next(s for s in _read(d)[0]["spans"] if s["name"] == "mcp.start")
    assert s["duration_ms"] == 8.0 and s["attrs"]["ok"] is True


# ---------------------------------------------------------------- the viewer
def _synthetic(cache="miss", tier="small", ms=1000.0, status="ok", llm=None, actions=None):
    spans = [{"id": "a", "parent": None, "name": "request", "start_ms": 0, "duration_ms": ms, "status": "ok", "attrs": {}}]
    for st, attrs in (llm or []):
        spans.append({"id": "b", "parent": "a", "name": "llm.call", "start_ms": 1, "duration_ms": 200.0,
                      "status": st, "attrs": attrs})
    return {"trace_id": "t", "ts": "2026-01-01T00:00:00+00:00", "user": {"id": "E001", "role": "employee", "group": "all"},
            "question": "q", "duration_ms": ms, "status": status, "spans": spans,
            "outcome": {"cache": cache, "tier": tier, "blocked": False, "escalated": False,
                        "repaired": False, "output_actions": actions or []}}


def test_summary_counts_latency_failures_and_tokens():
    ok = {"model": "m1", "prompt_tokens": 100, "completion_tokens": 20}
    traces = [
        _synthetic("miss", "small", 2000.0, llm=[("ok", ok)]),
        _synthetic("exact", None, 20.0),
        _synthetic("miss", "large", 4000.0, llm=[("error", {"model": "m1", "status_code": 429}), ("ok", ok)],
                   actions=["pii:email"]),
    ]
    s = trace_view.summarize(traces)
    assert s["traces"] == 3 and s["llm_calls"] == 3
    assert s["cache"] == {"miss": 2, "exact": 1}
    assert s["llm_failed"] == {"429": 1}
    assert s["tokens_prompt"] == 200 and s["tokens_completion"] == 40
    assert s["output_actions"] == {"pii:email": 1}
    assert s["latency_ms"]["cache_hit_p50"] == 20.0 and s["latency_ms"]["cache_miss_p50"] in (2000.0, 4000.0)


def test_render_shows_the_tree():
    d = _fresh()
    with tracing.start_trace(EMP, "How many casual leave days?"):
        with tracing.span("retrieve"):
            with tracing.span("vector_search"):
                pass
    text = trace_view.render(_read(d)[0])
    assert "retrieve" in text and "vector_search" in text and "E001" in text
