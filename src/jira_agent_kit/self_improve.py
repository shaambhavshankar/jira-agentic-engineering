#!/usr/bin/env python3
"""The self-improvement loop -- mechanics only.

WHY THIS MODULE STOPS WHERE IT DOES. Warp's transcript describes an
observer agent that finds a failure pattern across a batch of poorly
scored runs and proposes -- in their telling, applies -- an edit to the
factory's own definition. This kit builds the mechanical half only:
selecting the batch (`bottom_third`) and building the fixed prompt an
observer agent should be run against (`build_observer_prompt`). Finding
the pattern and writing the prose that describes it is exactly the kind
of open-ended reasoning a Claude Code session does, not something a
synchronous Python function can do -- so this module does not call an
LLM at all. A human or agent runs `build_observer_prompt`'s output
through a real session, and whatever comes back goes through
`propose_issue`, which writes it as a REVIEWABLE Jira issue. Nothing here
ever edits a skill file. That is the spec's NG2, stated as code, not just
as a paragraph: an agent editing the instructions that govern every
future agent on a repo is the single highest-blast-radius change in this
whole system, and it gets a human in the loop every time, no exceptions
that erode over time.

WHY THE MINIMUM SAMPLE IS 20. Direct from the source transcript: "you
need like a real sample, like maybe 20, 25 failed runs... otherwise it
starts to overcorrect based on single things." Refused outright below
this, not a soft warning -- a pattern-finder run on 5 data points finds
a pattern in noise.

WHY PROPOSE_ISSUE USES EXISTING LABELS, NOT A NEW ONE. A tempting label
like "eng-self-improve" is not in the closed vocabulary (schema.py's own
rule: a label outside it is rejected before any request is sent, and
extending the vocabulary is its own deliberate, tested change -- not
something to smuggle in here). goal=cleanup + origin=by-agent are real,
existing categories that describe this kind of proposal honestly; the
summary text is what actually marks it as a self-improvement proposal.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from jira_agent_kit.schema import Axes, AxisVerdict, Option, Vocabulary, description_adf
from jira_agent_kit.telemetry import TelemetryStore

__all__ = [
    "MIN_SAMPLE",
    "ScoredTask",
    "SelfImproveError",
    "bottom_third",
    "build_observer_prompt",
    "propose_issue",
]

MIN_SAMPLE = 20


class SelfImproveError(RuntimeError):
    """Not enough scored data to find a real pattern, not noise."""


@dataclass(frozen=True)
class ScoredTask:
    repo: str
    issue_key: str
    session_id: str
    score: float
    confidence: float


def bottom_third(
    store: TelemetryStore,
    *,
    dimension: str,
    repo: str | None = None,
    min_sample: int = MIN_SAMPLE,
) -> tuple[ScoredTask, ...]:
    """The lowest-scoring third of recorded scores for `dimension`.

    `repo=None` pools across every repo in the store -- a pattern too
    small to clear `min_sample` in any one repo alone can still be real
    once volume is pooled (spec §6.1's direct payoff of a multi-repo
    store). Refuses below `min_sample`, naming the actual count, rather
    than silently running on too little data.
    """
    rows = store.scores(repo=repo, dimension=dimension)
    if len(rows) < min_sample:
        raise SelfImproveError(
            f"only {len(rows)} {dimension!r} scores recorded "
            f"(repo={repo or 'all'}); need at least {min_sample}. "
            "Fewer than this overcorrects on noise -- score more tasks first."
        )

    ordered = sorted(rows, key=lambda r: r["score"])
    cutoff = max(1, len(ordered) // 3)
    bottom = ordered[:cutoff]
    return tuple(
        ScoredTask(
            repo=r["repo"], issue_key=r["issue_key"], session_id=r["session_id"],
            score=r["score"], confidence=r["confidence"],
        )
        for r in bottom
    )


def build_observer_prompt(dimension: str, batch: Sequence[ScoredTask]) -> str:
    """The fixed prompt an observer agent (a real Claude Code session,
    not this function) should be run against. Deliberately rigid, not
    free-form: every self-improvement run asks the same question, so its
    outputs stay comparable to each other over time.
    """
    repos = sorted({t.repo for t in batch})
    lines = [
        f"Here are {len(batch)} tasks that scored poorly on {dimension!r}, "
        f"pulled from repos: {', '.join(repos)}.",
        "Find the common pattern. Propose a specific edit to skills/jira/SKILL.md "
        "or CLAUDE.md, per repo if the pattern doesn't generalise, that would "
        "have prevented it.",
        "Do not apply the edit.",
        "",
        "Tasks:",
    ]
    for task in batch:
        lines.append(
            f"- {task.repo} {task.issue_key} (session {task.session_id}): "
            f"score={task.score:.2f} confidence={task.confidence:.2f}"
        )
    return "\n".join(lines)


def propose_issue(
    client,
    *,
    project_key: str,
    vocabulary: Vocabulary,
    dimension: str,
    batch: Sequence[ScoredTask],
    proposal_text: str,
) -> str:
    """Write the observer agent's proposal as a Jira Task. Never applies
    anything to a file -- this issue IS the review artifact.
    """
    repos = sorted({t.repo for t in batch})
    summary = f"Self-improvement proposal: {dimension} ({', '.join(repos)})"
    labels = [
        f"{vocabulary.prefix}-backend",
        f"{vocabulary.prefix}-cleanup",
        f"{vocabulary.prefix}-by-agent",
    ]

    same = AxisVerdict("Same", "This is a proposal only; nothing has shipped yet.")
    description = description_adf(
        problem=(
            f"{len(batch)} tasks scored poorly on {dimension!r} across "
            f"{', '.join(repos)}. An observer agent found a pattern and "
            "proposed the fix below."
        ),
        options=[
            Option(
                name="Apply the proposed edit",
                good="Fixes the pattern this batch surfaced",
                bad="Not yet reviewed by a human -- this issue exists to get that review",
            ),
            Option(
                name="Leave the skill/hook as-is",
                good="No risk of a bad edit landing",
                bad="The pattern in this batch keeps recurring",
            ),
        ],
        chosen=proposal_text,
        axes=Axes(accuracy=same, scalability=same, maintenance=same),
        details_link=None,
    )

    issue_type_id = client.resolve_issue_type_id(project_key, "task")
    return client.create_issue(
        project_key=project_key,
        issue_type_id=issue_type_id,
        summary=summary,
        description_adf=description,
        labels=labels,
        vocabulary=vocabulary,
        parent_key=None,
        extra_fields=None,
    )
