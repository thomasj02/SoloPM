"""SOLO-21: ticket tags/labels — normalization, store/migration, service, filtering."""

import sqlite3

import pytest

from solopm.core.errors import NotFoundError, ValidationError
from solopm.core.models import TAGS_MAX_COUNT, normalize_tag, normalize_tags
from solopm.core.service import Service
from solopm.core.store import Store


# --- normalization ----------------------------------------------------------


def test_normalize_tag_lowercases_and_trims():
    assert normalize_tag("  Bug ") == "bug"
    assert normalize_tag("Front-End_1") == "front-end_1"


@pytest.mark.parametrize("bad", ["", "   ", "a b", "-x", "_x", "white space", "café", "a/b", "x" * 33])
def test_normalize_tag_rejects_invalid(bad):
    with pytest.raises(ValidationError):
        normalize_tag(bad)


def test_normalize_tags_dedupes_case_insensitively_and_sorts():
    assert normalize_tags(["b", "A", "a", "B", "c"]) == ["a", "b", "c"]


def test_normalize_tags_enforces_max_count():
    assert len(normalize_tags([f"t{i}" for i in range(TAGS_MAX_COUNT)])) == TAGS_MAX_COUNT
    with pytest.raises(ValidationError):
        normalize_tags([f"t{i}" for i in range(TAGS_MAX_COUNT + 1)])


# --- service: add / remove --------------------------------------------------


def test_add_tags_sets_sorted_unique(service, project):
    service.create_ticket(project="SOLO", title="x")
    out = service.add_tags("SOLO-1", ["frontend", "Bug", "bug"])
    assert out.tags == ["bug", "frontend"]
    # surfaces in serialization
    assert out.to_dict()["tags"] == ["bug", "frontend"]
    assert out.to_summary()["tags"] == ["bug", "frontend"]


def test_add_tags_is_idempotent_and_logs_only_on_change(service, project):
    service.create_ticket(project="SOLO", title="x")
    service.add_tags("SOLO-1", ["bug"])
    before = len(service.get_ticket("SOLO-1").activity)
    again = service.add_tags("SOLO-1", ["Bug"])  # already present (case-folded)
    assert again.tags == ["bug"]
    assert len(again.activity) == before  # no-op wrote no activity
    # a real change logs a `tags` activity
    changed = service.add_tags("SOLO-1", ["frontend"])
    assert changed.tags == ["bug", "frontend"]
    assert changed.activity[-1].kind == "tags"


def test_add_tags_enforces_max_count(service, project):
    service.create_ticket(project="SOLO", title="x")
    service.add_tags("SOLO-1", [f"t{i}" for i in range(TAGS_MAX_COUNT)])
    with pytest.raises(ValidationError):
        service.add_tags("SOLO-1", ["one-too-many"])


def test_add_tags_requires_valid_actor_and_existing_ticket(service, project):
    with pytest.raises(NotFoundError):
        service.add_tags("SOLO-999", ["bug"])
    service.create_ticket(project="SOLO", title="x")
    with pytest.raises(ValidationError):
        service.add_tags("SOLO-1", ["bug"], actor="robot")


def test_remove_tag(service, project):
    service.create_ticket(project="SOLO", title="x")
    service.add_tags("SOLO-1", ["bug", "frontend"])
    out = service.remove_tag("SOLO-1", "Bug")  # case-insensitive
    assert out.tags == ["frontend"]
    # removing an absent tag is an idempotent no-op (no error, no activity)
    before = len(out.activity)
    again = service.remove_tag("SOLO-1", "nope")
    assert again.tags == ["frontend"]
    assert len(again.activity) == before


# --- list filtering ---------------------------------------------------------


def test_list_tickets_filters_by_tag_AND(service):
    service.add_project(key="SOLO", name="SoloPM")
    service.create_ticket(project="SOLO", title="a")  # SOLO-1
    service.create_ticket(project="SOLO", title="b")  # SOLO-2
    service.create_ticket(project="SOLO", title="c")  # SOLO-3
    service.add_tags("SOLO-1", ["bug", "frontend"])
    service.add_tags("SOLO-2", ["bug"])
    # single tag
    assert {t.id for t in service.list_tickets(tags=["bug"])} == {"SOLO-1", "SOLO-2"}
    # AND across multiple tags
    assert {t.id for t in service.list_tickets(tags=["bug", "frontend"])} == {"SOLO-1"}
    # filter is case-insensitive and ignores blanks
    assert {t.id for t in service.list_tickets(tags=["BUG", "  "])} == {"SOLO-1", "SOLO-2"}
    # no matches
    assert service.list_tickets(tags=["missing"]) == []


# --- migration --------------------------------------------------------------


def test_migration_adds_tags_column_and_backfills_empty(tmp_path):
    """A store created before tags existed gets the column on init(); old tickets read []."""
    old_schema = """
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
    db = tmp_path / "old.db"
    conn = sqlite3.connect(db)
    conn.executescript(old_schema)
    conn.execute(
        "INSERT INTO projects (key,name,branch_convention,seq_counter,created_at,updated_at) "
        "VALUES ('SOLO','SoloPM','{key}-{seq}-{slug}',1,'t','t')"
    )
    conn.execute(
        "INSERT INTO tickets (id,project_key,seq,title,created_at,updated_at) "
        "VALUES ('SOLO-1','SOLO',1,'old','t','t')"
    )
    conn.commit()
    conn.close()

    store = Store(db)
    store.init()  # must add the tags column
    service = Service(store)
    assert service.get_ticket("SOLO-1").tags == []
    # and tagging the migrated ticket works
    assert service.add_tags("SOLO-1", ["legacy"]).tags == ["legacy"]


def test_store_mutate_tags_missing_ticket_raises(service, project):
    with pytest.raises(NotFoundError):
        service.store.mutate_tags(
            "SOLO-404", lambda cur: (cur, "tags", "x", {}), actor="human", when="t"
        )
