"""The label vocabulary is closed, and closing it is the whole point.

Jira accepts any string as a label. A typo does not error -- it creates a
second bucket that never merges back into the first.
"""

import pytest

from jira_agent_kit.schema import (
    MAX_SENTENCE_WORDS,
    Axes,
    AxisVerdict,
    LabelError,
    Option,
    Vocabulary,
    description_adf,
    lint_prose,
    render_description,
    text_adf,
    validate_description,
)


def _vocab() -> Vocabulary:
    return Vocabulary("eng")


def _axes(accuracy="Same", scalability="Same", maintenance="Same") -> Axes:
    return Axes(
        accuracy=AxisVerdict(accuracy, "No change to the numbers."),
        scalability=AxisVerdict(scalability, "Same load, same cost."),
        maintenance=AxisVerdict(maintenance, "One file, one job."),
    )


def _options():
    return [
        Option("Read it from the file", "Simple.", "Slow on big files."),
        Option("Cache it in memory", "Fast.", "Can go stale."),
    ]


# --- Vocabulary --------------------------------------------------------------


def test_a_label_outside_the_vocabulary_is_rejected():
    vocab = _vocab()
    with pytest.raises(LabelError) as caught:
        vocab.validate_labels(["eng-fronted", "eng-feature", "eng-by-agent"])
    assert "eng-fronted" in str(caught.value)


def test_a_valid_set_is_returned_sorted_and_deduplicated():
    vocab = _vocab()
    got = vocab.validate_labels(
        [
            "eng-by-agent",
            "eng-backend",
            "eng-feature",
            "eng-area-src",
            "eng-area-src",
        ]
    )
    assert got == ("eng-area-src", "eng-backend", "eng-by-agent", "eng-feature")


def test_exactly_one_layer_is_required():
    vocab = _vocab()
    with pytest.raises(LabelError, match="layer"):
        vocab.validate_labels(["eng-feature", "eng-by-agent"])
    with pytest.raises(LabelError, match="layer"):
        vocab.validate_labels(["eng-frontend", "eng-backend", "eng-feature", "eng-by-agent"])


def test_exactly_one_goal_and_one_origin_are_required():
    vocab = _vocab()
    with pytest.raises(LabelError, match="goal"):
        vocab.validate_labels(["eng-backend", "eng-by-agent"])
    with pytest.raises(LabelError, match="origin"):
        vocab.validate_labels(["eng-backend", "eng-feature"])


def test_at_most_one_risk_and_one_needs_kind():
    vocab = _vocab()
    with pytest.raises(LabelError, match="risk"):
        vocab.validate_labels(
            ["eng-backend", "eng-feature", "eng-by-agent", "eng-risk-low", "eng-risk-high"]
        )
    with pytest.raises(LabelError, match="needs kind"):
        vocab.validate_labels(
            [
                "eng-backend", "eng-feature", "eng-by-agent",
                "eng-needs-decide", "eng-needs-provide",
            ]
        )


def test_area_and_hurt_labels_may_repeat_freely():
    vocab = _vocab()
    assert vocab.validate_labels(
        [
            "eng-backend", "eng-feature", "eng-by-agent",
            "eng-area-src", "eng-area-tests", "eng-area-docs",
            "eng-hurts-accuracy", "eng-hurts-scale", "eng-hurts-maintenance",
        ]
    )


def test_area_labels_are_not_a_closed_list():
    # Unlike every other axis, ANY top-level directory name is a valid
    # area label -- there is no allow-list to keep in sync with a repo's
    # own, arbitrary layout.
    vocab = _vocab()
    assert vocab.validate_labels(
        ["eng-backend", "eng-feature", "eng-by-agent", "eng-area-whatever-you-call-it"]
    )


def test_validate_label_names_checks_membership_without_cardinality():
    # For labels ADDED to an existing issue (add_labels), not created with
    # one: the layer is already on the issue and is not part of this call.
    vocab = _vocab()
    assert vocab.validate_label_names(["eng-hurts-scale", "eng-area-src"]) == (
        "eng-area-src",
        "eng-hurts-scale",
    )
    with pytest.raises(LabelError, match="eng-nonsense"):
        vocab.validate_label_names(["eng-nonsense"])


def test_different_prefixes_do_not_recognise_each_others_labels():
    # The whole point of the prefix: two teams on the same Jira site cannot
    # collide.
    eng = Vocabulary("eng")
    with pytest.raises(LabelError):
        eng.validate_label_names(["data-backend"])


# --- Axes and the description template ---------------------------------------


def test_rendered_description_has_every_required_section():
    text = render_description(
        problem="The board has no record of why a change was made.",
        options=_options(),
        chosen="Option 1, because a stale answer is worse than a slow one.",
        axes=_axes(),
        details_link="docs/example.md",
    )
    assert validate_description(text) == ()


def test_a_description_missing_a_section_is_reported_not_raised():
    problems = validate_description("## What is broken, or what is missing\nnothing else")
    assert problems
    assert any("Ways to fix it" in p for p in problems)


def test_one_option_is_rejected_because_a_lone_option_hides_a_decision():
    with pytest.raises(LabelError, match="two options"):
        render_description(
            problem="p", options=[Option("only", "good", "bad")],
            chosen="Option 1", axes=_axes(), details_link=None,
        )


def test_worse_axes_produce_exactly_the_matching_hurt_labels():
    vocab = _vocab()
    axes = Axes(
        accuracy=AxisVerdict("Worse", "Fewer analogs survive the filter."),
        scalability=AxisVerdict("Better", "One query instead of N."),
        maintenance=AxisVerdict("Worse", "Two code paths now."),
    )
    assert axes.hurt_labels(vocab) == ("eng-hurts-accuracy", "eng-hurts-maintenance")


def test_no_worse_axis_produces_no_hurt_labels():
    assert _axes().hurt_labels(_vocab()) == ()


def test_an_unknown_verdict_is_rejected():
    with pytest.raises(LabelError, match="verdict"):
        AxisVerdict("Slightly better", "hedging is not a verdict")


def test_validate_description_rejects_a_single_option():
    text = (
        "## What is broken, or what is missing\np\n\n"
        "## Ways to fix it\nOption 1 - only\nChosen: it\n\n"
        "## What this does to the three things that matter\n"
        "Accuracy of results - Same. x\nScalability - Same. x\n"
        "Ease of maintenance - Same. x\n"
    )
    assert any("fewer than two options" in p for p in validate_description(text))


def test_validate_description_rejects_a_hedged_verdict():
    text = (
        "## What is broken, or what is missing\np\n\n"
        "## Ways to fix it\nOption 1 - a\nOption 2 - b\nChosen: a\n\n"
        "## What this does to the three things that matter\n"
        "Accuracy of results - Slightly better. x\nScalability - Same. x\n"
        "Ease of maintenance - Same. x\n"
    )
    assert any("Accuracy of results" in p for p in validate_description(text))


def test_details_link_is_rendered_by_both_renderers():
    link = "docs/example.md"
    text = render_description(problem="p", options=_options(), chosen="c", axes=_axes(), details_link=link)
    assert "## Where the details live" in text and link in text

    doc = description_adf(problem="p", options=_options(), chosen="c", axes=_axes(), details_link=link)
    flat = [
        run["text"]
        for node in doc["content"]
        for run in node.get("content", [])
        if run.get("type") == "text"
    ]
    assert "Where the details live" in flat and link in flat


# --- ADF -----------------------------------------------------------------------


def test_adf_is_a_doc_of_version_one():
    doc = text_adf("One line.\n\nAnother line.")
    assert doc["type"] == "doc"
    assert doc["version"] == 1
    assert [node["type"] for node in doc["content"]] == ["paragraph", "paragraph"]


def test_adf_never_emits_an_empty_paragraph():
    doc = text_adf("First.\n\n\n\nSecond.")
    assert all(node["content"] for node in doc["content"])


def test_adf_uses_hard_breaks_rather_than_newlines_inside_text_nodes():
    doc = description_adf(problem="p", options=_options(), chosen="c", axes=_axes(), details_link=None)
    saw_hard_break = False
    for node in doc["content"]:
        for run in node.get("content", []):
            if run.get("type") == "text":
                assert "\n" not in run["text"], f"newline in text node: {run['text']!r}"
            if run.get("type") == "hardBreak":
                saw_hard_break = True
    # BOTH halves matter: asserting only the absence of newlines does not
    # discriminate, since dropping the hardBreak append still splits the
    # text and no newline survives while the line break is simply LOST.
    assert saw_hard_break, "line breaks were dropped rather than turned into hardBreak"


def test_description_adf_carries_the_three_axis_headings():
    doc = description_adf(problem="p", options=_options(), chosen="c", axes=_axes(), details_link=None)
    headings = [n["content"][0]["text"] for n in doc["content"] if n["type"] == "heading"]
    assert "What this does to the three things that matter" in headings


# --- lint -----------------------------------------------------------------------


def test_the_lint_is_green_on_plain_copy():
    # A lint that only ever fires is measuring nothing. This is the
    # control: it must pass, or every later red is meaningless.
    assert lint_prose(
        "The level key merged two things. They differ only in one field. "
        "This fix keeps them apart."
    ) == ()


def test_a_long_sentence_is_flagged():
    long_one = "This " + "very " * 30 + "long sentence never ends."
    assert any(f.kind == "long-sentence" for f in lint_prose(long_one))


def test_a_banned_word_is_flagged():
    findings = lint_prose("We leverage the cache to facilitate lookups.")
    assert "banned-word" in {f.kind for f in findings}


def test_the_threshold_is_exact_at_the_boundary():
    def sentence(n):
        return " ".join(["word"] * n) + "."

    assert [f for f in lint_prose(sentence(MAX_SENTENCE_WORDS)) if f.kind == "long-sentence"] == []
    assert [f for f in lint_prose(sentence(MAX_SENTENCE_WORDS + 1)) if f.kind == "long-sentence"] != []


def test_code_spans_are_exempt_from_the_word_count():
    text = (
        "Run `mycli create --type task --summary x --problem y --option a "
        "--option b --chosen c --accuracy d --scalability e --maintenance f` "
        "and read the exit code."
    )
    assert [f for f in lint_prose(text) if f.kind == "long-sentence"] == []


def test_a_heading_does_not_glue_to_the_paragraph_below_it():
    # A markdown heading has no terminal period. WITHOUT the structural
    # split, the heading (12 words) would glue to the paragraph beneath it
    # (24 words) into one impossible 36-word "sentence" -- each half alone
    # is under the 25-word limit, so this only fails if gluing happens.
    text = (
        "## What this section is actually about, stated in full each time\n"
        "This is the paragraph underneath it and it is also fairly long "
        "and should be measured on its own, not merged with the heading.\n"
    )
    assert [f for f in lint_prose(text) if f.kind == "long-sentence"] == []


def test_a_long_sentence_hard_wrapped_across_lines_is_still_flagged():
    # Prose is typically hard-wrapped, so a line-only split would make
    # every sentence look short and turn this check off entirely.
    text = (
        "It carries the files actually changed against the ones you\n"
        "predicted, the test exit code with the exact command, the second\n"
        "check's exit code, the three axes measured, and what is left.\n"
    )
    assert [f for f in lint_prose(text) if f.kind == "long-sentence"] != []


def test_a_leading_thematic_break_is_not_mistaken_for_frontmatter():
    # `---` opens YAML frontmatter AND is a markdown thematic break.
    text = (
        "---\n"
        "A first paragraph that is really quite long indeed and ought to be "
        "measured properly by this particular lint rather than being quietly "
        "deleted before anything counts its words at all.\n"
        "---\n"
        "second\n"
    )
    assert [f for f in lint_prose(text) if f.kind == "long-sentence"] != []


def test_real_frontmatter_is_still_stripped():
    text = (
        "---\n"
        "name: example\n"
        "description: A description long enough that if it were measured "
        "as prose it would trip the long-sentence check on its own quite "
        "easily, which is exactly why frontmatter must be exempt.\n"
        "---\n"
        "Real prose starts here.\n"
    )
    assert [f for f in lint_prose(text) if f.kind == "long-sentence"] == []
