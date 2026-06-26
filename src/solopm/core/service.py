"""The canonical SoloPM operations.

This is the single source of business logic. The HTTP server and (through it) the web
app and CLI are all clients of these operations — keeping the two interfaces honestly at
parity, as the product brief requires.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

from . import workflow
from .errors import ForbiddenTransitionError, NotFoundError, ValidationError
from .github import GitHubClient, validate_branch_name
from .models import (
    ASSIGNEES,
    ACTORS,
    DEFAULT_BRANCH_CONVENTION,
    DEFAULT_REVIEW_PROMPT,
    STATES,
    Activity,
    Project,
    Ticket,
    normalize_project_key,
)
from .store import Store

# Fields editable via ``set_project_field`` / project PATCH.
_PROJECT_SETTABLE = frozenset(
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


# Sentinel distinguishing "caller gave no position hint" (→ bottom of column) from an
# explicit ``after=None`` (→ top of column).
_UNSET = object()


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _require_actor(actor: str) -> str:
    if actor not in ACTORS:
        raise ValidationError(
            f"Unknown actor {actor!r}: expected one of {', '.join(ACTORS)}."
        )
    return actor


class Service:
    def __init__(self, store: Store, github: GitHubClient | None = None):
        self.store = store
        # Optional GitHub automation (Tier-1). When set, agent-managed tickets (those
        # with a SoloPM branch) drive PRs on transition; absent it, moves are pure.
        self.github = github

    @classmethod
    def open(cls, db_path) -> "Service":
        store = Store(db_path)
        return cls(store)

    # --- projects -----------------------------------------------------------

    def add_project(
        self,
        *,
        key: str,
        name: str,
        repo: str | None = None,
        master: str = "main",
        branch_convention: str = DEFAULT_BRANCH_CONVENTION,
        default_implementer: str = "claude",
        default_reviewer: str = "codex",
        review_prompt: str = DEFAULT_REVIEW_PROMPT,
    ) -> Project:
        key = normalize_project_key(key)
        if not name or not name.strip():
            raise ValidationError("Project name is required.")
        now = _now()
        project = Project(
            key=key,
            name=name.strip(),
            repo=repo,
            master_branch=master or "main",
            branch_convention=branch_convention or DEFAULT_BRANCH_CONVENTION,
            default_implementer=default_implementer,
            default_reviewer=default_reviewer,
            review_prompt=review_prompt,
            seq_counter=0,
            created_at=now,
            updated_at=now,
        )
        self.store.insert_project(project)  # raises DuplicateError on conflict
        return self.get_project(key)

    def list_projects(self) -> list[Project]:
        return self.store.list_projects()

    def get_project(self, key: str) -> Project:
        project = self.store.get_project(normalize_project_key(key))
        if project is None:
            raise NotFoundError(f"Project {key!r} not found.")
        return project

    def update_project(self, key: str, fields: dict) -> Project:
        key = normalize_project_key(key)
        self.get_project(key)  # existence check
        unknown = set(fields) - _PROJECT_SETTABLE
        if unknown:
            raise ValidationError(
                f"Cannot set field(s) {', '.join(sorted(unknown))}. "
                f"Editable: {', '.join(sorted(_PROJECT_SETTABLE))}."
            )
        if "name" in fields and not str(fields["name"]).strip():
            raise ValidationError("Project name cannot be blank.")
        self.store.update_project(key, fields, _now())
        return self.get_project(key)

    def set_project_field(self, key: str, field: str, value) -> Project:
        return self.update_project(key, {field: value})

    # --- tickets ------------------------------------------------------------

    def create_ticket(
        self,
        *,
        project: str,
        title: str,
        description: str = "",
        state: str = "backlog",
        assignee: str = "unassigned",
        actor: str = "human",
    ) -> Ticket:
        _require_actor(actor)
        key = normalize_project_key(project)
        self.get_project(key)  # raises NotFoundError if missing
        if not title or not title.strip():
            raise ValidationError("Ticket title is required.")
        if state not in STATES:
            raise ValidationError(f"Unknown state {state!r}.")
        if assignee not in ASSIGNEES:
            raise ValidationError(
                f"Unknown assignee {assignee!r}: expected one of {', '.join(ASSIGNEES)}."
            )
        # The "only the human reaches done" invariant also covers creation: an agent
        # cannot mint a ticket that is already closed.
        if state in workflow.HUMAN_ONLY_TARGETS and actor != "human":
            raise ForbiddenTransitionError(
                f"Only the human may create a ticket directly in {state}."
            )

        ticket = self.store.create_ticket(
            project_key=key,
            title=title.strip(),
            description=description or "",
            state=state,
            assignee=assignee,
            actor=actor,
            created_at=_now(),
        )
        return self.get_ticket(ticket.id)

    def list_tickets(
        self,
        *,
        project: str | None = None,
        state: str | None = None,
        assignee: str | None = None,
    ) -> list[Ticket]:
        if state is not None and state not in STATES:
            raise ValidationError(f"Unknown state {state!r}.")
        if assignee is not None and assignee not in ASSIGNEES:
            raise ValidationError(f"Unknown assignee {assignee!r}.")
        if project is not None:
            project = normalize_project_key(project)
        tickets = self.store.list_tickets(project=project, state=state, assignee=assignee)
        # Group by workflow state, then by manual position within each column.
        rank = {s: i for i, s in enumerate(STATES)}
        tickets.sort(key=lambda t: (rank.get(t.state, 99), t.position, t.seq))
        return tickets

    def get_ticket(self, ticket_id: str) -> Ticket:
        ticket = self.store.get_ticket(ticket_id)
        if ticket is None:
            raise NotFoundError(f"Ticket {ticket_id!r} not found.")
        return ticket

    def edit_ticket(
        self,
        ticket_id: str,
        *,
        title: str | None = None,
        description: str | None = None,
        actor: str = "human",
    ) -> Ticket:
        _require_actor(actor)
        self.get_ticket(ticket_id)  # existence check
        fields: dict = {}
        changed: list[str] = []
        if title is not None:
            if not title.strip():
                raise ValidationError("Ticket title cannot be blank.")
            fields["title"] = title.strip()
            changed.append("title")
        if description is not None:
            fields["description"] = description
            changed.append("description")
        if fields:
            self.store.change_ticket(
                ticket_id,
                fields,
                actor=actor,
                kind="edit",
                body="edited " + " and ".join(changed),
                meta={"fields": changed},
                when=_now(),
            )
        return self.get_ticket(ticket_id)

    def comment_ticket(self, ticket_id: str, *, body: str, actor: str = "human") -> Activity:
        _require_actor(actor)
        self.get_ticket(ticket_id)  # existence check
        if not body or not body.strip():
            raise ValidationError("Comment body is required.")
        # No column changes — change_ticket just bumps updated_at and logs the comment.
        return self.store.change_ticket(
            ticket_id,
            {},
            actor=actor,
            kind="comment",
            body=body.strip(),
            meta={},
            when=_now(),
        )

    def _position_in_column(self, project: str, state: str, after, *, exclude_id=None) -> float:
        """A position value placing a ticket within (``project``, ``state``).

        ``after`` is tri-state:
          * :data:`_UNSET` → bottom of the column;
          * ``None``       → top of the column;
          * a ticket id    → directly below it (midpoint to the next card — fractional
                             indexing, so siblings aren't renumbered).

        ``exclude_id`` drops a ticket from the neighbour calculation (used when
        reordering a ticket already in the column).
        """
        column = [
            t for t in self.store.list_tickets(project=project, state=state)
            if t.id != exclude_id
        ]
        column.sort(key=lambda t: (t.position, t.seq))

        if after is _UNSET:
            return (column[-1].position + 1.0) if column else 1.0
        if after is None:
            return (column[0].position - 1.0) if column else 1.0

        target = self.store.get_ticket(after)
        if target is None:
            raise NotFoundError(f"Ticket {after!r} not found.")
        if target.project != project or target.state != state:
            raise ValidationError(
                f"Cannot position after {after!r}: it is not in the same column."
            )
        idx = next(i for i, t in enumerate(column) if t.id == after)
        nxt = column[idx + 1] if idx + 1 < len(column) else None
        return (target.position + nxt.position) / 2 if nxt else target.position + 1.0

    def move_ticket(
        self,
        ticket_id: str,
        state: str,
        *,
        after=_UNSET,
        branch: str | None = None,
        actor: str = "human",
    ) -> Ticket:
        """Transition a ticket and place it in the target column.

        ``after`` is the position hint (see :meth:`_position_in_column`): omit it to land
        at the bottom, pass ``None`` for the top, or a ticket id to drop directly below
        that card. ``branch`` records the SoloPM branch when an agent self-transitions to
        ``in-ai-review``. State-transition and actor rules are enforced regardless.

        If GitHub automation is configured and the ticket has a SoloPM branch, the
        transition drives the PR: → in-ai-review pushes + opens/refreshes the PR;
        → done squash-merges it; → cancelled closes it. The GitHub side effects run
        **before** the state change, so a failure aborts the move.
        """
        _require_actor(actor)
        ticket = self.get_ticket(ticket_id)
        if workflow.is_noop(ticket.state, state):
            return ticket
        workflow.validate_transition(ticket.state, state, actor=actor)
        if branch:
            validate_branch_name(branch)
        # All local validation (transition, branch, position/after) runs BEFORE any
        # external GitHub side effect, so a bad request can never push/merge/close a PR
        # and then fail the move.
        new_pos = self._position_in_column(ticket.project, state, after, exclude_id=ticket_id)

        pr_fields = self._git_side_effects(ticket, state, branch or ticket.branch, actor)

        fields: dict = {"state": state, "position": new_pos}
        if branch:
            fields["branch"] = branch
        fields.update(pr_fields)
        self.store.change_ticket(
            ticket_id,
            fields,
            actor=actor,
            kind="state_change",
            body=f"moved {ticket.state} → {state}",
            meta={"from": ticket.state, "to": state},
            when=_now(),
        )
        return self.get_ticket(ticket_id)

    def _git_side_effects(
        self, ticket: Ticket, to_state: str, branch: str | None, actor: str
    ) -> dict:
        """Run GitHub PR side effects for a transition; return ticket fields to persist.

        A no-op unless GitHub automation is configured, the ticket has a SoloPM branch,
        and the project has a repo. Raises on a git/gh failure so the caller aborts the
        transition.
        """
        if self.github is None or not branch:
            return {}
        project = self.get_project(ticket.project)
        if not project.repo:
            return {}
        repo, base = project.repo, project.master_branch
        if to_state == "in-ai-review":
            # Git automation is agent-only: a human reaching in-ai-review (or supplying a
            # branch) must not push or open a PR.
            if actor == "human":
                return {}
            self.github.push_branch(repo, branch)
            pr = self.github.open_or_refresh_pr(
                repo, branch, base, f"{ticket.id}: {ticket.title}", ticket.description or ""
            )
            return {"pr_number": pr.number, "pr_url": pr.url, "pr_state": pr.state}
        if to_state in ("done", "cancelled"):
            # Merge/close the recorded PR; if none was recorded, resolve it by branch
            # (SoloPM owns the branch, so any PR on it is this ticket's).
            extra: dict = {}
            number = ticket.pr_number
            if number is None:
                found = self.github.find_pr(repo, branch)
                if found is None:
                    return {}  # nothing to merge/close
                number, extra = found.number, {"pr_number": found.number, "pr_url": found.url}
            if to_state == "done":
                self.github.merge_pr(repo, number)
                return {**extra, "pr_state": "merged"}
            self.github.close_pr(repo, number)
            return {**extra, "pr_state": "closed"}
        return {}

    def reorder_ticket(self, ticket_id: str, *, after: str | None = None, actor: str = "human") -> Ticket:
        """Reposition a ticket within its current column (cosmetic; no state change).

        ``after`` is the id of the ticket this one should sit immediately below, or
        ``None`` to move it to the top.
        """
        _require_actor(actor)
        ticket = self.get_ticket(ticket_id)
        if after == ticket_id:
            return ticket  # dropped onto itself
        new_pos = self._position_in_column(
            ticket.project, ticket.state, after, exclude_id=ticket_id
        )
        self.store.set_position(ticket_id, new_pos)
        return self.get_ticket(ticket_id)

    def assign_ticket(self, ticket_id: str, assignee: str, *, actor: str = "human") -> Ticket:
        _require_actor(actor)
        if assignee not in ASSIGNEES:
            raise ValidationError(
                f"Unknown assignee {assignee!r}: expected one of {', '.join(ASSIGNEES)}."
            )
        ticket = self.get_ticket(ticket_id)
        if ticket.assignee == assignee:
            return ticket
        self.store.change_ticket(
            ticket_id,
            {"assignee": assignee},
            actor=actor,
            kind="assignment",
            body=f"assigned {ticket.assignee} → {assignee}",
            meta={"from": ticket.assignee, "to": assignee},
            when=_now(),
        )
        return self.get_ticket(ticket_id)

    def submit_review(
        self,
        ticket_id: str,
        verdict: str,
        *,
        comment: str | None = None,
        criteria_results: list[dict] | None = None,
        actor: str = "human",
    ) -> Ticket:
        """Report an AI-review verdict on a ticket that is in ``in-ai-review``.

        ``pass`` advances the ticket to ``in-human-review``; ``fail`` records the review
        notes as a comment and kicks the ticket back to ``in-progress`` for the
        implementing agent to address. An optional ``comment`` carries the review notes.

        ``criteria_results`` is an optional per-criterion result set — a list of
        ``{criterion_id, verdict, note}`` — recorded to the activity log (it does not
        change the overall verdict, which still gates the transition).
        """
        _require_actor(actor)
        if verdict not in ("pass", "fail"):
            raise ValidationError(f"Unknown verdict {verdict!r}: expected 'pass' or 'fail'.")
        ticket = self.get_ticket(ticket_id)
        if ticket.state != "in-ai-review":
            raise ValidationError(
                f"Ticket {ticket_id} is not in AI review (state: {ticket.state})."
            )
        results = self._validate_criteria_results(
            criteria_results, {c.id for c in ticket.acceptance_criteria}
        )
        if results:
            self.store.change_ticket(
                ticket_id,
                {},
                actor=actor,
                kind="review",
                body=f"recorded {len(results)} per-criterion review result(s)",
                meta={"results": results},
                when=_now(),
            )
        if verdict == "pass":
            # Pass is move-only — review notes are a fail/kickback concept (per the brief).
            return self.move_ticket(ticket_id, "in-human-review", actor=actor)
        # Fail: record the review notes (if any), then kick back to the implementer.
        if comment and comment.strip():
            self.comment_ticket(ticket_id, body=comment, actor=actor)
        return self.move_ticket(ticket_id, "in-progress", actor=actor)

    @staticmethod
    def _validate_criteria_results(results: list[dict] | None, valid_ids: set[str]) -> list[dict]:
        if not results:
            return []
        clean: list[dict] = []
        for r in results:
            cid = r.get("criterion_id")
            verdict = r.get("verdict")
            if not cid:
                raise ValidationError("Each criteria result needs a 'criterion_id'.")
            if cid not in valid_ids:
                # Audit data must reference a real criterion on this ticket — a typo or
                # stale id would otherwise be recorded silently.
                raise ValidationError(f"Unknown criterion {cid!r} for this ticket.")
            if verdict not in ("pass", "fail"):
                raise ValidationError(
                    f"Criterion {cid} result verdict must be 'pass' or 'fail', got {verdict!r}."
                )
            clean.append({"criterion_id": cid, "verdict": verdict, "note": r.get("note")})
        return clean

    # --- acceptance criteria ------------------------------------------------
    #
    # Each mutation is applied through ``store.mutate_criteria`` so the read-modify-write
    # happens inside one write transaction — concurrent CLI/web/MCP edits to the same
    # ticket serialize and can't lose each other's updates. Input validation (actor, text)
    # runs here, up front; the closure does the id allocation / lookup atomically.

    @staticmethod
    def _next_criterion_id(criteria: list[dict]) -> str:
        nums = [int(c["id"][1:]) for c in criteria if str(c["id"])[1:].isdigit()]
        return f"c{(max(nums) + 1) if nums else 1}"

    @staticmethod
    def _criterion(criteria: list[dict], criterion_id: str, ticket_id: str) -> dict:
        for c in criteria:
            if c["id"] == criterion_id:
                return c
        raise NotFoundError(f"Criterion {criterion_id!r} not found on {ticket_id}.")

    def add_criterion(self, ticket_id: str, text: str, *, actor: str = "human") -> Ticket:
        _require_actor(actor)
        if not text or not text.strip():
            raise ValidationError("Criterion text is required.")
        text = text.strip()

        def mutate(criteria: list[dict]):
            cid = self._next_criterion_id(criteria)
            criteria.append({"id": cid, "text": text, "done": False})
            return criteria, "criteria", f"added acceptance criterion: {text}", {"op": "add", "id": cid}

        self.store.mutate_criteria(ticket_id, mutate, actor=actor, when=_now())
        return self.get_ticket(ticket_id)

    def edit_criterion(
        self, ticket_id: str, criterion_id: str, text: str, *, actor: str = "human"
    ) -> Ticket:
        _require_actor(actor)
        if not text or not text.strip():
            raise ValidationError("Criterion text is required.")
        text = text.strip()

        def mutate(criteria: list[dict]):
            self._criterion(criteria, criterion_id, ticket_id)["text"] = text
            return criteria, "criteria", f"edited acceptance criterion {criterion_id}", {"op": "edit", "id": criterion_id}

        self.store.mutate_criteria(ticket_id, mutate, actor=actor, when=_now())
        return self.get_ticket(ticket_id)

    def remove_criterion(self, ticket_id: str, criterion_id: str, *, actor: str = "human") -> Ticket:
        _require_actor(actor)

        def mutate(criteria: list[dict]):
            removed = self._criterion(criteria, criterion_id, ticket_id)
            kept = [c for c in criteria if c["id"] != criterion_id]
            return kept, "criteria", f"removed acceptance criterion: {removed['text']}", {"op": "remove", "id": criterion_id}

        self.store.mutate_criteria(ticket_id, mutate, actor=actor, when=_now())
        return self.get_ticket(ticket_id)

    def check_criterion(
        self, ticket_id: str, criterion_id: str, done: bool = True, *, actor: str = "human"
    ) -> Ticket:
        _require_actor(actor)

        def mutate(criteria: list[dict]):
            crit = self._criterion(criteria, criterion_id, ticket_id)
            crit["done"] = bool(done)
            verb = "checked" if crit["done"] else "unchecked"
            return criteria, "criteria", f"{verb} acceptance criterion: {crit['text']}", {"op": "check", "id": criterion_id, "done": crit["done"]}

        self.store.mutate_criteria(ticket_id, mutate, actor=actor, when=_now())
        return self.get_ticket(ticket_id)
