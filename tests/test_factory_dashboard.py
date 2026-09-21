"""The manager dashboard: per-repo and overall, computed from the
telemetry store -- JAE v2 spec §4, and its own honesty rule: a number this
module cannot actually compute is reported as None with a label, never
guessed. PR-to-first-human-review needs a Jira/GitHub read this item does
not make yet (§4.1); it is intentionally absent from DashboardStats rather
than faked from data that doesn't carry it.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from jira_agent_kit.factory_dashboard import (
    DashboardStats,
    compute_stats,
    per_repo_breakdown,
    render_html,
)
from jira_agent_kit.telemetry import TaskRecord, TelemetryStore


def _record(**overrides) -> TaskRecord:
    started = datetime(2026, 9, 22, 10, 0, tzinfo=timezone.utc)
    defaults = dict(
        issue_key="A-1",
        repo="repo-a",
        session_id="sess-1",
        started_at=started,
        finished_at=started + timedelta(minutes=30),
        model="claude-sonnet-5",
        files_changed=("a.py",),
        test_exit_code=0,
        contract_exit_code=None,
        human_interactions=0,
        cost_usd=None,
    )
    defaults.update(overrides)
    return TaskRecord(**defaults)


# --- compute_stats: the four numbers ----------------------------------------


def test_an_empty_repo_reports_zero_tasks_and_no_rates(tmp_path):
    store = TelemetryStore(tmp_path / "jae.db")

    stats = compute_stats(store, repo="nothing-here")

    assert stats.task_count == 0
    assert stats.automation_rate is None
    assert stats.mean_human_interactions is None
    assert stats.total_cost_usd is None
    assert stats.mean_kickoff_to_finish_seconds is None


def test_automation_rate_is_the_fraction_with_zero_human_interactions(tmp_path):
    store = TelemetryStore(tmp_path / "jae.db")
    store.record(_record(session_id="a", human_interactions=0))
    store.record(_record(session_id="b", human_interactions=0))
    store.record(_record(session_id="c", human_interactions=2))

    stats = compute_stats(store, repo="repo-a")

    assert stats.task_count == 3
    assert stats.automation_rate == pytest.approx(2 / 3)


def test_mean_human_interactions_averages_across_tasks(tmp_path):
    store = TelemetryStore(tmp_path / "jae.db")
    store.record(_record(session_id="a", human_interactions=0))
    store.record(_record(session_id="b", human_interactions=4))

    stats = compute_stats(store, repo="repo-a")

    assert stats.mean_human_interactions == pytest.approx(2.0)


def test_total_cost_sums_only_tasks_with_a_known_cost(tmp_path):
    """A task with cost_usd=None (not yet wired to a real cost source, per
    telemetry.py's own known gap) is excluded from the sum, not treated
    as zero -- treating it as zero would understate a real cost.
    """
    store = TelemetryStore(tmp_path / "jae.db")
    store.record(_record(session_id="a", cost_usd=1.50))
    store.record(_record(session_id="b", cost_usd=None))
    store.record(_record(session_id="c", cost_usd=2.25))

    stats = compute_stats(store, repo="repo-a")

    assert stats.total_cost_usd == pytest.approx(3.75)


def test_total_cost_is_none_when_no_task_has_a_known_cost(tmp_path):
    store = TelemetryStore(tmp_path / "jae.db")
    store.record(_record(session_id="a", cost_usd=None))

    stats = compute_stats(store, repo="repo-a")

    assert stats.total_cost_usd is None


def test_mean_kickoff_to_finish_is_the_average_duration_in_seconds(tmp_path):
    store = TelemetryStore(tmp_path / "jae.db")
    started = datetime(2026, 9, 22, 10, 0, tzinfo=timezone.utc)
    store.record(_record(session_id="a", started_at=started, finished_at=started + timedelta(minutes=10)))
    store.record(_record(session_id="b", started_at=started, finished_at=started + timedelta(minutes=30)))

    stats = compute_stats(store, repo="repo-a")

    assert stats.mean_kickoff_to_finish_seconds == pytest.approx((600 + 1800) / 2)


# --- repo scoping vs pooled: the whole point of this component -------------


def test_repo_none_pools_every_repo(tmp_path):
    store = TelemetryStore(tmp_path / "jae.db")
    store.record(_record(repo="repo-a", session_id="a"))
    store.record(_record(repo="repo-b", session_id="b"))

    stats = compute_stats(store, repo=None)

    assert stats.task_count == 2
    assert stats.repo is None


def test_a_specific_repo_excludes_the_others(tmp_path):
    store = TelemetryStore(tmp_path / "jae.db")
    store.record(_record(repo="repo-a", session_id="a", human_interactions=0))
    store.record(_record(repo="repo-b", session_id="b", human_interactions=99))

    stats = compute_stats(store, repo="repo-a")

    assert stats.task_count == 1
    assert stats.mean_human_interactions == pytest.approx(0)


def test_per_repo_breakdown_returns_one_entry_per_distinct_repo(tmp_path):
    store = TelemetryStore(tmp_path / "jae.db")
    store.record(_record(repo="repo-a", session_id="a"))
    store.record(_record(repo="repo-a", session_id="b"))
    store.record(_record(repo="repo-b", session_id="c"))

    breakdown = per_repo_breakdown(store)

    assert {s.repo for s in breakdown} == {"repo-a", "repo-b"}
    by_repo = {s.repo: s for s in breakdown}
    assert by_repo["repo-a"].task_count == 2
    assert by_repo["repo-b"].task_count == 1


def test_per_repo_breakdown_is_empty_when_the_store_is_empty(tmp_path):
    store = TelemetryStore(tmp_path / "jae.db")

    assert per_repo_breakdown(store) == ()


# --- render_html: renders something readable, doesn't crash on empty -------


def test_render_html_includes_the_repo_name_and_task_count():
    stats = DashboardStats(
        repo="repo-a", task_count=5, automation_rate=0.6,
        mean_human_interactions=1.2, total_cost_usd=10.0,
        mean_kickoff_to_finish_seconds=1800.0,
    )

    html = render_html(overall=stats, breakdown=(stats,))

    assert "repo-a" in html
    assert "5" in html


def test_render_html_shows_a_placeholder_for_an_unknown_cost_not_zero():
    """total_cost_usd=None must not render as "0" or "$0.00" -- that would
    read as "this cost nothing," which is a claim this module cannot make.
    """
    stats = DashboardStats(
        repo="repo-a", task_count=1, automation_rate=1.0,
        mean_human_interactions=0.0, total_cost_usd=None,
        mean_kickoff_to_finish_seconds=60.0,
    )

    html = render_html(overall=stats, breakdown=(stats,))

    assert "$0.00" not in html
    assert "unknown" in html.lower() or "n/a" in html.lower() or "--" in html


def test_render_html_handles_a_pooled_overall_with_no_repo_name():
    stats = DashboardStats(
        repo=None, task_count=2, automation_rate=0.5,
        mean_human_interactions=1.0, total_cost_usd=5.0,
        mean_kickoff_to_finish_seconds=900.0,
    )

    html = render_html(overall=stats, breakdown=())

    assert "overall" in html.lower()


def test_render_html_does_not_crash_on_an_empty_store():
    empty = DashboardStats(
        repo=None, task_count=0, automation_rate=None,
        mean_human_interactions=None, total_cost_usd=None,
        mean_kickoff_to_finish_seconds=None,
    )

    html = render_html(overall=empty, breakdown=())

    assert "0" in html
