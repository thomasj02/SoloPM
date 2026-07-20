"""GitHub PR side effects on transition (SOLO-2), tested with a fake client."""

import pytest

from solopm.core.errors import NotFoundError, ValidationError
from solopm.core.github import CloseResult, OpenPR, PR, GitHubError, MergeResult
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
        open_prs: list | None = None,
        has_pr_for_branch: bool = True,
        remote_urls: dict | None = None,
        branch_on_origin: bool = True,
    ):
        self.calls: list[tuple] = []
        self.pr_number = pr_number
        self.fail_on = fail_on
        self.merge_sha = merge_sha
        self.merge_state = merge_state  # "merged" or "queued"
        self.branch_deleted = branch_deleted
        self.open_prs = list(open_prs or [])  # what list_open_prs reports (SOLO-27)
        self.has_pr_for_branch = has_pr_for_branch  # find_pr returns None when False
        self.remote_urls = dict(remote_urls or {})  # repo path -> origin (fetch) URL
        self.remote_push_urls: dict = {}  # repo path -> origin push URL (when split)
        self.branch_on_origin = branch_on_origin  # what api_branch_exists reports (SOLO-29)
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
        if not self.has_pr_for_branch:
            return None
        self.head_branch = branch  # the PR matched on this branch → it is the head
        return PR(number=self.pr_number, url=f"https://github.com/thomasj02/SoloPM/pull/{self.pr_number}", state="open")

    def list_open_prs(self, repo):
        self._maybe_fail("list_open_prs")
        self.calls.append(("list_open", repo))
        return list(self.open_prs)

    def remote_url(self, repo):
        self._maybe_fail("remote_url")
        return (self.remote_urls or {}).get(repo)

    def remote_push_url(self, repo):
        self._maybe_fail("remote_push_url")
        # Like git: the push URL defaults to the fetch URL unless split explicitly.
        return self.remote_push_urls.get(repo) or self.remote_urls.get(repo)

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

    # --- API-mode surface (SOLO-29): remote projects, addressed by slug -------

    def api_check_repo(self, slug):
        self._maybe_fail("api_check_repo")
        self.calls.append(("api_check_repo", slug))

    def api_branch_exists(self, slug, branch):
        self._maybe_fail("api_branch_exists")
        self.calls.append(("api_branch_exists", slug, branch))
        return self.branch_on_origin

    def api_find_pr(self, slug, branch):
        self._maybe_fail("api_find_pr")
        self.calls.append(("api_find", slug, branch))
        if not self.has_pr_for_branch:
            return None
        self.head_branch = branch
        return PR(number=self.pr_number, url=f"https://github.com/acme/widget/pull/{self.pr_number}", state="open")

    def api_list_open_prs(self, slug):
        self._maybe_fail("api_list_open_prs")
        self.calls.append(("api_list_open", slug))
        return list(self.open_prs)

    def api_open_or_refresh_pr(self, slug, branch, base, title, body):
        self._maybe_fail("api_open_or_refresh_pr")
        self.calls.append(("api_pr", slug, branch, base, title))
        self.head_branch = branch
        return PR(number=self.pr_number, url=f"https://github.com/acme/widget/pull/{self.pr_number}", state="open")

    def api_pr_head(self, slug, number):
        self._maybe_fail("api_pr_head")
        self.calls.append(("api_head", slug, number))
        return self.head_branch

    def api_merge_pr(self, slug, number, branch=None):
        self._maybe_fail("api_merge_pr")
        self.calls.append(("api_merge", slug, number))
        self.merge_branch = branch
        if self.merge_state == "queued":
            return MergeResult("queued")
        # Mirror the real semantics: with no branch there is nothing to clean up,
        # so branch_deleted can never be True.
        deleted = self.branch_deleted and branch is not None
        return MergeResult("merged", self.merge_sha, branch_deleted=deleted)

    def api_close_pr(self, slug, number, branch=None):
        self._maybe_fail("api_close_pr")
        self.calls.append(("api_close", slug, number))
        self.close_branch = branch
        return CloseResult(branch_deleted=self.branch_deleted and branch is not None)


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
    # URL, squash sha, base branch, and the (retained) branch is appended.
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
    assert "solo-9-feature" in note  # the branch, retained for its worktree
    # [SOLO-18] The local branch is left in place for its worktree, never claimed deleted.
    assert "left in place" in note.lower()
    assert "deleted" not in note.lower()


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


def test_branchless_done_notes_nothing_merged(tmp_path):
    # [SOLO-11 → superseded by SOLO-27 c4] A branch-less done used to stay silent; that
    # silence masked skipped merges (AW-62), so it now leaves an explicit note instead.
    gh = FakeGitHub()  # no open PRs → discovery finds nothing
    svc = _svc(tmp_path, github=gh)
    t = svc.create_ticket(project="SOLO", title="y")
    svc.move_ticket(t.id, "in-progress")
    svc.move_ticket(t.id, "in-ai-review", actor="claude")  # no branch → no PR
    svc.move_ticket(t.id, "in-human-review", actor="codex")
    done = svc.move_ticket(t.id, "done", actor="human")
    assert done.state == "done"  # the move itself is never blocked
    notes = _comments(svc, t.id)
    assert len(notes) == 1 and "no PR was merged" in notes[0]


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


def test_merge_pr_remote_delete_failure_is_nonfatal(tmp_path, monkeypatch, caplog):
    # [SOLO-16 c1/c4 + SOLO-18] The squash-merge lands; a failing *remote* branch delete must
    # NOT abort — report merged with branch_deleted=False, and warn (don't raise). The local
    # branch is never touched on → done (it's held by the worktree).
    import logging

    from solopm.core.github import GitHub

    gh = GitHub()
    calls = []
    monkeypatch.setattr(gh, "_run", _gh_with_branch_fake(remote_delete_rc=1, record=calls))
    with caplog.at_level(logging.WARNING):
        result = gh.merge_pr("/repo", 17, branch="solo-16-x")
    assert result == MergeResult("merged", "abc123", branch_deleted=False)
    assert ["gh", "pr", "merge", "17", "--squash"] in calls
    assert not any("--delete-branch" in a for a in calls)
    assert not any(a[:2] == ["git", "branch"] for a in calls)  # local branch never deleted
    assert any("solo-16-x" in rec.message for rec in caplog.records)  # warned on remote failure


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


def test_merge_pr_never_deletes_local_branch_on_done(tmp_path, monkeypatch):
    # [SOLO-18] The → done merge must NEVER delete the local branch: it's checked out in the
    # developer's worktree, which SoloPM leaves in place. Even with NO worktree reported (the
    # detection can be unreliable), no `git branch -D` is attempted and branch_deleted is
    # False. The remote branch is still cleaned up — it never conflicts with the worktree.
    from solopm.core.github import GitHub

    gh = GitHub()
    calls = []
    monkeypatch.setattr(gh, "_run", _gh_with_branch_fake(record=calls))  # no worktree reported
    result = gh.merge_pr("/repo", 17, branch="solo-18-x")
    assert result == MergeResult("merged", "abc123", branch_deleted=False)
    assert not any(a[:2] == ["git", "branch"] for a in calls)  # local delete never attempted
    assert any(a[:2] == ["git", "push"] for a in calls)  # remote cleanup still happens


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
    # clean state, so report the branch deleted rather than falsely "retained". Exercised via
    # close_pr (→ cancelled), the path that still deletes the local branch — → done never does
    # (SOLO-18).
    from solopm.core.github import GitHub

    gh = GitHub()
    monkeypatch.setattr(gh, "_run", _gh_with_branch_fake(
        pre_state="OPEN",
        local_delete_rc=1,
        local_delete_stderr="error: branch 'solo-16-x' not found.",
    ))
    result = gh.close_pr("/repo", 17, branch="solo-16-x")
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


def test_remote_branch_already_gone_counts_as_deleted(tmp_path, monkeypatch):
    # [SOLO-16 gpt-review P2] When the remote branch is already absent (e.g. repo-level
    # auto-delete-on-merge removed it), `git push --delete` exits non-zero with "remote ref
    # does not exist". That is not a failure — the branch IS gone, so report deleted. Exercised
    # via close_pr (→ cancelled): → done keeps the local branch and never reports it deleted
    # (SOLO-18), so the "fully deleted" outcome only arises on the cancel path.
    from solopm.core.github import GitHub

    gh = GitHub()
    monkeypatch.setattr(gh, "_run", _gh_with_branch_fake(
        pre_state="OPEN",
        remote_delete_rc=1,
        remote_delete_stderr="error: unable to delete 'solo-16-x': remote ref does not exist",
        local_delete_rc=0,
    ))
    result = gh.close_pr("/repo", 17, branch="solo-16-x")
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
    # [SOLO-18] The merge note is honest about the retained branch rather than claiming deletion.
    note = _comments(svc, tid)[0]
    assert "left in place" in note.lower()
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


# --- SOLO-27: discover unrecorded PRs on done/cancelled + never skip silently ---


def _pr(number, head, base="main", cross_repo=False):
    return OpenPR(
        number=number,
        url=f"https://github.com/x/y/pull/{number}",
        head=head,
        base=base,
        cross_repo=cross_repo,
    )


def _to_human_review_no_branch(svc):
    """The AW-62 shape: the agent reviews and hands off without ever recording a branch
    (it opened the PR out-of-band with `gh`), so the ticket has branch/pr = null."""
    t = svc.create_ticket(project="SOLO", title="x")
    svc.move_ticket(t.id, "in-progress")
    svc.move_ticket(t.id, "in-ai-review", actor="claude")  # no branch passed
    svc.move_ticket(t.id, "in-human-review", actor="codex")
    return t.id


def test_done_discovers_unrecorded_pr_by_branch_convention(tmp_path):
    # [SOLO-27 c1/c5] The AW-62 regression: no recorded branch/PR, but an open PR whose
    # head follows the ticket's branch convention exists → adopt and squash-merge it.
    gh = FakeGitHub(open_prs=[_pr(46, "SOLO-1-anthropic-adaptive"), _pr(9, "OTHER-1-x")])
    svc = _svc(tmp_path, github=gh)
    tid = _to_human_review_no_branch(svc)
    assert svc.get_ticket(tid).branch is None  # precondition: nothing recorded

    done = svc.move_ticket(tid, "done", actor="human")
    assert ("merge", 46) in gh.calls
    assert done.state == "done"
    assert done.pr_number == 46
    assert done.pr_state == "merged"
    assert done.branch == "SOLO-1-anthropic-adaptive"  # adopted for prune/history
    assert any("Merged PR #46" in c for c in _comments(svc, tid))


def test_done_discovery_matches_bare_ticket_id_head(tmp_path):
    # [SOLO-27 c1] A head that is exactly the ticket id (no slug) also matches.
    gh = FakeGitHub(open_prs=[_pr(7, "SOLO-1")])
    svc = _svc(tmp_path, github=gh)
    tid = _to_human_review_no_branch(svc)
    done = svc.move_ticket(tid, "done", actor="human")
    assert ("merge", 7) in gh.calls
    assert done.pr_state == "merged"


def test_done_discovery_prefix_cannot_cross_ticket_ids(tmp_path):
    # [SOLO-27 c2] SOLO-1 must not adopt SOLO-10's or SOLO-19's PR.
    gh = FakeGitHub(open_prs=[_pr(10, "SOLO-10-foo"), _pr(19, "SOLO-19")])
    svc = _svc(tmp_path, github=gh)
    tid = _to_human_review_no_branch(svc)
    done = svc.move_ticket(tid, "done", actor="human")
    assert not any(c[0] == "merge" for c in gh.calls)
    assert done.state == "done"
    assert done.pr_number is None
    notes = _comments(svc, tid)
    assert len(notes) == 1 and "no PR was merged" in notes[0]


def test_done_discovery_ambiguous_merges_nothing(tmp_path):
    # [SOLO-27 c2] Two convention-matching open PRs: guessing could merge the wrong one.
    gh = FakeGitHub(open_prs=[_pr(1, "SOLO-1-first"), _pr(2, "SOLO-1-second")])
    svc = _svc(tmp_path, github=gh)
    tid = _to_human_review_no_branch(svc)
    done = svc.move_ticket(tid, "done", actor="human")
    assert not any(c[0] == "merge" for c in gh.calls)
    assert done.state == "done"
    note = _comments(svc, tid)[0]
    assert "no PR was merged" in note and "#1" in note and "#2" in note


def test_done_discovery_gh_failure_does_not_block_the_move(tmp_path):
    # [SOLO-27 c3] Failing to LOOK for a PR must not block the human's done-move.
    gh = FakeGitHub(open_prs=[_pr(46, "SOLO-1-x")], fail_on="list_open_prs")
    svc = _svc(tmp_path, github=gh)
    tid = _to_human_review_no_branch(svc)
    done = svc.move_ticket(tid, "done", actor="human")
    assert done.state == "done"
    assert not any(c[0] == "merge" for c in gh.calls)
    note = _comments(svc, tid)[0]
    assert "no PR was merged" in note and "discovery failed" in note


def test_done_merge_failure_after_discovery_aborts_but_keeps_the_adoption(tmp_path):
    # [SOLO-27 c3 + gpt-review r4 P1] Once a PR is resolved, action failures keep the
    # abort semantics — but the adoption must already be persisted: a merge that lands
    # remotely and then times out client-side leaves a PR that is no longer OPEN, so a
    # retry could never rediscover it. With the identity recorded, the retry goes
    # through the recorded-PR path exactly like a ticket that was tracked all along.
    gh = FakeGitHub(open_prs=[_pr(46, "SOLO-1-x")], fail_on="merge_pr")
    svc = _svc(tmp_path, github=gh)
    tid = _to_human_review_no_branch(svc)
    with pytest.raises(GitHubError):
        svc.move_ticket(tid, "done", actor="human")
    after = svc.get_ticket(tid)
    assert after.state == "in-human-review"  # the move itself aborted
    assert after.pr_number == 46  # ...but the discovered identity is not lost
    assert after.branch == "SOLO-1-x"
    assert after.pr_state == "open"

    # Retry: the PR is recorded now — merged via the recorded path, no re-discovery.
    gh.fail_on = None
    listing_calls = sum(1 for c in gh.calls if c[0] == "list_open")
    done = svc.move_ticket(tid, "done", actor="human")
    assert done.state == "done" and done.pr_state == "merged"
    assert sum(1 for c in gh.calls if c[0] == "list_open") == listing_calls



def test_cancelled_discovers_and_closes_unrecorded_pr(tmp_path):
    # [SOLO-27 c1] Cancelled gets the same recovery, closing instead of merging.
    gh = FakeGitHub(open_prs=[_pr(46, "SOLO-1-x")])
    svc = _svc(tmp_path, github=gh)
    t = svc.create_ticket(project="SOLO", title="x")
    svc.move_ticket(t.id, "in-progress")
    svc.move_ticket(t.id, "in-ai-review", actor="claude")  # no branch
    cancelled = svc.move_ticket(t.id, "cancelled", actor="claude")
    assert ("close", 46) in gh.calls
    assert cancelled.pr_state == "closed"
    assert cancelled.branch == "SOLO-1-x"
    assert any("Closed PR #46" in c for c in _comments(svc, t.id))


def test_recorded_branch_without_pr_notes_instead_of_silence(tmp_path):
    # [SOLO-27 c4] A branch recorded (e.g. on → in-progress for the radar) that has no PR:
    # the cancel notes it rather than silently closing nothing.
    gh = FakeGitHub(has_pr_for_branch=False)
    svc = _svc(tmp_path, github=gh)
    t = svc.create_ticket(project="SOLO", title="x")
    svc.move_ticket(t.id, "in-progress", branch="SOLO-1-wip", actor="claude")
    cancelled = svc.move_ticket(t.id, "cancelled", actor="claude")
    assert cancelled.state == "cancelled"
    note = _comments(svc, t.id)[0]
    assert "no PR was closed" in note and "SOLO-1-wip" in note


def test_no_repo_and_no_automation_stay_silent_on_done(tmp_path):
    # [SOLO-27 c4] No expectation of a PR → no note noise.
    gh = FakeGitHub(open_prs=[])
    svc_no_repo = _svc(tmp_path, github=gh, repo=None)
    tid = _to_human_review_no_branch(svc_no_repo)
    svc_no_repo.move_ticket(tid, "done", actor="human")
    assert _comments(svc_no_repo, tid) == []

    store = Store(tmp_path / "nogh.db")
    store.init()
    svc_no_gh = Service(store, github=None)
    svc_no_gh.add_project(key="SOLO", name="SoloPM", repo="/tmp/repo", master="main")
    tid = _to_human_review_no_branch(svc_no_gh)
    svc_no_gh.move_ticket(tid, "done", actor="human")
    assert _comments(svc_no_gh, tid) == []


def test_recorded_pr_skips_discovery(tmp_path):
    # A ticket with a recorded PR must never hit the list-open-PRs discovery path.
    gh = FakeGitHub()
    svc = _svc(tmp_path, github=gh)
    tid, _ = _to_ai_review(svc)
    svc.move_ticket(tid, "in-human-review", actor="codex")
    svc.move_ticket(tid, "done", actor="human")
    assert not any(c[0] == "list_open" for c in gh.calls)


def test_real_client_list_open_prs_parses_gh_output(monkeypatch):
    # The real GitHub.list_open_prs: exact gh invocation (state filter, field list,
    # limit — a wrong flag silently changes discovery semantics) + JSON parsing.
    from solopm.core.github import GitHub

    captured = {}

    class Proc:
        returncode = 0
        stdout = (
            '[{"number": 46, "url": "https://github.com/x/y/pull/46",'
            ' "headRefName": "AW-62-adaptive", "baseRefName": "main",'
            ' "isCrossRepository": false},'
            ' {"number": 47, "url": "https://github.com/x/y/pull/47",'
            ' "headRefName": "AW-63-doc", "baseRefName": "release-1.x",'
            ' "isCrossRepository": true}]'
        )
        stderr = ""

    gh = GitHub()

    def fake_run(args, cwd, *, check=True):
        captured["args"], captured["cwd"] = args, cwd
        return Proc()

    monkeypatch.setattr(gh, "_run", fake_run)
    prs = gh.list_open_prs("/some/repo")
    assert captured["cwd"] == "/some/repo"
    assert captured["args"] == [
        "gh", "pr", "list", "--state", "open",
        "--json", "number,url,headRefName,baseRefName,isCrossRepository",
        "--limit", "1000",
    ]
    assert [(p.number, p.head, p.base, p.cross_repo) for p in prs] == [
        (46, "AW-62-adaptive", "main", False),
        (47, "AW-63-doc", "release-1.x", True),
    ]
    assert prs[0].url.endswith("/pull/46")


def test_real_client_list_open_prs_wraps_parse_failures(monkeypatch):
    # [SOLO-27 c3] gh exiting 0 with non-JSON stdout (wrapper banner, changed fields)
    # must surface as GitHubError so the caller's best-effort contract holds.
    from solopm.core.github import GitHub

    gh = GitHub()

    def fake_run_garbage(args, cwd, *, check=True):
        class Proc:
            returncode = 0
            stdout = "A new gh release is available!\n[]"
            stderr = ""
        return Proc()

    monkeypatch.setattr(gh, "_run", fake_run_garbage)
    with pytest.raises(GitHubError):
        gh.list_open_prs("/some/repo")

    def fake_run_missing_field(args, cwd, *, check=True):
        class Proc:
            returncode = 0
            stdout = '[{"number": 1, "url": "u"}]'  # headRefName missing
            stderr = ""
        return Proc()

    monkeypatch.setattr(gh, "_run", fake_run_missing_field)
    with pytest.raises(GitHubError):
        gh.list_open_prs("/some/repo")


def test_real_client_list_open_prs_refuses_truncated_listings(monkeypatch):
    # [SOLO-27 c2] A listing that fills the limit may be truncated — matching against
    # it could bypass the ambiguity guard, so it must refuse rather than guess.
    import json as _json

    from solopm.core.github import GitHub

    rows = [
        {
            "number": i,
            "url": f"https://github.com/x/y/pull/{i}",
            "headRefName": f"b-{i}",
            "baseRefName": "main",
            "isCrossRepository": False,
        }
        for i in range(1000)
    ]

    class Proc:
        returncode = 0
        stdout = _json.dumps(rows)
        stderr = ""

    gh = GitHub()
    monkeypatch.setattr(gh, "_run", lambda args, cwd, *, check=True: Proc())
    with pytest.raises(GitHubError):
        gh.list_open_prs("/some/repo")


def test_cancel_from_planning_states_never_touches_github(tmp_path):
    # [SOLO-27 review P2] Cancelling a backlog/todo ticket (routine triage) must not
    # probe GitHub, must not adopt/close a coincidentally matching PR, and must not
    # stamp a note — pre-SOLO-27 silence was correct there: no work was ever recorded.
    for planning_state in ("backlog", "todo"):
        gh = FakeGitHub(open_prs=[_pr(46, "SOLO-1-spike")])
        svc = _svc(tmp_path / planning_state, github=gh)
        t = svc.create_ticket(project="SOLO", title="idea")
        if planning_state == "todo":
            svc.move_ticket(t.id, "todo")
        cancelled = svc.move_ticket(t.id, "cancelled", actor="human")
        assert cancelled.state == "cancelled"
        assert gh.calls == []  # no list/close — the spike PR survives
        assert _comments(svc, t.id) == []


def test_single_crossing_pr_is_not_adopted(tmp_path):
    # [SOLO-27 review] With ONLY SOLO-10's PR open, SOLO-1's done must not adopt it —
    # a plain startswith(ticket.id) match would (and the ambiguity guard can't save it).
    gh = FakeGitHub(open_prs=[_pr(10, "SOLO-10-foo")])
    svc = _svc(tmp_path, github=gh)
    tid = _to_human_review_no_branch(svc)
    done = svc.move_ticket(tid, "done", actor="human")
    assert not any(c[0] == "merge" for c in gh.calls)
    assert done.pr_number is None
    assert "no PR was merged" in _comments(svc, tid)[0]


def test_queued_merge_on_discovery_path_persists_adoption(tmp_path):
    # [SOLO-27 review] A discovered PR that lands in the merge queue must still be
    # adopted onto the ticket (number/url/branch) alongside pr_state=queued.
    gh = FakeGitHub(open_prs=[_pr(46, "SOLO-1-x")], merge_state="queued")
    svc = _svc(tmp_path, github=gh)
    tid = _to_human_review_no_branch(svc)
    done = svc.move_ticket(tid, "done", actor="human")
    assert done.pr_state == "queued"
    assert done.pr_number == 46
    assert done.branch == "SOLO-1-x"
    assert any("merge queue" in c.lower() for c in _comments(svc, tid))


def test_discovery_is_case_insensitive(tmp_path):
    # [SOLO-27 review] Hand-made branches are often lowercase (the repo's own examples
    # are, e.g. solo-9-feature) while ticket ids are uppercase — discovery must match.
    gh = FakeGitHub(open_prs=[_pr(46, "solo-1-fix")])
    svc = _svc(tmp_path, github=gh)
    tid = _to_human_review_no_branch(svc)
    done = svc.move_ticket(tid, "done", actor="human")
    assert ("merge", 46) in gh.calls
    assert done.branch == "solo-1-fix"




def test_discovery_only_adopts_prs_targeting_the_project_base(tmp_path):
    # [gpt-review r1 P1] A ticket-named PR aimed at a release/stacked branch must not be
    # adopted — merging it would land in THAT base while the note claims master. A
    # different-base candidate also must not count toward ambiguity.
    gh = FakeGitHub(open_prs=[_pr(46, "SOLO-1-x", base="release-1.x")])
    svc = _svc(tmp_path, github=gh)
    tid = _to_human_review_no_branch(svc)
    done = svc.move_ticket(tid, "done", actor="human")
    assert not any(c[0] == "merge" for c in gh.calls)
    assert done.pr_number is None
    assert "no PR was merged" in _comments(svc, tid)[0]

    gh2 = FakeGitHub(
        open_prs=[_pr(46, "SOLO-1-a", base="release-1.x"), _pr(47, "SOLO-1-b", base="main")]
    )
    svc2 = _svc(tmp_path / "mixed", github=gh2)
    tid2 = _to_human_review_no_branch(svc2)
    done2 = svc2.move_ticket(tid2, "done", actor="human")
    assert ("merge", 47) in gh2.calls  # the master-based PR, unambiguously
    assert done2.pr_number == 47


def test_discovery_excludes_fork_prs(tmp_path):
    # [gpt-review r1 P1] A cross-repository PR's head is a bare name in the FORK; branch
    # cleanup would treat it as an origin ref and could delete an unrelated same-named
    # branch. SoloPM's ownership claim only holds for same-repo heads — never adopt forks.
    gh = FakeGitHub(open_prs=[_pr(46, "SOLO-1-x", cross_repo=True)])
    svc = _svc(tmp_path, github=gh)
    tid = _to_human_review_no_branch(svc)
    done = svc.move_ticket(tid, "done", actor="human")
    assert not any(c[0] == "merge" for c in gh.calls)
    assert done.pr_number is None
    assert "no PR was merged" in _comments(svc, tid)[0]




def test_discovery_excludes_prs_recorded_on_another_ticket(tmp_path):
    # [gpt-review r6 P1] A head/PR already recorded on another ticket is that ticket's —
    # a branchless done must not re-adopt it, even when the name matches.
    gh = FakeGitHub(open_prs=[_pr(17, "SOLO-1-fix")])
    svc = _svc(tmp_path, github=gh)
    tid = _to_human_review_no_branch(svc)  # SOLO-1, nothing recorded
    other = svc.create_ticket(project="SOLO", title="claimer")  # SOLO-2
    # A branch override legally records the SOLO-1-shaped branch on SOLO-2.
    svc.move_ticket(other.id, "in-progress", branch="SOLO-1-fix", actor="claude")
    done = svc.move_ticket(tid, "done", actor="human")
    assert not any(c[0] == "merge" for c in gh.calls)
    assert done.pr_number is None
    assert "no PR was merged" in _comments(svc, tid)[0]




def test_remote_identity_matches_across_transport_schemes(tmp_path):
    # [gpt-review r10 P1] git@github.com:me/thing.git and https://github.com/Me/Thing
    # are the same repository — the shared-repo guard must normalize to
    # host/owner/repo identity, not just lowercase and trim.
    gh = FakeGitHub(
        open_prs=[_pr(46, "SOLO-1-fix")],
        remote_urls={
            "/tmp/repo": "git@github.com:me/thing.git",
            "/tmp/clone": "https://github.com/Me/Thing",
        },
    )
    svc = _svc(tmp_path, github=gh, repo="/tmp/repo")
    svc.add_project(key="B", name="B", repo="/tmp/clone", master="main")
    tid = _to_human_review_no_branch(svc)
    done = svc.move_ticket(tid, "done", actor="human")
    assert not any(c[0] == "merge" for c in gh.calls)
    assert done.pr_number is None
    assert "share this repo" in _comments(svc, tid)[0]




def test_projects_cannot_share_a_repo(tmp_path):
    # [gpt-review r8 P1] project ↔ repo is documented as 1:1 but was never enforced —
    # every repo-scoped feature (PR ownership/discovery, prune, radar, status) reasons
    # per-project and would cross wires across projects sharing a repository.
    gh = FakeGitHub()
    svc = _svc(tmp_path, github=gh, repo="/tmp/repo")
    with pytest.raises(ValidationError):
        svc.add_project(key="B", name="B", repo="/tmp/repo", master="main")
    with pytest.raises(ValidationError):  # normalization: trailing slash is the same repo
        svc.add_project(key="C", name="C", repo="/tmp/repo/", master="main")
    svc.add_project(key="D", name="D", repo="/tmp/other", master="main")
    with pytest.raises(ValidationError):  # update onto an already-mapped repo
        svc.update_project("D", {"repo": "/tmp/repo"})
    # Re-asserting a project's own repo is not a collision.
    updated = svc.update_project("SOLO", {"repo": "/tmp/repo"})
    assert updated.repo == "/tmp/repo"


def test_shared_remote_identity_declines_discovery(tmp_path):
    # [gpt-review r9 P1] Two projects on different CHECKOUTS (worktrees/clones) of one
    # GitHub repository defeat path comparison — the origin remote URL is the shared
    # identity signal, compared best-effort at discovery time (project CRUD must not
    # require a cloned repo or working git, so enforcement lives here).
    gh = FakeGitHub(
        open_prs=[_pr(46, "SOLO-1-fix")],
        remote_urls={
            "/tmp/repo": "git@github.com:me/thing.git",
            "/tmp/clone": "git@github.com:me/Thing",  # same repo: case/.git differences
        },
    )
    svc = _svc(tmp_path, github=gh, repo="/tmp/repo")
    svc.add_project(key="B", name="B", repo="/tmp/clone", master="main")
    tid = _to_human_review_no_branch(svc)
    done = svc.move_ticket(tid, "done", actor="human")
    assert not any(c[0] == "merge" for c in gh.calls)
    assert done.pr_number is None
    assert "share this repo" in _comments(svc, tid)[0]

    # Control: genuinely different remotes — discovery proceeds and adopts.
    gh2 = FakeGitHub(
        open_prs=[_pr(9, "SOLO-1-fix")],
        remote_urls={
            "/tmp/repo": "git@github.com:me/thing.git",
            "/tmp/other": "git@github.com:me/unrelated.git",
        },
    )
    svc2 = _svc(tmp_path / "distinct", github=gh2, repo="/tmp/repo")
    svc2.add_project(key="B", name="B", repo="/tmp/other", master="main")
    tid2 = _to_human_review_no_branch(svc2)
    done2 = svc2.move_ticket(tid2, "done", actor="human")
    assert ("merge", 9) in gh2.calls
    assert done2.pr_state == "merged"


def test_recreating_the_same_project_is_duplicate_not_repo_conflict(tmp_path):
    # [gpt-review r9/r12 P2] Any create with an EXISTING key is the documented 409
    # duplicate — including when the request also names a repo owned by a DIFFERENT
    # project (the key check must precede the repo-claim scan).
    from solopm.core.errors import DuplicateError

    gh = FakeGitHub()
    svc = _svc(tmp_path, github=gh, repo="/tmp/repo")
    with pytest.raises(DuplicateError):
        svc.add_project(key="SOLO", name="SoloPM", repo="/tmp/repo", master="main")
    svc.add_project(key="D", name="D", repo="/tmp/other", master="main")
    with pytest.raises(DuplicateError):  # existing key + someone else's repo → still 409
        svc.add_project(key="SOLO", name="SoloPM", repo="/tmp/other", master="main")


def test_unresolvable_home_in_repo_path_does_not_crash(tmp_path):
    # [gpt-review r12 P2] Path('~nouser').expanduser() raises RuntimeError, which an
    # OSError-only net misses — canonicalization must fall back to the raw path.
    gh = FakeGitHub()
    svc = _svc(tmp_path, github=gh, repo="/tmp/repo")
    created = svc.add_project(
        key="T", name="T", repo="~no_such_user_xyz_12345/repo", master="main"
    )
    assert created.repo == "~no_such_user_xyz_12345/repo"
    # [r13 P2] An embedded NUL raises ValueError from Path.resolve — same degrade.
    created2 = svc.add_project(key="U", name="U", repo="/tmp/x\x00y", master="main")
    assert created2.repo == "/tmp/x\x00y"


def test_legacy_shared_repo_declines_discovery(tmp_path):
    # [gpt-review r8 P1] A legacy store can already hold two projects on one repo
    # (pre-enforcement). Discovery must decline rather than reason per-project: with
    # {seq}-{slug}-style overlaps, A-1 could otherwise adopt B-1's PR.
    from solopm.core.models import DEFAULT_BRANCH_CONVENTION, DEFAULT_REVIEW_PROMPT, Project

    gh = FakeGitHub(open_prs=[_pr(46, "SOLO-1-fix")])
    svc = _svc(tmp_path, github=gh, repo="/tmp/repo")
    solo = svc.get_project("SOLO")
    svc.store.insert_project(  # bypasses service validation, like a pre-existing row
        Project(
            key="B",
            name="B",
            repo="/tmp/repo",
            master_branch="main",
            branch_convention=DEFAULT_BRANCH_CONVENTION,
            default_implementer="claude",
            default_reviewer="codex",
            review_prompt=DEFAULT_REVIEW_PROMPT,
            seq_counter=0,
            created_at=solo.created_at,
            updated_at=solo.updated_at,
        )
    )
    tid = _to_human_review_no_branch(svc)
    done = svc.move_ticket(tid, "done", actor="human")
    assert not any(c[0] == "merge" for c in gh.calls)
    assert done.pr_number is None
    note = _comments(svc, tid)[0]
    assert "share this repo" in note



def test_custom_convention_declines_discovery(tmp_path):
    # [SOLO-27 design, post-review] Automatic discovery supports only the DEFAULT
    # branch convention. Under a custom one, sibling heads can land on this ticket's
    # default <ID>- shape ({key}-{seq:x}-{slug} makes ticket 16 branch as SOLO-10-*),
    # so not even the default matcher can be trusted — and modelling arbitrary format
    # templates safely proved an open-ended problem. Custom conventions decline with a
    # note, including for default-shaped heads, and never raise (unrenderable ones too).
    conventions = (
        "feature/{key}-{seq}-{slug}",  # benign custom
        "{key}-{seq:x}-{slug}",        # the sibling/default-shape collision class
        "{key.foo}-{slug}",            # unrenderable — must decline, not crash
    )
    for i, conv in enumerate(conventions):
        gh = FakeGitHub(open_prs=[_pr(46, "SOLO-1-fix"), _pr(47, "feature/SOLO-1-fix")])
        svc = _svc(tmp_path / f"conv{i}", github=gh)
        svc.update_project("SOLO", {"branch_convention": conv})
        tid = _to_human_review_no_branch(svc)
        done = svc.move_ticket(tid, "done", actor="human")
        assert not any(c[0] == "merge" for c in gh.calls), conv
        assert done.pr_number is None, conv
        note = _comments(svc, tid)[0]
        assert "no PR was merged" in note, conv
        assert "default branch convention" in note, conv


def test_no_match_note_names_what_was_searched(tmp_path):
    # [SOLO-27 review] The note must state the pattern actually searched — not vaguely
    # claim "its branch convention" was checked.
    gh = FakeGitHub(open_prs=[])
    svc = _svc(tmp_path, github=gh)
    tid = _to_human_review_no_branch(svc)
    svc.move_ticket(tid, "done", actor="human")
    note = _comments(svc, tid)[0]
    assert "SOLO-1" in note  # the searched head shape is spelled out
