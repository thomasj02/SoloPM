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


def test_state_age_present_in_list_and_show(service, project):
    """SOLO-13: agents reason about staleness from list/show JSON."""
    t = tools_for(service)
    created = t.create_ticket(project="SOLO", title="a")

    summary = t.list_tickets(project="SOLO")["tickets"][0]
    assert summary["state_entered_at"] == created["created_at"]
    assert summary["time_in_state_seconds"] >= 0

    shown = t.show_ticket("SOLO-1")
    assert shown["state_entered_at"] == created["created_at"]
    assert shown["time_in_state_seconds"] >= 0


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
        "list_review_memory",
        "add_review_memory",
        "update_review_memory",
        "review_prompt",
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


def test_review_memory_via_mcp(service, project):
    t = tools_for(service)
    item = t.add_review_memory("SOLO", "check Y")
    assert item["status"] == "active"
    assert "check Y" in t.review_prompt("SOLO")["prompt"]
    assert any(i["id"] == item["id"] for i in t.list_review_memory("SOLO")["items"])
    assert t.update_review_memory("SOLO", item["id"], status="retired")["status"] == "retired"


def test_radar_tool(service, project):
    assert tools_for(service).radar() == {"overlaps": [], "skipped": []}  # service fixture has no github


def test_graph_tool(service, project):
    t = tools_for(service)
    t.create_ticket(project="SOLO", title="a")
    t.create_ticket(project="SOLO", title="b")
    t.link_ticket("SOLO-1", "blocks", "SOLO-2")
    g = t.graph(project="SOLO")
    assert {n["id"] for n in g["nodes"]} == {"SOLO-1", "SOLO-2"}
    assert g["edges"][0] == {"from": "SOLO-1", "to": "SOLO-2", "type": "blocks"}
    # structured error for a bad scope, not an exception
    assert t.graph(project="NOPE")["error"]["code"] == "not_found"


def test_graph_tool_registered(service, project):
    from solopm.mcp.server import build_server

    mcp = build_server(service, agent="claude")
    names = {tool.name for tool in asyncio.run(mcp.list_tools())}
    assert "graph" in names


def test_links_via_mcp_tools(service, project):
    t = tools_for(service)
    t.create_ticket(project="SOLO", title="a")
    t.create_ticket(project="SOLO", title="b")
    out = t.link_ticket("SOLO-1", "blocks", "SOLO-2")
    assert out["relations"][0]["key"] == "blocks"
    assert out["relations"][0]["ticket"]["id"] == "SOLO-2"
    # inverse + attribution surface in show_ticket
    shown = t.show_ticket("SOLO-2")
    assert shown["relations"][0]["key"] == "blocked_by"
    assert any(a["kind"] == "link" and a["actor"] == "claude" for a in shown["activity"])
    # unlink round-trips
    assert t.unlink_ticket("SOLO-1", "SOLO-2")["relations"] == []


def test_link_errors_returned_as_structured_dict(service, project):
    t = tools_for(service)
    t.create_ticket(project="SOLO", title="a")
    assert t.link_ticket("SOLO-1", "blocks", "SOLO-1")["error"]["code"] == "validation"
    assert t.link_ticket("SOLO-1", "blocks", "SOLO-999")["error"]["code"] == "not_found"


def test_link_tools_registered(service, project):
    from solopm.mcp.server import build_server

    mcp = build_server(service, agent="claude")
    names = {t.name for t in asyncio.run(mcp.list_tools())}
    assert {"link_ticket", "unlink_ticket"} <= names


def test_tag_and_untag_via_mcp(service, project):
    t = tools_for(service)
    t.create_ticket(project="SOLO", title="x")
    out = t.tag_ticket("SOLO-1", ["Bug", "frontend"])
    assert out["tags"] == ["bug", "frontend"]
    # tags surface in show_ticket + the tag activity is attributed to the agent
    shown = t.show_ticket("SOLO-1")
    assert shown["tags"] == ["bug", "frontend"]
    assert any(a["kind"] == "tags" and a["actor"] == "claude" for a in shown["activity"])
    # list filter
    t.create_ticket(project="SOLO", title="y")
    assert {tk["id"] for tk in t.list_tickets(tags=["bug"])["tickets"]} == {"SOLO-1"}
    # untag
    assert t.untag_ticket("SOLO-1", "bug")["tags"] == ["frontend"]
    # invalid tag -> structured error, not an exception
    assert t.tag_ticket("SOLO-1", ["bad tag"])["error"]["code"] == "validation"


def test_tag_tools_registered(service, project):
    from solopm.mcp.server import build_server

    mcp = build_server(service, agent="claude")
    names = {tool.name for tool in asyncio.run(mcp.list_tools())}
    assert {"tag_ticket", "untag_ticket"} <= names


def test_criteria_via_mcp_tools(service, project):
    t = tools_for(service)
    t.create_ticket(project="SOLO", title="x")
    cid = t.add_criterion("SOLO-1", "tests pass")["acceptance_criteria"][0]["id"]
    assert t.check_criterion("SOLO-1", cid)["acceptance_criteria"][0]["done"] is True
    # criteria surface in show_ticket
    assert t.show_ticket("SOLO-1")["acceptance_criteria"][0]["id"] == cid
    assert t.remove_criterion("SOLO-1", cid)["acceptance_criteria"] == []


# --- project management (SOLO-20) -------------------------------------------


def test_create_project_via_mcp(service):
    t = tools_for(service)
    created = t.create_project(key="BLOG", name="Blog", repo="/code/blog", master="trunk")
    assert created["key"] == "BLOG"
    assert created["name"] == "Blog"
    assert created["repo"] == "/code/blog"
    assert created["master_branch"] == "trunk"
    # surfaces in list_projects
    assert "BLOG" in {p["key"] for p in t.list_projects()["projects"]}


def test_create_project_lowercase_key_normalized(service):
    created = tools_for(service).create_project(key="blog", name="Blog")
    assert created["key"] == "BLOG"


def test_create_project_duplicate_is_structured_error(service, project):
    out = tools_for(service).create_project(key="SOLO", name="Again")
    assert out["error"]["code"] == "duplicate"


def test_create_project_invalid_key_is_structured_error(service):
    out = tools_for(service).create_project(key="9bad", name="X")
    assert out["error"]["code"] == "validation"


def test_edit_project_via_mcp(service, project):
    t = tools_for(service)
    edited = t.edit_project("SOLO", name="Renamed", review_prompt="Be strict.")
    assert edited["name"] == "Renamed"
    assert edited["review_prompt"] == "Be strict."
    # persisted
    assert t.list_projects()["projects"][0]["name"] == "Renamed"


def test_edit_project_no_fields_is_structured_error(service, project):
    out = tools_for(service).edit_project("SOLO")
    assert out["error"]["code"] == "validation"


def test_edit_project_preserves_omitted_fields(service, project):
    """A partial edit (None = omitted) must leave untouched fields alone — guards the
    None-omission filter so a regression can't silently clobber e.g. repo."""
    t = tools_for(service)
    # The `project` fixture has repo="/tmp/solopm", master="main".
    edited = t.edit_project("SOLO", review_prompt="Be strict.")
    assert edited["review_prompt"] == "Be strict."
    assert edited["repo"] == "/tmp/solopm"  # not nulled out
    assert edited["master_branch"] == "main"
    assert edited["default_reviewer"] == "codex"


def test_edit_unknown_project_is_structured_error(service):
    out = tools_for(service).edit_project("NOPE", name="x")
    assert out["error"]["code"] == "not_found"


def test_delete_empty_project_via_mcp(service, project):
    t = tools_for(service)
    out = t.delete_project("SOLO")
    assert out == {"key": "SOLO", "deleted": True, "tickets_deleted": 0}
    assert t.list_projects()["projects"] == []


def test_delete_nonempty_project_refused_without_force(service, project):
    t = tools_for(service)
    t.create_ticket(project="SOLO", title="x")
    out = t.delete_project("SOLO")
    assert out["error"]["code"] == "validation"
    # still there
    assert {p["key"] for p in t.list_projects()["projects"]} == {"SOLO"}


def test_delete_nonempty_project_with_force(service, project):
    t = tools_for(service)
    t.create_ticket(project="SOLO", title="x")
    out = t.delete_project("SOLO", force=True)
    assert out["deleted"] is True
    assert out["tickets_deleted"] == 1
    assert t.list_projects()["projects"] == []


def test_delete_unknown_project_is_structured_error(service):
    out = tools_for(service).delete_project("NOPE")
    assert out["error"]["code"] == "not_found"


def test_prune_merged_branches_via_mcp(service, project):
    t = tools_for(service)
    # The service fixture has no GitHub client, so prune degrades to an empty result — but the
    # plumbing (and the apply passthrough) is exercised end-to-end.
    assert t.prune_merged_branches("SOLO") == {
        "project": "SOLO", "applied": False, "pruned": [], "skipped": []
    }
    assert t.prune_merged_branches("SOLO", apply=True)["applied"] is True
    assert t.prune_merged_branches("NOPE")["error"]["code"] == "not_found"


def test_prune_tool_registered(service, project):
    from solopm.mcp.server import build_server

    mcp = build_server(service, agent="claude")
    names = {tool.name for tool in asyncio.run(mcp.list_tools())}
    assert "prune_merged_branches" in names


def test_project_management_tools_registered(service):
    from solopm.mcp.server import build_server

    mcp = build_server(service, agent="claude")
    names = {tool.name for tool in asyncio.run(mcp.list_tools())}
    assert {"create_project", "edit_project", "delete_project"} <= names


def test_project_management_tool_invocation(service):
    import json

    from solopm.mcp.server import build_server

    mcp = build_server(service, agent="claude")
    blocks = asyncio.run(
        mcp.call_tool("create_project", {"key": "BLOG", "name": "Blog"})
    )
    payload = json.loads(blocks[0].text)
    assert payload["key"] == "BLOG"
    blocks = asyncio.run(mcp.call_tool("delete_project", {"key": "BLOG"}))
    payload = json.loads(blocks[0].text)
    assert payload == {"key": "BLOG", "deleted": True, "tickets_deleted": 0}


# --- within-column reorder (SOLO-25) -----------------------------------------


def _column(t, state, project="SOLO"):
    """Ticket ids in a column, in the order agents see them (list_tickets is
    position-sorted, so the MCP output order IS the board order)."""
    return [tk["id"] for tk in t.list_tickets(project=project, state=state)["tickets"]]


def test_reorder_ticket_to_top_via_mcp(service, project):
    t = tools_for(service)
    for name in ("a", "b", "c"):
        t.create_ticket(project="SOLO", title=name)
    out = t.reorder_ticket("SOLO-3")  # after omitted -> top of column
    assert out["id"] == "SOLO-3"
    assert _column(t, "backlog") == ["SOLO-3", "SOLO-1", "SOLO-2"]


def test_reorder_ticket_after_specific_via_mcp(service, project):
    t = tools_for(service)
    for name in ("a", "b", "c"):
        t.create_ticket(project="SOLO", title=name)
    t.reorder_ticket("SOLO-1", after="SOLO-2")
    assert _column(t, "backlog") == ["SOLO-2", "SOLO-1", "SOLO-3"]


def test_reorder_errors_returned_as_structured_dict(service, project):
    t = tools_for(service)
    t.create_ticket(project="SOLO", title="a")
    t.create_ticket(project="SOLO", title="b", state="todo")
    assert t.reorder_ticket("SOLO-1", after="SOLO-999")["error"]["code"] == "not_found"
    # `after` must live in the same column
    assert t.reorder_ticket("SOLO-1", after="SOLO-2")["error"]["code"] == "validation"


def test_move_ticket_with_after_positions_in_target_column(service, project):
    t = tools_for(service)
    t.create_ticket(project="SOLO", title="x", state="todo")  # SOLO-1
    t.create_ticket(project="SOLO", title="y", state="todo")  # SOLO-2
    t.create_ticket(project="SOLO", title="z")  # SOLO-3, backlog
    moved = t.move_ticket("SOLO-3", "todo", after="SOLO-1")
    assert moved["state"] == "todo"
    assert _column(t, "todo") == ["SOLO-1", "SOLO-3", "SOLO-2"]


def test_move_ticket_without_after_still_lands_at_bottom(service, project):
    t = tools_for(service)
    t.create_ticket(project="SOLO", title="x", state="todo")
    t.create_ticket(project="SOLO", title="y", state="todo")
    t.create_ticket(project="SOLO", title="z")
    t.move_ticket("SOLO-3", "todo")
    assert _column(t, "todo") == ["SOLO-1", "SOLO-2", "SOLO-3"]


def test_move_ticket_same_state_with_after_repositions(service, project):
    """move_ticket must not silently drop an explicit hint when the ticket is already
    in the target state — it repositions instead (and the hint is validated)."""
    t = tools_for(service)
    for name in ("a", "b", "c"):
        t.create_ticket(project="SOLO", title=name)
    t.move_ticket("SOLO-1", "backlog", after="SOLO-2")
    assert _column(t, "backlog") == ["SOLO-2", "SOLO-1", "SOLO-3"]
    assert t.move_ticket("SOLO-1", "backlog", after="SOLO-99")["error"]["code"] == "not_found"


def test_reorder_tool_registered_and_invocable(service, project):
    import json

    from solopm.mcp.server import build_server

    mcp = build_server(service, agent="claude")
    names = {tool.name for tool in asyncio.run(mcp.list_tools())}
    assert "reorder_ticket" in names

    t = tools_for(service)
    t.create_ticket(project="SOLO", title="a")
    t.create_ticket(project="SOLO", title="b")
    blocks = asyncio.run(mcp.call_tool("reorder_ticket", {"ticket_id": "SOLO-2"}))
    assert json.loads(blocks[0].text)["id"] == "SOLO-2"
    assert _column(t, "backlog") == ["SOLO-2", "SOLO-1"]
    asyncio.run(mcp.call_tool("reorder_ticket", {"ticket_id": "SOLO-2", "after": "SOLO-1"}))
    assert _column(t, "backlog") == ["SOLO-1", "SOLO-2"]


def test_move_tool_accepts_after_over_the_wire(service, project):
    from solopm.mcp.server import build_server

    mcp = build_server(service, agent="claude")
    t = tools_for(service)
    t.create_ticket(project="SOLO", title="x", state="todo")  # SOLO-1
    t.create_ticket(project="SOLO", title="y", state="todo")  # SOLO-2
    t.create_ticket(project="SOLO", title="z")  # SOLO-3, backlog
    asyncio.run(
        mcp.call_tool("move_ticket", {"ticket_id": "SOLO-3", "state": "todo", "after": "SOLO-1"})
    )
    assert _column(t, "todo") == ["SOLO-1", "SOLO-3", "SOLO-2"]


def test_explicit_null_after_over_the_wire(service, project):
    """MCP clients may serialize an optional param as an explicit JSON null. That must
    keep meaning "no placement hint" (bottom) for move_ticket — indistinguishable from
    omitted — while for reorder_ticket null/omitted means top. Pins the generated
    schema accepting null (a later `after: str` tightening would break null-sending
    clients while every other test stayed green)."""
    from solopm.mcp.server import build_server

    mcp = build_server(service, agent="claude")
    t = tools_for(service)
    t.create_ticket(project="SOLO", title="x", state="todo")  # SOLO-1
    t.create_ticket(project="SOLO", title="y", state="todo")  # SOLO-2
    t.create_ticket(project="SOLO", title="z")  # SOLO-3, backlog
    asyncio.run(
        mcp.call_tool("move_ticket", {"ticket_id": "SOLO-3", "state": "todo", "after": None})
    )
    assert _column(t, "todo") == ["SOLO-1", "SOLO-2", "SOLO-3"]
    asyncio.run(mcp.call_tool("reorder_ticket", {"ticket_id": "SOLO-3", "after": None}))
    assert _column(t, "todo") == ["SOLO-3", "SOLO-1", "SOLO-2"]
