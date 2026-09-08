"""Config fails loud, with the exact fix, rather than defaulting to nonsense."""

import pytest

from jira_agent_kit.config import ConfigError, load


def _clear(monkeypatch):
    for var in (
        "JIRA_AGENT_SITE", "JIRA_AGENT_PROJECT_KEY", "JIRA_AGENT_EMAIL",
        "JIRA_AGENT_LABEL_PREFIX", "JIRA_AGENT_KEYCHAIN_SERVICE",
    ):
        monkeypatch.delenv(var, raising=False)


def test_missing_everything_names_every_missing_variable(monkeypatch):
    _clear(monkeypatch)
    with pytest.raises(ConfigError) as caught:
        load()
    message = str(caught.value)
    for var in ("JIRA_AGENT_SITE", "JIRA_AGENT_PROJECT_KEY", "JIRA_AGENT_EMAIL", "JIRA_AGENT_LABEL_PREFIX"):
        assert var in message


def test_missing_one_variable_names_only_that_one(monkeypatch):
    _clear(monkeypatch)
    monkeypatch.setenv("JIRA_AGENT_SITE", "example.atlassian.net")
    monkeypatch.setenv("JIRA_AGENT_PROJECT_KEY", "PROJ")
    monkeypatch.setenv("JIRA_AGENT_EMAIL", "t@example.com")
    with pytest.raises(ConfigError) as caught:
        load()
    message = str(caught.value)
    assert "JIRA_AGENT_LABEL_PREFIX" in message
    assert "JIRA_AGENT_SITE" not in message


def test_a_fully_configured_environment_loads(monkeypatch):
    _clear(monkeypatch)
    monkeypatch.setenv("JIRA_AGENT_SITE", "example.atlassian.net")
    monkeypatch.setenv("JIRA_AGENT_PROJECT_KEY", "PROJ")
    monkeypatch.setenv("JIRA_AGENT_EMAIL", "t@example.com")
    monkeypatch.setenv("JIRA_AGENT_LABEL_PREFIX", "eng")
    config = load()
    assert config.site == "example.atlassian.net"
    assert config.project_key == "PROJ"
    assert config.label_prefix == "eng"
    assert config.keychain_service == "jira-agent-proj"  # derived default


def test_keychain_service_can_be_overridden(monkeypatch):
    _clear(monkeypatch)
    monkeypatch.setenv("JIRA_AGENT_SITE", "example.atlassian.net")
    monkeypatch.setenv("JIRA_AGENT_PROJECT_KEY", "PROJ")
    monkeypatch.setenv("JIRA_AGENT_EMAIL", "t@example.com")
    monkeypatch.setenv("JIRA_AGENT_LABEL_PREFIX", "eng")
    monkeypatch.setenv("JIRA_AGENT_KEYCHAIN_SERVICE", "my-own-name")
    assert load().keychain_service == "my-own-name"


def test_a_trailing_dash_on_the_prefix_is_stripped(monkeypatch):
    # So a user writing `JIRA_AGENT_LABEL_PREFIX=eng-` does not end up with
    # a doubled dash in every label this kit produces.
    _clear(monkeypatch)
    monkeypatch.setenv("JIRA_AGENT_SITE", "example.atlassian.net")
    monkeypatch.setenv("JIRA_AGENT_PROJECT_KEY", "PROJ")
    monkeypatch.setenv("JIRA_AGENT_EMAIL", "t@example.com")
    monkeypatch.setenv("JIRA_AGENT_LABEL_PREFIX", "eng-")
    assert load().label_prefix == "eng"


def test_issue_key_pattern_reflects_the_project_key(monkeypatch):
    _clear(monkeypatch)
    monkeypatch.setenv("JIRA_AGENT_SITE", "example.atlassian.net")
    monkeypatch.setenv("JIRA_AGENT_PROJECT_KEY", "PROJ")
    monkeypatch.setenv("JIRA_AGENT_EMAIL", "t@example.com")
    monkeypatch.setenv("JIRA_AGENT_LABEL_PREFIX", "eng")
    assert load().issue_key_pattern == r"PROJ-\d+"
