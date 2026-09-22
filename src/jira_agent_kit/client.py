#!/usr/bin/env python3
"""The one module that reads a credential and opens a socket.

WHY THIS EXISTS AS ITS OWN FILE. Everything worth testing about this kit --
the vocabulary, the template, the lint, the exit-code rule -- is pure.
Keeping the socket in one small module means the rest of the package is
tested with no token, no network and no Jira.

THE TOKEN NEVER LEAVES THIS FILE. It is read from the macOS Keychain, held
in one header, and never logged -- including on the error paths, where
quoting the request instead of the response would leak the Authorization
header.

NOTHING IN THIS FILE IS SPECIFIC TO ANY ONE JIRA PROJECT. Issue type ids and
custom field ids are NOT fixed across Jira sites, and are not even fixed
across two team-managed projects on the SAME site: each project gets its
own local copies of Epic/Task/Bug/Subtask with locally generated ids.
Hardcoding one company's ids into this file would silently break on
everyone else's Jira. `resolve_issue_type_id` and `find_field_by_custom_type`
resolve both dynamically instead, by NAME and by Jira Software's own fixed
custom-field TYPE keys respectively -- the latter chosen because an admin
can rename a field's display name but not its underlying type.
"""

from __future__ import annotations

import base64
import subprocess
from collections.abc import Sequence

import httpx

from jira_agent_kit.schema import Vocabulary

__all__ = [
    "JiraClient",
    "JiraError",
    "TokenMissing",
    "read_token",
]

# Fixed across every Jira Cloud site, because these fields are created by
# Jira Software's own bundled system app, not by a per-site admin. A
# display name ("Story point estimate", "Story Points", ...) can be
# renamed; this cannot.
STORY_POINTS_CUSTOM_TYPE = "com.pyxis.greenhopper.jira:jsw-story-points"
SPRINT_CUSTOM_TYPE = "com.pyxis.greenhopper.jira:gh-sprint"

_TIMEOUT = httpx.Timeout(20.0)


class JiraError(RuntimeError):
    """Jira refused, or could not be reached."""


class TokenMissing(JiraError):
    """No API token in the Keychain. Recoverable, and the fix is one command."""


def read_token(service: str, account: str) -> str:
    """Read the API token from the macOS Keychain.

    No pipe: `security ... | head` would return head's exit status and this
    would happily treat a missing item as success.

    THE VALUE MUST BE STORED AS THE `-w` ARGUMENT, not typed at the prompt.
    `security` truncates at 128 characters when it reads from its own prompt
    or from stdin, and an Atlassian API token is about 192 -- the
    interactive form fails silently, because entry and retype are truncated
    identically and so match. See docs/SETUP.md for the exact command.
    """
    try:
        done = subprocess.run(
            ["security", "find-generic-password", "-s", service, "-a", account, "-w"],
            capture_output=True,
            text=True,
        )
    except OSError:
        # No `security` binary -- a machine that is not macOS. The class
        # docstring promises a recoverable TokenMissing, not an OSError.
        done = None

    if done is None or done.returncode != 0 or not done.stdout.strip():
        raise TokenMissing(
            f"no API token in the Keychain for service {service!r} and "
            f"account {account!r}. Create a token at "
            "id.atlassian.com/manage-profile/security/api-tokens, copy it, "
            "then run:\n"
            f'  security add-generic-password -U -s {service} -a {account} '
            '-w "$(pbpaste | tr -d \'\\n\')"\n'
            "The value MUST be the -w argument. See docs/SETUP.md for why."
        )
    return done.stdout.strip()


class JiraClient:
    """Jira Cloud REST v3, plus the two Agile-API calls sprints need.

    Bodies are ADF; see schema.text_adf / schema.description_adf.
    """

    def __init__(
        self,
        site: str,
        email: str,
        token: str,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._base = f"https://{site}/rest/api/3"
        self._agile_base = f"https://{site}/rest/agile/1.0"
        credentials = base64.b64encode(f"{email}:{token}".encode()).decode()
        self._client = httpx.Client(
            headers={
                "Authorization": f"Basic {credentials}",
                "Accept": "application/json",
                "Content-Type": "application/json",
            },
            timeout=_TIMEOUT,
            transport=transport,
        )
        self._issue_type_cache: dict[tuple[str, str], str] = {}

    def _request(
        self, method: str, path: str, payload: dict | None = None, *, agile: bool = False
    ) -> httpx.Response:
        base = self._agile_base if agile else self._base
        try:
            response = self._client.request(method, f"{base}{path}", json=payload)
        except httpx.HTTPError as error:  # network, DNS, timeout
            raise JiraError(f"{method} {path} could not be sent: {error}") from error

        if response.status_code >= 400:
            # response.text, never the request: the request carries the
            # Authorization header and would leak the token into a log.
            raise JiraError(
                f"{method} {path} returned {response.status_code}: "
                f"{response.text[:500]}"
            )
        return response

    @staticmethod
    def _json(response: httpx.Response) -> dict:
        """Decode a response body, turning a bad one into a JiraError.

        `json.JSONDecodeError` subclasses ValueError; a caller that treats
        ValueError as "bad user input" would otherwise report a truncated
        body from a network intermediary as if the CALLER had done
        something wrong.
        """
        try:
            return response.json()
        except ValueError as error:
            raise JiraError(
                f"{response.request.method} {response.request.url.path} "
                f"returned {response.status_code} but the body could not be "
                f"read as JSON: {response.text[:200]}"
            ) from error

    # -- identity -----------------------------------------------------------

    def myself(self) -> dict:
        """Who Jira thinks this credential is.

        Jira answers an ANONYMOUS or wrong-account request with 404 rather
        than 401 on many endpoints, so a bad credential can read as "that
        issue does not exist" and send you looking at project permissions
        instead of the login. This endpoint answers the question directly.
        """
        return self._json(self._request("GET", "/myself"))

    # -- issue types ----------------------------------------------------------

    def resolve_issue_type_id(self, project_key: str, type_name: str) -> str:
        """The issue type id for `type_name` on THIS project, by name.

        Issue type ids are per-project on a team-managed Jira site: two
        different projects each get their own local Epic/Task/Bug/Subtask
        with their own generated ids. A hardcoded id from one project (or
        one company) will not exist on another. Cached per (project, type)
        for the life of this client, since it does not change mid-run.
        """
        key = (project_key, type_name.lower())
        if key in self._issue_type_cache:
            return self._issue_type_cache[key]

        payload = self._json(self._request("GET", f"/project/{project_key}"))
        for issue_type in payload.get("issueTypes", []):
            if issue_type["name"].lower() == type_name.lower():
                self._issue_type_cache[key] = issue_type["id"]
                return issue_type["id"]
        raise JiraError(
            f"no issue type named {type_name!r} on project {project_key}. "
            f"Available: {[t['name'] for t in payload.get('issueTypes', [])]}"
        )

    # -- issues ---------------------------------------------------------------

    def create_issue(
        self,
        project_key: str,
        issue_type_id: str,
        summary: str,
        description_adf: dict,
        labels: Sequence[str],
        vocabulary: Vocabulary,
        parent_key: str | None = None,
        extra_fields: dict | None = None,
    ) -> str:
        """Create one issue and return its key. Labels are validated first.

        `extra_fields` is a raw fields dict, merged in last -- for a custom
        field (an estimate, a sprint) whose id was resolved separately.
        """
        fields: dict = {
            "project": {"key": project_key},
            "issuetype": {"id": issue_type_id},
            "summary": summary,
            "description": description_adf,
            "labels": list(vocabulary.validate_labels(labels)),
        }
        if parent_key:
            # The epic-link field on a team-managed project, and the same
            # field a subtask uses for its parent task.
            fields["parent"] = {"key": parent_key}
        if extra_fields:
            fields.update(extra_fields)

        return self._json(self._request("POST", "/issue", {"fields": fields}))["key"]

    def add_comment(self, key: str, body_adf: dict) -> None:
        self._request("POST", f"/issue/{key}/comment", {"body": body_adf})

    def list_comments(self, key: str, limit: int = 3) -> list[str]:
        """The most recent comment bodies, flattened to plain text."""
        payload = self._json(
            self._request(
                "GET", f"/issue/{key}/comment?maxResults={limit}&orderBy=-created"
            )
        )
        bodies = []
        for comment in payload.get("comments", []):
            runs = [
                run.get("text", "")
                for block in comment.get("body", {}).get("content", [])
                for run in block.get("content", [])
                if run.get("type") == "text"
            ]
            bodies.append(" ".join(runs).strip())
        return bodies

    def list_comment_authors(self, key: str, limit: int = 100) -> list[str | None]:
        """The `accountId` of every comment's author, oldest first.

        A large default page (100, not list_comments's 3) -- this exists
        to COUNT, not to display, and undercounting a busy issue's real
        comment history would silently understate human_interactions.
        `None` for a comment with no author on the response (rare, but a
        malformed response must not crash a count) rather than raising.
        """
        payload = self._json(
            self._request("GET", f"/issue/{key}/comment?maxResults={limit}")
        )
        return [comment.get("author", {}).get("accountId") for comment in payload.get("comments", [])]

    def add_labels(self, key: str, labels: Sequence[str], vocabulary: Vocabulary) -> None:
        """Add labels without touching the ones already there.

        `update` adds; setting `fields.labels` would replace the whole list
        and silently drop every label the issue already carried.
        """
        checked = vocabulary.validate_label_names(labels)
        self._request(
            "PUT",
            f"/issue/{key}",
            {"update": {"labels": [{"add": label} for label in checked]}},
        )

    def get_issue(self, key: str) -> dict:
        return self._json(self._request("GET", f"/issue/{key}"))

    def delete_issue(self, key: str) -> None:
        """Permanently remove an issue. Intended ONLY for automated test
        cleanup -- everywhere else, closing an issue (a transition) is the
        directed act, and deletion has no undo.
        """
        self._request("DELETE", f"/issue/{key}")

    def list_transitions(self, key: str) -> dict[str, str]:
        """Map of transition name to id, for the issue's current status."""
        payload = self._json(self._request("GET", f"/issue/{key}/transitions"))
        return {item["name"]: item["id"] for item in payload.get("transitions", [])}

    def transition(self, key: str, transition_id: str) -> None:
        self._request(
            "POST", f"/issue/{key}/transitions", {"transition": {"id": transition_id}}
        )

    # -- custom fields (estimate, sprint) --------------------------------------

    def find_field_by_custom_type(
        self, project_key: str, issue_type_id: str, custom_type: str
    ) -> str | None:
        """The field id whose Jira Software `custom` type matches, or None.

        Matching by TYPE rather than display name survives an admin
        renaming "Story point estimate" to "Points" -- the type string is
        set by Jira Software itself and does not change. Returns None
        rather than guessing when the field is not on this issue type's
        create screen at all (for instance: estimation is not enabled on
        this project).
        """
        payload = self._json(
            self._request(
                "GET", f"/issue/createmeta/{project_key}/issuetypes/{issue_type_id}"
            )
        )
        for field in payload.get("fields", []):
            if field.get("schema", {}).get("custom") == custom_type:
                return field.get("fieldId")
        return None

    def find_estimate_field(self, project_key: str, issue_type_id: str) -> str | None:
        return self.find_field_by_custom_type(
            project_key, issue_type_id, STORY_POINTS_CUSTOM_TYPE
        )

    def find_sprint_field(self, project_key: str, issue_type_id: str) -> str | None:
        return self.find_field_by_custom_type(
            project_key, issue_type_id, SPRINT_CUSTOM_TYPE
        )

    # -- sprints (Agile API) ----------------------------------------------------

    def find_board_id(self, project_key: str) -> int | None:
        """The first Agile board for this project, or None.

        A project with no board (or with Sprints never enabled) returns
        None rather than raising -- not every project has one.
        """
        payload = self._json(
            self._request("GET", f"/board?projectKeyOrId={project_key}", agile=True)
        )
        values = payload.get("values", [])
        return values[0]["id"] if values else None

    def list_sprints(self, board_id: int) -> list[dict]:
        """Sprints on a board, each as {id, name, state}."""
        payload = self._json(
            self._request("GET", f"/board/{board_id}/sprint", agile=True)
        )
        return [
            {"id": s["id"], "name": s["name"], "state": s["state"]}
            for s in payload.get("values", [])
        ]

    # -- filters and dashboards --------------------------------------------------

    def create_filter(self, name: str, jql: str, description: str = "") -> str:
        """Create a saved filter and return its id."""
        payload = self._json(
            self._request(
                "POST", "/filter",
                {"name": name, "jql": jql, "description": description},
            )
        )
        return payload["id"]

    def create_dashboard(self, name: str, description: str = "") -> str:
        """Create a private dashboard and return its id."""
        payload = self._json(
            self._request(
                "POST", "/dashboard",
                {"name": name, "description": description, "sharePermissions": []},
            )
        )
        return payload["id"]

    def add_dashboard_gadget(
        self, dashboard_id: str, uri: str, color: str, row: int, column: int, title: str
    ) -> int:
        """Add a gadget to a dashboard and return its item id, unconfigured."""
        payload = self._json(
            self._request(
                "POST", f"/dashboard/{dashboard_id}/gadget",
                {
                    "uri": uri,
                    "color": color,
                    "position": {"row": row, "column": column},
                    "title": title,
                },
            )
        )
        return payload["id"]

    def configure_gadget(self, dashboard_id: str, gadget_id: int, config: dict) -> None:
        """Set a gadget's configuration.

        The write body is the config dict DIRECTLY. Verified against a
        scratch gadget before this was written: the READ response wraps it
        as {"key": "config", "value": {...}}, but PUT does not want the
        wrapper -- sending it back nests the config one level too deep and
        every gadget field is silently ignored.
        """
        self._request(
            "PUT", f"/dashboard/{dashboard_id}/items/{gadget_id}/properties/config",
            config,
        )
