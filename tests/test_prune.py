"""SOLO-23: prune merged local branches — service orchestration (fakes) + real git."""

import shutil
import subprocess

import pytest

from solopm.core.github import GitHub, GitHubError, LocalBranch, Worktree
from solopm.core.service import Service
from solopm.core.store import Store


def _svc(tmp_path, github=None, repo="/tmp/repo", master="main"):
    store = Store(tmp_path / "solopm.db")
    store.init()
    svc = Service(store, github=github)
    svc.add_project(key="SOLO", name="SoloPM", repo=repo, master=master)
    return svc


# --- service orchestration (fake git client) --------------------------------


class FakePruneGit:
    """A minimal git fake for the prune flow: hands back branch/worktree state and records
    the destructive calls instead of running git."""

    def __init__(
        self, branches, worktrees=(), dirty=(), fail_remove=(), fail_delete=(),
        pr_heads=None, tips=None,
    ):
        self.branches = list(branches)
        self.worktrees = list(worktrees)
        self.dirty = set(dirty)
        self.fail_remove = set(fail_remove)
        self.fail_delete = set(fail_delete)
        self.pr_heads = dict(pr_heads or {})  # pr_number -> merged head oid
        self.tips = dict(tips or {})  # branch -> local tip oid
        self.removed: list[str] = []
        self.deleted: list[str] = []

    def local_branches(self, repo, master):
        return self.branches

    def list_worktrees(self, repo):
        return self.worktrees

    def worktree_is_dirty(self, path):
        return path in self.dirty

    def remove_worktree(self, repo, path):
        if path in self.fail_remove:
            raise GitHubError("worktree busy")
        self.removed.append(path)

    def delete_local_branch(self, repo, branch):
        if branch in self.fail_delete:
            raise GitHubError("branch checked out")
        self.deleted.append(branch)

    def pr_merged_head(self, repo, number):
        # pr_heads only holds the heads of PRs that are actually merged on GitHub.
        return self.pr_heads.get(number)

    def branch_tip(self, repo, branch):
        return self.tips.get(branch)


def _branch(name, *, current=False, gone=False, merged=False):
    return LocalBranch(name=name, is_current=current, upstream_gone=gone, merged=merged)


def test_prune_dry_run_lists_verified_candidates_without_deleting(tmp_path):
    gh = FakePruneGit([
        _branch("main", current=True),
        _branch("merged-ff", merged=True),  # reachable-merged → verified
        _branch("gone", gone=True),  # gone-upstream ONLY → not verified → skipped
        _branch("wip"),  # no signal → ignored
    ])
    svc = _svc(tmp_path, github=gh)
    res = svc.prune_merged_branches("SOLO")
    assert res["applied"] is False
    assert {p["branch"] for p in res["pruned"]} == {"merged-ff"}
    assert {s["branch"] for s in res["skipped"]} == {"gone"}  # surfaced, not deleted
    assert gh.deleted == [] and gh.removed == []  # dry run touches nothing
    assert next(p for p in res["pruned"] if p["branch"] == "merged-ff")["reasons"] == ["merged"]


def test_prune_gone_upstream_alone_is_not_force_deleted(tmp_path):
    """[SOLO-23 review] A gone upstream is not proof of a merge — never -D on it alone."""
    gh = FakePruneGit([_branch("gone-only", gone=True)])
    svc = _svc(tmp_path, github=gh)
    res = svc.prune_merged_branches("SOLO", apply=True)
    assert res["pruned"] == []
    assert [s["branch"] for s in res["skipped"]] == ["gone-only"]
    assert gh.deleted == []  # the unmerged branch's commits are NOT discarded


def test_prune_protects_branches_of_active_tickets(tmp_path):
    """[SOLO-23 gpt-review] A branch backing a non-terminal ticket is never pruned, even if
    reachable-merged (e.g. a fresh in-progress branch still equal to master, no commits yet)."""
    gh = FakePruneGit([_branch("active-b", merged=True)])
    svc = _svc(tmp_path, github=gh)
    t = svc.create_ticket(project="SOLO", title="active")
    svc.store.change_ticket(
        t.id, {"state": "in-progress", "branch": "active-b"}, actor="claude",
        kind="state_change", body="wip", meta={}, when="t",
    )
    res = svc.prune_merged_branches("SOLO", apply=True)
    assert res["pruned"] == [] and res["skipped"] == []  # in-use → silently protected
    assert gh.deleted == []


def test_prune_protects_current_and_master(tmp_path):
    # The current branch and master are never candidates, even if "merged".
    gh = FakePruneGit([
        _branch("main", current=False, merged=True),  # master by name
        _branch("feature", current=True, merged=True),  # current
    ])
    svc = _svc(tmp_path, github=gh, master="main")
    res = svc.prune_merged_branches("SOLO", apply=True)
    assert res["pruned"] == [] and gh.deleted == []


def test_prune_done_ticket_branch_is_a_candidate(tmp_path):
    # A branch recorded on a DONE ticket counts as merged even with no git signal.
    # done + PR merged + the branch tip still equals the merged PR head → verified.
    gh = FakePruneGit([_branch("solo-x")], pr_heads={7: "abc123"}, tips={"solo-x": "abc123"})
    svc = _svc(tmp_path, github=gh)
    t = svc.create_ticket(project="SOLO", title="x")
    svc.store.change_ticket(
        t.id, {"state": "done", "branch": "solo-x", "pr_state": "merged", "pr_number": 7},
        actor="human", kind="state_change", body="done", meta={}, when="t",
    )

    res = svc.prune_merged_branches("SOLO", apply=True)
    assert [p["branch"] for p in res["pruned"]] == ["solo-x"]
    assert "done" in res["pruned"][0]["reasons"]
    assert gh.deleted == ["solo-x"]


def test_prune_done_ticket_without_merged_pr_is_not_deleted(tmp_path):
    """[SOLO-23 review] A done ticket whose PR did NOT merge (e.g. closed) doesn't authorize
    a force-delete — its branch may hold unmerged work."""
    gh = FakePruneGit([_branch("solo-y", gone=True)])
    svc = _svc(tmp_path, github=gh)
    t = svc.create_ticket(project="SOLO", title="y")
    svc.store.change_ticket(
        t.id, {"state": "done", "branch": "solo-y", "pr_state": "closed"}, actor="human",
        kind="state_change", body="done", meta={}, when="t",
    )
    res = svc.prune_merged_branches("SOLO", apply=True)
    assert res["pruned"] == []
    assert [s["branch"] for s in res["skipped"]] == ["solo-y"]
    assert gh.deleted == []


def test_prune_done_branch_advanced_after_merge_is_skipped(tmp_path):
    """[SOLO-23 gpt-review P1] A done+merged branch whose tip advanced past the merged PR head
    (new committed work) is NOT force-deleted."""
    gh = FakePruneGit([_branch("solo-z")], pr_heads={9: "merged-head"}, tips={"solo-z": "new-tip"})
    svc = _svc(tmp_path, github=gh)
    t = svc.create_ticket(project="SOLO", title="z")
    svc.store.change_ticket(
        t.id, {"state": "done", "branch": "solo-z", "pr_state": "merged", "pr_number": 9},
        actor="human", kind="state_change", body="done", meta={}, when="t",
    )
    res = svc.prune_merged_branches("SOLO", apply=True)
    assert res["pruned"] == []
    assert [s["branch"] for s in res["skipped"]] == ["solo-z"]
    assert gh.deleted == []  # the advanced branch's new commits are not discarded


def test_prune_done_branch_not_deleted_when_pr_not_live_merged(tmp_path):
    """[SOLO-23 gpt-review] Even with a stored pr_state='merged', if the live PR isn't actually
    merged (pr_merged_head returns None), the branch is not force-deleted."""
    # tips has the branch but pr_heads is empty → pr_merged_head(11) is None (PR not live-merged).
    gh = FakePruneGit([_branch("solo-w", gone=True)], pr_heads={}, tips={"solo-w": "abc"})
    svc = _svc(tmp_path, github=gh)
    t = svc.create_ticket(project="SOLO", title="w")
    svc.store.change_ticket(
        t.id, {"state": "done", "branch": "solo-w", "pr_state": "merged", "pr_number": 11},
        actor="human", kind="state_change", body="done", meta={}, when="t",
    )
    res = svc.prune_merged_branches("SOLO", apply=True)
    assert res["pruned"] == []
    assert [s["branch"] for s in res["skipped"]] == ["solo-w"]
    assert gh.deleted == []


def test_prune_done_with_queued_pr_is_not_deleted(tmp_path):
    """[SOLO-23 gpt-review P2] A queued (merge-queued, not-yet-landed) PR doesn't authorize
    pruning."""
    gh = FakePruneGit([_branch("solo-q", gone=True)], pr_heads={5: "h"}, tips={"solo-q": "h"})
    svc = _svc(tmp_path, github=gh)
    t = svc.create_ticket(project="SOLO", title="q")
    svc.store.change_ticket(
        t.id, {"state": "done", "branch": "solo-q", "pr_state": "queued", "pr_number": 5},
        actor="human", kind="state_change", body="done", meta={}, when="t",
    )
    res = svc.prune_merged_branches("SOLO", apply=True)
    assert res["pruned"] == []
    assert [s["branch"] for s in res["skipped"]] == ["solo-q"]  # only gone-upstream → unverified
    assert gh.deleted == []


def test_prune_apply_deletes_and_handles_worktrees(tmp_path):
    gh = FakePruneGit(
        branches=[
            _branch("clean-wt", merged=True),
            _branch("dirty-wt", merged=True),
            _branch("plain", merged=True),
        ],
        worktrees=[
            Worktree(path="/wt/clean", branch="clean-wt"),
            Worktree(path="/wt/dirty", branch="dirty-wt"),
        ],
        dirty=["/wt/dirty"],
    )
    svc = _svc(tmp_path, github=gh)
    res = svc.prune_merged_branches("SOLO", apply=True)

    pruned = {p["branch"] for p in res["pruned"]}
    skipped = {s["branch"] for s in res["skipped"]}
    assert pruned == {"clean-wt", "plain"}
    assert skipped == {"dirty-wt"}  # dirty worktree → not touched
    assert gh.removed == ["/wt/clean"]  # only the clean worktree removed
    assert set(gh.deleted) == {"clean-wt", "plain"}
    # the clean-wt prune entry records the worktree it removed
    assert next(p for p in res["pruned"] if p["branch"] == "clean-wt")["worktree"] == "/wt/clean"


def test_prune_per_branch_delete_failure_is_reported_not_fatal(tmp_path):
    gh = FakePruneGit(
        branches=[_branch("a", merged=True), _branch("b", merged=True)],
        fail_delete=["a"],
    )
    svc = _svc(tmp_path, github=gh)
    res = svc.prune_merged_branches("SOLO", apply=True)
    assert [p["branch"] for p in res["pruned"]] == ["b"]
    assert [s["branch"] for s in res["skipped"]] == ["a"]
    assert gh.deleted == ["b"]  # b still pruned despite a failing


def test_prune_worktree_removal_failure_skips_branch_without_deleting(tmp_path):
    """[SOLO-23 review] If the worktree can't be removed, the branch is reported skipped and
    NOT force-deleted (which would orphan a still-checked-out branch)."""
    gh = FakePruneGit(
        branches=[_branch("held", merged=True), _branch("ok", merged=True)],
        worktrees=[Worktree(path="/wt/held", branch="held")],
        fail_remove=["/wt/held"],
    )
    svc = _svc(tmp_path, github=gh)
    res = svc.prune_merged_branches("SOLO", apply=True)
    assert [p["branch"] for p in res["pruned"]] == ["ok"]
    assert [s["branch"] for s in res["skipped"]] == ["held"]
    assert gh.deleted == ["ok"]  # 'held' is NOT deleted because its worktree survived
    assert "held" not in gh.deleted


def test_prune_no_repo_or_no_client_is_empty(tmp_path):
    svc = _svc(tmp_path / "a", github=None)
    assert svc.prune_merged_branches("SOLO") == {
        "project": "SOLO", "applied": False, "pruned": [], "skipped": []
    }
    svc2 = _svc(tmp_path / "b", github=FakePruneGit([]), repo=None)
    assert svc2.prune_merged_branches("SOLO")["pruned"] == []


def test_prune_git_error_degrades_to_empty(tmp_path):
    class Boom(FakePruneGit):
        def local_branches(self, repo, master):
            raise GitHubError("boom")

    svc = _svc(tmp_path, github=Boom([]))
    assert svc.prune_merged_branches("SOLO") == {
        "project": "SOLO", "applied": False, "pruned": [], "skipped": []
    }


# --- real git ---------------------------------------------------------------


def _git(*args, cwd):
    subprocess.run(["git", *args], cwd=str(cwd), check=True, capture_output=True, text=True)


def _init_repo(tmp_path):
    """A clone of a bare remote with an initial pushed commit on main."""
    remote = tmp_path / "remote.git"
    subprocess.run(
        ["git", "init", "--bare", "-b", "main", str(remote)], check=True, capture_output=True
    )
    repo = tmp_path / "work"
    subprocess.run(["git", "clone", str(remote), str(repo)], check=True, capture_output=True)
    _git("config", "user.email", "t@example.com", cwd=repo)
    _git("config", "user.name", "Tester", cwd=repo)
    _git("config", "commit.gpgsign", "false", cwd=repo)
    _git("commit", "--allow-empty", "-m", "init", cwd=repo)
    _git("push", "-u", "origin", "main", cwd=repo)
    return repo


def test_prune_real_git_signals(tmp_path):
    """Real git: only a reachable-merged branch is pruned. A gone-upstream branch with
    UNMERGED commits is surfaced but NOT deleted (no data loss); unmerged WIP + current +
    master are kept. Dry-run changes nothing; apply deletes."""
    if shutil.which("git") is None:
        pytest.skip("git not available")
    repo = _init_repo(tmp_path)

    # reachable-merged into main (no-ff merge) → verified → pruned
    _git("checkout", "-b", "feat-merged", cwd=repo)
    _git("commit", "--allow-empty", "-m", "m1", cwd=repo)
    _git("checkout", "main", cwd=repo)
    _git("merge", "--no-ff", "feat-merged", "-m", "merge", cwd=repo)
    # gone upstream but NEVER merged (pushed then remote-deleted) → unverified → skipped
    _git("checkout", "-b", "feat-gone", cwd=repo)
    _git("commit", "--allow-empty", "-m", "g1", cwd=repo)
    _git("push", "-u", "origin", "feat-gone", cwd=repo)
    _git("push", "origin", "--delete", "feat-gone", cwd=repo)
    _git("fetch", "--prune", "origin", cwd=repo)
    # an unmerged WIP branch (must be left alone)
    _git("checkout", "-b", "feat-wip", cwd=repo)
    _git("commit", "--allow-empty", "-m", "wip", cwd=repo)
    _git("checkout", "main", cwd=repo)  # current = main

    svc = _svc(tmp_path / "store", github=GitHub(), repo=str(repo))
    gh = GitHub()

    dry = svc.prune_merged_branches("SOLO")
    assert dry["applied"] is False
    assert {p["branch"] for p in dry["pruned"]} == {"feat-merged"}
    assert "feat-gone" in {s["branch"] for s in dry["skipped"]}  # unverified, not pruned
    # dry-run deleted nothing
    assert {"feat-merged", "feat-gone", "feat-wip", "main"} <= {
        b.name for b in gh.local_branches(str(repo), "main")
    }

    applied = svc.prune_merged_branches("SOLO", apply=True)
    assert applied["applied"] is True
    assert {p["branch"] for p in applied["pruned"]} == {"feat-merged"}
    remaining = {b.name for b in gh.local_branches(str(repo), "main")}
    assert "feat-merged" not in remaining  # the reachable-merged branch is gone
    # the gone-upstream-but-unmerged branch + WIP + current/master all survive
    assert {"main", "feat-wip", "feat-gone"} <= remaining


def test_prune_real_git_worktrees(tmp_path):
    """Real git: a merged branch in a CLEAN worktree is removed + deleted; one in a DIRTY
    worktree is skipped (worktree and branch survive)."""
    if shutil.which("git") is None:
        pytest.skip("git not available")
    repo = _init_repo(tmp_path)

    # a merged branch checked out in a clean worktree
    _git("checkout", "-b", "wt-clean", cwd=repo)
    _git("commit", "--allow-empty", "-m", "c", cwd=repo)
    _git("checkout", "main", cwd=repo)
    _git("merge", "--no-ff", "wt-clean", "-m", "m1", cwd=repo)
    clean_path = tmp_path / "wt-clean-dir"
    _git("worktree", "add", str(clean_path), "wt-clean", cwd=repo)

    # a merged branch checked out in a DIRTY worktree
    _git("checkout", "-b", "wt-dirty", cwd=repo)
    _git("commit", "--allow-empty", "-m", "d", cwd=repo)
    _git("checkout", "main", cwd=repo)
    _git("merge", "--no-ff", "wt-dirty", "-m", "m2", cwd=repo)
    dirty_path = tmp_path / "wt-dirty-dir"
    _git("worktree", "add", str(dirty_path), "wt-dirty", cwd=repo)
    (dirty_path / "uncommitted.txt").write_text("work in progress")

    svc = _svc(tmp_path / "store", github=GitHub(), repo=str(repo))
    res = svc.prune_merged_branches("SOLO", apply=True)

    assert "wt-clean" in {p["branch"] for p in res["pruned"]}
    assert "wt-dirty" in {s["branch"] for s in res["skipped"]}
    assert not clean_path.exists()  # clean worktree removed
    assert dirty_path.exists()  # dirty worktree preserved
    remaining = {b.name for b in GitHub().local_branches(str(repo), "main")}
    assert "wt-clean" not in remaining  # branch deleted
    assert "wt-dirty" in remaining  # branch kept (work not lost)


def test_prune_real_git_accepts_git_valid_branch_names(tmp_path):
    """[SOLO-23 gpt-review P2] A branch name that's valid to git but outside SoloPM's stricter
    branch regex (e.g. contains '+') must still be prunable — not abort with a ValidationError."""
    if shutil.which("git") is None:
        pytest.skip("git not available")
    repo = _init_repo(tmp_path)
    _git("checkout", "-b", "feat+plus", cwd=repo)
    _git("commit", "--allow-empty", "-m", "c", cwd=repo)
    _git("checkout", "main", cwd=repo)
    _git("merge", "--no-ff", "feat+plus", "-m", "m", cwd=repo)

    svc = _svc(tmp_path / "store", github=GitHub(), repo=str(repo))
    res = svc.prune_merged_branches("SOLO", apply=True)
    assert "feat+plus" in {p["branch"] for p in res["pruned"]}
    assert "feat+plus" not in {b.name for b in GitHub().local_branches(str(repo), "main")}


def test_worktree_is_dirty_treats_unrunnable_status_as_dirty(tmp_path):
    """[SOLO-23 review] When `git status` can't run cleanly (a valid dir that isn't a git
    worktree), treat it as DIRTY so the worktree is never removed."""
    if shutil.which("git") is None:
        pytest.skip("git not available")
    non_git = tmp_path / "plain-dir"
    non_git.mkdir()
    assert GitHub().worktree_is_dirty(str(non_git)) is True
