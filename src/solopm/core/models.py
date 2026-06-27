"""Domain enums and value objects.

These are plain dataclasses plus the canonical enumerations. The store maps rows to
these, the service operates on them, and the server/CLI serialize them.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timezone

# --- Enumerations -----------------------------------------------------------

STATES: tuple[str, ...] = (
    "backlog",
    "todo",
    "in-progress",
    "in-ai-review",
    "in-human-review",
    "done",
    "cancelled",
)

STATE_LABELS: dict[str, str] = {
    "backlog": "Backlog",
    "todo": "Todo",
    "in-progress": "In Progress",
    "in-ai-review": "In AI Review",
    "in-human-review": "In Human Review",
    "done": "Done",
    "cancelled": "Cancelled",
}

ASSIGNEES: tuple[str, ...] = ("human", "claude", "codex", "unassigned")

# Actors that may perform a write. ``system`` is reserved for automated activity and is
# not accepted from clients.
ACTORS: tuple[str, ...] = ("human", "claude", "codex")
AGENT_ACTORS: tuple[str, ...] = ("claude", "codex")

ACTIVITY_KINDS: tuple[str, ...] = (
    "created",
    "comment",
    "state_change",
    "assignment",
    "edit",
    "criteria",
    "review",
    "link",
    "unlink",
)

# Ticket relationship types (SOLO-10). Each has a defined canonical storage direction
# (the link is stored from→to once and the inverse is derived for the other ticket):
#   blocks    — from blocks to            (from is the blocker)
#   related   — symmetric                 (stored in a stable order so it dedupes)
#   duplicate — from is a duplicate of to (from is the duplicate)
#   parent    — from's parent is to       (from is the child, to is the parent)
LINK_TYPES: tuple[str, ...] = ("blocks", "related", "duplicate", "parent")

# (link type, viewing ticket is the stored ``from``) -> (perspective group key, label).
# Used to derive how a link reads from each endpoint's point of view.
_RELATION_VIEW: dict[tuple[str, bool], tuple[str, str]] = {
    ("blocks", True): ("blocks", "Blocks"),
    ("blocks", False): ("blocked_by", "Blocked by"),
    ("related", True): ("related", "Related"),
    ("related", False): ("related", "Related"),
    ("duplicate", True): ("duplicate_of", "Duplicate of"),
    ("duplicate", False): ("duplicated_by", "Duplicated by"),
    ("parent", True): ("parent", "Parent"),
    ("parent", False): ("sub", "Sub-tickets"),
}

# Stable display order of the perspective groups (used to sort a ticket's relations).
RELATION_GROUP_ORDER: tuple[str, ...] = (
    "parent",
    "sub",
    "blocks",
    "blocked_by",
    "related",
    "duplicate_of",
    "duplicated_by",
)


def relation_view(link_type: str, is_from: bool) -> tuple[str, str]:
    """The (group key, human label) for a link type as seen from one endpoint.

    ``is_from`` is True when the viewing ticket is the canonical ``from`` of the link.
    """
    return _RELATION_VIEW[(link_type, is_from)]

DEFAULT_REVIEW_PROMPT = (
    "You are reviewing a coding change with fresh eyes and no prior context. "
    "Read the diff on this ticket's branch carefully and think hard. Identify "
    "correctness bugs, missing edge cases, and spec deviations. Then report a verdict "
    "of pass or fail with specific, actionable comments via `solopm review submit`."
)

DEFAULT_BRANCH_CONVENTION = "{key}-{seq}-{slug}"


# --- Value objects ----------------------------------------------------------


@dataclass
class ReviewMemoryItem:
    """One per-project review-memory checklist item (the learning review gate)."""

    id: str
    text: str
    source: str = "manual"  # ai_fail | human_miss | manual
    status: str = "candidate"  # candidate | active | retired
    hits: int = 0
    ticket: str | None = None  # the ticket whose review produced this candidate
    created_at: str = ""

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "text": self.text,
            "source": self.source,
            "status": self.status,
            "hits": self.hits,
            "ticket": self.ticket,
            "created_at": self.created_at,
        }


@dataclass
class Project:
    key: str
    name: str
    repo: str | None = None
    master_branch: str = "main"
    branch_convention: str = DEFAULT_BRANCH_CONVENTION
    default_implementer: str = "claude"
    default_reviewer: str = "codex"
    review_prompt: str = DEFAULT_REVIEW_PROMPT
    review_memory: list[ReviewMemoryItem] = field(default_factory=list)
    seq_counter: int = 0
    created_at: str = ""
    updated_at: str = ""
    # Populated by the service on read; not a stored column.
    ticket_count: int = 0

    def to_dict(self) -> dict:
        return {
            "key": self.key,
            "name": self.name,
            "repo": self.repo,
            "master_branch": self.master_branch,
            "branch_convention": self.branch_convention,
            "default_implementer": self.default_implementer,
            "default_reviewer": self.default_reviewer,
            "review_prompt": self.review_prompt,
            "review_memory": [i.to_dict() for i in self.review_memory],
            "ticket_count": self.ticket_count,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


@dataclass
class Activity:
    id: int
    ticket_id: str
    actor: str
    kind: str
    body: str = ""
    meta: dict = field(default_factory=dict)
    created_at: str = ""

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "actor": self.actor,
            "kind": self.kind,
            "body": self.body,
            "meta": self.meta,
            "at": self.created_at,
        }


@dataclass
class Criterion:
    """One acceptance-criterion checklist item on a ticket."""

    id: str
    text: str
    done: bool = False

    def to_dict(self) -> dict:
        return {"id": self.id, "text": self.text, "done": self.done}


@dataclass
class Link:
    """One stored ticket relationship row (canonical direction; inverse is derived)."""

    id: int
    from_ticket: str
    to_ticket: str
    type: str  # blocks | related | duplicate | parent
    created_by: str = ""
    created_at: str = ""


@dataclass
class Relation:
    """A link as seen from one ticket — assembled on read, not stored.

    Carries the perspective ``key``/``label`` (e.g. ``blocked_by`` / "Blocked by") plus a
    brief of the *other* ticket so a single ticket's relations render without extra lookups.
    """

    type: str  # canonical link type
    key: str  # perspective group key (blocks | blocked_by | related | …)
    label: str  # human group label
    direction: str  # "out" when this ticket is the canonical from, else "in"
    other_id: str
    other_title: str
    other_state: str
    created_by: str = ""
    created_at: str = ""

    def to_dict(self) -> dict:
        return {
            "type": self.type,
            "key": self.key,
            "label": self.label,
            "direction": self.direction,
            "ticket": {
                "id": self.other_id,
                "title": self.other_title,
                "state": self.other_state,
            },
            "created_by": self.created_by,
            "created_at": self.created_at,
        }


@dataclass
class Ticket:
    id: str
    project: str
    seq: int
    title: str
    description: str = ""
    state: str = "backlog"
    assignee: str = "unassigned"
    branch: str | None = None
    pr_number: int | None = None
    pr_url: str | None = None
    pr_state: str | None = None
    session_id: str | None = None
    session_active: bool = False
    acceptance_criteria: list[Criterion] = field(default_factory=list)
    position: float = 0.0  # within-column ordering; internal, not serialized to the API
    # When the ticket entered its CURRENT state. Set at creation and refreshed on every
    # transition (SOLO-13); a denormalized copy of the latest state-change timestamp so
    # board/list reads don't scan the activity log. Reorder/edit/comment/assign leave it.
    state_entered_at: str = ""
    created_at: str = ""
    updated_at: str = ""
    # Populated by the service/store on read.
    activity: list[Activity] = field(default_factory=list)
    comment_count: int = 0
    # Ticket relationships (SOLO-10), assembled by the service on read. ``relations`` is the
    # full perspective list (serialized on the detail ticket); ``blocked`` / ``sub_*`` are
    # the derived board-summary signals (an open blocker exists; sub-ticket rollup).
    relations: list[Relation] = field(default_factory=list)
    blocked: bool = False
    sub_done: int = 0
    sub_total: int = 0

    def pr_dict(self) -> dict | None:
        if self.pr_number is None:
            return None
        return {"number": self.pr_number, "url": self.pr_url, "state": self.pr_state}

    def session_dict(self) -> dict | None:
        if self.session_id is None:
            return None
        return {"id": self.session_id, "active": self.session_active}

    def acceptance_progress(self) -> dict:
        return {
            "done": sum(1 for c in self.acceptance_criteria if c.done),
            "total": len(self.acceptance_criteria),
        }

    def time_in_state_seconds(self) -> int | None:
        """Whole seconds since the ticket entered its current state, or ``None``.

        Computed fresh from :attr:`state_entered_at` (falling back to ``created_at``) at
        serialization time, so a card/agent always sees the live age without any stored
        duration to keep in sync. Terminal states (Done/Cancelled) are never left, so this
        keeps growing — the web surface frames that as completion age ("done 2d ago")
        rather than staleness, and does not tint it.
        """
        anchor = self.state_entered_at or self.created_at
        if not anchor:
            return None
        try:
            entered = datetime.strptime(anchor, "%Y-%m-%dT%H:%M:%SZ").replace(
                tzinfo=timezone.utc
            )
        except ValueError:
            return None
        elapsed = (datetime.now(timezone.utc) - entered).total_seconds()
        return max(0, int(elapsed))

    def to_summary(self) -> dict:
        return {
            "id": self.id,
            "project": self.project,
            "title": self.title,
            "state": self.state,
            "assignee": self.assignee,
            "branch": self.branch,
            "session_active": self.session_active,
            "pr": self.pr_dict(),
            "acceptance": self.acceptance_progress(),
            "comment_count": self.comment_count,
            "blocked": self.blocked,
            "subtickets": {"done": self.sub_done, "total": self.sub_total},
            "state_entered_at": self.state_entered_at,
            "time_in_state_seconds": self.time_in_state_seconds(),
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

    def to_dict(self) -> dict:
        comments = [
            {"author": a.actor, "body": a.body, "at": a.created_at}
            for a in self.activity
            if a.kind == "comment"
        ]
        return {
            "id": self.id,
            "project": self.project,
            "seq": self.seq,
            "title": self.title,
            "description": self.description,
            "state": self.state,
            "assignee": self.assignee,
            "branch": self.branch,
            "pr": self.pr_dict(),
            "session": self.session_dict(),
            "acceptance_criteria": [c.to_dict() for c in self.acceptance_criteria],
            "relations": [r.to_dict() for r in self.relations],
            "comments": comments,
            "activity": [a.to_dict() for a in self.activity],
            "state_entered_at": self.state_entered_at,
            "time_in_state_seconds": self.time_in_state_seconds(),
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


# --- Helpers ----------------------------------------------------------------

_SLUG_STRIP = re.compile(r"[^a-z0-9]+")


def slugify(text: str, *, max_len: int = 50) -> str:
    """Lowercase, hyphenate, trim — for branch names."""
    slug = _SLUG_STRIP.sub("-", text.lower()).strip("-")
    if len(slug) > max_len:
        slug = slug[:max_len].rstrip("-")
    return slug or "ticket"


_KEY_RE = re.compile(r"^[A-Z][A-Z0-9]*$")


def normalize_project_key(key: str) -> str:
    """Uppercase and validate a project key (e.g. ``solo`` -> ``SOLO``)."""
    candidate = (key or "").strip().upper()
    if not _KEY_RE.match(candidate):
        from .errors import ValidationError

        raise ValidationError(
            f"Invalid project key {key!r}: must start with a letter and contain only "
            "letters and digits (e.g. SOLO, BLOG7)."
        )
    return candidate


_TICKET_ID_RE = re.compile(r"^([A-Za-z][A-Za-z0-9]*)-(\d+)$")


def normalize_ticket_id(ticket_id: str) -> str:
    """Normalize a ticket id's project prefix to uppercase (e.g. ``solo-10`` → ``SOLO-10``).

    Leaves an unrecognized shape untouched so the caller's existence check raises a clean
    ``not_found`` rather than this masking it.
    """
    candidate = (ticket_id or "").strip()
    match = _TICKET_ID_RE.match(candidate)
    if not match:
        return candidate
    return f"{match.group(1).upper()}-{match.group(2)}"
