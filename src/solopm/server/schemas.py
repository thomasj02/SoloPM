"""Pydantic request models for the HTTP API."""

from __future__ import annotations

from pydantic import BaseModel, Field


class ProjectCreate(BaseModel):
    key: str
    name: str
    repo: str | None = None
    master: str = "main"


class TicketCreate(BaseModel):
    project: str
    title: str
    description: str = ""
    state: str = "backlog"
    assignee: str = "unassigned"


class TicketPatch(BaseModel):
    title: str | None = None
    description: str | None = None


class CommentCreate(BaseModel):
    body: str = Field(..., description="Comment text.")


class MoveRequest(BaseModel):
    state: str
    # Optional position hint: omit → bottom of target column; null → top; id → below it.
    after: str | None = None
    # SoloPM branch to record (when an agent self-transitions to in-ai-review).
    branch: str | None = None


class AssignRequest(BaseModel):
    assignee: str


class ReorderRequest(BaseModel):
    after: str | None = None  # place below this ticket; None = top of the column
