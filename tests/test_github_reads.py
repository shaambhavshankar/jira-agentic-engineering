"""Real GitHub PR data, read via the `gh` CLI -- no new HTTP dependency,
matching the convention this whole kit already follows (shell out to a
real tool, `git` for versioning, `security` for the Keychain) rather than
adding another API client. Every test injects a fake `runner` so no test
ever shells out to a real `gh` process.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from jira_agent_kit.github_reads import (
    GitHubReadError,
    first_human_review_at,
    parse_pr_url,
)


class _FakeRunner:
    """Stands in for `subprocess.run`. Records the command, returns a
    canned (returncode, stdout, stderr).
    """

    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr
        self.calls = []

    def __call__(self, cmd, **kwargs):
        self.calls.append(cmd)
        return type("Result", (), {
            "returncode": self.returncode, "stdout": self.stdout, "stderr": self.stderr,
        })()


# --- parse_pr_url ------------------------------------------------------------


def test_parse_pr_url_extracts_owner_repo_number():
    owner, repo, number = parse_pr_url("https://github.com/acme/widgets/pull/42")
    assert (owner, repo, number) == ("acme", "widgets", 42)


def test_parse_pr_url_rejects_a_non_pr_url():
    with pytest.raises(GitHubReadError, match="not a GitHub PR URL"):
        parse_pr_url("https://github.com/acme/widgets/issues/42")


def test_parse_pr_url_rejects_a_non_github_url():
    with pytest.raises(GitHubReadError, match="not a GitHub PR URL"):
        parse_pr_url("https://gitlab.com/acme/widgets/-/merge_requests/42")


# --- first_human_review_at: real gh api shape --------------------------------


def _reviews_json(*reviews):
    return json.dumps(list(reviews))


def test_first_human_review_at_returns_the_earliest_review_timestamp():
    runner = _FakeRunner(stdout=_reviews_json(
        {"submitted_at": "2026-09-22T12:00:00Z", "user": {"login": "alice"}},
        {"submitted_at": "2026-09-22T10:00:00Z", "user": {"login": "bob"}},
    ))

    result = first_human_review_at("https://github.com/acme/widgets/pull/42", runner=runner)

    assert result == datetime(2026, 9, 22, 10, 0, 0, tzinfo=timezone.utc)


def test_first_human_review_at_returns_none_with_no_reviews_yet():
    runner = _FakeRunner(stdout=_reviews_json())

    result = first_human_review_at("https://github.com/acme/widgets/pull/42", runner=runner)

    assert result is None


def test_first_human_review_at_calls_the_expected_gh_api_path():
    runner = _FakeRunner(stdout=_reviews_json())

    first_human_review_at("https://github.com/acme/widgets/pull/42", runner=runner)

    assert runner.calls[0] == [
        "gh", "api", "repos/acme/widgets/pulls/42/reviews",
    ]


def test_first_human_review_at_raises_clearly_when_gh_fails():
    runner = _FakeRunner(returncode=1, stderr="HTTP 404: Not Found")

    with pytest.raises(GitHubReadError, match="404"):
        first_human_review_at("https://github.com/acme/widgets/pull/42", runner=runner)


def test_first_human_review_at_raises_clearly_on_malformed_json():
    runner = _FakeRunner(stdout="not json")

    with pytest.raises(GitHubReadError, match="could not parse"):
        first_human_review_at("https://github.com/acme/widgets/pull/42", runner=runner)
