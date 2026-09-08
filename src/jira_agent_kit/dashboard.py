#!/usr/bin/env python3
"""The 'where to look' dashboard: five saved filters, one dashboard, five gadgets.

WHY THIS EXISTS. A pattern seen on another team's Jira project answers "what
is waiting on me" at a glance, built entirely from saved filters and stock
filter-results gadgets -- no custom app. The mechanism, read directly from
that dashboard's own gadget configs and filter JQL rather than guessed: three
"waiting on you" filters, one per kind (decide/provide/go), each
`labels = <prefix>-needs-you AND labels = <prefix>-needs-<kind> AND
statusCategory != Done`; a "this sprint" filter (`sprint in openSprints()`);
and an open-bugs-by-priority filter.

Two gadgets from that original dashboard are NOT reproduced here, because
they depend on things this kit does not assume every team has: a
fixVersion-per-release process (a "deploy owed" filter needs one to mean
anything), and a big enough backlog for a 2D stats cross-tab to be useful
rather than sparse. Both are easy to add once you have positive answers to
"do we tie work to release versions?" and "is our backlog big enough to
matter?" -- see docs/SETUP.md.

THE WRITE SHAPES WERE VERIFIED LIVE, not read from documentation, when this
kit was first built: a scratch filter, dashboard, and gadget were created,
configured, read back, and deleted before any of this was written. In
particular: a gadget's config is PUT as the raw dict, not wrapped the way
the read response wraps it (see client.configure_gadget).
"""

from __future__ import annotations

from dataclasses import dataclass

from jira_agent_kit.config import Config
from jira_agent_kit.schema import Vocabulary

FILTER_RESULTS_GADGET_URI = (
    "rest/gadgets/1.0/g/com.atlassian.jira.gadgets:filter-results-gadget/"
    "gadgets/filter-results-gadget.xml"
)
COLUMN_NAMES = "issuetype|issuekey|summary|priority|labels|status"
NUM_ROWS = "15"


@dataclass(frozen=True)
class FilterSpec:
    key: str  # internal key a GadgetSpec references
    name: str  # the name Jira shows
    jql: str
    description: str


@dataclass(frozen=True)
class GadgetSpec:
    filter_key: str  # must match a FilterSpec.key
    title: str
    color: str
    row: int
    column: int


def plan(config: Config, vocab: Vocabulary) -> tuple[str, str, tuple[FilterSpec, ...], tuple[GadgetSpec, ...]]:
    """Return (dashboard_name, dashboard_description, filters, gadgets)."""
    p = config.project_key
    needs_you = vocab.needs_you

    def waiting_on_you(kind: str) -> str:
        return (
            f'project = {p} AND labels = "{needs_you}" '
            f'AND labels = "{vocab.needs_kind_label(kind)}" AND statusCategory != Done '
            "ORDER BY priority DESC, created"
        )

    filters = (
        FilterSpec(
            key="decide",
            name=f"{p} -- waiting on you: decide",
            jql=waiting_on_you("decide"),
            description="Needs a decision between real options.",
        ),
        FilterSpec(
            key="provide",
            name=f"{p} -- waiting on you: provide",
            jql=waiting_on_you("provide"),
            description="Needs an input or credential handed over.",
        ),
        FilterSpec(
            key="go",
            name=f"{p} -- waiting on you: go",
            jql=waiting_on_you("go"),
            description="Already decided. Needs a green light.",
        ),
        FilterSpec(
            key="sprint",
            name=f"{p} -- this sprint",
            jql=f"project = {p} AND sprint in openSprints() ORDER BY Rank",
            description="Work in the currently active sprint.",
        ),
        FilterSpec(
            key="bugs",
            name=f"{p} -- open bugs by priority",
            jql=(
                f"project = {p} AND issuetype = Bug AND statusCategory != Done "
                "ORDER BY priority DESC, created"
            ),
            description="All open defects, most severe first.",
        ),
    )

    gadgets = (
        GadgetSpec(filter_key="decide", title="Waiting on you -- decide", color="red", row=0, column=0),
        GadgetSpec(filter_key="provide", title="Waiting on you -- provide", color="yellow", row=0, column=1),
        GadgetSpec(filter_key="go", title="Waiting on you -- go", color="green", row=1, column=0),
        GadgetSpec(filter_key="sprint", title="This sprint", color="blue", row=1, column=1),
        GadgetSpec(filter_key="bugs", title="Open bugs by priority", color="red", row=2, column=0),
    )

    dashboard_name = f"{p} -- where to look"
    dashboard_description = (
        "What is waiting on you, by kind, plus this sprint's active work and "
        "the open bug list. Saved filters and stock gadgets, no custom app."
    )

    return dashboard_name, dashboard_description, filters, gadgets


def gadget_config(filter_id: str) -> dict:
    """The config dict every filter-results gadget needs, filled with one id."""
    return {
        "filterId": filter_id,
        "num": NUM_ROWS,
        "columnNames": COLUMN_NAMES,
        "refresh": "false",
        "isConfigured": "true",
    }
