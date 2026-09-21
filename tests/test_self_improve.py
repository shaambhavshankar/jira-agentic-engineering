"""The self-improvement loop's MECHANICS -- JAE v2 spec §6.

WHAT THIS MODULE DOES NOT DO. It does not invoke an LLM itself. Finding
the common pattern across a batch of low-scored tasks and proposing a
concrete skill edit is exactly the kind of open-ended reasoning a Claude
Code session does, not a synchronous library call. This module prepares
the batch and a fixed prompt (`build_observer_prompt`); a human or agent
runs that prompt through a real session, and `propose_issue` turns
whatever text comes back into a reviewable Jira issue -- never applied to
a file automatically (spec §6.3, NG2: no unattended skill edits, ever).
"""

from __future__ import annotations

import pytest

from jira_agent_kit.self_improve import (
    MIN_SAMPLE,
    ScoredTask,
    SelfImproveError,
    bottom_third,
    build_observer_prompt,
    propose_issue,
)
from jira_agent_kit.schema import Vocabulary
from jira_agent_kit.telemetry import TelemetryStore


def _fill(store, *, repo, dimension, n, low_score=0.0, high_score=2.0):
    """Record `n` scores for `dimension`, alternating low/high, each under
    a distinct issue_key/session_id so they don't collide on the PK.
    """
    for i in range(n):
        score = low_score if i % 2 == 0 else high_score
        store.record_score(
            repo=repo, issue_key=f"A-{i}", session_id=f"s{i}",
            dimension=dimension, score=score, confidence=0.9,
        )


# --- bottom_third: the minimum-sample refusal, and the actual selection ----


def test_fewer_than_min_sample_refuses_with_a_clear_count(tmp_path):
    store = TelemetryStore(tmp_path / "jae.db")
    _fill(store, repo="r", dimension="redundant_tests", n=5)

    with pytest.raises(SelfImproveError, match="only 5"):
        bottom_third(store, dimension="redundant_tests", repo="r")


def test_exactly_min_sample_is_permitted(tmp_path):
    store = TelemetryStore(tmp_path / "jae.db")
    _fill(store, repo="r", dimension="redundant_tests", n=MIN_SAMPLE)

    batch = bottom_third(store, dimension="redundant_tests", repo="r")

    assert len(batch) > 0


def test_the_default_minimum_matches_warps_own_stated_floor():
    """20-25 failed runs, per the source transcript. 20, not less."""
    assert MIN_SAMPLE == 20


def test_a_custom_min_sample_can_be_lower(tmp_path):
    store = TelemetryStore(tmp_path / "jae.db")
    _fill(store, repo="r", dimension="redundant_tests", n=6)

    batch = bottom_third(store, dimension="redundant_tests", repo="r", min_sample=5)

    assert len(batch) > 0


def test_bottom_third_selects_only_the_lowest_scoring_tasks(tmp_path):
    store = TelemetryStore(tmp_path / "jae.db")
    # 30 tasks: 15 scored 0.0 (bad), 15 scored 2.0 (good).
    _fill(store, repo="r", dimension="redundant_tests", n=30)

    batch = bottom_third(store, dimension="redundant_tests", repo="r")

    assert all(t.score == pytest.approx(0.0) for t in batch)
    assert len(batch) == 10  # 30 // 3


def test_repo_none_pools_across_every_repo(tmp_path):
    store = TelemetryStore(tmp_path / "jae.db")
    _fill(store, repo="repo-a", dimension="redundant_tests", n=15)
    _fill(store, repo="repo-b", dimension="redundant_tests", n=15)

    # Neither repo alone clears MIN_SAMPLE=20, but pooled (30) does.
    with pytest.raises(SelfImproveError):
        bottom_third(store, dimension="redundant_tests", repo="repo-a")

    batch = bottom_third(store, dimension="redundant_tests", repo=None)

    assert {t.repo for t in batch} == {"repo-a", "repo-b"}


def test_returned_tasks_carry_enough_to_locate_the_original_work(tmp_path):
    store = TelemetryStore(tmp_path / "jae.db")
    _fill(store, repo="r", dimension="redundant_tests", n=20)

    batch = bottom_third(store, dimension="redundant_tests", repo="r")

    for t in batch:
        assert isinstance(t, ScoredTask)
        assert t.repo == "r"
        assert t.issue_key.startswith("A-")
        assert t.session_id.startswith("s")


# --- build_observer_prompt: the fixed prompt, not free-form -----------------


def test_prompt_names_the_dimension_and_task_count():
    batch = (ScoredTask(repo="r", issue_key="A-1", session_id="s1", score=0.0, confidence=0.9),)

    prompt = build_observer_prompt("redundant_tests", batch)

    assert "redundant_tests" in prompt
    assert "1" in prompt


def test_prompt_lists_every_repo_involved():
    batch = (
        ScoredTask(repo="repo-a", issue_key="A-1", session_id="s1", score=0.0, confidence=0.9),
        ScoredTask(repo="repo-b", issue_key="B-1", session_id="s2", score=0.0, confidence=0.9),
    )

    prompt = build_observer_prompt("redundant_tests", batch)

    assert "repo-a" in prompt and "repo-b" in prompt


def test_prompt_explicitly_forbids_applying_the_edit():
    batch = (ScoredTask(repo="r", issue_key="A-1", session_id="s1", score=0.0, confidence=0.9),)

    prompt = build_observer_prompt("redundant_tests", batch)

    assert "do not apply" in prompt.lower()


def test_prompt_lists_each_task_individually():
    batch = (
        ScoredTask(repo="r", issue_key="A-1", session_id="s1", score=0.0, confidence=0.9),
        ScoredTask(repo="r", issue_key="A-2", session_id="s2", score=0.1, confidence=0.8),
    )

    prompt = build_observer_prompt("redundant_tests", batch)

    assert "A-1" in prompt and "A-2" in prompt


# --- propose_issue: writes a reviewable Jira issue, never applies anything -


class _RecordingClient:
    def __init__(self):
        self.created = None

    def create_issue(self, **kwargs):
        self.created = kwargs
        return "PROJ-99"

    def resolve_issue_type_id(self, project_key, type_name):
        return "10005"


def test_propose_issue_creates_a_task_with_valid_closed_vocabulary_labels():
    """No invented label: 'eng-self-improve' isn't in the closed vocabulary
    (schema.py's own rule: a label outside it is rejected before any
    request is sent). goal=cleanup + origin=by-agent are real, existing
    categories that fit a proposal like this honestly.
    """
    client = _RecordingClient()
    vocab = Vocabulary("eng")
    batch = (ScoredTask(repo="repo-a", issue_key="A-1", session_id="s1", score=0.0, confidence=0.9),)

    key = propose_issue(
        client, project_key="PROJ", vocabulary=vocab,
        dimension="redundant_tests", batch=batch,
        proposal_text="Add a rule to SKILL.md: don't write a new test per assertion.",
    )

    assert key == "PROJ-99"
    assert "eng-cleanup" in client.created["labels"]
    assert "eng-by-agent" in client.created["labels"]


def test_propose_issue_names_every_affected_repo_in_the_summary():
    client = _RecordingClient()
    vocab = Vocabulary("eng")
    batch = (
        ScoredTask(repo="repo-a", issue_key="A-1", session_id="s1", score=0.0, confidence=0.9),
        ScoredTask(repo="repo-b", issue_key="B-1", session_id="s2", score=0.0, confidence=0.9),
    )

    propose_issue(
        client, project_key="PROJ", vocabulary=vocab,
        dimension="redundant_tests", batch=batch,
        proposal_text="x",
    )

    assert "repo-a" in client.created["summary"]
    assert "repo-b" in client.created["summary"]


def test_propose_issue_puts_the_proposal_text_in_the_description_not_dropped():
    client = _RecordingClient()
    vocab = Vocabulary("eng")
    batch = (ScoredTask(repo="repo-a", issue_key="A-1", session_id="s1", score=0.0, confidence=0.9),)

    propose_issue(
        client, project_key="PROJ", vocabulary=vocab,
        dimension="redundant_tests", batch=batch,
        proposal_text="Add a rule about not writing one test per assertion.",
    )

    adf = client.created["description_adf"]
    text = " ".join(
        run["text"]
        for node in adf["content"]
        for run in node.get("content", [])
        if run.get("type") == "text"
    )
    assert "one test per assertion" in text
