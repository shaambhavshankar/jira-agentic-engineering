#!/usr/bin/env python3
"""The manager dashboard: per-repo and overall, from one telemetry store.

WHY THIS EXISTS. Answers, for one repo or pooled across every repo in the
store: how automated is the factory, how many times did a human have to
step in, what did it cost, how long did work take. The pooled view is the
direct build-out of "multi-repo with learnings and judge for each
individual repo and overall as well."

WHAT `compute_stats` DOES NOT DO: hit the network. It reads only what is
already in TelemetryStore, on purpose -- the dashboard's main render path
must stay fast and offline. Two things it needs a live credential for --
who wrote each Jira comment, and when a PR's first human review landed --
are separate, opt-in sync functions below (`sync_human_interactions`,
`mean_pr_review_wait_seconds`), wired to a `--sync` flag on the CLI
rather than run on every render.

`sync_human_interactions` HAS A REAL LIMITATION IN THIS KIT'S OWN
DOGFOOD SETUP: it counts a comment as human by comparing its author
against the automation credential's own account id, and in this kit's
own Jira project, the automation token IS the human operator's personal
account -- there is no separate bot account. Every comment on a JAE issue
looks self-authored by that heuristic, so it will always compute 0
against this project's own telemetry. That is a true fact about how this
project's automation is set up, not a bug in the count -- a team running
a real bot/service account will get real, non-zero counts.
"""

from __future__ import annotations

import subprocess
from collections.abc import Sequence
from dataclasses import dataclass

from jira_agent_kit import github_reads
from jira_agent_kit.telemetry import TaskRecord, TelemetryStore

__all__ = [
    "DashboardStats", "compute_stats", "mean_pr_review_wait_seconds",
    "per_repo_breakdown", "render_html", "sync_human_interactions",
]


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


def sync_human_interactions(store: TelemetryStore, client, *, repo: str | None = None) -> int:
    """Count real non-bot Jira comments per task and write them back.

    "Non-bot" means "not the automation credential's own account id" --
    `client.myself()["accountId"]` is the one fact that lets a comment
    count be a real signal instead of a total that includes the
    automation's own status comments. Returns how many task rows were
    updated, so a caller can report a real number rather than assuming
    success.
    """
    bot_account_id = client.myself()["accountId"]
    tasks = store.tasks(repo=repo)
    for task in tasks:
        authors = client.list_comment_authors(task.issue_key)
        human_count = sum(1 for author in authors if author != bot_account_id)
        store.update_human_interactions(
            repo=task.repo, issue_key=task.issue_key,
            session_id=task.session_id, count=human_count,
        )
    return len(tasks)


def mean_pr_review_wait_seconds(
    tasks: Sequence[TaskRecord], *, runner=subprocess.run
) -> float | None:
    """Mean seconds from a task's `finished_at` to its PR's first human
    review, over every task with a `pr_url` that has been reviewed.

    `finished_at` stands in for "PR opened at" -- this kit does not record
    a separate PR-open timestamp, and `finish` is the moment a task's PR
    is realistically ready for review, so it's the honest anchor available
    rather than a fabricated one. A task with no `pr_url`, or a `pr_url`
    with no review yet, is excluded rather than counted as a zero wait --
    an unreviewed PR is not a fast review, it's an unmeasured one. `None`
    when nothing qualifies, same "unknown, not zero" convention as every
    other field here.
    """
    waits: list[float] = []
    for task in tasks:
        if task.pr_url is None:
            continue
        reviewed_at = github_reads.first_human_review_at(task.pr_url, runner=runner)
        if reviewed_at is None:
            continue
        waits.append((reviewed_at - task.finished_at).total_seconds())
    return sum(waits) / len(waits) if waits else None


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
        "<p>Human interactions and PR-to-first-human-review both need a "
        "network read (Jira comments, GitHub PR reviews) this table does "
        "not do on every render. Run <code>jira-agent factory-dashboard "
        "--sync</code> to refresh human-interaction counts first, or see "
        "factory_dashboard.py's module docstring for the PR-review-wait "
        "computation.</p>"
        "</body></html>"
    )
