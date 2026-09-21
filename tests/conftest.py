"""Four guards, all load-bearing.

1. `jira_live` tests are skipped unless JIRA_AGENT_LIVE_TESTS=1 is set.
2. No test in this package may reach the real Jira, EXCEPT one carrying
   @pytest.mark.jira_live.
3. `jev_live` tests are skipped unless JIRA_AGENT_LIVE_TESTS=1 is set.
4. No test in this package may reach the real Jev API, EXCEPT one carrying
   @pytest.mark.jev_live.

WHY 1 AND 2 EXIST. An early version of this kit had neither. One test called
the CLI's `create` command with an argument the vocabulary happened to
accept at the time, and it was not given a fake transport. Every time the
suite ran, that test read a real API token out of the Keychain and created
a real issue in a real company's Jira project. Five issues, five suite runs,
before anyone noticed. The fix was not "write a better test" -- it was
"make it structurally impossible for ANY test to reach the network by
accident", which is what the fixtures below do.

WHY 3 AND 4 EXIST, SEPARATELY FROM 1 AND 2. typesafe-sdk (the Jev client)
vendors its own httpx fork, imported as `httpx2` -- a completely different
module object from `httpx`. Patching `httpx.HTTPTransport` alone does
nothing to it: a Jev call would sail straight through the guard above and
hit the real network, and the reason would not be obvious from the
patched-httpx test failing to catch it. Both packages get their own guard,
built the same way, on purpose.
"""

import os

import httpx
import pytest

try:
    import httpx2
except ImportError:  # pragma: no cover -- typesafe-sdk not installed yet
    httpx2 = None

JIRA_LIVE_ENV = "JIRA_AGENT_LIVE_TESTS"


def pytest_collection_modifyitems(config, items):
    """Skip every `jira_live` and `jev_live` test unless the operator
    opted in by environment.

    NOT `-m "not jira_live"` in ini options. A `-o addopts=...` override on
    the command line clears a marker deselection along with whatever it was
    aimed at, so that guard is one habitual flag away from silently not
    applying. An env-var skip cannot be cleared by accident: it has to be
    set on purpose, every time.
    """
    if os.environ.get(JIRA_LIVE_ENV):
        return
    for marker_name, api_name in (("jira_live", "Jira"), ("jev_live", "Jev")):
        skip = pytest.mark.skip(
            reason=f"{marker_name} test: set {JIRA_LIVE_ENV}=1 to contact the real {api_name} API"
        )
        for item in items:
            if marker_name in item.keywords:
                item.add_marker(skip)


class LiveJiraAttempt(AssertionError):
    """A test tried to open a real connection to Jira, and was not marked jira_live."""


class LiveJevAttempt(AssertionError):
    """A test tried to open a real connection to Jev, and was not marked jev_live."""


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


@pytest.fixture(autouse=True)
def _no_live_jev(request, monkeypatch):
    if httpx2 is None or request.node.get_closest_marker("jev_live"):
        return

    def refuse(self, http_request, *args, **kwargs):
        raise LiveJevAttempt(
            "a test tried to reach the network: "
            f"{http_request.method} {http_request.url}\n"
            "Pass transport=httpx2.MockTransport(...) to TypeSafeClient, or "
            "mark the test @pytest.mark.jev_live if it is deliberately a "
            "live-API test."
        )

    monkeypatch.setattr(httpx2.HTTPTransport, "handle_request", refuse)
