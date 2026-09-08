# jira-agent-kit

Give a coding agent a Jira presence: it creates issues with a real problem
statement and real alternatives considered, reads context in automatically
when a session starts, writes a commit trail out automatically, and you get
a small dashboard showing exactly what's blocked on you and why.

Extracted from a real internal integration, genericized so any team can
point it at their own Jira site. No mentions of the original project
remain; every identifier, label prefix, and example below is a placeholder
you configure.

## What it actually does

**Every issue has a real template**, not a one-line summary:

```
## What is broken, or what is missing
<the problem>

## Ways to fix it
Option 1 - ...
   Good: ...
   Bad: ...
Option 2 - ...
   Good: ...
   Bad: ...
Chosen: Option N, because ...

## What this does to the three things that matter
Accuracy of results - Better | Same | Worse. <sentence>
Scalability         - Better | Same | Worse. <sentence>
Ease of maintenance - Better | Same | Worse. <sentence>
```

At least two options, always — a lone option is how a decision gets made
without anyone noticing a decision was made. Three axes stated as a
one-word verdict plus one sentence — predicted before work starts, measured
again when it's done, and the *gap* between the two is the actual finding.
Rename the three axes in `schema.py` if your team's real priorities are
different; the mechanism is what matters, not these particular three
words.

**Labels are a closed vocabulary, code-enforced**, because Jira itself
enforces nothing — any string is accepted, so a typo silently creates a
second bucket that never merges back into the first. Every label is
namespaced under a prefix you choose, because labels are one flat
namespace shared by every team on your Jira site.

**Three layers, each working independently:**

1. A **skill** (`skills/jira/SKILL.md`) — the rules an agent follows when
   creating or updating an issue.
2. A **SessionStart hook** — reads the branch name, and if it names an
   issue from your project, fetches it and injects it as session context.
3. A **post-commit hook** — posts a cheap commit trail (sha, subject, files
   changed) to whichever issue the commit subject names, in the
   background, never blocking a commit.

**A "where to look" dashboard**, built entirely from saved Jira filters and
stock gadgets — no custom app, no plugin. Splits "blocked on a human" into
three different asks instead of one flat flag:

| Label | Means |
|---|---|
| `<prefix>-needs-decide` | pick between real options |
| `<prefix>-needs-provide` | hand over an input or credential |
| `<prefix>-needs-go` | already decided, just needs a green light |

Three different response times, hidden by any system that only tracks "is
this blocked". This particular idea is not original — the pattern was
observed on another team's Jira dashboard and reproduced here with the
mechanism read directly from its filter JQL and gadget configuration,
rather than guessed at from the dashboard's title.

## Quickstart

```bash
pip install -e .
```

Then follow `docs/SETUP.md` — five steps, each with the specific mistake it
prevents. The short version:

```bash
export JIRA_AGENT_SITE=yourcompany.atlassian.net
export JIRA_AGENT_PROJECT_KEY=PROJ
export JIRA_AGENT_EMAIL=you@yourcompany.com
export JIRA_AGENT_LABEL_PREFIX=eng

security add-generic-password -U -s jira-agent-proj -a you@yourcompany.com \
  -w "$(pbpaste | tr -d '\n')"   # token copied to clipboard first

jira-agent whoami   # confirms it all worked
```

## Commands

```
jira-agent create --type task --summary "..." --problem "..." \
  --option "name|good|bad" --option "name|good|bad" --chosen "..." \
  --accuracy "Same|..." --scalability "Same|..." --maintenance "Better|..." \
  --layer backend --goal feature

jira-agent show PROJ-12
jira-agent comment PROJ-12 --rev HEAD
jira-agent block PROJ-12 --need "..." --kind decide
jira-agent start PROJ-12 --target x --plan "..." --files "a.py,b.py" ...
jira-agent finish PROJ-12 --test-cmd "..." --predicted "a.py" --left "..." ...
jira-agent transition PROJ-12 --to Done
jira-agent sprints
jira-agent dashboard [--apply]
jira-agent whoami
jira-agent lint some-file.md
```

Full reference in `skills/jira/SKILL.md`.

## Why this exists as a genericized, standalone kit

The original integration hardcoded a real Jira site, a real project key, a
real personal email, and a Jira-instance-specific issue-type-id map. That
last one is not a cosmetic detail: **issue type ids are not fixed across
Jira sites, and are not even fixed across two projects on the same site.**
A team-managed Jira project gets its own locally generated copies of
Epic/Task/Bug/Subtask. Code that hardcodes `"11205"` for "Task" works for
exactly one project and breaks silently — or loudly, with a 404 that gives
no hint why — on anyone else's.

This kit resolves issue types by name at runtime instead
(`resolve_issue_type_id`), and resolves the Sprint and Story-points custom
fields by their fixed Jira Software system type rather than by a
display name an admin could rename (`find_field_by_custom_type`). Both were
verified against a real Jira site before being written this way, not
assumed correct from documentation.

## A real incident, and why the test suite is built the way it is

An early version of this kit's test suite created five real issues in a
production Jira project. One test invoked the `block` command with an
argument the label vocabulary happened to accept at the time, and the test
was not given a fake transport. Every time the suite ran, it read a real
API token out of the Keychain and posted a real issue. Nobody noticed for
five runs.

The fix was not "write a better test" — a codebase with hundreds of tests
will eventually have one that forgets to isolate itself, no matter how
careful anyone is. The fix was to make it **structurally impossible** for
any test in this suite to reach the real network by accident:
`tests/conftest.py` patches the underlying transport for every test,
raising instead of connecting, with exactly one narrow, deliberate
exception (`@pytest.mark.jira_live`, itself gated behind an environment
variable that must be set on purpose every time). There is a test that
proves this guard actually fires — a guard nobody has seen trip is a guard
nobody actually knows works.

If you extend this kit, keep that shape: a credential this powerful, held
by a test suite that runs unattended and often, needs a structural
guarantee, not a habit.

## License

MIT — see `LICENSE`. Use it, fork it, point it at your own Jira, change
the label taxonomy to whatever your team actually needs.

## What this kit does not do

- **No sprints/board setup for you.** `jira-agent create --sprint`/`--priority`
  work once your Jira admin has Estimation and Sprints turned on for your
  project — that toggle is a Jira project-settings action, not something
  reachable through the REST API.
- **No blast-radius analysis of its own.** `jira-agent start --impact-cmd`
  is a pluggable hook for whatever static-analysis tool you already have;
  this kit ships no such tool itself.
- **No status-transition automation.** Moving an issue through its
  workflow (`transition`) is always something you or your agent does on
  purpose, never something a hook does silently.
