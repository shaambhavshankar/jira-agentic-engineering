#!/usr/bin/env python3
"""The label vocabulary, the issue template, and the plain-language lint.

WHY THIS EXISTS. Jira labels are one flat, global, unvalidated namespace
shared by every project on your site. Any string is accepted, so a typo does
not error -- `eng-fronted` silently becomes a second bucket that never merges
back into `eng-frontend`, and a board that has drifted into synonyms cannot
be queried. Jira will not enforce a vocabulary, so this module does.

WHAT IT DOES NOT DO. It never touches the network and never reads a
credential; that is `client.py`. It cannot tell whether a description is
TRUE -- only whether it has the required parts and reads plainly.

EVERY LABEL IS PARAMETERISED BY A PREFIX (`Vocabulary(prefix=...)`), because
this file is meant to be used by more than one company on more than one
Jira site, and labels are the one thing certain to collide across them if
they are not namespaced.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass


class LabelError(ValueError):
    """A label set that Jira would accept and a reader could not trust."""


# WHERE the work is. Not company-specific, but you may want to trim or
# rename these to match your own stack.
LAYER_NAMES: frozenset[str] = frozenset(
    {"frontend", "backend", "data", "infra"}
)

# WHY it is being done. Separate from LAYERS on purpose: a dashboard speed
# fix is honestly both frontend AND perf; one combined list would force a
# false choice on every issue that is genuinely both.
GOAL_NAMES: frozenset[str] = frozenset(
    {"feature", "perf", "accuracy", "cleanup", "docs"}
)

ORIGIN_NAMES: frozenset[str] = frozenset({"by-agent", "by-human"})

RISK_NAMES: frozenset[str] = frozenset({"risk-low", "risk-med", "risk-high", "risk-critical"})

HURT_NAMES: frozenset[str] = frozenset(
    {"hurts-accuracy", "hurts-scale", "hurts-maintenance"}
)

# Three DIFFERENT asks, not one flat "blocked". "decide" needs a choice
# between real options, "provide" needs an input or credential handed over,
# "go" is already decided and only needs a green light. Collapsing these
# into one label hides which kind of ask it is from whoever is triaging.
NEEDS_KIND_NAMES: frozenset[str] = frozenset({"needs-decide", "needs-provide", "needs-go"})

NEEDS_YOU_NAME: str = "needs-you"

VERDICTS: frozenset[str] = frozenset({"Better", "Same", "Worse"})

REQUIRED_SECTIONS: tuple[str, ...] = (
    "## What is broken, or what is missing",
    "## Ways to fix it",
    "## What this does to the three things that matter",
)


@dataclass(frozen=True)
class Vocabulary:
    """The label vocabulary for one prefix, e.g. `Vocabulary("eng")`.

    Area labels are NOT a closed allow-list. An early draft of this kit
    tried a fixed allow-list of top-level directory names -- meaningless to
    anyone whose repo is laid out differently. Instead, an area label is
    valid for ANY top-level directory name: `is_area_label` checks the
    PREFIX pattern, not a fixed set, and `area_label()` derives the label
    directly from a real path. A path outside your repo contributes no
    label rather than a guessed one; a path inside it always gets an
    accurate one.
    """

    prefix: str

    @property
    def layers(self) -> frozenset[str]:
        return frozenset(f"{self.prefix}-{n}" for n in LAYER_NAMES)

    @property
    def goals(self) -> frozenset[str]:
        return frozenset(f"{self.prefix}-{n}" for n in GOAL_NAMES)

    @property
    def origins(self) -> frozenset[str]:
        return frozenset(f"{self.prefix}-{n}" for n in ORIGIN_NAMES)

    @property
    def risks(self) -> frozenset[str]:
        return frozenset(f"{self.prefix}-{n}" for n in RISK_NAMES)

    @property
    def hurts(self) -> frozenset[str]:
        return frozenset(f"{self.prefix}-{n}" for n in HURT_NAMES)

    @property
    def needs_kinds(self) -> frozenset[str]:
        return frozenset(f"{self.prefix}-{n}" for n in NEEDS_KIND_NAMES)

    @property
    def needs_you(self) -> str:
        return f"{self.prefix}-{NEEDS_YOU_NAME}"

    def area_label(self, top_level_dir: str) -> str:
        return f"{self.prefix}-area-{top_level_dir}"

    def is_area_label(self, label: str) -> bool:
        return label.startswith(f"{self.prefix}-area-")

    def hurt_label(self, name: str) -> str:
        return f"{self.prefix}-hurts-{name}"

    def needs_kind_label(self, kind: str) -> str:
        return f"{self.prefix}-needs-{kind}"

    def _is_known(self, label: str) -> bool:
        return (
            label in self.layers
            or label in self.goals
            or label in self.origins
            or label in self.risks
            or label in self.hurts
            or label in self.needs_kinds
            or label == self.needs_you
            or self.is_area_label(label)
        )

    def validate_label_names(self, labels: Sequence[str]) -> tuple[str, ...]:
        """Check vocabulary membership only, for labels ADDED to an existing issue.

        `validate_labels` also enforces cardinality ("exactly one layer"),
        which is right when creating an issue and wrong when adding to one:
        the layer is already on the issue and is not part of this call.
        Membership still applies, because a typo creates a second bucket
        either way.
        """
        unique = set(labels)
        unknown = sorted(name for name in unique if not self._is_known(name))
        if unknown:
            raise LabelError(
                "not in the vocabulary: "
                + ", ".join(unknown)
                + ". Add it to schema.py or fix the spelling."
            )
        return tuple(sorted(unique))

    def validate_labels(self, labels: Sequence[str]) -> tuple[str, ...]:
        """Return the labels sorted and deduplicated, or raise LabelError.

        Rules: every label is in the vocabulary; exactly one layer, one
        goal, one origin; at most one risk and one needs-kind. Area and
        hurt labels may repeat freely -- an issue can honestly touch
        several areas and regress on several axes at once.
        """
        unique = set(labels)

        unknown = sorted(name for name in unique if not self._is_known(name))
        if unknown:
            raise LabelError(
                "not in the vocabulary: "
                + ", ".join(unknown)
                + ". Add it to schema.py or fix the spelling."
            )

        for name, group, low, high in (
            ("layer", self.layers, 1, 1),
            ("goal", self.goals, 1, 1),
            ("origin", self.origins, 1, 1),
            ("risk", self.risks, 0, 1),
            ("needs kind", self.needs_kinds, 0, 1),
        ):
            found = sorted(unique & group)
            if not (low <= len(found) <= high):
                want = f"exactly {high}" if low == high else f"at most {high}"
                raise LabelError(
                    f"{name}: want {want}, got {len(found)}"
                    + (f" ({', '.join(found)})" if found else "")
                )

        return tuple(sorted(unique))


@dataclass(frozen=True)
class AxisVerdict:
    """One word and one sentence. The word is what makes it queryable."""

    verdict: str
    sentence: str

    def __post_init__(self) -> None:
        if self.verdict not in VERDICTS:
            raise LabelError(
                f"verdict must be one of {sorted(VERDICTS)}, got "
                f"{self.verdict!r}. A hedge is not a verdict."
            )


@dataclass(frozen=True)
class Axes:
    """The three things every product decision is prioritised against.

    Rename these three if your own team's priorities differ -- what matters
    is that every issue states a Better/Same/Worse verdict on a small,
    FIXED set of axes, predicted before work starts and measured again when
    it is done. The gap between the two predictions is the finding.
    """

    accuracy: AxisVerdict
    scalability: AxisVerdict
    maintenance: AxisVerdict

    def hurt_labels(self, vocab: Vocabulary) -> tuple[str, ...]:
        """Labels for the axes that came out Worse, so a regression is findable.

        Only Worse produces a label. Better and Same produce none: a label
        on every verdict would put three extra labels on every issue and
        drown the signal this exists to carry.
        """
        pairs = (
            (self.accuracy, "accuracy"),
            (self.scalability, "scale"),
            (self.maintenance, "maintenance"),
        )
        return tuple(
            vocab.hurt_label(name) for axis, name in pairs if axis.verdict == "Worse"
        )


@dataclass(frozen=True)
class Option:
    name: str
    good: str
    bad: str


def render_description(
    problem: str,
    options: Sequence[Option],
    chosen: str,
    axes: Axes,
    details_link: str | None,
) -> str:
    """Render the issue description as markdown, for humans and for the lint.

    Two options minimum. If a fix is genuinely forced, say WHY in `chosen`
    and pass the forced alternative as the second option -- a lone option
    with no stated alternative is how a decision gets made without anyone
    noticing a decision was made.
    """
    if len(options) < 2:
        raise LabelError(
            "a description needs at least two options; got "
            f"{len(options)}. A lone option hides the decision."
        )

    lines = [
        REQUIRED_SECTIONS[0],
        problem.strip(),
        "",
        REQUIRED_SECTIONS[1],
    ]
    for number, option in enumerate(options, start=1):
        lines += [
            f"Option {number} - {option.name}",
            f"   Good: {option.good}",
            f"   Bad:  {option.bad}",
        ]
    lines += [
        f"Chosen: {chosen}",
        "",
        REQUIRED_SECTIONS[2],
        f"Accuracy of results - {axes.accuracy.verdict}. {axes.accuracy.sentence}",
        f"Scalability         - {axes.scalability.verdict}. {axes.scalability.sentence}",
        f"Ease of maintenance - {axes.maintenance.verdict}. {axes.maintenance.sentence}",
    ]
    if details_link:
        lines += ["", "## Where the details live", details_link]
    return "\n".join(lines) + "\n"


def validate_description(text: str) -> tuple[str, ...]:
    """Return a tuple of problems. Empty means the shape is right.

    This REPORTS rather than raises because it also runs against
    descriptions written by hand in the Jira UI, where refusing to read a
    slightly wrong issue would be worse than naming what is missing.
    """
    problems: list[str] = []
    for section in REQUIRED_SECTIONS:
        if section not in text:
            problems.append(f"missing section: {section}")

    if (
        "## Ways to fix it" in text
        and len(re.findall(r"^Option \d+ - ", text, re.M)) < 2
    ):
        problems.append("fewer than two options under 'Ways to fix it'")

    for axis in ("Accuracy of results", "Scalability", "Ease of maintenance"):
        if not re.search(rf"^{axis}\s*-\s*(Better|Same|Worse)\.", text, re.M):
            problems.append(f"axis missing a Better/Same/Worse verdict: {axis}")

    return tuple(problems)


MAX_SENTENCE_WORDS: int = 25

# Words that make a sentence sound informed while saying less than the plain
# word they replaced. Not exhaustive, and not meant to be.
BANNED_WORDS: frozenset[str] = frozenset(
    {
        "leverage",
        "utilize",
        "facilitate",
        "robust",
        "seamless",
        "holistic",
        "streamline",
        "synergy",
    }
)

# A code span cannot cross a line. An EARLIER version of this regex,
# `[^`]*`, accepted newlines, so one unpaired backtick swallowed every
# character up to the next one and the lint reported clean on text it had
# already deleted.
_CODE_SPAN = re.compile(r"`[^`\n]*`")

# A fenced block is not prose and must not be measured.
_FENCE = re.compile(r"^```.*?^```", re.M | re.S)

# `---` opens YAML frontmatter and is ALSO a markdown thematic break.
# Requiring at least one `key: value` line inside tells them apart:
# frontmatter has one by definition, a paragraph between two rules does not.
_FRONTMATTER = re.compile(r"\A---\n((?:[^\n]*\n)*?)---\n", re.S)
_FRONTMATTER_KEY = re.compile(r"^[A-Za-z][\w.-]*:\s", re.M)

_SENTENCE_SPLIT = re.compile(r"(?<=[.!?])\s+")

# A structural line stands alone: heading, list item, numbered item, table
# row, blockquote, fence marker, thematic break. Everything else is
# paragraph text and is JOINED to the line above it -- markdown prose is
# typically hard-wrapped, so treating each physical line as one sentence
# would make every sentence look short and silently turn this check off.
_STRUCTURAL = re.compile(
    r"^\s*(#{1,6}\s|[-*+]\s|\d+[.)]\s|\||>|```|~~~|-{3,}$|\*{3,}$|_{3,}$)"
)


@dataclass(frozen=True)
class LintFinding:
    kind: str
    detail: str


def _strip_frontmatter(text: str) -> str:
    """Remove a leading YAML frontmatter block, and only a real one."""
    found = _FRONTMATTER.match(text)
    if found and _FRONTMATTER_KEY.search(found.group(1)):
        return text[found.end() :]
    return text


def _units(prose: str) -> list[str]:
    """Split markdown into measurable units.

    A structural line -- heading, list item, table row, fence -- is its own
    unit, because it has no terminal period and would otherwise glue to the
    paragraph beneath it. Consecutive plain lines are JOINED into one
    paragraph, because prose is typically hard-wrapped and a sentence spans
    several physical lines.
    """
    units: list[str] = []
    paragraph: list[str] = []

    def flush() -> None:
        if paragraph:
            units.append(" ".join(paragraph))
            paragraph.clear()

    for line in prose.splitlines():
        if not line.strip() or _STRUCTURAL.match(line):
            flush()
            if line.strip():
                units.append(line)
        else:
            paragraph.append(line.strip())
    flush()
    return units


def lint_prose(text: str) -> tuple[LintFinding, ...]:
    """Flag long sentences and inflated words. Warns; it never blocks.

    BE CLEAR WHAT THIS IS. A heuristic that catches obvious cases. It will
    pass sentences that are still hard to read, and it is not proof that
    the writing is simple.

    Code spans are stripped before measuring. A file path, a command or an
    error string is not prose: it must never be simplified, and counting it
    would send any comment quoting a long command red for no reason.
    """
    prose = _strip_frontmatter(text)
    prose = _FENCE.sub(" ", prose)
    prose = _CODE_SPAN.sub(" ", prose)

    findings: list[LintFinding] = []

    for unit in _units(prose):
        for sentence in _SENTENCE_SPLIT.split(unit):
            words = sentence.split()
            if len(words) > MAX_SENTENCE_WORDS:
                findings.append(
                    LintFinding(
                        "long-sentence",
                        f"{len(words)} words (limit {MAX_SENTENCE_WORDS}): "
                        f"{' '.join(words[:8])}...",
                    )
                )

    lowered = re.findall(r"[a-zA-Z]+", prose.lower())
    for banned in sorted(BANNED_WORDS):
        if banned in lowered:
            findings.append(LintFinding("banned-word", banned))

    return tuple(findings)


def _paragraph(text: str) -> dict:
    """One ADF paragraph, with hardBreak nodes where the text has newlines.

    ADF forbids a newline INSIDE a text node; a line break is its own node.
    Jira has been observed to tolerate a literal newline anyway, but "the
    server accepted it" is not the same as "it is valid" -- a stricter
    renderer, or a future validation pass, could reject documents built the
    other way.
    """
    content: list[dict] = []
    for index, line in enumerate(text.split("\n")):
        if index:
            content.append({"type": "hardBreak"})
        if line:
            content.append({"type": "text", "text": line})
    if not content:
        content = [{"type": "text", "text": ""}]
    return {"type": "paragraph", "content": content}


def _heading(text: str, level: int = 2) -> dict:
    return {
        "type": "heading",
        "attrs": {"level": level},
        "content": [{"type": "text", "text": text}],
    }


def text_adf(text: str) -> dict:
    """Wrap plain text as an ADF document, one paragraph per blank-line block.

    WHY THIS EXISTS. Jira REST v3 takes descriptions and comments as ADF -- a
    JSON node tree -- not as markdown. Building the tree from the same
    inputs the markdown renderer uses is the alternative to rendering
    markdown and then parsing it back, which would be a parser nobody needs
    to own.

    Empty paragraphs are dropped: Jira rejects a paragraph node with no
    content, and a run of blank lines otherwise produces one.
    """
    blocks = [block.strip() for block in text.split("\n\n")]
    return {
        "type": "doc",
        "version": 1,
        "content": [_paragraph(block) for block in blocks if block],
    }


def description_adf(
    problem: str,
    options: Sequence[Option],
    chosen: str,
    axes: Axes,
    details_link: str | None,
) -> dict:
    """The issue template as ADF. Same inputs and rules as render_description."""
    if len(options) < 2:
        raise LabelError(
            "a description needs at least two options; got "
            f"{len(options)}. A lone option hides the decision."
        )

    content: list[dict] = [
        _heading("What is broken, or what is missing"),
        _paragraph(problem.strip()),
        _heading("Ways to fix it"),
    ]
    for number, option in enumerate(options, start=1):
        content.append(
            _paragraph(
                f"Option {number} - {option.name}\n"
                f"Good: {option.good}\n"
                f"Bad: {option.bad}"
            )
        )
    content.append(_paragraph(f"Chosen: {chosen}"))

    content.append(_heading("What this does to the three things that matter"))
    content.append(
        _paragraph(
            f"Accuracy of results - {axes.accuracy.verdict}. {axes.accuracy.sentence}\n"
            f"Scalability - {axes.scalability.verdict}. {axes.scalability.sentence}\n"
            f"Ease of maintenance - {axes.maintenance.verdict}. {axes.maintenance.sentence}"
        )
    )

    if details_link:
        content.append(_heading("Where the details live"))
        content.append(_paragraph(details_link))

    return {"type": "doc", "version": 1, "content": content}
