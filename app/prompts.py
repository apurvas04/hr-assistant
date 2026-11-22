"""Prompt store: prompts live in files, one file per version, so every change is
reviewable, comparable and reversible.

    prompts/manifest.json            which version is ACTIVE per prompt   {"system": "v2", ...}
    prompts/system/v1.txt, v2.txt    the versions themselves (never edit an old one; add a new one)

Override per run for A/B comparisons:  set PROMPT_SYSTEM=v1   (or PROMPT_REPAIR=...)
Every answer's trace records name, version and a short hash of the text it used.
A missing file raises an error: there is no silent fallback to some other prompt.
"""
import hashlib
import json
import os
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

PROMPT_DIR = Path(__file__).resolve().parent.parent / "prompts"


@dataclass(frozen=True)
class Prompt:
    name: str
    version: str
    text: str
    sha: str


def active_version(name: str) -> str:
    override = os.getenv(f"PROMPT_{name.upper()}", "").strip()
    if override:
        return override
    manifest = json.loads((PROMPT_DIR / "manifest.json").read_text(encoding="utf-8"))
    return manifest[name]


@lru_cache(maxsize=32)
def _load(name: str, version: str) -> Prompt:
    text = (PROMPT_DIR / name / f"{version}.txt").read_text(encoding="utf-8").strip()
    return Prompt(name, version, text, hashlib.sha1(text.encode()).hexdigest()[:8])


def get(name: str, version: str | None = None) -> Prompt:
    return _load(name, version or active_version(name))


def list_versions(name: str) -> list[str]:
    return sorted(p.stem for p in (PROMPT_DIR / name).glob("*.txt"))
