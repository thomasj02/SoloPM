"""Tests for the CLI, driven against an in-process backend (no live server)."""

import json

import pytest
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from solopm.cli import main as cli_main
from solopm.cli.client import Api
from solopm.core.service import Service
from solopm.core.store import Store
from solopm.server.app import create_app

runner = CliRunner()


@pytest.fixture
def wired(tmp_path, monkeypatch):
    """Point the CLI's Api at an in-process backend (TestClient) on a temp store."""
    store = Store(tmp_path / "solopm.db")
    store.init()
    app = create_app(Service(store), allowed_hosts=["*"])

    def fake_make_api(call: cli_main.Call) -> Api:
        client = TestClient(app)
        return Api("http://test", agent=call.agent, client=client)

    monkeypatch.setattr(cli_main, "make_api", fake_make_api)
    return app


def invoke(*args):
    return runner.invoke(cli_main.app, list(args))


def test_project_add_and_list_json(wired):
    r = invoke("project", "add", "--key", "SOLO", "--name", "SoloPM", "--json")
    assert r.exit_code == 0, r.output
    assert json.loads(r.output)["key"] == "SOLO"

    r = invoke("project", "list", "--json")
    assert r.exit_code == 0
    assert json.loads(r.output)["projects"][0]["key"] == "SOLO"


def test_ticket_lifecycle_json(wired):
    invoke("project", "add", "--key", "SOLO", "--name", "SoloPM")
    r = invoke("ticket", "create", "--project", "SOLO", "--title", "Build it", "--json")
    assert r.exit_code == 0, r.output
    tid = json.loads(r.output)["id"]
    assert tid == "SOLO-1"

    # assign + move with agent attribution
    r = invoke("ticket", "assign", tid, "claude", "--json")
    assert json.loads(r.output)["assignee"] == "claude"

    r = invoke("ticket", "move", tid, "todo", "--json")
    assert json.loads(r.output)["state"] == "todo"
    r = invoke("ticket", "move", tid, "in-progress", "--agent", "claude", "--json")
    assert json.loads(r.output)["state"] == "in-progress"

    # comment as agent
    r = invoke("ticket", "comment", tid, "-b", "working on it", "--agent", "claude", "--json")
    assert r.exit_code == 0
    assert json.loads(r.output)["actor"] == "claude"

    # show reflects the comment
    r = invoke("ticket", "show", tid, "--json")
    body = json.loads(r.output)
    assert body["comments"][0]["body"] == "working on it"
    assert body["comments"][0]["author"] == "claude"


def test_agent_cannot_close_via_cli(wired):
    invoke("project", "add", "--key", "SOLO", "--name", "SoloPM")
    invoke("ticket", "create", "--project", "SOLO", "--title", "x")
    invoke("ticket", "move", "SOLO-1", "in-progress")
    invoke("ticket", "move", "SOLO-1", "in-ai-review", "--agent", "claude")
    invoke("ticket", "move", "SOLO-1", "in-human-review", "--agent", "codex")
    r = invoke("ticket", "move", "SOLO-1", "done", "--agent", "claude", "--json")
    assert r.exit_code == 1
    assert json.loads(r.output)["error"]["code"] == "forbidden_transition"

    r = invoke("ticket", "move", "SOLO-1", "done", "--json")
    assert r.exit_code == 0
    assert json.loads(r.output)["state"] == "done"


def test_ticket_reorder_json(wired):
    invoke("project", "add", "--key", "SOLO", "--name", "SoloPM")
    for t in ("a", "b", "c"):
        invoke("ticket", "create", "--project", "SOLO", "--title", t)
    # move SOLO-1 to the top is a no-op; move it below SOLO-3 instead
    r = invoke("ticket", "reorder", "SOLO-1", "--after", "SOLO-3", "--json")
    assert r.exit_code == 0, r.output
    assert json.loads(r.output)["id"] == "SOLO-1"
    # reorder to top
    r = invoke("ticket", "reorder", "SOLO-3", "--json")
    assert r.exit_code == 0
    # cross-column reorder is rejected
    invoke("ticket", "move", "SOLO-2", "todo")
    r = invoke("ticket", "reorder", "SOLO-1", "--after", "SOLO-2", "--json")
    assert r.exit_code == 1
    assert json.loads(r.output)["error"]["code"] == "validation"


def test_review_submit_pass_and_fail(wired):
    invoke("project", "add", "--key", "SOLO", "--name", "SoloPM")
    invoke("ticket", "create", "--project", "SOLO", "--title", "x")
    invoke("ticket", "move", "SOLO-1", "in-progress")
    invoke("ticket", "move", "SOLO-1", "in-ai-review", "--agent", "claude")
    # fail → kicks back to in-progress with the notes attributed to the reviewer
    r = invoke("review", "submit", "SOLO-1", "--verdict", "fail", "-c", "add tests",
               "--agent", "codex", "--json")
    assert r.exit_code == 0, r.output
    assert json.loads(r.output)["state"] == "in-progress"
    # re-review: back up to in-ai-review, then pass → in-human-review
    invoke("ticket", "move", "SOLO-1", "in-ai-review", "--agent", "claude")
    r = invoke("review", "submit", "SOLO-1", "--verdict", "pass", "--agent", "codex", "--json")
    assert json.loads(r.output)["state"] == "in-human-review"


def test_review_submit_wrong_state_errors(wired):
    invoke("project", "add", "--key", "SOLO", "--name", "SoloPM")
    invoke("ticket", "create", "--project", "SOLO", "--title", "x")  # backlog
    r = invoke("review", "submit", "SOLO-1", "--verdict", "pass", "--json")
    assert r.exit_code == 1
    assert json.loads(r.output)["error"]["code"] == "validation"


def test_error_contract_json_on_missing_ticket(wired):
    r = invoke("ticket", "show", "SOLO-999", "--json")
    assert r.exit_code == 1
    assert json.loads(r.output)["error"]["code"] == "not_found"


def test_human_output_does_not_crash(wired):
    invoke("project", "add", "--key", "SOLO", "--name", "SoloPM")
    invoke("ticket", "create", "--project", "SOLO", "--title", "Readable")
    r = invoke("ticket", "list", "--project", "SOLO")
    assert r.exit_code == 0
    assert "Readable" in r.output


def test_create_ticket_without_project_errors(wired, monkeypatch):
    monkeypatch.delenv("SOLOPM_PROJECT", raising=False)
    r = invoke("ticket", "create", "--title", "orphan", "--json")
    assert r.exit_code == 1
    assert json.loads(r.output)["error"]["code"] == "validation"


def test_version():
    r = runner.invoke(cli_main.app, ["--version"])
    assert r.exit_code == 0
    assert r.output.strip()


def test_review_memory_via_cli(wired):
    invoke("project", "add", "--key", "SOLO", "--name", "SoloPM")
    r = invoke("review", "memory", "add", "SOLO", "check security", "--json")
    assert r.exit_code == 0, r.output
    mid = json.loads(r.output)["id"]
    r = invoke("review", "memory", "list", "SOLO", "--json")
    assert any(i["id"] == mid for i in json.loads(r.output)["items"])
    r = invoke("review", "memory", "set", "SOLO", mid, "--status", "retired", "--json")
    assert json.loads(r.output)["status"] == "retired"


def test_radar_cli(wired):
    invoke("project", "add", "--key", "SOLO", "--name", "SoloPM")
    r = invoke("radar", "--json")
    assert r.exit_code == 0, r.output
    assert json.loads(r.output) == {"overlaps": []}


def test_links_roundtrip_via_cli(wired):
    invoke("project", "add", "--key", "SOLO", "--name", "SoloPM")
    invoke("ticket", "create", "--project", "SOLO", "--title", "a")
    invoke("ticket", "create", "--project", "SOLO", "--title", "b")
    # link SOLO-1 blocks SOLO-2 (attributed to claude)
    r = invoke("ticket", "link", "SOLO-1", "blocks", "SOLO-2", "--agent", "claude", "--json")
    assert r.exit_code == 0, r.output
    body = json.loads(r.output)
    assert body["relations"][0]["key"] == "blocks"
    assert body["relations"][0]["ticket"]["id"] == "SOLO-2"
    # the inverse appears on SOLO-2 in show
    r = invoke("ticket", "show", "SOLO-2", "--json")
    assert json.loads(r.output)["relations"][0]["key"] == "blocked_by"
    # unlink (order-independent)
    r = invoke("ticket", "unlink", "SOLO-2", "SOLO-1", "--json")
    assert r.exit_code == 0, r.output
    assert json.loads(r.output)["relations"] == []


def test_link_self_link_rejected_via_cli(wired):
    invoke("project", "add", "--key", "SOLO", "--name", "SoloPM")
    invoke("ticket", "create", "--project", "SOLO", "--title", "a")
    r = invoke("ticket", "link", "SOLO-1", "blocks", "SOLO-1", "--json")
    assert r.exit_code == 1
    assert json.loads(r.output)["error"]["code"] == "validation"


def test_unlink_type_filter_via_cli(wired):
    invoke("project", "add", "--key", "SOLO", "--name", "SoloPM")
    invoke("ticket", "create", "--project", "SOLO", "--title", "a")
    invoke("ticket", "create", "--project", "SOLO", "--title", "b")
    invoke("ticket", "link", "SOLO-1", "blocks", "SOLO-2")
    invoke("ticket", "link", "SOLO-1", "related", "SOLO-2")
    r = invoke("ticket", "unlink", "SOLO-1", "SOLO-2", "--type", "blocks", "--json")
    assert r.exit_code == 0, r.output
    keys = {rel["key"] for rel in json.loads(r.output)["relations"]}
    assert keys == {"related"}


def test_human_show_renders_relations(wired):
    invoke("project", "add", "--key", "SOLO", "--name", "SoloPM")
    invoke("ticket", "create", "--project", "SOLO", "--title", "Alpha")
    invoke("ticket", "create", "--project", "SOLO", "--title", "Beta")
    invoke("ticket", "link", "SOLO-1", "blocks", "SOLO-2")
    r = invoke("ticket", "show", "SOLO-1")
    assert r.exit_code == 0
    assert "Relations" in r.output
    assert "SOLO-2" in r.output


def test_criteria_roundtrip_via_cli(wired):
    invoke("project", "add", "--key", "SOLO", "--name", "SoloPM")
    invoke("ticket", "create", "--project", "SOLO", "--title", "x")
    r = invoke("ticket", "criteria", "add", "SOLO-1", "tests pass", "--json")
    assert r.exit_code == 0, r.output
    cid = json.loads(r.output)["acceptance_criteria"][0]["id"]
    r = invoke("ticket", "criteria", "check", "SOLO-1", cid, "--json")
    assert json.loads(r.output)["acceptance_criteria"][0]["done"] is True
    r = invoke("ticket", "criteria", "remove", "SOLO-1", cid, "--json")
    assert json.loads(r.output)["acceptance_criteria"] == []
