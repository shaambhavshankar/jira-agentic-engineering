#!/usr/bin/env python3
"""The multi-repo telemetry store.

WHY THIS EXISTS. `jira-agent finish` posts a commit trail as a Jira
comment, but nothing aggregates across issues, and nothing outlives one
repo's checkout. Every other JAE component -- the dashboard, the judge, the
self-improvement loop, replay -- reads from here first.

WHY IT IS NOT PER-REPO. A file inside one repo's checkout cannot record a
row for a second repo without either depending on that repo's checkout
existing on disk, which breaks the moment either repo is cloned alone. This
store lives at `~/.jira-agent/jae.db` -- outside any one repo -- specifically
so a query can scope to one `repo` or pool across all of them from the same
schema: "multi-repo with learnings and judge for each individual repo and
overall as well."

WHY SQLITE, NOT A HOSTED SERVICE. Every other piece of this kit runs as a
CLI invoked from a hook, with no daemon and nothing to keep running. A
hosted store would be the first component here that can go down on its
own. SQLite's WAL mode is enabled on connect and is enough for the write
volume one team produces; this is not a public-facing service.

WHAT'S DELIBERATELY MISSING. Multi-machine sync. `~/.jira-agent/jae.db` is
per-machine. A team of more than one person running JAE needs a shared
store eventually -- that's a real follow-on decision, not solved here.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path

__all__ = [
    "DEFAULT_DB_PATH",
    "TaskRecord",
    "TelemetryStore",
    "append_jsonl",
    "repo_name",
]

DEFAULT_DB_PATH = Path.home() / ".jira-agent" / "jae.db"

# The DB-column value standing in for "no factory_version" (a live score),
# so the column is never actually NULL. See record_score's docstring: a
# NULL column in a PRIMARY KEY never collides with another NULL under
# standard SQL, which broke upserting a live score twice. No real tag
# starts with a null byte, so this cannot collide with a real
# `factory-vN` value.
_LIVE_SENTINEL = "\x00live"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS task_records (
    issue_key           TEXT NOT NULL,
    repo                TEXT NOT NULL,
    session_id          TEXT NOT NULL,
    started_at          TEXT NOT NULL,
    finished_at         TEXT NOT NULL,
    model               TEXT NOT NULL,
    files_changed       TEXT NOT NULL,
    test_exit_code      INTEGER NOT NULL,
    contract_exit_code  INTEGER,
    human_interactions  INTEGER NOT NULL,
    cost_usd            REAL,
    pr_url              TEXT,
    PRIMARY KEY (repo, issue_key, session_id)
);

CREATE TABLE IF NOT EXISTS scores (
    repo             TEXT NOT NULL,
    issue_key        TEXT NOT NULL,
    session_id       TEXT NOT NULL,
    dimension        TEXT NOT NULL,
    score            REAL NOT NULL,
    confidence       REAL NOT NULL,
    scored_at        TEXT NOT NULL,
    factory_version  TEXT,
    PRIMARY KEY (repo, issue_key, session_id, dimension, factory_version)
);
"""


@dataclass(frozen=True)
class TaskRecord:
    """One finished `jira-agent finish` call.

    `human_interactions` is NOT known at write time -- it is defined (spec
    §4.2) as a formula over Jira comments, PR review comments, and re-run
    count, none of which are knowable the instant `finish` posts. Write 0
    here and let the dashboard (§4) call `update_human_interactions` once
    it has actually queried Jira and GitHub for the real count.
    """

    issue_key: str
    repo: str
    session_id: str
    started_at: datetime
    finished_at: datetime
    model: str
    files_changed: tuple[str, ...]
    test_exit_code: int
    contract_exit_code: int | None
    human_interactions: int
    cost_usd: float | None
    pr_url: str | None = None


class TelemetryStore:
    """A connection to one SQLite telemetry file.

    Opened once per process, not once per call -- `jira-agent finish`
    opens one, writes one record, and lets it close with the process. A
    long-lived dashboard process may hold one connection across many reads.
    """

    def __init__(self, db_path: Path | str = DEFAULT_DB_PATH) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self.db_path)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.executescript(_SCHEMA)
        self._migrate()
        self._conn.commit()

    def _migrate(self) -> None:
        """Add a column to a table_records file that predates it.

        `CREATE TABLE IF NOT EXISTS` only creates a table that doesn't
        exist yet -- it does nothing to an existing table missing a
        column a newer version of this schema added. The real
        ~/.jira-agent/jae.db predates `pr_url`; without this, opening it
        after this change would silently read every pr_url as absent from
        the row tuple rather than as the column that's actually missing.
        """
        columns = {row[1] for row in self._conn.execute("PRAGMA table_info(task_records)")}
        if "pr_url" not in columns:
            self._conn.execute("ALTER TABLE task_records ADD COLUMN pr_url TEXT")

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> "TelemetryStore":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def record(self, task: TaskRecord) -> None:
        """Write one TaskRecord. Same (repo, issue_key, session_id) upserts.

        Upsert, not insert-only: a `finish` re-run for the same session
        (the agent had to be re-run and `finish` called again) should
        replace what's known about that session, not duplicate the row a
        second time under the same primary key.
        """
        self._conn.execute(
            """
            INSERT INTO task_records (
                issue_key, repo, session_id, started_at, finished_at, model,
                files_changed, test_exit_code, contract_exit_code,
                human_interactions, cost_usd, pr_url
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (repo, issue_key, session_id) DO UPDATE SET
                started_at=excluded.started_at,
                finished_at=excluded.finished_at,
                model=excluded.model,
                files_changed=excluded.files_changed,
                test_exit_code=excluded.test_exit_code,
                contract_exit_code=excluded.contract_exit_code,
                human_interactions=excluded.human_interactions,
                cost_usd=excluded.cost_usd,
                pr_url=excluded.pr_url
            """,
            (
                task.issue_key,
                task.repo,
                task.session_id,
                task.started_at.isoformat(),
                task.finished_at.isoformat(),
                task.model,
                json.dumps(list(task.files_changed)),
                task.test_exit_code,
                task.contract_exit_code,
                task.human_interactions,
                task.cost_usd,
                task.pr_url,
            ),
        )
        self._conn.commit()

    def tasks(self, repo: str | None = None) -> tuple[TaskRecord, ...]:
        """Every recorded task, optionally scoped to one repo.

        `repo=None` pools every repo in the store -- the "overall" half of
        "per individual repo and overall as well" (spec §8.4).
        """
        if repo is None:
            rows = self._conn.execute(
                "SELECT * FROM task_records ORDER BY finished_at"
            ).fetchall()
        else:
            rows = self._conn.execute(
                "SELECT * FROM task_records WHERE repo = ? ORDER BY finished_at",
                (repo,),
            ).fetchall()
        return tuple(_row_to_task(row) for row in rows)

    def update_human_interactions(
        self, *, repo: str, issue_key: str, session_id: str, count: int
    ) -> None:
        """Set the real human-interaction count once the dashboard has
        computed it (spec §4.2). Silent no-op if the row doesn't exist yet
        -- a dashboard sweep racing an in-flight `finish` call must not
        crash either process over a timing coincidence.
        """
        self._conn.execute(
            """
            UPDATE task_records SET human_interactions = ?
            WHERE repo = ? AND issue_key = ? AND session_id = ?
            """,
            (count, repo, issue_key, session_id),
        )
        self._conn.commit()

    def record_score(
        self,
        *,
        repo: str,
        issue_key: str,
        session_id: str,
        dimension: str,
        score: float,
        confidence: float,
        factory_version: str | None = None,
    ) -> None:
        """One Jev judge score. `factory_version=None` marks a live run;
        a tag like "factory-v2" marks a replay (§7.2) -- both are kept,
        never overwriting each other, because replay's whole point is
        comparing a live score against a replayed one.

        Stores `None` as `_LIVE_SENTINEL`, not SQL NULL. Standard SQL
        treats every NULL in a PRIMARY KEY as distinct from every other
        NULL -- including a second NULL for the exact same row -- so
        `ON CONFLICT` on a column that can be NULL never fires for two
        live scores of the same task. A real duplicate-row bug, found by
        actually re-scoring the same real issue twice. A non-NULL
        sentinel is an ordinary value, and ordinary values DO collide on
        a PRIMARY KEY as expected.
        """
        stored_version = factory_version if factory_version is not None else _LIVE_SENTINEL
        self._conn.execute(
            """
            INSERT INTO scores (
                repo, issue_key, session_id, dimension, score, confidence,
                scored_at, factory_version
            ) VALUES (?, ?, ?, ?, ?, ?, datetime('now'), ?)
            ON CONFLICT (repo, issue_key, session_id, dimension, factory_version)
            DO UPDATE SET score=excluded.score, confidence=excluded.confidence,
                          scored_at=excluded.scored_at
            """,
            (repo, issue_key, session_id, dimension, score, confidence, stored_version),
        )
        self._conn.commit()

    def scores(
        self, repo: str | None = None, dimension: str | None = None
    ) -> tuple[dict, ...]:
        """Every recorded score, optionally scoped by repo and/or dimension."""
        clauses: list[str] = []
        params: list[str] = []
        if repo is not None:
            clauses.append("repo = ?")
            params.append(repo)
        if dimension is not None:
            clauses.append("dimension = ?")
            params.append(dimension)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        rows = self._conn.execute(
            f"SELECT * FROM scores {where} ORDER BY scored_at", params
        ).fetchall()
        columns = [d[0] for d in self._conn.execute("SELECT * FROM scores LIMIT 0").description]
        result = []
        for row in rows:
            record = dict(zip(columns, row))
            if record["factory_version"] == _LIVE_SENTINEL:
                record["factory_version"] = None
            result.append(record)
        return tuple(result)


def _row_to_task(row: Sequence) -> TaskRecord:
    (
        issue_key, repo, session_id, started_at, finished_at, model,
        files_changed, test_exit_code, contract_exit_code,
        human_interactions, cost_usd, pr_url,
    ) = row
    return TaskRecord(
        issue_key=issue_key,
        repo=repo,
        session_id=session_id,
        started_at=datetime.fromisoformat(started_at),
        finished_at=datetime.fromisoformat(finished_at),
        model=model,
        files_changed=tuple(json.loads(files_changed)),
        test_exit_code=test_exit_code,
        contract_exit_code=contract_exit_code,
        human_interactions=human_interactions,
        cost_usd=cost_usd,
        pr_url=pr_url,
    )


def repo_name(cwd: Path | str) -> str:
    """The repo's name, derived from `git remote get-url origin`.

    Not typed by hand: a repo record with a wrong or inconsistent `repo`
    string would silently split one repo's telemetry into two buckets. The
    remote URL is the one fact every clone of a repo agrees on. Handles
    both HTTPS (`.../owner/name.git` or `.../owner/name`) and SSH
    (`git@host:owner/name.git`) remote forms.
    """
    import subprocess

    try:
        done = subprocess.run(
            ["git", "remote", "get-url", "origin"],
            cwd=str(cwd),
            capture_output=True,
            text=True,
            check=True,
        )
    except subprocess.CalledProcessError as error:
        raise ValueError(
            f"no 'origin' remote in {cwd!r}; cannot derive a repo name. "
            "Run `git remote add origin <url>` first."
        ) from error

    url = done.stdout.strip()
    name = url.rsplit("/", 1)[-1]
    if name.endswith(".git"):
        name = name[: -len(".git")]
    return name


def append_jsonl(task: TaskRecord, path: Path | str) -> None:
    """Append one TaskRecord as one JSON line -- the offline/local fallback.

    Not the primary read path (that's TelemetryStore); this is what a repo
    working without the central store still gets, and what `finish` writes
    to before the central-store push (best-effort, see cli.py).
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = asdict(task)
    payload["started_at"] = task.started_at.isoformat()
    payload["finished_at"] = task.finished_at.isoformat()
    payload["files_changed"] = list(task.files_changed)
    with path.open("a") as f:
        f.write(json.dumps(payload))
        f.write("\n")
