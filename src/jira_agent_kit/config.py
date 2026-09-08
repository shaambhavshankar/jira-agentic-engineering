#!/usr/bin/env python3
"""Every company-specific value this kit needs, read from the environment.

WHY THIS EXISTS. The version this kit was extracted from had all of these
hardcoded: a real Jira site, a real project key, a real email, a real
Keychain service name. That works for exactly one company's exactly one
project. This module is the entire difference between "code for one Jira"
and "code for anyone's Jira" -- nothing below is optional, and nothing
guesses a default that would silently point at the wrong site.

FAIL LOUD, NOT QUIET. A missing required variable raises immediately, with
the exact `export` line to fix it, rather than falling through to `None`
and producing a confusing error three calls later inside an HTTP client.
"""

from __future__ import annotations

import os
from dataclasses import dataclass


class ConfigError(RuntimeError):
    """A required setting is missing, with the fix in the message."""


@dataclass(frozen=True)
class Config:
    site: str
    """Your Jira Cloud hostname, e.g. `yourcompany.atlassian.net`."""

    project_key: str
    """The Jira project this kit files issues into, e.g. `ENG`."""

    email: str
    """The Atlassian account email the API token belongs to.

    This is NOT necessarily the email your shell or CI runs under -- it is
    whichever Atlassian account owns the token. Getting this wrong produces
    a 401 that a generic "unauthenticated" message does not explain; see
    docs/SETUP.md for how this was discovered the hard way.
    """

    label_prefix: str
    """Every label this kit creates starts with this, e.g. `eng-`.

    Jira labels are one flat, global namespace shared by every project on
    your site. Without a prefix, a label like `needs-you` could collide with
    something an unrelated team already uses.
    """

    keychain_service: str
    """The macOS Keychain service name the API token is stored under."""

    @property
    def issue_key_pattern(self) -> str:
        """The regex fragment matching this project's issue keys, e.g. `ENG-\\d+`."""
        return rf"{self.project_key}-\d+"


def load() -> Config:
    """Read the five required settings from the environment, or raise.

    JIRA_AGENT_SITE, JIRA_AGENT_PROJECT_KEY, JIRA_AGENT_EMAIL,
    JIRA_AGENT_LABEL_PREFIX are always required. JIRA_AGENT_KEYCHAIN_SERVICE
    defaults to `jira-agent-<project_key, lowercased>` if unset -- a default
    here is safe because it only affects where THIS tool looks for its own
    token, not anything sent to Jira.
    """
    required = {
        "site": "JIRA_AGENT_SITE",
        "project_key": "JIRA_AGENT_PROJECT_KEY",
        "email": "JIRA_AGENT_EMAIL",
        "label_prefix": "JIRA_AGENT_LABEL_PREFIX",
    }
    values: dict[str, str] = {}
    missing: list[str] = []
    for field, var in required.items():
        value = os.environ.get(var, "").strip()
        if not value:
            missing.append(var)
        else:
            values[field] = value

    if missing:
        example_lines = "\n".join(
            f"  export {var}=..." for var in missing
        )
        raise ConfigError(
            "missing required configuration. Set:\n"
            f"{example_lines}\n"
            "See docs/SETUP.md for what each one means and how to find it."
        )

    project_key = values["project_key"]
    prefix = values["label_prefix"].rstrip("-")

    keychain_service = os.environ.get(
        "JIRA_AGENT_KEYCHAIN_SERVICE", f"jira-agent-{project_key.lower()}"
    ).strip()

    return Config(
        site=values["site"],
        project_key=project_key,
        email=values["email"],
        label_prefix=prefix,
        keychain_service=keychain_service,
    )
