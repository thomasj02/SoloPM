"""GitHub PR automation via the ``gh`` and ``git`` CLIs (Tier-1, agent-only).

A thin subprocess wrapper. The service depends only on the :class:`GitHubClient`
Protocol, so the transition side-effect logic is testable with a fake; the real
:class:`GitHub` shells out to ``git``/``gh``.
"""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from typing import Protocol

from .errors import SoloPMError


class GitHubError(SoloPMError):
    code = "github"
    status = 502


@dataclass
class PR:
    number: int
    url: str
    state: str  # open | merged | closed


class GitHubClient(Protocol):
    """The surface the service needs; implemented by :class:`GitHub` and test fakes."""

    def push_branch(self, repo: str, branch: str) -> None: ...

    def open_or_refresh_pr(
        self, repo: str, branch: str, base: str, title: str, body: str
    ) -> PR: ...

    def merge_pr(self, repo: str, number: int) -> None: ...

    def close_pr(self, repo: str, number: int) -> None: ...


class GitHub:
    """Real client over the local ``git`` and GitHub ``gh`` CLIs."""

    def __init__(self, timeout: float = 120.0):
        self.timeout = timeout

    def _run(self, args: list[str], cwd: str, *, check: bool = True) -> subprocess.CompletedProcess:
        try:
            proc = subprocess.run(
                args, cwd=cwd, capture_output=True, text=True, timeout=self.timeout
            )
        except FileNotFoundError as exc:
            raise GitHubError(f"Command not found: {args[0]} (is it installed?)") from exc
        except subprocess.TimeoutExpired as exc:
            raise GitHubError(f"Command timed out: {' '.join(args)}") from exc
        if check and proc.returncode != 0:
            detail = (proc.stderr or proc.stdout or "").strip()
            raise GitHubError(f"`{' '.join(args)}` failed: {detail}")
        return proc

    def push_branch(self, repo: str, branch: str) -> None:
        self._run(["git", "push", "-u", "origin", branch], cwd=repo)

    def _pr_for_branch(self, repo: str, branch: str) -> PR | None:
        proc = self._run(
            ["gh", "pr", "view", branch, "--json", "number,url,state"], cwd=repo, check=False
        )
        if proc.returncode != 0:
            return None  # no open PR for this branch
        data = json.loads(proc.stdout)
        return PR(number=int(data["number"]), url=data["url"], state=str(data["state"]).lower())

    def open_or_refresh_pr(self, repo: str, branch: str, base: str, title: str, body: str) -> PR:
        existing = self._pr_for_branch(repo, branch)
        if existing is not None:
            # The branch push above already refreshed the PR; just return it.
            return existing
        self._run(
            ["gh", "pr", "create", "--head", branch, "--base", base, "--title", title,
             "--body", body or ""],
            cwd=repo,
        )
        pr = self._pr_for_branch(repo, branch)
        if pr is None:
            raise GitHubError("PR was created but could not be read back from gh.")
        return pr

    def merge_pr(self, repo: str, number: int) -> None:
        self._run(["gh", "pr", "merge", str(number), "--squash", "--delete-branch"], cwd=repo)

    def close_pr(self, repo: str, number: int) -> None:
        self._run(["gh", "pr", "close", str(number), "--delete-branch"], cwd=repo)
