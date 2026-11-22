"""Web server: mock sign-in + chat API + static chat page.

    python -m app.server          then open http://127.0.0.1:8000

This is the "gateway" box of the architecture: it decides WHO is asking (a signed session
cookie) and enforces a per-user rate limit. The user id never comes from the request body,
so a client cannot ask "as" someone else. The sign-in is a DEMO stand-in for company SSO.

Settings: PORT (8000)  SESSION_SECRET (random per start)  RATE_LIMIT_PER_MIN (20)
"""
import os
import secrets
import threading
import time
from collections import defaultdict, deque
from datetime import timedelta
from pathlib import Path

from flask import Flask, jsonify, redirect, request, send_from_directory, session

from app.guardrails import MAX_CHARS, cited_numbers
from app.identity import all_records, get_user

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


def create_app(answer_fn=None, rate_limit: int | None = None, secret: str | None = None) -> Flask:
    app = Flask(__name__, static_folder=None)
    app.secret_key = secret or os.getenv("SESSION_SECRET") or secrets.token_hex(32)
    app.config.update(SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE="Lax",
                      PERMANENT_SESSION_LIFETIME=timedelta(hours=SESSION_HOURS),
                      MAX_CONTENT_LENGTH=16 * 1024)
    answer_fn = answer_fn or _default_answer_fn
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

    # ---- sign-in (demo)
    @app.get("/api/users")
    def users():
        return jsonify(users=[{"id": r["id"], "name": r["name"], "role": r["role"],
                               "department": r["department"]} for r in all_records()])

    @app.post("/api/login")
    def login():
        if not request.is_json:
            return jsonify(error="Send JSON."), 415
        uid = (request.get_json(silent=True) or {}).get("user_id")
        try:
            user = get_user(uid) if isinstance(uid, str) else None
        except PermissionError:
            user = None
        if user is None:
            return jsonify(error="Unknown user."), 400
        session.clear()
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
    if not os.getenv("SESSION_SECRET"):
        print("Note: no SESSION_SECRET set, so sign-ins reset whenever the server restarts.")
    print("Loading models...")
    warm_up()
    port = int(os.getenv("PORT", "8000"))
    print(f"Open http://127.0.0.1:{port}")
    create_app().run(host="127.0.0.1", port=port, threaded=True)
