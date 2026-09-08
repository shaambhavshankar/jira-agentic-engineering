"""Entry point: `python -m jira_agent_kit <command>` (or the installed `jira-agent` script)."""

import sys

from jira_agent_kit.cli import main

if __name__ == "__main__":
    sys.exit(main())
