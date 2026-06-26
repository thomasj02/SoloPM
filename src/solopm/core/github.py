"""GitHub PR automation via the ``gh`` and ``git`` CLIs (Tier-1, agent-only).

A thin subprocess wrapper. The service depends only on the :class:`GitHubClient`
Protocol, so the transition side-effect logic is testable with a fake; the real
:class:`GitHub` shells out to ``git``/``gh``.
"""

from __future__ import annotations

import json
import re
import subprocess
from dataclasses import dataclass
from typing import Protocol

from .errors import SoloPMError, ValidationError


class GitHubError(SoloPMError):
    code = "github"
    status = 502


# A conservative all-list for branch names. Crucially this rejects anything git could
# misread as an option (leading '-') or a refspec (':') — closing argument/refspec
# injection on `git push` — while allowing normal SoloPM branches like
# `solo-2-github-pr-side-effects` or `feature/x`.
_BRANCH_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]*$")


def validate_branch_name(branch: str) -> str:
    """Return ``branch`` if it is a safe git branch name, else raise ``ValidationError``."""
    if (
        not branch
        or len(branch) > 200
        or not _BRANCH_RE.match(branch)
        or ".." in branch
        or "@{" in branch
        or branch.endswith("/")
        or branch.endswith(".")
        or branch.endswith(".lock")
    ):
        raise ValidationError(f"Invalid branch name: {branch!r}.")
    return branch


@dataclass
class PR:
    number: int
    url: str
    state: str  # open | merged | closed


@dataclass
class Worktree:
    path: str
    branch: str | None  # short name, or None for a detached HEAD


class GitHubClient(Protocol):
    """The surface the service needs; implemented by :class:`GitHub` and test fakes."""

    def push_branch(self, repo: str, branch: str) -> None: ...

    def find_pr(self, repo: str, branch: str) -> PR | None: ...

    def open_or_refresh_pr(
        self, repo: str, branch: str, base: str, title: str, body: str
    ) -> PR: ...

    def merge_pr(self, repo: str, number: int) -> str | None: ...

    def close_pr(self, repo: str, number: int) -> None: ...

    def list_worktrees(self, repo: str) -> list[Worktree]: ...

    def worktree_changed_files(self, worktree_path: str, base: str) -> set[str]: ...


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

    # gh prints one of these to stderr when a branch simply has no PR (vs. a real error).
    _NO_PR_MARKERS = ("no pull requests found", "no open pull requests", "no pull request found")

    def push_branch(self, repo: str, branch: str) -> None:
        # Explicit src:dst refspec (and a pre-validated name) so the branch can never be
        # reinterpreted as a different ref or a git option.
        validate_branch_name(branch)
        refspec = f"refs/heads/{branch}:refs/heads/{branch}"
        self._run(["git", "push", "-u", "origin", refspec], cwd=repo)

    def find_pr(self, repo: str, branch: str) -> PR | None:
        proc = self._run(
            ["gh", "pr", "view", branch, "--json", "number,url,state"], cwd=repo, check=False
        )
        if proc.returncode != 0:
            stderr = (proc.stderr or "").lower()
            if any(marker in stderr for marker in self._NO_PR_MARKERS):
                return None  # genuinely no PR for this branch
            # An auth/network/other failure must NOT masquerade as "no PR" — else we'd
            # try to create a duplicate. Surface it.
            raise GitHubError(
                f"`gh pr view {branch}` failed: {(proc.stderr or proc.stdout or '').strip()}"
            )
        data = json.loads(proc.stdout)
        return PR(number=int(data["number"]), url=data["url"], state=str(data["state"]).lower())

    def open_or_refresh_pr(self, repo: str, branch: str, base: str, title: str, body: str) -> PR:
        existing = self.find_pr(repo, branch)
        if existing is not None:
            # The branch push above already refreshed the PR; just return it.
            return existing
        self._run(
            ["gh", "pr", "create", "--head", branch, "--base", base, "--title", title,
             "--body", body or ""],
            cwd=repo,
        )
        pr = self.find_pr(repo, branch)
        if pr is None:
            raise GitHubError("PR was created but could not be read back from gh.")
        return pr

    # mergeStateStatus values where `gh pr merge` (no --auto) cannot land the PR
    # synchronously — it would error or, under a merge queue, only *enqueue* it. We refuse
    # BEFORE issuing the merge so the transition aborts with no external side effect (vs.
    # discovering it afterwards, when GitHub may merge/delete the branch later out of band).
    _NOT_MERGEABLE_NOW = frozenset({"BLOCKED", "BEHIND", "DIRTY", "DRAFT"})

    def merge_pr(self, repo: str, number: int) -> str | None:
        # Preflight: only land a PR that will squash-merge right now. A blocked/behind/draft
        # PR (or one gated by a merge queue) must not be handed to `gh pr merge`, because a
        # queued merge returns success without landing — and recording the ticket as merged
        # would then drift from a PR that hasn't actually merged. Best-effort: if the state
        # can't be read, fall through and let `gh pr merge` itself be the gate.
        try:
            pre = self._run(
                ["gh", "pr", "view", str(number), "--json", "state,mergeStateStatus"],
                cwd=repo, check=False,
            )
        except GitHubError:
            pre = None
        if pre is not None and pre.returncode == 0:
            try:
                info = json.loads(pre.stdout)
            except (ValueError, TypeError):
                info = {}
            state = str(info.get("state") or "").upper()
            status = str(info.get("mergeStateStatus") or "").upper()
            if state and state != "OPEN":
                raise GitHubError(f"PR #{number} is {state}, not open — cannot merge.")
            if status in self._NOT_MERGEABLE_NOW:
                raise GitHubError(
                    f"PR #{number} is not mergeable now (status={status}); resolve it or wait "
                    "for required checks / the merge queue before moving to done."
                )

        self._run(["gh", "pr", "merge", str(number), "--squash", "--delete-branch"], cwd=repo)
        # Read back the squash commit so the ticket can record exactly what landed. A
        # *failed* read-back is non-fatal: the merge already succeeded, so return None
        # rather than abort over a missing sha. `check=False` suppresses a non-zero exit,
        # but a timeout / missing `gh` still raises GitHubError from `_run`, so swallow
        # that too — the merge must not be undone by a flaky lookup.
        try:
            proc = self._run(
                ["gh", "pr", "view", str(number), "--json", "mergeCommit"], cwd=repo, check=False
            )
        except GitHubError:
            return None
        if proc.returncode != 0:
            return None
        try:
            data = json.loads(proc.stdout)
        except (ValueError, TypeError):
            return None
        oid = (data.get("mergeCommit") or {}).get("oid")
        return str(oid) if oid else None

    def close_pr(self, repo: str, number: int) -> None:
        self._run(["gh", "pr", "close", str(number), "--delete-branch"], cwd=repo)

    def list_worktrees(self, repo: str) -> list[Worktree]:
        out = self._run(["git", "worktree", "list", "--porcelain"], cwd=repo).stdout
        worktrees: list[Worktree] = []
        path: str | None = None
        branch: str | None = None
        for line in out.splitlines() + [""]:
            if line.startswith("worktree "):
                path = line[len("worktree ") :].strip()
                branch = None
            elif line.startswith("branch "):
                # e.g. "branch refs/heads/solo-9-foo" -> "solo-9-foo"
                branch = line[len("branch ") :].strip().removeprefix("refs/heads/")
            elif line == "" and path is not None:
                worktrees.append(Worktree(path=path, branch=branch))
                path = None
        return worktrees

    def worktree_changed_files(self, worktree_path: str, base: str) -> set[str]:
        files: set[str] = set()
        # Committed changes this branch introduced vs. the base (merge-base diff).
        committed = self._run(
            ["git", "diff", "--name-only", f"{base}...HEAD"], cwd=worktree_path, check=False
        )
        if committed.returncode == 0:
            files.update(p for p in committed.stdout.splitlines() if p.strip())
        # Uncommitted edits in the worktree (porcelain: 2 status cols, space, path).
        status = self._run(["git", "status", "--porcelain"], cwd=worktree_path, check=False)
        if status.returncode == 0:
            for line in status.stdout.splitlines():
                entry = line[3:].strip()
                if not entry:
                    continue
                # Renames/copies show "old -> new"; record the new path.
                files.add(entry.split(" -> ")[-1].strip().strip('"'))
        return files
