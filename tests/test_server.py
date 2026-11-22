"""Server tests (Flask test client, model and Redis faked):  python -m pytest tests/test_server.py -q"""
import json
import os
import tempfile

import pytest
from flask import Flask
from flask.sessions import SecureCookieSessionInterface
from werkzeug.security import generate_password_hash

from app.server import create_app

USERNAMES = {"E001": "asha.rao@merh.example", "E002": "ravi.menon@merh.example",
             "E003": "meera.iyer@merh.example", "E004": "karan.shah@merh.example",
             "E005": "priya.nair@merh.example", "E006": "dev.patel@merh.example"}
PASSWORDS = {uid: f"Correct-Horse-{uid}-9" for uid in USERNAMES}
FAST = "pbkdf2:sha256:1000"                       # cheap hash so the tests run quickly
CREDS = {USERNAMES[u]: {"user_id": u, "password_hash": generate_password_hash(PASSWORDS[u], method=FAST)}
         for u in USERNAMES}

CALLS = []
LONG_CHUNK = "Casual leave is 8 days per year. " * 30
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


def make(limit=100, fn=fake_answer, **kw):
    CALLS.clear()
    return create_app(answer_fn=fn, rate_limit=limit, secret="test-secret-1", credentials=CREDS, **kw)


def login(client, uid="E001", password=None, username=None):
    return client.post("/api/login", json={"username": username or USERNAMES[uid],
                                           "password": password if password is not None else PASSWORDS[uid]})


def chat(client, question="How many casual leave days do I get?", **extra):
    return client.post("/api/chat", json={"question": question, **extra})


# ================================================================== signing in
def test_everything_requires_sign_in():
    c = make().test_client()
    assert c.get("/api/me").status_code == 401
    assert chat(c).status_code == 401
    r = c.get("/")
    assert r.status_code == 302 and r.headers["Location"].endswith("/login")
    assert CALLS == []


def test_every_one_of_the_six_users_can_sign_in_with_their_own_password():
    app = make()
    for uid in USERNAMES:
        c = app.test_client()
        assert login(c, uid).status_code == 200, uid
        assert c.get("/api/me").get_json()["id"] == uid


def test_login_sets_a_hardened_session_cookie_and_identifies_the_user():
    c = make().test_client()
    r = login(c, "E001")
    assert r.status_code == 200
    cookie = r.headers["Set-Cookie"]
    assert "HttpOnly" in cookie and "SameSite=Lax" in cookie
    me = c.get("/api/me").get_json()
    assert me == {"id": "E001", "name": "Asha Rao", "role": "employee", "department": "Engineering"}
    assert "leave_balance" not in json.dumps(me) and "manager_id" not in json.dumps(me)


def test_one_users_password_does_not_open_another_account():
    c = make().test_client()
    assert login(c, "E001", password=PASSWORDS["E003"]).status_code == 401      # Asha with Meera's password
    assert c.get("/api/me").status_code == 401


def test_usernames_are_trimmed_and_case_insensitive_but_passwords_are_not():
    c = make().test_client()
    assert login(c, "E001", username="  ASHA.RAO@Merh.Example ").status_code == 200
    c2 = make().test_client()
    assert login(c2, "E001", password=PASSWORDS["E001"].lower()).status_code == 401


def test_an_employee_id_is_not_a_username():
    assert login(make().test_client(), "E001", username="E001").status_code == 401


def test_a_wrong_password_and_an_unknown_user_look_identical():
    """No account enumeration: same status, same message."""
    c = make().test_client()
    wrong_pw = login(c, "E001", password="not-the-password")
    no_user = login(c, "E001", username="nobody@merh.example", password="not-the-password")
    assert wrong_pw.status_code == no_user.status_code == 401
    assert wrong_pw.get_json() == no_user.get_json() == {"error": "Incorrect username or password."}


def test_the_login_response_never_echoes_the_password_or_hash():
    c = make().test_client()
    r = login(c, "E001")
    blob = r.get_data(as_text=True) + r.headers["Set-Cookie"]
    assert PASSWORDS["E001"] not in blob and "pbkdf2" not in blob and "password" not in blob.lower()


@pytest.mark.parametrize("body", [
    {}, {"username": "asha.rao@merh.example"}, {"password": "x"}, {"username": "", "password": ""},
    {"username": "  ", "password": "x"}, {"username": None, "password": "x"},
    {"username": 123, "password": "x"}, {"username": ["a"], "password": "x"},
    {"username": "asha.rao@merh.example", "password": ["x"]}, {"username": "a" * 500, "password": "x"},
    {"username": "asha.rao@merh.example", "password": "x" * 500},
])
def test_malformed_login_bodies_are_rejected(body):
    c = make().test_client()
    assert c.post("/api/login", json=body).status_code == 400
    assert c.get("/api/me").status_code == 401


def test_login_requires_json():
    c = make().test_client()
    r = c.post("/api/login", data={"username": USERNAMES["E001"], "password": PASSWORDS["E001"]})
    assert r.status_code == 415 and c.get("/api/me").status_code == 401


def test_the_old_user_picker_endpoint_is_gone():
    c = make().test_client()
    assert c.get("/api/users").status_code == 404
    assert c.post("/api/login", json={"user_id": "E001"}).status_code == 400    # no more passwordless sign-in


# ---- brute force protection
def test_too_many_wrong_passwords_lock_the_account_even_for_the_right_password():
    c = make(max_failures=3).test_client()
    for _ in range(3):
        assert login(c, "E001", password="wrong").status_code == 401
    r = login(c, "E001")                                        # the CORRECT password, but locked
    assert r.status_code == 429 and int(r.headers["Retry-After"]) >= 1
    assert c.get("/api/me").status_code == 401
    assert login(c, "E004").status_code == 200                  # other accounts are unaffected


def test_a_successful_sign_in_resets_the_failure_count():
    c = make(max_failures=3).test_client()
    for _ in range(2):
        login(c, "E001", password="wrong")
    assert login(c, "E001").status_code == 200
    for _ in range(2):
        assert login(c, "E001", password="wrong").status_code == 401     # counter started over
    assert login(c, "E001").status_code == 200


def test_guessing_many_usernames_from_one_address_is_blocked_too():
    c = make(max_failures=50, ip_max_failures=3).test_client()
    for i in range(3):
        assert login(c, username=f"guess{i}@merh.example", password="x").status_code == 401
    assert login(c, "E001").status_code == 429                  # this address is locked, even for a valid login


# ---- sessions
def test_the_body_cannot_choose_who_is_asking():
    c = make().test_client()
    login(c, "E001")
    for attack in ({"user_id": "E003"}, {"user": "E003"}, {"role": "hr"}, {"as": "E003"}):
        assert chat(c, **attack).status_code == 200
    assert CALLS and all(uid == "E001" for _, uid in CALLS)


def test_a_cookie_forged_with_another_secret_is_rejected():
    real = make()
    attacker = Flask(__name__)
    attacker.secret_key = "attacker-guess"
    forged = SecureCookieSessionInterface().get_signing_serializer(attacker).dumps({"uid": "E003"})
    assert real.test_client().get("/api/me", headers={"Cookie": f"session={forged}"}).status_code == 401
    r = real.test_client().post("/api/chat", json={"question": "hi"}, headers={"Cookie": f"session={forged}"})
    assert r.status_code == 401 and CALLS == []


def test_garbage_and_tampered_cookies_are_rejected():
    app = make()
    c = app.test_client()
    login(c, "E001")
    good = c.get_cookie("session").value
    for cookie in ("garbage", good[:-3] + "xyz", good.replace("E", "F", 1), ""):
        assert app.test_client().get("/api/me", headers={"Cookie": f"session={cookie}"}).status_code == 401, cookie


def test_logout_ends_the_session():
    c = make().test_client()
    login(c)
    assert c.post("/api/logout").status_code == 200
    assert c.get("/api/me").status_code == 401 and chat(c).status_code == 401


def test_signing_in_as_someone_else_replaces_the_session():
    c = make().test_client()
    login(c, "E001")
    login(c, "E003")
    assert c.get("/api/me").get_json()["role"] == "hr"
    chat(c)
    assert CALLS[-1][1] == "E003"


# ---- accounts file
def test_the_server_refuses_to_start_without_login_accounts():
    os.environ["CREDENTIALS_FILE"] = os.path.join(tempfile.mkdtemp(), "missing.json")
    try:
        with pytest.raises(FileNotFoundError):
            create_app(answer_fn=fake_answer)
    finally:
        os.environ.pop("CREDENTIALS_FILE", None)


# ================================================================== chat responses
def test_the_response_contains_only_the_public_view():
    c = make().test_client()
    login(c)
    r = chat(c)
    body, raw = r.get_json(), r.get_data(as_text=True)
    assert body["answer"].startswith("Casual leave is")
    assert [s["n"] for s in body["sources"]] == [1]
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


def test_chat_requires_json_so_cross_site_form_posts_fail():
    c = make().test_client()
    login(c)
    assert c.post("/api/chat", data={"question": "hi"}).status_code == 415 and CALLS == []


@pytest.mark.parametrize("q", ["", "   ", None, 42, ["a"], "x" * 1001])
def test_bad_questions_are_rejected_before_any_work(q):
    c = make().test_client()
    login(c)
    assert c.post("/api/chat", json={"question": q}).status_code == 400 and CALLS == []


def test_oversized_request_bodies_are_refused():
    c = make().test_client()
    login(c)
    assert c.post("/api/chat", json={"question": "x" * 20000}).status_code == 413


def test_chat_rate_limit_is_per_user():
    app = make(limit=3)
    a, b = app.test_client(), app.test_client()
    login(a, "E001"); login(b, "E004")
    assert [chat(a).status_code for _ in range(3)] == [200, 200, 200]
    r = chat(a)
    assert r.status_code == 429 and int(r.headers["Retry-After"]) >= 1 and "too quickly" in r.get_json()["error"]
    assert chat(b).status_code == 200


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


# ================================================================== static files and pages
def test_only_whitelisted_static_files_are_served():
    c = make().test_client()
    assert c.get("/static/app.js").status_code == 200 and c.get("/static/style.css").status_code == 200
    assert c.get("/static/login.js").status_code == 200
    for path in ("/static/../app/server.py", "/static/..%2fapp%2fserver.py", "/static/index.html",
                 "/static/../../.env", "/static/../data/credentials.json", "/static/missing.js"):
        assert c.get(path).status_code == 404, path


def test_the_login_page_is_a_real_form_and_lists_nobody():
    c = make().test_client()
    page = c.get("/login").get_data(as_text=True)
    assert 'type="password"' in page and 'autocomplete="current-password"' in page
    assert 'autocomplete="username"' in page and "/static/login.js" in page
    for name in ("Asha", "Ravi", "Meera", "Karan", "Priya", "Dev"):
        assert name not in page
    js = c.get("/static/login.js").get_data(as_text=True)
    assert "/api/users" not in js and "user_id" not in js


def test_chat_page_renders_when_signed_in_without_inline_script():
    c = make().test_client()
    login(c)
    r = c.get("/")
    assert r.status_code == 200
    page = r.get_data(as_text=True)
    assert "/static/app.js" in page and "onclick=" not in page and "<script>" not in page
    assert 'id="signout"' in page and "Switch user" not in page
