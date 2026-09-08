"""Real calls to a real Jira project. Skipped unless JIRA_AGENT_LIVE_TESTS=1.

    JIRA_AGENT_LIVE_TESTS=1 \\
    JIRA_AGENT_SITE=yourcompany.atlassian.net \\
    JIRA_AGENT_PROJECT_KEY=PROJ \\
    JIRA_AGENT_EMAIL=you@yourcompany.com \\
    JIRA_AGENT_LABEL_PREFIX=eng \\
    pytest tests/test_live.py -v

WHY THIS FILE IS SAFE TO RUN. Three separate things stand between "opt in"
and creating unwanted issues in a real project. The `jira_live` marker plus
JIRA_AGENT_LIVE_TESTS gate whether the test runs at all (see conftest.py).
The narrow exception in conftest.py's network guard is the ONLY thing that
permits a real transport, and only for a test carrying the marker. And every
issue this file creates is deleted in a `finally`, not left for a human to
notice and clean up by hand.

If a delete fails, the test FAILS LOUDLY rather than swallowing it. A
leaked live-test issue in your Jira project is the incident this file
exists to prevent, and you need to know if one happened.

RUN THIS AGAINST A PROJECT YOU ARE OKAY CREATING AND DELETING TEST ISSUES
IN. It creates real issues, even though it deletes them straight after.
"""

import pytest

from jira_agent_kit.client import JiraClient, JiraError, read_token
from jira_agent_kit.config import load as load_config
from jira_agent_kit.schema import Axes, AxisVerdict, Option, Vocabulary, description_adf, text_adf

pytestmark = pytest.mark.jira_live

_SUMMARY_PREFIX = "LIVE TEST -- created and auto-deleted by tests/test_live.py"


@pytest.fixture
def config():
    return load_config()


@pytest.fixture
def vocab(config):
    return Vocabulary(config.label_prefix)


@pytest.fixture
def live_client(config):
    token = read_token(service=config.keychain_service, account=config.email)
    return JiraClient(site=config.site, email=config.email, token=token)


@pytest.fixture
def throwaway_issue(live_client, config, vocab):
    """Create one real Task, yield its key, delete it no matter what happens."""
    task_type = live_client.resolve_issue_type_id(config.project_key, "Task")
    key = live_client.create_issue(
        project_key=config.project_key,
        issue_type_id=task_type,
        summary=_SUMMARY_PREFIX,
        description_adf=description_adf(
            problem="Nothing. This issue exists only to be read back and deleted.",
            options=[
                Option("Create and delete automatically", "Proves the round trip.", "Needs live credentials."),
                Option("Never test against real Jira", "No live traffic.", "A fake transport cannot prove Jira accepted the shape."),
            ],
            chosen="Option 1, deleted in a finally block regardless of outcome.",
            axes=Axes(
                accuracy=AxisVerdict("Same", "A test fixture, not a product change."),
                scalability=AxisVerdict("Same", "One issue, created and deleted."),
                maintenance=AxisVerdict("Same", "No lasting state."),
            ),
            details_link=None,
        ),
        labels=[f"{vocab.prefix}-backend", f"{vocab.prefix}-cleanup", f"{vocab.prefix}-by-agent"],
        vocabulary=vocab,
    )
    try:
        yield key
    finally:
        live_client.delete_issue(key)


def test_whoami_authenticates_as_the_configured_account(live_client, config):
    me = live_client.myself()
    assert me["emailAddress"] == config.email
    assert me["active"] is True


def test_a_wrong_token_is_rejected_with_401(config):
    bad = JiraClient(site=config.site, email=config.email, token="not-a-real-token")
    with pytest.raises(JiraError) as caught:
        bad.myself()
    assert "401" in str(caught.value)


def test_resolve_issue_type_id_finds_a_real_task_type(live_client, config):
    task_id = live_client.resolve_issue_type_id(config.project_key, "Task")
    assert task_id


def test_create_read_comment_label_and_transition_round_trip(live_client, throwaway_issue):
    key = throwaway_issue

    issue = live_client.get_issue(key)
    assert issue["fields"]["summary"] == _SUMMARY_PREFIX

    live_client.add_comment(key, text_adf("A comment posted by the live test."))
    comments = live_client.list_comments(key, limit=3)
    assert any("comment posted by the live test" in body for body in comments)

    transitions = live_client.list_transitions(key)
    done_name = next((n for n in transitions if n.lower() == "done"), None)
    if done_name is None:
        pytest.skip(f"no 'Done' transition on this workflow: {sorted(transitions)}")
    live_client.transition(key, transitions[done_name])
    after = live_client.get_issue(key)
    assert after["fields"]["status"]["name"].lower() == "done"


def test_add_labels_rejects_an_unknown_label_before_any_request(live_client, throwaway_issue, vocab):
    from jira_agent_kit.schema import LabelError

    with pytest.raises(LabelError):
        live_client.add_labels(throwaway_issue, [f"{vocab.prefix}-not-a-real-label"], vocab)
