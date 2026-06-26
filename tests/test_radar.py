"""Overlap / conflict radar across concurrent worktrees (SOLO-9)."""

from solopm.core.github import Worktree
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


def test_no_github_is_empty(tmp_path):
    assert _svc(tmp_path, None).compute_radar("SOLO") == {"overlaps": []}


def test_no_repo_is_empty(tmp_path):
    gh = FakeRadarGit([Worktree("/wt/a", "solo-1-a")], {"/wt/a": {"x"}})
    assert _svc(tmp_path, gh, repo=None).compute_radar("SOLO") == {"overlaps": []}
