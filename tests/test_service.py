"""Tests for the canonical service operations (the heart of SoloPM)."""

import pytest

from solopm.core.errors import (
    DuplicateError,
    ForbiddenTransitionError,
    InvalidTransitionError,
    NotFoundError,
    ValidationError,
)


# --- Projects ---------------------------------------------------------------


def test_add_and_get_project(service):
    p = service.add_project(key="SOLO", name="SoloPM", repo="/code/solopm", master="main")
    assert p.key == "SOLO"
    assert p.name == "SoloPM"
    assert p.repo == "/code/solopm"
    assert p.master_branch == "main"
    assert p.default_implementer == "claude"
    assert p.default_reviewer == "codex"
    assert p.created_at and p.updated_at

    fetched = service.get_project("SOLO")
    assert fetched.key == "SOLO"
    assert fetched.ticket_count == 0


def test_project_key_is_normalized_uppercase(service):
    p = service.add_project(key="solo", name="SoloPM")
    assert p.key == "SOLO"
    assert service.get_project("solo").key == "SOLO"  # lookup also normalizes


def test_invalid_project_key_rejected(service):
    with pytest.raises(ValidationError):
        service.add_project(key="9bad", name="X")
    with pytest.raises(ValidationError):
        service.add_project(key="has space", name="X")


def test_empty_project_name_rejected(service):
    with pytest.raises(ValidationError):
        service.add_project(key="SOLO", name="   ")


def test_duplicate_project_rejected(service):
    service.add_project(key="SOLO", name="SoloPM")
    with pytest.raises(DuplicateError):
        service.add_project(key="SOLO", name="Again")


def test_get_missing_project_raises(service):
    with pytest.raises(NotFoundError):
        service.get_project("NOPE")


def test_list_projects(service):
    service.add_project(key="SOLO", name="SoloPM")
    service.add_project(key="BLOG", name="Blog")
    keys = {p.key for p in service.list_projects()}
    assert keys == {"SOLO", "BLOG"}


def test_set_project_field(service):
    service.add_project(key="SOLO", name="SoloPM")
    p = service.set_project_field("SOLO", "review_prompt", "Be tough.")
    assert p.review_prompt == "Be tough."
    assert service.get_project("SOLO").review_prompt == "Be tough."


def test_set_unknown_project_field_rejected(service):
    service.add_project(key="SOLO", name="SoloPM")
    with pytest.raises(ValidationError):
        service.set_project_field("SOLO", "seq_counter", "999")
    with pytest.raises(ValidationError):
        service.set_project_field("SOLO", "bogus", "x")


# --- Project deletion (SOLO-20) ---------------------------------------------


def test_delete_empty_project(service):
    service.add_project(key="SOLO", name="SoloPM")
    result = service.delete_project("SOLO")
    assert result == {"key": "SOLO", "deleted": True, "tickets_deleted": 0}
    with pytest.raises(NotFoundError):
        service.get_project("SOLO")
    assert service.list_projects() == []


def test_delete_missing_project_raises(service):
    with pytest.raises(NotFoundError):
        service.delete_project("NOPE")


def test_delete_project_key_is_normalized(service):
    service.add_project(key="SOLO", name="SoloPM")
    result = service.delete_project("solo")  # lowercase resolves to SOLO
    assert result["key"] == "SOLO"
    with pytest.raises(NotFoundError):
        service.get_project("SOLO")


def test_delete_nonempty_project_refused_without_force(service):
    service.add_project(key="SOLO", name="SoloPM")
    service.create_ticket(project="SOLO", title="a")
    service.create_ticket(project="SOLO", title="b")
    with pytest.raises(ValidationError):
        service.delete_project("SOLO")
    # Refusal leaves the project — and its tickets — untouched.
    assert service.get_project("SOLO").ticket_count == 2
    assert service.store.get_ticket("SOLO-1") is not None


def test_delete_nonempty_project_with_force_cascades(service):
    service.add_project(key="SOLO", name="SoloPM")
    service.create_ticket(project="SOLO", title="a")
    service.create_ticket(project="SOLO", title="b")
    service.link_tickets("SOLO-1", "blocks", "SOLO-2")

    result = service.delete_project("SOLO", force=True)
    assert result == {"key": "SOLO", "deleted": True, "tickets_deleted": 2}
    with pytest.raises(NotFoundError):
        service.get_project("SOLO")
    # Tickets, their activity, and their links are all cascade-deleted.
    assert service.store.get_ticket("SOLO-1") is None
    assert service.store.get_ticket("SOLO-2") is None
    assert service.store.list_links() == []
    assert service.store.max_activity_id() == 0


def test_delete_project_only_removes_its_own_data(service):
    service.add_project(key="SOLO", name="SoloPM")
    service.add_project(key="BLOG", name="Blog")
    service.create_ticket(project="SOLO", title="a")
    service.create_ticket(project="BLOG", title="b")
    # A cross-project link from the deleted project's ticket to a surviving one.
    service.link_tickets("SOLO-1", "related", "BLOG-1")

    service.delete_project("SOLO", force=True)

    # The other project and its ticket survive; only the cross-project link is gone.
    assert {p.key for p in service.list_projects()} == {"BLOG"}
    assert service.store.get_ticket("BLOG-1") is not None
    assert service.store.links_for_ticket("BLOG-1") == []


def test_store_delete_project_guard_is_atomic(service):
    """The non-empty guard, ticket count, and delete live in the store's single
    transaction (so a concurrent insert can't slip past force=False) — exercise that
    contract directly on the store, returning the real cascaded count."""
    service.add_project(key="SOLO", name="SoloPM")
    service.create_ticket(project="SOLO", title="a")

    with pytest.raises(ValidationError):
        service.store.delete_project("SOLO", force=False)
    assert service.store.get_ticket("SOLO-1") is not None  # refusal rolled back

    with pytest.raises(NotFoundError):
        service.store.delete_project("NOPE", force=True)

    assert service.store.delete_project("SOLO", force=True) == 1  # real cascaded count
    assert service.store.get_ticket("SOLO-1") is None


# --- Ticket creation & IDs --------------------------------------------------


def test_create_ticket_allocates_sequential_ids(service, project):
    t1 = service.create_ticket(project="SOLO", title="First")
    t2 = service.create_ticket(project="SOLO", title="Second")
    assert t1.id == "SOLO-1"
    assert t2.id == "SOLO-2"
    assert t1.seq == 1 and t2.seq == 2


def test_ticket_sequences_are_per_project(service):
    service.add_project(key="SOLO", name="SoloPM")
    service.add_project(key="BLOG", name="Blog")
    a = service.create_ticket(project="SOLO", title="A")
    b = service.create_ticket(project="BLOG", title="B")
    assert a.id == "SOLO-1"
    assert b.id == "BLOG-1"


def test_create_ticket_defaults(service, project):
    t = service.create_ticket(project="SOLO", title="Thing")
    assert t.state == "backlog"
    assert t.assignee == "unassigned"
    assert t.description == ""
    assert t.branch is None
    assert t.pr_dict() is None
    assert t.session_dict() is None


def test_create_ticket_logs_created_activity(service, project):
    t = service.create_ticket(project="SOLO", title="Thing", actor="human")
    full = service.get_ticket(t.id)
    assert len(full.activity) == 1
    assert full.activity[0].kind == "created"
    assert full.activity[0].actor == "human"


def test_create_ticket_requires_title(service, project):
    with pytest.raises(ValidationError):
        service.create_ticket(project="SOLO", title="   ")


def test_create_ticket_unknown_project(service):
    with pytest.raises(NotFoundError):
        service.create_ticket(project="NOPE", title="x")


def test_create_ticket_invalid_state(service, project):
    with pytest.raises(ValidationError):
        service.create_ticket(project="SOLO", title="x", state="wat")


def test_create_ticket_invalid_assignee(service, project):
    with pytest.raises(ValidationError):
        service.create_ticket(project="SOLO", title="x", assignee="bob")


def test_create_ticket_invalid_actor(service, project):
    with pytest.raises(ValidationError):
        service.create_ticket(project="SOLO", title="x", actor="robot")


def test_agent_cannot_create_ticket_directly_in_done(service, project):
    # The "only the human reaches done" invariant also covers creation.
    with pytest.raises(ForbiddenTransitionError):
        service.create_ticket(project="SOLO", title="x", state="done", actor="claude")
    # The human may.
    t = service.create_ticket(project="SOLO", title="x", state="done", actor="human")
    assert t.state == "done"


def test_project_ticket_count_reflects_tickets(service, project):
    service.create_ticket(project="SOLO", title="a")
    service.create_ticket(project="SOLO", title="b")
    assert service.get_project("SOLO").ticket_count == 2


# --- Listing & filtering ----------------------------------------------------


def test_list_tickets_filters(service):
    service.add_project(key="SOLO", name="SoloPM")
    service.add_project(key="BLOG", name="Blog")
    service.create_ticket(project="SOLO", title="a", assignee="claude")
    service.create_ticket(project="SOLO", title="b", state="todo")
    service.create_ticket(project="BLOG", title="c")

    assert len(service.list_tickets()) == 3
    assert len(service.list_tickets(project="SOLO")) == 2
    assert len(service.list_tickets(project="BLOG")) == 1
    assert len(service.list_tickets(state="todo")) == 1
    assert len(service.list_tickets(assignee="claude")) == 1


def test_list_tickets_invalid_filter_rejected(service, project):
    with pytest.raises(ValidationError):
        service.list_tickets(state="bogus")
    with pytest.raises(ValidationError):
        service.list_tickets(assignee="bogus")


# --- Get / edit -------------------------------------------------------------


def test_get_missing_ticket_raises(service):
    with pytest.raises(NotFoundError):
        service.get_ticket("SOLO-999")


def test_edit_ticket(service, project):
    t = service.create_ticket(project="SOLO", title="Old", description="old body")
    edited = service.edit_ticket(t.id, title="New", description="new body", actor="human")
    assert edited.title == "New"
    assert edited.description == "new body"
    full = service.get_ticket(t.id)
    assert any(a.kind == "edit" for a in full.activity)


def test_edit_ticket_blank_title_rejected(service, project):
    t = service.create_ticket(project="SOLO", title="Old")
    with pytest.raises(ValidationError):
        service.edit_ticket(t.id, title="   ")


def test_edit_ticket_partial(service, project):
    t = service.create_ticket(project="SOLO", title="Keep", description="body")
    edited = service.edit_ticket(t.id, description="changed")
    assert edited.title == "Keep"
    assert edited.description == "changed"


def test_edit_ticket_timestamp_matches_activity(service, project):
    # The ticket's updated_at and the edit activity must share one timestamp.
    t = service.create_ticket(project="SOLO", title="Old")
    edited = service.edit_ticket(t.id, title="New")
    edit_activity = [a for a in edited.activity if a.kind == "edit"][-1]
    assert edit_activity.created_at == edited.updated_at


# --- Comments ---------------------------------------------------------------


def test_comment_appends_activity(service, project):
    t = service.create_ticket(project="SOLO", title="x")
    a = service.comment_ticket(t.id, body="progress note", actor="claude")
    assert a.kind == "comment"
    assert a.actor == "claude"
    assert a.body == "progress note"
    full = service.get_ticket(t.id)
    comments = [x for x in full.activity if x.kind == "comment"]
    assert len(comments) == 1
    assert full.to_dict()["comments"][0]["author"] == "claude"


def test_comment_requires_body(service, project):
    t = service.create_ticket(project="SOLO", title="x")
    with pytest.raises(ValidationError):
        service.comment_ticket(t.id, body="  ", actor="human")


def test_comment_missing_ticket(service):
    with pytest.raises(NotFoundError):
        service.comment_ticket("SOLO-9", body="hi")


# --- Move (transitions) -----------------------------------------------------


def test_move_happy_path(service, project):
    t = service.create_ticket(project="SOLO", title="x", state="backlog")
    service.move_ticket(t.id, "todo")
    service.move_ticket(t.id, "in-progress")
    moved = service.move_ticket(t.id, "in-ai-review", actor="claude")
    assert moved.state == "in-ai-review"
    full = service.get_ticket(t.id)
    changes = [a for a in full.activity if a.kind == "state_change"]
    assert len(changes) == 3
    assert changes[-1].meta == {"from": "in-progress", "to": "in-ai-review"}


def test_move_same_state_is_noop(service, project):
    t = service.create_ticket(project="SOLO", title="x", state="backlog")
    moved = service.move_ticket(t.id, "backlog")
    assert moved.state == "backlog"
    full = service.get_ticket(t.id)
    assert not any(a.kind == "state_change" for a in full.activity)


def test_move_illegal_transition(service, project):
    t = service.create_ticket(project="SOLO", title="x", state="backlog")
    with pytest.raises(InvalidTransitionError):
        service.move_ticket(t.id, "done")


def test_agent_cannot_reach_done(service, project):
    t = service.create_ticket(project="SOLO", title="x", state="backlog")
    service.move_ticket(t.id, "in-progress")
    service.move_ticket(t.id, "in-ai-review", actor="claude")
    service.move_ticket(t.id, "in-human-review", actor="codex")
    with pytest.raises(ForbiddenTransitionError):
        service.move_ticket(t.id, "done", actor="claude")
    # human can.
    done = service.move_ticket(t.id, "done", actor="human")
    assert done.state == "done"


def test_move_missing_ticket(service):
    with pytest.raises(NotFoundError):
        service.move_ticket("SOLO-9", "todo")


# --- Assignment -------------------------------------------------------------


def test_assign_ticket(service, project):
    t = service.create_ticket(project="SOLO", title="x")
    assigned = service.assign_ticket(t.id, "claude", actor="human")
    assert assigned.assignee == "claude"
    full = service.get_ticket(t.id)
    assert any(a.kind == "assignment" for a in full.activity)


def test_assign_unassigned(service, project):
    t = service.create_ticket(project="SOLO", title="x", assignee="claude")
    assigned = service.assign_ticket(t.id, "unassigned")
    assert assigned.assignee == "unassigned"


def test_assign_invalid_assignee(service, project):
    t = service.create_ticket(project="SOLO", title="x")
    with pytest.raises(ValidationError):
        service.assign_ticket(t.id, "bob")


def test_assign_same_is_noop(service, project):
    t = service.create_ticket(project="SOLO", title="x", assignee="claude")
    service.assign_ticket(t.id, "claude")
    full = service.get_ticket(t.id)
    assert not any(a.kind == "assignment" for a in full.activity)


def test_assign_missing_ticket(service):
    with pytest.raises(NotFoundError):
        service.assign_ticket("SOLO-9", "claude")


# --- Activity ordering ------------------------------------------------------


def test_activity_is_chronological_oldest_first(service, project):
    t = service.create_ticket(project="SOLO", title="x")
    service.comment_ticket(t.id, body="one")
    service.move_ticket(t.id, "todo")
    service.comment_ticket(t.id, body="two")
    full = service.get_ticket(t.id)
    kinds = [a.kind for a in full.activity]
    assert kinds == ["created", "comment", "state_change", "comment"]
