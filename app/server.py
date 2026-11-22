"""Web server: mock sign-in + chat API + static chat page.

    python -m app.server          then open http://127.0.0.1:8000

This is the "gateway" box of the architecture: it decides WHO is asking (a signed session
cookie) and enforces a per-user rate limit. The user id never comes from the request body,
so a client cannot ask "as" someone else. The sign-in is a DEMO stand-in for company SSO.

Sign-in is username + password checked against salted hashes in data/credentials.json
(create accounts with:  python -m app.users_admin init). The server refuses to start without it.

Settings: PORT (8000)  SESSION_SECRET (random per start)  RATE_LIMIT_PER_MIN (20)
          LOGIN_MAX_FAILURES (5)  LOGIN_IP_MAX_FAILURES (20)  LOGIN_LOCKOUT_SECONDS (300)
"""
import os
import secrets
import threading
import time
from collections import defaultdict, deque
from datetime import timedelta
from pathlib import Path

from flask import Flask, jsonify, redirect, request, send_from_directory, session
from werkzeug.security import check_password_hash, generate_password_hash

from app.guardrails import MAX_CHARS, cited_numbers
from app.identity import get_user
from app.users_admin import load_credentials

WEB_DIR = Path(__file__).resolve().parent.parent / "web"
STATIC_FILES = {"app.js", "login.js", "style.css"}
SNIPPET_CHARS = 200
SESSION_HOURS = 8


class RateLimiter:
    """Sliding window per user id, in memory (one process). Returns seconds to wait, or 0."""

    def __init__(self, limit: int, window: float = 60.0):
        self.limit, self.window = limit, window
        self.hits, self.lock = defaultdict(deque), threading.Lock()

    def check(self, key: str) -> int:
        now = time.monotonic()
        with self.lock:
            q = self.hits[key]
            while q and now - q[0] > self.window:
                q.popleft()
            if len(q) >= self.limit:
                return max(1, int(self.window - (now - q[0])) + 1)
            q.append(now)
            return 0


class FailureLimiter:
    """Counts FAILED sign-ins per key (username or IP). After `max_failures` inside the window the
    key is locked until the oldest failure expires. Checked BEFORE the password is, so a locked
    account cannot be guessed at. (Behind a reverse proxy, remote_addr would be the proxy.)"""

    def __init__(self, max_failures: int, window: float = 300.0):
        self.max, self.window = max_failures, window
        self.hits, self.lock = defaultdict(deque), threading.Lock()

    def _prune(self, key, now):
        q = self.hits[key]
        while q and now - q[0] > self.window:
            q.popleft()
        if not q:
            self.hits.pop(key, None)
        return self.hits.get(key)

    def wait(self, key: str) -> int:
        now = time.monotonic()
        with self.lock:
            q = self._prune(key, now)
            if q and len(q) >= self.max:
                return max(1, int(self.window - (now - q[0])) + 1)
            return 0

    def fail(self, key: str) -> None:
        now = time.monotonic()
        with self.lock:
            if len(self.hits) > 5000:                       # stop random usernames growing memory forever
                for k in list(self.hits):
                    self._prune(k, now)
            self.hits[key].append(now)

    def clear(self, key: str) -> None:
        with self.lock:
            self.hits.pop(key, None)


# Checked against when the username is unknown, so "no such user" costs the same time as "wrong password".
DUMMY_HASH = generate_password_hash("not-a-real-password")


def public_view(out: dict, elapsed_ms: int) -> dict:
    """What the browser may see. Deliberately NOT included: raw model text, tool results,
    chunk text beyond a short snippet, distances. Only sources the answer actually cites."""
    cited = cited_numbers(out["answer"])
    sources = [{"n": i, "title": s["title"], "section": s["section"], "status": s["status"],
                "snippet": s["text"][:SNIPPET_CHARS]}
               for i, s in enumerate(out.get("sources", []), 1) if i in cited]
    g, rt = out.get("guardrails", {}), out.get("route") or {}
    return {
        "answer": out["answer"],
        "sources": sources,
        "tools": [{"name": t["name"], "args": t["args"]} for t in out.get("tool_calls", [])],
        "blocked": bool(g.get("blocked")),
        "details": {"cache": (out.get("cache") or {}).get("status"),
                    "tier": rt.get("tier"), "model": rt.get("model"), "escalated": rt.get("escalated"),
                    "output_actions": g.get("output_actions", []), "repaired": g.get("repaired", False),
                    "retrieved": len(out.get("sources", [])), "trace_id": out.get("trace_id"),
                    "elapsed_ms": elapsed_ms},
    }


def _default_answer_fn(question, user):
    from app.orchestrator import answer          # imported lazily: heavy (models, Redis, MCP)
    return answer(question, user)


def create_app(answer_fn=None, rate_limit: int | None = None, secret: str | None = None,
               credentials: dict | None = None, max_failures: int | None = None,
               ip_max_failures: int | None = None) -> Flask:
    app = Flask(__name__, static_folder=None)
    app.secret_key = secret or os.getenv("SESSION_SECRET") or secrets.token_hex(32)
    app.config.update(SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE="Lax",
                      PERMANENT_SESSION_LIFETIME=timedelta(hours=SESSION_HOURS),
                      MAX_CONTENT_LENGTH=16 * 1024)
    answer_fn = answer_fn or _default_answer_fn
    creds = credentials if credentials is not None else load_credentials()     # raises if there are no accounts
    lockout = float(os.getenv("LOGIN_LOCKOUT_SECONDS", "300"))
    user_failures = FailureLimiter(max_failures or int(os.getenv("LOGIN_MAX_FAILURES", "5")), lockout)
    ip_failures = FailureLimiter(ip_max_failures or int(os.getenv("LOGIN_IP_MAX_FAILURES", "20")), lockout)
    limiter = RateLimiter(rate_limit if rate_limit is not None else int(os.getenv("RATE_LIMIT_PER_MIN", "20")))

    def current_user():
        uid = session.get("uid")
        if not uid:
            return None
        try:
            return get_user(uid)
        except PermissionError:                  # user removed since the cookie was issued
            return None

    @app.after_request
    def security_headers(resp):
        resp.headers["Content-Security-Policy"] = ("default-src 'self'; script-src 'self'; "
                                                   "style-src 'self'; frame-ancestors 'none'")
        resp.headers["X-Content-Type-Options"] = "nosniff"
        resp.headers["Referrer-Policy"] = "no-referrer"
        if request.path.startswith("/api/"):
            resp.headers["Cache-Control"] = "no-store"
        return resp

    # ---- pages
    @app.get("/")
    def index():
        if not current_user():
            return redirect("/login")
        return send_from_directory(WEB_DIR, "index.html")

    @app.get("/login")
    def login_page():
        return send_from_directory(WEB_DIR, "login.html")

    @app.get("/static/<path:name>")
    def static_file(name):
        if name not in STATIC_FILES:             # whitelist: no path tricks possible
            return jsonify(error="Not found."), 404
        return send_from_directory(WEB_DIR, name)

    # ---- sign-in
    @app.post("/api/login")
    def login():
        if not request.is_json:                  # also blocks cross-site HTML form posts
            return jsonify(error="Send JSON."), 415
        body = request.get_json(silent=True) or {}
        username, password = body.get("username"), body.get("password")
        if (not isinstance(username, str) or not isinstance(password, str)
                or not username.strip() or not password or len(username) > 200 or len(password) > 200):
            return jsonify(error="Enter your username and password."), 400
        name, ip = username.strip().lower(), request.remote_addr or "?"

        wait = max(user_failures.wait(name), ip_failures.wait(ip))
        if wait:                                 # locked: do not even look at the password
            resp = jsonify(error=f"Too many failed attempts. Try again in {wait} seconds.")
            resp.status_code, resp.headers["Retry-After"] = 429, str(wait)
            return resp

        record = creds.get(name)
        password_ok = check_password_hash(record["password_hash"] if record else DUMMY_HASH, password)
        user = None
        if record and password_ok:
            try:
                user = get_user(record["user_id"])
            except PermissionError:              # account exists but the employee was removed
                user = None
        if user is None:
            user_failures.fail(name)             # unknown names count too: no way to probe which exist
            ip_failures.fail(ip)
            return jsonify(error="Incorrect username or password."), 401   # same message either way

        user_failures.clear(name)
        session.clear()                          # fresh session on every sign-in
        session["uid"] = user.id
        session.permanent = True
        return jsonify(ok=True, user={"id": user.id, "name": user.name, "role": user.role})

    @app.post("/api/logout")
    def logout():
        session.clear()
        return jsonify(ok=True)

    @app.get("/api/me")
    def me():
        user = current_user()
        if not user:
            return jsonify(error="Please sign in."), 401
        return jsonify(id=user.id, name=user.name, role=user.role, department=user.department)

    @app.get("/api/health")
    def health():
        return jsonify(ok=True)

    # ---- chat
    @app.post("/api/chat")
    def chat():
        user = current_user()
        if not user:
            return jsonify(error="Please sign in."), 401
        if not request.is_json:                  # also blocks cross-site HTML form posts
            return jsonify(error="Send JSON."), 415
        question = (request.get_json(silent=True) or {}).get("question")
        if not isinstance(question, str) or not question.strip():
            return jsonify(error="Please type a question."), 400
        if len(question) > MAX_CHARS:
            return jsonify(error=f"Please keep your question under {MAX_CHARS} characters."), 400
        wait = limiter.check(user.id)
        if wait:
            resp = jsonify(error=f"You're asking too quickly. Try again in {wait} seconds.")
            resp.status_code, resp.headers["Retry-After"] = 429, str(wait)
            return resp
        t0 = time.perf_counter()
        try:
            out = answer_fn(question, user)      # `user` comes from the session, never from the body
        except PermissionError:
            return jsonify(error="You don't have access to that."), 403
        except Exception:                        # noqa: BLE001
            app.logger.exception("answer failed")
            return jsonify(error="The assistant is temporarily unavailable. Please try again."), 502
        return jsonify(public_view(out, int((time.perf_counter() - t0) * 1000)))

    return app


def warm_up():
    """Load the embedding and reranker models now, so the first question isn't slow."""
    try:
        from app.embed import embed
        from app.retrieve import _reranker
        embed(["warm up"])
        _reranker()
    except Exception as e:                       # noqa: BLE001
        print(f"(warm-up skipped: {type(e).__name__})")


if __name__ == "__main__":
    from dotenv import load_dotenv
    load_dotenv()
    try:
        app = create_app()                       # fail fast if there are no login accounts
    except FileNotFoundError as e:
        print(f"\n{e}")
        raise SystemExit(1)
    if not os.getenv("SESSION_SECRET"):
        print("Note: no SESSION_SECRET set, so sign-ins reset whenever the server restarts.")
    print("Loading models...")
    warm_up()
    port = int(os.getenv("PORT", "8000"))
    print(f"Open http://127.0.0.1:{port}")
    app.run(host="127.0.0.1", port=port, threaded=True)
