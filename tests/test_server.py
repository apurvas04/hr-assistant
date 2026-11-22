"""Server tests (Flask test client, model and Redis faked):  python -m pytest tests/test_server.py -q"""
import json

import pytest
from flask import Flask
from flask.sessions import SecureCookieSessionInterface

from app.server import create_app

CALLS = []
LONG_CHUNK = "Casual leave is 8 days per year. " * 30          # far longer than the snippet limit
SECRET_TOOL_RESULT = '{"leave_balance_days": {"earned": 11}}'
RAW_SECRET = "RAW MODEL TEXT BEFORE GUARD"


def fake_answer(question, user):
    CALLS.append((question, user.id))
    return {
        "answer": "Casual leave is **8 days** per year [1].", "raw_answer": RAW_SECRET, "user": user.id,
        "sources": [
            {"doc_id": "leave-policy", "title": "Leave Policy", "section": "Types of leave",
             "text": LONG_CHUNK, "status": "current", "access": "all", "distance": 0.31},
            {"doc_id": "salary-bands", "title": "Salary Bands", "section": "Overview",
             "text": "L3 14 to 22 LPA", "status": "current", "access": "hr", "distance": 0.5},
        ],
        "tool_calls": [{"name": "get_leave_balance", "args": {}, "result": SECRET_TOOL_RESULT}],
        "route": {"tier": "small", "reasons": ["simple_single_doc"], "model": "m", "escalated": False},
        "cache": {"status": "miss"},
        "guardrails": {"blocked": False, "input_reason": None, "output_actions": [], "repaired": False},
        "trace_id": "abc123",
    }


def make(limit=100, fn=fake_answer):
    CALLS.clear()
    return create_app(answer_fn=fn, rate_limit=limit, secret="test-secret-1")


def login(client, uid="E001"):
    return client.post("/api/login", json={"user_id": uid})


def chat(client, question="How many casual leave days do I get?", **extra):
    return client.post("/api/chat", json={"question": question, **extra})


# ----------------------------------------------------------------- who is asking
def test_everything_requires_sign_in():
    c = make().test_client()
    assert c.get("/api/me").status_code == 401
    assert chat(c).status_code == 401
    r = c.get("/")
    assert r.status_code == 302 and r.headers["Location"].endswith("/login")
    assert CALLS == []


def test_login_sets_a_hardened_session_cookie_and_identifies_the_user():
    c = make().test_client()
    r = login(c, "E001")
    assert r.status_code == 200
    cookie = r.headers["Set-Cookie"]
    assert "HttpOnly" in cookie and "SameSite=Lax" in cookie
    me = c.get("/api/me").get_json()
    assert me == {"id": "E001", "name": "Asha Rao", "role": "employee", "department": "Engineering"}
    assert "leave_balance" not in json.dumps(me) and "manager_id" not in json.dumps(me)


def test_the_public_user_list_has_no_personal_data():
    users = make().test_client().get("/api/users").get_json()["users"]
    assert len(users) == 6 and all(set(u) == {"id", "name", "role", "department"} for u in users)


@pytest.mark.parametrize("bad", ["E999", "", None, 123, ["E001"], {"id": "E001"}])
def test_login_rejects_unknown_or_malformed_users(bad):
    c = make().test_client()
    assert login(c, bad).status_code == 400
    assert c.get("/api/me").status_code == 401


def test_the_body_cannot_choose_who_is_asking():
    c = make().test_client()
    login(c, "E001")
    for attack in ({"user_id": "E003"}, {"user": "E003"}, {"role": "hr"}, {"as": "E003"}):
        assert chat(c, **attack).status_code == 200
    assert CALLS and all(uid == "E001" for _, uid in CALLS)          # always the SESSION user


def test_a_cookie_forged_with_another_secret_is_rejected():
    real = make()
    attacker = Flask(__name__)
    attacker.secret_key = "attacker-guess"
    forged = SecureCookieSessionInterface().get_signing_serializer(attacker).dumps({"uid": "E003"})
    r = real.test_client().get("/api/me", headers={"Cookie": f"session={forged}"})
    assert r.status_code == 401
    r = real.test_client().post("/api/chat", json={"question": "hi"}, headers={"Cookie": f"session={forged}"})
    assert r.status_code == 401 and CALLS == []


def test_garbage_and_tampered_cookies_are_rejected():
    app = make()
    c = app.test_client()
    login(c, "E001")
    good = c.get_cookie("session").value
    for cookie in ("garbage", good[:-3] + "xyz", good.replace("E", "F", 1), ""):
        r = app.test_client().get("/api/me", headers={"Cookie": f"session={cookie}"})
        assert r.status_code == 401, cookie


def test_logout_ends_the_session():
    c = make().test_client()
    login(c)
    assert c.post("/api/logout").status_code == 200
    assert c.get("/api/me").status_code == 401 and chat(c).status_code == 401


def test_switching_user_replaces_the_session():
    c = make().test_client()
    login(c, "E001")
    login(c, "E003")
    assert c.get("/api/me").get_json()["role"] == "hr"
    chat(c)
    assert CALLS[-1][1] == "E003"


# --------------------------------------------------------------------- what the browser gets
def test_the_response_contains_only_the_public_view():
    c = make().test_client()
    login(c)
    r = chat(c)
    body, raw = r.get_json(), r.get_data(as_text=True)
    assert body["answer"].startswith("Casual leave is")
    assert [s["n"] for s in body["sources"]] == [1]                    # only the CITED source, not [2]
    assert body["sources"][0]["title"] == "Leave Policy" and len(body["sources"][0]["snippet"]) <= 200
    assert body["tools"] == [{"name": "get_leave_balance", "args": {}}]
    assert body["details"]["tier"] == "small" and body["details"]["trace_id"] == "abc123"
    assert body["details"]["retrieved"] == 2 and body["blocked"] is False
    for secret in (RAW_SECRET, SECRET_TOOL_RESULT, "leave_balance_days", "14 to 22", "Salary Bands", "distance"):
        assert secret not in raw, secret


def test_security_headers_are_set():
    c = make().test_client()
    h = c.get("/login").headers
    assert "default-src 'self'" in h["Content-Security-Policy"] and "script-src 'self'" in h["Content-Security-Policy"]
    assert h["X-Content-Type-Options"] == "nosniff" and h["Referrer-Policy"] == "no-referrer"
    assert c.get("/api/health").headers["Cache-Control"] == "no-store"


# ------------------------------------------------------------------------ input rules
def test_chat_requires_json_so_cross_site_form_posts_fail():
    c = make().test_client()
    login(c)
    r = c.post("/api/chat", data={"question": "hi"})
    assert r.status_code == 415 and CALLS == []


@pytest.mark.parametrize("q", ["", "   ", None, 42, ["a"], "x" * 1001])
def test_bad_questions_are_rejected_before_any_work(q):
    c = make().test_client()
    login(c)
    assert c.post("/api/chat", json={"question": q}).status_code == 400
    assert CALLS == []


def test_oversized_request_bodies_are_refused():
    c = make().test_client()
    login(c)
    assert c.post("/api/chat", json={"question": "x" * 20000}).status_code == 413


# -------------------------------------------------------------------------- rate limit
def test_rate_limit_is_per_user():
    CALLS.clear()
    app = create_app(answer_fn=fake_answer, rate_limit=3, secret="s")
    a, b = app.test_client(), app.test_client()
    login(a, "E001"); login(b, "E004")
    assert [chat(a).status_code for _ in range(3)] == [200, 200, 200]
    r = chat(a)
    assert r.status_code == 429 and int(r.headers["Retry-After"]) >= 1
    assert "too quickly" in r.get_json()["error"]
    assert chat(b).status_code == 200                                   # another user is unaffected


# ------------------------------------------------------------------------ failures
def test_internal_errors_do_not_leak_details():
    def boom(question, user):
        raise RuntimeError("GROQ_API_KEY=gsk_secret traceback /home/x")
    c = make(fn=boom).test_client()
    login(c)
    r = chat(c)
    assert r.status_code == 502
    assert "gsk_" not in r.get_data(as_text=True) and "RuntimeError" not in r.get_data(as_text=True)


def test_permission_errors_become_403():
    def deny(question, user):
        raise PermissionError("no access rules for role 'x'")
    c = make(fn=deny).test_client()
    login(c)
    r = chat(c)
    assert r.status_code == 403 and "no access rules" not in r.get_data(as_text=True)


# ------------------------------------------------------------------------ static files
def test_only_whitelisted_static_files_are_served():
    c = make().test_client()
    assert c.get("/static/app.js").status_code == 200
    assert c.get("/static/style.css").status_code == 200
    for path in ("/static/../app/server.py", "/static/..%2fapp%2fserver.py", "/static/index.html",
                 "/static/../../.env", "/static/missing.js"):
        assert c.get(path).status_code == 404, path


def test_pages_render_when_signed_in_and_never_use_inline_script():
    c = make().test_client()
    assert c.get("/login").status_code == 200
    login(c)
    r = c.get("/")
    assert r.status_code == 200
    page = r.get_data(as_text=True)
    assert "/static/app.js" in page and "onclick=" not in page and "<script>" not in page
