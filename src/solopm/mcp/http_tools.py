"""HTTP-backed MCP tool logic (SOLO-26) — the same tool surface as
:class:`~solopm.mcp.tools.SoloPMTools`, served by a remote backend over the HTTP API.

Each method issues the canonical endpoint's request and returns the response body
verbatim: the server already applies the exact transformations the in-process tools
layer applies, so no reshaping happens here (and none may be added — parity is
enforced by tests/test_mcp_http.py). Failures — domain errors relayed by the backend
as well as transport failures — come back as the same ``{"error": {"code", "message"}}``
dict the in-process ``_safe`` produces. Attribution rides the ``X-SoloPM-Actor``
header via :class:`~solopm.cli.client.Api`.
"""

from __future__ import annotations

import functools
from typing import Callable
from urllib.parse import quote

from ..cli.client import Api, ApiError
from ..core.errors import SoloPMError, ValidationError
from ..core.models import DEFAULT_BRANCH_CONVENTION, DEFAULT_REVIEW_PROMPT
from . import tools as _tools


def _safe(fn: Callable) -> Callable:
    @functools.wraps(fn)
    def wrapper(self: "HttpSoloPMTools", *args, **kwargs):
        try:
            return fn(self, *args, **kwargs)
        except (SoloPMError, ApiError) as exc:
            return exc.to_dict()

    return wrapper


def _seg(value: str) -> str:
    """Percent-encode a path segment so a crafted id can't smuggle query params.

    '/' can't survive the round-trip (ASGI decodes %2F before the router splits the
    path) and an empty segment changes the route, so both are rejected up front as
    domain validation errors instead of leaking router-level ``{"detail": ...}`` bodies.
    '.' and '..' are unreserved (quote passes them through) and httpx dot-normalizes
    the path — show_ticket('.') would hit GET /api/tickets and "succeed" with the list.
    """
    value = str(value)
    if not value or "/" in value or value in (".", ".."):
        raise ValidationError(f"Invalid path value {value!r}.")
    return quote(value, safe="")


def _compact(mapping: dict) -> dict:
    """Drop None-valued entries — omitted and null diverge on some endpoints."""
    return {k: v for k, v in mapping.items() if v is not None}


class HttpSoloPMTools:
    """Agent-facing operations over a remote backend. The ``api``'s agent identity is
    sent as the attribution header on every request (ignored by unattributed routes)."""

    def __init__(self, api: Api):
        self.api = api

    def workflow_info(self) -> dict:
        # Static workflow facts — identical in both modes, no round-trip needed.
        return _tools.workflow_info()

    @_safe
    def list_projects(self) -> dict:
        return self.api.get("/api/projects")

    @_safe
    def create_project(
        self,
        key: str,
        name: str,
        repo: str | None = None,
        master: str = "main",
        branch_convention: str = DEFAULT_BRANCH_CONVENTION,
        default_implementer: str = "claude",
        default_reviewer: str = "codex",
        review_prompt: str = DEFAULT_REVIEW_PROMPT,
    ) -> dict:
        return self.api.post(
            "/api/projects",
            json={
                "key": key,
                "name": name,
                "repo": repo,
                "master": master,
                "branch_convention": branch_convention,
                "default_implementer": default_implementer,
                "default_reviewer": default_reviewer,
                "review_prompt": review_prompt,
            },
        )

    @_safe
    def edit_project(
        self,
        key: str,
        name: str | None = None,
        repo: str | None = None,
        master_branch: str | None = None,
        branch_convention: str | None = None,
        default_implementer: str | None = None,
        default_reviewer: str | None = None,
        review_prompt: str | None = None,
    ) -> dict:
        fields = _compact(
            {
                "name": name,
                "repo": repo,
                "master_branch": master_branch,
                "branch_convention": branch_convention,
                "default_implementer": default_implementer,
                "default_reviewer": default_reviewer,
                "review_prompt": review_prompt,
            }
        )
        if not fields:
            raise ValidationError(
                "Provide at least one field to edit: name, repo, master_branch, "
                "branch_convention, default_implementer, default_reviewer, review_prompt."
            )
        return self.api.patch(f"/api/projects/{_seg(key)}", json=fields)

    @_safe
    def delete_project(self, key: str, force: bool = False) -> dict:
        return self.api.delete(f"/api/projects/{_seg(key)}", params={"force": force})

    @_safe
    def prune_merged_branches(self, project: str, apply: bool = False) -> dict:
        return self.api.post(f"/api/projects/{_seg(project)}/prune", json={"apply": apply})

    @_safe
    def list_tickets(
        self,
        project: str | None = None,
        state: str | None = None,
        assignee: str | None = None,
        tags: list[str] | None = None,
    ) -> dict:
        params = _compact({"project": project, "state": state, "assignee": assignee})
        if tags:
            params["tag"] = tags  # repeated query param, one per tag
        return self.api.get("/api/tickets", params=params)

    @_safe
    def show_ticket(self, ticket_id: str) -> dict:
        return self.api.get(f"/api/tickets/{_seg(ticket_id)}")

    @_safe
    def create_ticket(
        self,
        project: str,
        title: str,
        description: str = "",
        state: str = "backlog",
        assignee: str = "unassigned",
    ) -> dict:
        return self.api.post(
            "/api/tickets",
            json={
                "project": project,
                "title": title,
                "description": description,
                "state": state,
                "assignee": assignee,
            },
        )

    @_safe
    def edit_ticket(
        self, ticket_id: str, title: str | None = None, description: str | None = None
    ) -> dict:
        return self.api.patch(
            f"/api/tickets/{_seg(ticket_id)}",
            json=_compact({"title": title, "description": description}),
        )

    @_safe
    def comment_ticket(self, ticket_id: str, body: str) -> dict:
        return self.api.post(f"/api/tickets/{_seg(ticket_id)}/comments", json={"body": body})

    @_safe
    def move_ticket(
        self, ticket_id: str, state: str, branch: str | None = None, after: str | None = None
    ) -> dict:
        # `after` must be OMITTED when None: the endpoint reads model_fields_set, and an
        # explicit null means "top of column" while an absent key means "bottom" — the
        # same distinction tools.py preserves by dropping the kwarg (see tools.py:175).
        body = _compact({"state": state, "branch": branch, "after": after})
        return self.api.post(f"/api/tickets/{_seg(ticket_id)}/move", json=body)

    @_safe
    def reorder_ticket(self, ticket_id: str, after: str | None = None) -> dict:
        # Unlike move, reorder's null and omitted both mean "top", so pass it through.
        return self.api.post(f"/api/tickets/{_seg(ticket_id)}/reorder", json={"after": after})

    @_safe
    def assign_ticket(self, ticket_id: str, assignee: str) -> dict:
        return self.api.post(f"/api/tickets/{_seg(ticket_id)}/assign", json={"assignee": assignee})

    @_safe
    def submit_review(
        self,
        ticket_id: str,
        verdict: str,
        comment: str | None = None,
        criteria_results: list[dict] | None = None,
    ) -> dict:
        body = _compact({"comment": comment, "criteria_results": criteria_results})
        body["verdict"] = verdict
        return self.api.post(f"/api/tickets/{_seg(ticket_id)}/review", json=body)

    @_safe
    def radar(self, project: str | None = None) -> dict:
        return self.api.get("/api/radar", params=_compact({"project": project}))

    @_safe
    def graph(
        self,
        project: str | None = None,
        around: str | None = None,
        depth: int = 1,
        active_only: bool = False,
        types: list[str] | None = None,
    ) -> dict:
        params = _compact({"project": project, "around": around})
        params["depth"] = depth
        params["active_only"] = active_only
        if types:
            params["type"] = types  # repeated query param; note the singular name
        return self.api.get("/api/graph", params=params)

    @_safe
    def list_review_memory(self, project: str, status: str | None = None) -> dict:
        return self.api.get(
            f"/api/projects/{_seg(project)}/review-memory", params=_compact({"status": status})
        )

    @_safe
    def add_review_memory(self, project: str, text: str, status: str = "active") -> dict:
        return self.api.post(
            f"/api/projects/{_seg(project)}/review-memory",
            json={"text": text, "status": status},
        )

    @_safe
    def update_review_memory(
        self, project: str, item_id: str, text: str | None = None, status: str | None = None
    ) -> dict:
        return self.api.patch(
            f"/api/projects/{_seg(project)}/review-memory/{_seg(item_id)}",
            json=_compact({"text": text, "status": status}),
        )

    @_safe
    def review_prompt(self, project: str, record_hit: bool = False) -> dict:
        return self.api.get(
            f"/api/projects/{_seg(project)}/review-prompt", params={"record_hit": record_hit}
        )

    @_safe
    def add_criterion(self, ticket_id: str, text: str) -> dict:
        return self.api.post(f"/api/tickets/{_seg(ticket_id)}/criteria", json={"text": text})

    @_safe
    def check_criterion(self, ticket_id: str, criterion_id: str, done: bool = True) -> dict:
        return self.api.patch(
            f"/api/tickets/{_seg(ticket_id)}/criteria/{_seg(criterion_id)}", json={"done": done}
        )

    @_safe
    def edit_criterion(self, ticket_id: str, criterion_id: str, text: str) -> dict:
        return self.api.patch(
            f"/api/tickets/{_seg(ticket_id)}/criteria/{_seg(criterion_id)}", json={"text": text}
        )

    @_safe
    def remove_criterion(self, ticket_id: str, criterion_id: str) -> dict:
        return self.api.delete(f"/api/tickets/{_seg(ticket_id)}/criteria/{_seg(criterion_id)}")

    @_safe
    def tag_ticket(self, ticket_id: str, tags: list[str]) -> dict:
        return self.api.post(f"/api/tickets/{_seg(ticket_id)}/tags", json={"tags": tags})

    @_safe
    def untag_ticket(self, ticket_id: str, tag: str) -> dict:
        return self.api.delete(f"/api/tickets/{_seg(ticket_id)}/tags/{_seg(tag)}")

    @_safe
    def link_ticket(self, ticket_id: str, type: str, other_id: str) -> dict:
        # The endpoint's body field is `other`, not `other_id`.
        return self.api.post(
            f"/api/tickets/{_seg(ticket_id)}/links", json={"type": type, "other": other_id}
        )

    @_safe
    def unlink_ticket(
        self,
        ticket_id: str,
        other_id: str,
        type: str | None = None,
        direction: str | None = None,
    ) -> dict:
        return self.api.delete(
            f"/api/tickets/{_seg(ticket_id)}/links/{_seg(other_id)}",
            params=_compact({"type": type, "direction": direction}),
        )
