#!/usr/bin/env python3
"""Real GitHub PR data, via the `gh` CLI.

WHY `gh`, NOT A NEW HTTP CLIENT DEPENDENCY. Every other credential this
kit reads goes through an existing local tool already on the machine --
`git` for versioning, `security` for the Keychain. `gh` is the same shape
of dependency: it is not a new package this kit installs, it is a real
CLI the operator already authenticates once, outside this kit entirely
(`gh auth login`). Shelling out to it, with an injectable `runner` for
tests, keeps this module's own dependency list at zero.

WHAT THIS COMPUTES, AND WHY IT'S THE RIGHT SLICE. `factory_dashboard.py`'s
own module docstring names PR-to-first-human-review as a real, named gap
-- this module closes the GitHub half of it. `first_human_review_at`
returns the EARLIEST review timestamp, "human" here meaning "GitHub
recorded a submitted review" -- a bot reviewer would need its own
account-based filter the same way Jira comment counting does (see
factory_dashboard.py's sync_human_interactions), which is not built here;
most teams running review-required CI checks do not have a bot reviewer
posting formal PR reviews the way they might post PR comments.
"""

from __future__ import annotations

import json
import re
import subprocess
from datetime import datetime

__all__ = ["GitHubReadError", "first_human_review_at", "parse_pr_url"]

_PR_URL_RE = re.compile(r"^https://github\.com/([^/]+)/([^/]+)/pull/(\d+)$")


class GitHubReadError(RuntimeError):
    """A PR URL was not parseable, or `gh` refused or returned garbage."""


def parse_pr_url(pr_url: str) -> tuple[str, str, int]:
    """`https://github.com/owner/repo/pull/42` -> `("owner", "repo", 42)`."""
    match = _PR_URL_RE.match(pr_url.strip())
    if not match:
        raise GitHubReadError(f"not a GitHub PR URL: {pr_url!r}")
    owner, repo, number = match.groups()
    return owner, repo, int(number)


def first_human_review_at(pr_url: str, *, runner=subprocess.run) -> datetime | None:
    """The earliest `submitted_at` timestamp among this PR's reviews, or
    `None` if it has no reviews yet -- not an error, a PR that's simply
    still waiting is a real, valid state, not a failure to compute.

    `runner` defaults to the real `subprocess.run`; tests inject a fake
    one so nothing here ever shells out to a real `gh` process.
    """
    owner, repo, number = parse_pr_url(pr_url)
    result = runner(
        ["gh", "api", f"repos/{owner}/{repo}/pulls/{number}/reviews"],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        raise GitHubReadError(f"gh api failed for {pr_url}: {result.stderr.strip()}")

    try:
        reviews = json.loads(result.stdout)
    except json.JSONDecodeError as error:
        raise GitHubReadError(f"could not parse gh api output for {pr_url}: {error}") from error

    timestamps = [r["submitted_at"] for r in reviews if r.get("submitted_at")]
    if not timestamps:
        return None
    return min(datetime.fromisoformat(ts.replace("Z", "+00:00")) for ts in timestamps)
