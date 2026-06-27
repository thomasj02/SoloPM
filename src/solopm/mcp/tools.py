"""The SoloPM MCP tool logic — a thin layer over the canonical service.

Kept free of any MCP-SDK import so it is trivially testable on its own; the FastMCP
wiring lives in :mod:`solopm.mcp.server`. Domain failures are returned as the same
``{"error": {"code", "message"}}`` shape the HTTP API and CLI use, so the calling agent
gets a parseable result instead of a tool exception.
"""

from __future__ import annotations

import functools
from typing import Callable

from ..core.errors import SoloPMError
from ..core.models import ASSIGNEES, STATE_LABELS, STATES
from ..core.service import Service
from ..core.workflow import TRANSITIONS


def _safe(fn: Callable) -> Callable:
    @functools.wraps(fn)
    def wrapper(self: "SoloPMTools", *args, **kwargs):
        try:
            return fn(self, *args, **kwargs)
        except SoloPMError as exc:
            return exc.to_dict()

    return wrapper


class SoloPMTools:
    """Agent-facing operations. Writes are attributed to ``agent`` (default ``claude``)."""

    def __init__(self, service: Service, agent: str = "claude"):
        self.svc = service
        self.agent = agent

    def workflow_info(self) -> dict:
        return {
            "states": list(STATES),
            "state_labels": STATE_LABELS,
            "assignees": list(ASSIGNEES),
            "transitions": {s: list(t) for s, t in TRANSITIONS.items()},
            "rules": (
                "Only the human may move a ticket to 'done' (agents cannot close a "
                "ticket). Agents may reach 'in-ai-review' and 'in-human-review'. "
                "'cancelled' is reachable from any non-terminal state."
            ),
        }

    @_safe
    def list_projects(self) -> dict:
        return {"projects": [p.to_dict() for p in self.svc.list_projects()]}

    @_safe
    def list_tickets(
        self,
        project: str | None = None,
        state: str | None = None,
        assignee: str | None = None,
    ) -> dict:
        tickets = self.svc.list_tickets(project=project, state=state, assignee=assignee)
        return {"tickets": [t.to_summary() for t in tickets]}

    @_safe
    def show_ticket(self, ticket_id: str) -> dict:
        return self.svc.get_ticket(ticket_id).to_dict()

    @_safe
    def create_ticket(
        self,
        project: str,
        title: str,
        description: str = "",
        state: str = "backlog",
        assignee: str = "unassigned",
    ) -> dict:
        return self.svc.create_ticket(
            project=project,
            title=title,
            description=description,
            state=state,
            assignee=assignee,
            actor=self.agent,
        ).to_dict()

    @_safe
    def edit_ticket(
        self, ticket_id: str, title: str | None = None, description: str | None = None
    ) -> dict:
        return self.svc.edit_ticket(
            ticket_id, title=title, description=description, actor=self.agent
        ).to_dict()

    @_safe
    def comment_ticket(self, ticket_id: str, body: str) -> dict:
        return self.svc.comment_ticket(ticket_id, body=body, actor=self.agent).to_dict()

    @_safe
    def move_ticket(self, ticket_id: str, state: str, branch: str | None = None) -> dict:
        return self.svc.move_ticket(
            ticket_id, state, branch=branch, actor=self.agent
        ).to_dict()

    @_safe
    def assign_ticket(self, ticket_id: str, assignee: str) -> dict:
        return self.svc.assign_ticket(ticket_id, assignee, actor=self.agent).to_dict()

    @_safe
    def submit_review(
        self,
        ticket_id: str,
        verdict: str,
        comment: str | None = None,
        criteria_results: list[dict] | None = None,
    ) -> dict:
        return self.svc.submit_review(
            ticket_id,
            verdict,
            comment=comment,
            criteria_results=criteria_results,
            actor=self.agent,
        ).to_dict()

    @_safe
    def radar(self, project: str | None = None) -> dict:
        return self.svc.compute_radar(project)

    @_safe
    def list_review_memory(self, project: str, status: str | None = None) -> dict:
        return {"items": self.svc.list_review_memory(project, status=status)}

    @_safe
    def add_review_memory(self, project: str, text: str, status: str = "active") -> dict:
        return self.svc.add_review_memory(project, text, status=status)

    @_safe
    def update_review_memory(
        self, project: str, item_id: str, text: str | None = None, status: str | None = None
    ) -> dict:
        return self.svc.update_review_memory(project, item_id, text=text, status=status)

    @_safe
    def review_prompt(self, project: str, record_hit: bool = False) -> dict:
        return {"prompt": self.svc.assembled_review_prompt(project, record_hit=record_hit)}

    @_safe
    def add_criterion(self, ticket_id: str, text: str) -> dict:
        return self.svc.add_criterion(ticket_id, text, actor=self.agent).to_dict()

    @_safe
    def check_criterion(self, ticket_id: str, criterion_id: str, done: bool = True) -> dict:
        return self.svc.check_criterion(ticket_id, criterion_id, done, actor=self.agent).to_dict()

    @_safe
    def edit_criterion(self, ticket_id: str, criterion_id: str, text: str) -> dict:
        return self.svc.edit_criterion(ticket_id, criterion_id, text, actor=self.agent).to_dict()

    @_safe
    def remove_criterion(self, ticket_id: str, criterion_id: str) -> dict:
        return self.svc.remove_criterion(ticket_id, criterion_id, actor=self.agent).to_dict()

    @_safe
    def link_ticket(self, ticket_id: str, type: str, other_id: str) -> dict:
        return self.svc.link_tickets(ticket_id, type, other_id, actor=self.agent).to_dict()

    @_safe
    def unlink_ticket(
        self,
        ticket_id: str,
        other_id: str,
        type: str | None = None,
        direction: str | None = None,
    ) -> dict:
        return self.svc.unlink_tickets(
            ticket_id, other_id, type=type, direction=direction, actor=self.agent
        ).to_dict()
