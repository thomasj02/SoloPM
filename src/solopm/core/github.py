"""GitHub PR automation via the ``gh`` and ``git`` CLIs (Tier-1, agent-only).

A thin subprocess wrapper. The service depends only on the :class:`GitHubClient`
Protocol, so the transition side-effect logic is testable with a fake; the real
:class:`GitHub` shells out to ``git``/``gh``.
"""

from __future__ import annotations

import json
import logging
import re
import subprocess
from dataclasses import dataclass
from typing import Protocol

from .errors import SoloPMError, ValidationError

logger = logging.getLogger(__name__)


def _detail(proc: "subprocess.CompletedProcess | None") -> str:
    """Human-readable failure detail for a cleanup command (``None`` ⇒ it raised/timed out)."""
    if proc is None:
        return "command timed out or could not be run"
    return (proc.stderr or proc.stdout or "").strip()


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
class MergeResult:
    """Outcome of :meth:`GitHubClient.merge_pr`.

    ``state`` is ``"merged"`` when the PR actually landed, or ``"queued"`` when
    ``gh pr merge`` only enqueued it (a merge-queue-protected branch returns success
    without merging). ``sha`` is the squash commit when merged and known, else ``None``.
    ``branch_deleted`` records whether the best-effort local branch cleanup succeeded —
    it is *not* a gate on the merge, just a fact for the confirmation note. It is False
    for a queued merge (the branch is deleted later, by the queue) and for a branch left
    in place because a worktree holds it.
    """

    state: str  # merged | queued
    sha: str | None = None
    branch_deleted: bool = False


@dataclass
class CloseResult:
    """Outcome of :meth:`GitHubClient.close_pr`. ``branch_deleted`` mirrors
    :class:`MergeResult` — best-effort cleanup, never a gate on the close."""

    branch_deleted: bool = False


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

    def merge_pr(self, repo: str, number: int, branch: str | None = None) -> MergeResult: ...

    def close_pr(self, repo: str, number: int, branch: str | None = None) -> CloseResult: ...

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

    def merge_pr(self, repo: str, number: int, branch: str | None = None) -> MergeResult:
        """Squash-merge a PR, returning whether it actually landed or was only enqueued.

        The **merge** is the operation that gates the caller's transition; **branch
        cleanup is split out** as a separate best-effort step (``gh pr merge`` is run
        without ``--delete-branch``). A branch checked out in a worktree — the normal
        SoloPM workflow — cannot be deleted locally, and that failure must never abort a
        merge that already happened. Branch deletion is therefore attempted afterwards and
        its outcome only recorded in :attr:`MergeResult.branch_deleted`.

        The step is **idempotent**: a PR that is already ``MERGED`` is a no-op success
        (returning the recorded squash sha), so a retry after a partial failure can still
        land the ticket in ``done`` instead of erroring with "already merged". A PR that is
        ``CLOSED`` without ever merging is a genuine error and still raises.

        On a merge-queue-protected branch, ``gh pr merge`` exits 0 after merely *adding* a
        green PR to the queue rather than merging it — so success from the command is not
        proof the PR landed. Two signals separate the two: ``gh`` announces "added to the
        merge queue" in its own output, and a post-merge readback shows whether the PR is
        ``MERGED`` or still ``OPEN``. The readback (when it succeeds) is authoritative and
        also yields the squash sha; if it flakes, the command output is the fallback — so a
        queued PR is never silently reported as merged just because the readback timed out.
        A genuinely un-mergeable PR (conflicts, draft, blocked without a queue) makes
        ``gh pr merge`` exit non-zero, which raises and aborts before any state is recorded.
        """
        # Preflight, best-effort. Idempotent: an already-merged PR is a success, not an
        # error — return the recorded sha and (re)attempt branch cleanup. A closed-but-not-
        # merged PR genuinely cannot be merged. If the state can't be read, fall through and
        # let the merge command be the gate.
        pre_state = self._pr_state(repo, number)
        if pre_state == "MERGED":
            deleted = self._delete_branch_best_effort(repo, branch)
            return MergeResult("merged", self._merge_commit_oid(repo, number), deleted)
        if pre_state == "CLOSED":
            raise GitHubError(f"PR #{number} is CLOSED, not open — cannot merge.")

        merge = self._run(["gh", "pr", "merge", str(number), "--squash"], cwd=repo)
        # `gh` prints e.g. "Pull request #N will be added to the merge queue …" when it only
        # enqueues rather than merges. Captured here as the fallback queued signal.
        enqueued = "merge queue" in f"{merge.stdout or ''} {merge.stderr or ''}".lower()

        # Read back to confirm the merge and capture the squash sha. Best-effort: a flaky
        # read (timeout / non-zero / bad json) leaves state/oid empty and we fall back to the
        # command's own signal above — never papering a queued merge over as a real one.
        data = self._pr_json(repo, number, "state,mergeCommit")
        state = str(data.get("state") or "").upper()
        oid = (data.get("mergeCommit") or {}).get("oid")

        # A readback that positively shows the PR merged is the strongest signal.
        if oid or state == "MERGED":
            deleted = self._delete_branch_best_effort(repo, branch)
            return MergeResult("merged", str(oid) if oid else None, deleted)
        # Otherwise trust an explicit "queued" signal — from gh's output or a still-open
        # readback. The branch is left for the queue to delete once the merge lands.
        if enqueued or state == "OPEN":
            return MergeResult("queued")
        # Readback inconclusive and gh didn't mention a queue → assume the squash-merge landed.
        deleted = self._delete_branch_best_effort(repo, branch)
        return MergeResult("merged", None, deleted)

    def _pr_json(self, repo: str, number: int, fields: str) -> dict:
        """Read ``gh pr view <n> --json <fields>`` as a dict; ``{}`` if it can't be read.

        Best-effort by design — every caller treats an empty read as "unknown" and falls
        back to a safe default rather than failing.
        """
        try:
            proc = self._run(
                ["gh", "pr", "view", str(number), "--json", fields], cwd=repo, check=False
            )
        except GitHubError:
            return {}
        if proc.returncode != 0:
            return {}
        try:
            return json.loads(proc.stdout) or {}
        except (ValueError, TypeError):
            return {}

    def _pr_state(self, repo: str, number: int) -> str:
        """The PR's upper-cased state (``OPEN``/``MERGED``/``CLOSED``), or ``""`` if unknown."""
        return str(self._pr_json(repo, number, "state").get("state") or "").upper()

    def _merge_commit_oid(self, repo: str, number: int) -> str | None:
        oid = (self._pr_json(repo, number, "state,mergeCommit").get("mergeCommit") or {}).get("oid")
        return str(oid) if oid else None

    def _delete_branch_best_effort(self, repo: str, branch: str | None) -> bool:
        """Delete a merged/closed branch locally and on the remote, swallowing failures.

        Returns True only if the **local** branch was actually removed. Branch cleanup must
        never gate the merge/close: each git call is run with ``check=False`` *and* wrapped
        so that even a timeout or missing command (which :meth:`_run` raises as
        ``GitHubError`` regardless of ``check``) is logged, not propagated — otherwise a
        cleanup hang after a successful ``gh pr merge``/``gh pr close`` would abort the
        transition and leave SoloPM out of sync with GitHub. A branch checked out in a
        worktree (the normal SoloPM workflow) can't be deleted locally — that case is
        detected and skipped rather than producing a noisy expected error.
        """
        if not branch:
            return False
        try:
            validate_branch_name(branch)
        except ValidationError:
            return False  # never feed an unvalidated name to git
        # Remote delete is independent of local worktree state; attempt it regardless.
        # Fully-qualify the ref: a bare `--delete <name>` resolves to a same-named *tag*
        # when the branch is already gone, which would silently delete an unrelated tag.
        remote = self._run_cleanup(
            ["git", "push", "origin", "--delete", f"refs/heads/{branch}"], repo
        )
        remote_ok = self._remote_branch_gone(remote)
        if not remote_ok:
            logger.warning("Best-effort delete of remote branch %s failed: %s", branch, _detail(remote))
        # Local delete: skip a branch that a worktree has checked out (it can't be removed,
        # and that is expected — SoloPM keeps the worktree until the human cleans it up).
        if self._branch_in_worktree(repo, branch):
            logger.info(
                "Local branch %s is checked out in a worktree; leaving it in place.", branch
            )
            return False
        local = self._run_cleanup(["git", "branch", "-D", branch], repo)
        local_ok = local is not None and local.returncode == 0
        if not local_ok:
            logger.warning("Best-effort delete of local branch %s failed: %s", branch, _detail(local))
        # "Deleted" only when the branch is gone everywhere SoloPM can reach — otherwise the
        # confirmation note would falsely claim a cleanup that left the GitHub (or local)
        # branch behind.
        return remote_ok and local_ok

    @staticmethod
    def _remote_branch_gone(proc: "subprocess.CompletedProcess | None") -> bool:
        """Whether the remote branch is gone after a ``git push --delete``.

        True on success, and also when the push reports the ref is already absent (e.g. the
        repo's auto-delete-on-merge already removed it) — that is a clean state, not a
        cleanup failure. False when the command raised (``None``) or failed for any other
        reason (network / permission), so the caller doesn't over-claim deletion.
        """
        if proc is None:
            return False
        if proc.returncode == 0:
            return True
        return "remote ref does not exist" in (proc.stderr or proc.stdout or "").lower()

    def _run_cleanup(self, args: list[str], repo: str) -> subprocess.CompletedProcess | None:
        """Run a best-effort cleanup command, returning ``None`` instead of raising.

        Cleanup must never abort a transition, so this swallows the ``GitHubError`` that
        :meth:`_run` raises on a timeout / missing command in addition to ``check=False``
        already absorbing non-zero exits.
        """
        try:
            return self._run(args, cwd=repo, check=False)
        except GitHubError:
            return None

    def _branch_in_worktree(self, repo: str, branch: str) -> bool:
        try:
            return any(wt.branch == branch for wt in self.list_worktrees(repo))
        except GitHubError:
            return False  # can't tell → fall through and let the delete attempt decide

    def close_pr(self, repo: str, number: int, branch: str | None = None) -> CloseResult:
        """Close a PR (a ticket was cancelled), idempotently and tolerant of branch cleanup.

        Mirrors :meth:`merge_pr`: the close is the gating step, branch deletion is a
        separate best-effort step (no ``--delete-branch``). A PR that is already ``CLOSED``
        is a no-op success rather than an error, so a retry after a partial failure still
        cancels the ticket. A ``MERGED`` PR is *not* idempotently closed — that work landed,
        so ``gh pr close`` is still attempted and its error surfaces rather than letting the
        ticket silently record merged work as abandoned.
        """
        if self._pr_state(repo, number) != "CLOSED":
            self._run(["gh", "pr", "close", str(number)], cwd=repo)
        return CloseResult(branch_deleted=self._delete_branch_best_effort(repo, branch))

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
