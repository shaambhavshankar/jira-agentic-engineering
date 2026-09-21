#!/usr/bin/env python3
"""The Jev judge layer.

WHY THIS EXISTS. `jira-agent finish` reports a test exit code -- pass or
fail -- and nothing about the quality of what shipped. This module scores
a sample of finished tasks on three dimensions using TypeSafe's Jev
(docs.typesafe.ai), a typed-judgment API rather than a general-purpose
model asked to grade its own output in free text.

WHY THREE DIMENSIONS, WHY THESE THREE. `redundant_tests` and `scope_creep`
are generic to any repo. `prediction_accuracy` is specific to this kit's
own issue template: every issue created with `jira-agent create` already
requires a PREDICTED verdict on three axes (accuracy/scalability/
maintenance), and `finish` records the MEASURED verdict -- but nothing
compares the two. This dimension is what closes that loop.

WHY SAMPLING IS DETERMINISTIC, NOT RANDOM. `should_sample` hashes the
issue key. The same issue always samples the same way if scored twice
(e.g. once live, once during a §7 replay), which keeps a live-vs-replay
score comparison meaningful -- a random sampler could judge one run and
skip its replay, making the comparison silently incomplete.

WHY CONFIDENCE GATES THE WRITE, NOT JUST THE DISPLAY. Per
docs.typesafe.ai/confidence: act on high confidence, flag medium, refuse
low. A score below 0.5 is discarded outright here, stricter than Jev's own
"gather more context" suggestion for that band, because the consumer of
this data (a future self-improvement loop reading scored batches) needs a
clean sample -- training a pattern-finder on noisy low-confidence scores
finds patterns that are not there.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass

from typesafe_sdk import Score, TypeSafeClient

__all__ = [
    "DIMENSIONS",
    "DimensionSpec",
    "JevJudge",
    "JudgeResult",
    "confidence_action",
    "should_sample",
]


@dataclass(frozen=True)
class DimensionSpec:
    instructions: str
    criteria: tuple[str, ...]


DIMENSIONS: dict[str, DimensionSpec] = {
    "redundant_tests": DimensionSpec(
        instructions="Does this diff add tests that don't test new behaviour?",
        criteria=(
            "No redundant tests; every new test covers new behaviour",
            "A few tests likely redundant with existing coverage",
            "Most new tests are redundant; adds noise, not coverage",
        ),
    ),
    "scope_creep": DimensionSpec(
        instructions="Does this diff touch files unrelated to the stated problem?",
        criteria=(
            "No unrelated files touched",
            "Some unrelated files touched, and the description justifies it",
            "Unrelated files touched with no justification in the description",
        ),
    ),
    "prediction_accuracy": DimensionSpec(
        instructions=(
            "Does the PREDICTED verdict on accuracy/scalability/maintenance "
            "in this issue's description match the MEASURED verdict recorded "
            "when the work finished?"
        ),
        criteria=(
            "All three axes match",
            "One axis differs between predicted and measured",
            "Two or more axes differ between predicted and measured",
        ),
    ),
}


@dataclass(frozen=True)
class JudgeResult:
    dimension: str
    score: float
    confidence: float
    action: str  # "write" | "flag" | "discard"


def confidence_action(confidence: float) -> str:
    """Jev's own confidence-tier guidance, applied: >=0.9 act (write
    silently), 0.5-0.9 act cautiously (write, but flagged), <0.5 refuse
    (discard -- not written at all). See this module's docstring for why
    the <0.5 band is stricter here than Jev's own default suggestion.
    """
    if confidence >= 0.9:
        return "write"
    if confidence >= 0.5:
        return "flag"
    return "discard"


def should_sample(issue_key: str, sample_rate: float) -> bool:
    """Deterministic sampling: the same issue_key always gets the same
    answer for a given rate. Hash-based, not `random`, so a live score and
    a later replay score for the SAME issue are both taken or both
    skipped -- never one without the other.
    """
    digest = hashlib.sha256(issue_key.encode()).hexdigest()
    bucket = int(digest, 16) % 100
    return bucket < round(sample_rate * 100)


class JevJudge:
    """A thin wrapper over TypeSafeClient, scoped to this kit's dimensions.

    `api_key` and `transport` both pass straight through to
    `TypeSafeClient` -- `api_key=None` lets the SDK read `TYPESAFE_API_KEY`
    from the environment itself, and `transport` is how a test injects an
    `httpx2.MockTransport` instead of touching the real network.
    """

    def __init__(self, api_key: str | None = None, transport=None) -> None:
        self._client = TypeSafeClient(api_key=api_key, transport=transport)

    def score(self, dimension: str, state: str) -> JudgeResult:
        spec = DIMENSIONS.get(dimension)
        if spec is None:
            raise ValueError(
                f"unknown dimension {dimension!r}. Known dimensions: "
                f"{', '.join(sorted(DIMENSIONS))}"
            )

        response = self._client.system_one(
            state=state,
            questions={
                dimension: Score(instructions=spec.instructions, criteria=list(spec.criteria)),
            },
        )
        answer = response.answers[dimension]
        confidence = answer.confidence
        return JudgeResult(
            dimension=dimension,
            score=answer.score,
            confidence=confidence,
            action=confidence_action(confidence),
        )
