"""Thin HTTP client the CLI uses to talk to the local backend.

Translates HTTP failures into :class:`ApiError`, which carries the backend's
``{code, message}`` so the CLI can render the ``--json`` failure contract faithfully.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from urllib.parse import quote

import httpx

from ..core.errors import SoloPMError
from ..core.github import GitHub


class ApiError(Exception):
    """A backend or transport failure, with a stable code for the JSON contract."""

    def __init__(self, code: str, message: str, status: int | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status

    def to_dict(self) -> dict:
        return {"error": {"code": self.code, "message": self.message}}


class Api:
    """A small wrapper over httpx with actor attribution and uniform error handling."""

    def __init__(self, base_url: str, *, agent: str | None = None, client: httpx.Client | None = None):
        self.base_url = base_url.rstrip("/")
        self.agent = agent
        # Generous read timeout: some endpoints shell out to git/gh (move with PR
        # automation, prune); connect stays snappy so a dead backend fails fast.
        self._client = client or httpx.Client(
            base_url=self.base_url, timeout=httpx.Timeout(300.0, connect=5.0)
        )

    def _headers(self) -> dict:
        if self.agent:
            return {"X-SoloPM-Actor": self.agent}
        return {}

    def _request(self, method: str, path: str, **kwargs) -> Any:
        try:
            resp = self._client.request(method, path, headers=self._headers(), **kwargs)
        except httpx.ConnectError as exc:
            raise ApiError(
                "unreachable",
                f"Could not reach the SoloPM backend at {self.base_url}. "
                "Is `solopm serve` running?",
            ) from exc
        except httpx.HTTPError as exc:
            raise ApiError("transport", f"Request failed: {exc}") from exc

        # >= 300 (not just >= 400): the API never redirects, so a 3xx means we're
        # talking to something else (SSO portal, http->https hop) — surface it.
        if resp.status_code >= 300:
            payload = _safe_json(resp)
            if isinstance(payload, dict) and isinstance(payload.get("error"), dict):
                err = payload["error"]
                raise ApiError(
                    err.get("code", "error"),
                    err.get("message", resp.text),
                    status=resp.status_code,
                )
            raise ApiError("http_error", resp.text or f"HTTP {resp.status_code}", status=resp.status_code)

        if resp.status_code == 204 or not resp.content:
            return {}
        try:
            return resp.json()
        except Exception as exc:
            raise ApiError(
                "invalid_response",
                f"The server at {self.base_url} returned a non-JSON response — "
                "is that really the SoloPM API?",
                status=resp.status_code,
            ) from exc

    def get(self, path: str, **kwargs) -> Any:
        return self._request("GET", path, **kwargs)

    def post(self, path: str, json: dict | None = None, **kwargs) -> Any:
        return self._request("POST", path, json=json, **kwargs)

    def patch(self, path: str, json: dict | None = None, **kwargs) -> Any:
        return self._request("PATCH", path, json=json, **kwargs)

    def delete(self, path: str, **kwargs) -> Any:
        return self._request("DELETE", path, **kwargs)

    def close(self) -> None:
        # Closing transport must never surface as a command failure.
        try:
            self._client.close()
        except Exception:
            pass


def _safe_json(resp: httpx.Response) -> Any:
    try:
        return resp.json()
    except Exception:
        return None


def _path_seg(value: str) -> str:
    """Percent-encode a URL path segment, rejecting empty / '/' / '.' / '..' outright.

    The same rule as ``http_tools._seg`` (project standard from SOLO-26): quoting alone
    is insufficient — ASGI decodes %2F before routing and httpx dot-normalizes the
    path, so these values silently change the route instead of erroring. Raised as
    :class:`ApiError` (code ``validation``, same message shape) — this layer's native
    failure, rendered uniformly by the CLI and the HTTP MCP.
    """
    value = str(value)
    if not value or "/" in value or value in (".", ".."):
        raise ApiError("validation", f"Invalid path value {value!r}.")
    return quote(value, safe="")


def push_branch_for_remote_move(api: Api, ticket_id: str, state: str, branch: str | None) -> None:
    """The client half of the remote-project PR lifecycle (SOLO-29).

    For a project with ``github_repo`` set the backend has no checkout — the commits
    exist only on the machine running this client — so an agent move to
    ``in-ai-review`` must push the ticket's branch from HERE before the move API call
    (the backend then verifies it on origin and opens or refreshes the PR). The branch
    to push is the explicit ``branch`` argument or, mirroring the server's
    ``branch or ticket.branch`` fallback, the ticket's recorded branch — a re-review
    move that omits the (already pinned) branch must still push the fix commits, or
    the PR would be reviewed and merged stale.

    A no-op for every other transition, for local projects, and for HUMAN moves: git
    automation is agent-only (the backend's actor gate skips it for humans too), and a
    human recording a branch may be on a machine without the checkout. Raises
    :class:`ApiError` on any failure so the caller never reaches the move: pushing
    after a failed move (or moving after a failed push) would leave the lifecycle
    half-run.

    Trade-off: if the move itself is later rejected (bad transition, validation), the
    branch has already been pushed — harmless, it just sits on origin with no PR.
    """
    if state != "in-ai-review":
        return
    if not api.agent or api.agent == "human":
        return  # agent-only, matching the backend's actor gate
    ticket = api.get(f"/api/tickets/{_path_seg(ticket_id)}")
    branch = branch or str((ticket or {}).get("branch") or "") or None
    if not branch:
        return  # genuinely branchless move: there is no push half anywhere
    project_key = str((ticket or {}).get("project") or "")
    if not project_key:
        return  # malformed payload — let the move endpoint produce the real error
    project = api.get(f"/api/projects/{_path_seg(project_key)}")
    if not (project or {}).get("github_repo"):
        return  # local project: the backend pushes from its own checkout, as always
    repo = str(project.get("repo") or "")
    repo_path = Path(repo).expanduser() if repo else None
    if repo_path is None or not repo_path.is_dir():
        raise ApiError(
            "github",
            f"Project {project_key} is remote (github_repo set) and its repo path "
            f"{repo!r} was not found on this machine — the branch push must run where "
            f"the commits live. Fix the project's repo to this machine's checkout path "
            f"(`solopm project set {project_key} repo <path>`), or run the move from "
            "the machine that has the checkout.",
        )
    try:
        GitHub().push_branch(str(repo_path), branch)
    except SoloPMError as exc:
        # GitHubError (push failed) or ValidationError (bad branch name) — either way
        # ApiError is this layer's native failure, rendered uniformly by CLI and MCP.
        raise ApiError(getattr(exc, "code", "github"), str(exc)) from exc
