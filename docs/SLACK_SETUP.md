# Slack setup

Optional. `jira-agent` works entirely from the terminal without any of
this — this only adds `@jae` in a public channel as a second entry
point. Four things, in order.

## 1. Register a Slack app

Go to `api.slack.com/apps`, create a new app. Under **OAuth & Permissions**,
add these Bot Token Scopes:

- `app_mentions:read` — to see when someone tags the bot
- `chat:write` — to post back into the thread
- `files:read` — to read an attached image, if the message has one

Install the app to your workspace. Copy the **Bot User OAuth Token**
(`xoxb-...`) and the **Signing Secret** (under **Basic Information**) —
you need both.

**Why both, not just the token.** The token lets this kit post messages.
The signing secret is what proves an incoming webhook actually came from
Slack — without checking it, anyone who finds your receiver's URL could
POST a fake mention and trigger a real pipeline run.

## 2. Store the bot token in the Keychain

Same pattern as the Jira token, same reason — see `docs/SETUP.md` §3 for
why the token has to be the literal `-w` argument, not typed at a prompt:

```bash
security add-generic-password -U -s jira-agent-kit-slack \
  -a your-bot-app-name -w "$(pbpaste | tr -d '\n')"
```

The signing secret does **not** go in the Keychain the same way — it's
read directly by whatever receiver script you run (see step 4), typically
from its own environment variable, because it's checked on every single
incoming request before any Keychain lookup happens.

## 3. Map channels to repos

Create a JSON file — anywhere, this kit doesn't care where — mapping
each Slack channel ID to the repo it's for:

```json
{
  "C0123ABCDEF": "your-repo-name",
  "C0456GHIJKL": "another-repo"
}
```

**Finding a channel ID:** right-click the channel name in Slack → View
channel details → the ID is at the bottom.

Don't have this file, or a channel isn't in it? A message can still name
its repo explicitly: `@jae [your-repo-name] fix the flaky test`. No
mapping and no bracket: the bot can't guess, and `slack-ingest` refuses
with a message asking which repo — better than opening an issue and a PR
against the wrong codebase.

## 4. Run a receiver

This kit ships no hosted server — every other component here is a
library plus a CLI, and this doesn't break that pattern. A minimal
reference receiver, using nothing but the standard library, verifies
each webhook then hands it to `jira-agent slack-ingest`:

```python
#!/usr/bin/env python3
"""Reference Slack Events API receiver. Runs on your own infrastructure --
this kit does not host this for you. Point Slack's Event Subscriptions
Request URL at wherever this ends up running, over HTTPS.
"""
import json
import os
import subprocess
from http.server import BaseHTTPRequestHandler, HTTPServer

from jira_agent_kit.slack_ingress import verify_slack_signature

SIGNING_SECRET = os.environ["SLACK_SIGNING_SECRET"]
BOT_USER_ID = os.environ["SLACK_BOT_USER_ID"]
CHANNEL_MAP_FILE = os.environ.get("JIRA_AGENT_CHANNEL_MAP")  # optional


class Handler(BaseHTTPRequestHandler):
    def do_POST(self):
        body = self.rfile.read(int(self.headers["Content-Length"])).decode()
        timestamp = self.headers.get("X-Slack-Request-Timestamp", "")
        signature = self.headers.get("X-Slack-Signature", "")

        if not verify_slack_signature(SIGNING_SECRET, timestamp, body, signature):
            self.send_response(401)
            self.end_headers()
            return

        payload = json.loads(body)

        # Slack's one-time URL verification handshake.
        if payload.get("type") == "url_verification":
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.end_headers()
            self.wfile.write(payload["challenge"].encode())
            return

        self.send_response(200)  # ack immediately -- Slack retries on a slow response
        self.end_headers()

        # Real work happens after acking, out-of-band. This reference
        # implementation shells out to slack-ingest and prints the
        # resolved job; wiring that job into a real Claude Code session
        # is the one step no receiver script can automate for you (see
        # slack_ingress.py's module docstring -- turning a raw sentence
        # into a real issue needs real judgment, not a fixed pipeline).
        payload_path = "/tmp/slack_event.json"
        with open(payload_path, "w") as f:
            json.dump(payload, f)

        cmd = ["jira-agent", "slack-ingest", "--payload-file", payload_path,
               "--bot-user-id", BOT_USER_ID]
        if CHANNEL_MAP_FILE:
            cmd += ["--channel-map-file", CHANNEL_MAP_FILE]
        subprocess.run(cmd)


if __name__ == "__main__":
    HTTPServer(("0.0.0.0", 8080), Handler).serve_forever()
```

Put this behind HTTPS (a reverse proxy, a tunnel, whatever your
infrastructure already uses) and point Slack's **Event Subscriptions**
Request URL at it. Subscribe to the `app_mention` bot event.

## What happens after `slack-ingest` prints a job

`slack-ingest` resolves a mention into a repo and a task description —
it does not create a Jira issue, open a PR, or run any code. Turning
"fix the flaky test in checkout" into a real `jira-agent create` call,
with a stated problem, real alternatives, and predicted axes, needs real
judgment about the problem — the same reasoning a person or a Claude
Code session already provides when running `create` from a terminal.
The printed job is that session's starting context, not a finished
pipeline.

## Verifying it worked

Tag the bot in a mapped channel. The receiver should log the resolved
job (repo, task text, thread). No pipeline runs yet — that's the next
step above, done by whoever (or whatever) is watching for these jobs.
