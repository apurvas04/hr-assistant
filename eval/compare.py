"""Side-by-side table of every saved run:  python -m eval.compare"""
import json
from pathlib import Path

COLS = [("doc_hit@1", "doc@1"), ("chunk_hit@1", "chunk@1"), ("chunk_hit@3", "chunk@3"),
        ("chunk_mrr", "MRR"), ("archived_in_topk_rate", "archived"),
        ("version_current_beats_archived", "ver-ok"), ("leaks_as_employee", "leaks")]

runs = sorted(Path(__file__).parent.joinpath("results").glob("*.json"))
print(f'{"run":14}' + "".join(f"{label:>10}" for _, label in COLS))
for f in runs:
    s = json.loads(f.read_text(encoding="utf-8"))["summary"]
    cells = []
    for key, _ in COLS:
        v = s.get(key)
        cells.append(f'{"n/a":>10}' if v is None else f"{v:>10.3f}")
    print(f"{f.stem:14}" + "".join(cells))
