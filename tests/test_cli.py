"""CLI wiring and exit codes. No network, no Keychain."""

import pytest

from jira_agent_kit import cli
from jira_agent_kit.schema import AxisVerdict, LabelError, Option


@pytest.fixture(autouse=True)
def _config_env(monkeypatch):
    """Every test gets a fully configured environment by default.

    A test that wants the missing-config path is covered in test_config.py,
    not repeated here -- this file is about command behaviour, assuming
    config already loaded.
    """
    monkeypatch.setenv("JIRA_AGENT_SITE", "example.atlassian.net")
    monkeypatch.setenv("JIRA_AGENT_PROJECT_KEY", "PROJ")
    monkeypatch.setenv("JIRA_AGENT_EMAIL", "t@example.com")
    monkeypatch.setenv("JIRA_AGENT_LABEL_PREFIX", "eng")
    monkeypatch.delenv("JIRA_AGENT_KEYCHAIN_SERVICE", raising=False)
    # Off by default: `finish` samples issues by hashing the issue key, so
    # an unrelated test (e.g. "PROJ-12") could otherwise non-deterministically
    # trigger a real Jev call attempt depending on which key it happens to
    # use. Tests that specifically exercise judge wiring set this to "1"
    # themselves and stub JevJudge -- see the judge-wiring section below.
    monkeypatch.setenv("JIRA_AGENT_JUDGE_SAMPLE_RATE", "0")


class _RecordingClient:
    """Captures what a command sends, so the body can be asserted on."""

    def __init__(self):
        self.comments = []
        self.labels = []
        self.created = None

    def add_comment(self, key, body_adf):
        self.comments.append((key, body_adf))

    def add_labels(self, key, labels, vocabulary):
        self.labels.append((key, list(labels)))

    def create_issue(self, **kwargs):
        self.created = kwargs
        return "PROJ-99"

    def resolve_issue_type_id(self, project_key, type_name):
        return "10005"


def _adf_text(doc):
    return " ".join(
        run["text"]
        for node in doc["content"]
        for run in node.get("content", [])
        if run.get("type") == "text"
    )


# --- misuse and dispatch --------------------------------------------------------


def test_no_command_is_misuse():
    assert cli.main([]) == cli.EXIT_MISUSE


def test_missing_config_exits_misuse_before_any_parsing(monkeypatch):
    monkeypatch.delenv("JIRA_AGENT_SITE", raising=False)
    monkeypatch.delenv("JIRA_AGENT_PROJECT_KEY", raising=False)
    monkeypatch.delenv("JIRA_AGENT_EMAIL", raising=False)
    monkeypatch.delenv("JIRA_AGENT_LABEL_PREFIX", raising=False)
    assert cli.main(["whoami"]) == cli.EXIT_MISUSE


# --- lint -------------------------------------------------------------------------


def test_lint_is_green_on_plain_copy(tmp_path):
    target = tmp_path / "copy.txt"
    target.write_text("The fix keeps the two things apart.\n")
    assert cli.main(["lint", str(target)]) == cli.EXIT_OK


def test_lint_reports_and_still_exits_zero_because_it_warns(tmp_path, capsys):
    target = tmp_path / "copy.txt"
    target.write_text("We leverage the cache.\n")
    assert cli.main(["lint", str(target)]) == cli.EXIT_OK
    assert "leverage" in capsys.readouterr().out


def test_lint_on_a_missing_file_exits_two_instead_of_raising(capsys):
    assert cli.main(["lint", "/nope/does/not/exist.md"]) == cli.EXIT_MISUSE
    assert "exist" in capsys.readouterr().err


# --- token / auth errors -----------------------------------------------------------


def test_a_missing_token_exits_two_with_the_fix_in_the_message(monkeypatch, capsys):
    def no_token(*args, **kwargs):
        raise cli.TokenMissing("run: security add-generic-password -s x -a y -w")

    monkeypatch.setattr(cli, "read_token", no_token)
    assert cli.main(["show", "PROJ-1"]) == cli.EXIT_MISUSE
    assert "security add-generic-password" in capsys.readouterr().err


def test_an_unparseable_issue_key_is_misuse(capsys):
    assert cli.main(["show", "NOTANISSUE"]) == cli.EXIT_MISUSE


# --- create -----------------------------------------------------------------------


def _create_args(**overrides):
    base = {
        "--type": "task", "--summary": "s", "--problem": "p",
        "--chosen": "Option 1.", "--accuracy": "Same|x.",
        "--scalability": "Same|x.", "--maintenance": "Same|x.",
        "--layer": "backend", "--goal": "feature",
    }
    base.update(overrides)
    argv = ["create"]
    for k, v in base.items():
        if v is None:
            continue
        argv += [k, v]
    argv += ["--option", "a|g|b", "--option", "c|g|b"]
    return argv


def test_create_needs_two_options(monkeypatch, capsys):
    monkeypatch.setattr(
        cli, "_client", lambda config, email: pytest.fail("built a client on the refusal path")
    )
    argv = ["create", "--type", "task", "--summary", "s", "--problem", "p",
            "--chosen", "x", "--accuracy", "Same|x", "--scalability", "Same|x",
            "--maintenance", "Same|x", "--layer", "backend", "--goal", "feature",
            "--option", "only|good|bad"]
    code = cli.main(argv)
    assert code == cli.EXIT_MISUSE
    assert "two options" in capsys.readouterr().err


def test_create_creates_with_the_resolved_issue_type_id(monkeypatch):
    fake = _RecordingClient()
    monkeypatch.setattr(cli, "_client", lambda config, email: fake)
    cli.main(_create_args())
    assert fake.created["issue_type_id"] == "10005"
    assert fake.created["project_key"] == "PROJ"


def test_create_sends_the_hurt_labels_for_a_worse_axis(monkeypatch):
    fake = _RecordingClient()
    monkeypatch.setattr(cli, "_client", lambda config, email: fake)
    cli.main(_create_args(**{"--accuracy": "Worse|Fewer analogs.", "--goal": "accuracy"}))
    assert "eng-hurts-accuracy" in fake.created["labels"]
    assert "eng-by-agent" in fake.created["labels"]


def test_create_does_not_guess_area_labels(monkeypatch):
    fake = _RecordingClient()
    monkeypatch.setattr(cli, "_client", lambda config, email: fake)
    cli.main(_create_args())
    assert not any(label.startswith("eng-area-") for label in fake.created["labels"])


def test_create_lints_the_summary_and_problem_as_separate_texts(monkeypatch, capsys):
    """A summary is not the first clause of the problem. Joining them with
    a space glues the summary to the problem's first sentence and reports a
    length for text nobody wrote.
    """
    fake = _RecordingClient()
    monkeypatch.setattr(cli, "_client", lambda config, email: fake)
    cli.main(
        _create_args(
            **{
                "--summary": "Run the exam against the full library that was just built",
                "--problem": "The exam and its four separate thresholds are already written",
            }
        )
    )
    assert "long-sentence" not in capsys.readouterr().err


def test_subtask_requires_a_parent(capsys):
    argv = _create_args(**{"--type": "subtask"})
    code = cli.main(argv)
    assert code == cli.EXIT_MISUSE
    assert "--parent" in capsys.readouterr().err


def test_subtask_with_a_parent_resolves_the_subtask_type(monkeypatch):
    fake = _RecordingClient()
    monkeypatch.setattr(cli, "_client", lambda config, email: fake)
    cli.main(_create_args(**{"--type": "subtask", "--parent": "PROJ-12"}))
    assert fake.created["parent_key"] == "PROJ-12"


def test_estimate_errors_clearly_when_no_field_is_configured(monkeypatch, capsys):
    class FakeClient(_RecordingClient):
        def find_estimate_field(self, project_key, issue_type_id):
            return None

    monkeypatch.setattr(cli, "_client", lambda config, email: FakeClient())
    code = cli.main(_create_args(**{"--estimate": "3"}))
    assert code == cli.EXIT_MISUSE
    assert "no estimate field" in capsys.readouterr().err.lower()


def test_estimate_is_set_when_a_field_is_configured(monkeypatch):
    class FakeClient(_RecordingClient):
        def find_estimate_field(self, project_key, issue_type_id):
            return "customfield_10099"

    fake = FakeClient()
    monkeypatch.setattr(cli, "_client", lambda config, email: fake)
    cli.main(_create_args(**{"--estimate": "5"}))
    assert fake.created["extra_fields"]["customfield_10099"] == 5


def test_priority_is_sent_as_a_name_object(monkeypatch):
    fake = _RecordingClient()
    monkeypatch.setattr(cli, "_client", lambda config, email: fake)
    cli.main(_create_args(**{"--priority": "High"}))
    assert fake.created["extra_fields"]["priority"] == {"name": "High"}


class _SprintClient(_RecordingClient):
    def __init__(self, sprints, board_id=1, sprint_field="customfield_20"):
        super().__init__()
        self._sprints = sprints
        self._board_id = board_id
        self._sprint_field = sprint_field

    def find_board_id(self, project_key):
        return self._board_id

    def list_sprints(self, board_id):
        return self._sprints

    def find_sprint_field(self, project_key, issue_type_id):
        return self._sprint_field


def test_create_sprint_by_name_resolves_to_the_bare_int_id(monkeypatch):
    fake = _SprintClient([{"id": 7, "name": "Sprint 1", "state": "future"}])
    monkeypatch.setattr(cli, "_client", lambda config, email: fake)
    cli.main(_create_args(**{"--sprint": "sprint 1"}))
    assert fake.created["extra_fields"]["customfield_20"] == 7
    assert not isinstance(fake.created["extra_fields"]["customfield_20"], list)


def test_create_sprint_by_bare_id_skips_the_name_lookup(monkeypatch):
    fake = _SprintClient([{"id": 7, "name": "Sprint 1", "state": "future"}])
    monkeypatch.setattr(cli, "_client", lambda config, email: fake)
    cli.main(_create_args(**{"--sprint": "7"}))
    assert fake.created["extra_fields"]["customfield_20"] == 7


def test_create_sprint_by_an_unmatched_name_lists_what_exists(monkeypatch, capsys):
    fake = _SprintClient([{"id": 7, "name": "Sprint 1", "state": "future"}])
    monkeypatch.setattr(cli, "_client", lambda config, email: fake)
    code = cli.main(_create_args(**{"--sprint": "nonexistent"}))
    assert code == cli.EXIT_MISUSE
    assert "Sprint 1" in capsys.readouterr().err


def test_create_sprint_errors_when_no_sprint_field_is_configured(monkeypatch, capsys):
    fake = _SprintClient([], sprint_field=None)
    monkeypatch.setattr(cli, "_client", lambda config, email: fake)
    code = cli.main(_create_args(**{"--sprint": "1"}))
    assert code == cli.EXIT_MISUSE
    assert "sprint" in capsys.readouterr().err.lower()


# --- block ------------------------------------------------------------------------


def test_block_without_a_need_is_misuse(capsys):
    assert cli.main(["block", "PROJ-1", "--kind", "decide"]) == cli.EXIT_MISUSE
    assert "--need" in capsys.readouterr().err


def test_block_without_a_kind_is_misuse(capsys):
    assert cli.main(["block", "PROJ-1", "--need", "x"]) == cli.EXIT_MISUSE
    assert "--kind" in capsys.readouterr().err


def test_block_rejects_an_unknown_kind():
    with pytest.raises(SystemExit):
        cli._parser(cli.load_config()).parse_args(
            ["block", "PROJ-1", "--need", "x", "--kind", "urgent"]
        )


def test_block_applies_both_labels_and_posts_the_need(monkeypatch):
    fake = _RecordingClient()
    monkeypatch.setattr(cli, "_client", lambda config, email: fake)
    code = cli.main(["block", "PROJ-12", "--need", "Pick a hosting provider.", "--kind", "decide"])
    assert code == cli.EXIT_OK
    assert fake.labels == [("PROJ-12", ["eng-needs-you", "eng-needs-decide"])]
    key, doc = fake.comments[0]
    assert key == "PROJ-12"
    text = _adf_text(doc)
    assert "Pick a hosting provider." in text
    assert "decide" in text.lower()


def test_block_dropping_the_kind_label_would_fail_the_pairing_test(monkeypatch):
    """Mutation-shaped: proves the assertion above actually discriminates."""
    class SingleLabelClient(_RecordingClient):
        def add_labels(self, key, labels, vocabulary):
            self.labels.append((key, [labels[0]]))  # drop the kind label

    fake = SingleLabelClient()
    monkeypatch.setattr(cli, "_client", lambda config, email: fake)
    cli.main(["block", "PROJ-12", "--need", "x", "--kind", "go"])
    assert fake.labels != [("PROJ-12", ["eng-needs-you", "eng-needs-go"])]


# --- show / comment ------------------------------------------------------------------


def test_show_prints_labels_and_the_last_comments(monkeypatch, capsys):
    class FakeClient:
        def get_issue(self, key):
            return {"fields": {"summary": "A thing", "status": {"name": "To Do"}, "labels": ["eng-backend"]}}

        def list_comments(self, key, limit):
            return ["oldest note", "newest note"]

    monkeypatch.setattr(cli, "_client", lambda config, email: FakeClient())
    assert cli.main(["show", "PROJ-10"]) == cli.EXIT_OK
    out = capsys.readouterr().out
    assert "newest note" in out and "oldest note" in out


def test_show_survives_an_issue_with_no_status_field(monkeypatch, capsys):
    class FakeClient:
        def get_issue(self, key):
            return {"fields": {"summary": "No status here"}}

        def list_comments(self, key, limit):
            return []

    monkeypatch.setattr(cli, "_client", lambda config, email: FakeClient())
    assert cli.main(["show", "PROJ-1"]) == cli.EXIT_OK
    assert "No status here" in capsys.readouterr().out


def _fake_git(monkeypatch, files, sha, subject):
    monkeypatch.setattr(cli.context, "changed_files", lambda rev, repo: tuple(files))

    def git(args, repo):
        if args[0] == "rev-parse":
            return sha
        if args[0] == "log":
            return subject
        raise AssertionError(f"unexpected git call: {args}")

    monkeypatch.setattr(cli.context, "git", git)


def test_comment_posts_the_sha_subject_and_changed_files(monkeypatch):
    fake = _RecordingClient()
    monkeypatch.setattr(cli, "_client", lambda config, email: fake)
    _fake_git(monkeypatch, files=["src/db.py", "web/app.tsx"], sha="a" * 40, subject="feat: PROJ-12 x")

    assert cli.main(["comment", "PROJ-12"]) == cli.EXIT_OK
    key, doc = fake.comments[0]
    assert key == "PROJ-12"
    text = _adf_text(doc)
    assert "a" * 12 in text
    assert "src/db.py" in text and "web/app.tsx" in text


def test_comment_adds_area_labels_derived_from_the_changed_files(monkeypatch):
    fake = _RecordingClient()
    monkeypatch.setattr(cli, "_client", lambda config, email: fake)
    _fake_git(monkeypatch, files=["src/db.py", "web/app.tsx"], sha="a" * 40, subject="feat: PROJ-12 x")
    cli.main(["comment", "PROJ-12"])
    assert fake.labels == [("PROJ-12", ["eng-area-src", "eng-area-web"])]


def test_comment_with_no_key_falls_back_to_the_commit_subject(monkeypatch):
    fake = _RecordingClient()
    monkeypatch.setattr(cli, "_client", lambda config, email: fake)
    _fake_git(monkeypatch, files=["docs/x.md"], sha="c" * 40, subject="docs: PROJ-77 y")
    assert cli.main(["comment"]) == cli.EXIT_OK
    assert fake.comments[0][0] == "PROJ-77"


def test_comment_is_a_noop_when_the_subject_names_no_issue(monkeypatch):
    def no_client(config, email):  # pragma: no cover
        raise AssertionError("comment built a client with no issue key")

    monkeypatch.setattr(cli, "_client", no_client)
    monkeypatch.setattr(cli.context, "git", lambda args, repo: "chore: no key here")
    assert cli.main(["comment"]) == cli.EXIT_OK


def test_comment_derives_the_key_from_the_same_rev_it_reports_on(monkeypatch):
    fake = _RecordingClient()
    monkeypatch.setattr(cli, "_client", lambda config, email: fake)
    monkeypatch.setattr(cli.context, "changed_files", lambda rev, repo: ("docs/a.md",))

    def git(args, repo):
        if args[0] == "rev-parse":
            return "d" * 40
        if args[0] == "log":
            return "feat: PROJ-55 the older commit"
        raise AssertionError(args)

    monkeypatch.setattr(cli.context, "git", git)
    assert cli.main(["comment", "--rev", "older"]) == cli.EXIT_OK
    assert fake.comments[0][0] == "PROJ-55"


# --- transition ---------------------------------------------------------------------


class _TransitionClient(_RecordingClient):
    def __init__(self, transitions):
        super().__init__()
        self._transitions = transitions
        self.transitioned = []

    def list_transitions(self, key):
        return self._transitions

    def transition(self, key, transition_id):
        self.transitioned.append((key, transition_id))


def test_transition_matches_a_name_case_insensitively(monkeypatch):
    fake = _TransitionClient({"To Do": "11", "Done": "41"})
    monkeypatch.setattr(cli, "_client", lambda config, email: fake)
    assert cli.main(["transition", "PROJ-1", "--to", "done"]) == cli.EXIT_OK
    assert fake.transitioned == [("PROJ-1", "41")]


def test_transition_lists_available_names_on_a_miss(monkeypatch, capsys):
    fake = _TransitionClient({"To Do": "11", "Done": "41"})
    monkeypatch.setattr(cli, "_client", lambda config, email: fake)
    code = cli.main(["transition", "PROJ-1", "--to", "Cancelled"])
    assert code == cli.EXIT_MISUSE
    err = capsys.readouterr().err
    assert "To Do" in err and "Done" in err


# --- start / finish -------------------------------------------------------------------


def test_start_needs_target_and_plan():
    with pytest.raises(SystemExit):
        cli.main(["start", "PROJ-12"])


def test_start_without_impact_cmd_skips_the_blast_radius_section(monkeypatch):
    fake = _RecordingClient()
    monkeypatch.setattr(cli, "_client", lambda config, email: fake)
    monkeypatch.setattr(cli.context, "current_branch", lambda repo: "PROJ-12-thing")
    monkeypatch.setattr(cli.context, "head_sha", lambda repo: "a" * 40)
    code = cli.main(
        [
            "start", "PROJ-12", "--target", "x", "--plan", "Do the thing.", "--files", "a.py",
            "--accuracy", "Same|x.", "--scalability", "Same|x.", "--maintenance", "Same|x.",
        ]
    )
    assert code == cli.EXIT_OK
    text = _adf_text(fake.comments[0][1])
    assert "Blast radius" not in text


def test_start_with_impact_cmd_posts_the_result_and_sets_risk(monkeypatch):
    fake = _RecordingClient()
    monkeypatch.setattr(cli, "_client", lambda config, email: fake)
    monkeypatch.setattr(cli.context, "current_branch", lambda repo: "PROJ-12-thing")
    monkeypatch.setattr(cli.context, "head_sha", lambda repo: "a" * 40)
    monkeypatch.setattr(
        cli.context, "run_command",
        lambda cmd: pytest.fail("run_command should not be used; run_impact should"),
    )
    monkeypatch.setattr(
        cli.context, "run_impact",
        lambda command: cli.context.ImpactResult(found=True, risk="HIGH", impacted_count=9, direct=3, error=None),
    )
    code = cli.main(
        [
            "start", "PROJ-12", "--target", "thing", "--plan", "Add it.", "--files", "a.py",
            "--impact-cmd", "mytool impact {target} --direction {direction}",
            "--accuracy", "Same|x.", "--scalability", "Same|x.", "--maintenance", "Same|x.",
        ]
    )
    assert code == cli.EXIT_OK
    text = _adf_text(fake.comments[0][1])
    assert "risk=HIGH" in text and "direct=3" in text
    assert fake.labels == [("PROJ-12", ["eng-risk-high"])]


def test_start_reports_when_the_impact_tool_cannot_find_the_target(monkeypatch, capsys):
    monkeypatch.setattr(cli, "_client", lambda config, email: _RecordingClient())
    monkeypatch.setattr(cli.context, "current_branch", lambda repo: "PROJ-12-thing")
    monkeypatch.setattr(cli.context, "head_sha", lambda repo: "a" * 40)
    monkeypatch.setattr(
        cli.context, "run_impact",
        lambda command: cli.context.ImpactResult(found=False, risk=None, impacted_count=0, direct=0, error="not found"),
    )
    code = cli.main(
        [
            "start", "PROJ-12", "--target", "x", "--plan", "p", "--files", "a.py",
            "--impact-cmd", "mytool impact {target}",
            "--accuracy", "Same|x.", "--scalability", "Same|x.", "--maintenance", "Same|x.",
        ]
    )
    assert code == cli.EXIT_MISUSE
    assert "not found" in capsys.readouterr().err


def test_finish_needs_left_stated_explicitly():
    with pytest.raises(SystemExit):
        cli.main(["finish", "PROJ-12", "--test-cmd", "true"])


def test_finish_reads_the_real_exit_code_never_the_text(monkeypatch):
    fake = _RecordingClient()
    monkeypatch.setattr(cli, "_client", lambda config, email: fake)
    monkeypatch.setattr(cli.context, "changed_files", lambda rev, repo: ("a.py",))
    code = cli.main(
        [
            "finish", "PROJ-12",
            "--test-cmd", "python3 -c \"print('12 passed'); import sys; sys.exit(1)\"",
            "--predicted", "a.py",
            "--accuracy", "Same|x.", "--scalability", "Same|x.", "--maintenance", "Same|x.",
            "--left", "nothing",
        ]
    )
    assert code == cli.EXIT_OK  # the REPORT posts fine even though the run failed
    text = _adf_text(fake.comments[0][1])
    assert "FAILED" in text and "exit 1" in text


def test_finish_names_scope_creep_in_both_directions(monkeypatch):
    fake = _RecordingClient()
    monkeypatch.setattr(cli, "_client", lambda config, email: fake)
    monkeypatch.setattr(cli.context, "changed_files", lambda rev, repo: ("a.py", "b.py"))
    cli.main(
        [
            "finish", "PROJ-12", "--test-cmd", "true", "--predicted", "a.py,c.py",
            "--accuracy", "Same|x.", "--scalability", "Same|x.", "--maintenance", "Same|x.",
            "--left", "nothing",
        ]
    )
    text = _adf_text(fake.comments[0][1])
    assert "b.py" in text and "not predicted" in text
    assert "c.py" in text and "not changed" in text


def test_finish_adds_hurt_labels_for_worse_axes(monkeypatch):
    fake = _RecordingClient()
    monkeypatch.setattr(cli, "_client", lambda config, email: fake)
    monkeypatch.setattr(cli.context, "changed_files", lambda rev, repo: ())
    cli.main(
        [
            "finish", "PROJ-12", "--test-cmd", "true", "--predicted", "",
            "--accuracy", "Worse|Fewer analogs.", "--scalability", "Same|x.",
            "--maintenance", "Same|x.", "--left", "nothing",
        ]
    )
    assert "eng-hurts-accuracy" in fake.labels[0][1]


def test_finish_dropping_hurt_labels_would_fail_the_test_above(monkeypatch):
    """Mutation-shaped control: proves the assertion above discriminates."""
    class NoHurtClient(_RecordingClient):
        def add_labels(self, key, labels, vocabulary):
            pass  # simulate the bug: never called

    fake = NoHurtClient()
    monkeypatch.setattr(cli, "_client", lambda config, email: fake)
    monkeypatch.setattr(cli.context, "changed_files", lambda rev, repo: ())
    cli.main(
        [
            "finish", "PROJ-12", "--test-cmd", "true", "--predicted", "",
            "--accuracy", "Worse|x.", "--scalability", "Same|x.",
            "--maintenance", "Same|x.", "--left", "nothing",
        ]
    )
    assert fake.labels == []  # confirms the mutation really does remove the signal


def test_finish_runs_an_optional_second_check_and_reports_its_exit_code(monkeypatch):
    fake = _RecordingClient()
    monkeypatch.setattr(cli, "_client", lambda config, email: fake)
    monkeypatch.setattr(cli.context, "changed_files", lambda rev, repo: ())
    cli.main(
        [
            "finish", "PROJ-12", "--test-cmd", "true",
            "--contract-cmd", "python3 -c \"import sys; sys.exit(3)\"",
            "--predicted", "",
            "--accuracy", "Same|x.", "--scalability", "Same|x.", "--maintenance", "Same|x.",
            "--left", "nothing",
        ]
    )
    text = _adf_text(fake.comments[0][1])
    assert "exit 3" in text


# --- finish: telemetry (JAE v2 spec §3) ------------------------------------------------


def test_finish_records_a_telemetry_row(monkeypatch, tmp_path):
    fake = _RecordingClient()
    monkeypatch.setattr(cli, "_client", lambda config, email: fake)
    monkeypatch.setattr(cli.context, "changed_files", lambda rev, repo: ("a.py",))
    monkeypatch.setattr(cli.telemetry, "repo_name", lambda repo: "test-repo")
    monkeypatch.setenv("JIRA_AGENT_DB_PATH", str(tmp_path / "jae.db"))

    cli.main(
        [
            "finish", "PROJ-12", "--test-cmd", "true", "--predicted", "a.py",
            "--accuracy", "Same|x.", "--scalability", "Same|x.", "--maintenance", "Same|x.",
            "--left", "nothing",
        ]
    )

    store = cli.telemetry.TelemetryStore(tmp_path / "jae.db")
    tasks = store.tasks(repo="test-repo")
    assert len(tasks) == 1
    assert tasks[0].issue_key == "PROJ-12"
    assert tasks[0].test_exit_code == 0
    assert tasks[0].files_changed == ("a.py",)


def test_finish_telemetry_records_a_nonzero_test_exit_code(monkeypatch, tmp_path):
    fake = _RecordingClient()
    monkeypatch.setattr(cli, "_client", lambda config, email: fake)
    monkeypatch.setattr(cli.context, "changed_files", lambda rev, repo: ())
    monkeypatch.setattr(cli.telemetry, "repo_name", lambda repo: "test-repo")
    monkeypatch.setenv("JIRA_AGENT_DB_PATH", str(tmp_path / "jae.db"))

    cli.main(
        [
            "finish", "PROJ-12",
            "--test-cmd", "python3 -c \"import sys; sys.exit(1)\"",
            "--predicted", "",
            "--accuracy", "Same|x.", "--scalability", "Same|x.", "--maintenance", "Same|x.",
            "--left", "nothing",
        ]
    )

    store = cli.telemetry.TelemetryStore(tmp_path / "jae.db")
    assert store.tasks(repo="test-repo")[0].test_exit_code == 1


def test_finish_still_posts_the_report_when_telemetry_cannot_be_written(monkeypatch, tmp_path):
    """Best-effort, per spec §3.2: a telemetry write failure must never fail
    the finish command the way a Jira or network failure never does either.
    """
    fake = _RecordingClient()
    monkeypatch.setattr(cli, "_client", lambda config, email: fake)
    monkeypatch.setattr(cli.context, "changed_files", lambda rev, repo: ())

    def _raise_repo_name(repo):
        raise ValueError("no 'origin' remote")

    monkeypatch.setattr(cli.telemetry, "repo_name", _raise_repo_name)
    monkeypatch.setenv("JIRA_AGENT_DB_PATH", str(tmp_path / "jae.db"))

    code = cli.main(
        [
            "finish", "PROJ-12", "--test-cmd", "true", "--predicted", "",
            "--accuracy", "Same|x.", "--scalability", "Same|x.", "--maintenance", "Same|x.",
            "--left", "nothing",
        ]
    )

    assert code == cli.EXIT_OK
    assert len(fake.comments) == 1  # the report still posted


# --- sprints / dashboard --------------------------------------------------------------


def test_sprints_lists_id_name_and_state(monkeypatch, capsys):
    fake = _SprintClient([{"id": 7, "name": "Sprint 1", "state": "future"}])
    monkeypatch.setattr(cli, "_client", lambda config, email: fake)
    assert cli.main(["sprints"]) == cli.EXIT_OK
    out = capsys.readouterr().out
    assert "7" in out and "Sprint 1" in out and "future" in out


def test_sprints_reports_when_the_project_has_no_board(monkeypatch, capsys):
    fake = _SprintClient([], board_id=None)
    monkeypatch.setattr(cli, "_client", lambda config, email: fake)
    assert cli.main(["sprints"]) == cli.EXIT_MISUSE
    assert "no agile board" in capsys.readouterr().err.lower()


def test_dashboard_is_dry_run_by_default(monkeypatch, capsys):
    monkeypatch.setattr(
        cli, "_client", lambda config, email: pytest.fail("dashboard contacted Jira without --apply")
    )
    assert cli.main(["dashboard"]) == cli.EXIT_OK
    out = capsys.readouterr().out
    assert "would create 5 filters" in out
    assert "PROJ -- waiting on you: decide" in out


class _DashboardClient:
    def __init__(self):
        self.filters_created = []
        self.dashboards_created = []
        self.gadgets_added = []
        self.gadget_configs = {}
        self._next_filter_id = 100
        self._next_gadget_id = 900

    def create_filter(self, name, jql, description=""):
        self._next_filter_id += 1
        fid = str(self._next_filter_id)
        self.filters_created.append({"id": fid, "name": name, "jql": jql})
        return fid

    def create_dashboard(self, name, description=""):
        self.dashboards_created.append({"name": name})
        return "200"

    def add_dashboard_gadget(self, dashboard_id, uri, color, row, column, title):
        self._next_gadget_id += 1
        gid = self._next_gadget_id
        self.gadgets_added.append({"id": gid, "dashboard_id": dashboard_id, "title": title})
        return gid

    def configure_gadget(self, dashboard_id, gadget_id, config):
        self.gadget_configs[gadget_id] = config


def test_dashboard_apply_creates_every_filter_and_gadget(monkeypatch):
    fake = _DashboardClient()
    monkeypatch.setattr(cli, "_client", lambda config, email: fake)
    assert cli.main(["dashboard", "--apply"]) == cli.EXIT_OK
    assert len(fake.filters_created) == 5
    assert len(fake.dashboards_created) == 1
    assert len(fake.gadgets_added) == 5
    assert len(fake.gadget_configs) == 5


def test_dashboard_wires_each_gadget_to_the_matching_filter_not_a_wrong_one(monkeypatch):
    """The defect class this exists for: five filters and five gadgets
    created in a loop is exactly the shape where gadget N could silently
    end up pointed at filter M instead, and every count-based assertion
    above would still pass.
    """
    fake = _DashboardClient()
    monkeypatch.setattr(cli, "_client", lambda config, email: fake)
    cli.main(["dashboard", "--apply"])

    by_name = {f["name"]: f["id"] for f in fake.filters_created}
    by_title = {g["title"]: g["id"] for g in fake.gadgets_added}

    assert (
        fake.gadget_configs[by_title["Waiting on you -- decide"]]["filterId"]
        == by_name["PROJ -- waiting on you: decide"]
    )
    assert (
        fake.gadget_configs[by_title["This sprint"]]["filterId"]
        == by_name["PROJ -- this sprint"]
    )
    assert (
        fake.gadget_configs[by_title["Open bugs by priority"]]["filterId"]
        == by_name["PROJ -- open bugs by priority"]
    )


def test_dashboard_wiring_by_loop_position_instead_of_key_would_fail_above(monkeypatch):
    """Mutation-shaped control: proves the wiring test discriminates."""
    fake = _DashboardClient()
    monkeypatch.setattr(cli, "_client", lambda config, email: fake)

    from jira_agent_kit import dashboard as dash
    from jira_agent_kit.config import load as load_config
    from jira_agent_kit.schema import Vocabulary

    config = load_config()
    vocab = Vocabulary(config.label_prefix)
    _, _, filters, gadgets = dash.plan(config, vocab)

    filter_ids = {f.key: f"wrong-{i}" for i, f in enumerate(filters)}  # scrambled on purpose
    for g in gadgets:
        gid = fake.add_dashboard_gadget(
            dashboard_id="200", uri=dash.FILTER_RESULTS_GADGET_URI,
            color=g.color, row=g.row, column=g.column, title=g.title,
        )
        # Deliberately wire to a DIFFERENT key than g.filter_key.
        wrong_key = next(k for k in filter_ids if k != g.filter_key)
        fake.configure_gadget("200", gid, dash.gadget_config(filter_ids[wrong_key]))

    by_title = {g["title"]: g["id"] for g in fake.gadgets_added}
    decide_gid = by_title["Waiting on you -- decide"]
    assert fake.gadget_configs[decide_gid]["filterId"] != filter_ids["decide"]


def test_needs_you_filters_require_both_labels_together(monkeypatch):
    fake = _DashboardClient()
    monkeypatch.setattr(cli, "_client", lambda config, email: fake)
    cli.main(["dashboard", "--apply"])
    for f in fake.filters_created:
        if "waiting on you" in f["name"]:
            assert "eng-needs-you" in f["jql"]


# --- whoami --------------------------------------------------------------------------


def test_whoami_reports_the_authenticated_identity(monkeypatch, capsys):
    class FakeClient:
        def myself(self):
            return {"emailAddress": "someone@example.com", "accountId": "1", "active": True}

    monkeypatch.setattr(cli, "_client", lambda config, email: FakeClient())
    assert cli.main(["whoami"]) == cli.EXIT_OK
    assert "someone@example.com" in capsys.readouterr().out


# --- factory-dashboard -----------------------------------------------------------------


def test_factory_dashboard_writes_html_for_an_empty_store(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("JIRA_AGENT_DB_PATH", str(tmp_path / "jae.db"))
    out = tmp_path / "out.html"

    code = cli.main(["factory-dashboard", "--out", str(out)])

    assert code == cli.EXIT_OK
    assert out.exists()
    assert "JAE factory dashboard" in out.read_text()


def test_factory_dashboard_scoped_to_one_repo_excludes_others(monkeypatch, tmp_path):
    monkeypatch.setenv("JIRA_AGENT_DB_PATH", str(tmp_path / "jae.db"))
    store = cli.telemetry.TelemetryStore(tmp_path / "jae.db")
    from datetime import datetime, timezone

    now = datetime(2026, 9, 22, 10, 0, tzinfo=timezone.utc)
    store.record(cli.telemetry.TaskRecord(
        issue_key="A-1", repo="repo-a", session_id="s1", started_at=now, finished_at=now,
        model="m", files_changed=(), test_exit_code=0, contract_exit_code=None,
        human_interactions=0, cost_usd=None,
    ))
    store.record(cli.telemetry.TaskRecord(
        issue_key="B-1", repo="repo-b", session_id="s2", started_at=now, finished_at=now,
        model="m", files_changed=(), test_exit_code=0, contract_exit_code=None,
        human_interactions=0, cost_usd=None,
    ))
    store.close()

    out = tmp_path / "out.html"
    code = cli.main(["factory-dashboard", "--repo", "repo-a", "--out", str(out)])

    assert code == cli.EXIT_OK
    text = out.read_text()
    assert "repo-a" in text
    # scoped to one repo: no per-repo breakdown table beyond the overall row,
    # and the pooled repo-b row must not appear
    assert "repo-b" not in text


# --- finish: judge scoring wiring (JAE v2 spec §5) --------------------------------------


class _StubJudge:
    """Replaces JevJudge entirely -- no network, no real Jev SDK call.
    Returns a fixed result per dimension, settable per test.
    """

    def __init__(self, results):
        self._results = results  # dict[dimension] -> cli.judge_mod.JudgeResult

    def score(self, dimension, state):
        return self._results[dimension]


def _judge_result(dimension, score, confidence, action):
    return cli.judge_mod.JudgeResult(dimension=dimension, score=score, confidence=confidence, action=action)


def test_a_sampled_issue_gets_scored_on_every_dimension(monkeypatch, tmp_path):
    fake = _RecordingClient()
    monkeypatch.setattr(cli, "_client", lambda config, email: fake)
    monkeypatch.setattr(cli.context, "changed_files", lambda rev, repo: ())
    monkeypatch.setattr(cli.telemetry, "repo_name", lambda repo: "test-repo")
    monkeypatch.setenv("JIRA_AGENT_DB_PATH", str(tmp_path / "jae.db"))
    monkeypatch.setenv("JIRA_AGENT_JUDGE_SAMPLE_RATE", "1")  # always sample

    stub = _StubJudge({
        "redundant_tests": _judge_result("redundant_tests", 0.0, 0.95, "write"),
        "scope_creep": _judge_result("scope_creep", 1.0, 0.95, "write"),
        "prediction_accuracy": _judge_result("prediction_accuracy", 0.0, 0.95, "write"),
    })
    monkeypatch.setattr(cli.judge_mod, "JevJudge", lambda: stub)

    cli.main(
        [
            "finish", "PROJ-12", "--test-cmd", "true", "--predicted", "",
            "--accuracy", "Same|x.", "--scalability", "Same|x.", "--maintenance", "Same|x.",
            "--left", "nothing",
        ]
    )

    store = cli.telemetry.TelemetryStore(tmp_path / "jae.db")
    scores = store.scores(repo="test-repo")
    assert {s["dimension"] for s in scores} == {"redundant_tests", "scope_creep", "prediction_accuracy"}


def test_a_discarded_low_confidence_score_is_not_written(monkeypatch, tmp_path):
    fake = _RecordingClient()
    monkeypatch.setattr(cli, "_client", lambda config, email: fake)
    monkeypatch.setattr(cli.context, "changed_files", lambda rev, repo: ())
    monkeypatch.setattr(cli.telemetry, "repo_name", lambda repo: "test-repo")
    monkeypatch.setenv("JIRA_AGENT_DB_PATH", str(tmp_path / "jae.db"))
    monkeypatch.setenv("JIRA_AGENT_JUDGE_SAMPLE_RATE", "1")

    stub = _StubJudge({
        "redundant_tests": _judge_result("redundant_tests", 0.0, 0.2, "discard"),
        "scope_creep": _judge_result("scope_creep", 1.0, 0.95, "write"),
        "prediction_accuracy": _judge_result("prediction_accuracy", 0.0, 0.95, "write"),
    })
    monkeypatch.setattr(cli.judge_mod, "JevJudge", lambda: stub)

    cli.main(
        [
            "finish", "PROJ-12", "--test-cmd", "true", "--predicted", "",
            "--accuracy", "Same|x.", "--scalability", "Same|x.", "--maintenance", "Same|x.",
            "--left", "nothing",
        ]
    )

    store = cli.telemetry.TelemetryStore(tmp_path / "jae.db")
    scores = store.scores(repo="test-repo")
    assert "redundant_tests" not in {s["dimension"] for s in scores}


def test_writing_the_discarded_score_anyway_would_fail_the_test_above(monkeypatch, tmp_path):
    """Mutation-shaped control: proves the assertion above discriminates."""
    fake = _RecordingClient()
    monkeypatch.setattr(cli, "_client", lambda config, email: fake)
    monkeypatch.setattr(cli.context, "changed_files", lambda rev, repo: ())
    monkeypatch.setattr(cli.telemetry, "repo_name", lambda repo: "test-repo")
    monkeypatch.setenv("JIRA_AGENT_DB_PATH", str(tmp_path / "jae.db"))
    monkeypatch.setenv("JIRA_AGENT_JUDGE_SAMPLE_RATE", "1")

    # Simulate the bug: the "discard" action written anyway (mutated to "write").
    stub = _StubJudge({
        "redundant_tests": _judge_result("redundant_tests", 0.0, 0.2, "write"),
        "scope_creep": _judge_result("scope_creep", 1.0, 0.95, "write"),
        "prediction_accuracy": _judge_result("prediction_accuracy", 0.0, 0.95, "write"),
    })
    monkeypatch.setattr(cli.judge_mod, "JevJudge", lambda: stub)

    cli.main(
        [
            "finish", "PROJ-12", "--test-cmd", "true", "--predicted", "",
            "--accuracy", "Same|x.", "--scalability", "Same|x.", "--maintenance", "Same|x.",
            "--left", "nothing",
        ]
    )

    store = cli.telemetry.TelemetryStore(tmp_path / "jae.db")
    scores = store.scores(repo="test-repo")
    assert "redundant_tests" in {s["dimension"] for s in scores}  # confirms the mutation is visible


def test_an_unsampled_issue_is_not_scored_at_all(monkeypatch, tmp_path):
    fake = _RecordingClient()
    monkeypatch.setattr(cli, "_client", lambda config, email: fake)
    monkeypatch.setattr(cli.context, "changed_files", lambda rev, repo: ())
    monkeypatch.setattr(cli.telemetry, "repo_name", lambda repo: "test-repo")
    monkeypatch.setenv("JIRA_AGENT_DB_PATH", str(tmp_path / "jae.db"))
    monkeypatch.setenv("JIRA_AGENT_JUDGE_SAMPLE_RATE", "0")  # never sample

    def fail_if_constructed():
        pytest.fail("JevJudge constructed for an unsampled issue")

    monkeypatch.setattr(cli.judge_mod, "JevJudge", fail_if_constructed)

    code = cli.main(
        [
            "finish", "PROJ-12", "--test-cmd", "true", "--predicted", "",
            "--accuracy", "Same|x.", "--scalability", "Same|x.", "--maintenance", "Same|x.",
            "--left", "nothing",
        ]
    )

    assert code == cli.EXIT_OK  # no judge call attempted, no crash either


def test_finish_still_succeeds_when_judge_scoring_raises(monkeypatch, tmp_path):
    fake = _RecordingClient()
    monkeypatch.setattr(cli, "_client", lambda config, email: fake)
    monkeypatch.setattr(cli.context, "changed_files", lambda rev, repo: ())
    monkeypatch.setattr(cli.telemetry, "repo_name", lambda repo: "test-repo")
    monkeypatch.setenv("JIRA_AGENT_DB_PATH", str(tmp_path / "jae.db"))
    monkeypatch.setenv("JIRA_AGENT_JUDGE_SAMPLE_RATE", "1")

    class RaisingJudge:
        def score(self, dimension, state):
            raise RuntimeError("simulated Jev outage")

    monkeypatch.setattr(cli.judge_mod, "JevJudge", lambda: RaisingJudge())

    code = cli.main(
        [
            "finish", "PROJ-12", "--test-cmd", "true", "--predicted", "",
            "--accuracy", "Same|x.", "--scalability", "Same|x.", "--maintenance", "Same|x.",
            "--left", "nothing",
        ]
    )

    assert code == cli.EXIT_OK
    assert len(fake.comments) == 1  # the report still posted
    # the telemetry row still recorded -- judge's failure doesn't unwind it
    store = cli.telemetry.TelemetryStore(tmp_path / "jae.db")
    assert len(store.tasks(repo="test-repo")) == 1


def test_factory_dashboard_pooled_view_shows_the_breakdown(monkeypatch, tmp_path):
    monkeypatch.setenv("JIRA_AGENT_DB_PATH", str(tmp_path / "jae.db"))
    store = cli.telemetry.TelemetryStore(tmp_path / "jae.db")
    from datetime import datetime, timezone

    now = datetime(2026, 9, 22, 10, 0, tzinfo=timezone.utc)
    store.record(cli.telemetry.TaskRecord(
        issue_key="A-1", repo="repo-a", session_id="s1", started_at=now, finished_at=now,
        model="m", files_changed=(), test_exit_code=0, contract_exit_code=None,
        human_interactions=0, cost_usd=None,
    ))
    store.record(cli.telemetry.TaskRecord(
        issue_key="B-1", repo="repo-b", session_id="s2", started_at=now, finished_at=now,
        model="m", files_changed=(), test_exit_code=0, contract_exit_code=None,
        human_interactions=0, cost_usd=None,
    ))
    store.close()

    out = tmp_path / "out.html"
    code = cli.main(["factory-dashboard", "--out", str(out)])

    assert code == cli.EXIT_OK
    text = out.read_text()
    assert "repo-a" in text
    assert "repo-b" in text


# --- factory-version / replay (JAE v2 spec §7) -------------------------------------------


@pytest.fixture
def git_repo(tmp_path):
    import subprocess
    path = tmp_path / "repo"
    path.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.email", "t@example.com"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=path, check=True)
    (path / "a.txt").write_text("x")
    subprocess.run(["git", "add", "a.txt"], cwd=path, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=path, check=True)
    return path


def test_factory_version_tag_creates_a_tag(git_repo, capsys):
    code = cli.main(["--repo", str(git_repo), "factory-version", "tag", "v2", "--note", "risk field added"])

    assert code == cli.EXIT_OK
    assert "factory-v2" in capsys.readouterr().out


def test_factory_version_tagging_twice_is_misuse_not_a_crash(git_repo, capsys):
    cli.main(["--repo", str(git_repo), "factory-version", "tag", "v2", "--note", "first"])

    code = cli.main(["--repo", str(git_repo), "factory-version", "tag", "v2", "--note", "second"])

    assert code == cli.EXIT_MISUSE
    assert "already exists" in capsys.readouterr().err


def test_factory_version_list_shows_every_tag(git_repo, capsys):
    cli.main(["--repo", str(git_repo), "factory-version", "tag", "v1", "--note", "baseline"])
    cli.main(["--repo", str(git_repo), "factory-version", "tag", "v2", "--note", "next"])

    code = cli.main(["--repo", str(git_repo), "factory-version", "list"])

    assert code == cli.EXIT_OK
    out = capsys.readouterr().out
    assert "factory-v1" in out and "factory-v2" in out


def test_factory_version_list_with_no_tags_says_so(git_repo, capsys):
    code = cli.main(["--repo", str(git_repo), "factory-version", "list"])

    assert code == cli.EXIT_OK
    assert "no factory" in capsys.readouterr().out.lower()


def test_replay_reports_the_delta_between_two_scored_versions(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("JIRA_AGENT_DB_PATH", str(tmp_path / "jae.db"))
    store = cli.telemetry.TelemetryStore(tmp_path / "jae.db")
    store.record_score(
        repo="repo-a", issue_key="A-1", session_id="s1", dimension="redundant_tests",
        score=1.0, confidence=0.9, factory_version=None,
    )
    store.record_score(
        repo="repo-a", issue_key="A-1", session_id="s1", dimension="redundant_tests",
        score=0.2, confidence=0.9, factory_version="factory-v2",
    )
    store.close()

    code = cli.main(
        [
            "replay", "--repo-name", "repo-a", "--issue", "A-1", "--session", "s1",
            "--dimension", "redundant_tests", "--to-version", "factory-v2",
        ]
    )

    assert code == cli.EXIT_OK
    out = capsys.readouterr().out
    assert "1.00" in out and "0.20" in out and "-0.80" in out


def test_replay_with_a_missing_score_is_misuse_not_a_crash(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("JIRA_AGENT_DB_PATH", str(tmp_path / "jae.db"))

    code = cli.main(
        [
            "replay", "--repo-name", "repo-a", "--issue", "A-1", "--session", "s1",
            "--dimension", "redundant_tests", "--to-version", "factory-v2",
        ]
    )

    assert code == cli.EXIT_MISUSE
    assert "no redundant_tests score" in capsys.readouterr().err
