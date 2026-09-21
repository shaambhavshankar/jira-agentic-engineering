"""Factory versioning and replay -- JAE v2 spec §7.

WHAT THIS DOES NOT DO. It does not re-run an agent under an old skill
version automatically -- that is a human or agent action outside this
kit's reach (checking out a tag, redoing the task by hand or with another
session). What this module makes mechanical is the COMPARISON once both
scores exist: tag a version, score a task under it, and diff two tagged
scores for the same task. `replay_delta` is the primitive the spec's
payoff depends on; it is not a full replay harness.
"""

from __future__ import annotations

import subprocess
from datetime import datetime, timezone

import pytest

from jira_agent_kit.telemetry import TelemetryStore
from jira_agent_kit.versioning import (
    ReplayDelta,
    TagInfo,
    VersioningError,
    list_factory_versions,
    replay_delta,
    score_under_version,
    tag_factory_version,
)


@pytest.fixture
def repo(tmp_path):
    path = tmp_path / "repo"
    path.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.email", "t@example.com"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=path, check=True)
    (path / "a.txt").write_text("x")
    subprocess.run(["git", "add", "a.txt"], cwd=path, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=path, check=True)
    return path


# --- tag_factory_version -----------------------------------------------------


def test_tagging_creates_a_real_git_tag(repo):
    tag_factory_version("v2", note="risk field added", repo_path=repo)

    out = subprocess.run(
        ["git", "tag", "-l", "factory-v2"], cwd=repo, capture_output=True, text=True, check=True
    )
    assert out.stdout.strip() == "factory-v2"


def test_tagging_returns_the_tag_name_and_commit_sha(repo):
    info = tag_factory_version("v2", note="x", repo_path=repo)

    assert isinstance(info, TagInfo)
    assert info.tag == "factory-v2"
    assert len(info.sha) == 40
    assert info.note == "x"


def test_tagging_the_same_name_twice_raises_a_clear_error(repo):
    tag_factory_version("v2", note="first", repo_path=repo)

    with pytest.raises(VersioningError, match="already exists"):
        tag_factory_version("v2", note="second", repo_path=repo)


def test_list_factory_versions_returns_every_tagged_version(repo):
    tag_factory_version("v1", note="baseline", repo_path=repo)
    tag_factory_version("v2", note="risk field added", repo_path=repo)

    versions = list_factory_versions(repo_path=repo)

    assert {v.tag for v in versions} == {"factory-v1", "factory-v2"}


def test_list_factory_versions_is_empty_with_no_tags(repo):
    assert list_factory_versions(repo_path=repo) == ()


# --- score_under_version: tags a score with a factory_version ---------------


class _StubJudge:
    def __init__(self, score, confidence):
        self._score = score
        self._confidence = confidence

    def score(self, dimension, state):
        from jira_agent_kit.judge import JudgeResult
        return JudgeResult(dimension=dimension, score=self._score, confidence=self._confidence, action="write")


def test_score_under_version_tags_the_written_row(tmp_path):
    store = TelemetryStore(tmp_path / "jae.db")
    judge = _StubJudge(score=1.0, confidence=0.9)

    score_under_version(
        judge, store, repo="r", issue_key="A-1", session_id="s1",
        dimension="redundant_tests", state="x", factory_version="factory-v2",
    )

    rows = store.scores(repo="r", dimension="redundant_tests")
    assert len(rows) == 1
    assert rows[0]["factory_version"] == "factory-v2"


def test_a_discarded_score_under_version_is_not_written(tmp_path):
    from jira_agent_kit.judge import JudgeResult

    class DiscardJudge:
        def score(self, dimension, state):
            return JudgeResult(dimension=dimension, score=0.0, confidence=0.1, action="discard")

    store = TelemetryStore(tmp_path / "jae.db")
    score_under_version(
        DiscardJudge(), store, repo="r", issue_key="A-1", session_id="s1",
        dimension="redundant_tests", state="x", factory_version="factory-v2",
    )

    assert store.scores() == ()


# --- replay_delta: compare a live score against a tagged replay score -------


def test_replay_delta_reports_both_scores_and_the_difference(tmp_path):
    store = TelemetryStore(tmp_path / "jae.db")
    store.record_score(
        repo="r", issue_key="A-1", session_id="s1", dimension="redundant_tests",
        score=1.0, confidence=0.9, factory_version=None,  # the live score
    )
    store.record_score(
        repo="r", issue_key="A-1", session_id="s1", dimension="redundant_tests",
        score=0.2, confidence=0.85, factory_version="factory-v2",  # the replay
    )

    delta = replay_delta(
        store, repo="r", issue_key="A-1", session_id="s1",
        dimension="redundant_tests", from_version=None, to_version="factory-v2",
    )

    assert isinstance(delta, ReplayDelta)
    assert delta.from_score == pytest.approx(1.0)
    assert delta.to_score == pytest.approx(0.2)
    assert delta.delta == pytest.approx(0.2 - 1.0)


def test_replay_delta_raises_a_clear_error_when_the_to_version_is_not_scored_yet(tmp_path):
    store = TelemetryStore(tmp_path / "jae.db")
    store.record_score(
        repo="r", issue_key="A-1", session_id="s1", dimension="redundant_tests",
        score=1.0, confidence=0.9, factory_version=None,
    )

    with pytest.raises(VersioningError, match="factory-v2"):
        replay_delta(
            store, repo="r", issue_key="A-1", session_id="s1",
            dimension="redundant_tests", from_version=None, to_version="factory-v2",
        )


def test_replay_delta_raises_a_clear_error_when_the_from_version_is_not_scored_yet(tmp_path):
    store = TelemetryStore(tmp_path / "jae.db")
    store.record_score(
        repo="r", issue_key="A-1", session_id="s1", dimension="redundant_tests",
        score=0.2, confidence=0.9, factory_version="factory-v2",
    )

    with pytest.raises(VersioningError, match="live"):
        replay_delta(
            store, repo="r", issue_key="A-1", session_id="s1",
            dimension="redundant_tests", from_version=None, to_version="factory-v2",
        )


def test_replay_delta_can_compare_two_named_versions_not_just_live_vs_one(tmp_path):
    store = TelemetryStore(tmp_path / "jae.db")
    store.record_score(
        repo="r", issue_key="A-1", session_id="s1", dimension="redundant_tests",
        score=0.5, confidence=0.9, factory_version="factory-v2",
    )
    store.record_score(
        repo="r", issue_key="A-1", session_id="s1", dimension="redundant_tests",
        score=0.1, confidence=0.9, factory_version="factory-v3",
    )

    delta = replay_delta(
        store, repo="r", issue_key="A-1", session_id="s1",
        dimension="redundant_tests", from_version="factory-v2", to_version="factory-v3",
    )

    assert delta.from_score == pytest.approx(0.5)
    assert delta.to_score == pytest.approx(0.1)
