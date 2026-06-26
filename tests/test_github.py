"""GitHub PR side effects on transition (SOLO-2), tested with a fake client."""

import pytest

from solopm.core.errors import NotFoundError, ValidationError
from solopm.core.github import PR, GitHubError, MergeResult
from solopm.core.service import Service
from solopm.core.store import Store


class FakeGitHub:
    """Records calls; no real git/gh. `fail_on` makes one method raise."""

    def __init__(
        self,
        pr_number: int = 17,
        fail_on: str | None = None,
        merge_sha: str | None = "1a2b3c4d5e6f",
        merge_state: str = "merged",
    ):
        self.calls: list[tuple] = []
        self.pr_number = pr_number
        self.fail_on = fail_on
        self.merge_sha = merge_sha
        self.merge_state = merge_state  # "merged" or "queued"

    def _maybe_fail(self, name: str) -> None:
        if self.fail_on == name:
            raise GitHubError(f"boom in {name}")

    def push_branch(self, repo, branch):
        self._maybe_fail("push_branch")
        self.calls.append(("push", branch))

    def find_pr(self, repo, branch):
        self._maybe_fail("find_pr")
        self.calls.append(("find", branch))
        return PR(number=self.pr_number, url=f"https://github.com/thomasj02/SoloPM/pull/{self.pr_number}", state="open")

    def open_or_refresh_pr(self, repo, branch, base, title, body):
        self._maybe_fail("open_or_refresh_pr")
        self.calls.append(("pr", branch, base, title))
        return PR(number=self.pr_number, url=f"https://github.com/thomasj02/SoloPM/pull/{self.pr_number}", state="open")

    def merge_pr(self, repo, number):
        self._maybe_fail("merge_pr")
        self.calls.append(("merge", number))
        if self.merge_state == "queued":
            return MergeResult("queued")
        return MergeResult("merged", self.merge_sha)

    def close_pr(self, repo, number):
        self._maybe_fail("close_pr")
        self.calls.append(("close", number))


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


def _merge_pr_fake(*, preflight, readback, record=None):
    """Build a `_run` stand-in for merge_pr's three calls: preflight view (`--json state`),
    the merge, and the readback view (`--json state,mergeCommit`). `preflight`/`readback`
    are JSON strings, or a GitHubError instance to raise for that call."""

    def fake_run(args, cwd, check=True):
        if record is not None:
            record.append(args)
        is_merge = args[:3] == ["gh", "pr", "merge"]
        is_readback = "state,mergeCommit" in args  # the readback view's --json value
        if is_merge:
            payload = None
        elif is_readback:
            payload = readback
        else:
            payload = preflight
        if isinstance(payload, GitHubError):
            raise payload

        class P:
            returncode = 0
            stderr = ""
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
    assert ["gh", "pr", "merge", "17", "--squash", "--delete-branch"] in calls


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


def test_merge_pr_non_open_refused_before_merge(tmp_path, monkeypatch):
    # [SOLO-11] An already-merged/closed PR is refused at preflight (no double-merge),
    # before `gh pr merge` is ever issued.
    from solopm.core.github import GitHub

    gh = GitHub()
    calls = []
    monkeypatch.setattr(gh, "_run", _merge_pr_fake(
        preflight='{"state": "MERGED"}',
        readback='{"state": "MERGED", "mergeCommit": null}',
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
    assert ["gh", "pr", "merge", "17", "--squash", "--delete-branch"] in calls
