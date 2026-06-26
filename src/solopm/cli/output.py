"""Rendering: ``--json`` emits one structured object; otherwise pretty human output."""

from __future__ import annotations

import json

from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from ..core.models import STATE_LABELS

console = Console()
err_console = Console(stderr=True)

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
            str(count) if count else "",
        )
    console.print(table)


def render_ticket(t: dict) -> None:
    age = fmt_age(t.get("time_in_state_seconds"))
    header = (
        f"[bold]{t['id']}[/]  {_state_label(t['state'])} [grey62]({age})[/]  "
        f"assignee: {_assignee_label(t['assignee'])}"
    )
    body = [header, "", f"[bold]{t['title']}[/]"]
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
