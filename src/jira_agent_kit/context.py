#!/usr/bin/env python3
"""Facts about the repo, gathered for a Jira comment.

WHY THIS EXISTS. A comment saying "tests pass" is worth nothing unless the
claim came from somewhere checkable. This module gathers the checkable
part -- which branch, which commit, which files, which exit code -- so the
comment quotes a measurement rather than a memory.

THE RULE THAT DOES NOT BEND. Pass and fail come from the EXIT CODE, never
from output text. A pipeline's exit status is its last stage, so
`pytest ... | tail` reports tail's success and `security ... | head` reports
head's. `read_test_result` therefore treats the code as authoritative and
the counts as decoration that may be absent.

WHAT IT DOES NOT DO. No network, no credentials. It shells out to `git` and
nothing else.
"""

from __future__ import annotations

import json
import re
import subprocess
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

__all__ = [
    "CONTRACT_MEANING",
    "ImpactResult",
    "ShellResult",
    "TestResult",
    "area_labels",
    "changed_files",
    "current_branch",
    "git",
    "head_sha",
    "issue_key_from",
    "make_issue_key_pattern",
    "read_test_result",
    "run_command",
    "run_impact",
]

CONTRACT_MEANING: dict[int, str] = {
    0: "pass -- no public signature broke",
    1: "contract break -- a signature a caller depends on changed",
    2: "could not run",
    3: "nothing to compare",
}

_COLLECTED_RE = re.compile(r"collected (\d+) item")
_PASSED_RE = re.compile(r"(\d+) passed")


def make_issue_key_pattern(project_key: str) -> re.Pattern[str]:
    """A word-bounded regex matching this project's issue keys.

    Word-bounded so `PROJ-4` does not also match inside `XPROJ-4` -- a
    lowercase or embedded near-miss is a different issue key, and silently
    accepting it would post a comment to the wrong one.
    """
    return re.compile(rf"\b{re.escape(project_key)}-\d+\b")


def issue_key_from(text: str, pattern: re.Pattern[str]) -> str | None:
    """The first matching issue key in the text, or None."""
    found = pattern.search(text)
    return found.group(0) if found else None


def _top_level(path: str) -> str:
    """The first path segment. No filesystem access, so it works on any string."""
    return path.replace("\\", "/").split("/", 1)[0]


def area_labels(paths: Sequence[str], prefix: str) -> tuple[str, ...]:
    """Area labels for these paths, sorted and deduplicated.

    Derived directly from each path's real top-level directory -- there is
    no allow-list to keep in sync with your repo's own layout. A label is
    only ever accurate, never guessed: whatever the top-level directory
    genuinely is becomes the label, and there is nothing to miss.
    """
    found = {f"{prefix}-area-{_top_level(path)}" for path in paths if _top_level(path)}
    return tuple(sorted(found))


@dataclass(frozen=True)
class TestResult:
    command: str
    exit_code: int
    collected: int | None
    passed: int | None

    @property
    def ok(self) -> bool:
        """The exit code, and only the exit code."""
        return self.exit_code == 0


def read_test_result(command: str, exit_code: int, output: str) -> TestResult:
    """Build a result whose verdict is the exit code.

    Counts are parsed if the summary happens to be there. Under a quiet
    flag, a test runner may print no summary at all in EITHER direction --
    no "N passed" on success and no "N failed" on failure -- so a check that
    reads the text can hang forever on a green run or pass a red one.
    Absent counts are None and change nothing about `ok`.
    """
    collected = _COLLECTED_RE.search(output)
    passed = _PASSED_RE.search(output)
    return TestResult(
        command=command,
        exit_code=exit_code,
        collected=int(collected.group(1)) if collected else None,
        passed=int(passed.group(1)) if passed else None,
    )


def git(args: Sequence[str], cwd: Path | str) -> str:
    """Run git and return stdout, stripped. Raises on a non-zero exit."""
    done = subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        check=True,
    )
    return done.stdout.strip()


def current_branch(cwd: Path | str) -> str:
    """The branch name, or an empty string when HEAD is detached.

    Detached HEAD makes `rev-parse --abbrev-ref` print the literal "HEAD",
    so that one exact value is the sentinel. Do NOT strip the substring
    instead: a branch legitimately containing "HEAD" in its name would lose
    part of it.
    """
    name = git(["rev-parse", "--abbrev-ref", "HEAD"], cwd)
    return "" if name == "HEAD" else name


def head_sha(cwd: Path | str) -> str:
    return git(["rev-parse", "HEAD"], cwd)


def changed_files(rev: str, cwd: Path | str) -> tuple[str, ...]:
    """Paths changed by `rev`, including merges and the root commit.

    `-m --first-parent` because a plain `git show --name-only` prints
    NOTHING for a merge commit -- every merge would otherwise report "no
    files changed", which is a fact about the command, not the commit.
    The trailing `--` ends the REVISION list and begins the path list, so
    it goes AFTER `rev`; putting it before makes git read the revision as a
    filename and return nothing for every call.
    """
    out = git(
        [
            "show",
            "--pretty=format:",
            "--name-only",
            "--no-renames",
            "-m",
            "--first-parent",
            rev,
            "--",
        ],
        cwd,
    )
    return tuple(line for line in out.splitlines() if line)


@dataclass(frozen=True)
class ImpactResult:
    """A blast-radius check's result, or the reason it could not be measured.

    Some blast-radius tools report failure through their EXIT CODE, and
    some report it through their OUTPUT while still exiting 0 -- if yours
    does the latter, `run_impact` below is where to read the body instead of
    trusting the exit status. Whichever your tool does, this shape is the
    same either way: `found=False` with `.error` set means "could not
    measure", never "measured zero risk".
    """

    found: bool
    risk: str | None
    impacted_count: int
    direct: int
    error: str | None

    def risk_label(self, prefix: str, risk_names: frozenset[str]) -> str | None:
        """The matching `{prefix}-{risk name}` label, or None.

        Only names present in `risk_names` map. Anything else -- an
        "UNKNOWN" result, or a value your tool might add later -- maps to
        None rather than being forced into a bucket: a guessed label is
        worse than a missing one, because it is wrong AND findable.
        """
        if not self.found or not self.risk:
            return None
        candidate = f"{prefix}-risk-{self.risk.lower()}"
        return candidate if candidate in risk_names else None


def run_impact(command: Sequence[str]) -> ImpactResult:
    """Run a blast-radius command and parse its JSON stdout.

    THIS ASSUMES YOUR TOOL PRINTS JSON TO STDOUT with `risk`, `impactedCount`,
    and `summary.direct` keys, and reports "target not found" via an
    `"error"` key rather than (or as well as) a non-zero exit code -- adapt
    the parsing below to whatever blast-radius tool you actually use.
    Never raises: a missing binary, a crash, or unparseable output all come
    back as `found=False` with `.error` set, so a caller can report the
    failure in one line rather than propagate a traceback.
    """
    try:
        done = subprocess.run(list(command), capture_output=True, text=True)
    except OSError as error:
        return ImpactResult(
            found=False, risk=None, impacted_count=0, direct=0,
            error=f"could not run {command[0]!r}: {error}",
        )

    try:
        payload = json.loads(done.stdout)
    except ValueError:
        detail = done.stderr.strip() or done.stdout.strip() or "no output"
        return ImpactResult(
            found=False, risk=None, impacted_count=0, direct=0,
            error=f"{command[0]} produced no parseable JSON: {detail[:300]}",
        )

    if "error" in payload:
        return ImpactResult(
            found=False, risk=payload.get("risk"), impacted_count=0, direct=0,
            error=payload["error"],
        )

    return ImpactResult(
        found=True,
        risk=payload.get("risk"),
        impacted_count=payload.get("impactedCount", 0),
        direct=payload.get("summary", {}).get("direct", 0),
        error=None,
    )


@dataclass(frozen=True)
class ShellResult:
    exit_code: int
    output: str


def run_command(command: Sequence[str], cwd: Path | str | None = None) -> ShellResult:
    """Run a command for real and capture its exit code and combined output.

    Stderr is merged into stdout: a caller building a test-result or a
    contract-check verdict from this needs both streams, and a message
    printed only to stderr must not silently vanish from the record.
    """
    done = subprocess.run(
        list(command),
        cwd=str(cwd) if cwd else None,
        capture_output=True,
        text=True,
    )
    return ShellResult(exit_code=done.returncode, output=done.stdout + done.stderr)
