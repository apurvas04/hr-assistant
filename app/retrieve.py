"""Retrieval with access control.

Every query is filtered INSIDE Redis by (a) what the asker's role may read and
(b) policy status. The filter runs before the nearest-neighbour search, so
restricted text never reaches the reranker, the prompt, or any log or trace.

`user` is a required argument with no default: forgetting it is a TypeError,
not a silent unfiltered search. There is deliberately only one query path.
Spans record ids, counts and distances, never chunk text.
"""
from functools import lru_cache

from redis.commands.search.query import Query
from sentence_transformers import CrossEncoder

from app import tracing
from app.embed import embed
from app.identity import User, allowed_access
from app.store import r, INDEX

FIELDS = ("doc_id", "title", "section", "text", "status", "access")


@lru_cache(maxsize=1)
def _reranker():
    return CrossEncoder("cross-encoder/ms-marco-MiniLM-L-6-v2")


def _filter(user: User, include_archived: bool) -> str:
    groups = "|".join(allowed_access(user))          # raises for unknown roles
    parts = [f"@access:{{{groups}}}"]
    if not include_archived:
        parts.append("@status:{current}")
    return "(" + " ".join(parts) + ")"


def _vector(question: str, user: User, n: int, include_archived: bool) -> list[dict]:
    with tracing.span("vector_search", n=n) as sp:
        vec = embed([question])[0].tobytes()
        q = (Query(f"{_filter(user, include_archived)}=>[KNN {n} @embedding $vec AS distance]")
             .sort_by("distance").return_fields(*FIELDS, "distance").dialect(2))
        docs = r.ft(INDEX).search(q, {"vec": vec}).docs
        rows = []
        for d in docs:
            row = {f: getattr(d, f) for f in FIELDS}
            row["distance"] = float(d.distance)
            rows.append(row)
        sp.set(n_found=len(rows))
        return rows


def retrieve(question: str, user: User, k: int = 4, mode: str = "vector_rerank",
             include_archived: bool = False, candidates: int = 20) -> list[dict]:
    with tracing.span("retrieve", mode=mode, k=k, group="_".join(allowed_access(user))) as sp:
        if mode == "vector":
            hits = _vector(question, user, k, include_archived)
        elif mode == "vector_rerank":
            pool = _vector(question, user, candidates, include_archived)[:15]
            with tracing.span("rerank", candidates=len(pool)):
                pairs = [(question, f'{h["title"]} - {h["section"]}\n{h["text"]}') for h in pool]
                for h, s in zip(pool, _reranker().predict(pairs)):
                    h["rerank_score"] = float(s)
                pool.sort(key=lambda h: h["rerank_score"], reverse=True)
            hits = pool[:k]
        else:
            raise ValueError(f"unknown mode: {mode}")
        sp.set(n_hits=len(hits),
               top_distance=min((h["distance"] for h in hits), default=None),
               chunks=[f'{h["doc_id"]}>{h["section"]}' for h in hits])     # ids only, never text
        return hits
