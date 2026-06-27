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


def test_count_unpushed_commits_counts_log_lines(monkeypatch):
    # `git log --branches --not --remotes --format=%H` prints one sha per line.
    gh = GitHub()
    calls = []
    monkeypatch.setattr(gh, "_run", _fake_run(0, "a1\nb2\nc3\n", record=calls))
    assert gh.count_unpushed_commits("/repo") == 3
    args = calls[0]
    assert args[:2] == ["git", "log"]
    assert {"--branches", "--not", "--remotes"} <= set(args)


def test_count_unpushed_commits_zero_when_no_unpushed(monkeypatch):
    gh = GitHub()
    monkeypatch.setattr(gh, "_run", _fake_run(0, ""))
    assert gh.count_unpushed_commits("/repo") == 0


def test_count_unpushed_commits_nonzero_exit_is_zero(monkeypatch):
    # A non-git / bare / odd repo exits non-zero — report zero, don't raise.
    gh = GitHub()
    monkeypatch.setattr(gh, "_run", _fake_run(128, "", "not a git repository"))
    assert gh.count_unpushed_commits("/repo") == 0


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
