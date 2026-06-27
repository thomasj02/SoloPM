"""Dependency-graph builder (SOLO-14) — nodes + typed edges over SOLO-10's ticket_links.

Read-only derivation: project / ego-graph scopes, per-type and active-only filters,
canonical edge directions, blocks-cycle detection, and the derived node signals.
"""

import pytest

from solopm.core.errors import NotFoundError, ValidationError


def _mk(service, project="SOLO", title="x", **kw):
    return service.create_ticket(project=project, title=title, **kw)


def _ids(graph):
    return sorted(n["id"] for n in graph["nodes"])


def _edges(graph):
    return {(e["from"], e["to"], e["type"]) for e in graph["edges"]}


def _node(graph, ticket_id):
    return next(n for n in graph["nodes"] if n["id"] == ticket_id)


# --- project scope ----------------------------------------------------------


def test_project_graph_nodes_and_edges(service, project):
    a = _mk(service, title="a")
    b = _mk(service, title="b")
    c = _mk(service, title="c")
    service.link_tickets(a.id, "blocks", b.id, actor="human")
    service.link_tickets(b.id, "blocks", c.id, actor="human")
    g = service.build_graph(project="SOLO")
    assert _ids(g) == ["SOLO-1", "SOLO-2", "SOLO-3"]
    assert _edges(g) == {("SOLO-1", "SOLO-2", "blocks"), ("SOLO-2", "SOLO-3", "blocks")}
    assert g["cycles"] == []
    assert g["truncated"] is False
    assert g["scope"]["project"] == "SOLO"


def test_project_graph_excludes_isolated_tickets(service, project):
    a = _mk(service, title="a")
    b = _mk(service, title="b")
    _mk(service, title="lonely")  # SOLO-3 — no relations
    service.link_tickets(a.id, "blocks", b.id, actor="human")
    g = service.build_graph(project="SOLO")
    assert "SOLO-3" not in _ids(g)  # a relationship graph omits unconnected tickets


def test_node_shape_carries_summary_fields(service, project):
    a = _mk(service, title="Alpha", assignee="claude")
    b = _mk(service, title="Beta")
    service.link_tickets(a.id, "blocks", b.id, actor="human")
    n = _node(service.build_graph(project="SOLO"), a.id)
    assert n["project"] == "SOLO"
    assert n["title"] == "Alpha"
    assert n["state"] == "backlog"
    assert n["assignee"] == "claude"
    assert n["blocked"] is False
    assert n["subtickets"] == {"done": 0, "total": 0}


# --- canonical edge direction -----------------------------------------------


def test_edge_directions_match_canonical_rules(service, project):
    blocker = _mk(service, title="blocker")
    blocked = _mk(service, title="blocked")
    child = _mk(service, title="child")
    parent = _mk(service, title="parent")
    service.link_tickets(blocker.id, "blocks", blocked.id, actor="human")
    service.link_tickets(child.id, "parent", parent.id, actor="human")
    edges = _edges(service.build_graph(project="SOLO"))
    # blocks: blocker -> blocked; parent: child -> parent (canonical storage).
    assert (blocker.id, blocked.id, "blocks") in edges
    assert (child.id, parent.id, "parent") in edges


# --- cross-project ----------------------------------------------------------


def test_cross_project_neighbor_included(service):
    service.add_project(key="SOLO", name="SoloPM")
    service.add_project(key="BLOG", name="Blog")
    a = service.create_ticket(project="SOLO", title="a")
    b = service.create_ticket(project="BLOG", title="b")
    service.link_tickets(a.id, "related", b.id, actor="human")
    g = service.build_graph(project="SOLO")
    assert "BLOG-1" in _ids(g)  # the cross-project neighbor is pulled in
    # related is stored in canonical (project, seq) order — BLOG-1 sorts before SOLO-1.
    assert _edges(g) == {("BLOG-1", "SOLO-1", "related")}


# --- ego-graph --------------------------------------------------------------


def _chain(service):
    # a -blocks-> b -blocks-> c -blocks-> d
    a = _mk(service, title="a")
    b = _mk(service, title="b")
    c = _mk(service, title="c")
    d = _mk(service, title="d")
    service.link_tickets(a.id, "blocks", b.id, actor="human")
    service.link_tickets(b.id, "blocks", c.id, actor="human")
    service.link_tickets(c.id, "blocks", d.id, actor="human")
    return a, b, c, d


def test_ego_graph_depth_one(service, project):
    a, b, c, d = _chain(service)
    g = service.build_graph(around=b.id, depth=1)
    assert _ids(g) == [a.id, b.id, c.id]  # b plus its direct neighbours
    assert _edges(g) == {(a.id, b.id, "blocks"), (b.id, c.id, "blocks")}


def test_ego_graph_depth_two_reaches_further(service, project):
    a, b, c, d = _chain(service)
    g = service.build_graph(around=b.id, depth=2)
    assert _ids(g) == [a.id, b.id, c.id, d.id]


def test_ego_graph_depth_zero_is_just_the_node(service, project):
    a, b, c, d = _chain(service)
    g = service.build_graph(around=b.id, depth=0)
    assert _ids(g) == [b.id]
    assert g["edges"] == []


def test_empty_ego_graph_for_unconnected_ticket(service, project):
    lonely = _mk(service, title="lonely")
    g = service.build_graph(around=lonely.id, depth=3)
    assert _ids(g) == [lonely.id]
    assert g["edges"] == []
    assert g["cycles"] == []


def test_ego_graph_normalizes_ticket_id(service, project):
    a, b, c, d = _chain(service)
    g = service.build_graph(around="solo-2", depth=1)
    assert b.id in _ids(g)


# --- filters ----------------------------------------------------------------


def test_type_filter_keeps_only_selected_edges(service, project):
    a = _mk(service, title="a")
    b = _mk(service, title="b")
    c = _mk(service, title="c")
    service.link_tickets(a.id, "blocks", b.id, actor="human")
    service.link_tickets(a.id, "related", c.id, actor="human")
    g = service.build_graph(project="SOLO", types=["blocks"])
    assert _edges(g) == {(a.id, b.id, "blocks")}
    assert "SOLO-3" not in _ids(g)  # c only had a (filtered-out) related edge


def test_active_only_prunes_orphaned_cross_project_neighbor(service):
    # A done SOLO ticket related to an open BLOG ticket: active_only drops the SOLO node, and
    # the BLOG neighbour — now edgeless and outside the requested project — must not linger.
    service.add_project(key="SOLO", name="SoloPM")
    service.add_project(key="BLOG", name="Blog")
    done = service.create_ticket(project="SOLO", title="done", state="done", actor="human")
    other = service.create_ticket(project="BLOG", title="open")
    service.link_tickets(done.id, "related", other.id, actor="human")
    g = service.build_graph(project="SOLO", active_only=True)
    assert g["nodes"] == []
    assert g["edges"] == []


def test_ego_graph_node_cap_keeps_root_and_nearest(service, project):
    a, b, c, d = _chain(service)  # SOLO-1..4: a→b→c→d
    g = service.build_graph(around=d.id, depth=3, limit=2)
    assert g["truncated"] is True
    ids = _ids(g)
    assert len(ids) == 2
    assert d.id in ids  # the ego root survives the cap…
    assert c.id in ids  # …along with its nearest neighbour
    assert a.id not in ids  # the farthest node is dropped


def test_active_only_drops_finished_nodes(service, project):
    a = _mk(service, title="a")
    done = service.create_ticket(project="SOLO", title="done one", state="done", actor="human")
    service.link_tickets(a.id, "blocks", done.id, actor="human")
    g = service.build_graph(project="SOLO", active_only=True)
    assert _ids(g) == [a.id]  # the done node and its edge are dropped
    assert g["edges"] == []


# --- derived node signals ---------------------------------------------------


def test_node_blocked_flag(service, project):
    blocker = _mk(service, title="blocker")
    blocked = _mk(service, title="blocked")
    service.link_tickets(blocker.id, "blocks", blocked.id, actor="human")
    g = service.build_graph(project="SOLO")
    assert _node(g, blocked.id)["blocked"] is True
    assert _node(g, blocker.id)["blocked"] is False


def test_node_subticket_rollup(service, project):
    parent = _mk(service, title="parent")
    done_child = service.create_ticket(project="SOLO", title="c1", state="done", actor="human")
    open_child = _mk(service, title="c2")
    service.link_tickets(done_child.id, "parent", parent.id, actor="human")
    service.link_tickets(open_child.id, "parent", parent.id, actor="human")
    assert _node(service.build_graph(project="SOLO"), parent.id)["subtickets"] == {
        "done": 1,
        "total": 2,
    }


# --- cycles -----------------------------------------------------------------


def test_blocks_cycle_is_detected_not_fatal(service, project):
    a = _mk(service, title="a")
    b = _mk(service, title="b")
    c = _mk(service, title="c")
    service.link_tickets(a.id, "blocks", b.id, actor="human")
    service.link_tickets(b.id, "blocks", c.id, actor="human")
    service.link_tickets(c.id, "blocks", a.id, actor="human")  # closes the blocks loop
    g = service.build_graph(project="SOLO")
    assert g["cycles"], "the blocks cycle should be reported"
    assert set(g["cycles"][0]) == {a.id, b.id, c.id}
    assert len(g["edges"]) == 3  # build did not crash or drop edges


def test_no_false_cycle_on_dag(service, project):
    a, b, c, d = _chain(service)
    assert service.build_graph(project="SOLO")["cycles"] == []


# --- validation -------------------------------------------------------------


def test_unknown_project_raises(service):
    with pytest.raises(NotFoundError):
        service.build_graph(project="NOPE")


def test_unknown_around_raises(service, project):
    with pytest.raises(NotFoundError):
        service.build_graph(around="SOLO-999")


def test_unknown_type_raises(service, project):
    _mk(service, title="a")
    with pytest.raises(ValidationError):
        service.build_graph(project="SOLO", types=["bogus"])


def test_negative_depth_raises(service, project):
    a = _mk(service, title="a")
    with pytest.raises(ValidationError):
        service.build_graph(around=a.id, depth=-1)
