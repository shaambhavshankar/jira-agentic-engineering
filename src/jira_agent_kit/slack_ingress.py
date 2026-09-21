#!/usr/bin/env python3
"""Slack ingress -- verifying a mention, parsing it, posting back.

WHY THIS EXISTS. Every entry point into this kit today is a person
typing a CLI command in a terminal. This is what makes a public Slack
channel a second, valid entry point: @jae in a channel resolves to a
repo, becomes a task, and every step gets echoed back into the thread --
the "work happens in public" property that mattered more than any single
pipeline step in the source material this whole build is based on.

WHAT THIS DOES NOT DO, ON PURPOSE.

1. NO HOSTED SERVER SHIPS HERE. Every other component in this kit is a
   library plus a CLI invocation -- `factory_dashboard.py` writes a
   static file, `telemetry.py` opens a local SQLite file, nothing here
   runs a daemon. A Slack Events API webhook needs *something* listening
   on a public URL, but that something is deployment, not library code:
   docs/SLACK_SETUP.md shows a minimal reference receiver (stdlib
   `http.server`, no new dependency) that calls `verify_slack_signature`
   then `parse_event` from THIS module. Building and hosting that server
   is a decision for whoever deploys it, same as the existing git hooks
   are shipped as scripts to wire up, not services this kit runs itself.

2. NO LLM TRIAGE. A raw Slack sentence ("fix the flaky test in
   checkout") is not a `jira-agent create --problem ... --option ...`
   call -- that template needs a real judgment about the problem and its
   alternatives, which is exactly the kind of reasoning self_improve.py
   already draws a hard line around for its own observer-agent step.
   `parse_event` produces an `IngressJob` (repo, task text, thread) and
   stops there; turning that into a real Jira issue is a real Claude Code
   session's job, using the existing `create`/`start`/`finish` commands
   same as it does today, with the job's fields as its starting context.

3. NO LIVE VERIFICATION YET. This module is fully unit-tested against a
   real HMAC signature and a real Slack Web API response shape, but
   nothing here has been run against an actual Slack workspace -- that
   needs a Slack app registered by hand at api.slack.com/apps, OAuth
   scopes, and a bot token, none of which this kit can create for you.
   See docs/SLACK_SETUP.md.
"""

from __future__ import annotations

import hashlib
import hmac
import re
import time
from dataclasses import dataclass

import httpx

__all__ = [
    "IngressError",
    "IngressJob",
    "SlackClient",
    "extract_repo_override",
    "parse_event",
    "strip_mention",
    "verify_slack_signature",
]

_TIMEOUT = httpx.Timeout(15.0)
_MAX_SIGNATURE_AGE_SECONDS = 300  # Slack's own replay-attack guidance: 5 minutes


class IngressError(RuntimeError):
    """A Slack event could not be turned into a job, or Slack refused a call."""


def verify_slack_signature(signing_secret: str, timestamp: str, body: str, signature: str) -> bool:
    """Real HMAC-SHA256 verification of a Slack Events API webhook.

    Slack's own scheme: sign `v0:{timestamp}:{raw body}` with the app's
    signing secret, compare against the `X-Slack-Signature` header using
    a constant-time comparison. A timestamp older than 5 minutes is
    refused even with a mathematically correct signature -- an old,
    intercepted request replayed later must not re-trigger a task.
    A non-numeric timestamp fails closed (False), never raises: a
    malformed webhook is refused the same as a forged one, not a crash.
    """
    try:
        age = abs(time.time() - float(timestamp))
    except (TypeError, ValueError):
        return False
    if age > _MAX_SIGNATURE_AGE_SECONDS:
        return False

    base = f"v0:{timestamp}:{body}"
    expected = "v0=" + hmac.new(signing_secret.encode(), base.encode(), hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature)


_MENTION_RE = re.compile(r"<@([A-Z0-9]+)>\s*")


def strip_mention(text: str, *, bot_user_id: str) -> str:
    """Remove the leading `<@BOTID>` Slack renders for an app mention."""
    return _MENTION_RE.sub("", text, count=1).strip() if f"<@{bot_user_id}>" in text else text.strip()


_BRACKET_RE = re.compile(r"^\[([^\]]+)\]\s*")


def extract_repo_override(text: str) -> tuple[str | None, str]:
    """A leading `[repo-name]` names the repo explicitly, overriding
    whatever the channel maps to -- for a channel that isn't
    repo-specific. Returns (repo_or_None, remaining_text).
    """
    text = text.strip()
    match = _BRACKET_RE.match(text)
    if not match:
        return None, text
    return match.group(1), text[match.end():].strip()


@dataclass(frozen=True)
class IngressJob:
    channel: str
    thread_ts: str
    user: str
    repo: str
    task_text: str


def parse_event(payload: dict, *, bot_user_id: str, channel_map: dict[str, str]) -> IngressJob:
    """A Slack Events API payload -> an IngressJob, or IngressError.

    Repo resolution, in order: an explicit `[repo-name]` in the message
    text, then `channel_map[channel]`. Neither present is refused, not
    guessed -- opening an issue and a PR against the wrong repo costs
    real work to undo; asking which repo costs one Slack reply.
    """
    event = payload.get("event", {})
    if event.get("type") != "app_mention":
        raise IngressError(f"not an app_mention event: {event.get('type')!r}")

    channel = event.get("channel", "")
    raw_text = event.get("text", "")
    message_ts = event.get("ts", "")
    thread_ts = event.get("thread_ts") or message_ts

    stripped = strip_mention(raw_text, bot_user_id=bot_user_id)
    override, task_text = extract_repo_override(stripped)

    if not task_text:
        raise IngressError("the message is empty after removing the mention -- nothing to do")

    repo = override or channel_map.get(channel)
    if repo is None:
        raise IngressError(
            f"no repo mapped for channel {channel!r} and no [repo-name] override in the "
            "message -- reply with which repo this is for, e.g. \"[my-repo] ...\""
        )

    return IngressJob(
        channel=channel, thread_ts=thread_ts, user=event.get("user", ""),
        repo=repo, task_text=task_text,
    )


class SlackClient:
    """The one Slack Web API call this kit needs: posting into a thread.

    Uses the SAME `httpx.HTTPTransport` the Jira client uses, so the
    existing network guard in tests/conftest.py already covers this --
    no third guard class needed, unlike Jev's vendored httpx fork.
    """

    def __init__(self, token: str, transport: httpx.BaseTransport | None = None) -> None:
        self._client = httpx.Client(
            base_url="https://slack.com/api",
            headers={"Authorization": f"Bearer {token}"},
            timeout=_TIMEOUT,
            transport=transport,
        )

    def post_message(self, *, channel: str, text: str, thread_ts: str | None) -> None:
        """Post `text` into `channel`, threaded under `thread_ts` if given.

        Slack's Web API returns HTTP 200 for almost every failure, with
        `{"ok": false, "error": "..."}` in the body -- the HTTP status
        alone is never the signal, `ok` is.
        """
        payload = {"channel": channel, "text": text}
        if thread_ts:
            payload["thread_ts"] = thread_ts

        try:
            response = self._client.post("/chat.postMessage", json=payload)
        except httpx.HTTPError as error:
            raise IngressError(f"chat.postMessage could not be sent: {error}") from error

        if response.status_code >= 400:
            raise IngressError(f"chat.postMessage returned {response.status_code}: {response.text[:500]}")

        body = response.json()
        if not body.get("ok"):
            raise IngressError(f"chat.postMessage refused: {body.get('error', '(no error given)')}")
