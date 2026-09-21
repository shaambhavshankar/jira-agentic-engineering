#!/usr/bin/env python3
"""Commands for the Jira record.

    jira-agent create --type task --summary "..." ...
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

Configuration comes entirely from the environment; see config.py and
docs/SETUP.md. Nothing here is specific to any one company's Jira.

EXIT CODES:
    0  fine
    1  the operation failed (Jira refused, or could not be reached)
    2  cannot run (bad input, missing config, or no token)

`lint` exits 0 even when it finds something. It warns; it does not block.
"""

from __future__ import annotations

import argparse
import os
import shlex
import subprocess
import sys
import uuid
from collections.abc import Sequence
from datetime import datetime, timezone
from pathlib import Path

from jira_agent_kit import context, dashboard as dash, schema, telemetry
from jira_agent_kit.client import JiraClient, JiraError, TokenMissing, read_token
from jira_agent_kit.config import Config, ConfigError, load as load_config

EXIT_OK = 0
EXIT_FAILED = 1
EXIT_MISUSE = 2


def _parser(config: Config) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="jira-agent")
    parser.add_argument("--email", default=config.email)
    parser.add_argument("--repo", default=".")
    sub = parser.add_subparsers(dest="command")

    show = sub.add_parser("show", help="print an issue, its labels, and its last comments")
    show.add_argument("key")

    comment = sub.add_parser("comment", help="post the cheap commit trail")
    comment.add_argument("key", nargs="?")
    comment.add_argument("--rev", default="HEAD")

    block = sub.add_parser("block", help="mark an issue as waiting on a human")
    block.add_argument("key")
    block.add_argument("--need", help="what exactly you are blocked on")
    block.add_argument(
        "--kind", choices=("decide", "provide", "go"),
        help=(
            "decide: pick between real options. provide: hand over an "
            "input or credential. go: already decided, needs a green light."
        ),
    )

    lint = sub.add_parser("lint", help="run the plain-language lint on a file")
    lint.add_argument("path")

    sub.add_parser("whoami", help="who Jira thinks this credential is")

    sub.add_parser("sprints", help="list this project's sprints: id, name, state")

    dashboard = sub.add_parser(
        "dashboard", help="create the 'where to look' filters and dashboard"
    )
    dashboard.add_argument(
        "--apply", action="store_true",
        help="actually create them; without this it only prints the plan",
    )

    transition = sub.add_parser("transition", help="move an issue to a new status")
    transition.add_argument("key")
    transition.add_argument("--to", required=True, help="target status name, e.g. Done")

    start = sub.add_parser("start", help="post a start comment before editing something")
    start.add_argument("key")
    start.add_argument("--target", required=True, help="what you are about to change")
    start.add_argument("--direction", default="upstream", choices=("upstream", "downstream"))
    start.add_argument("--plan", required=True)
    start.add_argument("--files", required=True, help="comma-separated expected files")
    start.add_argument("--details", default=None)
    start.add_argument(
        "--impact-cmd", default=None,
        help=(
            "optional blast-radius command, with {target} and {direction} "
            'placeholders, e.g. "mytool impact {target} --direction {direction}". '
            "Must print JSON with risk/impactedCount/summary.direct keys, or "
            "an \"error\" key on failure -- see context.run_impact. Omit to "
            "skip the blast-radius section entirely."
        ),
    )
    start.add_argument("--accuracy", required=True, metavar="Verdict|sentence")
    start.add_argument("--scalability", required=True, metavar="Verdict|sentence")
    start.add_argument("--maintenance", required=True, metavar="Verdict|sentence")

    finish = sub.add_parser("finish", help="post the finish report after work is done")
    finish.add_argument("key")
    finish.add_argument("--test-cmd", required=True, help="the exact test command to run")
    finish.add_argument("--contract-cmd", default=None, help="an optional second check to run and report")
    finish.add_argument("--predicted", default="", help="comma-separated files predicted in start")
    finish.add_argument("--rev", default="HEAD")
    finish.add_argument("--accuracy", required=True, metavar="Verdict|sentence")
    finish.add_argument("--scalability", required=True, metavar="Verdict|sentence")
    finish.add_argument("--maintenance", required=True, metavar="Verdict|sentence")
    finish.add_argument("--left", required=True, help='what is undone, even if "nothing"')

    create = sub.add_parser("create", help="create an issue from the template")
    create.add_argument("--type", required=True, help="e.g. task, bug, epic, subtask")
    create.add_argument("--summary", required=True)
    create.add_argument("--problem", required=True)
    create.add_argument(
        "--option", action="append", default=[], metavar="name|good|bad",
        help="repeat at least twice",
    )
    create.add_argument("--chosen", required=True)
    create.add_argument("--accuracy", required=True, metavar="Verdict|sentence")
    create.add_argument("--scalability", required=True, metavar="Verdict|sentence")
    create.add_argument("--maintenance", required=True, metavar="Verdict|sentence")
    create.add_argument("--layer", required=True, choices=sorted(schema.LAYER_NAMES))
    create.add_argument("--goal", required=True, choices=sorted(schema.GOAL_NAMES))
    create.add_argument("--risk", default=None, choices=sorted(schema.RISK_NAMES))
    create.add_argument("--parent", default=None, help="parent key, e.g. PROJ-9")
    create.add_argument("--details", default=None, help="link to a spec or plan")
    create.add_argument(
        "--estimate", type=int, default=None,
        help="story-point-style estimate; errors if no such field is configured",
    )
    create.add_argument(
        "--priority", default=None,
        help="a priority name that exists on your project, e.g. 'High'",
    )
    create.add_argument(
        "--sprint", default=None,
        help="sprint name (matched case-insensitively) or a bare sprint id",
    )

    return parser


def parse_option(raw: str) -> schema.Option:
    """Parse `name|good|bad` into an Option.

    Pipes, not JSON: an option has exactly three parts, and typing JSON on a
    command line invites a quoting mistake that then looks like a content
    mistake.
    """
    parts = [part.strip() for part in raw.split("|")]
    if len(parts) != 3:
        raise ValueError(f"--option wants name|good|bad, got {raw!r}")
    return schema.Option(*parts)


def parse_axis(raw: str) -> schema.AxisVerdict:
    """Parse `Better|One sentence.` into an AxisVerdict.

    An unknown verdict raises LabelError from AxisVerdict itself, so a hedge
    like "Slightly better" is refused in one place rather than two.
    """
    verdict, _, sentence = raw.partition("|")
    return schema.AxisVerdict(verdict.strip(), sentence.strip())


def _client(config: Config, email: str) -> JiraClient:
    token = read_token(service=config.keychain_service, account=email)
    return JiraClient(site=config.site, email=email, token=token)


def _require_key(raw: str, pattern) -> str:
    key = context.issue_key_from(raw, pattern)
    if key is None:
        raise ValueError(f"not a recognised issue key: {raw!r}")
    return key


def _resolve_sprint(client: JiraClient, project_key: str, raw: str) -> int | None:
    """A sprint id or name -> a sprint id, or None with a message printed.

    A digit string is used directly, skipping the board/sprint lookup.
    Otherwise the name is matched case-insensitively against the project's
    board, and an unmatched name lists what actually exists rather than
    failing silently -- the same shape as `transition`'s status lookup.
    """
    if raw.isdigit():
        return int(raw)

    board_id = client.find_board_id(project_key)
    if board_id is None:
        print(f"project {project_key} has no Agile board; cannot resolve a sprint by name", file=sys.stderr)
        return None

    sprints = client.list_sprints(board_id)
    match = next((s["id"] for s in sprints if s["name"].lower() == raw.lower()), None)
    if match is None:
        available = ", ".join(f"{s['name']} ({s['state']})" for s in sprints) or "(none)"
        print(f"no sprint named {raw!r}. Available: {available}", file=sys.stderr)
        return None
    return match


def _do_create(args, config: Config, vocab: schema.Vocabulary) -> int:
    options = [parse_option(raw) for raw in args.option]
    if len(options) < 2:
        print(
            "at least two options are required. A lone option is how a "
            "decision gets made without anyone noticing a decision was made. "
            "If the fix is forced, say why in --chosen and pass the "
            "alternative you rejected.",
            file=sys.stderr,
        )
        return EXIT_MISUSE

    axes = schema.Axes(
        accuracy=parse_axis(args.accuracy),
        scalability=parse_axis(args.scalability),
        maintenance=parse_axis(args.maintenance),
    )
    labels = [
        f"{vocab.prefix}-{args.layer}",
        f"{vocab.prefix}-{args.goal}",
        f"{vocab.prefix}-by-agent",
        *axes.hurt_labels(vocab),
    ]
    if args.risk:
        labels.append(f"{vocab.prefix}-{args.risk}")

    # Blank line, not a space. A summary has no terminal period, so joining
    # them with a space glues the summary to the problem's first sentence
    # and reports a length for text nobody wrote -- the same defect _units
    # exists to prevent, reintroduced by pre-joining the inputs.
    combined = args.summary + "\n\n" + args.problem
    for finding in schema.lint_prose(combined):
        # Warn, never block: the lint is a heuristic.
        print(f"lint {finding.kind}: {finding.detail}", file=sys.stderr)

    if args.type.lower() == "subtask" and not args.parent:
        print(
            "--parent is required for --type subtask. A subtask with no "
            "parent is not a valid Jira issue.",
            file=sys.stderr,
        )
        return EXIT_MISUSE

    client = _client(config, args.email)
    issue_type_id = client.resolve_issue_type_id(config.project_key, args.type)

    extra_fields: dict = {}

    if args.estimate is not None:
        field_id = client.find_estimate_field(config.project_key, issue_type_id)
        if field_id is None:
            print(
                "no estimate field is configured for this issue type. "
                "Estimation may be off for this project -- that is a Jira "
                "project-settings toggle, not something this tool can do "
                "via the API.",
                file=sys.stderr,
            )
            return EXIT_MISUSE
        extra_fields[field_id] = args.estimate

    if args.priority:
        extra_fields["priority"] = {"name": args.priority}

    if args.sprint:
        sprint_field = client.find_sprint_field(config.project_key, issue_type_id)
        if sprint_field is None:
            print(
                "no Sprint field is configured for this issue type. "
                "Sprints may be off for this project.",
                file=sys.stderr,
            )
            return EXIT_MISUSE
        sprint_id = _resolve_sprint(client, config.project_key, args.sprint)
        if sprint_id is None:
            return EXIT_MISUSE
        # A bare int, NOT an array. Verified live against a real Jira site:
        # an array, a string, and an array of strings were all rejected
        # with "Specify a valid value for Sprint". Only a bare int works.
        extra_fields[sprint_field] = sprint_id

    # Area labels are deliberately NOT set here. They are derived from the
    # files a commit actually touched and added by `comment`. Typing them
    # at creation time is guessing, and a guessed label is wrong AND
    # findable.
    key = client.create_issue(
        project_key=config.project_key,
        issue_type_id=issue_type_id,
        summary=args.summary,
        description_adf=schema.description_adf(
            problem=args.problem,
            options=options,
            chosen=args.chosen,
            axes=axes,
            details_link=args.details,
        ),
        labels=labels,
        vocabulary=vocab,
        parent_key=args.parent,
        extra_fields=extra_fields or None,
    )
    print(f"created {key}")
    return EXIT_OK


def _telemetry_db_path() -> Path:
    """`JIRA_AGENT_DB_PATH` if set, else the shared multi-repo default.

    A separate env var, not folded into Config: the telemetry store is not
    company-specific the way site/project/email are -- it is a local
    machine path, and every repo on one machine shares the same one by
    design (spec §3.4). Overridable for tests and for anyone who wants a
    non-default location.
    """
    raw = os.environ.get("JIRA_AGENT_DB_PATH", "").strip()
    return Path(raw) if raw else telemetry.DEFAULT_DB_PATH


def _record_telemetry(
    *,
    key: str,
    repo_path: str,
    files_changed: Sequence[str],
    test_exit_code: int,
    contract_exit_code: int | None,
) -> None:
    """Write one TaskRecord for this `finish` call. Best-effort, always.

    Never allowed to fail `finish`: a telemetry write is exactly the kind
    of side channel that must not turn a successful Jira post into a
    failed command, the same non-blocking rule every network call in this
    kit already follows. Errors are swallowed here, not silently -- a
    warning goes to stderr, same as a lint finding.
    """
    try:
        repo = telemetry.repo_name(repo_path)
        session_id = os.environ.get("JIRA_AGENT_SESSION_ID", "").strip() or uuid.uuid4().hex
        model = os.environ.get("JIRA_AGENT_MODEL", "").strip() or "unknown"
        now = datetime.now(timezone.utc)
        try:
            authored = context.git(["log", "-1", "--format=%aI", "HEAD"], repo_path)
            started_at = datetime.fromisoformat(authored)
        except (subprocess.CalledProcessError, ValueError):
            started_at = now  # best-effort: an approximation, not a fabrication

        store = telemetry.TelemetryStore(_telemetry_db_path())
        try:
            store.record(
                telemetry.TaskRecord(
                    issue_key=key,
                    repo=repo,
                    session_id=session_id,
                    started_at=started_at,
                    finished_at=now,
                    model=model,
                    files_changed=tuple(files_changed),
                    test_exit_code=test_exit_code,
                    contract_exit_code=contract_exit_code,
                    human_interactions=0,  # computed later, by the dashboard (spec §4.2)
                    cost_usd=None,  # not available at this layer yet
                )
            )
        finally:
            store.close()
    except Exception as error:  # noqa: BLE001 -- deliberately broad, see docstring
        print(f"telemetry write failed (finish still succeeded): {error}", file=sys.stderr)


def _do_transition(args, config: Config) -> int:
    """Move an issue to a new status by NAME, matched case-insensitively.

    Jira transition ids are workflow-specific and not memorable; the name
    on the button is what a person actually knows. An unmatched name lists
    what IS available, because failing silently would be worse than failing
    loudly with the real options beside it.
    """
    client = _client(config, args.email)
    available = client.list_transitions(args.key)
    match = next(
        (tid for name, tid in available.items() if name.lower() == args.to.lower()),
        None,
    )
    if match is None:
        print(
            f"no transition named {args.to!r}. Available: "
            + ", ".join(sorted(available)),
            file=sys.stderr,
        )
        return EXIT_MISUSE
    client.transition(args.key, match)
    print(f"{args.key} -> {args.to}")
    return EXIT_OK


def _do_start(args, config: Config) -> int:
    """Post the start comment: branch, base sha, plan, files, optional blast
    radius, axes predicted.

    The blast-radius check is OPTIONAL and pluggable via --impact-cmd,
    because this kit does not assume you have any particular static-analysis
    tool. If you do, wire its command line in with {target}/{direction}
    placeholders; if you don't, this simply posts everything else.
    """
    axes = schema.Axes(
        accuracy=parse_axis(args.accuracy),
        scalability=parse_axis(args.scalability),
        maintenance=parse_axis(args.maintenance),
    )

    branch = context.current_branch(args.repo)
    sha = context.head_sha(args.repo)
    files = [f.strip() for f in args.files.split(",") if f.strip()]

    body_lines = [
        f"Branch: {branch}",
        f"Base commit: {sha[:12]}",
        "",
        "Plan:",
        args.plan,
        "",
        "Files expected to change:",
        "\n".join(files) if files else "(none named)",
    ]

    if args.impact_cmd:
        command = shlex.split(
            args.impact_cmd.format(target=args.target, direction=args.direction)
        )
        result = context.run_impact(command)
        if not result.found:
            print(f"blast-radius check could not resolve {args.target!r}: {result.error}", file=sys.stderr)
            return EXIT_MISUSE
        body_lines += [
            "",
            f"Blast radius on {args.target} ({args.direction}): "
            f"risk={result.risk}, impacted={result.impacted_count}, direct={result.direct}",
        ]

    body_lines += [
        "",
        "Axes, predicted:",
        f"Accuracy of results - {axes.accuracy.verdict}. {axes.accuracy.sentence}",
        f"Scalability - {axes.scalability.verdict}. {axes.scalability.sentence}",
        f"Ease of maintenance - {axes.maintenance.verdict}. {axes.maintenance.sentence}",
    ]
    if args.details:
        body_lines += ["", f"Details: {args.details}"]

    client = _client(config, args.email)
    vocab = schema.Vocabulary(config.label_prefix)
    client.add_comment(args.key, schema.text_adf("\n".join(body_lines)))

    if args.impact_cmd:
        risk_label = result.risk_label(config.label_prefix, vocab.risks)
        if risk_label:
            client.add_labels(args.key, [risk_label], vocab)
            print(f"posted start comment on {args.key}, labelled {risk_label}")
            return EXIT_OK

    print(f"posted start comment on {args.key}")
    return EXIT_OK


def _do_finish(args, config: Config) -> int:
    """Post the finish report: files diff, real test result, an optional
    second check's result, measured axes, and what is left undone.

    Runs the test command (and --contract-cmd, if given) FOR REAL and reads
    their EXIT CODES -- never their printed text. A command that prints
    "12 passed" and then exits 1 is reported as failing.
    """
    axes = schema.Axes(
        accuracy=parse_axis(args.accuracy),
        scalability=parse_axis(args.scalability),
        maintenance=parse_axis(args.maintenance),
    )

    test_result = context.run_command(shlex.split(args.test_cmd))
    verdict = context.read_test_result(args.test_cmd, test_result.exit_code, test_result.output)

    predicted = {f.strip() for f in args.predicted.split(",") if f.strip()}
    actual = set(context.changed_files(args.rev, args.repo))
    unexpected = sorted(actual - predicted)
    missing = sorted(predicted - actual)

    lines = [
        f"Files predicted: {', '.join(sorted(predicted)) or '(none)'}",
        f"Files actually changed: {', '.join(sorted(actual)) or '(none)'}",
    ]
    if unexpected:
        lines.append(f"Changed but not predicted: {', '.join(unexpected)}")
    if missing:
        lines.append(f"Predicted but not changed: {', '.join(missing)}")

    lines += [
        "",
        f"Test command: {args.test_cmd}",
        f"Result: {'PASSED' if verdict.ok else 'FAILED'} (exit {verdict.exit_code})"
        + (f", {verdict.passed} passed" if verdict.passed is not None else ""),
    ]

    if args.contract_cmd:
        contract_result = context.run_command(shlex.split(args.contract_cmd))
        lines += [
            "",
            f"Second check: {args.contract_cmd}",
            f"Result: exit {contract_result.exit_code}"
            + (
                f" ({context.CONTRACT_MEANING[contract_result.exit_code]})"
                if contract_result.exit_code in context.CONTRACT_MEANING
                else ""
            ),
        ]

    lines += [
        "",
        "Axes, measured:",
        f"Accuracy of results - {axes.accuracy.verdict}. {axes.accuracy.sentence}",
        f"Scalability - {axes.scalability.verdict}. {axes.scalability.sentence}",
        f"Ease of maintenance - {axes.maintenance.verdict}. {axes.maintenance.sentence}",
        "",
        f"Left undone: {args.left}",
    ]

    client = _client(config, args.email)
    vocab = schema.Vocabulary(config.label_prefix)
    client.add_comment(args.key, schema.text_adf("\n".join(lines)))

    hurt = axes.hurt_labels(vocab)
    if hurt:
        client.add_labels(args.key, list(hurt), vocab)

    _record_telemetry(
        key=args.key,
        repo_path=args.repo,
        files_changed=sorted(actual),
        test_exit_code=verdict.exit_code,
        contract_exit_code=contract_result.exit_code if args.contract_cmd else None,
    )

    print(f"posted finish report on {args.key}" + (f", labelled {', '.join(hurt)}" if hurt else ""))
    return EXIT_OK


def _do_dashboard(args, config: Config) -> int:
    vocab = schema.Vocabulary(config.label_prefix)
    dashboard_name, dashboard_description, filters, gadgets = dash.plan(config, vocab)

    if not args.apply:
        print(f"would create {len(filters)} filters:")
        for f in filters:
            print(f"  {f.name}")
            print(f"    {f.jql}")
        print(f"would create 1 dashboard ({dashboard_name}) with {len(gadgets)} gadgets")
        return EXIT_OK

    client = _client(config, args.email)

    # Every filter first, keyed by the SAME `key` a GadgetSpec references --
    # not by name or by loop position, which is exactly the shape where
    # gadget N could end up wired to filter M's id by accident.
    filter_ids: dict[str, str] = {}
    for f in filters:
        fid = client.create_filter(f.name, f.jql, f.description)
        filter_ids[f.key] = fid
        print(f"created filter {fid}  {f.name}")

    dashboard_id = client.create_dashboard(dashboard_name, dashboard_description)
    print(f"created dashboard {dashboard_id}  {dashboard_name}")

    for g in gadgets:
        gadget_id = client.add_dashboard_gadget(
            dashboard_id=dashboard_id,
            uri=dash.FILTER_RESULTS_GADGET_URI,
            color=g.color,
            row=g.row,
            column=g.column,
            title=g.title,
        )
        client.configure_gadget(
            dashboard_id, gadget_id, dash.gadget_config(filter_ids[g.filter_key])
        )
        print(f"  configured gadget {gadget_id}  {g.title}")

    return EXIT_OK


def _do_sprints(args, config: Config) -> int:
    client = _client(config, args.email)
    board_id = client.find_board_id(config.project_key)
    if board_id is None:
        print(f"project {config.project_key} has no Agile board", file=sys.stderr)
        return EXIT_MISUSE
    for sprint in client.list_sprints(board_id):
        print(f"{sprint['id']}  {sprint['name']}  ({sprint['state']})")
    return EXIT_OK


def main(argv: Sequence[str] | None = None) -> int:
    try:
        config = load_config()
    except ConfigError as error:
        print(str(error), file=sys.stderr)
        return EXIT_MISUSE

    args = _parser(config).parse_args(list(argv) if argv is not None else None)

    if not args.command:
        print("no command. Try: create, show, comment, block, start, finish, "
              "transition, sprints, dashboard, whoami, lint", file=sys.stderr)
        return EXIT_MISUSE

    vocab = schema.Vocabulary(config.label_prefix)
    key_pattern = context.make_issue_key_pattern(config.project_key)

    try:
        if args.command == "lint":
            findings = schema.lint_prose(Path(args.path).read_text())
            for finding in findings:
                print(f"{finding.kind}: {finding.detail}")
            if not findings:
                print("clean")
            # Exits 0 either way: the lint warns, it does not block.
            return EXIT_OK

        if args.command == "create":
            return _do_create(args, config, vocab)

        if args.command == "block" and not args.need:
            print(
                "--need is required. A tse-needs-you-style label without a "
                "stated need moves the work of finding out onto the reader.",
                file=sys.stderr,
            )
            return EXIT_MISUSE

        if args.command == "block" and not args.kind:
            print(
                "--kind is required: decide, provide, or go. A flat "
                "'blocked' flag hides which kind of ask this is from "
                "whoever is triaging.",
                file=sys.stderr,
            )
            return EXIT_MISUSE

        if args.command == "dashboard":
            return _do_dashboard(args, config)

        if args.command == "sprints":
            return _do_sprints(args, config)

        if args.command == "transition":
            return _do_transition(args, config)

        if args.command == "start":
            return _do_start(args, config)

        if args.command == "finish":
            return _do_finish(args, config)

        if args.command == "whoami":
            client = _client(config, args.email)
            me = client.myself()
            print(f"authenticated as {me.get('emailAddress')}")
            print(f"accountId: {me.get('accountId')}")
            print(f"active: {me.get('active')}")
            return EXIT_OK

        key = _require_key(args.key, key_pattern) if getattr(args, "key", None) else None
        if args.command == "comment" and key is None:
            subject = context.git(["log", "-1", "--pretty=%s", args.rev], args.repo)
            key = context.issue_key_from(subject, key_pattern)
            if key is None:
                return EXIT_OK  # no key in the commit subject: nothing to do

        client = _client(config, args.email)

        if args.command == "show":
            issue = client.get_issue(key)
            fields = issue["fields"]
            print(f"{key}  {fields.get('summary', '(no summary)')}")
            # .get, not [..]: a response shaped differently than expected
            # must not become a traceback with no exit code.
            print(f"status: {fields.get('status', {}).get('name', '(unknown)')}")
            print(f"labels: {', '.join(fields.get('labels', [])) or '(none)'}")
            for body in client.list_comments(key, limit=3):
                if body:
                    print(f"comment: {body}")
            return EXIT_OK

        if args.command == "comment":
            files = context.changed_files(args.rev, args.repo)
            sha = context.git(["rev-parse", args.rev], args.repo)
            # The SAME rev the trail reports on -- reading HEAD's subject
            # while gathering files from --rev would post one commit's
            # trail onto the issue named by another.
            subject = context.git(["log", "-1", "--pretty=%s", args.rev], args.repo)
            body = "\n\n".join(
                [
                    f"Commit {sha[:12]} - {subject}",
                    "Files changed:\n" + "\n".join(files) if files else "No files changed.",
                ]
            )
            client.add_comment(key, schema.text_adf(body))
            areas = context.area_labels(files, config.label_prefix)
            if areas:
                client.add_labels(key, areas, vocab)
            return EXIT_OK

        if args.command == "block":
            client.add_labels(key, [vocab.needs_you, vocab.needs_kind_label(args.kind)], vocab)
            client.add_comment(
                key, schema.text_adf(f"Waiting on you ({args.kind}).\n\n{args.need}")
            )
            return EXIT_OK

    except TokenMissing as error:
        print(str(error), file=sys.stderr)
        return EXIT_MISUSE
    except (ValueError, OSError) as error:
        # OSError covers a missing file for `lint` and a missing `security`
        # binary. Both would otherwise escape main() as tracebacks rather
        # than as an exit code a caller could read.
        print(str(error), file=sys.stderr)
        return EXIT_MISUSE
    except subprocess.CalledProcessError as error:
        # context.git runs with check=True. A bad revision is an operation
        # that failed, not bad input to this program.
        print(f"git failed: {' '.join(error.cmd)}: {error.stderr}", file=sys.stderr)
        return EXIT_FAILED
    except JiraError as error:
        print(str(error), file=sys.stderr)
        return EXIT_FAILED

    # Not reachable today: argparse rejects an unknown command before
    # main() runs, and every subcommand returns inside the try. Kept as a
    # guard for a subcommand added to the parser and not to the body, which
    # would otherwise fall off the end and return None -- an exit code of 0
    # for work never done.
    print(f"no handler for command {args.command!r}", file=sys.stderr)
    return EXIT_MISUSE
