"""HTTP-backed MCP mode (SOLO-26): HttpSoloPMTools drives the backend over the HTTP API.

The adapter must be observably identical to the in-process SoloPMTools: same public
surface, same result shapes, same ``{"error": {code, message}}`` failures, same actor
attribution — proven by driving the same operation sequence through both and comparing.
"""

import inspect
import re

import httpx
import pytest
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from solopm.cli import main as cli_main
from solopm.cli.client import Api
from solopm.core.service import Service
from solopm.core.store import Store
from solopm.mcp.http_tools import HttpSoloPMTools
from solopm.mcp.tools import SoloPMTools
from solopm.server.app import create_app


def _http_tools(tmp_path, name="http.db", agent="claude"):
    store = Store(tmp_path / name)
    store.init()
    app = create_app(Service(store), allowed_hosts=["*"])
    api = Api("http://test", agent=agent, client=TestClient(app))
    return HttpSoloPMTools(api)


@pytest.fixture
def http_tools(tmp_path):
    return _http_tools(tmp_path)


# --- surface parity ----------------------------------------------------------


def _public_methods(cls) -> dict:
    return {
        name: fn
        for name, fn in inspect.getmembers(cls, inspect.isfunction)
        if not name.startswith("_")
    }


def test_http_tools_cover_the_full_tool_surface():
    """Every tool SoloPMTools offers must exist on HttpSoloPMTools with the same
    signature — this is the drift guard for future tools."""
    local = _public_methods(SoloPMTools)
    remote = _public_methods(HttpSoloPMTools)
    missing = set(local) - set(remote)
    assert not missing, f"HttpSoloPMTools is missing tools: {sorted(missing)}"
    for name, fn in local.items():
        assert inspect.signature(remote[name]) == inspect.signature(fn), name


# --- basic operation + attribution (c2, c4) ----------------------------------


def test_create_and_show_ticket_attributes_agent(http_tools):
    http_tools.create_project(key="SOLO", name="SoloPM")
    created = http_tools.create_ticket(project="SOLO", title="From HTTP MCP")
    assert created["id"] == "SOLO-1"
    assert created["activity"][0]["actor"] == "claude"

    shown = http_tools.show_ticket("SOLO-1")
    assert shown["title"] == "From HTTP MCP"


def test_comment_is_attributed_activity(http_tools):
    http_tools.create_project(key="SOLO", name="SoloPM")
    http_tools.create_ticket(project="SOLO", title="x")
    a = http_tools.comment_ticket("SOLO-1", "working on it")
    assert a["kind"] == "comment"
    assert a["actor"] == "claude"


def test_create_project_carries_full_config(http_tools):
    """create_project must be atomic over HTTP — all config fields in one POST."""
    p = http_tools.create_project(
        key="SOLO",
        name="SoloPM",
        repo="/tmp/solopm",
        master="dev",
        branch_convention="{key}_{seq}",
        default_implementer="codex",
        default_reviewer="claude",
        review_prompt="Be ruthless.",
    )
    assert p["master_branch"] == "dev"
    assert p["branch_convention"] == "{key}_{seq}"
    assert p["default_implementer"] == "codex"
    assert p["default_reviewer"] == "claude"
    assert p["review_prompt"] == "Be ruthless."


# --- the omitted-`after` placement trap ---------------------------------------


def test_move_with_no_after_lands_at_bottom_and_reorder_none_goes_top(http_tools):
    """The server maps an omitted `after` to bottom-of-column but an explicit null to
    top; the adapter must OMIT the key when after is None (mirroring tools.py), or
    every plain move silently jumps the queue."""
    t = http_tools
    t.create_project(key="SOLO", name="SoloPM")
    t.create_ticket(project="SOLO", title="a")  # SOLO-1
    t.create_ticket(project="SOLO", title="b")  # SOLO-2
    t.create_ticket(project="SOLO", title="c")  # SOLO-3
    t.move_ticket("SOLO-1", "todo")
    t.move_ticket("SOLO-2", "todo")
    t.move_ticket("SOLO-3", "todo")  # omitted after => bottom
    order = [x["id"] for x in t.list_tickets(project="SOLO", state="todo")["tickets"]]
    assert order == ["SOLO-1", "SOLO-2", "SOLO-3"]

    t.reorder_ticket("SOLO-3", after=None)  # explicit None => top
    order = [x["id"] for x in t.list_tickets(project="SOLO", state="todo")["tickets"]]
    assert order == ["SOLO-3", "SOLO-1", "SOLO-2"]

    t.move_ticket("SOLO-1", "todo", after="SOLO-2")  # explicit after => placed below it
    order = [x["id"] for x in t.list_tickets(project="SOLO", state="todo")["tickets"]]
    assert order == ["SOLO-3", "SOLO-2", "SOLO-1"]


# --- error behavior (c3, c6) ---------------------------------------------------


def test_errors_returned_as_structured_dict(http_tools):
    http_tools.create_project(key="SOLO", name="SoloPM")
    out = http_tools.show_ticket("SOLO-999")
    assert out["error"]["code"] == "not_found"

    http_tools.create_ticket(project="SOLO", title="x")
    http_tools.move_ticket("SOLO-1", "in-progress")
    http_tools.move_ticket("SOLO-1", "in-ai-review")
    http_tools.move_ticket("SOLO-1", "in-human-review")
    out = http_tools.move_ticket("SOLO-1", "done")
    assert out["error"]["code"] == "forbidden_transition"


def test_edit_project_without_fields_fails_before_any_request(tmp_path):
    """tools.py validates this client-side; the adapter must too (message parity)."""

    class Boom(Api):
        def _request(self, *a, **k):  # pragma: no cover - must not be reached
            raise AssertionError("edit_project with no fields must not hit the network")

    t = HttpSoloPMTools(Boom("http://test", agent="claude", client=object()))
    out = t.edit_project("SOLO")
    assert out["error"]["code"] == "validation"


def test_unreachable_backend_is_a_structured_error():
    api = Api("http://127.0.0.1:9", agent="claude")  # nothing listens on port 9
    t = HttpSoloPMTools(api)
    out = t.list_projects()
    assert out["error"]["code"] == "unreachable"
    assert "solopm serve" in out["error"]["message"]


def _mock_tools(handler):
    """HttpSoloPMTools over a canned httpx transport — for hostile-backend behavior."""
    client = httpx.Client(base_url="http://mock", transport=httpx.MockTransport(handler))
    return HttpSoloPMTools(Api("http://mock", agent="claude", client=client))


def test_non_json_success_body_is_a_structured_error():
    """A 200 from something that isn't the SoloPM API (proxy splash page, wrong port)
    must not escape as a raw JSONDecodeError."""
    t = _mock_tools(lambda req: httpx.Response(200, text="<html>welcome</html>"))
    out = t.list_projects()
    assert out["error"]["code"] == "invalid_response"


def test_string_valued_error_payload_is_a_structured_error():
    """A gateway answering {"error": "unauthorized"} (error not a dict) must not raise."""
    t = _mock_tools(lambda req: httpx.Response(502, json={"error": "unauthorized"}))
    out = t.list_projects()
    assert out["error"]["code"] == "http_error"
    assert "unauthorized" in out["error"]["message"]


def test_redirect_is_a_structured_error_not_an_empty_success():
    """A 302 (SSO portal, http->https redirect) must not surface as a successful {}."""
    t = _mock_tools(
        lambda req: httpx.Response(302, headers={"location": "https://sso.corp/login"})
    )
    out = t.list_projects()
    assert out["error"]["code"] == "http_error"
    assert "302" in out["error"]["message"]


def test_path_segment_smuggling_is_neutralized(http_tools):
    """Review-memory m3: a crafted key must not smuggle query params through the path."""
    http_tools.create_project(key="SOLO", name="SoloPM")
    http_tools.create_ticket(project="SOLO", title="x")
    out = http_tools.delete_project("SOLO?force=true")
    assert "error" in out, out
    # the project (and its ticket) must have survived the attempt
    assert [p["key"] for p in http_tools.list_projects()["projects"]] == ["SOLO"]
    assert http_tools.show_ticket("SOLO-1")["id"] == "SOLO-1"


def test_slash_or_empty_path_values_are_validation_errors(http_tools):
    """'/' can't survive the ASGI decode round-trip and '' changes the route — both are
    rejected client-side as domain validation errors, not router-level {"detail": ...}."""
    http_tools.create_project(key="SOLO", name="SoloPM")
    http_tools.create_ticket(project="SOLO", title="x")
    assert http_tools.untag_ticket("SOLO-1", "a/b")["error"]["code"] == "validation"
    assert http_tools.show_ticket("")["error"]["code"] == "validation"
    assert http_tools.delete_project("A/B")["error"]["code"] == "validation"


def test_non_string_review_note_is_validation_in_both_modes(tmp_path):
    """A non-string criteria note must not fork state: HTTP 422s (code 'validation'),
    so in-process must reject it with the same code rather than recording it."""

    def drive(t):
        t.create_project(key="SOLO", name="SoloPM")
        t.create_ticket(project="SOLO", title="x")
        t.add_criterion("SOLO-1", "c")
        t.move_ticket("SOLO-1", "in-progress")
        t.move_ticket("SOLO-1", "in-ai-review")
        return t.submit_review(
            "SOLO-1",
            "pass",
            criteria_results=[{"criterion_id": "c1", "verdict": "pass", "note": 123}],
        )

    store = Store(tmp_path / "note-local.db")
    store.init()
    local = drive(SoloPMTools(Service(store), agent="claude"))
    remote = drive(_http_tools(tmp_path, name="note-remote.db"))
    assert local["error"]["code"] == "validation"
    assert remote["error"]["code"] == "validation"


def test_unknown_agent_is_rejected_by_the_backend(tmp_path):
    t = _http_tools(tmp_path, agent="gemini")
    # project writes are unattributed — the header is ignored there
    assert t.create_project(key="SOLO", name="SoloPM")["key"] == "SOLO"
    # ticket writes resolve the actor — an unknown agent is a structured validation error
    out = t.create_ticket(project="SOLO", title="x")
    assert out["error"]["code"] == "validation"
    assert "gemini" in out["error"]["message"]


# --- full parity drive (c1, c3, c4) -------------------------------------------

_TS = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")


def _scrub(obj):
    """Blank out wall-clock-dependent values; everything else must match exactly."""
    if isinstance(obj, dict):
        return {
            k: (0 if "seconds" in k and isinstance(v, (int, float)) else _scrub(v))
            for k, v in obj.items()
        }
    if isinstance(obj, list):
        return [_scrub(x) for x in obj]
    if isinstance(obj, str) and _TS.match(obj):
        return "<ts>"
    return obj


def _drive(t) -> list:
    """The same scripted session both backends replay: every tool, happy and sad paths."""
    r = []
    r.append(t.create_project(key="PAR", name="Parity", branch_convention="{key}-{seq}"))
    r.append(t.list_projects())
    r.append(t.create_ticket(project="PAR", title="A", description="first"))
    r.append(t.create_ticket(project="PAR", title="B", assignee="claude"))
    r.append(t.create_ticket(project="PAR", title="C"))
    r.append(t.tag_ticket("PAR-1", ["Bug", "tech-debt"]))
    r.append(t.list_tickets(project="PAR", tags=["bug", "tech-debt"]))
    r.append(t.untag_ticket("PAR-1", "BUG"))
    r.append(t.edit_ticket("PAR-1", title="A!", description="edited"))
    r.append(t.comment_ticket("PAR-1", "note"))
    r.append(t.assign_ticket("PAR-1", "claude"))
    r.append(t.move_ticket("PAR-1", "in-progress"))
    r.append(t.move_ticket("PAR-2", "todo"))
    r.append(t.move_ticket("PAR-3", "todo"))
    r.append(t.move_ticket("PAR-2", "todo", after="PAR-3"))
    r.append(t.reorder_ticket("PAR-3", after=None))
    r.append(t.list_tickets(project="PAR", state="todo"))
    r.append(t.add_criterion("PAR-1", "does the thing"))
    r.append(t.check_criterion("PAR-1", "c1", done=True))
    r.append(t.check_criterion("PAR-1", "c1", done=False))
    r.append(t.check_criterion("PAR-1", "c1", done=True))
    r.append(t.edit_criterion("PAR-1", "c1", "does the whole thing"))
    r.append(t.link_ticket("PAR-1", "blocks", "PAR-2"))
    r.append(t.link_ticket("PAR-3", "parent", "PAR-1"))
    r.append(t.graph(project="PAR"))
    r.append(t.graph(around="PAR-1", depth=2, types=["blocks", "parent"]))
    r.append(t.unlink_ticket("PAR-1", "PAR-2", type="blocks"))
    r.append(t.remove_criterion("PAR-1", "c1"))
    r.append(t.add_review_memory("PAR", "Check both bounds", status="active"))
    r.append(t.list_review_memory("PAR"))
    r.append(t.update_review_memory("PAR", "m1", status="retired"))
    r.append(t.review_prompt("PAR", record_hit=True))
    r.append(t.edit_project("PAR", name="Parity!", master_branch="trunk"))
    r.append(t.add_criterion("PAR-1", "wire check"))
    r.append(t.move_ticket("PAR-1", "in-ai-review", branch="PAR-1-a"))
    r.append(
        t.submit_review(
            "PAR-1",
            "fail",
            comment="needs work",
            criteria_results=[{"criterion_id": "c2", "verdict": "fail", "note": "tighten"}],
        )
    )
    r.append(t.move_ticket("PAR-1", "in-ai-review"))
    r.append(t.submit_review("PAR-1", "pass", comment="ok"))
    r.append(t.show_ticket("PAR-1"))
    r.append(t.radar(project="PAR"))
    r.append(t.prune_merged_branches("PAR"))
    r.append(t.workflow_info())
    # sad paths — the error dicts must match byte for byte
    r.append(t.show_ticket("PAR-999"))
    r.append(t.move_ticket("PAR-1", "done"))
    r.append(t.edit_project("PAR"))
    r.append(t.create_ticket(project="PAR", title="bad", state="done"))
    r.append(t.create_project(key="PAR", name="dup"))
    r.append(t.delete_project("PAR"))
    r.append(t.delete_project("PAR", force=True))
    return r


def test_http_and_inprocess_tools_are_observably_identical(tmp_path):
    in_store = Store(tmp_path / "inproc.db")
    in_store.init()
    local = SoloPMTools(Service(in_store), agent="claude")
    remote = _http_tools(tmp_path, name="remote.db")

    local_results = _drive(local)
    remote_results = _drive(remote)

    assert len(local_results) == len(remote_results)
    for i, (a, b) in enumerate(zip(local_results, remote_results)):
        assert _scrub(a) == _scrub(b), f"parity break at step {i}: {a!r} != {b!r}"


# --- build_server accepts a tools backend (c1: old signature untouched) --------


def test_build_server_accepts_tools_override(tmp_path):
    from solopm.mcp.server import build_server

    mcp = build_server(tools=_http_tools(tmp_path))
    import asyncio

    tool_names = {tool.name for tool in asyncio.run(mcp.list_tools())}
    assert "create_ticket" in tool_names and "move_ticket" in tool_names
    # the registered toolset is exactly the in-process one
    store = Store(tmp_path / "cmp.db")
    store.init()
    classic = build_server(Service(store), agent="claude")
    assert tool_names == {tool.name for tool in asyncio.run(classic.list_tools())}


# --- CLI wiring (c2, c5) --------------------------------------------------------

runner = CliRunner()


def test_mcp_url_mode_builds_http_tools_and_never_touches_the_store(monkeypatch):
    captured = {}

    class FakeServer:
        def run(self):
            captured["ran"] = True

    def fake_build_server(service=None, agent="claude", *, tools=None):
        captured["agent"] = agent
        captured["tools"] = tools
        return FakeServer()

    monkeypatch.setattr("solopm.mcp.server.build_server", fake_build_server)
    monkeypatch.setattr(
        "solopm.core.store.Store",
        lambda *a, **k: pytest.fail("URL mode must not open the local store"),
    )
    r = runner.invoke(cli_main.app, ["mcp", "--url", "http://example.test:8787"])
    assert r.exit_code == 0, r.output
    assert captured["ran"]
    assert isinstance(captured["tools"], HttpSoloPMTools)
    assert captured["tools"].api.base_url == "http://example.test:8787"
    assert captured["tools"].api.agent == "claude"


def test_mcp_channel_with_url_is_rejected(monkeypatch):
    monkeypatch.setattr(
        "solopm.core.store.Store",
        lambda *a, **k: pytest.fail("rejected combination must not open the store"),
    )
    r = runner.invoke(cli_main.app, ["mcp", "--url", "http://example.test:8787", "--channel"])
    assert r.exit_code != 0
    assert "channel" in (r.output + str(r.exception or "")).lower()


def test_mcp_url_with_unknown_agent_fails_fast(monkeypatch):
    monkeypatch.setattr(
        "solopm.core.store.Store",
        lambda *a, **k: pytest.fail("must fail before opening anything"),
    )
    r = runner.invoke(
        cli_main.app, ["mcp", "--url", "http://example.test:8787", "--agent", "gemini"]
    )
    assert r.exit_code != 0
    assert "gemini" in (r.output + str(r.exception or ""))


def test_mcp_url_agent_is_normalized_before_sending(monkeypatch):
    """The backend's get_actor strips/lowercases the header — the CLI must send the value
    it validated, or ' CODEX ' passes the fail-fast and then breaks as a header value."""
    captured = {}

    class FakeServer:
        def run(self):
            pass

    def fake_build_server(service=None, agent="claude", *, tools=None):
        captured["tools"] = tools
        return FakeServer()

    monkeypatch.setattr("solopm.mcp.server.build_server", fake_build_server)
    r = runner.invoke(
        cli_main.app, ["mcp", "--url", "http://example.test:8787", "--agent", " CODEX "]
    )
    assert r.exit_code == 0, r.output
    assert captured["tools"].api.agent == "codex"


def test_mcp_empty_url_is_rejected_not_silently_local(monkeypatch):
    """`--url ""` (e.g. an unset shell var) must error, not quietly open the local store."""
    monkeypatch.setattr(
        "solopm.core.store.Store",
        lambda *a, **k: pytest.fail("an empty --url must not fall back to the local store"),
    )
    for empty in ("", "   "):
        r = runner.invoke(cli_main.app, ["mcp", "--url", empty])
        assert r.exit_code != 0
