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
    def move_ticket(self, ticket_id: str, state: str) -> dict:
        return self.svc.move_ticket(ticket_id, state, actor=self.agent).to_dict()

    @_safe
    def assign_ticket(self, ticket_id: str, assignee: str) -> dict:
        return self.svc.assign_ticket(ticket_id, assignee, actor=self.agent).to_dict()
