"""The multi-repo telemetry store: one SQLite file, every row tagged by
repo, readable per-repo or pooled -- the JAE v2 spec's §3.4 and §8.4
resolution ("multi-repo with learnings and judge for each individual repo
and overall as well") built as one schema, not two systems.
"""

from __future__ import annotations

import json
import subprocess
from datetime import datetime, timezone

import pytest

from jira_agent_kit.telemetry import (
    TaskRecord,
    TelemetryStore,
    append_jsonl,
    repo_name,
)


def _record(**overrides) -> TaskRecord:
    defaults = dict(
        issue_key="PROJ-1",
        repo="repo-a",
        session_id="sess-1",
        started_at=datetime(2026, 9, 22, 10, 0, tzinfo=timezone.utc),
        finished_at=datetime(2026, 9, 22, 10, 30, tzinfo=timezone.utc),
        model="claude-sonnet-5",
        files_changed=("a.py", "b.py"),
        test_exit_code=0,
        contract_exit_code=None,
        human_interactions=0,
        cost_usd=None,
    )
    defaults.update(overrides)
    return TaskRecord(**defaults)


# --- TelemetryStore: writing and reading -----------------------------------


def test_a_fresh_store_creates_its_db_file(tmp_path):
    db_path = tmp_path / "jae.db"
    assert not db_path.exists()

    TelemetryStore(db_path)

    assert db_path.exists()


def test_a_fresh_store_creates_its_parent_directory(tmp_path):
    db_path = tmp_path / "nested" / "dir" / "jae.db"

    TelemetryStore(db_path)

    assert db_path.exists()


def test_recording_a_task_is_readable_back(tmp_path):
    store = TelemetryStore(tmp_path / "jae.db")
    store.record(_record())

    tasks = store.tasks()

    assert len(tasks) == 1
    assert tasks[0].issue_key == "PROJ-1"
    assert tasks[0].repo == "repo-a"


def test_files_changed_round_trips_as_a_tuple_not_a_list(tmp_path):
    store = TelemetryStore(tmp_path / "jae.db")
    store.record(_record(files_changed=("x.py", "y.py")))

    got = store.tasks()[0].files_changed

    assert got == ("x.py", "y.py")
    assert isinstance(got, tuple)


def test_timestamps_round_trip_with_timezone(tmp_path):
    store = TelemetryStore(tmp_path / "jae.db")
    store.record(_record())

    got = store.tasks()[0]

    assert got.started_at == datetime(2026, 9, 22, 10, 0, tzinfo=timezone.utc)
    assert got.finished_at == datetime(2026, 9, 22, 10, 30, tzinfo=timezone.utc)


def test_a_none_contract_exit_code_round_trips_as_none(tmp_path):
    store = TelemetryStore(tmp_path / "jae.db")
    store.record(_record(contract_exit_code=None))

    assert store.tasks()[0].contract_exit_code is None


def test_a_real_contract_exit_code_round_trips(tmp_path):
    store = TelemetryStore(tmp_path / "jae.db")
    store.record(_record(contract_exit_code=1))

    assert store.tasks()[0].contract_exit_code == 1


def test_cost_usd_round_trips_when_present(tmp_path):
    store = TelemetryStore(tmp_path / "jae.db")
    store.record(_record(cost_usd=0.42))

    assert store.tasks()[0].cost_usd == pytest.approx(0.42)


# --- per-repo vs pooled reads: the whole point of §3.4 ----------------------


def test_scoping_to_one_repo_excludes_the_others(tmp_path):
    store = TelemetryStore(tmp_path / "jae.db")
    store.record(_record(repo="repo-a", issue_key="REPOA-1", session_id="a"))
    store.record(_record(repo="jira-agent-kit", issue_key="JAE-1", session_id="b"))

    only_sim = store.tasks(repo="repo-a")

    assert [t.issue_key for t in only_sim] == ["REPOA-1"]


def test_no_repo_filter_pools_every_repo(tmp_path):
    store = TelemetryStore(tmp_path / "jae.db")
    store.record(_record(repo="repo-a", issue_key="REPOA-1", session_id="a"))
    store.record(_record(repo="jira-agent-kit", issue_key="JAE-1", session_id="b"))

    pooled = store.tasks()

    assert {t.issue_key for t in pooled} == {"REPOA-1", "JAE-1"}


def test_a_repo_with_no_records_returns_empty_not_an_error(tmp_path):
    store = TelemetryStore(tmp_path / "jae.db")
    store.record(_record(repo="repo-a"))

    assert store.tasks(repo="some-other-repo") == ()


# --- the primary key: same (repo, issue_key, session_id) is one row --------


def test_recording_the_same_session_twice_upserts_not_duplicates(tmp_path):
    store = TelemetryStore(tmp_path / "jae.db")
    store.record(_record(human_interactions=0))
    store.record(_record(human_interactions=3))  # a re-run of finish for the same session

    tasks = store.tasks()

    assert len(tasks) == 1
    assert tasks[0].human_interactions == 3


def test_a_different_session_for_the_same_issue_is_a_second_row(tmp_path):
    store = TelemetryStore(tmp_path / "jae.db")
    store.record(_record(session_id="sess-1"))
    store.record(_record(session_id="sess-2"))  # the agent had to be re-run

    assert len(store.tasks()) == 2


# --- updating human_interactions after the fact (§4.2 is computed later) ---


def test_human_interactions_can_be_updated_after_recording(tmp_path):
    store = TelemetryStore(tmp_path / "jae.db")
    store.record(_record(human_interactions=0))

    store.update_human_interactions(
        repo="repo-a", issue_key="PROJ-1", session_id="sess-1", count=5
    )

    assert store.tasks()[0].human_interactions == 5


def test_updating_human_interactions_for_a_record_that_does_not_exist_is_silent(tmp_path):
    store = TelemetryStore(tmp_path / "jae.db")
    # No record exists yet. This must not raise -- a dashboard sweep that
    # races a `finish` call in progress should not crash either process.
    store.update_human_interactions(
        repo="nowhere", issue_key="X-1", session_id="none", count=1
    )

    assert store.tasks() == ()


# --- scores: judge output, per-repo and pooled, live vs replay-tagged ------


def test_recording_a_score_is_readable_back(tmp_path):
    store = TelemetryStore(tmp_path / "jae.db")
    store.record(_record())

    store.record_score(
        repo="repo-a", issue_key="PROJ-1", session_id="sess-1",
        dimension="redundant_tests", score=1.43, confidence=0.87,
    )

    scores = store.scores()
    assert len(scores) == 1
    assert scores[0]["dimension"] == "redundant_tests"
    assert scores[0]["score"] == pytest.approx(1.43)


def test_scores_scope_by_repo_same_as_tasks(tmp_path):
    store = TelemetryStore(tmp_path / "jae.db")
    store.record_score(
        repo="repo-a", issue_key="REPOA-1", session_id="a",
        dimension="scope_creep", score=0.0, confidence=0.9,
    )
    store.record_score(
        repo="jira-agent-kit", issue_key="JAE-1", session_id="b",
        dimension="scope_creep", score=2.0, confidence=0.9,
    )

    only_sim = store.scores(repo="repo-a")

    assert len(only_sim) == 1
    assert only_sim[0]["repo"] == "repo-a"


def test_scores_can_scope_by_dimension(tmp_path):
    store = TelemetryStore(tmp_path / "jae.db")
    store.record_score(
        repo="r", issue_key="X-1", session_id="a",
        dimension="redundant_tests", score=1.0, confidence=0.9,
    )
    store.record_score(
        repo="r", issue_key="X-1", session_id="a",
        dimension="scope_creep", score=0.0, confidence=0.9,
    )

    only_redundant = store.scores(dimension="redundant_tests")

    assert [s["dimension"] for s in only_redundant] == ["redundant_tests"]


def test_a_live_score_and_a_replay_score_for_the_same_task_are_both_kept(tmp_path):
    """factory_version distinguishes a live run (NULL) from a replay run
    (a tag like 'factory-v2') -- §7.2 replays the SAME task under two
    versions and needs both scores to survive, not overwrite each other.
    """
    store = TelemetryStore(tmp_path / "jae.db")
    store.record_score(
        repo="r", issue_key="X-1", session_id="a",
        dimension="redundant_tests", score=1.0, confidence=0.9,
        factory_version=None,
    )
    store.record_score(
        repo="r", issue_key="X-1", session_id="a",
        dimension="redundant_tests", score=0.2, confidence=0.9,
        factory_version="factory-v2",
    )

    assert len(store.scores()) == 2


def test_recording_the_same_live_score_twice_upserts_not_duplicates(tmp_path):
    """Real bug, found by actually running the real pipeline twice against
    the same real issue: standard SQL treats each NULL in a PRIMARY KEY as
    distinct from every other NULL, including itself -- so two live scores
    (factory_version=None both times) for the exact same task never
    collided on the PK, and ON CONFLICT never fired. Every re-score of a
    live task silently duplicated instead of updating.
    """
    store = TelemetryStore(tmp_path / "jae.db")
    store.record_score(
        repo="r", issue_key="X-1", session_id="a",
        dimension="redundant_tests", score=1.0, confidence=0.9,
    )
    store.record_score(
        repo="r", issue_key="X-1", session_id="a",
        dimension="redundant_tests", score=0.3, confidence=0.95,
    )

    rows = store.scores(repo="r", dimension="redundant_tests")

    assert len(rows) == 1
    assert rows[0]["score"] == pytest.approx(0.3)


# --- repo_name: derived from git remote, not typed by hand -----------------


def test_repo_name_strips_the_dot_git_suffix_from_an_https_remote(tmp_path):
    repo = tmp_path / "myrepo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(
        ["git", "remote", "add", "origin", "https://github.com/acme/myrepo.git"],
        cwd=repo, check=True,
    )

    assert repo_name(repo) == "myrepo"


def test_repo_name_handles_an_ssh_style_remote(tmp_path):
    repo = tmp_path / "another"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(
        ["git", "remote", "add", "origin", "git@github.com:acme/another.git"],
        cwd=repo, check=True,
    )

    assert repo_name(repo) == "another"


def test_repo_name_handles_a_remote_with_no_dot_git_suffix(tmp_path):
    repo = tmp_path / "bare-name"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(
        ["git", "remote", "add", "origin", "https://github.com/acme/bare-name"],
        cwd=repo, check=True,
    )

    assert repo_name(repo) == "bare-name"


def test_repo_name_raises_a_clear_error_with_no_origin_remote(tmp_path):
    repo = tmp_path / "no-remote"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)

    with pytest.raises(ValueError, match="origin"):
        repo_name(repo)


# --- append_jsonl: the local fallback, unchanged in shape -------------------


def test_append_jsonl_writes_one_line_per_record(tmp_path):
    path = tmp_path / "telemetry.jsonl"
    append_jsonl(_record(issue_key="A-1"), path)
    append_jsonl(_record(issue_key="A-2"), path)

    lines = path.read_text().splitlines()

    assert len(lines) == 2
    assert json.loads(lines[0])["issue_key"] == "A-1"
    assert json.loads(lines[1])["issue_key"] == "A-2"


def test_append_jsonl_creates_the_parent_directory(tmp_path):
    path = tmp_path / "nested" / "telemetry.jsonl"

    append_jsonl(_record(), path)

    assert path.exists()
