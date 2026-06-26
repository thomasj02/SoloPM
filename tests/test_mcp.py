"""Tests for the MCP server: the tool logic, and tool registration on the FastMCP app."""

import asyncio

from solopm.mcp.tools import SoloPMTools


def tools_for(service):
    return SoloPMTools(service, agent="claude")


# --- tool logic (SoloPMTools, no MCP plumbing) ------------------------------


def test_list_projects(service, project):
    out = tools_for(service).list_projects()
    assert [p["key"] for p in out["projects"]] == ["SOLO"]


def test_workflow_info(service):
    info = tools_for(service).workflow_info()
    assert "backlog" in info["states"]
    assert info["transitions"]["in-human-review"] == ["in-progress", "done", "cancelled"]
    assert "done" in info["rules"].lower()


def test_create_and_show_ticket_attributes_agent(service, project):
    t = tools_for(service)
    created = t.create_ticket(project="SOLO", title="From MCP", description="body")
    assert created["id"] == "SOLO-1"
    assert created["assignee"] == "unassigned"
    # creation is attributed to the agent
    assert created["activity"][0]["actor"] == "claude"

    shown = t.show_ticket("SOLO-1")
    assert shown["title"] == "From MCP"


def test_comment_and_move_and_assign(service, project):
    t = tools_for(service)
    t.create_ticket(project="SOLO", title="x")
    a = t.comment_ticket("SOLO-1", "working on it")
    assert a["kind"] == "comment"
    assert a["actor"] == "claude"

    assigned = t.assign_ticket("SOLO-1", "claude")
    assert assigned["assignee"] == "claude"

    moved = t.move_ticket("SOLO-1", "todo")
    assert moved["state"] == "todo"


def test_list_tickets_filters(service, project):
    t = tools_for(service)
    t.create_ticket(project="SOLO", title="a", assignee="claude")
    t.create_ticket(project="SOLO", title="b")
    assert len(t.list_tickets(project="SOLO")["tickets"]) == 2
    assert len(t.list_tickets(assignee="claude")["tickets"]) == 1


def test_errors_returned_as_structured_dict(service, project):
    t = tools_for(service)
    # unknown ticket -> not_found error dict (not an exception)
    out = t.show_ticket("SOLO-999")
    assert out["error"]["code"] == "not_found"

    # agent cannot move a ticket to done -> forbidden_transition error dict
    t.create_ticket(project="SOLO", title="x")
    t.move_ticket("SOLO-1", "in-progress")
    t.move_ticket("SOLO-1", "in-ai-review")
    t.move_ticket("SOLO-1", "in-human-review")
    out = t.move_ticket("SOLO-1", "done")
    assert out["error"]["code"] == "forbidden_transition"


def test_agent_cannot_create_in_done(service, project):
    out = tools_for(service).create_ticket(project="SOLO", title="x", state="done")
    assert out["error"]["code"] == "forbidden_transition"


def test_submit_review_tool(service, project):
    t = tools_for(service)
    t.create_ticket(project="SOLO", title="x")
    t.move_ticket("SOLO-1", "in-progress")
    t.move_ticket("SOLO-1", "in-ai-review")
    out = t.submit_review("SOLO-1", "pass", comment="looks good")
    assert out["state"] == "in-human-review"
    # wrong-state review is returned as a structured error, not raised
    t.create_ticket(project="SOLO", title="y")  # SOLO-2, backlog
    err = t.submit_review("SOLO-2", "pass")
    assert err["error"]["code"] == "validation"


# --- FastMCP registration ---------------------------------------------------


def test_build_server_registers_expected_tools(service, project):
    from solopm.mcp.server import build_server

    mcp = build_server(service, agent="claude")
    names = {t.name for t in asyncio.run(mcp.list_tools())}
    assert {
        "list_projects",
        "workflow_info",
        "list_tickets",
        "show_ticket",
        "create_ticket",
        "edit_ticket",
        "comment_ticket",
        "move_ticket",
        "assign_ticket",
        "add_criterion",
        "check_criterion",
        "edit_criterion",
        "remove_criterion",
        "radar",
    } <= names


def test_build_server_tool_invocation(service, project):
    import json

    from solopm.mcp.server import build_server

    mcp = build_server(service, agent="claude")
    # FastMCP.call_tool returns a list of content blocks; the tool's dict is JSON in the
    # first TextContent block.
    blocks = asyncio.run(mcp.call_tool("create_ticket", {"project": "SOLO", "title": "via mcp"}))
    payload = json.loads(blocks[0].text)
    assert payload["id"] == "SOLO-1"
    assert payload["activity"][0]["actor"] == "claude"  # attributed to the agent


def test_radar_tool(service, project):
    assert tools_for(service).radar() == {"overlaps": []}  # service fixture has no github


def test_criteria_via_mcp_tools(service, project):
    t = tools_for(service)
    t.create_ticket(project="SOLO", title="x")
    cid = t.add_criterion("SOLO-1", "tests pass")["acceptance_criteria"][0]["id"]
    assert t.check_criterion("SOLO-1", cid)["acceptance_criteria"][0]["done"] is True
    # criteria surface in show_ticket
    assert t.show_ticket("SOLO-1")["acceptance_criteria"][0]["id"] == cid
    assert t.remove_criterion("SOLO-1", cid)["acceptance_criteria"] == []
