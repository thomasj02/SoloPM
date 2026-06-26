"""Domain enums and value objects.

These are plain dataclasses plus the canonical enumerations. The store maps rows to
these, the service operates on them, and the server/CLI serialize them.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

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

ACTIVITY_KINDS: tuple[str, ...] = ("created", "comment", "state_change", "assignment", "edit")

DEFAULT_REVIEW_PROMPT = (
    "You are reviewing a coding change with fresh eyes and no prior context. "
    "Read the diff on this ticket's branch carefully and think hard. Identify "
    "correctness bugs, missing edge cases, and spec deviations. Then report a verdict "
    "of pass or fail with specific, actionable comments via `solopm review submit`."
)

DEFAULT_BRANCH_CONVENTION = "{key}-{seq}-{slug}"


# --- Value objects ----------------------------------------------------------


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
    created_at: str = ""
    updated_at: str = ""
    # Populated by the service/store on read.
    activity: list[Activity] = field(default_factory=list)
    comment_count: int = 0

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
            "comments": comments,
            "activity": [a.to_dict() for a in self.activity],
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
