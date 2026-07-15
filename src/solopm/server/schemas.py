"""Pydantic request models for the HTTP API."""

from __future__ import annotations

from pydantic import BaseModel, Field

from ..core.models import DEFAULT_BRANCH_CONVENTION, DEFAULT_REVIEW_PROMPT


class ProjectCreate(BaseModel):
    key: str
    name: str
    repo: str | None = None
    master: str = "main"
    branch_convention: str = DEFAULT_BRANCH_CONVENTION
    default_implementer: str = "claude"
    default_reviewer: str = "codex"
    review_prompt: str = DEFAULT_REVIEW_PROMPT


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


class CriterionResult(BaseModel):
    criterion_id: str
    verdict: str  # "pass" | "fail"
    note: str | None = None


class ReviewRequest(BaseModel):
    verdict: str  # "pass" | "fail"
    comment: str | None = None
    criteria_results: list[CriterionResult] | None = None


class LinkCreate(BaseModel):
    type: str  # blocks | related | duplicate | parent
    other: str  # the other ticket id


class TagsBody(BaseModel):
    tags: list[str]  # one or more tags to add (normalized server-side)


class PruneRequest(BaseModel):
    apply: bool = False  # dry-run unless true


class CriterionCreate(BaseModel):
    text: str


class CriterionPatch(BaseModel):
    text: str | None = None
    done: bool | None = None


class ReviewMemoryCreate(BaseModel):
    text: str
    source: str = "manual"
    status: str = "active"


class ReviewMemoryPatch(BaseModel):
    text: str | None = None
    status: str | None = None
