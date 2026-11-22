"""Calibrate the semantic cache (needs the embedding model, no Redis, no LLM):

    python -m eval.run_cache

For each labelled pair it prints the embedding distance, whether the word check passes,
and whether the cache WOULD serve one question's answer for the other.
`false hits` must be 0. The "embeddings only" line shows what would go wrong without the word check.
"""
import json
import os
from pathlib import Path

from app.cache import lexical_ok
from app.embed import embed

PAIRS = Path(__file__).parent / "cache_pairs.jsonl"


def main():
    threshold = float(os.getenv("CACHE_THRESHOLD", "0.15"))
    pairs = [json.loads(l) for l in open(PAIRS, encoding="utf-8") if l.strip()]
    va = embed([p["a"] for p in pairs])
    vb = embed([p["b"] for p in pairs])
    rows = []
    for p, x, y in zip(pairs, va, vb):
        d = float(1.0 - x @ y)                     # cosine distance (vectors are normalised)
        rows.append({**p, "dist": d, "lex": lexical_ok(p["a"], p["b"])})

    print(f"threshold = {threshold}\n")
    print(f'{"dist":>6}  {"words":>5}  {"serves?":>7}  {"should":>6}   pair')
    for r in sorted(rows, key=lambda r: r["dist"]):
        serves = r["dist"] <= threshold and r["lex"]
        flag = "   <-- FALSE HIT" if serves and not r["should_hit"] else ""
        print(f'{r["dist"]:6.3f}  {"ok" if r["lex"] else "diff":>5}  {str(serves):>7}  '
              f'{str(r["should_hit"]):>6}   {r["a"]}  |  {r["b"]}{flag}')

    pos = [r for r in rows if r["should_hit"]]
    neg = [r for r in rows if not r["should_hit"]]
    false_hits = [r for r in neg if r["dist"] <= threshold and r["lex"]]
    emb_only = [r for r in neg if r["dist"] <= threshold]
    hits = [r for r in pos if r["dist"] <= threshold and r["lex"]]
    print(f"\nfalse hits (different question served a cached answer): {len(false_hits)} of {len(neg)}   <- must be 0")
    print(f"embeddings only, no word check, would have falsely hit:   {len(emb_only)} of {len(neg)}")
    print(f"paraphrases served from cache:                            {len(hits)} of {len(pos)}")
    closest_neg = min(r["dist"] for r in neg)
    print(f"closest DIFFERENT pair is at distance {closest_neg:.3f}; "
          f"farthest paraphrase is at {max(r['dist'] for r in pos):.3f}")


if __name__ == "__main__":
    main()
