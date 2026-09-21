"""The Jev judge layer: three Score-primitive dimensions, deterministic
sampling, confidence-gated writes -- JAE v2 spec §5. No test here may
reach the real Jev API (tests/conftest.py's `_no_live_jev` guard enforces
this structurally); every JevJudge is built with an injected
httpx2.MockTransport.
"""

from __future__ import annotations

import httpx2
import pytest

from jira_agent_kit.judge import (
    DIMENSIONS,
    JevJudge,
    JudgeResult,
    confidence_action,
    should_sample,
)


def _client_returning(dimension: str, score: float, confidence: float) -> JevJudge:
    def handler(request):
        return httpx2.Response(
            200,
            json={
                "model": "jev-test",
                "usage": {"input_tokens": 1, "output_tokens": 1},
                "answers": {
                    dimension: {
                        "type": "score",
                        "score": score,
                        "confidence": confidence,
                        "legend": {str(i): c for i, c in enumerate(DIMENSIONS[dimension].criteria)},
                        "probabilities": {str(i): 0.0 for i in range(len(DIMENSIONS[dimension].criteria))},
                    }
                },
            },
        )
    return JevJudge(api_key="fake", transport=httpx2.MockTransport(handler))


# --- confidence_action: Jev's own confidence-tier guidance, §5.5 -----------


@pytest.mark.parametrize(
    "confidence,expected",
    [
        (1.0, "write"),
        (0.9, "write"),
        (0.89, "flag"),
        (0.5, "flag"),
        (0.49, "discard"),
        (0.0, "discard"),
    ],
)
def test_confidence_action_thresholds(confidence, expected):
    assert confidence_action(confidence) == expected


# --- should_sample: deterministic by issue_key, not random -----------------


def test_the_same_issue_key_always_samples_the_same_way():
    first = should_sample("PROJ-1", 0.2)
    second = should_sample("PROJ-1", 0.2)
    assert first == second


def test_sample_rate_zero_never_samples():
    for key in ("A-1", "B-2", "C-3", "D-4", "E-5"):
        assert should_sample(key, 0.0) is False


def test_sample_rate_one_always_samples():
    for key in ("A-1", "B-2", "C-3", "D-4", "E-5"):
        assert should_sample(key, 1.0) is True


def test_sampling_at_20_percent_is_roughly_1_in_5_over_many_keys():
    """Not exact -- a hash-based sampler has no promise of an exact count
    over any specific set -- but it must be in a sane range, not 0 and not
    everything.
    """
    sampled = sum(should_sample(f"KEY-{i}", 0.2) for i in range(2000))
    assert 300 < sampled < 500  # ~400 expected, generous band


# --- DIMENSIONS: the three phase-1 dimensions from spec §5.3 ---------------


def test_three_dimensions_are_defined():
    assert set(DIMENSIONS) == {"redundant_tests", "scope_creep", "prediction_accuracy"}


def test_every_dimension_has_at_least_two_criteria_levels():
    for name, spec in DIMENSIONS.items():
        assert len(spec.criteria) >= 2, name


# --- JevJudge.score: the real call shape ------------------------------------


def test_score_returns_the_judges_score_and_confidence():
    judge = _client_returning("redundant_tests", score=1.43, confidence=0.87)

    result = judge.score("redundant_tests", state="a diff and some test output")

    assert isinstance(result, JudgeResult)
    assert result.dimension == "redundant_tests"
    assert result.score == pytest.approx(1.43)
    assert result.confidence == pytest.approx(0.87)


def test_score_carries_the_confidence_action():
    judge = _client_returning("scope_creep", score=0.0, confidence=0.95)

    result = judge.score("scope_creep", state="x")

    assert result.action == "write"


def test_score_on_an_unknown_dimension_raises_with_a_clear_message():
    judge = _client_returning("redundant_tests", score=1.0, confidence=0.9)

    with pytest.raises(ValueError, match="unknown dimension"):
        judge.score("not_a_real_dimension", state="x")


# --- live guard: one deliberate, marked test proves the guard fires --------


@pytest.mark.jev_live
def test_a_jev_live_marked_test_is_permitted_through_the_guard():
    """Runs only with JIRA_AGENT_LIVE_TESTS=1 and a real TYPESAFE_API_KEY.
    Its purpose here is structural: this test EXISTING and being properly
    skipped by default is the proof that jev_live tests are opt-in, not
    that this specific assertion is interesting.
    """
    import os

    if not os.environ.get("TYPESAFE_API_KEY"):
        pytest.skip("no TYPESAFE_API_KEY in the environment")
    judge = JevJudge()
    result = judge.score("redundant_tests", state="one new test: test_addition_returns_sum")
    assert 0.0 <= result.confidence <= 1.0
