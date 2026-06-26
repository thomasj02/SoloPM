"""Tests for the HTTP API via FastAPI's TestClient."""

import pytest
from fastapi.testclient import TestClient

from solopm.core.service import Service
from solopm.core.store import Store
from solopm.server.app import create_app


@pytest.fixture
def client(tmp_path):
    store = Store(tmp_path / "solopm.db")
    store.init()
    # allowed_hosts=["*"] disables host-checking so TestClient's "testserver" host works.
    app = create_app(Service(store), allowed_hosts=["*"])
    return TestClient(app)


def _make_project(client, key="SOLO", name="SoloPM"):
    return client.post("/api/projects", json={"key": key, "name": name, "master": "main"})


# --- meta -------------------------------------------------------------------


def test_meta(client):
    r = client.get("/api/meta")
    assert r.status_code == 200
    body = r.json()
    assert "backlog" in body["states"]
    assert body["transitions"]["in-human-review"] == ["in-progress", "done", "cancelled"]


# --- projects ---------------------------------------------------------------


def test_create_and_get_project(client):
    r = _make_project(client)
    assert r.status_code == 201
    assert r.json()["key"] == "SOLO"

    r2 = client.get("/api/projects/SOLO")
    assert r2.status_code == 200
    assert r2.json()["name"] == "SoloPM"


def test_create_project_lowercase_key_normalized(client):
    r = client.post("/api/projects", json={"key": "blog", "name": "Blog"})
    assert r.status_code == 201
    assert r.json()["key"] == "BLOG"


def test_duplicate_project_409(client):
    _make_project(client)
    r = _make_project(client)
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "duplicate"


def test_missing_project_404(client):
    r = client.get("/api/projects/NOPE")
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "not_found"


def test_list_projects(client):
    _make_project(client, "SOLO", "SoloPM")
    _make_project(client, "BLOG", "Blog")
    r = client.get("/api/projects")
    assert {p["key"] for p in r.json()["projects"]} == {"SOLO", "BLOG"}


def test_patch_project_field_value_form(client):
    _make_project(client)
    r = client.patch("/api/projects/SOLO", json={"field": "review_prompt", "value": "Strict."})
    assert r.status_code == 200
    assert r.json()["review_prompt"] == "Strict."


def test_patch_project_partial_object(client):
    _make_project(client)
    r = client.patch("/api/projects/SOLO", json={"name": "Renamed", "master_branch": "trunk"})
    assert r.status_code == 200
    assert r.json()["name"] == "Renamed"
    assert r.json()["master_branch"] == "trunk"


def test_patch_project_unknown_field_400(client):
    _make_project(client)
    r = client.patch("/api/projects/SOLO", json={"field": "seq_counter", "value": "9"})
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "validation"


def test_patch_project_non_string_field_does_not_500(client):
    # {"field": <array/object>, "value": ...} must not crash with a bare 500.
    _make_project(client)
    for bad in ([("a", "b")], {"k": 1}):
        r = client.patch("/api/projects/SOLO", json={"field": bad, "value": "x"})
        assert r.status_code == 400, r.text
        assert r.json()["error"]["code"] == "validation"


# --- tickets ----------------------------------------------------------------


def test_create_ticket(client):
    _make_project(client)
    r = client.post("/api/tickets", json={"project": "SOLO", "title": "Hello"})
    assert r.status_code == 201
    body = r.json()
    assert body["id"] == "SOLO-1"
    assert body["state"] == "backlog"
    assert body["assignee"] == "unassigned"
    assert body["pr"] is None
    assert body["session"] is None
    assert body["activity"][0]["kind"] == "created"


def test_create_ticket_missing_title_422(client):
    _make_project(client)
    r = client.post("/api/tickets", json={"project": "SOLO"})
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "validation"


def test_create_ticket_unknown_project_404(client):
    r = client.post("/api/tickets", json={"project": "NOPE", "title": "x"})
    assert r.status_code == 404


def test_list_and_filter_tickets(client):
    _make_project(client)
    client.post("/api/tickets", json={"project": "SOLO", "title": "a", "assignee": "claude"})
    client.post("/api/tickets", json={"project": "SOLO", "title": "b", "state": "todo"})
    r = client.get("/api/tickets?project=SOLO")
    assert len(r.json()["tickets"]) == 2
    r = client.get("/api/tickets?project=SOLO&assignee=claude")
    assert len(r.json()["tickets"]) == 1
    # summary shape
    t = r.json()["tickets"][0]
    assert set(["id", "title", "state", "assignee", "comment_count"]).issubset(t.keys())


def test_edit_ticket(client):
    _make_project(client)
    client.post("/api/tickets", json={"project": "SOLO", "title": "Old"})
    r = client.patch("/api/tickets/SOLO-1", json={"title": "New", "description": "body"})
    assert r.status_code == 200
    assert r.json()["title"] == "New"
    assert r.json()["description"] == "body"


def test_comment_ticket(client):
    _make_project(client)
    client.post("/api/tickets", json={"project": "SOLO", "title": "x"})
    r = client.post("/api/tickets/SOLO-1/comments", json={"body": "a note"})
    assert r.status_code == 201
    assert r.json()["kind"] == "comment"
    full = client.get("/api/tickets/SOLO-1").json()
    assert full["comments"][0]["body"] == "a note"
    assert full["comments"][0]["author"] == "human"


def test_comment_with_agent_attribution(client):
    _make_project(client)
    client.post("/api/tickets", json={"project": "SOLO", "title": "x"})
    r = client.post(
        "/api/tickets/SOLO-1/comments",
        json={"body": "from the bot"},
        headers={"X-SoloPM-Actor": "claude"},
    )
    assert r.status_code == 201
    assert r.json()["actor"] == "claude"


def test_invalid_actor_header_400(client):
    _make_project(client)
    client.post("/api/tickets", json={"project": "SOLO", "title": "x"})
    r = client.post(
        "/api/tickets/SOLO-1/comments",
        json={"body": "hi"},
        headers={"X-SoloPM-Actor": "robot"},
    )
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "validation"


def test_move_ticket_and_actor_rules(client):
    _make_project(client)
    client.post("/api/tickets", json={"project": "SOLO", "title": "x"})
    assert client.post("/api/tickets/SOLO-1/move", json={"state": "todo"}).status_code == 200
    assert (
        client.post("/api/tickets/SOLO-1/move", json={"state": "in-progress"}).status_code == 200
    )
    # agent self-transition to ai review
    r = client.post(
        "/api/tickets/SOLO-1/move",
        json={"state": "in-ai-review"},
        headers={"X-SoloPM-Actor": "claude"},
    )
    assert r.status_code == 200
    client.post(
        "/api/tickets/SOLO-1/move",
        json={"state": "in-human-review"},
        headers={"X-SoloPM-Actor": "codex"},
    )
    # agent cannot close
    r = client.post(
        "/api/tickets/SOLO-1/move",
        json={"state": "done"},
        headers={"X-SoloPM-Actor": "claude"},
    )
    assert r.status_code == 403
    assert r.json()["error"]["code"] == "forbidden_transition"
    # human can close
    r = client.post("/api/tickets/SOLO-1/move", json={"state": "done"})
    assert r.status_code == 200
    assert r.json()["state"] == "done"


def test_illegal_transition_409(client):
    _make_project(client)
    client.post("/api/tickets", json={"project": "SOLO", "title": "x"})
    r = client.post("/api/tickets/SOLO-1/move", json={"state": "done"})
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "invalid_transition"


def test_assign_ticket(client):
    _make_project(client)
    client.post("/api/tickets", json={"project": "SOLO", "title": "x"})
    r = client.post("/api/tickets/SOLO-1/assign", json={"assignee": "claude"})
    assert r.status_code == 200
    assert r.json()["assignee"] == "claude"


def test_move_records_branch(client):
    # Recording the branch on the in-ai-review self-transition (no GitHub in tests).
    _make_project(client)
    client.post("/api/tickets", json={"project": "SOLO", "title": "x"})
    client.post("/api/tickets/SOLO-1/move", json={"state": "in-progress"})
    r = client.post(
        "/api/tickets/SOLO-1/move",
        json={"state": "in-ai-review", "branch": "solo-1-x"},
        headers={"X-SoloPM-Actor": "claude"},
    )
    assert r.status_code == 200
    assert r.json()["branch"] == "solo-1-x"
    assert r.json()["pr"] is None  # automation off in tests


def test_reorder_endpoint(client):
    _make_project(client)
    for t in ("a", "b", "c"):
        client.post("/api/tickets", json={"project": "SOLO", "title": t})
    r = client.post("/api/tickets/SOLO-3/reorder", json={"after": None})  # to top
    assert r.status_code == 200
    # The board sorts a column by the (internal) position; verify via a re-list + move.
    r2 = client.post("/api/tickets/SOLO-1/reorder", json={"after": "SOLO-3"})
    assert r2.status_code == 200


def test_reorder_cross_state_400(client):
    _make_project(client)
    client.post("/api/tickets", json={"project": "SOLO", "title": "a"})  # backlog
    client.post("/api/tickets", json={"project": "SOLO", "title": "b", "state": "todo"})
    r = client.post("/api/tickets/SOLO-1/reorder", json={"after": "SOLO-2"})
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "validation"


def _todo_ids(client):
    return [t["id"] for t in client.get("/api/tickets?project=SOLO&state=todo").json()["tickets"]]


def _seed_two_todo_one_backlog(client):
    _make_project(client)
    for t in ("a", "b"):
        client.post("/api/tickets", json={"project": "SOLO", "title": t, "state": "todo"})
    client.post("/api/tickets", json={"project": "SOLO", "title": "c"})  # SOLO-3 backlog


def test_move_with_after_positions_in_target_column(client):
    _seed_two_todo_one_backlog(client)
    r = client.post("/api/tickets/SOLO-3/move", json={"state": "todo", "after": "SOLO-1"})
    assert r.status_code == 200
    assert _todo_ids(client) == ["SOLO-1", "SOLO-3", "SOLO-2"]


def test_move_with_after_null_goes_to_top(client):
    _seed_two_todo_one_backlog(client)
    client.post("/api/tickets/SOLO-3/move", json={"state": "todo", "after": None})
    assert _todo_ids(client) == ["SOLO-3", "SOLO-1", "SOLO-2"]


def test_move_without_after_goes_to_bottom(client):
    _seed_two_todo_one_backlog(client)
    client.post("/api/tickets/SOLO-3/move", json={"state": "todo"})
    assert _todo_ids(client) == ["SOLO-1", "SOLO-2", "SOLO-3"]


def test_move_after_wrong_column_400(client):
    _seed_two_todo_one_backlog(client)
    # SOLO-2 is in todo; move SOLO-3 to in-progress after SOLO-2 (wrong column) -> 400
    r = client.post("/api/tickets/SOLO-3/move", json={"state": "in-progress", "after": "SOLO-2"})
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "validation"


def test_reorder_unknown_after_404(client):
    _make_project(client)
    client.post("/api/tickets", json={"project": "SOLO", "title": "a"})
    r = client.post("/api/tickets/SOLO-1/reorder", json={"after": "SOLO-99"})
    assert r.status_code == 404


def test_trusted_host_rejects_foreign_host(tmp_path):
    # With the default loopback allow-list, a foreign Host header (DNS-rebinding) is 400.
    store = Store(tmp_path / "solopm.db")
    store.init()
    app = create_app(Service(store))  # default allowed_hosts = loopback
    c = TestClient(app)
    assert c.get("/api/health", headers={"host": "evil.example.com"}).status_code == 400
    assert c.get("/api/health", headers={"host": "127.0.0.1"}).status_code == 200
    assert c.get("/api/health", headers={"host": "localhost:8787"}).status_code == 200
