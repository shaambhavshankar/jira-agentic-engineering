# Setup

Five things, in order. Each one was learned the hard way once; the note
under it says how.

## 1. Install

```bash
pip install -e .
# or, from anywhere, once installed:
jira-agent --help
```

Requires Python 3.11+ and `httpx`. That's the entire dependency list.

## 2. Get an Atlassian API token

Go to `id.atlassian.com/manage-profile/security/api-tokens` and create one.
Copy it — you won't see it again.

**The account this token belongs to is the account every issue, comment,
and label will be attributed to.** Use a real human account, or a
dedicated bot account if your org has one for this purpose. This kit does
not support OAuth app-style credentials; it uses HTTP Basic auth with an
email + API token, the same as `curl -u email:token`.

## 3. Store the token in the macOS Keychain

```bash
security add-generic-password -U -s jira-agent-<project-key-lowercased> \
  -a your-email@company.com -w "$(pbpaste | tr -d '\n')"
```

**The value MUST be passed as the `-w` argument, exactly like this.** Do
not omit `-w`'s value and let `security` prompt you interactively, and do
not pipe the token into it.

Here is why, discovered the hard way while building this kit: `security`
truncates a value at 128 characters when it reads from its own interactive
prompt or from stdin. An Atlassian API token is about 192 characters. Type
it at the prompt, and both your entry and the retype are truncated to the
same 128 characters — so `security` sees them match, accepts the write,
and reports success. The Keychain now holds a truncated token that will
fail every real request with a `401`, and nothing about the failed
`security` call told you this happened. The fix is the `-w "$(pbpaste | tr
-d '\n')"` form above, which passes the full value as a single argument.

**The account passed to `-a` is the ATLASSIAN account email**, which may
not be the email your shell, your CI runner, or your git config uses. If
`jira-agent whoami` returns a `401`, this mismatch is the first thing to
check — Jira answers many wrong-credential and wrong-account requests with
a `404` on unrelated endpoints rather than a `401`, so a login problem can
look exactly like "that issue doesn't exist."

On Linux or another OS without the macOS Keychain, replace
`client.read_token` with your own secret-store lookup — it's a five-line
function.

## 4. Set the required environment variables

```bash
export JIRA_AGENT_SITE=yourcompany.atlassian.net
export JIRA_AGENT_PROJECT_KEY=PROJ
export JIRA_AGENT_EMAIL=your-email@company.com
export JIRA_AGENT_LABEL_PREFIX=eng
```

All four are required; `jira-agent` refuses to run with a clear message
naming exactly which are missing, rather than guessing at a default that
would silently point at the wrong Jira site.

`JIRA_AGENT_LABEL_PREFIX` matters more than it looks. Jira labels are one
flat, global namespace shared by every project and every team on your
site. Without a prefix, a label like `needs-you` could already mean
something to an unrelated team. Pick something short and specific to your
team or project — `eng`, `data`, `ml` — not something generic like `dev`.

An optional fifth variable:

```bash
export JIRA_AGENT_KEYCHAIN_SERVICE=jira-agent-proj  # defaults to this
```

## 5. Wire up the hooks (optional, but where the automatic half comes from)

**SessionStart** — put your agent's SessionStart config to run
`hooks/session_context.sh`. It reads `JIRA_AGENT_PROJECT_KEY` and the
current branch name; if the branch contains an issue key from your
project, it fetches that issue and injects it as session context, framed
explicitly as untrusted data (see the comment in the script for why that
framing matters).

**post-commit** — install `hooks/post_commit.sh` as (or chained into)
`.git/hooks/post-commit`. It posts a cheap commit trail — sha, subject,
files changed — to whichever issue the commit subject names, entirely in
the background, and never runs your test suite (that's what `jira-agent
finish` is for, run deliberately).

```bash
# If your repo has no existing post-commit hook:
ln -s "$(pwd)/hooks/post_commit.sh" .git/hooks/post-commit

# If it already has one, CHAIN instead of overwriting it:
cat > .git/hooks/post-commit <<'HOOK'
#!/usr/bin/env bash
TOP="$(git rev-parse --show-toplevel)"
[[ -x "${TOP}/your/existing/hook.sh" ]] && "${TOP}/your/existing/hook.sh" || true
[[ -x "${TOP}/hooks/post_commit.sh" ]] && "${TOP}/hooks/post_commit.sh" || true
exit 0
HOOK
chmod +x .git/hooks/post-commit
```

`.git/hooks/` is not version-controlled by git itself, so this step has to
be repeated by every clone. If you use `pre-commit` or a similar framework
already, wire `hooks/post_commit.sh` in as one more hook there instead —
it's a plain script with no framework dependency.

## Verifying it worked

```bash
jira-agent whoami
```

Prints the authenticated account, its `accountId`, and whether it's
active. This is the single fastest way to confirm steps 2–4 are all
correct, and it's the endpoint to check FIRST if anything else returns a
confusing 404.
