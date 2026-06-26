"""GitHub PR side effects on transition (SOLO-2), tested with a fake client."""

import pytest

from solopm.core.github import PR, GitHubError
from solopm.core.service import Service
from solopm.core.store import Store


class FakeGitHub:
    """Records calls; no real git/gh. `fail_on` makes one method raise."""

    def __init__(self, pr_number: int = 17, fail_on: str | None = None):
        self.calls: list[tuple] = []
        self.pr_number = pr_number
        self.fail_on = fail_on

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
