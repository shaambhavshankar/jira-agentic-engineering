#!/usr/bin/env python3
"""Factory versioning and replay.

WHY THIS EXISTS. The factory's definition -- skills, hooks -- is already
versioned by git, but nothing names a snapshot a replay can reference, and
nothing compares one task's score under two different versions. This
closes that gap using two primitives already built for other JAE items:
git tags (nothing new) and the `scores` table's `factory_version` column
(already present since telemetry.py's first version, precisely so this
item would not need a schema change).

WHAT THIS DOES NOT DO. It does not re-run an agent under an old factory
version automatically. Actually redoing a task under a tagged skill
version -- checking the tag out, running the agent again -- is a human or
agent action this kit does not orchestrate; that's real engineering work,
not a mechanical replay. What IS mechanical, and is what this module
provides, is the comparison: tag a version (`tag_factory_version`), score
a task under it (`score_under_version`, a thin wrapper making the tag
explicit), and diff two tagged scores for the same task
(`replay_delta`). A `factory_version=None` score is the live run; a named
tag is a replay.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path

from jira_agent_kit.telemetry import TelemetryStore

__all__ = [
    "ReplayDelta",
    "TagInfo",
    "VersioningError",
    "list_factory_versions",
    "replay_delta",
    "score_under_version",
    "tag_factory_version",
]


class VersioningError(RuntimeError):
    """A version tag or a score this operation needs does not exist."""


@dataclass(frozen=True)
class TagInfo:
    tag: str
    sha: str
    note: str


def tag_factory_version(name: str, *, note: str, repo_path: Path | str = ".") -> TagInfo:
    """Create an annotated git tag `factory-{name}` at HEAD.

    The mechanism Warp's transcript called "freezing the state of the
    factory at a given point," described in product language: this is
    plain git tagging, nothing more exotic. Raises VersioningError if the
    tag already exists -- silently overwriting a version tag would corrupt
    any past replay comparison that already used it.
    """
    tag = f"factory-{name}"
    existing = subprocess.run(
        ["git", "tag", "-l", tag], cwd=str(repo_path),
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    if existing:
        raise VersioningError(f"tag {tag!r} already exists. Pick a new name.")

    subprocess.run(
        ["git", "tag", "-a", tag, "-m", note], cwd=str(repo_path), check=True,
    )
    sha = subprocess.run(
        ["git", "rev-parse", tag], cwd=str(repo_path),
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    return TagInfo(tag=tag, sha=sha, note=note)


def list_factory_versions(repo_path: Path | str = ".") -> tuple[TagInfo, ...]:
    """Every `factory-*` tag in this repo, with its annotation note.

    Uses `git for-each-ref`, not `git tag -n99`. `-n99` prints a
    CONTINUATION line for every extra line of a multi-line annotation --
    indented, with no tag name -- and a naive per-line parser that tries
    `line.partition(" ")` on one of those reads an empty tag name (a line
    starting with whitespace partitions to "" at the very first space),
    which then crashed `git rev-parse ''`. Real bug, found running this
    against `factory-v1`'s own multi-line note. `for-each-ref` with
    `%(contents:subject)` gives exactly one line per tag -- the note is
    truncated to its first line here as a result; the full message is
    still on the tag itself, via `git show <tag>`, for anyone who needs it.
    """
    out = subprocess.run(
        [
            "git", "for-each-ref",
            "--format=%(refname:short)|%(objectname)|%(contents:subject)",
            "refs/tags/factory-*",
        ],
        cwd=str(repo_path), capture_output=True, text=True, check=True,
    ).stdout
    versions = []
    for line in out.splitlines():
        if not line.strip():
            continue
        tag, sha, note = line.split("|", 2)
        versions.append(TagInfo(tag=tag, sha=sha, note=note))
    return tuple(versions)


def score_under_version(
    judge,
    store: TelemetryStore,
    *,
    repo: str,
    issue_key: str,
    session_id: str,
    dimension: str,
    state: str,
    factory_version: str,
) -> None:
    """Score `state` on `dimension`, tagged with `factory_version`.

    A thin wrapper over `judge.score` + `store.record_score` -- exists so
    a replay's write path is explicit and testable on its own, rather than
    duplicated inline wherever a replay is run. Same confidence-gated
    discard rule as a live score (judge.py's confidence_action): a
    low-confidence replay score is not evidence either.
    """
    result = judge.score(dimension, state)
    if result.action == "discard":
        return
    store.record_score(
        repo=repo, issue_key=issue_key, session_id=session_id,
        dimension=result.dimension, score=result.score,
        confidence=result.confidence, factory_version=factory_version,
    )


@dataclass(frozen=True)
class ReplayDelta:
    dimension: str
    from_version: str | None
    to_version: str | None
    from_score: float
    to_score: float
    delta: float


def replay_delta(
    store: TelemetryStore,
    *,
    repo: str,
    issue_key: str,
    session_id: str,
    dimension: str,
    from_version: str | None,
    to_version: str | None,
) -> ReplayDelta:
    """Compare a task's score under two factory versions.

    `None` means the live run (no tag). Raises VersioningError, naming
    which side is missing, if either score has not been recorded yet --
    answering "was it better" with half the data would be a guess wearing
    a number's clothes.
    """
    rows = store.scores(repo=repo, dimension=dimension)
    rows = [r for r in rows if r["issue_key"] == issue_key and r["session_id"] == session_id]

    def _find(version: str | None) -> dict | None:
        return next((r for r in rows if r["factory_version"] == version), None)

    from_row = _find(from_version)
    to_row = _find(to_version)

    if from_row is None:
        label = from_version or "live"
        raise VersioningError(
            f"no {dimension} score recorded for {issue_key} under {label!r}. "
            "Score it first, then replay."
        )
    if to_row is None:
        label = to_version or "live"
        raise VersioningError(
            f"no {dimension} score recorded for {issue_key} under {label!r}. "
            "Score it first, then replay."
        )

    return ReplayDelta(
        dimension=dimension,
        from_version=from_version,
        to_version=to_version,
        from_score=from_row["score"],
        to_score=to_row["score"],
        delta=to_row["score"] - from_row["score"],
    )
