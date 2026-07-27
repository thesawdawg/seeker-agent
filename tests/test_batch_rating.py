"""
Batched relevance rating (review V3).

One call per paper was the dominant model cost of the whole pipeline —
several hundred to a few thousand calls per run — to produce a single
ordering column. These pin the batching, and the degradation rules that keep
a bad batch from fabricating judgements.
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest

from agents import social


def _responder(answer):
    """Stub llm.call, counting invocations."""
    calls = {"n": 0, "prompts": []}

    def fake(prompt, system, agent_name="unknown", **kw):
        calls["n"] += 1
        calls["prompts"].append(prompt)
        return answer(prompt) if callable(answer) else answer
    return fake, calls


def _well_formed(prompt):
    import re
    n = len(re.findall(r"^\d+\. ", prompt, re.M))
    return json.dumps([{"n": i, "rating": "High" if i == 1 else "Low",
                        "reason": f"reason {i}"} for i in range(1, n + 1)])


def _papers(count):
    return [{"title": f"Paper {i}", "abstract": "an abstract"} for i in range(count)]


# ---------------------------------------------------------------------------
# The saving
# ---------------------------------------------------------------------------

def test_a_batch_is_one_call_not_one_per_paper(monkeypatch):
    fake, calls = _responder(_well_formed)
    monkeypatch.setattr(social.llm, "call", fake)

    out = social.rate_relevance_batch(_papers(10), "problem", "theme")
    assert len(out) == 10
    assert calls["n"] == 1


def test_large_sets_are_split_into_full_batches(monkeypatch):
    fake, calls = _responder(_well_formed)
    monkeypatch.setattr(social.llm, "call", fake)

    out = social.rate_relevance_batch(_papers(95), "problem", "theme")
    assert len(out) == 95
    expected = -(-95 // social.RATING_BATCH_SIZE)
    assert calls["n"] == expected == 10


def test_an_empty_list_makes_no_calls(monkeypatch):
    fake, calls = _responder(_well_formed)
    monkeypatch.setattr(social.llm, "call", fake)
    assert social.rate_relevance_batch([], "problem", "theme") == []
    assert calls["n"] == 0


def test_ratings_stay_aligned_with_their_papers(monkeypatch):
    monkeypatch.setattr(social.llm, "call", lambda p, s, **k: json.dumps([
        {"n": 1, "rating": "Low", "reason": "first"},
        {"n": 2, "rating": "High", "reason": "second"},
        {"n": 3, "rating": "Medium", "reason": "third"},
    ]))
    out = social.rate_relevance_batch(_papers(3), "problem", "theme")
    assert out == [("Low", "first"), ("High", "second"), ("Medium", "third")]


def test_a_reordered_response_is_realigned_by_its_own_numbering(monkeypatch):
    """Models return lists out of order; "n" is what puts them back."""
    monkeypatch.setattr(social.llm, "call", lambda p, s, **k: json.dumps([
        {"n": 3, "rating": "High", "reason": "third"},
        {"n": 1, "rating": "Low", "reason": "first"},
        {"n": 2, "rating": "Medium", "reason": "second"},
    ]))
    out = social.rate_relevance_batch(_papers(3), "problem", "theme")
    assert [r for r, _ in out] == ["Low", "Medium", "High"]


# ---------------------------------------------------------------------------
# Degradation — never fabricate a rating (review V5)
# ---------------------------------------------------------------------------

def test_a_failed_call_leaves_the_batch_unrated_not_medium(monkeypatch):
    monkeypatch.setattr(social.llm, "call",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("no provider")))
    out = social.rate_relevance_batch(_papers(4), "problem", "theme")
    assert len(out) == 4
    assert all(rating is None for rating, _ in out)
    assert all("not assessed" in reason for _, reason in out)


def test_unparseable_json_leaves_the_batch_unrated(monkeypatch):
    monkeypatch.setattr(social.llm, "call", lambda *a, **k: "I cannot comply.")
    out = social.rate_relevance_batch(_papers(3), "problem", "theme")
    assert all(rating is None for rating, _ in out)


def test_a_short_response_only_loses_the_missing_entries(monkeypatch):
    """One bad line must not discard the good judgements beside it."""
    monkeypatch.setattr(social.llm, "call", lambda p, s, **k: json.dumps([
        {"n": 1, "rating": "High", "reason": "kept"},
        {"n": 2, "rating": "Low", "reason": "also kept"},
    ]))
    out = social.rate_relevance_batch(_papers(4), "problem", "theme")
    assert out[0] == ("High", "kept")
    assert out[1] == ("Low", "also kept")
    assert out[2][0] is None and "missing" in out[2][1]
    assert out[3][0] is None


def test_an_invalid_rating_value_is_unrated(monkeypatch):
    monkeypatch.setattr(social.llm, "call", lambda p, s, **k: json.dumps([
        {"n": 1, "rating": "Extremely High", "reason": "off-scale"},
        {"n": 2, "rating": "Medium", "reason": "fine"},
    ]))
    out = social.rate_relevance_batch(_papers(2), "problem", "theme")
    assert out[0][0] is None and "unusable rating" in out[0][1]
    assert out[1] == ("Medium", "fine")


def test_one_failed_batch_does_not_sink_the_others(monkeypatch):
    state = {"n": 0}

    def flaky(prompt, system, **kw):
        state["n"] += 1
        if state["n"] == 2:
            raise RuntimeError("transient")
        return _well_formed(prompt)

    monkeypatch.setattr(social.llm, "call", flaky)
    out = social.rate_relevance_batch(_papers(30), "problem", "theme")
    assert len(out) == 30
    rated = [r for r, _ in out if r is not None]
    unrated = [r for r, _ in out if r is None]
    assert len(unrated) == social.RATING_BATCH_SIZE       # exactly one batch lost
    assert len(rated) == 20


def test_the_prompt_asks_for_comparative_judgement(monkeypatch):
    """
    Rating papers one at a time gave the model nothing to compare against,
    which is how the shipped database ended up 90% Low. The batch prompt has
    to actually ask for a spread.
    """
    fake, calls = _responder(_well_formed)
    monkeypatch.setattr(social.llm, "call", fake)
    social.rate_relevance_batch(_papers(5), "the problem", "the theme")

    assert "Do not rate everything Medium" in social.BATCH_RATING_SYSTEM
    prompt = calls["prompts"][0]
    assert "the problem" in prompt and "the theme" in prompt
    for i in range(1, 6):
        assert f"{i}. Paper {i - 1}" in prompt


def test_single_paper_rating_still_works(monkeypatch):
    """The one-at-a-time path is still used by callers outside Social."""
    monkeypatch.setattr(
        social.llm, "call",
        lambda *a, **k: '{"rating": "High", "reason": "on point"}')
    assert social.rate_relevance("t", "a", "p", "th") == ("High", "on point")
