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
class OpenPR:
    """One row of the open-PR listing (SOLO-27): enough to match a PR to a ticket by
    its head branch and act on it safely — ``base`` gates adoption to PRs targeting the
    project's master, and ``cross_repo`` excludes fork PRs (their bare head name is a
    ref in the FORK; treating it as an origin branch could delete an unrelated ref)."""

    number: int
    url: str
    head: str  # head branch name (headRefName)
    base: str = ""  # base branch name (baseRefName)
    cross_repo: bool = False  # isCrossRepository (fork PR)


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


@dataclass
class LocalBranch:
    """A local branch plus the signals the prune helper (SOLO-23) needs."""

    name: str
    is_current: bool  # the checked-out branch of the main worktree (HEAD)
    upstream_gone: bool  # tracking branch deleted (the squash-merge cleanup signal)
    merged: bool  # reachable-merged into the queried master branch


class GitHubClient(Protocol):
    """The surface the service needs; implemented by :class:`GitHub` and test fakes."""

    def push_branch(self, repo: str, branch: str) -> None: ...

    def find_pr(self, repo: str, branch: str) -> PR | None: ...

    def list_open_prs(self, repo: str) -> list[OpenPR]: ...

    def open_or_refresh_pr(
        self, repo: str, branch: str, base: str, title: str, body: str
    ) -> PR: ...

    def pr_head(self, repo: str, number: int) -> str | None: ...

    def merge_pr(self, repo: str, number: int, branch: str | None = None) -> MergeResult: ...

    def close_pr(self, repo: str, number: int, branch: str | None = None) -> CloseResult: ...

    def list_worktrees(self, repo: str) -> list[Worktree]: ...

    def worktree_changed_files(self, worktree_path: str, base: str) -> set[str]: ...

    def count_unpushed_commits(self, repo: str) -> int: ...

    def local_branches(self, repo: str, master: str) -> list[LocalBranch]: ...

    def worktree_is_dirty(self, path: str) -> bool: ...

    def remove_worktree(self, repo: str, path: str) -> None: ...

    def delete_local_branch(self, repo: str, branch: str) -> None: ...

    def pr_merged_head(self, repo: str, number: int) -> str | None: ...

    def branch_tip(self, repo: str, branch: str) -> str | None: ...


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
        except OSError as exc:
            # A repo path that is a regular file or an unreadable directory makes the spawn
            # raise NotADirectoryError/PermissionError (OSError) from `cwd`, not a non-zero
            # exit. Wrap it as a GitHubError so callers' best-effort paths (status, radar,
            # cleanup) absorb it via their existing GitHubError handling instead of letting
            # a raw OSError escape to a 500.
            raise GitHubError(f"Could not run `{' '.join(args)}` in {cwd!r}: {exc}") from exc
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

    # A listing that FILLS this limit may be truncated (gh sorts newest-first, and the
    # match is typically old) — matching against a truncated view could bypass the
    # caller's ambiguity guard, so list_open_prs refuses instead of guessing.
    _OPEN_PR_LIMIT = 1000

    def list_open_prs(self, repo: str) -> list[OpenPR]:
        """All open PRs with their head branches, for convention-based discovery
        (SOLO-27). Raises :class:`GitHubError` on any gh failure, unparseable output,
        or a possibly-truncated listing — the caller decides whether that's fatal."""
        proc = self._run(
            ["gh", "pr", "list", "--state", "open",
             "--json", "number,url,headRefName,baseRefName,isCrossRepository",
             "--limit", str(self._OPEN_PR_LIMIT)],
            cwd=repo,
        )
        try:
            data = json.loads(proc.stdout or "[]")
            prs = [
                OpenPR(
                    number=int(d["number"]),
                    url=str(d["url"]),
                    head=str(d["headRefName"]),
                    base=str(d["baseRefName"]),
                    cross_repo=bool(d["isCrossRepository"]),
                )
                for d in data
            ]
        except (ValueError, KeyError, TypeError) as exc:
            raise GitHubError(f"Could not parse `gh pr list` output: {exc}") from exc
        if len(prs) >= self._OPEN_PR_LIMIT:
            raise GitHubError(
                f"{self._OPEN_PR_LIMIT}+ open PRs — the listing may be truncated, "
                "refusing to match against an incomplete view."
            )
        return prs

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
            # [SOLO-18] Never delete the local branch on a merge — it's checked out in the
            # developer's worktree, which SoloPM leaves in place. The remote is still cleaned.
            deleted = self._delete_branch_best_effort(repo, branch, delete_local=False)
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
            # [SOLO-18] Local branch kept (held by the worktree); remote still cleaned up.
            deleted = self._delete_branch_best_effort(repo, branch, delete_local=False)
            return MergeResult("merged", str(oid) if oid else None, deleted)
        # Otherwise trust an explicit "queued" signal — from gh's output or a still-open
        # readback. The branch is left for the queue to delete once the merge lands.
        if enqueued or state == "OPEN":
            return MergeResult("queued")
        # Readback inconclusive and gh didn't mention a queue → optimistically report the
        # squash-merge landed (pre-existing behaviour), but do NOT delete the branch: without
        # a positive MERGED readback the PR may still be open (e.g. auto-merge enabled with
        # checks pending), and deleting its head branch would break it. Cleanup only runs on
        # a confirmed merge.
        return MergeResult("merged", None)

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

    def pr_head(self, repo: str, number: int) -> str | None:
        """The PR's head branch name (the ref it was opened from), or ``None`` if unknown.

        The authoritative source for what branch a merge/close should clean up — preferred
        over any stored ticket field, which could have drifted from the real PR head.
        """
        head = self._pr_json(repo, number, "headRefName").get("headRefName")
        return str(head) if head else None

    def _merge_commit_oid(self, repo: str, number: int) -> str | None:
        oid = (self._pr_json(repo, number, "state,mergeCommit").get("mergeCommit") or {}).get("oid")
        return str(oid) if oid else None

    def _delete_branch_best_effort(
        self, repo: str, branch: str | None, *, delete_local: bool = True
    ) -> bool:
        """Delete a merged/closed branch locally and on the remote, swallowing failures.

        Returns True only if the **local** branch was actually removed. Branch cleanup must
        never gate the merge/close: each git call is run with ``check=False`` *and* wrapped
        so that even a timeout or missing command (which :meth:`_run` raises as
        ``GitHubError`` regardless of ``check``) is logged, not propagated — otherwise a
        cleanup hang after a successful ``gh pr merge``/``gh pr close`` would abort the
        transition and leave SoloPM out of sync with GitHub.

        ``delete_local=False`` skips the local ``git branch -D`` entirely (the remote delete
        still runs). The → done path uses this [SOLO-18]: a ticket's local branch is checked
        out in the developer's worktree, so deleting it can't succeed and isn't wanted —
        SoloPM leaves the branch (and its worktree) in place for the developer to remove.
        With ``delete_local=True`` (the → cancelled path), a branch a worktree still has
        checked out is detected and skipped rather than producing a noisy expected error.
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
        # Local delete: skipped outright on → done (the branch lives in the developer's
        # worktree), and skipped on → cancelled when a worktree still has it checked out.
        # Either way SoloPM keeps the local branch until the developer tears the worktree down.
        if not delete_local:
            logger.info("Leaving local branch %s in place (held by its worktree).", branch)
            return False
        if self._branch_in_worktree(repo, branch):
            logger.info(
                "Local branch %s is checked out in a worktree; leaving it in place.", branch
            )
            return False
        local = self._run_cleanup(["git", "branch", "-D", branch], repo)
        local_ok = self._local_branch_gone(local)
        if not local_ok:
            logger.warning("Best-effort delete of local branch %s failed: %s", branch, _detail(local))
        # "Deleted" only when the branch is gone everywhere SoloPM can reach — otherwise the
        # confirmation note would falsely claim a cleanup that left the GitHub (or local)
        # branch behind.
        return remote_ok and local_ok

    @staticmethod
    def _local_branch_gone(proc: "subprocess.CompletedProcess | None") -> bool:
        """Whether the local branch is gone after a ``git branch -D``.

        True on success, and also when git reports the branch isn't there — an absent local
        ref (e.g. a cleanup retry, or it was deleted manually) is a clean state, not a failure.
        False when the command raised (``None``) or failed for any other reason.
        """
        if proc is None:
            return False
        if proc.returncode == 0:
            return True
        return "not found" in (proc.stderr or proc.stdout or "").lower()

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

    def local_branches(self, repo: str, master: str) -> list[LocalBranch]:
        """Local branches annotated with the prune signals (SOLO-23): which is current
        (``HEAD``), whose upstream is ``[gone]``, and which are reachable-merged into
        ``master``. Best-effort: a non-git / odd repo (for-each-ref exits non-zero) yields an
        empty list rather than raising.
        """
        info = self._run(
            [
                "git", "for-each-ref",
                "--format=%(refname:short)%09%(HEAD)%09%(upstream:track)",
                "refs/heads",
            ],
            cwd=repo,
            check=False,
        )
        if info.returncode != 0:
            return []
        merged = self._run(
            ["git", "branch", "--format=%(refname:short)", "--merged", master],
            cwd=repo,
            check=False,
        )
        merged_names = (
            {ln.strip() for ln in merged.stdout.splitlines() if ln.strip()}
            if merged.returncode == 0
            else set()
        )
        branches: list[LocalBranch] = []
        for line in info.stdout.splitlines():
            parts = line.split("\t")
            name = parts[0].strip() if parts else ""
            if not name:
                continue
            head_marker = parts[1] if len(parts) > 1 else ""
            track = parts[2] if len(parts) > 2 else ""
            branches.append(
                LocalBranch(
                    name=name,
                    is_current=head_marker.strip() == "*",
                    upstream_gone=track.strip() == "[gone]",
                    merged=name in merged_names,
                )
            )
        return branches

    def worktree_is_dirty(self, path: str) -> bool:
        """True if the worktree at ``path`` has uncommitted changes (tracked or untracked).

        A query that can't run (the worktree dir is gone/odd) is treated as *dirty* so the
        caller never removes a worktree whose state it couldn't verify.
        """
        proc = self._run(["git", "status", "--porcelain"], cwd=path, check=False)
        if proc.returncode != 0:
            return True
        return bool(proc.stdout.strip())

    def remove_worktree(self, repo: str, path: str) -> None:
        """Remove the worktree at ``path`` (it must be clean). Raises ``GitHubError`` on failure."""
        self._run(["git", "worktree", "remove", path], cwd=repo)

    def delete_local_branch(self, repo: str, branch: str) -> None:
        """Force-delete a local branch (``-D`` — squash-merged branches aren't ``-d``-deletable).

        The name comes from ``local_branches`` (git's own ref enumeration) and is passed as a
        separate argv, so it can't be reinterpreted as an option (git forbids a leading ``-``
        in a ref). We deliberately do NOT run the stricter ``validate_branch_name`` here — it
        rejects git-valid names like ``feature+123`` and would raise outside the caller's
        per-branch ``GitHubError`` handling, aborting the whole prune. Raises ``GitHubError`` on
        a git failure (e.g. the branch is checked out elsewhere)."""
        self._run(["git", "branch", "-D", "--", branch], cwd=repo)

    def pr_merged_head(self, repo: str, number: int) -> str | None:
        """The head commit OID of the PR **only if it is actually MERGED on GitHub**, else
        ``None``.

        Gating on the live ``state`` matters: ``headRefOid`` is present for open/closed PRs
        too, and a ticket's stored ``pr_state`` can read ``merged`` in edge cases (e.g. an
        inconclusive merge readback) while the PR hasn't landed — so prune must confirm the
        live merge before force-deleting a branch on the strength of a ticket record."""
        info = self._pr_json(repo, number, "state,headRefOid")
        if str(info.get("state", "")).upper() != "MERGED":
            return None
        oid = info.get("headRefOid")
        return str(oid) if oid else None

    def branch_tip(self, repo: str, branch: str) -> str | None:
        """The local branch's tip commit OID, or ``None`` if it can't be resolved."""
        proc = self._run(
            ["git", "rev-parse", "--verify", "--quiet", f"refs/heads/{branch}"],
            cwd=repo,
            check=False,
        )
        out = proc.stdout.strip()
        return out if proc.returncode == 0 and out else None

    def count_unpushed_commits(self, repo: str) -> int:
        """Count locally-committed work that isn't on any remote — the board's "unpushed" signal.

        Counts commits reachable from local branches but not from any remote-tracking branch,
        **excluding branches whose upstream is gone** (their remote branch was deleted). SoloPM
        squash-merges each ticket's PR and deletes the remote branch while keeping the local one
        (SOLO-18); a plain ``git log --branches --not --remotes`` would then count those merged
        commits forever, because the squash commit on master has a different sha so the originals
        still look unpushed. Resolving the live branch set (``git for-each-ref`` with
        ``%(upstream:track)``) and dropping the ``[gone]`` ones keeps the count to genuinely
        unpushed work — branches never pushed (no upstream) or ahead of a live upstream; branches
        whose remote still exists are already excluded by ``--not --remotes``.

        Trade-off: a remote branch a user deleted by hand for *unmerged* work would also be
        excluded — acceptable for this squash-merge workflow.

        Best-effort: a non-git / bare / odd repo (or a query that exits non-zero) reports zero
        rather than failing; a missing ``git`` or a timeout still raises ``GitHubError`` for the
        caller to absorb.
        """
        refs = self._run(
            ["git", "for-each-ref", "--format=%(refname)%09%(upstream:track)", "refs/heads"],
            cwd=repo,
            check=False,
        )
        if refs.returncode != 0:
            return 0
        branches: list[str] = []
        for line in refs.stdout.splitlines():
            name, _, track = line.partition("\t")
            name = name.strip()
            if not name or track.strip() == "[gone]":
                continue  # detached/blank, or merged-and-cleaned (gone upstream)
            branches.append(name)
        if not branches:
            return 0
        # The branch refs are POSITIVE revisions and MUST come before ``--not`` — ``--not``
        # flips the sense of every revision that follows it, so listing the branches after it
        # would negate them too and the query would always return nothing.
        proc = self._run(
            ["git", "log", "--format=%H", *branches, "--not", "--remotes"],
            cwd=repo,
            check=False,
        )
        if proc.returncode != 0:
            return 0
        return sum(1 for line in proc.stdout.splitlines() if line.strip())

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
