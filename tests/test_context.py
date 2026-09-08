"""Repo facts, and the one rule that does not bend: pass/fail is the exit code.

`security ... | head` returns head's status. `pytest ... | tail` returns
tail's. Reading the text instead of the code is how a failing suite gets
reported green, so read_test_result takes the code as authoritative and
treats the counts as decoration.
"""

import subprocess

import pytest

from jira_agent_kit.context import (
    CONTRACT_MEANING,
    area_labels,
    changed_files,
    current_branch,
    head_sha,
    issue_key_from,
    make_issue_key_pattern,
    read_test_result,
    run_command,
    run_impact,
)

PATTERN = make_issue_key_pattern("PROJ")


@pytest.mark.parametrize(
    "text,expected",
    [
        ("feat: PROJ-12 add the thing", "PROJ-12"),
        ("agent/PROJ-3-label-vocabulary", "PROJ-3"),
        ("no key here", None),
        ("OTHERPROJ-4 is a different project", None),
        ("lowercase proj-9 does not count", None),
        ("PROJ-12 and PROJ-13 -- first wins", "PROJ-12"),
    ],
)
def test_issue_key_parsing(text, expected):
    assert issue_key_from(text, PATTERN) == expected


def test_the_pattern_does_not_match_inside_a_longer_project_prefix():
    # `XPROJ-4` is not `PROJ-4`. A bare, non-word-bounded regex would match
    # inside it, posting a comment to the wrong project's issue.
    xpattern = make_issue_key_pattern("PROJ")
    assert issue_key_from("XPROJ-4 not our project", xpattern) is None


def test_area_labels_come_from_the_top_level_directory():
    assert area_labels(["src/api/db.py", "tests/context_test.py", "src/cli.py"], "eng") == (
        "eng-area-src",
        "eng-area-tests",
    )


def test_area_labels_have_no_allow_list_any_top_level_dir_works():
    # Unlike an earlier version of this kit tied to one repo's own layout,
    # there is nothing to keep in sync: whatever the real directory is
    # becomes the label.
    assert area_labels(["whatever-you-call-it/thing.py"], "eng") == ("eng-area-whatever-you-call-it",)


def test_a_result_is_failing_when_the_exit_code_is_nonzero_whatever_the_text_says():
    result = read_test_result(command="pytest", exit_code=1, output="12 passed in 4.10s")
    assert result.ok is False
    assert result.exit_code == 1


def test_counts_are_parsed_when_present_and_none_when_not():
    parsed = read_test_result("pytest", 0, "collected 39 items\n39 passed in 8s")
    assert (parsed.collected, parsed.passed) == (39, 39)

    silent = read_test_result("pytest -qq", 0, "")
    assert (silent.collected, silent.passed) == (None, None)
    assert silent.ok is True  # exit code decides, not the missing summary


def test_contract_exit_codes_are_the_documented_four():
    assert set(CONTRACT_MEANING) == {0, 1, 2, 3}
    assert CONTRACT_MEANING[0].startswith("pass")


# --- run_impact: the exit code of a blast-radius tool can lie -----------------


def test_run_impact_reads_the_json_body_not_just_the_exit_code(monkeypatch):
    # Some blast-radius tools exit 0 EVEN WHEN THE TARGET IS NOT FOUND --
    # the failure lives in the JSON body under "error". Trusting the exit
    # code alone would report every miss as a clean, risk-free result.
    def fake_run(cmd, **kwargs):
        class Done:
            returncode = 0
            stdout = '{"error": "Target \\u0027ghost\\u0027 not found", "risk": "UNKNOWN"}'
            stderr = ""

        return Done()

    monkeypatch.setattr("jira_agent_kit.context.subprocess.run", fake_run)
    result = run_impact(["mytool", "impact", "ghost"])
    assert result.found is False
    assert "not found" in result.error


def test_run_impact_parses_a_real_result(monkeypatch):
    def fake_run(cmd, **kwargs):
        class Done:
            returncode = 0
            stdout = '{"risk": "LOW", "impactedCount": 2, "summary": {"direct": 1}}'
            stderr = ""

        return Done()

    monkeypatch.setattr("jira_agent_kit.context.subprocess.run", fake_run)
    result = run_impact(["mytool", "impact", "thing"])
    assert (result.found, result.risk, result.impacted_count, result.direct) == (True, "LOW", 2, 1)


def test_run_impact_risk_label_maps_only_known_risk_names():
    from jira_agent_kit.context import ImpactResult

    known = frozenset({"eng-risk-low", "eng-risk-high"})
    assert ImpactResult(True, "HIGH", 5, 2, None).risk_label("eng", known) == "eng-risk-high"
    assert ImpactResult(True, "UNKNOWN", 0, 0, None).risk_label("eng", known) is None
    assert ImpactResult(False, None, 0, 0, "x").risk_label("eng", known) is None


def test_run_impact_when_the_binary_is_missing(monkeypatch):
    def no_binary(*args, **kwargs):
        raise FileNotFoundError(2, "No such file or directory", "mytool")

    monkeypatch.setattr("jira_agent_kit.context.subprocess.run", no_binary)
    result = run_impact(["mytool", "impact", "x"])
    assert result.found is False
    assert "mytool" in result.error


def test_run_impact_on_unparseable_output(monkeypatch):
    def fake_run(cmd, **kwargs):
        class Done:
            returncode = 1
            stdout = ""
            stderr = "some crash trace"

        return Done()

    monkeypatch.setattr("jira_agent_kit.context.subprocess.run", fake_run)
    result = run_impact(["mytool", "impact", "x"])
    assert result.found is False
    assert result.error


def test_run_command_captures_exit_code_and_output():
    result = run_command(["python3", "-c", "import sys; print('hi'); sys.exit(3)"])
    assert result.exit_code == 3
    assert "hi" in result.output


def test_run_command_merges_stderr_into_output():
    result = run_command(["python3", "-c", "import sys; print('oops', file=sys.stderr)"])
    assert "oops" in result.output


# --- git helpers ---------------------------------------------------------------


def _repo_with_a_merge(tmp_path):
    def run(*args):
        subprocess.run(args, cwd=tmp_path, check=True, capture_output=True)

    run("git", "init", "-q", "-b", "main")
    run("git", "config", "user.email", "t@t")
    run("git", "config", "user.name", "t")
    (tmp_path / "a.txt").write_text("a\n")
    run("git", "add", "-A")
    run("git", "commit", "-qm", "base")
    run("git", "checkout", "-qb", "side")
    (tmp_path / "b.txt").write_text("b\n")
    run("git", "add", "-A")
    run("git", "commit", "-qm", "side")
    run("git", "checkout", "-q", "main")
    (tmp_path / "c.txt").write_text("c\n")
    run("git", "add", "-A")
    run("git", "commit", "-qm", "main2")
    run("git", "merge", "--no-ff", "-m", "merge side", "side")
    return tmp_path


def test_git_helpers_work_in_a_real_repo(tmp_path):
    def run(*args):
        subprocess.run(args, cwd=tmp_path, check=True, capture_output=True)

    run("git", "init", "-q", "-b", "PROJ-7-branch")
    run("git", "config", "user.email", "t@t")
    run("git", "config", "user.name", "t")
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "a.py").write_text("x = 1\n")
    run("git", "add", "-A")
    run("git", "commit", "-qm", "PROJ-7 first")

    assert current_branch(tmp_path) == "PROJ-7-branch"
    assert len(head_sha(tmp_path)) == 40
    assert changed_files("HEAD", tmp_path) == ("src/a.py",)
    assert area_labels(changed_files("HEAD", tmp_path), "eng") == ("eng-area-src",)


def test_detached_head_reports_an_empty_branch(tmp_path):
    def run(*args):
        subprocess.run(args, cwd=tmp_path, check=True, capture_output=True)

    run("git", "init", "-q", "-b", "main")
    run("git", "config", "user.email", "t@t")
    run("git", "config", "user.name", "t")
    (tmp_path / "a.txt").write_text("x\n")
    run("git", "add", "-A")
    run("git", "commit", "-qm", "first")
    sha = head_sha(tmp_path)
    run("git", "checkout", "-q", sha)
    assert current_branch(tmp_path) == ""


def test_changed_files_reports_a_merge_commit(tmp_path):
    # `git show --name-only` prints NOTHING for a merge, so a naive
    # implementation would report "no files changed" for every merge.
    repo = _repo_with_a_merge(tmp_path)
    assert changed_files("HEAD", repo) == ("b.txt",)


def test_changed_files_still_reports_an_ordinary_commit(tmp_path):
    repo = _repo_with_a_merge(tmp_path)
    assert changed_files("HEAD^", repo) == ("c.txt",)


def test_changed_files_ignores_a_rename_as_a_pair(tmp_path):
    def run(*args):
        subprocess.run(args, cwd=tmp_path, check=True, capture_output=True)

    run("git", "init", "-q", "-b", "main")
    run("git", "config", "user.email", "t@t")
    run("git", "config", "user.name", "t")
    (tmp_path / "old.txt").write_text("x" * 200 + "\n")
    run("git", "add", "-A")
    run("git", "commit", "-qm", "base")
    run("git", "mv", "old.txt", "new.txt")
    run("git", "commit", "-qm", "rename")
    # --no-renames: git reports BOTH sides. With rename detection it would
    # report one path and the trail would silently lose the deletion.
    assert set(changed_files("HEAD", tmp_path)) == {"old.txt", "new.txt"}


def test_the_revision_is_followed_by_a_path_separator(monkeypatch):
    """`rev` must be marked as ending the revision list, not the option list.

    Asserted structurally, on the argv. The obvious behavioural test --
    pass a revision starting with a dash and expect an error -- does not
    discriminate: git fails either way, once as an unknown flag and once as
    a missing path.
    """
    seen = {}

    def fake_run(cmd, **kwargs):
        seen["cmd"] = cmd

        class Done:
            returncode = 0
            stdout = ""
            stderr = ""

        return Done()

    monkeypatch.setattr("jira_agent_kit.context.subprocess.run", fake_run)
    changed_files("HEAD", ".")
    assert "--" in seen["cmd"]
    assert seen["cmd"].index("HEAD") < seen["cmd"].index("--")
