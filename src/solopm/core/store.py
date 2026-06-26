"""SQLite persistence.

Deliberately dumb: the store maps rows <-> value objects and owns the schema and the
atomic sequence allocation. All business rules live in :mod:`solopm.core.service`.

A fresh connection is opened per call (each in WAL mode) so the store is safe to use
from FastAPI's threadpool without sharing a connection across threads.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Callable

from .errors import DuplicateError, NotFoundError
from .models import Activity, Criterion, Project, Ticket

SCHEMA = """
CREATE TABLE IF NOT EXISTS projects (
    key                 TEXT PRIMARY KEY,
    name                TEXT NOT NULL,
    repo                TEXT,
    master_branch       TEXT NOT NULL DEFAULT 'main',
    branch_convention   TEXT NOT NULL,
    default_implementer TEXT NOT NULL DEFAULT 'claude',
    default_reviewer    TEXT NOT NULL DEFAULT 'codex',
    review_prompt       TEXT NOT NULL DEFAULT '',
    seq_counter         INTEGER NOT NULL DEFAULT 0,
    created_at          TEXT NOT NULL,
    updated_at          TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS tickets (
    id             TEXT PRIMARY KEY,
    project_key    TEXT NOT NULL REFERENCES projects(key) ON DELETE CASCADE,
    seq            INTEGER NOT NULL,
    title          TEXT NOT NULL,
    description    TEXT NOT NULL DEFAULT '',
    state          TEXT NOT NULL DEFAULT 'backlog',
    assignee       TEXT NOT NULL DEFAULT 'unassigned',
    branch         TEXT,
    pr_number      INTEGER,
    pr_url         TEXT,
    pr_state       TEXT,
    session_id     TEXT,
    session_active INTEGER NOT NULL DEFAULT 0,
    acceptance_criteria TEXT NOT NULL DEFAULT '[]',
    position       REAL NOT NULL DEFAULT 0,
    created_at     TEXT NOT NULL,
    updated_at     TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS activity (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    ticket_id  TEXT NOT NULL REFERENCES tickets(id) ON DELETE CASCADE,
    actor      TEXT NOT NULL,
    kind       TEXT NOT NULL,
    body       TEXT NOT NULL DEFAULT '',
    meta       TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_tickets_project ON tickets(project_key);
CREATE INDEX IF NOT EXISTS idx_activity_ticket ON activity(ticket_id, id);
"""

# Columns clients may update on a ticket (whitelist guards against arbitrary writes).
_TICKET_UPDATABLE = frozenset(
    {
        "title",
        "description",
        "state",
        "assignee",
        "branch",
        "pr_number",
        "pr_url",
        "pr_state",
        "session_id",
        "session_active",
        "acceptance_criteria",
        "position",
    }
)

_PROJECT_UPDATABLE = frozenset(
    {
        "name",
        "repo",
        "master_branch",
        "branch_convention",
        "default_implementer",
        "default_reviewer",
        "review_prompt",
    }
)


class Store:
    def __init__(self, path: str | Path):
        self.path = Path(path)

    # --- connection / schema ------------------------------------------------

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA journal_mode = WAL")
        return conn

    def init(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.executescript(SCHEMA)
            self._migrate(conn)

    @staticmethod
    def _migrate(conn: sqlite3.Connection) -> None:
        """Forward-only migrations for stores created by an earlier version."""
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(tickets)")}
        if "position" not in cols:
            # Added in 0.1: per-column manual ordering. Backfill = creation order.
            conn.execute("ALTER TABLE tickets ADD COLUMN position REAL NOT NULL DEFAULT 0")
            conn.execute("UPDATE tickets SET position = seq")
        if "acceptance_criteria" not in cols:
            # SOLO-6: structured acceptance criteria. Existing tickets start empty.
            conn.execute(
                "ALTER TABLE tickets ADD COLUMN acceptance_criteria TEXT NOT NULL DEFAULT '[]'"
            )

    def exists(self) -> bool:
        return self.path.exists()

    # --- row mapping --------------------------------------------------------

    @staticmethod
    def _project(row: sqlite3.Row, ticket_count: int = 0) -> Project:
        return Project(
            key=row["key"],
            name=row["name"],
            repo=row["repo"],
            master_branch=row["master_branch"],
            branch_convention=row["branch_convention"],
            default_implementer=row["default_implementer"],
            default_reviewer=row["default_reviewer"],
            review_prompt=row["review_prompt"],
            seq_counter=row["seq_counter"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            ticket_count=ticket_count,
        )

    @staticmethod
    def _ticket(row: sqlite3.Row) -> Ticket:
        return Ticket(
            id=row["id"],
            project=row["project_key"],
            seq=row["seq"],
            title=row["title"],
            description=row["description"],
            state=row["state"],
            assignee=row["assignee"],
            branch=row["branch"],
            pr_number=row["pr_number"],
            pr_url=row["pr_url"],
            pr_state=row["pr_state"],
            session_id=row["session_id"],
            session_active=bool(row["session_active"]),
            acceptance_criteria=[
                Criterion(id=c["id"], text=c["text"], done=bool(c["done"]))
                for c in json.loads(row["acceptance_criteria"] or "[]")
            ],
            position=row["position"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    @staticmethod
    def _activity(row: sqlite3.Row) -> Activity:
        return Activity(
            id=row["id"],
            ticket_id=row["ticket_id"],
            actor=row["actor"],
            kind=row["kind"],
            body=row["body"],
            meta=json.loads(row["meta"] or "{}"),
            created_at=row["created_at"],
        )

    # --- projects -----------------------------------------------------------

    def insert_project(self, project: Project) -> None:
        with self._connect() as conn:
            try:
                conn.execute(
                    """INSERT INTO projects
                       (key, name, repo, master_branch, branch_convention,
                        default_implementer, default_reviewer, review_prompt,
                        seq_counter, created_at, updated_at)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        project.key,
                        project.name,
                        project.repo,
                        project.master_branch,
                        project.branch_convention,
                        project.default_implementer,
                        project.default_reviewer,
                        project.review_prompt,
                        project.seq_counter,
                        project.created_at,
                        project.updated_at,
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise DuplicateError(f"Project {project.key!r} already exists.") from exc

    def get_project(self, key: str) -> Project | None:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM projects WHERE key = ?", (key,)).fetchone()
            if row is None:
                return None
            count = conn.execute(
                "SELECT COUNT(*) AS c FROM tickets WHERE project_key = ?", (key,)
            ).fetchone()["c"]
            return self._project(row, count)

    def list_projects(self) -> list[Project]:
        with self._connect() as conn:
            rows = conn.execute("SELECT * FROM projects ORDER BY key").fetchall()
            counts = {
                r["project_key"]: r["c"]
                for r in conn.execute(
                    "SELECT project_key, COUNT(*) AS c FROM tickets GROUP BY project_key"
                ).fetchall()
            }
            return [self._project(r, counts.get(r["key"], 0)) for r in rows]

    def update_project(self, key: str, fields: dict, updated_at: str) -> None:
        clean = {k: v for k, v in fields.items() if k in _PROJECT_UPDATABLE}
        if not clean:
            return
        sets = ", ".join(f"{k} = ?" for k in clean)
        values = list(clean.values()) + [updated_at, key]
        with self._connect() as conn:
            conn.execute(
                f"UPDATE projects SET {sets}, updated_at = ? WHERE key = ?", values
            )

    # --- tickets ------------------------------------------------------------

    def create_ticket(
        self,
        *,
        project_key: str,
        title: str,
        description: str,
        state: str,
        assignee: str,
        actor: str,
        created_at: str,
    ) -> Ticket:
        """Allocate the sequence, insert the ticket, and log creation — all atomically."""
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                conn.execute(
                    "UPDATE projects SET seq_counter = seq_counter + 1 WHERE key = ?",
                    (project_key,),
                )
                seq = conn.execute(
                    "SELECT seq_counter FROM projects WHERE key = ?", (project_key,)
                ).fetchone()["seq_counter"]
                ticket_id = f"{project_key}-{seq}"
                # New tickets default to the end of their column (position = seq, which
                # is monotonic), so creation order is the initial board order.
                conn.execute(
                    """INSERT INTO tickets
                       (id, project_key, seq, title, description, state, assignee,
                        position, created_at, updated_at)
                       VALUES (?,?,?,?,?,?,?,?,?,?)""",
                    (
                        ticket_id,
                        project_key,
                        seq,
                        title,
                        description,
                        state,
                        assignee,
                        float(seq),
                        created_at,
                        created_at,
                    ),
                )
                conn.execute(
                    """INSERT INTO activity (ticket_id, actor, kind, body, meta, created_at)
                       VALUES (?,?,?,?,?,?)""",
                    (
                        ticket_id,
                        actor,
                        "created",
                        "created ticket",
                        json.dumps({"state": state, "assignee": assignee}),
                        created_at,
                    ),
                )
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise
        return Ticket(
            id=ticket_id,
            project=project_key,
            seq=seq,
            title=title,
            description=description,
            state=state,
            assignee=assignee,
            position=float(seq),
            created_at=created_at,
            updated_at=created_at,
        )

    def change_ticket(
        self,
        ticket_id: str,
        fields: dict,
        *,
        actor: str,
        kind: str,
        body: str,
        meta: dict | None,
        when: str,
    ) -> Activity:
        """Apply a column update (+ bump updated_at) and append one activity row, atomically.

        ``fields`` may be empty (e.g. a comment), in which case only ``updated_at`` is
        bumped. The ticket mutation and the activity insert share one transaction and one
        timestamp, so the two can never drift apart or half-commit.
        """
        clean = {k: v for k, v in fields.items() if k in _TICKET_UPDATABLE}
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                if clean:
                    sets = ", ".join(f"{k} = ?" for k in clean)
                    conn.execute(
                        f"UPDATE tickets SET {sets}, updated_at = ? WHERE id = ?",
                        [*clean.values(), when, ticket_id],
                    )
                else:
                    conn.execute(
                        "UPDATE tickets SET updated_at = ? WHERE id = ?", (when, ticket_id)
                    )
                cur = conn.execute(
                    """INSERT INTO activity (ticket_id, actor, kind, body, meta, created_at)
                       VALUES (?,?,?,?,?,?)""",
                    (ticket_id, actor, kind, body, json.dumps(meta or {}), when),
                )
                new_id = cur.lastrowid
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise
        return Activity(
            id=new_id,
            ticket_id=ticket_id,
            actor=actor,
            kind=kind,
            body=body,
            meta=meta or {},
            created_at=when,
        )

    def mutate_criteria(
        self,
        ticket_id: str,
        mutate: Callable[[list[dict]], tuple[list[dict], str, str, dict]],
        *,
        actor: str,
        when: str,
    ) -> Activity:
        """Atomically read-modify-write a ticket's acceptance criteria + log one activity.

        ``mutate(criteria) -> (new_criteria, kind, body, meta)`` runs INSIDE the write
        transaction (``BEGIN IMMEDIATE``), so concurrent mutations on the same ticket
        serialize and can't drop each other's updates (no lost-update race). The callback
        may raise (e.g. ``NotFoundError`` for an unknown criterion) to abort the change.
        """
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                row = conn.execute(
                    "SELECT acceptance_criteria FROM tickets WHERE id = ?", (ticket_id,)
                ).fetchone()
                if row is None:
                    raise NotFoundError(f"Ticket {ticket_id!r} not found.")
                new_criteria, kind, body, meta = mutate(json.loads(row["acceptance_criteria"] or "[]"))
                conn.execute(
                    "UPDATE tickets SET acceptance_criteria = ?, updated_at = ? WHERE id = ?",
                    (json.dumps(new_criteria), when, ticket_id),
                )
                cur = conn.execute(
                    """INSERT INTO activity (ticket_id, actor, kind, body, meta, created_at)
                       VALUES (?,?,?,?,?,?)""",
                    (ticket_id, actor, kind, body, json.dumps(meta or {}), when),
                )
                new_id = cur.lastrowid
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise
        return Activity(
            id=new_id, ticket_id=ticket_id, actor=actor, kind=kind, body=body,
            meta=meta or {}, created_at=when,
        )

    def get_ticket(self, ticket_id: str) -> Ticket | None:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM tickets WHERE id = ?", (ticket_id,)).fetchone()
            if row is None:
                return None
            ticket = self._ticket(row)
            ticket.activity = [
                self._activity(a)
                for a in conn.execute(
                    "SELECT * FROM activity WHERE ticket_id = ? ORDER BY id", (ticket_id,)
                ).fetchall()
            ]
            ticket.comment_count = sum(1 for a in ticket.activity if a.kind == "comment")
            return ticket

    def list_tickets(
        self,
        *,
        project: str | None = None,
        state: str | None = None,
        assignee: str | None = None,
    ) -> list[Ticket]:
        clauses, params = [], []
        if project is not None:
            clauses.append("project_key = ?")
            params.append(project)
        if state is not None:
            clauses.append("state = ?")
            params.append(state)
        if assignee is not None:
            clauses.append("assignee = ?")
            params.append(assignee)
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        with self._connect() as conn:
            rows = conn.execute(
                f"SELECT * FROM tickets{where} ORDER BY project_key, position, seq", params
            ).fetchall()
            tickets = [self._ticket(r) for r in rows]
            # Attach comment counts in one pass (used by summaries).
            counts = {
                r["ticket_id"]: r["c"]
                for r in conn.execute(
                    "SELECT ticket_id, COUNT(*) AS c FROM activity "
                    "WHERE kind = 'comment' GROUP BY ticket_id"
                ).fetchall()
            }
        for t in tickets:
            t.comment_count = counts.get(t.id, 0)
        return tickets

    def set_position(self, ticket_id: str, position: float) -> None:
        """Set a ticket's ordering position only — no activity, no ``updated_at`` bump.

        Reordering is a cosmetic, UI-only concern, so it deliberately leaves the audit
        trail and timestamps untouched.
        """
        with self._connect() as conn:
            conn.execute(
                "UPDATE tickets SET position = ? WHERE id = ?", (position, ticket_id)
            )

