"""Overlap / conflict radar across concurrent worktrees (SOLO-9)."""

from solopm.core.github import MergeResult, PR, Worktree
from solopm.core.service import Service
from solopm.core.store import Store


class FakeRadarGit:
    """A git adapter returning canned worktrees + changed-file sets (no real git)."""

    def __init__(self, worktrees: list[Worktree], files: dict[str, set[str]]):
        self._worktrees = worktrees
        self._files = files

    def list_worktrees(self, repo: str) -> list[Worktree]:
        return self._worktrees

    def worktree_changed_files(self, worktree_path: str, base: str) -> set[str]:
        return set(self._files.get(worktree_path, set()))

    def find_pr(self, repo: str, branch: str):
        # No PR to merge/close: lets a → done transition short-circuit in tests.
        return None


class QueuedMergeRadarGit(FakeRadarGit):
    """A → done leaves the PR merely enqueued (merge-queue-protected branch)."""

    def find_pr(self, repo: str, branch: str):
        return PR(number=99, url="http://pr/99", state="open")

    def pr_head(self, repo: str, number: int):
        return "head-sha"

    def merge_pr(self, repo: str, number: int, branch=None):
        return MergeResult(state="queued")


def _svc(tmp_path, github, repo="/repo", db="solopm.db"):
    store = Store(tmp_path / db)
    store.init()
    svc = Service(store, github=github)
    svc.add_project(key="SOLO", name="SoloPM", repo=repo, master="main")
    return svc


def test_overlap_between_two_worktrees(tmp_path):
    gh = FakeRadarGit(
        [Worktree("/wt/a", "solo-1-a"), Worktree("/wt/b", "solo-2-b"), Worktree("/repo", "main")],
        {"/wt/a": {"src/x.py", "src/y.py"}, "/wt/b": {"src/y.py", "src/z.py"}, "/repo": {"q.py"}},
    )
    out = _svc(tmp_path, gh).compute_radar("SOLO")
    assert len(out["overlaps"]) == 1
    ov = out["overlaps"][0]
    assert ov["files"] == ["src/y.py"]
    assert {ov["a"]["branch"], ov["b"]["branch"]} == {"solo-1-a", "solo-2-b"}


def test_master_worktree_is_excluded(tmp_path):
    # The primary checkout on master must never appear as an overlap party.
    gh = FakeRadarGit(
        [Worktree("/repo", "main"), Worktree("/wt/a", "solo-1-a")],
        {"/repo": {"src/y.py"}, "/wt/a": {"src/y.py"}},
    )
    assert _svc(tmp_path, gh).compute_radar("SOLO")["overlaps"] == []


def test_no_overlap_when_disjoint(tmp_path):
    gh = FakeRadarGit(
        [Worktree("/wt/a", "solo-1-a"), Worktree("/wt/b", "solo-2-b")],
        {"/wt/a": {"src/x.py"}, "/wt/b": {"src/z.py"}},
    )
    assert _svc(tmp_path, gh).compute_radar("SOLO")["overlaps"] == []


def test_worktrees_with_no_changes_are_ignored(tmp_path):
    gh = FakeRadarGit(
        [Worktree("/wt/a", "solo-1-a"), Worktree("/wt/b", "solo-2-b")],
        {"/wt/a": {"src/y.py"}, "/wt/b": set()},  # b touched nothing
    )
    assert _svc(tmp_path, gh).compute_radar("SOLO")["overlaps"] == []


def test_overlap_annotated_with_active_ticket(tmp_path):
    gh = FakeRadarGit(
        [Worktree("/wt/a", "solo-1-a"), Worktree("/wt/b", "solo-2-b")],
        {"/wt/a": {"src/y.py"}, "/wt/b": {"src/y.py"}},
    )
    svc = _svc(tmp_path, gh)
    t = svc.create_ticket(project="SOLO", title="a")
    svc.move_ticket(t.id, "in-progress", branch="solo-1-a", actor="claude")  # active + branch
    ov = svc.compute_radar("SOLO")["overlaps"][0]
    by_branch = {ov["a"]["branch"]: ov["a"]["ticket"], ov["b"]["branch"]: ov["b"]["ticket"]}
    assert by_branch["solo-1-a"] == t.id  # mapped to the active ticket
    assert by_branch["solo-2-b"] is None  # unmapped branch still reported


def _drive_to(svc, ticket_id, target, *, branch):
    """Walk a ticket from in-progress up to ``target`` along the legal path."""
    svc.move_ticket(ticket_id, "in-progress", branch=branch, actor="human")
    path = ["in-ai-review", "in-human-review", "done"]
    for state in path:
        svc.move_ticket(ticket_id, state, actor="human")
        if state == target:
            break


def test_done_ticket_worktree_excluded(tmp_path):
    # A merged ticket whose worktree lingers must not raise a conflict against active work.
    gh = FakeRadarGit(
        [Worktree("/wt/a", "solo-1-a"), Worktree("/wt/b", "solo-2-b")],
        {"/wt/a": {"src/y.py"}, "/wt/b": {"src/y.py"}},
    )
    svc = _svc(tmp_path, gh)
    active = svc.create_ticket(project="SOLO", title="active")
    svc.move_ticket(active.id, "in-progress", branch="solo-1-a", actor="human")
    merged = svc.create_ticket(project="SOLO", title="merged")
    _drive_to(svc, merged.id, "done", branch="solo-2-b")
    assert svc.compute_radar("SOLO")["overlaps"] == []


def test_in_human_review_ticket_is_active(tmp_path):
    # "Ready for human review" still counts as active work for the radar.
    gh = FakeRadarGit(
        [Worktree("/wt/a", "solo-1-a"), Worktree("/wt/b", "solo-2-b")],
        {"/wt/a": {"src/y.py"}, "/wt/b": {"src/y.py"}},
    )
    svc = _svc(tmp_path, gh)
    t = svc.create_ticket(project="SOLO", title="under review")
    _drive_to(svc, t.id, "in-human-review", branch="solo-1-a")
    ov = svc.compute_radar("SOLO")["overlaps"][0]
    by_branch = {ov["a"]["branch"]: ov["a"]["ticket"], ov["b"]["branch"]: ov["b"]["ticket"]}
    assert by_branch["solo-1-a"] == t.id  # in-human-review is annotated as active
    assert by_branch["solo-2-b"] is None  # unmapped branch still reported


def test_queued_done_ticket_still_reported(tmp_path):
    # A → done whose PR only enqueued (merge queue) has not landed on master yet — its
    # changes can still conflict, so the radar must keep reporting it during that window.
    gh = QueuedMergeRadarGit(
        [Worktree("/wt/a", "solo-1-a"), Worktree("/wt/b", "solo-2-b")],
        {"/wt/a": {"src/y.py"}, "/wt/b": {"src/y.py"}},
    )
    svc = _svc(tmp_path, gh)
    queued = svc.create_ticket(project="SOLO", title="queued")
    _drive_to(svc, queued.id, "done", branch="solo-1-a")
    assert svc.get_ticket(queued.id).pr_state == "queued"  # guard: really enqueued, not merged
    ov = svc.compute_radar("SOLO")["overlaps"][0]
    by_branch = {ov["a"]["branch"]: ov["a"]["ticket"], ov["b"]["branch"]: ov["b"]["ticket"]}
    assert by_branch["solo-1-a"] == queued.id  # queued done work is still live and annotated
    assert by_branch["solo-2-b"] is None


def test_real_adapter_parses_worktrees_and_changed_files(monkeypatch):
    from solopm.core.github import GitHub

    gh = GitHub()
    porcelain = (
        "worktree /repo\nHEAD aaa\nbranch refs/heads/main\n\n"
        "worktree /wt/a\nHEAD bbb\nbranch refs/heads/solo-9-foo\n\n"
        "worktree /wt/detached\nHEAD ccc\ndetached\n\n"
    )

    def fake_run(args, cwd, check=True):
        p = type("P", (), {"returncode": 0, "stdout": "", "stderr": ""})()
        if args[:3] == ["git", "worktree", "list"]:
            p.stdout = porcelain
        elif args[:3] == ["git", "diff", "--name-only"]:
            p.stdout = "src/a.py\nsrc/b.py\n"
        elif args[:2] == ["git", "status"]:
            p.stdout = " M src/b.py\n?? src/c.py\nR  old.py -> new.py\n"
        return p

    monkeypatch.setattr(gh, "_run", fake_run)
    assert gh.list_worktrees("/repo") == [
        Worktree("/repo", "main"),
        Worktree("/wt/a", "solo-9-foo"),
        Worktree("/wt/detached", None),
    ]
    assert gh.worktree_changed_files("/wt/a", "main") == {"src/a.py", "src/b.py", "src/c.py", "new.py"}


def test_broken_repo_degrades_gracefully(tmp_path):
    # A stale / non-git repo path must not fail the radar — it skips that project.
    from solopm.core.github import GitHubError

    class BrokenGit:
        def list_worktrees(self, repo):
            raise GitHubError("not a git repository")

        def worktree_changed_files(self, path, base):
            return set()

    assert _svc(tmp_path, BrokenGit()).compute_radar("SOLO") == {"overlaps": []}


def test_no_github_is_empty(tmp_path):
    assert _svc(tmp_path, None).compute_radar("SOLO") == {"overlaps": []}


def test_no_repo_is_empty(tmp_path):
    gh = FakeRadarGit([Worktree("/wt/a", "solo-1-a")], {"/wt/a": {"x"}})
    assert _svc(tmp_path, gh, repo=None).compute_radar("SOLO") == {"overlaps": []}
