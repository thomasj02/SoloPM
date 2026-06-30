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

from .errors import DuplicateError, NotFoundError, ValidationError
from .models import Activity, Criterion, Link, Project, ReviewMemoryItem, Ticket

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
    review_memory       TEXT NOT NULL DEFAULT '[]',
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
    state_entered_at TEXT,
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

CREATE TABLE IF NOT EXISTS ticket_links (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    from_ticket TEXT NOT NULL REFERENCES tickets(id) ON DELETE CASCADE,
    to_ticket   TEXT NOT NULL REFERENCES tickets(id) ON DELETE CASCADE,
    type        TEXT NOT NULL,
    created_by  TEXT NOT NULL,
    created_at  TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_tickets_project ON tickets(project_key);
CREATE INDEX IF NOT EXISTS idx_activity_ticket ON activity(ticket_id, id);
CREATE INDEX IF NOT EXISTS idx_links_from ON ticket_links(from_ticket);
CREATE INDEX IF NOT EXISTS idx_links_to ON ticket_links(to_ticket);
-- Dedupe identical links (one row per from/to/type).
CREATE UNIQUE INDEX IF NOT EXISTS idx_links_unique ON ticket_links(from_ticket, to_ticket, type);
-- Enforce "at most one parent": a ticket appears as a parent-link `from` at most once.
CREATE UNIQUE INDEX IF NOT EXISTS idx_links_one_parent ON ticket_links(from_ticket) WHERE type = 'parent';
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
        "state_entered_at",
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
        if "state_entered_at" not in cols:
            # SOLO-13: time-in-current-state. Backfill from the most recent state-change
            # activity (creation counts as entry into the initial state), falling back to
            # created_at when a ticket has never been moved.
            conn.execute("ALTER TABLE tickets ADD COLUMN state_entered_at TEXT")
            conn.execute(
                """
                UPDATE tickets SET state_entered_at = COALESCE(
                    (SELECT a.created_at FROM activity a
                     WHERE a.ticket_id = tickets.id AND a.kind = 'state_change'
                     ORDER BY a.id DESC LIMIT 1),
                    created_at
                )
                """
            )
        pcols = {r["name"] for r in conn.execute("PRAGMA table_info(projects)")}
        if "review_memory" not in pcols:
            # SOLO-7: per-project review memory (the learning review gate).
            conn.execute(
                "ALTER TABLE projects ADD COLUMN review_memory TEXT NOT NULL DEFAULT '[]'"
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
            review_memory=[
                ReviewMemoryItem(**item)
                for item in json.loads(row["review_memory"] or "[]")
            ],
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
            state_entered_at=row["state_entered_at"] or row["created_at"],
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

    @staticmethod
    def _link(row: sqlite3.Row) -> Link:
        return Link(
            id=row["id"],
            from_ticket=row["from_ticket"],
            to_ticket=row["to_ticket"],
            type=row["type"],
            created_by=row["created_by"],
            created_at=row["created_at"],
        )

    def max_activity_id(self) -> int:
        with self._connect() as conn:
            row = conn.execute("SELECT COALESCE(MAX(id), 0) AS m FROM activity").fetchone()
            return int(row["m"])

    def activities_since(self, after_id: int, limit: int = 200) -> list[Activity]:
        """The global activity feed past ``after_id`` (ordered) — a change-feed across all
        tickets/writers, used by the MCP channel watcher."""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM activity WHERE id > ? ORDER BY id LIMIT ?", (after_id, limit)
            ).fetchall()
            return [self._activity(row) for row in rows]

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

    def delete_project(self, key: str, *, force: bool) -> int:
        """Atomically guard + delete a project and its dependents; return the ticket count.

        The existence check, the ticket count, the non-empty guard, and the deletes all run
        inside one ``BEGIN IMMEDIATE`` transaction. That atomicity is the point: a ticket
        inserted by a concurrent writer (another MCP agent, the web, or the CLI against the
        same WAL store) in the window between counting and deleting would otherwise be
        silently removed despite ``force=False`` — a TOCTOU that defeats the very guard the
        feature exists for. Holding the write lock from the count through the delete closes
        that window (the codebase keeps check + mutation in one transaction elsewhere, e.g.
        ``create_ticket``).

        Dependents are deleted **explicitly** (links → activity → tickets → project, the
        FK-safe child-before-parent order) rather than trusting ``ON DELETE CASCADE``. The
        cascade only fires if the table was *created* with the constraint, but the schema
        uses ``CREATE TABLE IF NOT EXISTS`` and SQLite cannot retrofit a foreign key via
        ``ALTER``/migration — so a store migrated from an early SoloPM schema (whose
        tickets/activity tables predate the cascade FKs, and which had no ``ticket_links``
        table at all) would otherwise keep orphan rows while this still reported a delete,
        and a later re-add of the same key would then collide on a duplicate ticket id.
        Deleting the rows ourselves is correct on both old and new stores. The ``ticket_links``
        delete matches *either* endpoint, so cross-project links to/from a surviving project
        are cleaned up too.

        Raises ``NotFoundError`` for an unknown project and ``ValidationError`` for a
        non-empty project without ``force``.
        """
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                if conn.execute(
                    "SELECT 1 FROM projects WHERE key = ?", (key,)
                ).fetchone() is None:
                    raise NotFoundError(f"Project {key!r} not found.")
                count = conn.execute(
                    "SELECT COUNT(*) AS c FROM tickets WHERE project_key = ?", (key,)
                ).fetchone()["c"]
                if count and not force:
                    raise ValidationError(
                        f"Project {key} has {count} ticket(s). Pass force=true to delete it "
                        "along with all its tickets, activity, and relationships."
                    )
                conn.execute(
                    "DELETE FROM ticket_links WHERE "
                    "from_ticket IN (SELECT id FROM tickets WHERE project_key = ?) OR "
                    "to_ticket   IN (SELECT id FROM tickets WHERE project_key = ?)",
                    (key, key),
                )
                conn.execute(
                    "DELETE FROM activity WHERE "
                    "ticket_id IN (SELECT id FROM tickets WHERE project_key = ?)",
                    (key,),
                )
                conn.execute("DELETE FROM tickets WHERE project_key = ?", (key,))
                conn.execute("DELETE FROM projects WHERE key = ?", (key,))
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise
        return count

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
                row = conn.execute(
                    "SELECT seq_counter FROM projects WHERE key = ?", (project_key,)
                ).fetchone()
                # The project can vanish between the service's existence check and this
                # write — a create/delete race made reachable once projects became
                # deletable (SOLO-20). Without the project row the UPDATE hit zero rows and
                # this SELECT is empty; raise a clean NotFoundError (→ 404) instead of
                # subscripting None into an internal 500.
                if row is None:
                    raise NotFoundError(f"Project {project_key!r} not found.")
                seq = row["seq_counter"]
                ticket_id = f"{project_key}-{seq}"
                # New tickets default to the end of their column (position = seq, which
                # is monotonic), so creation order is the initial board order.
                conn.execute(
                    """INSERT INTO tickets
                       (id, project_key, seq, title, description, state, assignee,
                        position, state_entered_at, created_at, updated_at)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        ticket_id,
                        project_key,
                        seq,
                        title,
                        description,
                        state,
                        assignee,
                        float(seq),
                        created_at,  # creation = entry into the initial state
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
            state_entered_at=created_at,
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

    def mutate_review_memory(
        self, project_key: str, mutate: Callable[[list[dict]], list[dict]], *, when: str
    ) -> None:
        """Atomically read-modify-write a project's review_memory JSON.

        Projects have no activity log, so this just serializes the list update inside one
        ``BEGIN IMMEDIATE`` transaction — concurrent candidate-captures and curation can't
        drop each other's edits. ``mutate(items) -> new_items`` may raise to abort.
        """
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                row = conn.execute(
                    "SELECT review_memory FROM projects WHERE key = ?", (project_key,)
                ).fetchone()
                if row is None:
                    raise NotFoundError(f"Project {project_key!r} not found.")
                new_items = mutate(json.loads(row["review_memory"] or "[]"))
                conn.execute(
                    "UPDATE projects SET review_memory = ?, updated_at = ? WHERE key = ?",
                    (json.dumps(new_items), when, project_key),
                )
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise

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

    # --- ticket links (relationships) ---------------------------------------

    def add_link(
        self,
        from_id: str,
        to_id: str,
        link_type: str,
        *,
        actor: str,
        when: str,
        body_from: str,
        body_to: str,
    ) -> tuple[Link, bool]:
        """Insert one canonical link and log a ``link`` activity on BOTH endpoints, atomically.

        Idempotent: if the identical (from, to, type) link already exists, returns it with
        ``created=False`` and writes nothing — so re-adding a link dedupes silently. The
        existence check, insert, and both activity rows share one ``BEGIN IMMEDIATE``
        transaction (and one timestamp), so concurrent callers can't double-insert or drift.
        """
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                existing = conn.execute(
                    "SELECT * FROM ticket_links WHERE from_ticket=? AND to_ticket=? AND type=?",
                    (from_id, to_id, link_type),
                ).fetchone()
                if existing is not None:
                    conn.execute("COMMIT")
                    return self._link(existing), False
                cur = conn.execute(
                    """INSERT INTO ticket_links (from_ticket, to_ticket, type, created_by, created_at)
                       VALUES (?,?,?,?,?)""",
                    (from_id, to_id, link_type, actor, when),
                )
                link_id = cur.lastrowid
                meta = json.dumps({"type": link_type, "from": from_id, "to": to_id})
                for ticket_id, body in ((from_id, body_from), (to_id, body_to)):
                    conn.execute(
                        """INSERT INTO activity (ticket_id, actor, kind, body, meta, created_at)
                           VALUES (?,?,?,?,?,?)""",
                        (ticket_id, actor, "link", body, meta, when),
                    )
                conn.execute(
                    "UPDATE tickets SET updated_at=? WHERE id IN (?, ?)", (when, from_id, to_id)
                )
                conn.execute("COMMIT")
            except sqlite3.IntegrityError as exc:
                conn.execute("ROLLBACK")
                # Backstop for the unique indexes. The service pre-checks dedupe (above) and
                # the one-parent rule, but those checks aren't race-free across connections;
                # under truly concurrent writers the partial one-parent index can still fire
                # here (e.g. two simultaneous parent links for the same child) — surface it as
                # a clean domain error rather than a raw IntegrityError. (Parent cycles are
                # guarded only by the service-level walk, which is best-effort under the same
                # rare concurrency; a single-user tool serializes these writes in practice.)
                raise DuplicateError("That link conflicts with an existing one.") from exc
            except Exception:
                conn.execute("ROLLBACK")
                raise
        return (
            Link(
                id=link_id,
                from_ticket=from_id,
                to_ticket=to_id,
                type=link_type,
                created_by=actor,
                created_at=when,
            ),
            True,
        )

    def remove_links(
        self,
        a_id: str,
        b_id: str,
        *,
        link_type: str | None,
        direction: str | None,
        actor: str,
        when: str,
        body_for: "Callable[[Link, str], str]",
    ) -> int:
        """Delete link(s) between ``a`` and ``b`` and log an ``unlink`` activity on both
        endpoints for each removed link. Returns the number removed.

        ``link_type`` optionally narrows to one relation type. ``direction`` selects the
        stored orientation relative to ``a``: ``"out"`` (``a``→``b``), ``"in"`` (``b``→``a``),
        or ``None`` (either order). Direction matters only when a pair holds *opposing*
        directional links (e.g. ``A blocks B`` and ``B blocks A``): passing it removes just
        the one the caller means, instead of both. ``body_for(link, ticket_id)`` renders the
        per-endpoint activity body (perspective-aware). All deletes + activity rows share one
        transaction.
        """
        if direction == "out":
            clauses = "(from_ticket=? AND to_ticket=?)"
            params: list = [a_id, b_id]
        elif direction == "in":
            clauses = "(from_ticket=? AND to_ticket=?)"
            params = [b_id, a_id]
        else:
            clauses = "((from_ticket=? AND to_ticket=?) OR (from_ticket=? AND to_ticket=?))"
            params = [a_id, b_id, b_id, a_id]
        if link_type is not None:
            clauses += " AND type=?"
            params.append(link_type)
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                rows = conn.execute(
                    f"SELECT * FROM ticket_links WHERE {clauses}", params
                ).fetchall()
                links = [self._link(r) for r in rows]
                for link in links:
                    conn.execute("DELETE FROM ticket_links WHERE id=?", (link.id,))
                    meta = json.dumps(
                        {"type": link.type, "from": link.from_ticket, "to": link.to_ticket}
                    )
                    for ticket_id in (link.from_ticket, link.to_ticket):
                        conn.execute(
                            """INSERT INTO activity (ticket_id, actor, kind, body, meta, created_at)
                               VALUES (?,?,?,?,?,?)""",
                            (ticket_id, actor, "unlink", body_for(link, ticket_id), meta, when),
                        )
                if links:
                    conn.execute(
                        "UPDATE tickets SET updated_at=? WHERE id IN (?, ?)", (when, a_id, b_id)
                    )
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise
        return len(links)

    def links_for_ticket(self, ticket_id: str) -> list[Link]:
        """Every link touching ``ticket_id`` (as either endpoint), oldest first."""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM ticket_links WHERE from_ticket=? OR to_ticket=? ORDER BY id",
                (ticket_id, ticket_id),
            ).fetchall()
            return [self._link(r) for r in rows]

    def list_links(self) -> list[Link]:
        """All links across the store, oldest first (used to batch-resolve board relations)."""
        with self._connect() as conn:
            return [
                self._link(r)
                for r in conn.execute("SELECT * FROM ticket_links ORDER BY id").fetchall()
            ]

    def get_parent(self, ticket_id: str) -> str | None:
        """The ticket id of ``ticket_id``'s parent, or ``None`` (used for cycle/one-parent checks)."""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT to_ticket FROM ticket_links WHERE from_ticket=? AND type='parent'",
                (ticket_id,),
            ).fetchone()
            return row["to_ticket"] if row else None

    def ticket_briefs(self, ids) -> dict[str, dict]:
        """``id -> {title, state, project, assignee}`` for a set of ticket ids, one query."""
        ids = list(ids)
        if not ids:
            return {}
        placeholders = ",".join("?" * len(ids))
        with self._connect() as conn:
            rows = conn.execute(
                f"SELECT id, title, state, project_key, assignee FROM tickets "
                f"WHERE id IN ({placeholders})",
                ids,
            ).fetchall()
            return {
                r["id"]: {
                    "title": r["title"],
                    "state": r["state"],
                    "project": r["project_key"],
                    "assignee": r["assignee"],
                }
                for r in rows
            }

