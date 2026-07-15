"""Process-wide configuration: where the store lives, and where the server listens.

All values are overridable via environment variables so tests and power users can
relocate the store and point the CLI at a non-default backend.
"""

from __future__ import annotations

import os
from pathlib import Path

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8787


def home_dir() -> Path:
    """The SoloPM home directory (holds the SQLite store). ``~/.solopm`` by default."""
    override = os.environ.get("SOLOPM_HOME")
    if override:
        return Path(override).expanduser()
    return Path.home() / ".solopm"


def db_path() -> Path:
    """Path to the SQLite database file."""
    override = os.environ.get("SOLOPM_DB")
    if override:
        return Path(override).expanduser()
    return home_dir() / "solopm.db"


def server_host() -> str:
    return os.environ.get("SOLOPM_HOST", DEFAULT_HOST)


def server_port() -> int:
    raw = os.environ.get("SOLOPM_PORT")
    if raw:
        try:
            return int(raw)
        except ValueError:
            pass
    return DEFAULT_PORT


def base_url() -> str:
    """Base URL the CLI uses to reach the backend."""
    override = os.environ.get("SOLOPM_URL")
    if override:
        return override.rstrip("/")
    return f"http://{server_host()}:{server_port()}"


def extra_allowed_hosts() -> list[str]:
    """Extra Host-header values the server accepts (comma-separated env var).

    Needed when remote clients reach the backend by a name/IP the server can't infer
    (e.g. ``solopm mcp --url http://workstation:8787`` sends ``Host: workstation``).
    """
    raw = os.environ.get("SOLOPM_ALLOWED_HOSTS", "")
    return [h.strip() for h in raw.split(",") if h.strip()]


def default_project() -> str | None:
    """Project key inferred from the environment (set inside a session worktree)."""
    return os.environ.get("SOLOPM_PROJECT") or None
