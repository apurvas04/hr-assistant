"""Rename the company name everywhere in the project.

    python rename_brand.py              DRY RUN: lists every file and line that would change
    python rename_brand.py --apply      makes the changes

Run from the project root. Defaults: Merh -> Merh  (also merh -> merh, MERH -> MERH).
Other names:   python rename_brand.py --old Merh --new Acme --apply

What it deliberately does NOT touch:
  * .git, .venv, __pycache__, traces/, eval/results/   (history, generated files, measurements)
  * .env                                               (secrets)
  * existing prompt versions (prompts/<name>/vN.txt)   prompts are versioned and never edited in place.
    Instead, if the ACTIVE version mentions the old name, it writes the next version (v3, ...) with the
    new name and points prompts/manifest.json at it. Old versions stay as a record.
After applying:  python -m app.ingest   (the policy text changed, so this also refreshes the answer cache)
"""
import argparse
import json
import re
import sys
from pathlib import Path

SKIP_DIRS = {".git", ".venv", "venv", "__pycache__", "node_modules", ".pytest_cache", "traces"}
SKIP_PREFIXES = [("eval", "results"), ("prompts",)]
TEXT_SUFFIXES = {".py", ".md", ".txt", ".json", ".jsonl", ".html", ".js", ".css", ".yml", ".yaml",
                 ".toml", ".cfg", ".ini", ".example", ".csv"}
TEXT_NAMES = {".gitignore", ".env.example"}


def variants(old: str, new: str):
    return [(old.upper(), new.upper()), (old.capitalize(), new.capitalize()), (old.lower(), new.lower())]


def replace_all(text: str, pairs) -> str:
    for a, b in pairs:
        text = text.replace(a, b)
    return text


def candidate_files(root: Path):
    for p in sorted(root.rglob("*")):
        if not p.is_file():
            continue
        rel = p.relative_to(root)
        parts = rel.parts
        if set(parts[:-1]) & SKIP_DIRS or p.name == ".env":
            continue
        if any(parts[:len(pre)] == pre for pre in SKIP_PREFIXES):
            continue
        if p.suffix.lower() in TEXT_SUFFIXES or p.name in TEXT_NAMES:
            yield p, rel


def read_text(p: Path):
    try:
        data = p.read_bytes()
        if b"\x00" in data:
            return None
        return data.decode("utf-8")
    except (UnicodeDecodeError, OSError):
        return None


def next_version(folder: Path) -> str:
    nums = [int(m.group(1)) for f in folder.glob("v*.txt") if (m := re.fullmatch(r"v(\d+)", f.stem))]
    return f"v{max(nums, default=0) + 1}"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--old", default="Merh")
    ap.add_argument("--new", default="Merh")
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()
    root, pairs = Path.cwd(), variants(args.old, args.new)
    rx = re.compile(re.escape(args.old), re.I)

    changes = []
    for p, rel in candidate_files(root):
        text = read_text(p)
        if text and rx.search(text):
            lines = [(i, ln) for i, ln in enumerate(text.splitlines(), 1) if rx.search(ln)]
            changes.append((p, rel, text, lines))

    total = 0
    print(f"Files that mention '{args.old}':\n")
    for p, rel, text, lines in changes:
        print(f"  {rel.as_posix()}  ({len(lines)} line{'s' if len(lines) != 1 else ''})")
        for i, ln in lines:
            print(f"      {i:>4}: {ln.strip()[:110]}")
        total += len(lines)

    # prompts: version, never edit in place
    prompt_plan = []
    manifest_path = root / "prompts" / "manifest.json"
    if manifest_path.exists():
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        for name, ver in manifest.items():
            f = root / "prompts" / name / f"{ver}.txt"
            if f.exists() and rx.search(f.read_text(encoding="utf-8")):
                prompt_plan.append((name, ver, next_version(f.parent), f))
    if prompt_plan:
        print("\nPrompts (versioned, so a NEW version is created instead of editing the old one):")
        for name, ver, nxt, f in prompt_plan:
            print(f"  prompts/{name}/{ver}.txt mentions '{args.old}'  ->  create prompts/{name}/{nxt}.txt "
                  f"and set the manifest to {nxt}")

    print(f"\n{len(changes)} file(s), {total} line(s)" + (f", {len(prompt_plan)} new prompt version(s)" if prompt_plan else "") + ".")
    if not args.apply:
        print("DRY RUN: nothing was changed. Add --apply to make the changes.")
        return 0

    for p, rel, text, lines in changes:
        p.write_text(replace_all(text, pairs), encoding="utf-8", newline="")
    if prompt_plan:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        for name, ver, nxt, f in prompt_plan:
            new_file = f.parent / f"{nxt}.txt"
            new_file.write_text(replace_all(f.read_text(encoding="utf-8"), pairs), encoding="utf-8", newline="")
            manifest[name] = nxt
        manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    print("Done. Now run:  python -m app.ingest")
    return 0


if __name__ == "__main__":
    sys.exit(main())
