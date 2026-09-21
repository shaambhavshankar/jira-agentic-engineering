"""Slack ingress -- JAE v2 spec §11.

WHAT THIS DOES NOT DO. No hosted webhook server ships in this kit --
every other component here is a library plus a CLI, never a daemon, and
this doesn't break that pattern (see slack_ingress.py's module docstring
and docs/SLACK_SETUP.md for the one-time, by-hand Slack app registration
this needs before it can run live). It also does not turn a raw Slack
sentence into a full `create --problem/--option/--chosen` template --
that's an LLM triage step, the same principled scope-cut self_improve.py
makes for its own observer-agent boundary. What this DOES do: verify a
real webhook came from Slack, parse a mention into a repo + task text,
and post progress back into the thread.
"""

from __future__ import annotations

import hashlib
import hmac
import time

import httpx
import pytest

from jira_agent_kit.slack_ingress import (
    IngressError,
    SlackClient,
    extract_repo_override,
    parse_event,
    strip_mention,
    verify_slack_signature,
)

SIGNING_SECRET = "shh-a-real-secret-would-be-longer"


def _sign(body: str, timestamp: str, secret: str = SIGNING_SECRET) -> str:
    base = f"v0:{timestamp}:{body}"
    digest = hmac.new(secret.encode(), base.encode(), hashlib.sha256).hexdigest()
    return f"v0={digest}"


# --- verify_slack_signature: real HMAC, no network --------------------------


def test_a_correctly_signed_request_verifies():
    body = '{"type":"event_callback"}'
    ts = str(int(time.time()))
    sig = _sign(body, ts)

    assert verify_slack_signature(SIGNING_SECRET, ts, body, sig) is True


def test_a_tampered_body_fails_verification():
    body = '{"type":"event_callback"}'
    ts = str(int(time.time()))
    sig = _sign(body, ts)

    assert verify_slack_signature(SIGNING_SECRET, ts, body + "tampered", sig) is False


def test_the_wrong_secret_fails_verification():
    body = '{"type":"event_callback"}'
    ts = str(int(time.time()))
    sig = _sign(body, ts, secret="wrong-secret")

    assert verify_slack_signature(SIGNING_SECRET, ts, body, sig) is False


def test_a_stale_timestamp_fails_verification():
    """Slack's own replay-attack guidance: reject anything older than 5
    minutes, even with a mathematically correct signature.
    """
    body = '{"type":"event_callback"}'
    ts = str(int(time.time()) - 600)  # 10 minutes old
    sig = _sign(body, ts)

    assert verify_slack_signature(SIGNING_SECRET, ts, body, sig) is False


def test_a_garbage_timestamp_fails_verification_not_a_crash():
    body = "x"
    assert verify_slack_signature(SIGNING_SECRET, "not-a-number", body, "v0=whatever") is False


# --- strip_mention / extract_repo_override -----------------------------------


def test_strip_mention_removes_the_bot_tag():
    text = "<@U123BOT> fix the flaky test in checkout"
    assert strip_mention(text, bot_user_id="U123BOT") == "fix the flaky test in checkout"


def test_strip_mention_with_no_match_returns_the_text_unchanged():
    text = "no mention here"
    assert strip_mention(text, bot_user_id="U123BOT") == "no mention here"


def test_extract_repo_override_finds_a_bracketed_repo_name():
    repo, rest = extract_repo_override("[repo-a] fix the flaky test")
    assert repo == "repo-a"
    assert rest == "fix the flaky test"


def test_extract_repo_override_with_no_bracket_returns_none():
    repo, rest = extract_repo_override("fix the flaky test")
    assert repo is None
    assert rest == "fix the flaky test"


def test_extract_repo_override_strips_surrounding_whitespace():
    repo, rest = extract_repo_override("  [repo-a]   do the thing  ")
    assert repo == "repo-a"
    assert rest == "do the thing"


# --- parse_event: the full pipeline from a raw Slack payload ---------------


def _mention_event(*, channel="C123", text="<@U123BOT> fix it", ts="1700000000.000100", thread_ts=None):
    return {
        "type": "event_callback",
        "event": {
            "type": "app_mention",
            "channel": channel,
            "user": "U999HUMAN",
            "text": text,
            "ts": ts,
            **({"thread_ts": thread_ts} if thread_ts else {}),
        },
    }


def test_parse_event_resolves_repo_from_channel_map():
    job = parse_event(
        _mention_event(channel="C123"), bot_user_id="U123BOT",
        channel_map={"C123": "repo-a"},
    )
    assert job.repo == "repo-a"
    assert job.task_text == "fix it"
    assert job.channel == "C123"
    assert job.user == "U999HUMAN"


def test_parse_event_explicit_bracket_overrides_the_channel_map():
    job = parse_event(
        _mention_event(channel="C123", text="<@U123BOT> [jira-agent-kit] fix it"),
        bot_user_id="U123BOT", channel_map={"C123": "repo-a"},
    )
    assert job.repo == "jira-agent-kit"


def test_parse_event_with_no_mapping_and_no_bracket_raises_asking_which_repo():
    with pytest.raises(IngressError, match="which repo"):
        parse_event(
            _mention_event(channel="C_UNKNOWN"), bot_user_id="U123BOT", channel_map={},
        )


def test_parse_event_uses_thread_ts_when_present_else_the_message_ts():
    job = parse_event(
        _mention_event(channel="C123", ts="200.1", thread_ts="100.1"),
        bot_user_id="U123BOT", channel_map={"C123": "repo-a"},
    )
    assert job.thread_ts == "100.1"


def test_parse_event_falls_back_to_message_ts_with_no_existing_thread():
    job = parse_event(
        _mention_event(channel="C123", ts="200.1"),
        bot_user_id="U123BOT", channel_map={"C123": "repo-a"},
    )
    assert job.thread_ts == "200.1"


def test_parse_event_ignores_a_non_mention_event_type():
    payload = {"type": "event_callback", "event": {"type": "message", "text": "irrelevant"}}
    with pytest.raises(IngressError, match="app_mention"):
        parse_event(payload, bot_user_id="U123BOT", channel_map={"C123": "repo-a"})


def test_parse_event_rejects_empty_task_text():
    with pytest.raises(IngressError, match="empty"):
        parse_event(
            _mention_event(channel="C123", text="<@U123BOT>   "),
            bot_user_id="U123BOT", channel_map={"C123": "repo-a"},
        )


# --- SlackClient: guarded network, real Slack Web API shape ----------------


def test_post_message_sends_the_expected_body_and_auth_header():
    captured = {}

    def handler(request):
        captured["auth"] = request.headers.get("authorization")
        captured["body"] = request.content
        return httpx.Response(200, json={"ok": True})

    client = SlackClient(token="xoxb-fake", transport=httpx.MockTransport(handler))
    client.post_message(channel="C123", text="hello", thread_ts="100.1")

    assert captured["auth"] == "Bearer xoxb-fake"
    assert b'"channel":"C123"' in captured["body"] or b"C123" in captured["body"]
    assert b"100.1" in captured["body"]


def test_post_message_raises_on_a_slack_level_error_even_with_http_200():
    """Slack's Web API returns HTTP 200 with `{"ok": false, "error": "..."}`
    for almost every failure -- the HTTP status alone is not the signal.
    """
    def handler(request):
        return httpx.Response(200, json={"ok": False, "error": "channel_not_found"})

    client = SlackClient(token="xoxb-fake", transport=httpx.MockTransport(handler))

    with pytest.raises(Exception, match="channel_not_found"):
        client.post_message(channel="C_BAD", text="hello", thread_ts=None)


def test_post_message_raises_on_a_real_http_error():
    def handler(request):
        return httpx.Response(500, text="internal error")

    client = SlackClient(token="xoxb-fake", transport=httpx.MockTransport(handler))

    with pytest.raises(Exception):
        client.post_message(channel="C123", text="hello", thread_ts=None)
