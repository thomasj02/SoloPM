"""Rendering: ``--json`` emits one structured object; otherwise pretty human output."""

from __future__ import annotations

import json

from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from ..core.models import STATE_LABELS

# highlight=False disables rich's automatic regex highlighting (which recolors digits,
# so a ticket id like "SOLO-2" would be split into separate style spans). Intentional
# ``[markup]`` styling still applies; ids/numbers render uniformly and predictably.
console = Console(highlight=False)
err_console = Console(stderr=True, highlight=False)

# Color per state for human output.
STATE_STYLE = {
    "backlog": "grey62",
    "todo": "white",
    "in-progress": "yellow",
    "in-ai-review": "cyan",
    "in-human-review": "magenta",
    "done": "green",
    "cancelled": "red",
}

ASSIGNEE_STYLE = {
    "human": "blue",
    "claude": "magenta",
    "codex": "cyan",
    "unassigned": "grey50",
}


def print_json(obj) -> None:
    """Emit one JSON object on stdout as PLAIN text.

    Agents run the CLI inside a PTY (tmux), where rich would syntax-highlight with ANSI
    escape codes and break `json.loads`. A plain ``print`` keeps the output valid JSON
    regardless of whether stdout is a terminal.
    """
    print(json.dumps(obj, indent=2, ensure_ascii=False))


def print_error_json(err: dict) -> None:
    """Emit the {"error": {...}} contract on stdout (agents parse stdout)."""
    print(json.dumps(err))


def fmt_age(seconds: int | None) -> str:
    """Compact, human-friendly time-in-state, e.g. ``3d`` / ``5h`` / ``12m`` (SOLO-13).

    Mirrors the web board badge's m/h/d granularity. ``None``/negative → ``"—"``.
    """
    if seconds is None or seconds < 0:
        return "—"
    if seconds < 60:
        return "just now"
    mins = seconds // 60
    if mins < 60:
        return f"{mins}m"
    hours = mins // 60
    if hours < 24:
        return f"{hours}h"
    return f"{hours // 24}d"


def _state_label(state: str) -> str:
    label = STATE_LABELS.get(state, state)
    return f"[{STATE_STYLE.get(state, 'white')}]{label}[/]"


def _assignee_label(assignee: str) -> str:
    return f"[{ASSIGNEE_STYLE.get(assignee, 'white')}]{assignee}[/]"


def render_projects(projects: list[dict]) -> None:
    if not projects:
        console.print("[grey50]No projects yet. Add one with[/] [bold]solopm project add[/].")
        return
    table = Table(title="Projects", header_style="bold", expand=False)
    table.add_column("Key", style="bold")
    table.add_column("Name")
    table.add_column("Repo", style="grey62")
    table.add_column("Master")
    table.add_column("Tickets", justify="right")
    for p in projects:
        table.add_row(
            p["key"], p["name"], p.get("repo") or "—", p["master_branch"], str(p["ticket_count"])
        )
    console.print(table)


def render_project(p: dict) -> None:
    lines = [
        f"[bold]{p['key']}[/] — {p['name']}",
        f"repo:            {p.get('repo') or '—'}",
        # Remote mode changes the whole PR lifecycle — it must be visible here.
        *(
            [f"github repo:     {p['github_repo']} [grey62](remote — PR lifecycle via GitHub API)[/]"]
            if p.get("github_repo")
            else []
        ),
        f"master branch:   {p['master_branch']}",
        f"branch convention: {p['branch_convention']}",
        f"implementer:     {_assignee_label(p['default_implementer'])}",
        f"reviewer:        {_assignee_label(p['default_reviewer'])}",
        f"tickets:         {p['ticket_count']}",
        "",
        "[grey62]review prompt:[/]",
        p["review_prompt"],
    ]
    console.print(Panel("\n".join(lines), title="Project", expand=False))


def render_prune(r: dict) -> None:
    """Human view of a branch prune (SOLO-23): what was (or would be) deleted, and skips."""
    pruned = r.get("pruned", [])
    skipped = r.get("skipped", [])
    applied = r.get("applied", False)
    if r.get("note"):
        # The service declined (e.g. a remote project) — that must not render as a
        # clean "nothing to prune", which would imply the repo was scanned.
        console.print(f"[yellow]⚠[/] {r['note']}")
        return
    if not pruned and not skipped:
        console.print("[green]✓[/] No merged local branches to prune.")
        return
    if pruned:
        verb = "Deleted" if applied else "Would delete"
        console.print(f"[bold]{verb} {len(pruned)} branch(es):[/]")
        for p in pruned:
            reasons = ", ".join(p.get("reasons", []))
            wt = f" [grey62](+ worktree {p['worktree']})[/]" if p.get("worktree") else ""
            console.print(f"  [bold]{p['branch']}[/] [grey62]({reasons})[/]{wt}")
    if skipped:
        console.print(f"[yellow]Skipped {len(skipped)}:[/]")
        for s in skipped:
            console.print(f"  [bold]{s['branch']}[/] [grey62]— {s.get('reason', '')}[/]")
    if pruned and not applied:
        console.print("[grey62]Dry run — re-run with [bold]--apply[/] to delete.[/]")


def render_tickets(tickets: list[dict]) -> None:
    if not tickets:
        console.print("[grey50]No tickets match.[/]")
        return
    table = Table(header_style="bold", expand=False)
    table.add_column("ID", style="bold")
    table.add_column("Title")
    table.add_column("State")
    table.add_column("Age", style="grey62")
    table.add_column("Assignee")
    table.add_column("Tags", style="grey62")
    table.add_column("💬", justify="right")
    for t in tickets:
        active = " [green]●[/]" if t.get("session_active") else ""
        count = t.get("comment_count", 0)
        table.add_row(
            t["id"] + active,
            t["title"],
            _state_label(t["state"]),
            fmt_age(t.get("time_in_state_seconds")),
            _assignee_label(t["assignee"]),
            ", ".join(t.get("tags") or []),
            str(count) if count else "",
        )
    console.print(table)


_GRAPH_EDGE_VERB = {
    "blocks": "blocks",
    "parent": "child of",  # canonical from→to is child→parent
    "related": "related to",
    "duplicate": "duplicate of",
}


def render_graph(g: dict) -> None:
    """Human-readable dependency graph: scope + counts, cycle warnings, and edges by type."""
    scope = g.get("scope", {})
    nodes = g.get("nodes", [])
    edges = g.get("edges", [])
    if scope.get("around"):
        where = f"around [bold]{scope['around']}[/] (depth {scope.get('depth')})"
    elif scope.get("project"):
        where = f"project [bold]{scope['project']}[/]"
    else:
        where = "all projects"
    console.print(
        f"[bold]Dependency graph[/] — {where} · {len(nodes)} node(s) · {len(edges)} edge(s)"
    )
    if g.get("truncated"):
        console.print("[yellow]⚠ truncated — node cap reached[/]")
    for cyc in g.get("cycles", []):
        console.print(f"[red]⚠ blocks cycle:[/] {' → '.join(cyc)} → {cyc[0]}")

    blocked = [n["id"] for n in nodes if n.get("blocked")]
    if blocked:
        console.print(f"[grey62]blocked:[/] {', '.join(blocked)}")

    if not edges:
        console.print("[dim]No relationships in scope.[/]")
        return

    by_type: dict[str, list[dict]] = {}
    for e in edges:
        by_type.setdefault(e["type"], []).append(e)
    for typ in ("blocks", "parent", "related", "duplicate"):
        group = by_type.get(typ)
        if not group:
            continue
        verb = _GRAPH_EDGE_VERB.get(typ, typ)
        console.print(f"  [grey62]{typ}[/]")
        for e in group:
            console.print(f"    [bold]{e['from']}[/] {verb} [bold]{e['to']}[/]")


def render_ticket(t: dict) -> None:
    age = fmt_age(t.get("time_in_state_seconds"))
    header = (
        f"[bold]{t['id']}[/]  {_state_label(t['state'])} [grey62]({age})[/]  "
        f"assignee: {_assignee_label(t['assignee'])}"
    )
    body = [header, "", f"[bold]{t['title']}[/]"]
    if t.get("tags"):
        body += ["", f"[grey62]tags:[/] {', '.join(t['tags'])}"]
    if t.get("description"):
        body += ["", t["description"]]
    if t.get("branch"):
        body += ["", f"[grey62]branch:[/] {t['branch']}"]
    if t.get("pr"):
        pr = t["pr"]
        body += [f"[grey62]PR:[/] #{pr['number']} ({pr['state']}) {pr.get('url') or ''}"]
    if t.get("session"):
        s = t["session"]
        live = "[green]live[/]" if s.get("active") else "ended"
        body += [f"[grey62]session:[/] {s['id']} ({live})"]
    console.print(Panel("\n".join(body), expand=False))

    relations = t.get("relations") or []
    if relations:
        console.print("[bold]Relations[/]")
        # Relations arrive pre-sorted by perspective group; print each group once.
        seen: list[str] = []
        for r in relations:
            label = r.get("label", r.get("key", ""))
            if label not in seen:
                seen.append(label)
                console.print(f"  [grey62]{label}[/]")
            tk = r.get("ticket", {})
            console.print(
                f"    [bold]{tk.get('id', '')}[/] {tk.get('title', '')} "
                f"{_state_label(tk.get('state', ''))}"
            )

    activity = t.get("activity") or []
    if activity:
        console.print("[bold]Activity[/]")
        for a in activity:
            ts = a.get("at", "")
            actor = a.get("actor", "")
            if a["kind"] == "comment":
                console.print(f"  [grey50]{ts}[/] [bold]{actor}[/]: {a['body']}")
            else:
                console.print(f"  [grey50]{ts}[/] [grey62]{actor} {a['body']}[/]")
