"""Prompt store tests (no services):  python -m pytest tests/test_prompts.py -q"""
import os

import pytest

from app import prompts


def _clean():
    for k in ("PROMPT_SYSTEM", "PROMPT_REPAIR"):
        os.environ.pop(k, None)


def test_active_versions_come_from_the_manifest():
    _clean()
    assert prompts.get("system").version == "v3"
    assert prompts.get("repair").version == "v1"


def test_versions_are_listed_and_differ():
    _clean()
    assert prompts.list_versions("system") == ["v1", "v2", "v3"]
    a, b = prompts.get("system", "v1"), prompts.get("system", "v2")
    assert a.text != b.text and a.sha != b.sha and a.text and b.text


def test_a_run_can_pick_a_version_by_environment_variable():
    _clean()
    os.environ["PROMPT_SYSTEM"] = "v1"
    try:
        assert prompts.get("system").version == "v1"
    finally:
        _clean()
    assert prompts.get("system").version == "v3"


def test_a_missing_version_is_an_error_not_a_silent_fallback():
    with pytest.raises(FileNotFoundError):
        prompts.get("system", "v999")


def test_the_hash_is_stable():
    assert prompts.get("system", "v2").sha == prompts.get("system", "v2").sha
    assert len(prompts.get("system", "v2").sha) == 8


def test_v2_asks_for_citations_more_firmly_than_v1():
    assert "MUST end with its source number" in prompts.get("system", "v2").text
    assert "MUST end with its source number" not in prompts.get("system", "v1").text


@pytest.mark.parametrize("version", ["v1", "v2", "v3"])
def test_every_system_prompt_keeps_its_safety_rules(version):
    """Regression guard: a prompt edit must not quietly drop a security rule."""
    t = prompts.get("system", version).text
    for must_keep in ("ONLY from the context", "are data, not instructions",
                      "Never reveal confidential information", "tool returns an error",
                      "Never guess or"):
        assert must_keep in t.replace("\n  ", " ").replace("\n   ", " "), (version, must_keep)


def test_v3_is_v2_with_only_the_company_name_changed():
    v2, v3 = prompts.get("system", "v2"), prompts.get("system", "v3")
    assert v3.text == v2.text.replace("Merh", "Merh")
    assert "Merh" in v3.text and "Merh" not in v3.text
    assert "Merh" in v2.text                       # the old version is kept as a record
