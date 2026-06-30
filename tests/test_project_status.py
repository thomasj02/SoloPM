"""Project git/PR status (SOLO-12): open-PR + unpushed-commit counts.

The adapter's git query is tested with a fake ``_run``; the service's aggregation and its
graceful degradation are tested with a fake git/gh client.
"""

import pytest

from solopm.core.github import GitHub, GitHubError, PR
from solopm.core.service import Service
from solopm.core.store import Store


# --- the adapter's git query -------------------------------------------------


def _fake_run(returncode, stdout, stderr="", record=None):
    def run(args, cwd, check=True):
        if record is not None:
            record.append(args)

        class P:
            pass

        p = P()
        p.returncode, p.stdout, p.stderr = returncode, stdout, stderr
        return p

    return run


def _fake_run_map(responses, record=None):
    """A git fake that dispatches on the subcommand (``args[1]``), e.g. 'for-each-ref' / 'log'.

    ``responses`` maps a subcommand to ``(returncode, stdout)``; unlisted subcommands return
    ``(0, "")``. ``count_unpushed_commits`` now makes two calls, so the fixed-response fake
    isn't enough on its own.
    """
    def run(args, cwd, check=True):
        if record is not None:
            record.append(args)
        rc, out = responses.get(args[1], (0, ""))

        class P:
            pass

        p = P()
        p.returncode, p.stdout, p.stderr = rc, out, ""
        return p

    return run


def test_count_unpushed_excludes_gone_upstream_branches(monkeypatch):
    # SOLO-22: branches whose upstream is gone (squash-merged + remote deleted) are dropped;
    # the log query runs over only the surviving branches.
    gh = GitHub()
    calls = []
    fer = (
        "refs/heads/main\t\n"  # in sync (empty track)
        "refs/heads/feature\t[ahead 2]\n"  # ahead of a live upstream
        "refs/heads/merged-cleaned\t[gone]\n"  # squash-merged + remote deleted → excluded
        "refs/heads/wip\t\n"  # never pushed (no upstream) → counts
    )
    monkeypatch.setattr(
        gh, "_run", _fake_run_map({"for-each-ref": (0, fer), "log": (0, "a1\nb2\nc3\n")}, record=calls)
    )
    assert gh.count_unpushed_commits("/repo") == 3
    assert calls[0][:2] == ["git", "for-each-ref"]
    log_args = calls[1]
    assert log_args[:2] == ["git", "log"]
    assert {"--not", "--remotes"} <= set(log_args)
    assert "refs/heads/merged-cleaned" not in log_args  # the gone branch is excluded
    # The branch refs must be POSITIVE revisions — listed BEFORE `--not` (which negates
    # everything after it, so a branch placed after `--not` would be subtracted, not counted).
    not_idx = log_args.index("--not")
    for b in ("refs/heads/main", "refs/heads/feature", "refs/heads/wip"):
        assert b in log_args and log_args.index(b) < not_idx


def test_count_unpushed_all_branches_gone_returns_zero_without_log(monkeypatch):
    gh = GitHub()
    calls = []
    fer = "refs/heads/a\t[gone]\nrefs/heads/b\t[gone]\n"
    monkeypatch.setattr(gh, "_run", _fake_run_map({"for-each-ref": (0, fer)}, record=calls))
    assert gh.count_unpushed_commits("/repo") == 0
    assert len(calls) == 1  # nothing to count → the log query is never run


def test_count_unpushed_commits_zero_when_no_unpushed(monkeypatch):
    # No branches at all (detached HEAD / empty for-each-ref) → zero.
    gh = GitHub()
    monkeypatch.setattr(gh, "_run", _fake_run_map({"for-each-ref": (0, "")}))
    assert gh.count_unpushed_commits("/repo") == 0


def test_count_unpushed_for_each_ref_nonzero_exit_is_zero(monkeypatch):
    # A non-git / bare / odd repo exits non-zero on the first query — report zero, don't raise.
    gh = GitHub()
    monkeypatch.setattr(gh, "_run", _fake_run_map({"for-each-ref": (128, "")}))
    assert gh.count_unpushed_commits("/repo") == 0


def test_count_unpushed_log_nonzero_exit_is_zero(monkeypatch):
    gh = GitHub()
    fer = "refs/heads/main\t\n"
    monkeypatch.setattr(
        gh, "_run", _fake_run_map({"for-each-ref": (0, fer), "log": (128, "")})
    )
    assert gh.count_unpushed_commits("/repo") == 0


def test_count_unpushed_real_git_end_to_end(tmp_path):
    """Real git (no fake), so git's revision semantics are actually exercised — this is what
    catches the `--not` arg-ordering bug a canned fake can't: a never-pushed branch's commits
    are counted, a pushed branch's are not, and a gone-upstream (remote-deleted) branch is
    excluded. [SOLO-22, gpt-review]"""
    import shutil
    import subprocess

    if shutil.which("git") is None:
        pytest.skip("git not available")

    def git(*args, cwd):
        subprocess.run(
            ["git", *args], cwd=str(cwd), check=True, capture_output=True, text=True
        )

    remote = tmp_path / "remote.git"
    subprocess.run(
        ["git", "init", "--bare", "-b", "main", str(remote)], check=True, capture_output=True
    )
    repo = tmp_path / "work"
    subprocess.run(["git", "clone", str(remote), str(repo)], check=True, capture_output=True)
    git("config", "user.email", "t@example.com", cwd=repo)
    git("config", "user.name", "Tester", cwd=repo)
    git("config", "commit.gpgsign", "false", cwd=repo)

    gh = GitHub()

    # Initial commit on main, pushed → nothing unpushed.
    git("commit", "--allow-empty", "-m", "init", cwd=repo)
    git("push", "-u", "origin", "main", cwd=repo)
    assert gh.count_unpushed_commits(str(repo)) == 0

    # A never-pushed feature branch with 2 commits → counted (the regression the fake missed).
    git("checkout", "-b", "feature", cwd=repo)
    git("commit", "--allow-empty", "-m", "c1", cwd=repo)
    git("commit", "--allow-empty", "-m", "c2", cwd=repo)
    assert gh.count_unpushed_commits(str(repo)) == 2

    # Push it → now on the remote → not counted.
    git("push", "-u", "origin", "feature", cwd=repo)
    assert gh.count_unpushed_commits(str(repo)) == 0

    # Delete the remote branch + prune → upstream gone (the squash-merge cleanup) → excluded.
    git("push", "origin", "--delete", "feature", cwd=repo)
    git("fetch", "--prune", "origin", cwd=repo)
    assert gh.count_unpushed_commits(str(repo)) == 0


def test_run_wraps_bad_cwd_oserror_as_github_error(tmp_path):
    # [gpt-review P2] A repo path that is a regular file (or unreadable dir) makes the spawn
    # raise NotADirectoryError/PermissionError (OSError), not a non-zero exit. The shared
    # `_run` must wrap it as GitHubError so callers' graceful paths absorb it — not let a raw
    # OSError escape (which would 500 the status endpoint).
    repo_file = tmp_path / "not-a-dir"
    repo_file.write_text("x")
    gh = GitHub()
    with pytest.raises(GitHubError):
        gh.count_unpushed_commits(str(repo_file))


# --- the service aggregation -------------------------------------------------


class FakeGit:
    """A minimal git/gh fake: enough PR side-effects to open PRs, plus the unpushed count.

    ``unpushed`` is what ``count_unpushed_commits`` returns; ``raises=True`` makes it raise
    ``GitHubError`` (simulating a missing git / timeout) so the degradation path is covered.
    """

    def __init__(self, unpushed: int = 0, raises: bool = False):
        self.unpushed = unpushed
        self.raises = raises
        self.count_calls: list[str] = []

    def push_branch(self, repo, branch):
        pass

    def open_or_refresh_pr(self, repo, branch, base, title, body):
        return PR(number=1, url="https://example/pr/1", state="open")

    def count_unpushed_commits(self, repo):
        self.count_calls.append(repo)
        if self.raises:
            raise GitHubError("boom")
        return self.unpushed


def _svc(tmp_path, github=None, repo="/tmp/repo"):
    store = Store(tmp_path / "solopm.db")
    store.init()
    svc = Service(store, github=github)
    svc.add_project(key="SOLO", name="SoloPM", repo=repo, master="main")
    return svc


def _open_pr_ticket(svc, branch):
    t = svc.create_ticket(project="SOLO", title="x")
    svc.move_ticket(t.id, "in-progress")
    svc.move_ticket(t.id, "in-ai-review", branch=branch, actor="claude")
    return t.id


def test_status_counts_open_prs_and_unpushed(tmp_path):
    gh = FakeGit(unpushed=4)
    svc = _svc(tmp_path, github=gh)
    _open_pr_ticket(svc, "solo-12-a")
    _open_pr_ticket(svc, "solo-12-b")
    status = svc.project_status("SOLO")
    assert status == {"open_prs": 2, "unpushed_commits": 4}
    assert gh.count_calls == ["/tmp/repo"]


def test_status_excludes_non_open_prs(tmp_path):
    # Only tickets whose recorded PR is still `open` count; a branchless backlog ticket and
    # a never-opened ticket do not.
    gh = FakeGit(unpushed=0)
    svc = _svc(tmp_path, github=gh)
    _open_pr_ticket(svc, "solo-12-a")
    svc.create_ticket(project="SOLO", title="no pr")  # backlog, no PR
    assert svc.project_status("SOLO")["open_prs"] == 1


def test_status_no_repo_returns_zero_unpushed(tmp_path):
    # No repo configured → unpushed is zero and git is never consulted (no 500).
    gh = FakeGit(unpushed=9)
    svc = _svc(tmp_path, github=gh, repo=None)
    status = svc.project_status("SOLO")
    assert status["unpushed_commits"] == 0
    assert gh.count_calls == []


def test_status_git_error_degrades_to_zero(tmp_path):
    # A git failure (missing git / timeout surfacing as GitHubError) degrades to zero.
    gh = FakeGit(raises=True)
    svc = _svc(tmp_path, github=gh)
    assert svc.project_status("SOLO")["unpushed_commits"] == 0


def test_status_bad_repo_path_degrades_to_zero_with_real_client(tmp_path):
    # [gpt-review P2] End-to-end with the REAL GitHub client: a repo path pointing at a file
    # (NotADirectoryError from the spawn) degrades to zeros instead of escaping as a 500.
    repo_file = tmp_path / "repo-file"
    repo_file.write_text("not a dir")
    svc = _svc(tmp_path, github=GitHub(), repo=str(repo_file))
    assert svc.project_status("SOLO") == {"open_prs": 0, "unpushed_commits": 0}


def test_status_no_github_client_returns_zero_unpushed(tmp_path):
    svc = _svc(tmp_path, github=None)
    assert svc.project_status("SOLO") == {"open_prs": 0, "unpushed_commits": 0}


def test_status_unknown_project_raises_not_found(tmp_path):
    from solopm.core.errors import NotFoundError

    svc = _svc(tmp_path, github=None)
    with pytest.raises(NotFoundError):
        svc.project_status("NOPE")
