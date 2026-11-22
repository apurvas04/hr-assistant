"""Cache tests.  Python-only ones need nothing; test_redis_* need Redis + the embedding model.

    python -m pytest tests/test_cache.py -q
"""
import os
import uuid

import pytest

from app import cache
from app.guardrails import UNVERIFIED
from app.identity import get_user

EMP, EMP2, MGR, HR = get_user("E001"), get_user("E004"), get_user("E002"), get_user("E003")
SRC = [{"doc_id": "leave-policy", "title": "Leave Policy", "section": "Types of leave",
        "text": "Casual leave is 8 days.", "status": "current", "access": "all", "distance": 0.3}]
VER = "test" + uuid.uuid4().hex[:10]


def _out(answer="Casual leave is 8 days per year [1].", **over):
    out = {"answer": answer, "sources": SRC, "tool_calls": [],
           "guardrails": {"blocked": False, "input_reason": None, "output_actions": [], "repaired": False}}
    out.update(over)
    return out


# ---------------------------------------------------------------- pure python
def test_group_names():
    assert cache.group_of(EMP) == "all"
    assert cache.group_of(MGR) == "all_managers"
    assert cache.group_of(HR) == "all_managers_hr"


@pytest.mark.parametrize("a,b", [
    ("How many casual leave days do I get?", "how many casual leave days do I get per year?"),
    ("How many casual leave days do I get?", "Please tell me how many casual leave days I get"),
    ("How many casual leave days do I get?", "How many days of casual leave do I get?"),
    ("What is the hotel limit in Mumbai?", "Hotel limit in Mumbai?"),
    ("Can I carry forward sick leave?", "can i carry forward sick leave"),
])
def test_lexical_check_accepts_rewordings(a, b):
    assert cache.lexical_ok(a, b)


@pytest.mark.parametrize("a,b", [
    ("How many casual leave days do I get?", "How many sick leave days do I get?"),
    ("How many casual leave days do I get?", "How many earned leave days do I get?"),
    ("How long is paternity leave?", "How long is maternity leave?"),
    ("What is the salary band for L3?", "What is the salary band for L4?"),
    ("What is the hotel limit in Mumbai?", "What is the hotel limit in Delhi?"),
    ("Can I carry forward sick leave?", "Can I carry forward earned leave?"),
    ("Who approves international travel?", "Who approves domestic travel?"),
    ("How many days a week do I work from office?", "How many days a week can I work from another city?"),
    ("Can I take leave?", "Can I not take leave?"),
])
def test_lexical_check_rejects_a_changed_fact_word(a, b):
    assert not cache.lexical_ok(a, b)


def test_cacheable_accepts_a_clean_policy_answer():
    assert cache.cacheable("How many casual leave days do I get?", _out())


@pytest.mark.parametrize("question,over", [
    ("How many casual leave days do I get?", {"tool_calls": [{"name": "x", "args": {}, "result": "{}"}]}),
    ("How many casual leave days do I get?", {"guardrails": {"blocked": True, "output_actions": []}}),
    ("How many casual leave days do I get?", {"guardrails": {"blocked": False, "output_actions": ["pii:email"]}}),
    ("How many casual leave days do I get?", {"answer": UNVERIFIED}),
    ("How many casual leave days do I get?", {"answer": "I can't find that in the policies available to you."}),
    ("How many casual leave days do I get?", {"sources": []}),
    ("What is my leave balance?", {}),
])
def test_cacheable_rejects(question, over):
    assert not cache.cacheable(question, _out(**over))


# ---------------------------------------------------------- needs Redis + model
def teardown_module(module):
    from app.store import r
    for key in r.scan_iter(f"cx:{VER}:*"):
        r.delete(key)
    for key in r.scan_iter("cache:*"):
        v = r.hget(key, "version")
        if v and v.decode() == VER:
            r.delete(key)


def test_redis_exact_hit_ignores_case_and_punctuation():
    cache.store("How many casual leave days do I get?", EMP, VER, _out())
    payload, kind, _ = cache.lookup("how many casual leave days do i get", EMP, VER)
    assert kind == "exact" and "8 days" in payload["answer"]


def test_redis_users_in_the_same_group_share_answers():
    q = "What is the policy on carrying forward earned leave?"
    cache.store(q, EMP, VER, _out("Up to 10 days carry forward [1]."))
    assert cache.lookup(q, EMP2, VER) is not None          # Karan (employee) reuses Asha's answer


def test_redis_an_answer_is_never_served_across_access_groups():
    q = "What is the target bonus percentage for my level?"
    cache.store(q, HR, VER, _out("HR-ONLY: 10 percent [1]."))
    assert cache.lookup(q, HR, VER) is not None
    assert cache.lookup(q, EMP, VER) is None                # employee must miss
    assert cache.lookup(q, MGR, VER) is None                # manager must miss
    cache.store(q, MGR, VER, _out("Manager answer [1]."))
    assert "HR-ONLY" not in cache.lookup(q, MGR, VER)[0]["answer"]
    assert cache.lookup(q, EMP, VER) is None


def test_redis_semantic_tier_also_respects_groups():
    cache.store("What is the confidential salary band for level L3?", HR, VER, _out("14 to 22 LPA [1]."))
    assert cache.lookup("what is the confidential salary band for level L3 please", EMP, VER) is None


def test_redis_a_new_corpus_or_prompt_version_misses():
    q = "How many public holidays does the company have?"
    cache.store(q, EMP, VER, _out("12 public holidays [1]."))
    assert cache.lookup(q, EMP, VER + "x") is None


def test_redis_changed_fact_word_never_hits_even_if_embeddings_are_close():
    cache.store("How many casual leave days do I get?", EMP, VER, _out())
    os.environ["CACHE_THRESHOLD"] = "2.0"                   # accept ANY embedding distance
    try:
        for q in ("How many sick leave days do I get?", "How many earned leave days do I get?"):
            assert cache.lookup(q, EMP, VER) is None, q
    finally:
        os.environ.pop("CACHE_THRESHOLD", None)
