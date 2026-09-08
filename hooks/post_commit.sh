#!/usr/bin/env bash
# Post the cheap commit trail to the Jira issue named in the commit subject.
#
# WHY THIS IS CHEAP. It runs `git` and one HTTP request. It does NOT run
# your test suite: a background job that size firing on every commit, in
# every worktree, is not a trail, it is a resource leak. Worse, its result
# would be attributed to a commit a later commit may already have
# superseded. The full finish report (`jira-agent finish`) is written
# deliberately, by whoever actually ran the measurements.
#
# ALWAYS EXITS 0.
#
# INSTALLING THIS. If your repo already has a post-commit hook, chain this
# into it rather than overwriting it:
#
#   TOP="$(git rev-parse --show-toplevel)"
#   [[ -x "${TOP}/hooks/post_commit.sh" ]] && "${TOP}/hooks/post_commit.sh" || true
#
# `.git/hooks/` is NOT version-controlled, so a symlink or an overwrite here
# silently deletes whatever else your repo's post-commit hook was doing, and
# nothing will detect the loss.
#
# REQUIRES: JIRA_AGENT_PROJECT_KEY set in the environment.
set -uo pipefail

PROJECT_KEY="${JIRA_AGENT_PROJECT_KEY:-}"
[[ -z "${PROJECT_KEY}" ]] && exit 0

TOP="$(git rev-parse --show-toplevel 2>/dev/null)" || exit 0
cd "${TOP}" || exit 0

SUBJECT="$(git log -1 --pretty=%s 2>/dev/null)"

# Word-boundaried, matching context.py's pattern. A bare
# `grep -qE "${PROJECT_KEY}-[0-9]+"` also fires inside a longer prefix
# ending the same way, spawning a background job guaranteed to do nothing.
if [[ ! "${SUBJECT}" =~ (^|[^A-Za-z0-9])${PROJECT_KEY}-[0-9]+([^0-9]|$) ]]; then
  exit 0
fi

# Resolve HEAD NOW, not when the background job starts. A commit is passed
# the SYMBOLIC "HEAD" to a background job, and a second commit landing
# first would make the trail describe a different commit than the one that
# triggered it.
SHA="$(git rev-parse HEAD 2>/dev/null)" || exit 0

mkdir -p .jira-agent
LOG=".jira-agent/last-error.log"

# Capped, appended. An uncapped log is a slow leak; `>` (truncate) on every
# commit destroys the record of why a PREVIOUS run failed, at exactly the
# moment that record is wanted.
if [[ -f "${LOG}" ]] && (( $(wc -c <"${LOG}") > 262144 )); then
  tail -c 131072 "${LOG}" >"${LOG}.tmp" && mv "${LOG}.tmp" "${LOG}"
fi

# Backgrounded: a Jira round trip must never sit between a developer and
# their next command.
nohup jira-agent comment --rev "${SHA}" >>"${LOG}" 2>&1 &
exit 0
