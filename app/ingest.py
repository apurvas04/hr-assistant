import hashlib
import re
from pathlib import Path

import yaml

from app.embed import embed
from app.store import r, reset_index

POLICY_DIR = Path("data/policies")


def parse(path: Path):
    _, front, body = path.read_text(encoding="utf-8").split("---", 2)
    return yaml.safe_load(front), body.strip()


def chunk(meta: dict, body: str) -> list[dict]:
    parts = re.split(r"(?m)^## ", body)
    intro = re.sub(r"(?m)^# .*$", "", parts[0]).strip()   # drop the H1 line
    pieces = []
    if intro:
        pieces.append(("Overview", intro))
    for sec in parts[1:]:
        heading, _, text = sec.partition("\n")
        pieces.append((heading.strip(), text.strip()))
    return [
        {
            "doc_id": meta["id"], "title": meta["title"], "section": heading,
            "access": meta["access"], "status": meta["status"],
            "text": text,
            # what we embed carries the context; what we show the LLM is `text`
            "embed_text": f'{meta["title"]} - {heading}\n{text}',
        }
        for heading, text in pieces
    ]


def main():
    reset_index()
    chunks = []
    for path in sorted(POLICY_DIR.glob("*.md")):      # employees.json is not a policy
        meta, body = parse(path)
        chunks += chunk(meta, body)
    vectors = embed([c["embed_text"] for c in chunks])
    pipe = r.pipeline()
    for i, (c, v) in enumerate(zip(chunks, vectors)):
        mapping = {k: c[k] for k in ("doc_id", "title", "section", "access", "status", "text")}
        mapping["embedding"] = v.tobytes()
        pipe.hset(f'chunk:{c["doc_id"]}:{i}', mapping=mapping)
    # the answer cache keys on this, so re-ingesting changed policies invalidates old answers
    version = hashlib.sha1("".join(c["embed_text"] + c["access"] for c in chunks).encode()).hexdigest()[:12]
    pipe.set("meta:corpus_version", version)
    pipe.execute()
    print(f"Ingested {len(chunks)} chunks from {len(list(POLICY_DIR.glob('*.md')))} documents "
          f"(corpus version {version})")


if __name__ == "__main__":
    main()
