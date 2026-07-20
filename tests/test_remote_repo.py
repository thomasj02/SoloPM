"""SOLO-29: remote-repo projects — GitHub-API PR lifecycle + client-side push.

A project with ``github_repo`` set ("owner/name") has its checkout on ANOTHER machine.
The backend must never run cwd-based git/gh for it: the PR lifecycle goes through the
GitHub API (``gh --repo <slug>``), and the one operation that genuinely needs the dev
machine's disk — the branch push — happens in the client half (HTTP MCP / CLI) on the
machine that has the commits, before the move API call.
"""

import sqlite3

import pytest

from solopm.cli.client import Api, ApiError, push_branch_for_remote_move
from solopm.core.errors import ValidationError
from solopm.core.github import GitHub, GitHubError, validate_github_repo
from solopm.core.service import Service
from solopm.core.store import Store
from test_github import FakeGitHub, _pr

SLUG = "acme/widget"

# Every cwd-based FakeGitHub call name: none of these may ever fire for a remote project.
_CWD_CALLS = {"push", "find", "pr", "head", "merge", "close", "list_open"}


def _svc(tmp_path, github=None):
    store = Store(tmp_path / "solopm.db")
    store.init()
    return Service(store, github=github)


def _remote_project(svc, key="CM", slug=SLUG, **kwargs):
    return svc.add_project(
        key=key, name="ChessMimic", repo="/home/dev/chessmimic", github_repo=slug, **kwargs
    )


def _assert_no_cwd_git(gh):
    cwd_calls = [c for c in gh.calls if c[0] in _CWD_CALLS]
    assert not cwd_calls, f"backend ran cwd-based git/gh for a remote project: {cwd_calls}"


def _to_ai_review(svc, branch="CM-1-fix"):
    t = svc.create_ticket(project="CM", title="x", description="the body")
    svc.move_ticket(t.id, "in-progress")
    return t.id, svc.move_ticket(t.id, "in-ai-review", branch=branch, actor="claude")


def _comments(svc, tid):
    return [a.body for a in svc.get_ticket(tid).activity if a.kind == "comment"]


# --- model / store ------------------------------------------------------------


def test_github_repo_round_trips_via_store(tmp_path):
    svc = _svc(tmp_path)
    _remote_project(svc)
    p = svc.get_project("CM")
    assert p.github_repo == SLUG
    assert p.to_dict()["github_repo"] == SLUG
    # and stays None for a plain local project
    svc.add_project(key="LOC", name="Local", repo="/tmp/loc")
    assert svc.get_project("LOC").github_repo is None


def test_migration_adds_github_repo_column(tmp_path):
    """A store created before SOLO-29 (projects table without github_repo) migrates."""
    db = tmp_path / "old.db"
    conn = sqlite3.connect(db)
    conn.execute(
        """CREATE TABLE projects (
               key TEXT PRIMARY KEY, name TEXT NOT NULL, repo TEXT,
               master_branch TEXT NOT NULL DEFAULT 'main', branch_convention TEXT NOT NULL,
               default_implementer TEXT NOT NULL DEFAULT 'claude',
               default_reviewer TEXT NOT NULL DEFAULT 'codex',
               review_prompt TEXT NOT NULL DEFAULT '',
               review_memory TEXT NOT NULL DEFAULT '[]',
               seq_counter INTEGER NOT NULL DEFAULT 0,
               created_at TEXT NOT NULL, updated_at TEXT NOT NULL)"""
    )
    conn.execute(
        "INSERT INTO projects (key, name, branch_convention, created_at, updated_at) "
        "VALUES ('OLD', 'Old', '{key}-{seq}-{slug}', 't', 't')"
    )
    conn.commit()
    conn.close()
    store = Store(db)
    store.init()  # must ALTER the existing table
    svc = Service(store)
    assert svc.get_project("OLD").github_repo is None
    svc.update_project("OLD", {"github_repo": SLUG})
    assert svc.get_project("OLD").github_repo == SLUG


# --- slug validation / claims ---------------------------------------------------


@pytest.mark.parametrize(
    "bad",
    ["acme", "acme/", "/widget", "-acme/widget", "a/b/c", "a b/c", "acme/..", "acme/.",
     "acme/-widget", "acme/wid get", "acme/wi;dget"],
)
def test_invalid_github_repo_slugs_rejected(tmp_path, bad):
    svc = _svc(tmp_path)
    with pytest.raises(ValidationError):
        _remote_project(svc, slug=bad)


@pytest.mark.parametrize("good", ["1Nf6/chessmimic", "a-b/c.d_e", "octo/.github", "a/b"])
def test_valid_github_repo_slugs_accepted(good):
    assert validate_github_repo(good) == good


def test_github_repo_claim_is_one_to_one(tmp_path):
    svc = _svc(tmp_path)
    _remote_project(svc)
    with pytest.raises(ValidationError, match="1:1"):
        _remote_project(svc, key="CM2", slug=SLUG)
    with pytest.raises(ValidationError, match="1:1"):  # case-insensitive: same GitHub repo
        _remote_project(svc, key="CM3", slug="ACME/Widget")
    svc.add_project(key="OTH", name="Other")
    with pytest.raises(ValidationError, match="1:1"):
        svc.update_project("OTH", {"github_repo": SLUG})


def test_setting_github_repo_preflights_backend_gh_access(tmp_path):
    gh = FakeGitHub(fail_on="api_check_repo")
    svc = _svc(tmp_path, github=gh)
    with pytest.raises(ValidationError, match="(?i)access"):
        _remote_project(svc)
    # Without a github client the check can't run — the set still succeeds. Same
    # store: the failed create above must have persisted nothing.
    svc_plain = Service(svc.store)
    _remote_project(svc_plain)
    assert svc_plain.get_project("CM").github_repo == SLUG


def test_clearing_github_repo_returns_project_to_local_mode(tmp_path):
    svc = _svc(tmp_path)
    _remote_project(svc)
    svc.update_project("CM", {"github_repo": ""})
    assert svc.get_project("CM").github_repo is None


# --- backend lifecycle via the GitHub API ---------------------------------------


def test_remote_in_ai_review_verifies_branch_and_opens_pr_via_api(tmp_path):
    gh = FakeGitHub()
    svc = _svc(tmp_path, github=gh)
    _remote_project(svc)
    tid, moved = _to_ai_review(svc)
    assert ("api_branch_exists", SLUG, "CM-1-fix") in gh.calls
    assert ("api_pr", SLUG, "CM-1-fix", "main", "CM-1: x") in gh.calls
    assert moved.branch == "CM-1-fix"
    assert moved.pr_number == 17
    assert moved.pr_state == "open"
    assert ("push", "CM-1-fix") not in gh.calls  # the backend has no checkout to push
    _assert_no_cwd_git(gh)


def test_remote_in_ai_review_aborts_when_branch_not_on_origin(tmp_path):
    gh = FakeGitHub(branch_on_origin=False)
    svc = _svc(tmp_path, github=gh)
    _remote_project(svc)
    t = svc.create_ticket(project="CM", title="x")
    svc.move_ticket(t.id, "in-progress")
    with pytest.raises(GitHubError, match="(?i)not on origin"):
        svc.move_ticket(t.id, "in-ai-review", branch="CM-1-fix", actor="claude")
    after = svc.get_ticket(t.id)
    assert after.state == "in-progress"  # the move aborted
    assert after.branch is None
    assert not any(c[0] == "api_pr" for c in gh.calls)  # never got to PR creation


def test_remote_done_merges_recorded_pr_via_api(tmp_path):
    gh = FakeGitHub()
    svc = _svc(tmp_path, github=gh)
    _remote_project(svc)
    tid, _ = _to_ai_review(svc)
    svc.move_ticket(tid, "in-human-review", actor="codex")
    done = svc.move_ticket(tid, "done", actor="human")
    assert ("api_merge", SLUG, 17) in gh.calls
    assert done.pr_state == "merged"
    # Cleanup targets the freshly resolved PR head, threaded through to the merge.
    assert gh.merge_branch == "CM-1-fix"
    note = _comments(svc, tid)[-1]
    assert "Merged PR #17" in note
    # honest remote wording: the checkout lives on the dev machine, not here
    assert "dev machine" in note
    assert "worktree" not in note
    _assert_no_cwd_git(gh)


def test_remote_done_queued_merge_recorded_honestly(tmp_path):
    gh = FakeGitHub(merge_state="queued")
    svc = _svc(tmp_path, github=gh)
    _remote_project(svc)
    tid, _ = _to_ai_review(svc)
    svc.move_ticket(tid, "in-human-review", actor="codex")
    done = svc.move_ticket(tid, "done", actor="human")
    assert done.pr_state == "queued"
    assert "merge queue" in _comments(svc, tid)[-1]


def test_remote_done_resolves_pr_by_recorded_branch_via_api(tmp_path):
    gh = FakeGitHub()
    svc = _svc(tmp_path, github=gh)
    _remote_project(svc)
    # A human move records the branch but runs no automation → no PR recorded.
    t = svc.create_ticket(project="CM", title="x")
    svc.move_ticket(t.id, "in-progress")
    svc.move_ticket(t.id, "in-ai-review", branch="CM-1-fix", actor="human")
    svc.move_ticket(t.id, "in-human-review", actor="codex")
    done = svc.move_ticket(t.id, "done", actor="human")
    assert ("api_find", SLUG, "CM-1-fix") in gh.calls
    assert ("api_merge", SLUG, 17) in gh.calls
    assert done.pr_state == "merged"
    _assert_no_cwd_git(gh)


def test_remote_done_discovery_lists_open_prs_via_api(tmp_path):
    gh = FakeGitHub(open_prs=[_pr(41, head="CM-1-anything")])
    svc = _svc(tmp_path, github=gh)
    _remote_project(svc)
    t = svc.create_ticket(project="CM", title="x")
    svc.move_ticket(t.id, "in-progress")
    svc.move_ticket(t.id, "in-ai-review", actor="claude")  # branch never recorded
    svc.move_ticket(t.id, "in-human-review", actor="codex")
    done = svc.move_ticket(t.id, "done", actor="human")
    assert ("api_list_open", SLUG) in gh.calls
    assert ("api_merge", SLUG, 41) in gh.calls
    assert done.pr_number == 41
    assert done.pr_state == "merged"
    assert any("Adopted open PR #41" in c for c in _comments(svc, t.id))
    _assert_no_cwd_git(gh)


def test_remote_discovery_still_declines_custom_convention(tmp_path):
    gh = FakeGitHub(open_prs=[_pr(41, head="CM-1-x")])
    svc = _svc(tmp_path, github=gh)
    _remote_project(svc, branch_convention="{key}/{seq}-{slug}")
    t = svc.create_ticket(project="CM", title="x")
    svc.move_ticket(t.id, "in-progress")
    svc.move_ticket(t.id, "in-ai-review", actor="claude")
    svc.move_ticket(t.id, "in-human-review", actor="codex")
    done = svc.move_ticket(t.id, "done", actor="human")
    assert done.pr_number is None
    assert not any(c[0] == "api_list_open" for c in gh.calls)
    assert any("no PR was merged" in c for c in _comments(svc, t.id))


def test_remote_discovery_declines_when_projects_share_the_slug(tmp_path):
    """Two projects on one GitHub repo (a legacy store predating the slug claim) make
    PR ownership ambiguous — discovery must decline with a note, like shared local repos."""
    gh = FakeGitHub(open_prs=[_pr(41, head="CM-1-x")])
    svc = _svc(tmp_path, github=gh)
    _remote_project(svc)
    svc.add_project(key="CM2", name="Twin")
    # Bypass the service-level 1:1 claim the way a legacy store would present it.
    svc.store.update_project("CM2", {"github_repo": SLUG}, "t")
    t = svc.create_ticket(project="CM", title="x")
    svc.move_ticket(t.id, "in-progress")
    svc.move_ticket(t.id, "in-ai-review", actor="claude")
    svc.move_ticket(t.id, "in-human-review", actor="codex")
    done = svc.move_ticket(t.id, "done", actor="human")
    assert done.pr_number is None
    assert not any(c[0] == "api_merge" for c in gh.calls)
    assert any("share this repo" in c for c in _comments(svc, t.id))


def test_remote_discovery_declines_when_local_clone_shares_identity(tmp_path):
    """A LOCAL project whose origin points at the same GitHub repo as a remote project's
    slug is the same repository — ownership is ambiguous across the two projects."""
    gh = FakeGitHub(
        open_prs=[_pr(41, head="CM-1-x")],
        remote_urls={"/tmp/widget-clone": "git@github.com:acme/widget.git"},
    )
    svc = _svc(tmp_path, github=gh)
    _remote_project(svc)
    svc.add_project(key="LOC", name="Clone", repo="/tmp/widget-clone")
    t = svc.create_ticket(project="CM", title="x")
    svc.move_ticket(t.id, "in-progress")
    svc.move_ticket(t.id, "in-ai-review", actor="claude")
    svc.move_ticket(t.id, "in-human-review", actor="codex")
    done = svc.move_ticket(t.id, "done", actor="human")
    assert done.pr_number is None
    assert any("share this repo" in c for c in _comments(svc, t.id))


def test_remote_cancelled_closes_pr_via_api(tmp_path):
    gh = FakeGitHub()
    svc = _svc(tmp_path, github=gh)
    _remote_project(svc)
    tid, _ = _to_ai_review(svc)
    cancelled = svc.move_ticket(tid, "cancelled", actor="claude")
    assert ("api_close", SLUG, 17) in gh.calls
    assert cancelled.pr_state == "closed"
    assert gh.close_branch == "CM-1-fix"  # cleanup targets the resolved PR head
    note = _comments(svc, tid)[-1]
    assert "Closed PR #17" in note
    # Remote wording: only the ORIGIN ref was cleaned; the dev machine is untouched.
    assert "Origin branch" in note and "dev machine" in note
    assert "worktree" not in note
    _assert_no_cwd_git(gh)


def test_remote_planning_cancel_stays_silent(tmp_path):
    gh = FakeGitHub()
    svc = _svc(tmp_path, github=gh)
    _remote_project(svc)
    gh.calls.clear()  # drop the config-time api_check_repo preflight
    t = svc.create_ticket(project="CM", title="idea")
    cancelled = svc.move_ticket(t.id, "cancelled", actor="human")
    assert gh.calls == []  # triaging an idea probes nothing
    assert _comments(svc, t.id) == []


# --- machine-local helpers decline honestly -------------------------------------


def test_prune_declines_remote_project_with_note(tmp_path):
    gh = FakeGitHub()
    svc = _svc(tmp_path, github=gh)
    _remote_project(svc)
    gh.calls.clear()  # drop the config-time api_check_repo preflight
    result = svc.prune_merged_branches("CM")
    assert result["pruned"] == [] and result["skipped"] == []
    assert "dev machine" in result["note"]
    assert gh.calls == []


def test_radar_skips_remote_project_with_note(tmp_path):
    gh = FakeGitHub()
    svc = _svc(tmp_path, github=gh)
    _remote_project(svc)
    gh.calls.clear()  # drop the config-time api_check_repo preflight
    result = svc.compute_radar()
    assert result["overlaps"] == []
    assert result["skipped"] == [
        {"project": "CM", "reason": "remote project — its worktrees live on the dev machine, not scanned"}
    ]
    assert gh.calls == []


def test_project_status_remote_skips_local_git_query(tmp_path):
    gh = FakeGitHub()
    svc = _svc(tmp_path, github=gh)
    _remote_project(svc)
    gh.calls.clear()  # drop the config-time api_check_repo preflight
    status = svc.project_status("CM")
    assert status["unpushed_commits"] == 0
    assert gh.calls == []


# --- GitHub client: error attribution + API-mode plumbing ------------------------


def test_run_missing_repo_dir_names_the_path_not_the_binary(tmp_path):
    missing = str(tmp_path / "definitely-not-a-repo")
    with pytest.raises(GitHubError) as exc:
        GitHub()._run(["git", "--version"], cwd=missing)
    msg = str(exc.value)
    assert missing in msg
    assert "Command not found" not in msg


def test_run_missing_binary_still_blames_the_binary(tmp_path):
    with pytest.raises(GitHubError, match="Command not found"):
        GitHub()._run(["solopm-no-such-binary-xyz"], cwd=str(tmp_path))


class _Proc:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def test_api_branch_exists_distinguishes_branch_404_from_repo_404():
    gh = GitHub()
    runs = []

    def fake_run(args, cwd, *, check=True):
        runs.append((args, cwd))
        return fake_run.proc

    gh._run = fake_run  # instance attribute shadows the method

    fake_run.proc = _Proc(0, stdout='{"name": "main"}')
    assert gh.api_branch_exists(SLUG, "CM-1-fix") is True
    args, cwd = runs[-1]
    assert cwd is None and args[:2] == ["gh", "api"]

    fake_run.proc = _Proc(1, stdout='{"message": "Branch not found"}', stderr="gh: Branch not found (HTTP 404)")
    assert gh.api_branch_exists(SLUG, "CM-1-fix") is False

    fake_run.proc = _Proc(1, stdout='{"message": "Not Found"}', stderr="gh: Not Found (HTTP 404)")
    with pytest.raises(GitHubError, match="(?i)not found or not accessible"):
        gh.api_branch_exists(SLUG, "CM-1-fix")

    fake_run.proc = _Proc(1, stderr="dial tcp: connection refused")
    with pytest.raises(GitHubError, match="(?i)could not verify"):
        gh.api_branch_exists(SLUG, "CM-1-fix")


def test_api_methods_pass_repo_flag_and_no_cwd(monkeypatch):
    """Every API-mode command must carry `--repo <slug>` and never rely on a cwd."""
    gh = GitHub()
    seen = []

    def fake_run(args, cwd, *, check=True):
        seen.append((args, cwd))
        if args[:3] == ["gh", "pr", "view"]:
            return _Proc(0, stdout='{"number": 5, "url": "u", "state": "OPEN"}')
        if args[:3] == ["gh", "pr", "list"]:
            return _Proc(0, stdout="[]")
        return _Proc(0, stdout="{}")

    gh._run = fake_run
    assert gh.api_find_pr(SLUG, "CM-1-fix").number == 5
    assert gh.api_list_open_prs(SLUG) == []
    assert gh.api_pr_head(SLUG, 5) is None  # stub json has no headRefName
    for args, cwd in seen:
        assert cwd is None, args
        assert "--repo" in args and SLUG in args, args


def test_api_slug_is_validated_before_any_command(monkeypatch):
    gh = GitHub()
    gh._run = lambda *a, **k: pytest.fail("a command ran despite an invalid slug")
    with pytest.raises(ValidationError):
        gh.api_find_pr("not-a-slug", "b")
    with pytest.raises(ValidationError):
        gh.api_merge_pr("owner/re po", 1)


# --- client-side push (the HTTP MCP / CLI half) ----------------------------------


class _StubApi:
    """Just enough of Api for push_branch_for_remote_move: canned GET responses."""

    def __init__(self, ticket, project, agent="claude"):
        self._responses = {"ticket": ticket, "project": project}
        self.agent = agent
        self.gets: list[str] = []

    def get(self, path, **kwargs):
        self.gets.append(path)
        return self._responses["ticket" if "/tickets/" in path else "project"]


def test_push_helper_pushes_for_remote_project(tmp_path, monkeypatch):
    pushes = []
    monkeypatch.setattr(
        "solopm.cli.client.GitHub.push_branch", lambda self, repo, branch: pushes.append((repo, branch))
    )
    repo = tmp_path / "checkout"
    repo.mkdir()
    api = _StubApi({"project": "CM"}, {"github_repo": SLUG, "repo": str(repo)})
    push_branch_for_remote_move(api, "CM-8", "in-ai-review", "CM-8-fix")
    assert pushes == [(str(repo), "CM-8-fix")]


def test_push_helper_falls_back_to_the_recorded_branch(tmp_path, monkeypatch):
    """A re-review move legally omits the (already pinned) branch — the server falls
    back to ticket.branch, and the client half must push the same branch or the PR is
    reviewed and merged stale."""
    pushes = []
    monkeypatch.setattr(
        "solopm.cli.client.GitHub.push_branch", lambda self, repo, branch: pushes.append((repo, branch))
    )
    repo = tmp_path / "checkout"
    repo.mkdir()
    api = _StubApi(
        {"project": "CM", "branch": "CM-8-fix"}, {"github_repo": SLUG, "repo": str(repo)}
    )
    push_branch_for_remote_move(api, "CM-8", "in-ai-review", None)
    assert pushes == [(str(repo), "CM-8-fix")]


def test_push_helper_skips_human_moves(tmp_path, monkeypatch):
    """Git automation is agent-only: the backend skips it for human actors, and the
    client half must too — a human recording a branch may not even have the checkout."""
    monkeypatch.setattr(
        "solopm.cli.client.GitHub.push_branch",
        lambda self, repo, branch: pytest.fail("pushed for a human move"),
    )
    for actor in (None, "human"):
        api = _StubApi(
            {"project": "CM", "branch": "b"}, {"github_repo": SLUG, "repo": "/gone"}, agent=actor
        )
        push_branch_for_remote_move(api, "CM-8", "in-ai-review", "CM-8-fix")
        assert api.gets == []  # not even a lookup round-trip


def test_push_helper_rejects_pathological_ids_before_any_request(tmp_path):
    """Review-memory standard: identifiers in URL paths reject empty/'/'/'.'/'..'
    outright — quote() alone lets httpx dot-normalize them onto different routes."""
    api = _StubApi({}, {})
    for bad in ("", ".", "..", "CM/8"):
        with pytest.raises(ApiError, match="Invalid path value"):
            push_branch_for_remote_move(api, bad, "in-ai-review", "b")
    assert api.gets == []


def test_push_helper_noop_for_local_projects_and_other_states(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "solopm.cli.client.GitHub.push_branch",
        lambda self, repo, branch: pytest.fail("pushed for a local project"),
    )
    api = _StubApi({"project": "CM"}, {"github_repo": None, "repo": "/x"})
    push_branch_for_remote_move(api, "CM-8", "in-ai-review", "CM-8-fix")  # local → no push
    idle = _StubApi({}, {})
    push_branch_for_remote_move(idle, "CM-8", "done", "CM-8-fix")  # wrong state
    assert idle.gets == []  # not even a lookup round-trip
    # No explicit branch and none recorded on the ticket → nothing to push.
    branchless = _StubApi({"project": "CM", "branch": None}, {"github_repo": SLUG, "repo": "/x"})
    push_branch_for_remote_move(branchless, "CM-8", "in-ai-review", None)
    assert len(branchless.gets) == 1  # looked at the ticket, then stopped


def test_push_helper_missing_repo_dir_fails_before_the_move(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "solopm.cli.client.GitHub.push_branch",
        lambda self, repo, branch: pytest.fail("pushed despite a missing checkout"),
    )
    api = _StubApi({"project": "CM"}, {"github_repo": SLUG, "repo": str(tmp_path / "gone")})
    with pytest.raises(ApiError, match="(?i)not found on this machine"):
        push_branch_for_remote_move(api, "CM-8", "in-ai-review", "CM-8-fix")


def test_push_helper_wraps_push_failure_as_api_error(tmp_path, monkeypatch):
    def boom(self, repo, branch):
        raise GitHubError("`git push` failed: no upstream auth")

    monkeypatch.setattr("solopm.cli.client.GitHub.push_branch", boom)
    repo = tmp_path / "checkout"
    repo.mkdir()
    api = _StubApi({"project": "CM"}, {"github_repo": SLUG, "repo": str(repo)})
    with pytest.raises(ApiError, match="no upstream auth"):
        push_branch_for_remote_move(api, "CM-8", "in-ai-review", "CM-8-fix")


def test_http_move_ticket_pushes_before_the_move_and_aborts_on_failure(tmp_path, monkeypatch):
    """End-to-end through HttpSoloPMTools against an in-process backend."""
    from fastapi.testclient import TestClient

    from solopm.mcp.http_tools import HttpSoloPMTools
    from solopm.server.app import create_app

    store = Store(tmp_path / "http.db")
    store.init()
    app = create_app(Service(store), allowed_hosts=["*"])
    tools = HttpSoloPMTools(Api("http://test", agent="claude", client=TestClient(app)))

    repo = tmp_path / "checkout"
    repo.mkdir()
    tools.create_project(key="CM", name="ChessMimic", repo=str(repo), github_repo=SLUG)
    t = tools.create_ticket(project="CM", title="x")
    tools.move_ticket(t["id"], "in-progress")

    pushes = []
    monkeypatch.setattr(
        "solopm.cli.client.GitHub.push_branch", lambda self, r, b: pushes.append((r, b))
    )
    moved = tools.move_ticket(t["id"], "in-ai-review", branch="CM-1-fix")
    assert "error" not in moved
    assert pushes == [(str(repo), "CM-1-fix")]

    # A push failure surfaces as the uniform error dict and the move never happens.
    tools.move_ticket(t["id"], "in-progress")

    def boom(self, r, b):
        raise GitHubError("push exploded")

    monkeypatch.setattr("solopm.cli.client.GitHub.push_branch", boom)
    failed = tools.move_ticket(t["id"], "in-ai-review", branch="CM-1-fix")
    assert failed["error"]["message"] == "push exploded"
    assert tools.show_ticket(t["id"])["state"] == "in-progress"

    # The review-loop resubmission: the branch is already recorded and pinned, so the
    # move legally omits it — the client must still push the RECORDED branch, or the
    # fix commits never reach the PR and a later done merges stale code.
    pushes.clear()
    monkeypatch.setattr(
        "solopm.cli.client.GitHub.push_branch", lambda self, r, b: pushes.append((r, b))
    )
    removed = tools.move_ticket(t["id"], "in-ai-review")
    assert "error" not in removed
    assert pushes == [(str(repo), "CM-1-fix")]


# --- claim model: paths are local identities, slugs are remote ones ---------------


def test_remote_repo_paths_are_not_local_claims(tmp_path):
    """A remote project's repo names a directory on ANOTHER machine — an equal path
    string is not the same repository, so it neither claims nor blocks local paths."""
    svc = _svc(tmp_path)
    _remote_project(svc)  # CM: remote, repo=/home/dev/chessmimic (a dev-machine path)
    # A LOCAL project may use the same path string — it's a different machine's dir.
    svc.add_project(key="LOC", name="Local", repo="/home/dev/chessmimic")
    # And another REMOTE project (different slug) may too — think standardized
    # dev-container paths on two different machines.
    svc.add_project(
        key="RM2", name="Other", repo="/home/dev/chessmimic", github_repo="acme/gadget"
    )
    # Local ↔ local still enforces the 1:1 claim.
    with pytest.raises(ValidationError, match="1:1"):
        svc.add_project(key="LOC2", name="Local2", repo="/home/dev/chessmimic")


def test_clearing_github_repo_rechecks_the_path_claim(tmp_path):
    """Going remote → local re-localizes the path: if another local project has since
    claimed it, the clear must be refused, or two local projects would share a repo."""
    svc = _svc(tmp_path)
    _remote_project(svc)
    svc.add_project(key="LOC", name="Local", repo="/home/dev/chessmimic")  # ok: CM remote
    with pytest.raises(ValidationError, match="1:1"):
        svc.update_project("CM", {"github_repo": ""})


# --- API-mode command shapes for the mutating commands -----------------------------


def test_api_mutating_commands_carry_the_slug():
    """A dropped --repo on a cwd-less command would make gh act on whatever checkout
    the backend process happens to run in — every mutating command must carry the slug."""
    gh = GitHub()
    seen = []

    class Script:
        def __init__(self):
            self.no_pr_once = True

        def __call__(self, args, cwd, *, check=True):
            seen.append((args, cwd))
            if args[:3] == ["gh", "pr", "view"] and args[3] == "b-fix":  # find by branch
                if self.no_pr_once:
                    self.no_pr_once = False
                    return _Proc(1, stderr="no pull requests found")
                return _Proc(0, '{"number": 7, "url": "u", "state": "OPEN"}')
            if args[:3] == ["gh", "pr", "view"]:  # by number: preflight/readback
                return _Proc(0, '{"state": "MERGED", "mergeCommit": {"oid": "abc"}}')
            if args[:3] == ["gh", "repo", "view"]:  # config-time write preflight
                return _Proc(0, '{"name": "widget", "viewerPermission": "WRITE"}')
            return _Proc(0, "{}")

    gh._run = Script()

    pr = gh.api_open_or_refresh_pr(SLUG, "b-fix", "main", "t", "")
    assert pr.number == 7
    merged = gh.api_merge_pr(SLUG, 7, "b-fix")  # already-MERGED preflight path
    assert merged.state == "merged" and merged.sha == "abc" and merged.branch_deleted
    gh.api_close_pr(SLUG, 8, "b2")  # MERGED != CLOSED -> gh pr close runs
    gh.api_check_repo(SLUG)

    assert len(seen) >= 8
    for args, cwd in seen:
        assert cwd is None, args
        via_repo_flag = "--repo" in args and SLUG in args
        via_api_path = args[:2] == ["gh", "api"] and any(
            f"repos/{SLUG}/" in str(a) for a in args
        )
        via_repo_view = args[:3] == ["gh", "repo", "view"] and SLUG in args
        assert via_repo_flag or via_api_path or via_repo_view, args


def test_api_delete_remote_branch_semantics():
    gh = GitHub()
    calls = []

    def runner(args, cwd, *, check=True):
        calls.append((args, cwd))
        return runner.proc

    gh._run = runner
    runner.proc = _Proc(0)
    assert gh._api_delete_remote_branch(SLUG, "feature/x") is True
    args, cwd = calls[-1]
    assert cwd is None and args[:4] == ["gh", "api", "-X", "DELETE"]
    assert args[4] == f"repos/{SLUG}/git/refs/heads/feature/x"
    # An already-absent ref is a clean state (auto-delete-on-merge beat us), not a failure.
    runner.proc = _Proc(1, stderr="gh: Reference does not exist (HTTP 422)")
    assert gh._api_delete_remote_branch(SLUG, "b") is True
    runner.proc = _Proc(1, stderr="HTTP 403")
    assert gh._api_delete_remote_branch(SLUG, "b") is False
    ran = len(calls)
    assert gh._api_delete_remote_branch(SLUG, None) is False  # nothing to delete
    assert gh._api_delete_remote_branch(SLUG, "-bad") is False  # never feed git an option
    assert len(calls) == ran  # neither reached a command


# --- CLI wiring -------------------------------------------------------------------


def _cli(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from typer.testing import CliRunner

    from solopm.cli import main as cli_main
    from solopm.server.app import create_app

    store = Store(tmp_path / "cli.db")
    store.init()
    app = create_app(Service(store), allowed_hosts=["*"])
    monkeypatch.setattr(
        cli_main,
        "make_api",
        lambda call: Api("http://test", agent=call.agent, client=TestClient(app)),
    )
    runner = CliRunner()
    return runner, cli_main


def test_cli_move_pushes_for_remote_project(tmp_path, monkeypatch):
    runner, cli_main = _cli(tmp_path, monkeypatch)
    repo = tmp_path / "checkout"
    repo.mkdir()
    r = runner.invoke(
        cli_main.app,
        ["project", "add", "--key", "CM", "--name", "C", "--repo", str(repo),
         "--github-repo", SLUG, "--json"],
    )
    assert r.exit_code == 0, r.output
    runner.invoke(cli_main.app, ["ticket", "create", "--project", "CM", "--title", "x", "--json"])
    runner.invoke(cli_main.app, ["ticket", "move", "CM-1", "in-progress", "--json"])

    pushes = []
    monkeypatch.setattr(
        "solopm.cli.client.GitHub.push_branch", lambda self, rp, b: pushes.append((rp, b))
    )
    r = runner.invoke(
        cli_main.app,
        ["ticket", "move", "CM-1", "in-ai-review", "--branch", "CM-1-fix",
         "--agent", "claude", "--json"],
    )
    assert r.exit_code == 0, r.output
    assert pushes == [(str(repo), "CM-1-fix")]

    # A HUMAN move (no --agent) records the branch but never touches git.
    runner.invoke(cli_main.app, ["ticket", "move", "CM-1", "in-progress", "--json"])
    monkeypatch.setattr(
        "solopm.cli.client.GitHub.push_branch",
        lambda self, rp, b: pytest.fail("a human move pushed"),
    )
    r = runner.invoke(
        cli_main.app,
        ["ticket", "move", "CM-1", "in-ai-review", "--branch", "CM-1-fix", "--json"],
    )
    assert r.exit_code == 0, r.output


def test_cli_renders_remote_declines(tmp_path, monkeypatch):
    """The honest declines must survive the human-readable renderers — a remote
    project's prune/radar must not print as a clean scanned-and-empty result."""
    runner, cli_main = _cli(tmp_path, monkeypatch)
    r = runner.invoke(
        cli_main.app,
        ["project", "add", "--key", "CM", "--name", "C", "--repo", "/home/dev/x",
         "--github-repo", SLUG, "--json"],
    )
    assert r.exit_code == 0, r.output
    r = runner.invoke(cli_main.app, ["project", "prune", "CM"])
    assert "dev machine" in r.output
    assert "No merged local branches" not in r.output
    r = runner.invoke(cli_main.app, ["radar"])
    assert "not scanned" in r.output
    assert "CM" in r.output
    r = runner.invoke(cli_main.app, ["project", "show", "CM"])
    assert SLUG in r.output  # remote mode is visible in the project view


# --- gpt-review round 1 fixes -------------------------------------------------------


def test_remote_done_declines_recorded_pr_from_another_repo(tmp_path):
    """Changing github_repo after a PR was recorded must not route the old PR NUMBER
    into the new repo — same-numbered PRs are unrelated across repos. The action-time
    guard compares the recorded pr_url's owner/name against the current slug."""
    gh = FakeGitHub()
    svc = _svc(tmp_path, github=gh)
    _remote_project(svc)
    tid, _ = _to_ai_review(svc)  # records PR #17 at github.com/acme/widget
    svc.move_ticket(tid, "in-human-review", actor="codex")
    svc.update_project("CM", {"github_repo": "other/repo"})  # slug re-pointed
    done = svc.move_ticket(tid, "done", actor="human")
    assert not any(c[0] == "api_merge" for c in gh.calls)
    assert done.pr_state != "merged"
    note = _comments(svc, tid)[-1]
    assert "no PR was merged" in note and "other/repo" in note


def test_remote_cancel_declines_recorded_pr_from_another_repo(tmp_path):
    gh = FakeGitHub()
    svc = _svc(tmp_path, github=gh)
    _remote_project(svc)
    tid, _ = _to_ai_review(svc)
    svc.update_project("CM", {"github_repo": "other/repo"})
    cancelled = svc.move_ticket(tid, "cancelled", actor="claude")
    assert not any(c[0] == "api_close" for c in gh.calls)
    assert any("no PR was closed" in c for c in _comments(svc, tid))


def test_api_check_repo_requires_write_permission():
    """`gh repo view` succeeds with read access, but merge/close/branch-delete need
    write — the config-time preflight must reject read-only visibility."""
    gh = GitHub()

    def run_with(permission):
        def fake_run(args, cwd, *, check=True):
            assert cwd is None and "viewerPermission" in " ".join(args)
            return _Proc(0, f'{{"name": "widget", "viewerPermission": "{permission}"}}')
        gh._run = fake_run

    for ok in ("ADMIN", "MAINTAIN", "WRITE"):
        run_with(ok)
        gh.api_check_repo(SLUG)  # must not raise
    for insufficient in ("READ", "TRIAGE", "NONE", ""):
        run_with(insufficient)
        with pytest.raises(GitHubError, match="(?i)write"):
            gh.api_check_repo(SLUG)


def test_push_helper_normalizes_the_actor(tmp_path, monkeypatch):
    """The backend strips + lowercases X-SoloPM-Actor, so `--agent HUMAN` is a human
    move server-side — the client gate must apply the same normalization."""
    monkeypatch.setattr(
        "solopm.cli.client.GitHub.push_branch",
        lambda self, repo, branch: pytest.fail("pushed for a (normalized) human move"),
    )
    api = _StubApi({"project": "CM", "branch": "b"}, {"github_repo": SLUG, "repo": "/x"},
                   agent=" HUMAN ")
    push_branch_for_remote_move(api, "CM-8", "in-ai-review", "CM-8-fix")
    assert api.gets == []


def test_push_helper_translates_expanduser_failures(tmp_path, monkeypatch):
    """`~nosuchuser/...` makes Path.expanduser raise RuntimeError — that must surface
    as the layer's structured ApiError, not an internal exception."""
    monkeypatch.setattr(
        "solopm.cli.client.GitHub.push_branch",
        lambda self, repo, branch: pytest.fail("pushed despite an unexpandable path"),
    )
    api = _StubApi(
        {"project": "CM"},
        {"github_repo": SLUG, "repo": "~no-such-user-solopm-xyz/repo"},
    )
    with pytest.raises(ApiError):
        push_branch_for_remote_move(api, "CM-8", "in-ai-review", "CM-8-fix")


def test_push_helper_refuses_a_checkout_whose_origin_is_another_repo(tmp_path, monkeypatch):
    """Pushing a fork (or an unrelated checkout that happens to sit at the configured
    path) would pollute the wrong repository — or worse, leave a same-named stale
    branch on the real slug to be PR'd. Compare the checkout's origin owner/name
    against the slug before pushing; host differences (SSH aliases) are tolerated."""
    pushes = []
    monkeypatch.setattr(
        "solopm.cli.client.GitHub.push_branch", lambda self, repo, branch: pushes.append(branch)
    )
    repo = tmp_path / "checkout"
    repo.mkdir()
    api_for = lambda: _StubApi({"project": "CM"}, {"github_repo": SLUG, "repo": str(repo)})

    # Fork / unrelated origin → refuse before pushing.
    monkeypatch.setattr(
        "solopm.cli.client.GitHub.remote_url", lambda self, r: "git@github.com:evil/fork.git"
    )
    with pytest.raises(ApiError, match="(?i)origin"):
        push_branch_for_remote_move(api_for(), "CM-8", "in-ai-review", "CM-8-fix")
    assert pushes == []

    # An SSH host alias for the same owner/name is the SAME repo — must pass
    # (this is exactly how multi-account setups address GitHub).
    monkeypatch.setattr(
        "solopm.cli.client.GitHub.remote_url",
        lambda self, r: "git@github-widget:ACME/Widget.git",
    )
    push_branch_for_remote_move(api_for(), "CM-8", "in-ai-review", "CM-8-fix")
    assert pushes == ["CM-8-fix"]

    # Unreadable origin degrades to best-effort: push and let git/the backend decide.
    monkeypatch.setattr("solopm.cli.client.GitHub.remote_url", lambda self, r: None)
    push_branch_for_remote_move(api_for(), "CM-8", "in-ai-review", "CM-8-fix")
    assert pushes == ["CM-8-fix", "CM-8-fix"]
