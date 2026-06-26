"""The ``solopm`` command-line interface.

Structure: ``solopm <noun> <verb> [args] [flags]``. Nouns: ``project``, ``ticket``,
plus top-level ``init`` / ``serve``. ``--json`` emits a single structured object (the
agent contract); ``--agent <name>`` attributes the write. The CLI is a thin client of
the local backend — except ``init``/``serve``, which act on the store/server directly.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Optional

import typer
from typing_extensions import Annotated

from .. import __version__, config
from . import output
from .client import Api, ApiError

app = typer.Typer(
    name="solopm",
    help="SoloPM — an AI-first project tracker for solo developers.",
    no_args_is_help=True,
    add_completion=False,
)
project_app = typer.Typer(help="Manage projects.", no_args_is_help=True)
ticket_app = typer.Typer(help="Manage tickets.", no_args_is_help=True)
app.add_typer(project_app, name="project")
app.add_typer(ticket_app, name="ticket")

# Reusable global flags (placed on each command so they may follow positional args,
# matching the spec's `solopm ticket show SOLO-42 --json` usage).
JsonOpt = Annotated[bool, typer.Option("--json", help="Emit a single structured JSON object.")]
AgentOpt = Annotated[
    Optional[str],
    typer.Option("--agent", help="Attribute this action to an agent (e.g. claude, codex)."),
]
UrlOpt = Annotated[
    Optional[str], typer.Option("--url", help="Backend base URL (default: local server).")
]


@dataclass
class Call:
    json: bool
    agent: Optional[str]
    url: Optional[str]

    def api(self) -> Api:
        base = self.url or config.base_url()
        return Api(base, agent=self.agent)


# Test seam: overridable so the CLI can be driven against an in-process app.
def make_api(call: Call) -> Api:
    return call.api()


def _run(call: Call, fn: Callable[[Api], object], renderer: Callable[[object], None]) -> None:
    """Execute an API call, render the result, and apply the uniform error contract."""
    api = make_api(call)
    try:
        result = fn(api)
    except ApiError as exc:
        if call.json:
            output.print_error_json(exc.to_dict())
        else:
            output.err_console.print(f"[red]error[/] [grey62]({exc.code})[/]: {exc.message}")
        raise typer.Exit(code=1)
    finally:
        api.close()

    if call.json:
        output.print_json(result)
    else:
        renderer(result)


# --- top level --------------------------------------------------------------


def _version_callback(value: bool) -> None:
    if value:
        typer.echo(__version__)
        raise typer.Exit()


@app.callback()
def _root(
    version: Annotated[
        bool,
        typer.Option(
            "--version",
            help="Show version and exit.",
            callback=_version_callback,
            is_eager=True,
        ),
    ] = False,
) -> None:
    """SoloPM root command."""


@app.command()
def init(json_out: JsonOpt = False) -> None:
    """Create the local SoloPM store. Run once per machine."""
    from ..core.store import Store

    path = config.db_path()
    store = Store(path)
    store.init()
    if json_out:
        output.print_json({"ok": True, "db": str(path)})
    else:
        output.console.print(f"[green]✓[/] Initialized SoloPM store at [bold]{path}[/]")


@app.command()
def serve(
    host: Annotated[Optional[str], typer.Option(help="Host to bind.")] = None,
    port: Annotated[Optional[int], typer.Option(help="Port to bind.")] = None,
) -> None:
    """Run the local backend and serve the web app."""
    import uvicorn

    from ..server.app import create_app

    bind_host = host or config.server_host()
    bind_port = port or config.server_port()
    output.console.print(
        f"[green]SoloPM[/] serving on [bold]http://{bind_host}:{bind_port}[/]  "
        f"[grey62](store: {config.db_path()})[/]"
    )
    uvicorn.run(create_app(), host=bind_host, port=bind_port, log_level="info")


# --- project ----------------------------------------------------------------


@project_app.command("add")
def project_add(
    key: Annotated[str, typer.Option("--key", help="Project key, e.g. SOLO.")],
    name: Annotated[str, typer.Option("--name", help="Project name.")],
    repo: Annotated[Optional[str], typer.Option("--repo", help="Local repo path.")] = None,
    master: Annotated[str, typer.Option("--master", help="Master branch.")] = "main",
    json_out: JsonOpt = False,
    url: UrlOpt = None,
) -> None:
    """Register a project."""
    call = Call(json_out, None, url)
    body = {"key": key, "name": name, "repo": repo, "master": master}
    _run(call, lambda api: api.post("/api/projects", json=body), output.render_project)


@project_app.command("list")
def project_list(json_out: JsonOpt = False, url: UrlOpt = None) -> None:
    """List projects."""
    call = Call(json_out, None, url)
    _run(
        call,
        lambda api: api.get("/api/projects"),
        lambda r: output.render_projects(r["projects"]),
    )


@project_app.command("show")
def project_show(
    key: Annotated[str, typer.Argument(help="Project key.")],
    json_out: JsonOpt = False,
    url: UrlOpt = None,
) -> None:
    """Show a project's full configuration."""
    call = Call(json_out, None, url)
    _run(call, lambda api: api.get(f"/api/projects/{key}"), output.render_project)


@project_app.command("set")
def project_set(
    key: Annotated[str, typer.Argument(help="Project key.")],
    field: Annotated[str, typer.Argument(help="Field to set.")],
    value: Annotated[str, typer.Argument(help="New value.")],
    json_out: JsonOpt = False,
    url: UrlOpt = None,
) -> None:
    """Edit one project config field."""
    call = Call(json_out, None, url)
    body = {"field": field, "value": value}
    _run(call, lambda api: api.patch(f"/api/projects/{key}", json=body), output.render_project)


# --- ticket -----------------------------------------------------------------


@ticket_app.command("create")
def ticket_create(
    title: Annotated[str, typer.Option("--title", help="Ticket title (required).")],
    description: Annotated[
        str, typer.Option("--description", "-d", help="Markdown description.")
    ] = "",
    project: Annotated[
        Optional[str], typer.Option("--project", help="Project key.")
    ] = None,
    state: Annotated[str, typer.Option("--state", help="Initial state.")] = "backlog",
    assignee: Annotated[str, typer.Option("--assignee", help="Assignee.")] = "unassigned",
    json_out: JsonOpt = False,
    agent: AgentOpt = None,
    url: UrlOpt = None,
) -> None:
    """Create a ticket. Prints the new ID."""
    call = Call(json_out, agent, url)
    if not project:
        project = config.default_project()
    if not project:
        msg = "No project given. Pass --project KEY (or set SOLOPM_PROJECT)."
        if json_out:
            output.print_error_json({"error": {"code": "validation", "message": msg}})
        else:
            output.err_console.print(f"[red]error[/]: {msg}")
        raise typer.Exit(code=1)
    body = {
        "project": project,
        "title": title,
        "description": description,
        "state": state,
        "assignee": assignee,
    }

    def render(t: dict) -> None:
        output.console.print(f"[green]✓[/] Created [bold]{t['id']}[/]")
        output.render_ticket(t)

    _run(call, lambda api: api.post("/api/tickets", json=body), render)


@ticket_app.command("list")
def ticket_list(
    project: Annotated[Optional[str], typer.Option("--project", help="Project key.")] = None,
    state: Annotated[Optional[str], typer.Option("--state", help="Filter by state.")] = None,
    assignee: Annotated[
        Optional[str], typer.Option("--assignee", help="Filter by assignee.")
    ] = None,
    json_out: JsonOpt = False,
    url: UrlOpt = None,
) -> None:
    """List tickets (the board query)."""
    call = Call(json_out, None, url)
    params = {}
    project = project or config.default_project()
    if project:
        params["project"] = project
    if state:
        params["state"] = state
    if assignee:
        params["assignee"] = assignee
    _run(
        call,
        lambda api: api.get("/api/tickets", params=params),
        lambda r: output.render_tickets(r["tickets"]),
    )


@ticket_app.command("show")
def ticket_show(
    ticket_id: Annotated[str, typer.Argument(help="Ticket ID, e.g. SOLO-42.")],
    json_out: JsonOpt = False,
    agent: AgentOpt = None,
    url: UrlOpt = None,
) -> None:
    """Full ticket detail — what an agent is seeded to fetch its own context."""
    call = Call(json_out, agent, url)
    _run(call, lambda api: api.get(f"/api/tickets/{ticket_id}"), output.render_ticket)


@ticket_app.command("edit")
def ticket_edit(
    ticket_id: Annotated[str, typer.Argument(help="Ticket ID.")],
    title: Annotated[Optional[str], typer.Option("--title", help="New title.")] = None,
    description: Annotated[
        Optional[str], typer.Option("--description", "-d", help="New description.")
    ] = None,
    json_out: JsonOpt = False,
    agent: AgentOpt = None,
    url: UrlOpt = None,
) -> None:
    """Update a ticket's title and/or description."""
    call = Call(json_out, agent, url)
    body: dict = {}
    if title is not None:
        body["title"] = title
    if description is not None:
        body["description"] = description
    _run(call, lambda api: api.patch(f"/api/tickets/{ticket_id}", json=body), output.render_ticket)


@ticket_app.command("comment")
def ticket_comment(
    ticket_id: Annotated[str, typer.Argument(help="Ticket ID.")],
    body: Annotated[str, typer.Option("--body", "-b", help="Comment text.")],
    json_out: JsonOpt = False,
    agent: AgentOpt = None,
    url: UrlOpt = None,
) -> None:
    """Append a comment (progress notes and review notes)."""
    call = Call(json_out, agent, url)

    def render(a: dict) -> None:
        output.console.print(f"[green]✓[/] Comment added by [bold]{a['actor']}[/]")

    _run(
        call,
        lambda api: api.post(f"/api/tickets/{ticket_id}/comments", json={"body": body}),
        render,
    )


@ticket_app.command("move")
def ticket_move(
    ticket_id: Annotated[str, typer.Argument(help="Ticket ID.")],
    state: Annotated[str, typer.Argument(help="Target state.")],
    after: Annotated[
        Optional[str],
        typer.Option("--after", help="Land directly below this ticket; omit for the column bottom."),
    ] = None,
    json_out: JsonOpt = False,
    agent: AgentOpt = None,
    url: UrlOpt = None,
) -> None:
    """Transition a ticket to a new state (optionally positioned within the column)."""
    call = Call(json_out, agent, url)
    body = {"state": state} if after is None else {"state": state, "after": after}

    def render(t: dict) -> None:
        output.console.print(f"[green]✓[/] {t['id']} → [bold]{t['state']}[/]")

    _run(
        call,
        lambda api: api.post(f"/api/tickets/{ticket_id}/move", json=body),
        render,
    )


@ticket_app.command("assign")
def ticket_assign(
    ticket_id: Annotated[str, typer.Argument(help="Ticket ID.")],
    assignee: Annotated[str, typer.Argument(help="human | claude | codex | unassigned.")],
    json_out: JsonOpt = False,
    agent: AgentOpt = None,
    url: UrlOpt = None,
) -> None:
    """Assign a ticket."""
    call = Call(json_out, agent, url)

    def render(t: dict) -> None:
        output.console.print(f"[green]✓[/] {t['id']} assigned to [bold]{t['assignee']}[/]")

    _run(
        call,
        lambda api: api.post(f"/api/tickets/{ticket_id}/assign", json={"assignee": assignee}),
        render,
    )


@ticket_app.command("reorder")
def ticket_reorder(
    ticket_id: Annotated[str, typer.Argument(help="Ticket ID to reposition.")],
    after: Annotated[
        Optional[str],
        typer.Option("--after", help="Place below this ticket ID; omit for top of column."),
    ] = None,
    json_out: JsonOpt = False,
    agent: AgentOpt = None,
    url: UrlOpt = None,
) -> None:
    """Reorder a ticket within its column (cosmetic; no state change)."""
    call = Call(json_out, agent, url)

    def render(t: dict) -> None:
        where = f"after {after}" if after else "to the top"
        output.console.print(f"[green]✓[/] {t['id']} moved {where}")

    _run(
        call,
        lambda api: api.post(f"/api/tickets/{ticket_id}/reorder", json={"after": after}),
        render,
    )


def run() -> None:
    app()


if __name__ == "__main__":
    run()
