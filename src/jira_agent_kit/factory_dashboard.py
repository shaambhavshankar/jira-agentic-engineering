#!/usr/bin/env python3
"""The manager dashboard: per-repo and overall, from one telemetry store.

WHY THIS EXISTS. Answers, for one repo or pooled across every repo in the
store: how automated is the factory, how many times did a human have to
step in, what did it cost, how long did work take. The pooled view is the
direct build-out of "multi-repo with learnings and judge for each
individual repo and overall as well."

WHAT THIS DOES NOT COMPUTE, ON PURPOSE. The spec's original draft (§4.1)
described splitting cycle time into "kickoff-to-PR" and "PR-to-first-
human-review." The second half needs Jira comment timestamps and GitHub PR
review timestamps -- neither is in TelemetryStore yet, and neither has a
read integration built in this item. Reporting a number for it anyway
would be a fabrication, exactly what docs/verifying-your-own-work.md
warns against. This module reports `mean_kickoff_to_finish_seconds`
instead -- the true duration this kit DOES measure (started_at to
finished_at, per TaskRecord) -- and leaves the human-review split as a
named gap for whichever item wires in the Jira/GitHub reads. A missing
number here is honest; a wrong one is worse than no dashboard at all.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from jira_agent_kit.telemetry import TelemetryStore

__all__ = ["DashboardStats", "compute_stats", "per_repo_breakdown", "render_html"]


@dataclass(frozen=True)
class DashboardStats:
    """One repo's numbers, or the pooled numbers when `repo` is None.

    Every rate/mean field is `None`, not `0` or `0.0`, when there is
    nothing to compute it from -- a repo with zero recorded tasks has an
    UNKNOWN automation rate, not a zero automation rate, and those read
    very differently to a manager glancing at this page.
    """

    repo: str | None
    task_count: int
    automation_rate: float | None
    mean_human_interactions: float | None
    total_cost_usd: float | None
    mean_kickoff_to_finish_seconds: float | None


def compute_stats(store: TelemetryStore, repo: str | None = None) -> DashboardStats:
    """The four numbers for one repo, or pooled across all of them when
    `repo` is None.
    """
    tasks = store.tasks(repo=repo)
    if not tasks:
        return DashboardStats(
            repo=repo, task_count=0, automation_rate=None,
            mean_human_interactions=None, total_cost_usd=None,
            mean_kickoff_to_finish_seconds=None,
        )

    automated = sum(1 for t in tasks if t.human_interactions == 0)
    known_costs = [t.cost_usd for t in tasks if t.cost_usd is not None]
    durations = [(t.finished_at - t.started_at).total_seconds() for t in tasks]

    return DashboardStats(
        repo=repo,
        task_count=len(tasks),
        automation_rate=automated / len(tasks),
        mean_human_interactions=sum(t.human_interactions for t in tasks) / len(tasks),
        total_cost_usd=sum(known_costs) if known_costs else None,
        mean_kickoff_to_finish_seconds=sum(durations) / len(durations),
    )


def per_repo_breakdown(store: TelemetryStore) -> tuple[DashboardStats, ...]:
    """One DashboardStats per distinct repo that has at least one task."""
    repos = sorted({task.repo for task in store.tasks()})
    return tuple(compute_stats(store, repo=r) for r in repos)


def _fmt_rate(value: float | None) -> str:
    return f"{value:.0%}" if value is not None else "unknown"


def _fmt_count(value: float | None) -> str:
    return f"{value:.1f}" if value is not None else "unknown"


def _fmt_cost(value: float | None) -> str:
    return f"${value:.2f}" if value is not None else "unknown"


def _fmt_duration(value: float | None) -> str:
    if value is None:
        return "unknown"
    minutes = value / 60
    return f"{minutes:.0f} min"


def _row(label: str, stats: DashboardStats) -> str:
    return (
        "<tr>"
        f"<td>{label}</td>"
        f"<td>{stats.task_count}</td>"
        f"<td>{_fmt_rate(stats.automation_rate)}</td>"
        f"<td>{_fmt_count(stats.mean_human_interactions)}</td>"
        f"<td>{_fmt_cost(stats.total_cost_usd)}</td>"
        f"<td>{_fmt_duration(stats.mean_kickoff_to_finish_seconds)}</td>"
        "</tr>"
    )


def render_html(overall: DashboardStats, breakdown: Sequence[DashboardStats]) -> str:
    """One static HTML page: the pooled row, then one row per repo.

    Static, not a hosted service, for the same reason the store itself
    is a local SQLite file and not a server -- nothing in this kit runs a
    daemon, and this dashboard should not be the first thing that does.
    """
    rows = [_row(overall.repo or "overall", overall)]
    rows += [_row(s.repo or "(unknown)", s) for s in breakdown]

    return (
        "<!doctype html><html><head><meta charset=\"utf-8\">"
        "<title>JAE factory dashboard</title></head><body>"
        "<h1>JAE factory dashboard</h1>"
        "<table border=\"1\" cellpadding=\"6\">"
        "<thead><tr>"
        "<th>repo</th><th>tasks</th><th>automation rate</th>"
        "<th>mean human interactions</th><th>total cost</th>"
        "<th>mean kickoff-to-finish</th>"
        "</tr></thead>"
        f"<tbody>{''.join(rows)}</tbody>"
        "</table>"
        "<p>PR-to-first-human-review is not shown: it needs Jira comment "
        "and GitHub PR review timestamps this item does not read yet. "
        "See factory_dashboard.py's module docstring.</p>"
        "</body></html>"
    )
