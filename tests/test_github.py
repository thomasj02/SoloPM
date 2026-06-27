"""GitHub PR side effects on transition (SOLO-2), tested with a fake client."""

import pytest

from solopm.core.errors import NotFoundError, ValidationError
from solopm.core.github import CloseResult, PR, GitHubError, MergeResult
from solopm.core.service import Service
from solopm.core.store import Store


class FakeGitHub:
    """Records calls; no real git/gh. `fail_on` makes one method raise.

    ``branch_deleted`` is what merge_pr/close_pr report for best-effort branch cleanup;
    set it False to simulate a branch that could not be removed (e.g. checked out in a
    worktree) — the merge/close itself still succeeds.
    """

    def __init__(
        self,
        pr_number: int = 17,
        fail_on: str | None = None,
        merge_sha: str | None = "1a2b3c4d5e6f",
        merge_state: str = "merged",
        branch_deleted: bool = True,
    ):
        self.calls: list[tuple] = []
        self.pr_number = pr_number
        self.fail_on = fail_on
        self.merge_sha = merge_sha
        self.merge_state = merge_state  # "merged" or "queued"
        self.branch_deleted = branch_deleted
        self.head_branch = None  # the PR head, recorded when a PR is opened/found
        self.merge_branch = None  # branch the client was last asked to clean up
        self.close_branch = None

    def _maybe_fail(self, name: str) -> None:
        if self.fail_on == name:
            raise GitHubError(f"boom in {name}")

    def push_branch(self, repo, branch):
        self._maybe_fail("push_branch")
        self.calls.append(("push", branch))

    def find_pr(self, repo, branch):
        self._maybe_fail("find_pr")
        self.calls.append(("find", branch))
        self.head_branch = branch  # the PR matched on this branch → it is the head
        return PR(number=self.pr_number, url=f"https://github.com/thomasj02/SoloPM/pull/{self.pr_number}", state="open")

    def open_or_refresh_pr(self, repo, branch, base, title, body):
        self._maybe_fail("open_or_refresh_pr")
        self.calls.append(("pr", branch, base, title))
        self.head_branch = branch
        return PR(number=self.pr_number, url=f"https://github.com/thomasj02/SoloPM/pull/{self.pr_number}", state="open")

    def pr_head(self, repo, number):
        self._maybe_fail("pr_head")
        self.calls.append(("head", number))
        return self.head_branch

    def merge_pr(self, repo, number, branch=None):
        self._maybe_fail("merge_pr")
        self.calls.append(("merge", number))
        self.merge_branch = branch  # the branch the client was asked to clean up
        if self.merge_state == "queued":
            return MergeResult("queued")
        return MergeResult("merged", self.merge_sha, branch_deleted=self.branch_deleted)

    def close_pr(self, repo, number, branch=None):
        self._maybe_fail("close_pr")
        self.calls.append(("close", number))
        self.close_branch = branch
        return CloseResult(branch_deleted=self.branch_deleted)


def _svc(tmp_path, github=None, repo="/tmp/repo"):
    store = Store(tmp_path / "solopm.db")
    store.init()
    svc = Service(store, github=github)
    svc.add_project(key="SOLO", name="SoloPM", repo=repo, master="main")
    return svc


def _to_ai_review(svc, branch="solo-9-feature"):
    t = svc.create_ticket(project="SOLO", title="x", description="the body")
    svc.move_ticket(t.id, "in-progress")
    return t.id, svc.move_ticket(t.id, "in-ai-review", branch=branch, actor="claude")


def test_in_ai_review_pushes_branch_and_opens_pr(tmp_path):
    gh = FakeGitHub()
    svc = _svc(tmp_path, github=gh)
    tid, moved = _to_ai_review(svc)
    assert ("push", "solo-9-feature") in gh.calls
    assert ("pr", "solo-9-feature", "main", "SOLO-1: x") in gh.calls
    assert moved.branch == "solo-9-feature"
    assert moved.pr_number == 17
    assert moved.pr_url.endswith("/pull/17")
    assert moved.pr_state == "open"


def test_done_squash_merges_pr(tmp_path):
    gh = FakeGitHub()
    svc = _svc(tmp_path, github=gh)
    tid, _ = _to_ai_review(svc)
    svc.move_ticket(tid, "in-human-review", actor="codex")
    done = svc.move_ticket(tid, "done", actor="human")
    assert ("merge", 17) in gh.calls
    assert done.pr_state == "merged"


def test_cancelled_closes_pr(tmp_path):
    gh = FakeGitHub()
    svc = _svc(tmp_path, github=gh)
    tid, _ = _to_ai_review(svc)
    cancelled = svc.move_ticket(tid, "cancelled", actor="claude")
    assert ("close", 17) in gh.calls
    assert cancelled.pr_state == "closed"


def test_in_human_review_has_no_git_side_effect(tmp_path):
    gh = FakeGitHub()
    svc = _svc(tmp_path, github=gh)
    tid, _ = _to_ai_review(svc)
    before = len(gh.calls)
    svc.move_ticket(tid, "in-human-review", actor="codex")
    assert len(gh.calls) == before  # no push/merge/close on → in-human-review


def test_branchless_ticket_triggers_nothing(tmp_path):
    gh = FakeGitHub()
    svc = _svc(tmp_path, github=gh)
    t = svc.create_ticket(project="SOLO", title="y")
    svc.move_ticket(t.id, "in-progress")
    svc.move_ticket(t.id, "in-ai-review", actor="claude")  # no --branch
    assert gh.calls == []


def test_no_github_client_records_branch_but_no_pr(tmp_path):
    svc = _svc(tmp_path, github=None)
    t = svc.create_ticket(project="SOLO", title="x")
    svc.move_ticket(t.id, "in-progress")
    moved = svc.move_ticket(t.id, "in-ai-review", branch="b", actor="claude")
    assert moved.branch == "b"  # branch is still recorded
    assert moved.pr_dict() is None  # but no PR automation


def test_no_repo_configured_skips_side_effects(tmp_path):
    gh = FakeGitHub()
    svc = _svc(tmp_path, github=gh, repo=None)
    t = svc.create_ticket(project="SOLO", title="x")
    svc.move_ticket(t.id, "in-progress")
    svc.move_ticket(t.id, "in-ai-review", branch="b", actor="claude")
    assert gh.calls == []


def test_human_move_to_ai_review_does_not_push(tmp_path):
    # [P1] Git automation is agent-only: a human supplying a branch must not push/open a PR.
    gh = FakeGitHub()
    svc = _svc(tmp_path, github=gh)
    t = svc.create_ticket(project="SOLO", title="x")
    svc.move_ticket(t.id, "in-progress")
    moved = svc.move_ticket(t.id, "in-ai-review", branch="b", actor="human")
    assert gh.calls == []  # no push/PR for a human
    assert moved.branch == "b"  # branch is still recorded
    assert moved.pr_dict() is None


def test_done_resolves_pr_by_branch_when_unrecorded(tmp_path):
    # [P2b] A branch-backed ticket with no recorded PR still gets merged on done — the PR
    # is resolved by branch (SoloPM owns the branch).
    gh = FakeGitHub()
    svc = _svc(tmp_path, github=gh)
    t = svc.create_ticket(project="SOLO", title="x")
    svc.move_ticket(t.id, "in-progress")
    svc.move_ticket(t.id, "in-ai-review", branch="b", actor="human")  # records branch, no PR
    svc.move_ticket(t.id, "in-human-review", actor="codex")
    assert svc.get_ticket(t.id).pr_number is None
    done = svc.move_ticket(t.id, "done", actor="human")
    assert ("find", "b") in gh.calls
    assert ("merge", 17) in gh.calls
    assert done.pr_state == "merged"


def test_find_pr_distinguishes_no_pr_from_real_error(tmp_path, monkeypatch):
    # [P2a] gh "no PR" → None; any other gh failure must surface, not look like "no PR".
    from solopm.core.github import GitHub

    gh = GitHub()

    def fake_run(returncode, stderr):
        class P:
            pass
        p = P()
        p.returncode, p.stdout, p.stderr = returncode, "", stderr
        return p

    monkeypatch.setattr(gh, "_run", lambda *a, **k: fake_run(1, "no pull requests found for branch 'b'"))
    assert gh.find_pr("/repo", "b") is None

    monkeypatch.setattr(gh, "_run", lambda *a, **k: fake_run(1, "HTTP 401: Bad credentials"))
    with pytest.raises(GitHubError):
        gh.find_pr("/repo", "b")


def test_malicious_branch_name_is_rejected_before_any_git_op(tmp_path):
    # [P1] git refspec/argument injection: a hostile branch never reaches git.
    gh = FakeGitHub()
    svc = _svc(tmp_path, github=gh)
    t = svc.create_ticket(project="SOLO", title="x")
    svc.move_ticket(t.id, "in-progress")
    for bad in ("HEAD:refs/heads/main", "--delete", "a b", "../evil", "x:y", "-x", "foo.", "x@{0}"):
        with pytest.raises(ValidationError):
            svc.move_ticket(t.id, "in-ai-review", branch=bad, actor="claude")
    assert gh.calls == []  # nothing pushed/opened for any rejected branch
    assert svc.get_ticket(t.id).state == "in-progress"


def test_local_validation_runs_before_side_effects(tmp_path):
    # [P1] an invalid `after` must abort BEFORE any push/merge/close.
    gh = FakeGitHub()
    svc = _svc(tmp_path, github=gh)
    tid, _ = _to_ai_review(svc)  # PR #17 recorded
    svc.move_ticket(tid, "in-human-review", actor="codex")
    gh.calls.clear()
    with pytest.raises(NotFoundError):
        svc.move_ticket(tid, "done", after="SOLO-999", actor="human")
    assert gh.calls == []  # no merge happened
    assert svc.get_ticket(tid).state == "in-human-review"


def test_git_failure_aborts_the_move(tmp_path):
    gh = FakeGitHub(fail_on="push_branch")
    svc = _svc(tmp_path, github=gh)
    t = svc.create_ticket(project="SOLO", title="x")
    svc.move_ticket(t.id, "in-progress")
    with pytest.raises(GitHubError):
        svc.move_ticket(t.id, "in-ai-review", branch="b", actor="claude")
    # The move is aborted: the ticket stays in-progress with no branch/PR recorded.
    aborted = svc.get_ticket(t.id)
    assert aborted.state == "in-progress"
    assert aborted.branch is None


def _comments(svc, tid):
    return [a.body for a in svc.get_ticket(tid).activity if a.kind == "comment"]


def test_done_appends_merge_confirmation_comment(tmp_path):
    # [SOLO-11] On done with a recorded PR, a confirmation comment naming the PR #,
    # URL, squash sha, base branch, and branch deletion is appended.
    gh = FakeGitHub()
    svc = _svc(tmp_path, github=gh)
    tid, _ = _to_ai_review(svc)  # PR #17 on branch solo-9-feature
    svc.move_ticket(tid, "in-human-review", actor="codex")
    svc.move_ticket(tid, "done", actor="human")

    comments = _comments(svc, tid)
    assert len(comments) == 1
    note = comments[0]
    assert "#17" in note
    assert "/pull/17" in note  # the PR URL
    assert "1a2b3c4d5e6f" in note  # the squash commit sha
    assert "main" in note  # the base branch
    assert "solo-9-feature" in note  # the deleted branch
    assert "deleted" in note.lower()


def test_merge_confirmation_comment_attributed_to_actor(tmp_path):
    # [SOLO-11] The note is attributed to the actor performing the Done (human).
    gh = FakeGitHub()
    svc = _svc(tmp_path, github=gh)
    tid, _ = _to_ai_review(svc)
    svc.move_ticket(tid, "in-human-review", actor="codex")
    svc.move_ticket(tid, "done", actor="human")

    notes = [a for a in svc.get_ticket(tid).activity if a.kind == "comment"]
    assert len(notes) == 1
    assert notes[0].actor == "human"


def test_done_comment_records_sha_resolved_by_branch(tmp_path):
    # [SOLO-11] The merge sha returned by merge_pr is used even when the PR was
    # resolved by branch rather than recorded up front.
    gh = FakeGitHub(merge_sha="cafef00dbabe")
    svc = _svc(tmp_path, github=gh)
    t = svc.create_ticket(project="SOLO", title="x")
    svc.move_ticket(t.id, "in-progress")
    svc.move_ticket(t.id, "in-ai-review", branch="b", actor="human")  # records branch, no PR
    svc.move_ticket(t.id, "in-human-review", actor="codex")
    svc.move_ticket(t.id, "done", actor="human")

    comments = _comments(svc, t.id)
    assert len(comments) == 1
    assert "cafef00dbabe" in comments[0]
    assert "#17" in comments[0]


def test_done_with_queued_merge_records_queued_not_merged(tmp_path):
    # [SOLO-11 c5] When gh only enqueues the PR (merge queue), record pr_state='queued'
    # with a "merge queue" note — NOT a false "Merged … Branch deleted" confirmation.
    gh = FakeGitHub(merge_state="queued")
    svc = _svc(tmp_path, github=gh)
    tid, _ = _to_ai_review(svc)  # PR #17 on branch solo-9-feature
    svc.move_ticket(tid, "in-human-review", actor="codex")
    done = svc.move_ticket(tid, "done", actor="human")

    assert done.pr_state == "queued"  # not "merged"
    comments = _comments(svc, tid)
    assert len(comments) == 1
    note = comments[0]
    assert "#17" in note
    assert "merge queue" in note.lower()
    assert "Merged PR" not in note  # not a false merge confirmation


def test_branchless_done_appends_no_comment(tmp_path):
    # [SOLO-11] A branch-less ticket reaching done has no PR, so no merge note.
    gh = FakeGitHub()
    svc = _svc(tmp_path, github=gh)
    t = svc.create_ticket(project="SOLO", title="y")
    svc.move_ticket(t.id, "in-progress")
    svc.move_ticket(t.id, "in-ai-review", actor="claude")  # no branch → no PR
    svc.move_ticket(t.id, "in-human-review", actor="codex")
    svc.move_ticket(t.id, "done", actor="human")
    assert _comments(svc, t.id) == []


def test_done_without_github_appends_no_comment(tmp_path):
    # [SOLO-11] No GitHub automation → no merge note even with a recorded branch.
    svc = _svc(tmp_path, github=None)
    t = svc.create_ticket(project="SOLO", title="x")
    svc.move_ticket(t.id, "in-progress")
    svc.move_ticket(t.id, "in-ai-review", branch="b", actor="claude")
    svc.move_ticket(t.id, "in-human-review", actor="codex")
    svc.move_ticket(t.id, "done", actor="human")
    assert _comments(svc, t.id) == []


def test_cancelled_appends_close_note(tmp_path):
    # [SOLO-11] Mirror for cancelled: a "closed PR #N" note attributed to the actor.
    gh = FakeGitHub()
    svc = _svc(tmp_path, github=gh)
    tid, _ = _to_ai_review(svc)  # PR #17 on branch solo-9-feature
    svc.move_ticket(tid, "cancelled", actor="claude")

    notes = [a for a in svc.get_ticket(tid).activity if a.kind == "comment"]
    assert len(notes) == 1
    assert "#17" in notes[0].body
    assert "solo-9-feature" in notes[0].body
    assert notes[0].actor == "claude"


def _merge_pr_fake(*, preflight, readback, merge_output="", record=None):
    """Build a `_run` stand-in for merge_pr's three calls: preflight view (`--json state`),
    the merge (whose stderr is `merge_output` — e.g. a "merge queue" notice), and the
    readback view (`--json state,mergeCommit`). `preflight`/`readback` are JSON strings, or
    a GitHubError instance to raise for that call."""

    def fake_run(args, cwd, check=True):
        if record is not None:
            record.append(args)
        is_merge = args[:3] == ["gh", "pr", "merge"]
        is_readback = "state,mergeCommit" in args  # the readback view's --json value
        if is_merge:
            payload, err = "", merge_output
        elif is_readback:
            payload, err = readback, ""
        else:
            payload, err = preflight, ""
        if isinstance(payload, GitHubError):
            raise payload

        class P:
            returncode = 0
            stderr = err
            stdout = payload or ""

        return P()

    return fake_run


def test_merge_pr_returns_sha_from_gh(tmp_path, monkeypatch):
    # [SOLO-11] A clean, open PR squash-merges; the adapter reports merged + the sha.
    from solopm.core.github import GitHub

    gh = GitHub()
    calls = []
    monkeypatch.setattr(gh, "_run", _merge_pr_fake(
        preflight='{"state": "OPEN"}',
        readback='{"state": "MERGED", "mergeCommit": {"oid": "deadbeefcafe"}}',
        record=calls,
    ))
    assert gh.merge_pr("/repo", 17) == MergeResult("merged", "deadbeefcafe")
    # The gating merge no longer bundles --delete-branch (branch cleanup is separate, best-effort).
    assert ["gh", "pr", "merge", "17", "--squash"] in calls
    assert not any("--delete-branch" in a for a in calls)


def test_merge_pr_sha_readback_failure_is_nonfatal(tmp_path, monkeypatch):
    # [SOLO-11] The merge already happened, so a failing readback (e.g. a timeout surfacing
    # as GitHubError despite check=False) must NOT abort — report merged, sha unknown.
    from solopm.core.github import GitHub

    gh = GitHub()
    monkeypatch.setattr(gh, "_run", _merge_pr_fake(
        preflight='{"state": "OPEN"}',
        readback=GitHubError("Command timed out: gh pr view 17"),
    ))
    assert gh.merge_pr("/repo", 17) == MergeResult("merged", None)  # does not raise


def test_merge_pr_merged_without_oid_is_sha_less(tmp_path, monkeypatch):
    # [SOLO-11] Eventual consistency: readback shows state=MERGED but the mergeCommit oid
    # hasn't propagated. That's a real merge — report merged with no sha (sha-less note).
    from solopm.core.github import GitHub

    gh = GitHub()
    monkeypatch.setattr(gh, "_run", _merge_pr_fake(
        preflight='{"state": "OPEN"}',
        readback='{"state": "MERGED", "mergeCommit": null}',
    ))
    assert gh.merge_pr("/repo", 17) == MergeResult("merged", None)


def test_merge_pr_open_after_merge_is_queued(tmp_path, monkeypatch):
    # [SOLO-11 c5] `gh pr merge` succeeded but the PR reads back still OPEN with no merge
    # commit → it was enqueued in a merge queue, not landed. Report queued, not merged.
    from solopm.core.github import GitHub

    gh = GitHub()
    monkeypatch.setattr(gh, "_run", _merge_pr_fake(
        preflight='{"state": "OPEN"}',
        readback='{"state": "OPEN", "mergeCommit": null}',
    ))
    assert gh.merge_pr("/repo", 17) == MergeResult("queued", None)


def test_merge_pr_queued_via_command_output_when_readback_flakes(tmp_path, monkeypatch):
    # [SOLO-11 c5] If the readback fails, the merge command's own "added to the merge queue"
    # notice must still classify the PR as queued — never silently downgraded to merged.
    from solopm.core.github import GitHub

    gh = GitHub()
    monkeypatch.setattr(gh, "_run", _merge_pr_fake(
        preflight='{"state": "OPEN"}',
        merge_output="! Pull request #17 will be added to the merge queue when ready",
        readback=GitHubError("Command timed out: gh pr view 17"),
    ))
    assert gh.merge_pr("/repo", 17) == MergeResult("queued", None)


def test_merge_pr_already_merged_is_idempotent(tmp_path, monkeypatch):
    # [SOLO-16 c2] An already-merged PR is a no-op success — NOT a GitHubError. `gh pr merge`
    # is never re-issued; the recorded squash sha (from the readback) is returned, so a retry
    # after a partial failure can still land the ticket in done.
    from solopm.core.github import GitHub

    gh = GitHub()
    calls = []
    monkeypatch.setattr(gh, "_run", _merge_pr_fake(
        preflight='{"state": "MERGED"}',
        readback='{"state": "MERGED", "mergeCommit": {"oid": "feedface"}}',
        record=calls,
    ))
    assert gh.merge_pr("/repo", 17) == MergeResult("merged", "feedface")
    assert not any(a[:3] == ["gh", "pr", "merge"] for a in calls)


def test_merge_pr_closed_unmerged_still_refused(tmp_path, monkeypatch):
    # [SOLO-16] A PR that is CLOSED without ever merging genuinely cannot be merged — that
    # still surfaces as a GitHubError (it is not the idempotent already-done case).
    from solopm.core.github import GitHub

    gh = GitHub()
    calls = []
    monkeypatch.setattr(gh, "_run", _merge_pr_fake(
        preflight='{"state": "CLOSED"}',
        readback='{"state": "CLOSED", "mergeCommit": null}',
        record=calls,
    ))
    with pytest.raises(GitHubError):
        gh.merge_pr("/repo", 17)
    assert not any(a[:3] == ["gh", "pr", "merge"] for a in calls)


def test_merge_pr_proceeds_when_preflight_unreadable(tmp_path, monkeypatch):
    # [SOLO-11] Preflight is best-effort: if the PR state can't be read, fall through and
    # let `gh pr merge` itself be the gate (don't block a legitimate merge on a flaky read).
    from solopm.core.github import GitHub

    gh = GitHub()
    calls = []
    monkeypatch.setattr(gh, "_run", _merge_pr_fake(
        preflight=GitHubError("Command timed out: gh pr view 17"),
        readback='{"state": "MERGED", "mergeCommit": {"oid": "abc123"}}',
        record=calls,
    ))
    assert gh.merge_pr("/repo", 17) == MergeResult("merged", "abc123")
    assert ["gh", "pr", "merge", "17", "--squash"] in calls
    assert not any("--delete-branch" in a for a in calls)


# --- SOLO-16: robust → done / → cancelled (branch cleanup best-effort, idempotent) ------


def _gh_with_branch_fake(
    *,
    pre_state="OPEN",
    readback='{"state": "MERGED", "mergeCommit": {"oid": "abc123"}}',
    worktree_branch=None,
    local_delete_rc=0,
    local_delete_stderr=None,
    remote_delete_rc=0,
    remote_delete_stderr=None,
    remote_delete_raises=False,
    local_delete_raises=False,
    record=None,
):
    """A `_run` stand-in covering merge/close PLUS the separate branch-cleanup git calls.

    `worktree_branch` (if set) is reported by `git worktree list` as checked out — so the
    real client should skip the local `git branch -D`. `local_delete_rc`/`remote_delete_rc`
    simulate cleanup non-zero exits; `*_delete_raises` simulate a `_run` that *raises*
    `GitHubError` (a timeout or missing command) — both must be tolerated without aborting.
    """
    from solopm.core.github import GitHub  # noqa: F401

    def fake_run(args, cwd, check=True):
        if record is not None:
            record.append(args)

        class P:
            returncode = 0
            stdout = ""
            stderr = ""

        p = P()
        if args[:3] == ["gh", "pr", "merge"] or args[:3] == ["gh", "pr", "close"]:
            return p
        if args[:3] == ["gh", "pr", "view"]:
            p.stdout = readback if "state,mergeCommit" in args else f'{{"state": "{pre_state}"}}'
            return p
        if args[:3] == ["git", "worktree", "list"]:
            p.stdout = f"worktree /wt\nbranch refs/heads/{worktree_branch}\n\n" if worktree_branch else ""
            return p
        if args[:2] == ["git", "push"]:  # remote delete
            if remote_delete_raises:
                raise GitHubError("Command timed out: git push origin --delete")
            p.returncode = remote_delete_rc
            if remote_delete_stderr is not None:
                p.stderr = remote_delete_stderr
            elif remote_delete_rc:
                p.stderr = "remote rejected"
            return p
        if args[:2] == ["git", "branch"]:  # local delete
            if local_delete_raises:
                raise GitHubError("Command timed out: git branch -D")
            p.returncode = local_delete_rc
            if local_delete_stderr is not None:
                p.stderr = local_delete_stderr
            elif local_delete_rc:
                p.stderr = "error: used by worktree"
            return p
        return p

    return fake_run


def test_merge_pr_branch_delete_failure_is_nonfatal(tmp_path, monkeypatch, caplog):
    # [SOLO-16 c1/c4] The squash-merge lands; a failing local branch delete must NOT abort —
    # report merged with branch_deleted=False, and warn (don't raise).
    import logging

    from solopm.core.github import GitHub

    gh = GitHub()
    calls = []
    monkeypatch.setattr(gh, "_run", _gh_with_branch_fake(local_delete_rc=1, record=calls))
    with caplog.at_level(logging.WARNING):
        result = gh.merge_pr("/repo", 17, branch="solo-16-x")
    assert result == MergeResult("merged", "abc123", branch_deleted=False)
    assert ["gh", "pr", "merge", "17", "--squash"] in calls
    assert not any("--delete-branch" in a for a in calls)
    assert any("solo-16-x" in rec.message for rec in caplog.records)  # warned


def test_merge_pr_skips_local_delete_for_worktree_branch(tmp_path, monkeypatch):
    # [SOLO-16 c1] A branch checked out in a worktree is left in place: no `git branch -D`
    # is attempted, the merge still succeeds, branch_deleted is False.
    from solopm.core.github import GitHub

    gh = GitHub()
    calls = []
    monkeypatch.setattr(
        gh, "_run", _gh_with_branch_fake(worktree_branch="solo-16-x", record=calls)
    )
    result = gh.merge_pr("/repo", 17, branch="solo-16-x")
    assert result == MergeResult("merged", "abc123", branch_deleted=False)
    assert not any(a[:2] == ["git", "branch"] for a in calls)  # local delete skipped


def test_merge_pr_deletes_branch_when_not_in_worktree(tmp_path, monkeypatch):
    # [SOLO-16] The happy path still cleans up: branch not checked out anywhere → deleted,
    # branch_deleted=True.
    from solopm.core.github import GitHub

    gh = GitHub()
    calls = []
    monkeypatch.setattr(gh, "_run", _gh_with_branch_fake(record=calls))
    result = gh.merge_pr("/repo", 17, branch="solo-16-x")
    assert result == MergeResult("merged", "abc123", branch_deleted=True)
    assert ["git", "branch", "-D", "solo-16-x"] in calls


def test_close_pr_idempotent_against_already_closed(tmp_path, monkeypatch):
    # [SOLO-16 c3] Closing an already-closed PR is a no-op success: `gh pr close` is never
    # issued and no error is raised.
    from solopm.core.github import GitHub

    gh = GitHub()
    calls = []
    monkeypatch.setattr(
        gh, "_run", _gh_with_branch_fake(pre_state="CLOSED", record=calls)
    )
    result = gh.close_pr("/repo", 17, branch="solo-16-x")
    assert isinstance(result, CloseResult)
    assert not any(a[:3] == ["gh", "pr", "close"] for a in calls)


def test_close_pr_closes_open_without_delete_branch_flag(tmp_path, monkeypatch):
    # [SOLO-16 c4] Closing an open PR issues `gh pr close` WITHOUT --delete-branch; cleanup
    # is the separate best-effort step.
    from solopm.core.github import GitHub

    gh = GitHub()
    calls = []
    monkeypatch.setattr(gh, "_run", _gh_with_branch_fake(pre_state="OPEN", record=calls))
    gh.close_pr("/repo", 17, branch="solo-16-x")
    assert ["gh", "pr", "close", "17"] in calls
    assert not any("--delete-branch" in a for a in calls)


def test_close_pr_branch_delete_failure_is_nonfatal(tmp_path, monkeypatch):
    # [SOLO-16 c3] A failing branch delete during close does not raise.
    from solopm.core.github import GitHub

    gh = GitHub()
    monkeypatch.setattr(
        gh, "_run", _gh_with_branch_fake(pre_state="OPEN", local_delete_rc=1)
    )
    result = gh.close_pr("/repo", 17, branch="solo-16-x")  # must not raise
    assert result.branch_deleted is False


def test_merge_pr_inconclusive_readback_does_not_delete_branch(tmp_path, monkeypatch):
    # [SOLO-16 gpt-review P1] `gh pr merge` exited 0 but the readback flaked (empty), and gh
    # didn't mention a queue. We still optimistically report merged (pre-existing behaviour),
    # but must NOT delete the branch — the PR may still be open (auto-merge pending) and
    # deleting its head branch would break it. Cleanup only runs on a confirmed MERGED.
    from solopm.core.github import GitHub

    gh = GitHub()
    calls = []
    monkeypatch.setattr(gh, "_run", _gh_with_branch_fake(readback="{}", record=calls))
    result = gh.merge_pr("/repo", 17, branch="solo-16-x")
    assert result.state == "merged"
    assert result.branch_deleted is False
    assert not any(a[:2] == ["git", "push"] for a in calls)  # no remote delete
    assert not any(a[:2] == ["git", "branch"] for a in calls)  # no local delete


def test_remote_branch_delete_is_fully_qualified(tmp_path, monkeypatch):
    # [SOLO-16 gpt-review P1] A bare `git push origin --delete <name>` resolves to a same-
    # named TAG when the branch is already gone, deleting an unrelated tag (data loss). The
    # delete must target refs/heads/<branch> so it can only ever remove the branch.
    from solopm.core.github import GitHub

    gh = GitHub()
    calls = []
    monkeypatch.setattr(gh, "_run", _gh_with_branch_fake(record=calls))
    gh.merge_pr("/repo", 17, branch="solo-16-x")
    push_deletes = [a for a in calls if a[:2] == ["git", "push"]]
    assert push_deletes, "expected a remote branch delete"
    for a in push_deletes:
        assert a[-1] == "refs/heads/solo-16-x"  # qualified ref, never the bare tag-ambiguous name


def test_merge_pr_remote_delete_failure_reports_not_deleted(tmp_path, monkeypatch):
    # [SOLO-16 gpt-review P2] If the remote delete fails (network/permission) but the local
    # delete succeeds, the branch still exists on GitHub — branch_deleted must be False so
    # the note does not falsely claim the branch was deleted.
    from solopm.core.github import GitHub

    gh = GitHub()
    monkeypatch.setattr(
        gh, "_run", _gh_with_branch_fake(remote_delete_rc=1, local_delete_rc=0)
    )
    result = gh.merge_pr("/repo", 17, branch="solo-16-x")
    assert result.state == "merged"
    assert result.branch_deleted is False


def test_local_branch_already_gone_counts_as_deleted(tmp_path, monkeypatch):
    # [SOLO-16 gpt-review P3] A `git branch -D` on an already-absent local branch (a cleanup
    # retry, or it was deleted manually) exits non-zero with "branch not found". That is a
    # clean state, so report the branch deleted rather than falsely "retained".
    from solopm.core.github import GitHub

    gh = GitHub()
    monkeypatch.setattr(gh, "_run", _gh_with_branch_fake(
        local_delete_rc=1,
        local_delete_stderr="error: branch 'solo-16-x' not found.",
    ))
    result = gh.merge_pr("/repo", 17, branch="solo-16-x")
    assert result.branch_deleted is True


def test_pr_head_parses_head_ref_name(tmp_path, monkeypatch):
    # [SOLO-16 gpt-review P2] pr_head resolves the PR's real head branch; an unreadable
    # response yields None (caller then skips cleanup rather than guessing).
    from solopm.core.github import GitHub

    gh = GitHub()
    monkeypatch.setattr(gh, "_run", lambda *a, **k: type(
        "P", (), {"returncode": 0, "stdout": '{"headRefName": "solo-16-real-head"}', "stderr": ""})())
    assert gh.pr_head("/repo", 17) == "solo-16-real-head"
    monkeypatch.setattr(gh, "_run", lambda *a, **k: type(
        "P", (), {"returncode": 1, "stdout": "", "stderr": "boom"})())
    assert gh.pr_head("/repo", 17) is None


def test_cleanup_uses_github_head_not_stored_branch(tmp_path):
    # [SOLO-16 gpt-review P2] Cleanup targets the PR head resolved fresh from GitHub, never the
    # stored ticket.branch (which could be stale on a row predating branch pinning). Simulate
    # the stored branch diverging from GitHub's real head; the real head is what's cleaned up.
    gh = FakeGitHub()
    svc = _svc(tmp_path, github=gh)
    tid, _ = _to_ai_review(svc, branch="solo-16-stored")  # ticket.branch = solo-16-stored
    svc.move_ticket(tid, "in-human-review", actor="codex")
    gh.head_branch = "solo-16-true-head"  # GitHub's real PR head differs from the stored value
    svc.move_ticket(tid, "done", actor="human")
    assert ("head", 17) in gh.calls  # head resolved from GitHub
    assert gh.merge_branch == "solo-16-true-head"  # cleaned up the real head, not the stored branch
    assert "solo-16-true-head" in _comments(svc, tid)[0]  # note names the real head


def test_cleanup_skipped_when_head_unconfirmed(tmp_path):
    # [SOLO-16 gpt-review P2] If the PR head can't be confirmed, cleanup is skipped (the client
    # is handed no branch) rather than risk deleting an unrelated branch.
    gh = FakeGitHub()
    svc = _svc(tmp_path, github=gh)
    tid, _ = _to_ai_review(svc, branch="solo-16-real")
    svc.move_ticket(tid, "in-human-review", actor="codex")
    gh.head_branch = None  # pr_head returns None → unconfirmed
    done = svc.move_ticket(tid, "done", actor="human")
    assert done.pr_state == "merged"  # the merge still gates the transition
    assert gh.merge_branch is None  # but no branch handed to cleanup


def test_merge_pr_remote_already_gone_counts_as_deleted(tmp_path, monkeypatch):
    # [SOLO-16 gpt-review P2] When the remote branch is already absent (e.g. repo-level
    # auto-delete-on-merge removed it), `git push --delete` exits non-zero with "remote ref
    # does not exist". That is not a failure — the branch IS gone, so report deleted.
    from solopm.core.github import GitHub

    gh = GitHub()
    monkeypatch.setattr(gh, "_run", _gh_with_branch_fake(
        remote_delete_rc=1,
        remote_delete_stderr="error: unable to delete 'solo-16-x': remote ref does not exist",
        local_delete_rc=0,
    ))
    result = gh.merge_pr("/repo", 17, branch="solo-16-x")
    assert result.branch_deleted is True


def test_queued_note_does_not_promise_branch_deletion(tmp_path):
    # [SOLO-16 gpt-review P2] The gating merge no longer carries --delete-branch, so SoloPM
    # cannot schedule deletion for a queued merge. The queued note must not promise it will
    # "delete branch X" — that would be a promise nothing keeps.
    gh = FakeGitHub(merge_state="queued")
    svc = _svc(tmp_path, github=gh)
    tid, _ = _to_ai_review(svc)
    svc.move_ticket(tid, "in-human-review", actor="codex")
    svc.move_ticket(tid, "done", actor="human")
    note = _comments(svc, tid)[0]
    assert "merge queue" in note.lower()
    assert "delete branch" not in note.lower()


def test_merge_pr_cleanup_timeout_does_not_abort(tmp_path, monkeypatch):
    # [SOLO-16 gpt-review P2] `check=False` stops non-zero exits from raising, but `_run`
    # still raises GitHubError on a *timeout* / missing command. A cleanup git call that
    # times out AFTER the merge already landed must NOT propagate and abort the transition.
    from solopm.core.github import GitHub

    gh = GitHub()
    monkeypatch.setattr(
        gh, "_run", _gh_with_branch_fake(remote_delete_raises=True, local_delete_raises=True)
    )
    result = gh.merge_pr("/repo", 17, branch="solo-16-x")  # must not raise
    assert result.state == "merged"
    assert result.branch_deleted is False  # cleanup couldn't complete, but merge stands


def test_close_pr_cleanup_timeout_does_not_abort(tmp_path, monkeypatch):
    # [SOLO-16 gpt-review P2] Same guarantee for the close path's branch cleanup.
    from solopm.core.github import GitHub

    gh = GitHub()
    monkeypatch.setattr(
        gh, "_run", _gh_with_branch_fake(pre_state="OPEN", remote_delete_raises=True)
    )
    result = gh.close_pr("/repo", 17, branch="solo-16-x")  # must not raise
    assert isinstance(result, CloseResult)


def test_close_pr_does_not_treat_merged_as_closed(tmp_path, monkeypatch):
    # [SOLO-16 gpt-review P2] A PR already MERGED on GitHub must NOT be silently recorded as
    # a successful cancellation. Only CLOSED is the idempotent no-op; a merged PR surfaces
    # (gh pr close errors) rather than misreporting landed work as abandoned.
    from solopm.core.github import GitHub

    gh = GitHub()
    calls = []

    def fake_run(args, cwd, check=True):
        calls.append(args)

        class P:
            returncode = 0
            stdout = ""
            stderr = ""

        p = P()
        if args[:3] == ["gh", "pr", "view"]:
            p.stdout = '{"state": "MERGED"}'
            return p
        if args[:3] == ["gh", "pr", "close"]:
            raise GitHubError("Pull request #17 is already merged")
        return p

    monkeypatch.setattr(gh, "_run", fake_run)
    with pytest.raises(GitHubError):
        gh.close_pr("/repo", 17, branch="solo-16-x")
    assert any(a[:3] == ["gh", "pr", "close"] for a in calls)  # close was attempted, not skipped


def test_done_reaches_done_when_branch_delete_fails(tmp_path):
    # [SOLO-16 c1] End-to-end through the service: a merge whose branch cleanup failed still
    # lands the ticket in done with pr_state=merged — the transition is NOT aborted.
    gh = FakeGitHub(branch_deleted=False)
    svc = _svc(tmp_path, github=gh)
    tid, _ = _to_ai_review(svc)
    svc.move_ticket(tid, "in-human-review", actor="codex")
    done = svc.move_ticket(tid, "done", actor="human")
    assert done.state == "done"
    assert done.pr_state == "merged"
    # The merge note is honest about the retained branch rather than claiming deletion.
    note = _comments(svc, tid)[0]
    assert "retained" in note.lower()
    assert "deleted" not in note.lower()


def test_branch_pinned_to_pr_head_after_pr_opened(tmp_path):
    # [SOLO-16 gpt-review P1] The recorded branch is the PR head used for merge/close cleanup,
    # so once a PR exists it is pinned. A differing branch on any later move is rejected —
    # both the reviewer's intermediate-move exploit and a terminal-move override.
    gh = FakeGitHub()
    svc = _svc(tmp_path, github=gh)
    tid, _ = _to_ai_review(svc, branch="solo-16-real")  # PR #17, head solo-16-real
    # Exploit: rewrite the pinned branch on an intermediate move.
    with pytest.raises(ValidationError):
        svc.move_ticket(tid, "in-human-review", actor="codex", branch="main")
    assert svc.get_ticket(tid).branch == "solo-16-real"  # unchanged
    # And a bogus override on the terminal move itself is rejected before any merge.
    svc.move_ticket(tid, "in-human-review", actor="codex")
    with pytest.raises(ValidationError):
        svc.move_ticket(tid, "done", actor="human", branch="solo-16-bogus")
    assert svc.get_ticket(tid).state == "in-human-review"  # not moved
    assert gh.merge_branch is None  # no merge attempted with a bogus branch


def test_branch_settable_before_pr_exists(tmp_path):
    # [SOLO-16] Before a PR is opened the branch is still free to set/change (e.g. an agent
    # recording its worktree branch on → in-progress) — only the post-PR head is pinned.
    svc = _svc(tmp_path, github=None)  # no PR automation → pr_number stays None
    t = svc.create_ticket(project="SOLO", title="x")
    a = svc.move_ticket(t.id, "in-progress", branch="solo-16-a", actor="claude")
    assert a.branch == "solo-16-a"
    # Still no PR recorded, so a different branch on a later move is allowed.
    b = svc.move_ticket(t.id, "backlog", branch="solo-16-b", actor="claude")
    assert b.branch == "solo-16-b"


def test_done_cleanup_targets_recorded_pr_head(tmp_path):
    # [SOLO-16 gpt-review P1] With the branch pinned to the PR head, done cleanup targets it.
    gh = FakeGitHub()
    svc = _svc(tmp_path, github=gh)
    tid, _ = _to_ai_review(svc, branch="solo-16-real")
    svc.move_ticket(tid, "in-human-review", actor="codex")
    svc.move_ticket(tid, "done", actor="human")
    assert ("merge", 17) in gh.calls
    assert gh.merge_branch == "solo-16-real"


def test_cancelled_cleanup_targets_recorded_pr_head(tmp_path):
    # [SOLO-16 gpt-review P1] Same for the close path.
    gh = FakeGitHub()
    svc = _svc(tmp_path, github=gh)
    tid, _ = _to_ai_review(svc, branch="solo-16-real")
    svc.move_ticket(tid, "cancelled", actor="claude")
    assert ("close", 17) in gh.calls
    assert gh.close_branch == "solo-16-real"


def test_branch_override_allowed_when_unchanged(tmp_path):
    # [SOLO-16] Passing the *same* branch on a later move is a harmless no-op, not rejected.
    gh = FakeGitHub()
    svc = _svc(tmp_path, github=gh)
    tid, _ = _to_ai_review(svc, branch="solo-16-real")
    moved = svc.move_ticket(tid, "in-human-review", actor="codex", branch="solo-16-real")
    assert moved.state == "in-human-review"
    assert moved.branch == "solo-16-real"


def test_cancelled_reaches_cancelled_when_branch_delete_fails(tmp_path):
    # [SOLO-16 c3] Mirror for cancelled: a close whose branch cleanup failed still lands the
    # ticket in cancelled.
    gh = FakeGitHub(branch_deleted=False)
    svc = _svc(tmp_path, github=gh)
    tid, _ = _to_ai_review(svc)
    cancelled = svc.move_ticket(tid, "cancelled", actor="claude")
    assert cancelled.state == "cancelled"
    assert cancelled.pr_state == "closed"
    assert "retained" in _comments(svc, tid)[0].lower()
