"""The ``solopm`` command-line interface.

Structure: ``solopm <noun> <verb> [args] [flags]``. Nouns: ``project``, ``ticket``,
plus top-level ``init`` / ``serve``. ``--json`` emits a single structured object (the
agent contract); ``--agent <name>`` attributes the write. The CLI is a thin client of
the local backend — except ``init``/``serve``, which act on the store/server directly.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, List, Optional
from urllib.parse import quote, urlencode

import typer
from typing_extensions import Annotated

from .. import __version__, config
from . import client, output
from .client import Api, ApiError

app = typer.Typer(
    name="solopm",
    help="SoloPM — an AI-first project tracker for solo developers.",
    no_args_is_help=True,
    add_completion=False,
)
project_app = typer.Typer(help="Manage projects.", no_args_is_help=True)
ticket_app = typer.Typer(help="Manage tickets.", no_args_is_help=True)
review_app = typer.Typer(help="AI review verdicts.", no_args_is_help=True)
criteria_app = typer.Typer(help="Manage a ticket's acceptance criteria.", no_args_is_help=True)
memory_app = typer.Typer(help="Per-project review memory (the learning review gate).", no_args_is_help=True)
app.add_typer(project_app, name="project")
app.add_typer(ticket_app, name="ticket")
app.add_typer(review_app, name="review")
ticket_app.add_typer(criteria_app, name="criteria")
review_app.add_typer(memory_app, name="memory")

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
def radar(
    project: Annotated[
        Optional[str], typer.Option("--project", help="Limit to one project key.")
    ] = None,
    json_out: JsonOpt = False,
    url: UrlOpt = None,
) -> None:
    """Overlap radar — warn when active worktrees touch the same files (informational)."""
    call = Call(json_out, None, url)
    path = f"/api/radar?project={project}" if project else "/api/radar"

    def render(data: dict) -> None:
        overlaps = data.get("overlaps", [])
        if not overlaps:
            output.console.print("[green]✓[/] No overlaps among active worktrees.")
            return
        output.console.print(f"[yellow]⚠ {len(overlaps)} overlap(s):[/]")
        for ov in overlaps:
            a = ov["a"]["ticket"] or ov["a"]["branch"]
            b = ov["b"]["ticket"] or ov["b"]["branch"]
            output.console.print(f"  [bold]{a}[/] ⇄ [bold]{b}[/] — {', '.join(ov['files'])}")

    _run(call, lambda api: api.get(path), render)


@app.command()
def graph(
    project: Annotated[
        Optional[str], typer.Option("--project", help="Whole-project relational graph.")
    ] = None,
    around: Annotated[
        Optional[str], typer.Option("--around", help="Ego-graph around this ticket id.")
    ] = None,
    depth: Annotated[
        int, typer.Option("--depth", help="Ego-graph hop depth (used with --around).")
    ] = 1,
    type: Annotated[
        Optional[List[str]],
        typer.Option("--type", help="Filter relation type(s): blocks|related|duplicate|parent (repeatable)."),
    ] = None,
    active_only: Annotated[
        bool, typer.Option("--active-only", help="Drop done/cancelled nodes.")
    ] = False,
    json_out: JsonOpt = False,
    url: UrlOpt = None,
) -> None:
    """Dependency graph of ticket relationships — nodes + typed edges (blocks DAG, parent
    trees, related/duplicate). With neither flag, uses the default project (else all projects)."""
    call = Call(json_out, None, url)
    # Default to the configured project (like `ticket list`) unless an ego-graph is requested.
    if not around and not project:
        project = config.default_project()
    params: list[tuple[str, str]] = []
    if project:
        params.append(("project", project))
    if around:
        params.append(("around", around))
        params.append(("depth", str(depth)))
    if active_only:
        params.append(("active_only", "true"))
    for t in type or []:
        params.append(("type", t))
    query = urlencode(params)
    path = "/api/graph" + (f"?{query}" if query else "")

    _run(call, lambda api: api.get(path), output.render_graph)


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


@app.command(name="mcp")
def mcp_cmd(
    agent: Annotated[
        str, typer.Option("--agent", help="Attribute MCP writes to this agent name.")
    ] = "claude",
    channel: Annotated[
        bool,
        typer.Option(
            "--channel",
            help="Run as a Claude Code channel: push ticket/review/overlap events into the "
            "session (load with `claude --dangerously-load-development-channels server:solopm`).",
        ),
    ] = False,
    scope: Annotated[
        str, typer.Option("--scope", help="Channel event scope: 'mine' (this agent's tickets) or 'all'.")
    ] = "mine",
    poll: Annotated[
        float, typer.Option("--poll", help="Channel poll interval, seconds.")
    ] = 3.0,
    url: Annotated[
        Optional[str],
        typer.Option(
            "--url",
            help="Drive a remote backend over its HTTP API (e.g. http://host:8787) "
            "instead of opening the local store. Deliberately not read from "
            "SOLOPM_URL — HTTP mode is always an explicit choice.",
        ),
    ] = None,
) -> None:
    """Run the SoloPM MCP server (stdio) so an AI agent can drive SoloPM as MCP tools."""
    # Imported lazily so the rest of the CLI works without the optional `mcp` dependency.
    # NOTE: stdio is the MCP transport — nothing may be printed to stdout here.
    if url is not None:
        from ..core.models import ACTORS
        from ..mcp.http_tools import HttpSoloPMTools
        from ..mcp.server import build_server

        if not url.strip():
            raise typer.BadParameter(
                "--url must not be empty (an unset shell variable?) — HTTP mode is "
                "an explicit choice and never falls back to the local store."
            )
        if channel:
            raise typer.BadParameter(
                "--channel needs direct store access (the HTTP API has no activity "
                "feed to poll yet) — run channel mode on the backend's machine, or "
                "drop --url."
            )
        # Send the value we validated: the backend's get_actor strips/lowercases the
        # header, and a padded value wouldn't even be a legal header.
        actor = agent.strip().lower()
        if actor not in ACTORS:
            raise typer.BadParameter(
                f"--agent {agent!r} would be rejected by the backend: the HTTP API "
                f"attributes writes to one of {', '.join(ACTORS)}."
            )
        build_server(agent=actor, tools=HttpSoloPMTools(Api(url, agent=actor))).run()
        return

    from ..core.github import GitHub
    from ..core.service import Service
    from ..core.store import Store

    store = Store(config.db_path())
    store.init()  # lazily create/migrate the store so the server works standalone
    service = Service(store, github=GitHub())
    if channel:
        from ..mcp.channel import run_channel_server

        run_channel_server(service, agent=agent, scope=scope, poll_interval=poll)
    else:
        from ..mcp.server import build_server

        build_server(service, agent=agent).run()


# --- project ----------------------------------------------------------------


@project_app.command("add")
def project_add(
    key: Annotated[str, typer.Option("--key", help="Project key, e.g. SOLO.")],
    name: Annotated[str, typer.Option("--name", help="Project name.")],
    repo: Annotated[Optional[str], typer.Option("--repo", help="Repo checkout path.")] = None,
    github_repo: Annotated[
        Optional[str],
        typer.Option(
            "--github-repo",
            help="owner/name slug for a repo whose checkout lives on another machine "
            "than the backend (PR lifecycle then runs via the GitHub API).",
        ),
    ] = None,
    master: Annotated[str, typer.Option("--master", help="Master branch.")] = "main",
    json_out: JsonOpt = False,
    url: UrlOpt = None,
) -> None:
    """Register a project."""
    call = Call(json_out, None, url)
    body = {"key": key, "name": name, "repo": repo, "github_repo": github_repo, "master": master}
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


@project_app.command("delete")
def project_delete(
    key: Annotated[str, typer.Argument(help="Project key.")],
    force: Annotated[
        bool,
        typer.Option("--force", help="Delete even if it has tickets (cascades all of them)."),
    ] = False,
    json_out: JsonOpt = False,
    url: UrlOpt = None,
) -> None:
    """Delete a project. Refused if it still has tickets unless --force, which also deletes
    all of its tickets, their activity, and their relationships (irreversible)."""
    call = Call(json_out, None, url)
    # Encode the key into the path segment so a crafted key (e.g. "SOLO?force=true") can't
    # smuggle force=true past the --force guard on this destructive command — force is set
    # ONLY by the flag below.
    path = f"/api/projects/{quote(key, safe='')}" + ("?force=true" if force else "")

    def render(r: dict) -> None:
        n = r.get("tickets_deleted", 0)
        extra = f" and {n} ticket{'' if n == 1 else 's'}" if n else ""
        output.console.print(f"[green]✓[/] Deleted project [bold]{r.get('key', key)}[/]{extra}")

    _run(call, lambda api: api.delete(path), render)


@project_app.command("prune")
def project_prune(
    key: Annotated[str, typer.Argument(help="Project key.")],
    apply: Annotated[
        bool,
        typer.Option("--apply", help="Actually delete (default: a dry-run that only lists candidates)."),
    ] = False,
    json_out: JsonOpt = False,
    url: UrlOpt = None,
) -> None:
    """Prune local branches whose merge is verified — reachable-merged into master, or on a done
    ticket whose PR merged. (A merely gone upstream is surfaced but not deleted.) Dry-run by
    default; --apply deletes them — removing a clean git worktree first and skipping any worktree
    with uncommitted changes. Never touches the current branch or master."""
    call = Call(json_out, None, url)
    path = f"/api/projects/{quote(key, safe='')}/prune"
    _run(call, lambda api: api.post(path, json={"apply": apply}), output.render_prune)


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
    tag: Annotated[
        Optional[List[str]],
        typer.Option("--tag", help="Filter by tag (repeatable; a ticket must carry ALL given tags)."),
    ] = None,
    json_out: JsonOpt = False,
    url: UrlOpt = None,
) -> None:
    """List tickets (the board query)."""
    call = Call(json_out, None, url)
    params: list[tuple[str, str]] = []
    project = project or config.default_project()
    if project:
        params.append(("project", project))
    if state:
        params.append(("state", state))
    if assignee:
        params.append(("assignee", assignee))
    for tg in tag or []:
        params.append(("tag", tg))
    query = urlencode(params)
    path = "/api/tickets" + (f"?{query}" if query else "")
    _run(
        call,
        lambda api: api.get(path),
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
    branch: Annotated[
        Optional[str],
        typer.Option("--branch", help="Record this SoloPM branch (when self-transitioning to in-ai-review)."),
    ] = None,
    json_out: JsonOpt = False,
    agent: AgentOpt = None,
    url: UrlOpt = None,
) -> None:
    """Transition a ticket to a new state (optionally positioned within the column)."""
    call = Call(json_out, agent, url)
    body: dict = {"state": state}
    if after is not None:
        body["after"] = after
    if branch is not None:
        body["branch"] = branch

    def render(t: dict) -> None:
        output.console.print(f"[green]✓[/] {t['id']} → [bold]{t['state']}[/]")

    def do_move(api: client.Api) -> dict:
        # SOLO-29: for a remote project (github_repo set) the commits live on THIS
        # machine — push the branch from here first; a failure aborts before the move.
        client.push_branch_for_remote_move(api, ticket_id, state, branch)
        return api.post(f"/api/tickets/{ticket_id}/move", json=body)

    _run(call, do_move, render)


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


@ticket_app.command("link")
def ticket_link(
    ticket_id: Annotated[str, typer.Argument(help="Ticket ID (the subject of the relation).")],
    type: Annotated[
        str, typer.Argument(help="blocks | related | duplicate | parent.")
    ],
    other_id: Annotated[str, typer.Argument(help="The other ticket ID.")],
    json_out: JsonOpt = False,
    agent: AgentOpt = None,
    url: UrlOpt = None,
) -> None:
    """Relate two tickets. Read as "<id> <type> <other>": `link A blocks B`, `link A
    related B`, `link A duplicate B` (A duplicates B), `link A parent B` (B is A's parent)."""
    call = Call(json_out, agent, url)
    body = {"type": type, "other": other_id}

    def render(t: dict) -> None:
        output.console.print(f"[green]✓[/] {ticket_id} [bold]{type}[/] {other_id}")

    _run(call, lambda api: api.post(f"/api/tickets/{ticket_id}/links", json=body), render)


@ticket_app.command("unlink")
def ticket_unlink(
    ticket_id: Annotated[str, typer.Argument(help="Ticket ID.")],
    other_id: Annotated[str, typer.Argument(help="The other ticket ID.")],
    type: Annotated[
        Optional[str],
        typer.Option("--type", help="Only remove this relation type; omit to remove all links to it."),
    ] = None,
    direction: Annotated[
        Optional[str],
        typer.Option(
            "--direction",
            help="out|in — pin one orientation (out = <id> is the blocker/duplicate/child) "
            "when a pair holds opposing directional links.",
        ),
    ] = None,
    json_out: JsonOpt = False,
    agent: AgentOpt = None,
    url: UrlOpt = None,
) -> None:
    """Remove the relationship(s) between two tickets (order-independent by default)."""
    call = Call(json_out, agent, url)
    params = {}
    if type is not None:
        params["type"] = type
    if direction is not None:
        params["direction"] = direction
    query = urlencode(params)
    path = f"/api/tickets/{ticket_id}/links/{other_id}" + (f"?{query}" if query else "")

    def render(t: dict) -> None:
        output.console.print(f"[green]✓[/] {ticket_id} ⇄ {other_id} unlinked")

    _run(call, lambda api: api.delete(path), render)


@ticket_app.command("tag")
def ticket_tag(
    ticket_id: Annotated[str, typer.Argument(help="Ticket ID.")],
    tags: Annotated[List[str], typer.Argument(help="One or more tags to add.")],
    json_out: JsonOpt = False,
    agent: AgentOpt = None,
    url: UrlOpt = None,
) -> None:
    """Add one or more tags/labels to a ticket (normalized to lowercase)."""
    call = Call(json_out, agent, url)

    def render(t: dict) -> None:
        shown = ", ".join(t.get("tags") or []) or "—"
        output.console.print(f"[green]✓[/] {t['id']} tags: [bold]{shown}[/]")

    _run(
        call,
        lambda api: api.post(f"/api/tickets/{ticket_id}/tags", json={"tags": tags}),
        render,
    )


@ticket_app.command("untag")
def ticket_untag(
    ticket_id: Annotated[str, typer.Argument(help="Ticket ID.")],
    tag: Annotated[str, typer.Argument(help="The tag to remove.")],
    json_out: JsonOpt = False,
    agent: AgentOpt = None,
    url: UrlOpt = None,
) -> None:
    """Remove a tag from a ticket."""
    call = Call(json_out, agent, url)

    def render(t: dict) -> None:
        shown = ", ".join(t.get("tags") or []) or "—"
        output.console.print(f"[green]✓[/] {t['id']} tags: [bold]{shown}[/]")

    # Encode the tag path segment so a crafted value can't manipulate the request URL.
    _run(
        call,
        lambda api: api.delete(f"/api/tickets/{ticket_id}/tags/{quote(tag, safe='')}"),
        render,
    )


# --- review -----------------------------------------------------------------


@review_app.command("submit")
def review_submit(
    ticket_id: Annotated[str, typer.Argument(help="Ticket ID under AI review.")],
    verdict: Annotated[str, typer.Option("--verdict", help="pass | fail")],
    comment: Annotated[
        Optional[str], typer.Option("--comment", "-c", help="Review notes (recorded as a comment).")
    ] = None,
    json_out: JsonOpt = False,
    agent: AgentOpt = None,
    url: UrlOpt = None,
) -> None:
    """Report an AI-review verdict — pass → in-human-review, fail → kick back to in-progress."""
    call = Call(json_out, agent, url)
    body: dict = {"verdict": verdict}
    if comment is not None:
        body["comment"] = comment

    def render(t: dict) -> None:
        output.console.print(f"[green]✓[/] {t['id']} review [bold]{verdict}[/] → [bold]{t['state']}[/]")

    _run(call, lambda api: api.post(f"/api/tickets/{ticket_id}/review", json=body), render)


@criteria_app.command("add")
def criteria_add(
    ticket_id: Annotated[str, typer.Argument(help="Ticket ID.")],
    text: Annotated[str, typer.Argument(help="Criterion text.")],
    json_out: JsonOpt = False,
    agent: AgentOpt = None,
    url: UrlOpt = None,
) -> None:
    """Add an acceptance criterion to a ticket."""
    call = Call(json_out, agent, url)

    def render(t: dict) -> None:
        crit = t["acceptance_criteria"]
        output.console.print(
            f"[green]✓[/] criterion [bold]{crit[-1]['id']}[/] added to {t['id']} ({len(crit)} total)"
        )

    _run(call, lambda api: api.post(f"/api/tickets/{ticket_id}/criteria", json={"text": text}), render)


@criteria_app.command("check")
def criteria_check(
    ticket_id: Annotated[str, typer.Argument(help="Ticket ID.")],
    criterion_id: Annotated[str, typer.Argument(help="Criterion ID (e.g. c1).")],
    uncheck: Annotated[bool, typer.Option("--uncheck", help="Mark it not-done instead.")] = False,
    json_out: JsonOpt = False,
    agent: AgentOpt = None,
    url: UrlOpt = None,
) -> None:
    """Tick (or --uncheck) an acceptance criterion."""
    call = Call(json_out, agent, url)

    def render(t: dict) -> None:
        output.console.print(
            f"[green]✓[/] {ticket_id} {criterion_id} {'unchecked' if uncheck else 'checked'}"
        )

    _run(
        call,
        lambda api: api.patch(
            f"/api/tickets/{ticket_id}/criteria/{criterion_id}", json={"done": not uncheck}
        ),
        render,
    )


@criteria_app.command("edit")
def criteria_edit(
    ticket_id: Annotated[str, typer.Argument(help="Ticket ID.")],
    criterion_id: Annotated[str, typer.Argument(help="Criterion ID (e.g. c1).")],
    text: Annotated[str, typer.Argument(help="New criterion text.")],
    json_out: JsonOpt = False,
    agent: AgentOpt = None,
    url: UrlOpt = None,
) -> None:
    """Edit an acceptance criterion's text."""
    call = Call(json_out, agent, url)

    def render(t: dict) -> None:
        output.console.print(f"[green]✓[/] {ticket_id} {criterion_id} edited")

    _run(
        call,
        lambda api: api.patch(
            f"/api/tickets/{ticket_id}/criteria/{criterion_id}", json={"text": text}
        ),
        render,
    )


@criteria_app.command("remove")
def criteria_remove(
    ticket_id: Annotated[str, typer.Argument(help="Ticket ID.")],
    criterion_id: Annotated[str, typer.Argument(help="Criterion ID (e.g. c1).")],
    json_out: JsonOpt = False,
    agent: AgentOpt = None,
    url: UrlOpt = None,
) -> None:
    """Remove an acceptance criterion."""
    call = Call(json_out, agent, url)

    def render(t: dict) -> None:
        output.console.print(f"[green]✓[/] {ticket_id} {criterion_id} removed")

    _run(call, lambda api: api.delete(f"/api/tickets/{ticket_id}/criteria/{criterion_id}"), render)


@review_app.command("prompt")
def review_prompt(
    project: Annotated[str, typer.Argument(help="Project key.")],
    record: Annotated[bool, typer.Option("--record", help="Count this as a review (bump item hits).")] = False,
    json_out: JsonOpt = False,
    url: UrlOpt = None,
) -> None:
    """Print the assembled review prompt (base prompt + active review memory)."""
    call = Call(json_out, None, url)
    path = f"/api/projects/{project}/review-prompt" + ("?record_hit=true" if record else "")

    def render(d: dict) -> None:
        output.console.print(d.get("prompt") or "[dim](empty review prompt)[/]")

    _run(call, lambda api: api.get(path), render)


@memory_app.command("list")
def memory_list(
    project: Annotated[str, typer.Argument(help="Project key.")],
    status: Annotated[
        Optional[str], typer.Option("--status", help="candidate | active | retired")
    ] = None,
    json_out: JsonOpt = False,
    url: UrlOpt = None,
) -> None:
    """List a project's review-memory items."""
    call = Call(json_out, None, url)
    path = f"/api/projects/{project}/review-memory" + (f"?status={status}" if status else "")

    def render(data: dict) -> None:
        items = data.get("items", [])
        if not items:
            output.console.print("[dim]No review-memory items.[/]")
            return
        for i in items:
            output.console.print(
                f"  [bold]{i['id']}[/] [{i['status']}/{i['source']}] hits={i['hits']}: {i['text']}"
            )

    _run(call, lambda api: api.get(path), render)


@memory_app.command("add")
def memory_add(
    project: Annotated[str, typer.Argument(help="Project key.")],
    text: Annotated[str, typer.Argument(help="Checklist item text.")],
    json_out: JsonOpt = False,
    url: UrlOpt = None,
) -> None:
    """Add an active review-memory item."""
    call = Call(json_out, None, url)

    def render(i: dict) -> None:
        output.console.print(f"[green]✓[/] added [bold]{i['id']}[/] ({i['status']})")

    _run(
        call,
        lambda api: api.post(f"/api/projects/{project}/review-memory", json={"text": text}),
        render,
    )


@memory_app.command("set")
def memory_set(
    project: Annotated[str, typer.Argument(help="Project key.")],
    item_id: Annotated[str, typer.Argument(help="Item id (e.g. m1).")],
    text: Annotated[Optional[str], typer.Option("--text", help="New text.")] = None,
    status: Annotated[
        Optional[str], typer.Option("--status", help="candidate | active | retired.")
    ] = None,
    json_out: JsonOpt = False,
    url: UrlOpt = None,
) -> None:
    """Update a review-memory item's text and/or status (promote, retire, edit)."""
    call = Call(json_out, None, url)
    body: dict = {}
    if text is not None:
        body["text"] = text
    if status is not None:
        body["status"] = status

    def render(i: dict) -> None:
        output.console.print(f"[green]✓[/] {i['id']} → [bold]{i['status']}[/]")

    _run(
        call,
        lambda api: api.patch(f"/api/projects/{project}/review-memory/{item_id}", json=body),
        render,
    )


def run() -> None:
    app()


if __name__ == "__main__":
    run()
