"""Within-column ordering: a persistent `position` and the reorder operation."""

import sqlite3

import pytest

from solopm.core.errors import NotFoundError, ValidationError
from solopm.core.service import Service
from solopm.core.store import Store


def ordered(service, state, project="SOLO"):
    """Ticket IDs in a state column, in display order (position then seq)."""
    ts = service.list_tickets(project=project, state=state)
    ts = [t for t in ts if t.state == state]
    ts.sort(key=lambda t: (t.position, t.seq))
    return [t.id for t in ts]


def _seed(service, n, state="backlog"):
    return [service.create_ticket(project="SOLO", title=f"t{i}", state=state).id for i in range(n)]


# --- defaults ---------------------------------------------------------------


def test_new_tickets_in_creation_order(service, project):
    _seed(service, 3)
    assert ordered(service, "backlog") == ["SOLO-1", "SOLO-2", "SOLO-3"]


# --- reorder ----------------------------------------------------------------


def test_reorder_to_top(service, project):
    _seed(service, 3)
    service.reorder_ticket("SOLO-3", after=None)
    assert ordered(service, "backlog") == ["SOLO-3", "SOLO-1", "SOLO-2"]


def test_reorder_after_specific(service, project):
    _seed(service, 3)
    service.reorder_ticket("SOLO-1", after="SOLO-2")  # move first to just after second
    assert ordered(service, "backlog") == ["SOLO-2", "SOLO-1", "SOLO-3"]


def test_reorder_to_bottom(service, project):
    _seed(service, 3)
    service.reorder_ticket("SOLO-1", after="SOLO-3")
    assert ordered(service, "backlog") == ["SOLO-2", "SOLO-3", "SOLO-1"]


def test_reorder_is_stable_across_many_moves(service, project):
    _seed(service, 4)
    service.reorder_ticket("SOLO-4", after=None)       # 4,1,2,3
    service.reorder_ticket("SOLO-2", after="SOLO-4")   # 4,2,1,3
    service.reorder_ticket("SOLO-3", after="SOLO-4")   # 4,3,2,1
    assert ordered(service, "backlog") == ["SOLO-4", "SOLO-3", "SOLO-2", "SOLO-1"]


def test_reorder_after_self_is_noop(service, project):
    _seed(service, 3)
    service.reorder_ticket("SOLO-2", after="SOLO-2")
    assert ordered(service, "backlog") == ["SOLO-1", "SOLO-2", "SOLO-3"]


def test_reorder_after_must_be_same_state(service, project):
    service.create_ticket(project="SOLO", title="a")  # SOLO-1 backlog
    service.create_ticket(project="SOLO", title="b", state="todo")  # SOLO-2 todo
    with pytest.raises(ValidationError):
        service.reorder_ticket("SOLO-1", after="SOLO-2")


def test_reorder_after_unknown_raises(service, project):
    _seed(service, 1)
    with pytest.raises(NotFoundError):
        service.reorder_ticket("SOLO-1", after="SOLO-99")


def test_reorder_unknown_ticket_raises(service, project):
    with pytest.raises(NotFoundError):
        service.reorder_ticket("SOLO-99", after=None)


def test_reorder_does_not_log_activity_or_bump_updated(service, project):
    _seed(service, 2)
    before = service.get_ticket("SOLO-1")
    service.reorder_ticket("SOLO-1", after="SOLO-2")
    after = service.get_ticket("SOLO-1")
    assert after.updated_at == before.updated_at
    assert len(after.activity) == len(before.activity)  # reorder is not activity


# --- move lands at the bottom of the target column --------------------------


def test_move_to_new_state_lands_at_bottom(service, project):
    service.create_ticket(project="SOLO", title="x", state="todo")  # SOLO-1
    service.create_ticket(project="SOLO", title="y", state="todo")  # SOLO-2
    service.create_ticket(project="SOLO", title="z")  # SOLO-3 backlog
    service.move_ticket("SOLO-3", "todo")  # after omitted -> bottom
    assert ordered(service, "todo") == ["SOLO-1", "SOLO-2", "SOLO-3"]


# --- positioned cross-column move (drop exactly where you want) --------------


def _two_todo_one_backlog(service):
    service.create_ticket(project="SOLO", title="x", state="todo")  # SOLO-1
    service.create_ticket(project="SOLO", title="y", state="todo")  # SOLO-2
    service.create_ticket(project="SOLO", title="z")  # SOLO-3 backlog


def test_move_with_after_positions_between(service, project):
    _two_todo_one_backlog(service)
    service.move_ticket("SOLO-3", "todo", after="SOLO-1")
    assert ordered(service, "todo") == ["SOLO-1", "SOLO-3", "SOLO-2"]


def test_move_with_after_none_goes_to_top(service, project):
    _two_todo_one_backlog(service)
    service.move_ticket("SOLO-3", "todo", after=None)
    assert ordered(service, "todo") == ["SOLO-3", "SOLO-1", "SOLO-2"]


def test_move_with_after_last_goes_to_bottom(service, project):
    _two_todo_one_backlog(service)
    service.move_ticket("SOLO-3", "todo", after="SOLO-2")
    assert ordered(service, "todo") == ["SOLO-1", "SOLO-2", "SOLO-3"]


def test_move_after_must_be_in_target_column(service, project):
    _two_todo_one_backlog(service)
    # SOLO-1 is in todo; trying to move into 'in-progress' after a todo card is invalid.
    service.move_ticket("SOLO-3", "in-progress")  # valid transition first
    with pytest.raises(ValidationError):
        service.move_ticket("SOLO-1", "in-progress", after="SOLO-2")  # SOLO-2 is in todo


def test_move_same_state_with_after_repositions(service, project):
    """A same-state move with an explicit hint is a reorder, not a silent no-op (SOLO-25)."""
    _seed(service, 3)
    service.move_ticket("SOLO-1", "backlog", after="SOLO-2")
    assert ordered(service, "backlog") == ["SOLO-2", "SOLO-1", "SOLO-3"]


def test_move_same_state_with_after_none_goes_to_top(service, project):
    _seed(service, 3)
    service.move_ticket("SOLO-3", "backlog", after=None)
    assert ordered(service, "backlog") == ["SOLO-3", "SOLO-1", "SOLO-2"]


def test_move_same_state_with_after_keeps_state_metadata(service, project):
    """Repositioning must not masquerade as a state change: no activity, no age reset."""
    _seed(service, 2)
    before = service.get_ticket("SOLO-1")
    service.move_ticket("SOLO-1", "backlog", after="SOLO-2")
    t = service.get_ticket("SOLO-1")
    assert t.state_entered_at == before.state_entered_at
    assert not any(a.kind == "state_change" for a in t.activity)


def test_move_same_state_with_unknown_after_raises(service, project):
    _seed(service, 1)
    with pytest.raises(NotFoundError):
        service.move_ticket("SOLO-1", "backlog", after="SOLO-99")


def test_move_still_validates_transition_and_actor(service, project):
    from solopm.core.errors import ForbiddenTransitionError, InvalidTransitionError

    _two_todo_one_backlog(service)
    # illegal transition even with a position hint
    with pytest.raises(InvalidTransitionError):
        service.move_ticket("SOLO-3", "done", after=None)
    # actor rule still enforced
    service.move_ticket("SOLO-1", "in-progress")
    service.move_ticket("SOLO-1", "in-ai-review", actor="claude")
    service.move_ticket("SOLO-1", "in-human-review", actor="codex")
    with pytest.raises(ForbiddenTransitionError):
        service.move_ticket("SOLO-1", "done", after=None, actor="claude")


# --- migration of a pre-position store --------------------------------------

_OLD_SCHEMA = """
CREATE TABLE projects (
    key TEXT PRIMARY KEY, name TEXT NOT NULL, repo TEXT,
    master_branch TEXT NOT NULL DEFAULT 'main', branch_convention TEXT NOT NULL,
    default_implementer TEXT NOT NULL DEFAULT 'claude', default_reviewer TEXT NOT NULL DEFAULT 'codex',
    review_prompt TEXT NOT NULL DEFAULT '', seq_counter INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE tickets (
    id TEXT PRIMARY KEY, project_key TEXT NOT NULL, seq INTEGER NOT NULL,
    title TEXT NOT NULL, description TEXT NOT NULL DEFAULT '', state TEXT NOT NULL DEFAULT 'backlog',
    assignee TEXT NOT NULL DEFAULT 'unassigned', branch TEXT, pr_number INTEGER, pr_url TEXT,
    pr_state TEXT, session_id TEXT, session_active INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE activity (
    id INTEGER PRIMARY KEY AUTOINCREMENT, ticket_id TEXT NOT NULL, actor TEXT NOT NULL,
    kind TEXT NOT NULL, body TEXT NOT NULL DEFAULT '', meta TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL
);
"""


def test_migration_adds_and_backfills_position(tmp_path):
    db = tmp_path / "old.db"
    conn = sqlite3.connect(db)
    conn.executescript(_OLD_SCHEMA)
    conn.execute(
        "INSERT INTO projects (key,name,branch_convention,seq_counter,created_at,updated_at) "
        "VALUES ('SOLO','SoloPM','{key}-{seq}-{slug}',2,'t','t')"
    )
    for seq in (1, 2):
        conn.execute(
            "INSERT INTO tickets (id,project_key,seq,title,created_at,updated_at) VALUES (?,?,?,?,?,?)",
            (f"SOLO-{seq}", "SOLO", seq, f"t{seq}", "t", "t"),
        )
    conn.commit()
    conn.close()

    store = Store(db)
    store.init()  # must add the position column and backfill = seq
    service = Service(store)
    # Ordering works and a reorder takes effect on the migrated rows.
    assert ordered(service, "backlog") == ["SOLO-1", "SOLO-2"]
    service.reorder_ticket("SOLO-1", after="SOLO-2")
    assert ordered(service, "backlog") == ["SOLO-2", "SOLO-1"]
