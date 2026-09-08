"""The shell entry points, run for real. Nothing here may reach the network."""

import os
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SESSION_HOOK = ROOT / "hooks/session_context.sh"
POST_COMMIT = ROOT / "hooks/post_commit.sh"
BOTH_HOOKS = (SESSION_HOOK, POST_COMMIT)


def _env(project_key="PROJ"):
    env = dict(os.environ)
    env["JIRA_AGENT_PROJECT_KEY"] = project_key
    return env


def _git_repo(tmp_path, subject="chore: no issue key here"):
    def run(*args):
        subprocess.run(args, cwd=tmp_path, check=True, capture_output=True)

    run("git", "init", "-q", "-b", "main")
    run("git", "config", "user.email", "t@t")
    run("git", "config", "user.name", "t")
    (tmp_path / "a.txt").write_text("a\n")
    run("git", "add", "-A")
    run("git", "commit", "-qm", subject)
    return tmp_path


@pytest.mark.parametrize("script", BOTH_HOOKS, ids=lambda p: p.name)
def test_every_hook_is_executable(script):
    assert os.access(script, os.X_OK), f"{script} is not executable"


@pytest.mark.parametrize("script", BOTH_HOOKS, ids=lambda p: p.name)
def test_a_hook_exits_zero_with_no_project_key_configured(script, tmp_path):
    env = dict(os.environ)
    env.pop("JIRA_AGENT_PROJECT_KEY", None)
    done = subprocess.run([str(script)], cwd=tmp_path, capture_output=True, text=True, timeout=60, env=env)
    assert done.returncode == 0


@pytest.mark.parametrize("script", BOTH_HOOKS, ids=lambda p: p.name)
def test_a_hook_exits_zero_outside_a_git_repository(script, tmp_path):
    done = subprocess.run([str(script)], cwd=tmp_path, capture_output=True, text=True, timeout=60, env=_env())
    assert done.returncode == 0, done.stderr


def test_the_post_commit_hook_is_silent_when_the_subject_names_no_issue(tmp_path):
    repo = _git_repo(tmp_path)
    done = subprocess.run([str(POST_COMMIT)], cwd=repo, capture_output=True, text=True, timeout=60, env=_env())
    assert done.returncode == 0
    assert not (repo / ".jira-agent").exists(), "spawned work for a commit with no key"


def test_the_post_commit_hook_ignores_a_key_inside_a_longer_prefix(tmp_path):
    # `XPROJ-4` is not `PROJ-4`.
    repo = _git_repo(tmp_path, subject="chore: XPROJ-4 not our project")
    done = subprocess.run([str(POST_COMMIT)], cwd=repo, capture_output=True, text=True, timeout=60, env=_env())
    assert done.returncode == 0
    assert not (repo / ".jira-agent").exists()


def test_the_session_hook_is_silent_on_a_branch_with_no_issue_key(tmp_path):
    repo = _git_repo(tmp_path)
    done = subprocess.run([str(SESSION_HOOK)], cwd=repo, capture_output=True, text=True, timeout=60, env=_env())
    assert done.returncode == 0
    assert done.stdout.strip() == ""


def test_the_session_hook_frames_jira_content_as_untrusted():
    body = SESSION_HOOK.read_text()
    assert "UNTRUSTED DATA" in body
    assert "--- begin Jira content ---" in body
    assert "--- end Jira content ---" in body


def test_the_post_commit_hook_resolves_head_before_backgrounding():
    body = POST_COMMIT.read_text()
    assert 'SHA="$(git rev-parse HEAD' in body
    assert "--rev HEAD" not in body


def test_the_live_jira_guard_actually_fires():
    """The control for tests/conftest.py's network guard.

    A guard nobody has seen fire is a guard nobody knows works. This builds
    a client the way production code does -- with no transport -- and
    requires the attempt to be refused rather than sent.
    """
    from jira_agent_kit.client import JiraClient

    live = JiraClient(site="example.invalid", email="t@example.com", token="x")
    with pytest.raises(AssertionError) as caught:
        live.get_issue("PROJ-1")
    assert "tried to reach the network" in str(caught.value)
