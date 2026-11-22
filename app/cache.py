"""Answer cache. The rule that matters: WHO is asking is part of the key.

A cached answer is only ever served to someone in the SAME access group, on the SAME
corpus/prompt version. So an answer produced for HR can never reach an employee.

Two tiers:  exact (same question after normalising case/punctuation)  and  semantic
(nearest cached question in the same group, within CACHE_THRESHOLD AND with the same
content words, because one changed word, "casual" vs "sick", changes the answer).

Never cached: blocked questions, answers that used tools (personal data), personal
questions, refusals, anything the output guard touched. The cache is an optimisation:
if Redis fails, lookups behave as a miss.
Settings (read at call time): CACHE_ENABLED (1) CACHE_THRESHOLD (0.15) CACHE_TTL (86400) CACHE_SALT (1)
"""
import hashlib
import json
import os
import re
import time
import uuid

import redis
from redis.commands.search.field import TagField, VectorField
from redis.commands.search.index_definition import IndexDefinition, IndexType
from redis.commands.search.query import Query

from app.embed import DIM, embed
from app.guardrails import REFUSAL_RX, UNVERIFIED
from app.identity import User, allowed_access
from app.router import is_personal
from app.store import r

INDEX = "idx:cache"
PREFIX = "cache:"

# ---- lexical safety check for the semantic tier -----------------------------
_STOP = {"a", "an", "the", "is", "are", "was", "were", "be", "been", "do", "does", "did", "i", "my",
         "me", "we", "you", "your", "it", "of", "in", "on", "at", "to", "for", "and", "or", "what",
         "whats", "how", "can", "could", "would", "should", "will", "there", "this", "that", "about",
         "if", "s"}
_BENIGN = {"please", "kindly", "tell", "show", "explain", "know", "wondering", "hi", "hello", "hey",
           "thanks", "thank", "per", "year", "yearly", "annually", "annual", "exactly", "actually",
           "just", "quick", "question", "company", "nimbus", "policy", "get", "entitled"}


def _content_words(text: str) -> set:
    words = re.sub(r"[^a-z0-9]+", " ", text.lower()).split()
    stemmed = [w[:-1] if len(w) > 3 and w.endswith("s") and not w.endswith("ss") else w for w in words]
    return {w for w in stemmed if w not in _STOP}


def lexical_ok(a: str, b: str) -> bool:
    """Two questions may share a cached answer only if every word that differs is filler."""
    return (_content_words(a) ^ _content_words(b)) <= {w for w in _BENIGN}


# ---- keys -----------------------------------------------------------------
def group_of(user: User) -> str:
    return "_".join(allowed_access(user))          # employee: all | manager: all_managers | hr: all_managers_hr


def version(system_prompt: str):
    """Changes when the corpus is re-ingested, the prompt changes, or CACHE_SALT is bumped."""
    try:
        corpus = (r.get("meta:corpus_version") or b"none").decode()
    except redis.RedisError:
        return None
    salt = re.sub(r"[^A-Za-z0-9]", "", os.getenv("CACHE_SALT", "1"))
    return f"{corpus}{hashlib.sha1(system_prompt.encode()).hexdigest()[:6]}{salt}"


def _norm(question: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[?!.\s]+$", "", question.strip().lower()))


def _exact_key(question: str, group: str, ver: str) -> str:
    return f"cx:{ver}:{group}:{hashlib.sha1(_norm(question).encode()).hexdigest()}"


def _ensure_index():
    try:
        r.ft(INDEX).info()
    except redis.ResponseError:
        r.ft(INDEX).create_index(
            [TagField("group"), TagField("version"),
             VectorField("embedding", "HNSW", {"TYPE": "FLOAT32", "DIM": DIM, "DISTANCE_METRIC": "COSINE"})],
            definition=IndexDefinition(prefix=[PREFIX], index_type=IndexType.HASH))


# ---- public API -----------------------------------------------------------
def lookup(question: str, user: User, ver: str):
    """Return (payload, kind, distance) or None. kind is 'exact' or 'semantic'."""
    group = group_of(user)                         # raises for unknown roles: fail closed
    try:
        raw = r.get(_exact_key(question, group, ver))
        if raw:
            return json.loads(raw), "exact", 0.0
        _ensure_index()
        vec = embed([question])[0].tobytes()
        q = (Query(f"(@group:{{{group}}} @version:{{{ver}}})=>[KNN 1 @embedding $vec AS distance]")
             .sort_by("distance").return_fields("distance").dialect(2))
        docs = r.ft(INDEX).search(q, {"vec": vec}).docs
        if not docs:
            return None
        dist = float(docs[0].distance)
        if dist > float(os.getenv("CACHE_THRESHOLD", "0.15")):
            return None
        payload = json.loads(r.hget(docs[0].id, "payload"))
        if not lexical_ok(question, payload["question"]):
            return None
        return payload, "semantic", dist
    except redis.RedisError:
        return None


def cacheable(question: str, out: dict) -> bool:
    g = out["guardrails"]
    return bool(
        not g["blocked"] and not out["tool_calls"] and not g["output_actions"]
        and out["sources"] and not is_personal(question)
        and out["answer"] != UNVERIFIED and not REFUSAL_RX.search(out["answer"]))


def store(question: str, user: User, ver: str, out: dict) -> None:
    group = group_of(user)
    ttl = int(os.getenv("CACHE_TTL", "86400"))
    payload = json.dumps({"question": question, "answer": out["answer"],
                          "sources": out["sources"], "stored_at": time.time()})
    try:
        r.set(_exact_key(question, group, ver), payload, ex=ttl)
        _ensure_index()
        key = f"{PREFIX}{uuid.uuid4().hex}"
        r.hset(key, mapping={"question": question, "group": group, "version": ver, "payload": payload,
                             "embedding": embed([question])[0].tobytes()})
        r.expire(key, ttl)
    except redis.RedisError:
        pass
