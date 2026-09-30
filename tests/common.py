"""Shared settings for the test suites, taken from the environment set by run.sh."""

import os
import subprocess
from pathlib import Path

BASE = os.environ.get("TT_BASE_URL", "http://localhost:18000")
SMTP_PORT = int(os.environ.get("TT_SMTP_PORT", "13025"))
IMAP_PORT = int(os.environ.get("TT_IMAP_PORT", "13143"))
SHOTS = os.environ.get("TT_SHOTS", str(Path(__file__).resolve().parent / ".shots"))
REPO = str(Path(__file__).resolve().parents[1])
# docker compose, pointed at the test project (COMPOSE_PROJECT_NAME) and override files.
COMPOSE = ["docker", "compose", *os.environ.get("TT_COMPOSE_FILES", "-f docker-compose.yml").split(), "--profile", "mailtest"]


def psql(query: str) -> str:
    out = subprocess.run([*COMPOSE, "exec", "-T", "db", "psql", "-U", "tickets", "-d", "tickets", "-tA", "-c", query],
                         cwd=REPO, capture_output=True, text=True)
    return out.stdout.strip()
