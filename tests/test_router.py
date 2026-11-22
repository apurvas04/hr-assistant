"""Router tests (pure, no services):  python -m pytest tests/test_router.py -q"""
import os

from app.router import is_personal, models_for, route


def H(doc, dist=0.30):
    return {"doc_id": doc, "distance": dist}


SAME_DOC = [H("leave-policy", 0.30), H("leave-policy", 0.45), H("benefits", 0.60)]


def _clean_env():
    for k in ("ROUTER_ENABLED", "ROUTER_MAX_DISTANCE", "MODEL_SMALL", "MODEL_LARGE", "ANSWER_MODELS"):
        os.environ.pop(k, None)


def test_simple_single_doc_question_goes_small():
    _clean_env()
    d = route("How many casual leave days do I get?", SAME_DOC)
    assert d["tier"] == "small" and d["reasons"] == ["simple_single_doc"]


def test_personal_questions_go_large():
    _clean_env()
    for q in ["What is my leave balance?", "Who reports to me?", "Who is my manager?",
              "How many days do I have left?"]:
        assert route(q, SAME_DOC)["tier"] == "large", q
        assert "personal_or_tool" in route(q, SAME_DOC)["reasons"]
    assert is_personal("What is Priya Nair's leave balance?")


def test_weak_retrieval_goes_large():
    _clean_env()
    d = route("How many casual leave days do I get?", [H("leave-policy", 0.80)])
    assert d["tier"] == "large" and any(r.startswith("weak_retrieval") for r in d["reasons"])


def test_threshold_is_configurable():
    _clean_env()
    os.environ["ROUTER_MAX_DISTANCE"] = "0.90"
    try:
        assert route("How many casual leave days do I get?", [H("leave-policy", 0.80)])["tier"] == "small"
    finally:
        _clean_env()


def test_long_or_multi_part_or_comparison_goes_large():
    _clean_env()
    assert "long" in route("x " * 100, SAME_DOC)["reasons"]
    assert "complex" in route("What is casual leave? And what is sick leave?", SAME_DOC)["reasons"]
    assert "complex" in route("What is the difference between casual and sick leave", SAME_DOC)["reasons"]


def test_top_two_chunks_from_different_documents_go_large():
    _clean_env()
    d = route("How many days can I take?", [H("leave-policy"), H("parental-leave", 0.35)])
    assert d["tier"] == "large" and "multi_doc" in d["reasons"]


def test_no_context_goes_large():
    _clean_env()
    assert route("What is the dress code?", [])["tier"] == "large"


def test_router_can_be_switched_off():
    _clean_env()
    os.environ["ROUTER_ENABLED"] = "0"
    try:
        assert route("How many casual leave days do I get?", SAME_DOC) == {
            "tier": "large", "reasons": ["router_disabled"]}
    finally:
        _clean_env()


def test_model_order_and_fallbacks():
    _clean_env()
    assert models_for("small")[:2] == ["openai/gpt-oss-20b", "openai/gpt-oss-120b"]
    assert models_for("large")[:2] == ["openai/gpt-oss-120b", "openai/gpt-oss-20b"]
    os.environ["ANSWER_MODELS"] = "openai/gpt-oss-120b,openai/gpt-oss-20b,qwen/qwen3.8-27b"
    try:
        m = models_for("small")
        assert m == ["openai/gpt-oss-20b", "openai/gpt-oss-120b", "qwen/qwen3.8-27b"]
        assert len(m) == len(set(m))                     # no duplicates
    finally:
        _clean_env()
