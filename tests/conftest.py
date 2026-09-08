"""Two guards, both load-bearing.

1. `jira_live` tests are skipped unless JIRA_AGENT_LIVE_TESTS=1 is set.
2. No test in this package may reach the real Jira, EXCEPT one carrying
   @pytest.mark.jira_live.

WHY BOTH EXIST. An early version of this kit had neither. One test called
the CLI's `create` command with an argument the vocabulary happened to
accept at the time, and it was not given a fake transport. Every time the
suite ran, that test read a real API token out of the Keychain and created
a real issue in a real company's Jira project. Five issues, five suite runs,
before anyone noticed. The fix was not "write a better test" -- it was
"make it structurally impossible for ANY test to reach the network by
accident", which is what the fixture below does.
"""

import os

import httpx
import pytest

JIRA_LIVE_ENV = "JIRA_AGENT_LIVE_TESTS"


def pytest_collection_modifyitems(config, items):
    """Skip every `jira_live` test unless the operator opted in by
    environment.

    NOT `-m "not jira_live"` in ini options. A `-o addopts=...` override on
    the command line clears a marker deselection along with whatever it was
    aimed at, so that guard is one habitual flag away from silently not
    applying. An env-var skip cannot be cleared by accident: it has to be
    set on purpose, every time.
    """
    if os.environ.get(JIRA_LIVE_ENV):
        return
    skip = pytest.mark.skip(
        reason=f"jira_live test: set {JIRA_LIVE_ENV}=1 to contact the real Jira API"
    )
    for item in items:
        if "jira_live" in item.keywords:
            item.add_marker(skip)


class LiveJiraAttempt(AssertionError):
    """A test tried to open a real connection, and was not marked jira_live."""


@pytest.fixture(autouse=True)
def _no_live_jira(request, monkeypatch):
    if request.node.get_closest_marker("jira_live"):
        return

    def refuse(self, http_request, *args, **kwargs):
        raise LiveJiraAttempt(
            "a test tried to reach the network: "
            f"{http_request.method} {http_request.url}\n"
            "Pass transport=httpx.MockTransport(...) to JiraClient, or mark "
            "the test @pytest.mark.jira_live if it is deliberately a "
            "live-API test."
        )

    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", refuse)
