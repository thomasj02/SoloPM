"""SOLO-13: time-in-current-state (`state_entered_at` + computed time-in-state).

The denormalized `state_entered_at` is set at creation and refreshed on every state
transition, but is deliberately inert to reorder/edit/comment/assign (none of which
change the column). Backfill derives it from the last state-change activity, falling
back to `created_at`.
"""

import sqlite3

from solopm.core.service import Service
from solopm.core.store import Store


# --- state_entered_at lifecycle ---------------------------------------------


def test_state_entered_at_set_on_create(service, project):
    t = service.create_ticket(project="SOLO", title="t", actor="human")
    # Creation counts as entry into the initial state.
    assert t.state_entered_at == t.created_at
    assert t.to_summary()["state_entered_at"] == t.created_at
    assert t.to_dict()["state_entered_at"] == t.created_at


def test_move_updates_state_entered_at(service, project):
    t = service.create_ticket(project="SOLO", title="t", actor="human")
    moved = service.move_ticket(t.id, "in-progress", actor="human")
    # The new entry time shares the move's timestamp: == updated_at, and == the
    # state-change activity that recorded the transition.
    assert moved.state_entered_at == moved.updated_at
    state_changes = [a for a in moved.activity if a.kind == "state_change"]
    assert moved.state_entered_at == state_changes[-1].created_at


def test_reorder_does_not_reset_state_entered_at(service, project):
    a = service.create_ticket(project="SOLO", title="a", actor="human")
    b = service.create_ticket(project="SOLO", title="b", actor="human")
    before = service.get_ticket(a.id).state_entered_at
    service.reorder_ticket(a.id, after=b.id)
    assert service.get_ticket(a.id).state_entered_at == before


def test_edit_comment_assign_do_not_reset_state_entered_at(service, project):
    t = service.create_ticket(project="SOLO", title="t", actor="human")
    before = t.state_entered_at
    service.edit_ticket(t.id, title="t2", actor="human")
    assert service.get_ticket(t.id).state_entered_at == before
    service.comment_ticket(t.id, body="hi", actor="human")
    assert service.get_ticket(t.id).state_entered_at == before
    service.assign_ticket(t.id, "claude", actor="human")
    assert service.get_ticket(t.id).state_entered_at == before


def test_time_in_state_seconds_present_and_nonnegative(service, project):
    t = service.create_ticket(project="SOLO", title="t", actor="human")
    secs = t.to_summary()["time_in_state_seconds"]
    assert isinstance(secs, int) and secs >= 0
    assert t.to_dict()["time_in_state_seconds"] >= 0


# --- backfill migration ------------------------------------------------------

# The schema as it stood BEFORE state_entered_at (current shape minus the column),
# used to build a pre-migration store and assert the backfill.
_PRE_SCHEMA = """
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
    acceptance_criteria TEXT NOT NULL DEFAULT '[]', position REAL NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE activity (
    id INTEGER PRIMARY KEY AUTOINCREMENT, ticket_id TEXT NOT NULL, actor TEXT NOT NULL,
    kind TEXT NOT NULL, body TEXT NOT NULL DEFAULT '', meta TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL
);
"""


def _seed_pre_store(db):
    conn = sqlite3.connect(db)
    conn.executescript(_PRE_SCHEMA)
    conn.execute(
        "INSERT INTO projects (key,name,branch_convention,seq_counter,created_at,updated_at) "
        "VALUES ('SOLO','SoloPM','{key}-{seq}-{slug}',2,'2026-01-01T00:00:00Z','2026-01-01T00:00:00Z')"
    )
    # SOLO-1: created then moved — backfill should pick the latest state-change time.
    conn.execute(
        "INSERT INTO tickets (id,project_key,seq,title,state,created_at,updated_at) "
        "VALUES ('SOLO-1','SOLO',1,'moved','in-progress','2026-01-01T00:00:00Z','2026-01-03T00:00:00Z')"
    )
    conn.execute(
        "INSERT INTO activity (ticket_id,actor,kind,body,created_at) "
        "VALUES ('SOLO-1','human','created','created ticket','2026-01-01T00:00:00Z')"
    )
    conn.execute(
        "INSERT INTO activity (ticket_id,actor,kind,body,created_at) "
        "VALUES ('SOLO-1','human','state_change','moved backlog → in-progress','2026-01-02T12:00:00Z')"
    )
    # SOLO-2: never moved (only created) — backfill should fall back to created_at.
    conn.execute(
        "INSERT INTO tickets (id,project_key,seq,title,state,created_at,updated_at) "
        "VALUES ('SOLO-2','SOLO',2,'fresh','backlog','2026-01-05T00:00:00Z','2026-01-05T00:00:00Z')"
    )
    conn.execute(
        "INSERT INTO activity (ticket_id,actor,kind,body,created_at) "
        "VALUES ('SOLO-2','human','created','created ticket','2026-01-05T00:00:00Z')"
    )
    conn.commit()
    conn.close()


def test_backfill_derives_entry_time_from_activity(tmp_path):
    db = tmp_path / "pre.db"
    _seed_pre_store(db)

    store = Store(db)
    store.init()  # adds state_entered_at and backfills it
    service = Service(store)

    moved = service.get_ticket("SOLO-1")
    assert moved.state_entered_at == "2026-01-02T12:00:00Z"  # last state-change

    fresh = service.get_ticket("SOLO-2")
    assert fresh.state_entered_at == "2026-01-05T00:00:00Z"  # falls back to created_at
