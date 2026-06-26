"""FastAPI application: the canonical HTTP API + the served web app.

The API is a thin wrapper over :class:`solopm.core.service.Service`. Domain errors are
translated to ``{"error": {...}}`` JSON with the right status code, so both the web app
and the CLI get a uniform failure contract.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import Body, Depends, FastAPI, Header, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.trustedhost import TrustedHostMiddleware

from .. import __version__, config
from ..core.errors import SoloPMError, ValidationError
from ..core.github import GitHub
from ..core.models import (
    ACTORS,
    ASSIGNEES,
    STATE_LABELS,
    STATES,
)
from ..core.service import Service
from ..core.store import Store
from ..core.workflow import TRANSITIONS
from .schemas import (
    AssignRequest,
    CommentCreate,
    MoveRequest,
    ProjectCreate,
    ReorderRequest,
    TicketCreate,
    TicketPatch,
)

# The web app is the built output of the Vite + TypeScript project in ../../frontend.
# Run `npm --prefix frontend run build` to (re)generate it.
WEB_DIR = Path(__file__).resolve().parent.parent / "web" / "dist"


def get_service(request: Request) -> Service:
    return request.app.state.service


def get_actor(x_solopm_actor: str | None = Header(default=None)) -> str:
    """Resolve the acting identity from the attribution header (default ``human``)."""
    if x_solopm_actor is None or x_solopm_actor.strip() == "":
        return "human"
    actor = x_solopm_actor.strip().lower()
    if actor not in ACTORS:
        raise ValidationError(
            f"Unknown actor {actor!r}: expected one of {', '.join(ACTORS)}."
        )
    return actor


def create_app(
    service: Service | None = None, allowed_hosts: list[str] | None = None
) -> FastAPI:
    app = FastAPI(title="SoloPM", version=__version__)

    if service is None:
        store = Store(config.db_path())
        store.init()  # lazily create the store so `serve` works without explicit `init`
        service = Service(store, github=GitHub())  # enable Tier-1 PR automation
    app.state.service = service

    # SoloPM is local-first; reject requests bearing a foreign Host header to close the
    # DNS-rebinding vector against the unauthenticated local API. Tests pass ["*"].
    if allowed_hosts is None:
        allowed_hosts = sorted(
            {"127.0.0.1", "localhost", "::1", config.server_host()}
        )
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=allowed_hosts)

    # --- error translation --------------------------------------------------

    @app.exception_handler(SoloPMError)
    async def _domain_error_handler(_: Request, exc: SoloPMError) -> JSONResponse:
        return JSONResponse(status_code=exc.status, content=exc.to_dict())

    @app.exception_handler(RequestValidationError)
    async def _request_validation_handler(
        _: Request, exc: RequestValidationError
    ) -> JSONResponse:
        # Surface malformed/missing fields through the same {error:{...}} contract.
        first = exc.errors()[0] if exc.errors() else {}
        loc = ".".join(str(p) for p in first.get("loc", []) if p != "body")
        msg = first.get("msg", "Invalid request.")
        message = f"{loc}: {msg}" if loc else msg
        return JSONResponse(
            status_code=422, content={"error": {"code": "validation", "message": message}}
        )

    @app.exception_handler(Exception)
    async def _unhandled_handler(_: Request, exc: Exception) -> JSONResponse:
        # Safety net: no endpoint may leak a bare 500 outside the {error:{...}} contract.
        return JSONResponse(
            status_code=500,
            content={"error": {"code": "internal", "message": "Internal server error."}},
        )

    # --- meta ---------------------------------------------------------------

    @app.get("/api/meta")
    def meta() -> dict:
        return {
            "version": __version__,
            "states": list(STATES),
            "state_labels": STATE_LABELS,
            "assignees": list(ASSIGNEES),
            "actors": list(ACTORS),
            "transitions": {s: list(t) for s, t in TRANSITIONS.items()},
        }

    @app.get("/api/health")
    def health() -> dict:
        return {"ok": True, "version": __version__}

    # --- projects -----------------------------------------------------------

    @app.get("/api/projects")
    def list_projects(svc: Service = Depends(get_service)) -> dict:
        return {"projects": [p.to_dict() for p in svc.list_projects()]}

    @app.post("/api/projects", status_code=201)
    def create_project(payload: ProjectCreate, svc: Service = Depends(get_service)) -> dict:
        project = svc.add_project(
            key=payload.key,
            name=payload.name,
            repo=payload.repo,
            master=payload.master,
        )
        return project.to_dict()

    @app.get("/api/projects/{key}")
    def get_project(key: str, svc: Service = Depends(get_service)) -> dict:
        return svc.get_project(key).to_dict()

    @app.patch("/api/projects/{key}")
    def patch_project(
        key: str,
        body: dict[str, Any] = Body(...),
        svc: Service = Depends(get_service),
    ) -> dict:
        # Accept either {"field": ..., "value": ...} or a partial object of fields.
        if set(body.keys()) == {"field", "value"}:
            if not isinstance(body["field"], str):
                raise ValidationError("'field' must be a string.")
            fields = {body["field"]: body["value"]}
        else:
            fields = body
        if not fields:
            raise ValidationError("No fields to update.")
        return svc.update_project(key, fields).to_dict()

    # --- tickets ------------------------------------------------------------

    @app.get("/api/tickets")
    def list_tickets(
        project: str | None = None,
        state: str | None = None,
        assignee: str | None = None,
        svc: Service = Depends(get_service),
    ) -> dict:
        tickets = svc.list_tickets(project=project, state=state, assignee=assignee)
        return {"tickets": [t.to_summary() for t in tickets]}

    @app.post("/api/tickets", status_code=201)
    def create_ticket(
        payload: TicketCreate,
        svc: Service = Depends(get_service),
        actor: str = Depends(get_actor),
    ) -> dict:
        ticket = svc.create_ticket(
            project=payload.project,
            title=payload.title,
            description=payload.description,
            state=payload.state,
            assignee=payload.assignee,
            actor=actor,
        )
        return ticket.to_dict()

    @app.get("/api/tickets/{ticket_id}")
    def get_ticket(ticket_id: str, svc: Service = Depends(get_service)) -> dict:
        return svc.get_ticket(ticket_id).to_dict()

    @app.patch("/api/tickets/{ticket_id}")
    def edit_ticket(
        ticket_id: str,
        payload: TicketPatch,
        svc: Service = Depends(get_service),
        actor: str = Depends(get_actor),
    ) -> dict:
        ticket = svc.edit_ticket(
            ticket_id,
            title=payload.title,
            description=payload.description,
            actor=actor,
        )
        return ticket.to_dict()

    @app.post("/api/tickets/{ticket_id}/comments", status_code=201)
    def comment_ticket(
        ticket_id: str,
        payload: CommentCreate,
        svc: Service = Depends(get_service),
        actor: str = Depends(get_actor),
    ) -> dict:
        activity = svc.comment_ticket(ticket_id, body=payload.body, actor=actor)
        return activity.to_dict()

    @app.post("/api/tickets/{ticket_id}/move")
    def move_ticket(
        ticket_id: str,
        payload: MoveRequest,
        svc: Service = Depends(get_service),
        actor: str = Depends(get_actor),
    ) -> dict:
        # Distinguish an omitted `after` (→ bottom) from an explicit null (→ top).
        if "after" in payload.model_fields_set:
            ticket = svc.move_ticket(
                ticket_id, payload.state, after=payload.after, branch=payload.branch, actor=actor
            )
        else:
            ticket = svc.move_ticket(
                ticket_id, payload.state, branch=payload.branch, actor=actor
            )
        return ticket.to_dict()

    @app.post("/api/tickets/{ticket_id}/assign")
    def assign_ticket(
        ticket_id: str,
        payload: AssignRequest,
        svc: Service = Depends(get_service),
        actor: str = Depends(get_actor),
    ) -> dict:
        return svc.assign_ticket(ticket_id, payload.assignee, actor=actor).to_dict()

    @app.post("/api/tickets/{ticket_id}/reorder")
    def reorder_ticket(
        ticket_id: str,
        payload: ReorderRequest,
        svc: Service = Depends(get_service),
        actor: str = Depends(get_actor),
    ) -> dict:
        return svc.reorder_ticket(ticket_id, after=payload.after, actor=actor).to_dict()

    # --- static web app (mounted last so /api wins) -------------------------

    if WEB_DIR.is_dir() and (WEB_DIR / "index.html").exists():
        app.mount("/", StaticFiles(directory=str(WEB_DIR), html=True), name="web")
    else:

        @app.get("/", response_class=HTMLResponse)
        def _web_not_built() -> str:
            return (
                "<!doctype html><meta charset=utf-8><title>SoloPM</title>"
                "<body style='font-family:system-ui;max-width:42rem;margin:4rem auto;"
                "color:#e6e6e9;background:#0d0d11'>"
                "<h1>SoloPM</h1><p>The web app hasn't been built yet. From the repo root run:</p>"
                "<pre style='background:#17171c;padding:1rem;border-radius:8px'>"
                "npm --prefix frontend install\nnpm --prefix frontend run build</pre>"
                "<p>then reload this page. The API is already running at "
                "<code>/api</code>.</p></body>"
            )

    return app
