---
name: jira
description: Use when creating or updating a Jira issue for this project - tasks, epics, bugs, labels, the description template, or the plain-language copy rule. Triggers on "make a ticket", "log this", "what am I blocked on", or starting work on a branch named after an issue.
---

# The Jira record for this project

Configured entirely through environment variables -- see `docs/SETUP.md`.
Nothing below names a specific project, site, or company; that is the whole
point.

## When an issue is warranted

One issue per unit of work that someone could pick up alone. Not one per
commit, and not one per file.

- **Epic** - a major initiative.
- **Task** - work that adds or changes behaviour.
- **Bug** - behaviour that is already wrong.
- **Subtask** - a piece of a Task, always created with `--parent`.

## Every issue carries

Labels, from the closed vocabulary in `schema.py`:

- exactly one layer: `frontend` `backend` `data` `infra`
- exactly one goal: `feature` `perf` `accuracy` `cleanup` `docs`
- exactly one origin: `by-agent` `by-human`
- at most one risk, at most one needs-kind
- area labels, derived from the files touched -- never typed by hand

A label outside the vocabulary is rejected before any request is sent. If
you need a new one, add it to `schema.py` with a test, in its own commit.

A description with all four sections. At least two options, always. If the
fix is forced, say why it is forced and give the alternative you rejected. A
lone option is how a decision gets made without anyone noticing a decision
was made.

The three axes -- accuracy of results, scalability, ease of maintenance --
each as one word (Better, Same, Worse) plus one sentence. Predicted in the
description. Measured again when the work is done. **The gap between the
two is the finding**, so do not quietly edit the prediction to match the
outcome. Rename these three axes in `schema.py` if your team's actual
priorities differ; what matters is a small, fixed set stated every time.

## Copy rule

Short sentences, common words, readable by someone with no context on this
project.

Never simplified: file paths, symbol names, commands, exact error strings,
commit SHAs, exit codes. A stack trace rewritten for a five-year-old stops
being a stack trace, and a paraphrased error string can no longer be
searched.

`jira-agent lint <file>` warns. It is a heuristic. It will pass sentences
that are still hard to read, so it is not permission to stop reading your
own copy.

## Creating an issue

```
jira-agent create \
  --type task --summary "..." --problem "..." \
  --option "name|good|bad" --option "name|good|bad" \
  --chosen "Option 1, because ..." \
  --accuracy "Same|..." --scalability "Same|..." --maintenance "Better|..." \
  --layer backend --goal feature
```

## Starting work

```
jira-agent start PROJ-12 \
  --target symbolName --direction upstream \
  --plan "..." --files "a.py,b.py" \
  --accuracy "Same|..." --scalability "Same|..." --maintenance "Better|..." \
  [--impact-cmd "yourtool impact {target} --direction {direction}"]
```

`--impact-cmd` is optional and pluggable. Say your tool prints JSON with
`risk`/`impactedCount`/`summary.direct` keys. Wire its command line in with
`{target}`/`{direction}` placeholders, and its result is posted and
labelled automatically. No tool, no problem: this posts the plan and files,
just without a blast-radius section.

## Finishing work

```
jira-agent finish PROJ-12 \
  --test-cmd "your test command" --predicted "a.py,b.py" \
  --accuracy "Same|..." --scalability "Same|..." --maintenance "Better|..." \
  --left "nothing" \
  [--contract-cmd "an optional second check"]
```

Runs the test command, and `--contract-cmd` if given, FOR REAL. It reads
their EXIT CODES, never their printed text -- a command that prints "12
passed" and then exits 1 is reported as failing. Diffs the files it actually
finds changed against `--predicted`, both directions: named but not
touched, touched but not named. `--left` is required even as `"nothing"`.
An unfinished item is a decision you stated, not an omission the report
papers over.

**If a measurement was not taken, do not run `finish` yet.** It runs the
commands itself. It does not accept a claim in place of a result.

## Moving an issue

```
jira-agent transition PROJ-12 --to Done
```

Matched by name, case-insensitively. An unmatched name lists what IS on the
workflow rather than failing silently.

## Sprints and priority

```
jira-agent create --sprint "Sprint 12" ...
jira-agent create --priority "High" ...
jira-agent sprints
```

`--sprint` takes a name (matched case-insensitively against your project's
real sprints) or a bare id. It errs clearly if Sprints is off for your
project. It never guesses at a custom field id -- those are NOT the same
across Jira sites. `--priority` accepts whatever priority names your
project actually has.

## Blocked on the user

```
jira-agent block PROJ-12 --need "..." --kind decide
```

`--kind` is required: `decide` (pick between real options), `provide`
(hand over an input or credential), or `go` (already decided, needs a
green light). Three different asks, not one flat "blocked" -- a flat flag
hides which kind of ask it is from whoever is triaging. `--need` is
required too, and must name the exact decision, file or credential, and
what you will do for each answer they might give.

## The "where to look" dashboard

```
jira-agent dashboard          # prints the plan, creates nothing
jira-agent dashboard --apply  # creates 5 filters and a dashboard for real
```

See `README.md` for what this builds and where the pattern came from.
