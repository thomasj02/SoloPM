"""Ticket relationships — blocks/blocked-by, related, duplicate, parent/sub-ticket (SOLO-10).

Canonical storage + derived inverse, validation rules, and the surfacing of relations
(plus the derived ``blocked`` / ``subtickets`` summary fields) through the service.
"""

import pytest

from solopm.core.errors import NotFoundError, ValidationError


# --- helpers ----------------------------------------------------------------


def _relations(ticket):
    return ticket.to_dict()["relations"]


def _by_key(ticket, key):
    """Linked ticket ids for one perspective group key (e.g. 'blocks', 'blocked_by')."""
    return [r["ticket"]["id"] for r in _relations(ticket) if r["key"] == key]


def _mk(service, project="SOLO", title="x", **kw):
    return service.create_ticket(project=project, title=title, **kw)


# --- canonical direction + derived inverse ----------------------------------


def test_blocks_derives_blocked_by_on_the_other_ticket(service, project):
    a = _mk(service, title="blocker")
    b = _mk(service, title="blocked")
    service.link_tickets(a.id, "blocks", b.id, actor="claude")

    a_full = service.get_ticket(a.id)
    b_full = service.get_ticket(b.id)
    # A blocks B; the inverse shows on B as "blocked by A".
    assert _by_key(a_full, "blocks") == [b.id]
    assert _by_key(a_full, "blocked_by") == []
    assert _by_key(b_full, "blocked_by") == [a.id]
    assert _by_key(b_full, "blocks") == []


def test_related_is_symmetric_and_deduped_across_direction(service, project):
    a = _mk(service, title="a")
    b = _mk(service, title="b")
    service.link_tickets(a.id, "related", b.id, actor="human")
    # The reverse-direction add is the same symmetric link → deduped, not a second row.
    service.link_tickets(b.id, "related", a.id, actor="human")

    assert _by_key(service.get_ticket(a.id), "related") == [b.id]
    assert _by_key(service.get_ticket(b.id), "related") == [a.id]
    assert len(service.store.list_links()) == 1


def test_duplicate_derives_duplicated_by(service, project):
    a = _mk(service, title="dup")
    b = _mk(service, title="canonical")
    service.link_tickets(a.id, "duplicate", b.id, actor="human")
    assert _by_key(service.get_ticket(a.id), "duplicate_of") == [b.id]
    assert _by_key(service.get_ticket(b.id), "duplicated_by") == [a.id]


def test_parent_derives_sub_ticket(service, project):
    child = _mk(service, title="child")
    parent = _mk(service, title="parent")
    # "link <child> parent <parent>" sets <child>'s parent to <parent>.
    service.link_tickets(child.id, "parent", parent.id, actor="human")
    assert _by_key(service.get_ticket(child.id), "parent") == [parent.id]
    assert _by_key(service.get_ticket(parent.id), "sub") == [child.id]


def test_relation_carries_other_ticket_title_and_state(service, project):
    a = _mk(service, title="a")
    b = _mk(service, title="Beautiful B")
    service.link_tickets(a.id, "blocks", b.id, actor="human")
    rel = _relations(service.get_ticket(a.id))[0]
    assert rel["ticket"] == {"id": b.id, "title": "Beautiful B", "state": "backlog"}
    assert rel["type"] == "blocks"
    assert rel["created_by"] == "human"
    assert rel["created_at"]


# --- validation -------------------------------------------------------------


def test_self_link_rejected(service, project):
    a = _mk(service, title="a")
    for typ in ("blocks", "related", "duplicate", "parent"):
        with pytest.raises(ValidationError):
            service.link_tickets(a.id, typ, a.id, actor="human")


def test_unknown_relation_type_rejected(service, project):
    a = _mk(service, title="a")
    b = _mk(service, title="b")
    with pytest.raises(ValidationError):
        service.link_tickets(a.id, "supersedes", b.id, actor="human")


def test_unknown_ticket_rejected_either_side(service, project):
    a = _mk(service, title="a")
    with pytest.raises(NotFoundError):
        service.link_tickets(a.id, "blocks", "SOLO-999", actor="human")
    with pytest.raises(NotFoundError):
        service.link_tickets("SOLO-999", "blocks", a.id, actor="human")


def test_duplicate_link_deduped_idempotently(service, project):
    a = _mk(service, title="a")
    b = _mk(service, title="b")
    service.link_tickets(a.id, "blocks", b.id, actor="claude")
    before = [x for x in service.get_ticket(a.id).activity if x.kind == "link"]
    # Re-adding the identical link is an idempotent no-op (no dup row, no new activity).
    service.link_tickets(a.id, "blocks", b.id, actor="claude")
    after_t = service.get_ticket(a.id)
    assert _by_key(after_t, "blocks") == [b.id]
    assert len(service.store.list_links()) == 1
    after = [x for x in after_t.activity if x.kind == "link"]
    assert len(after) == len(before)


def test_opposing_blocks_are_distinct_links(service, project):
    # A blocks B and B blocks A are different (directional) links — both may exist.
    a = _mk(service, title="a")
    b = _mk(service, title="b")
    service.link_tickets(a.id, "blocks", b.id, actor="human")
    service.link_tickets(b.id, "blocks", a.id, actor="human")
    assert _by_key(service.get_ticket(a.id), "blocks") == [b.id]
    assert _by_key(service.get_ticket(a.id), "blocked_by") == [b.id]
    assert len(service.store.list_links()) == 2


def test_multi_parent_rejected(service, project):
    child = _mk(service, title="child")
    p1 = _mk(service, title="p1")
    p2 = _mk(service, title="p2")
    service.link_tickets(child.id, "parent", p1.id, actor="human")
    # Re-stating the same parent is idempotent...
    service.link_tickets(child.id, "parent", p1.id, actor="human")
    assert _by_key(service.get_ticket(child.id), "parent") == [p1.id]
    # ...but a second, different parent is rejected.
    with pytest.raises(ValidationError):
        service.link_tickets(child.id, "parent", p2.id, actor="human")


def test_parent_cycle_rejected_direct(service, project):
    a = _mk(service, title="a")
    b = _mk(service, title="b")
    service.link_tickets(a.id, "parent", b.id, actor="human")  # a's parent is b
    with pytest.raises(ValidationError):
        service.link_tickets(b.id, "parent", a.id, actor="human")  # would cycle


def test_parent_cycle_rejected_transitive(service, project):
    a = _mk(service, title="a")
    b = _mk(service, title="b")
    c = _mk(service, title="c")
    service.link_tickets(a.id, "parent", b.id, actor="human")  # a -> b
    service.link_tickets(b.id, "parent", c.id, actor="human")  # b -> c
    with pytest.raises(ValidationError):
        service.link_tickets(c.id, "parent", a.id, actor="human")  # c -> a closes the loop


def test_link_normalizes_ticket_ids(service, project):
    _mk(service, title="a")
    _mk(service, title="b")
    service.link_tickets("solo-1", "blocks", "solo-2", actor="human")
    assert _by_key(service.get_ticket("SOLO-1"), "blocks") == ["SOLO-2"]


# --- cross-project ----------------------------------------------------------


def test_cross_project_link_resolves_and_renders_on_both(service):
    service.add_project(key="SOLO", name="SoloPM")
    service.add_project(key="BLOG", name="Blog")
    a = service.create_ticket(project="SOLO", title="a")
    b = service.create_ticket(project="BLOG", title="b")
    service.link_tickets(a.id, "related", b.id, actor="human")
    assert _by_key(service.get_ticket("SOLO-1"), "related") == ["BLOG-1"]
    assert _by_key(service.get_ticket("BLOG-1"), "related") == ["SOLO-1"]


# --- activity logging -------------------------------------------------------


def test_link_logs_activity_on_both_tickets(service, project):
    a = _mk(service, title="a")
    b = _mk(service, title="b")
    service.link_tickets(a.id, "blocks", b.id, actor="claude")
    a_acts = [x for x in service.get_ticket(a.id).activity if x.kind == "link"]
    b_acts = [x for x in service.get_ticket(b.id).activity if x.kind == "link"]
    assert a_acts and a_acts[-1].actor == "claude"
    assert b_acts and b_acts[-1].actor == "claude"


# --- unlink -----------------------------------------------------------------


def test_unlink_removes_relation_from_both_perspectives(service, project):
    a = _mk(service, title="a")
    b = _mk(service, title="b")
    service.link_tickets(a.id, "blocks", b.id, actor="human")
    service.unlink_tickets(a.id, b.id, actor="human")
    assert _relations(service.get_ticket(a.id)) == []
    assert _relations(service.get_ticket(b.id)) == []
    assert service.store.list_links() == []


def test_unlink_is_order_independent(service, project):
    a = _mk(service, title="a")
    b = _mk(service, title="b")
    service.link_tickets(a.id, "blocks", b.id, actor="human")
    # Unlinking with the arguments reversed still removes the link between the pair.
    service.unlink_tickets(b.id, a.id, actor="human")
    assert service.store.list_links() == []


def test_unlink_with_type_filter_keeps_other_relations(service, project):
    a = _mk(service, title="a")
    b = _mk(service, title="b")
    service.link_tickets(a.id, "blocks", b.id, actor="human")
    service.link_tickets(a.id, "related", b.id, actor="human")
    service.unlink_tickets(a.id, b.id, type="blocks", actor="human")
    assert _by_key(service.get_ticket(a.id), "blocks") == []
    assert _by_key(service.get_ticket(a.id), "related") == [b.id]


def test_typed_unlink_preserves_opposing_directional_link(service, project):
    # A blocks B AND B blocks A both exist; removing A's "blocks" (outgoing) must leave the
    # opposing "blocked by" (B blocks A) intact — direction disambiguates the two rows.
    a = _mk(service, title="a")
    b = _mk(service, title="b")
    service.link_tickets(a.id, "blocks", b.id, actor="human")  # a -> b
    service.link_tickets(b.id, "blocks", a.id, actor="human")  # b -> a
    service.unlink_tickets(a.id, b.id, type="blocks", direction="out", actor="human")
    a_full = service.get_ticket(a.id)
    assert _by_key(a_full, "blocks") == []  # the a->b link is gone
    assert _by_key(a_full, "blocked_by") == [b.id]  # the b->a link survives
    assert len(service.store.list_links()) == 1


def test_unlink_direction_in_targets_incoming_link(service, project):
    a = _mk(service, title="a")
    b = _mk(service, title="b")
    service.link_tickets(a.id, "blocks", b.id, actor="human")  # a -> b
    service.link_tickets(b.id, "blocks", a.id, actor="human")  # b -> a
    # From A, direction "in" is the b->a link (A is the `to`).
    service.unlink_tickets(a.id, b.id, type="blocks", direction="in", actor="human")
    a_full = service.get_ticket(a.id)
    assert _by_key(a_full, "blocked_by") == []
    assert _by_key(a_full, "blocks") == [b.id]


def test_unlink_bad_direction_rejected(service, project):
    a = _mk(service, title="a")
    b = _mk(service, title="b")
    service.link_tickets(a.id, "blocks", b.id, actor="human")
    with pytest.raises(ValidationError):
        service.unlink_tickets(a.id, b.id, direction="sideways", actor="human")


def test_unlink_nonexistent_raises(service, project):
    a = _mk(service, title="a")
    b = _mk(service, title="b")
    with pytest.raises(NotFoundError):
        service.unlink_tickets(a.id, b.id, actor="human")


def test_unlink_logs_activity_on_both(service, project):
    a = _mk(service, title="a")
    b = _mk(service, title="b")
    service.link_tickets(a.id, "blocks", b.id, actor="human")
    service.unlink_tickets(a.id, b.id, actor="claude")
    assert any(x.kind == "unlink" for x in service.get_ticket(a.id).activity)
    assert any(x.kind == "unlink" for x in service.get_ticket(b.id).activity)


# --- cascade / cancelled ----------------------------------------------------


def test_link_to_cancelled_ticket_still_renders(service, project):
    a = _mk(service, title="a")
    b = _mk(service, title="b")
    service.link_tickets(a.id, "blocks", b.id, actor="human")
    service.move_ticket(b.id, "cancelled", actor="human")
    rel = _relations(service.get_ticket(a.id))[0]
    assert rel["ticket"]["id"] == b.id
    assert rel["ticket"]["state"] == "cancelled"


# --- derived summary fields: blocked + subtickets ---------------------------


def test_blocked_indicator_in_summary(service, project):
    blocker = _mk(service, title="blocker")
    blocked = _mk(service, title="blocked")
    service.link_tickets(blocker.id, "blocks", blocked.id, actor="human")
    assert service.get_ticket(blocked.id).to_summary()["blocked"] is True
    assert service.get_ticket(blocker.id).to_summary()["blocked"] is False


def test_blocked_clears_when_blocker_closes(service, project):
    blocker = _mk(service, title="blocker")
    blocked = _mk(service, title="blocked")
    service.link_tickets(blocker.id, "blocks", blocked.id, actor="human")
    service.move_ticket(blocker.id, "cancelled", actor="human")
    # A done/cancelled blocker no longer blocks.
    assert service.get_ticket(blocked.id).to_summary()["blocked"] is False


def test_finished_ticket_is_never_blocked(service, project):
    blocker = _mk(service, title="blocker")
    blocked = _mk(service, title="blocked")
    service.link_tickets(blocker.id, "blocks", blocked.id, actor="human")
    assert service.get_ticket(blocked.id).to_summary()["blocked"] is True
    # The blocked ticket itself is cancelled — even with the blocker still open, a finished
    # ticket can't be "blocked".
    service.move_ticket(blocked.id, "cancelled", actor="human")
    assert service.get_ticket(blocked.id).to_summary()["blocked"] is False


def test_list_computes_blocked_flag(service, project):
    a = _mk(service, title="a")
    b = _mk(service, title="b")
    service.link_tickets(a.id, "blocks", b.id, actor="human")
    summaries = {t.id: t.to_summary() for t in service.list_tickets(project="SOLO")}
    assert summaries[b.id]["blocked"] is True
    assert summaries[a.id]["blocked"] is False


def test_subticket_rollup_in_summary(service, project):
    parent = _mk(service, title="parent")
    done_child = service.create_ticket(
        project="SOLO", title="c1", state="done", actor="human"
    )
    open_child = _mk(service, title="c2")
    service.link_tickets(done_child.id, "parent", parent.id, actor="human")
    service.link_tickets(open_child.id, "parent", parent.id, actor="human")
    summary = service.get_ticket(parent.id).to_summary()
    assert summary["subtickets"] == {"done": 1, "total": 2}


def test_no_subtickets_reports_zero_total(service, project):
    t = _mk(service, title="lonely")
    assert t.to_summary()["subtickets"] == {"done": 0, "total": 0}
    assert t.to_summary()["blocked"] is False
