"""MCP *channel* mode (SOLO-15): push SoloPM events into a live Claude session.

Channels are a Claude Code research-preview MCP extension: the server declares the
``claude/channel`` capability and emits ``notifications/claude/channel`` events that
arrive in the session wrapped in a ``<channel source="solopm" …>`` tag.

The MCP server is spawned per session over stdio, so it can't see writes made elsewhere
(the web app, the CLI, a second agent) directly — instead it **watches the canonical
store's append-only activity feed** (and the overlap radar) and forwards the relevant
changes. Events are scoped to the running session's agent so it isn't spammed.

Run it via ``solopm mcp --channel`` and load it with
``claude --dangerously-load-development-channels server:solopm``.
"""

from __future__ import annotations

from pydantic import BaseModel

from ..core.service import Service


class ChannelNotification(BaseModel):
    """A ``notifications/claude/channel`` event. ``send_notification`` just ``model_dump``s
    this into a JSON-RPC notification, so a plain model with the custom method works."""

    method: str = "notifications/claude/channel"
    params: dict


def _trim(text: str, n: int = 160) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= n else text[: n - 1] + "…"


class ChannelWatcher:
    """Diffs the store's activity feed (+ overlap radar) into channel events.

    ``scope`` is ``"mine"`` (only tickets assigned to this agent) or ``"all"``. The agent's
    own actions are never echoed back — a channel is for changes from *outside* the session.
    """

    def __init__(self, service: Service, *, agent: str = "claude", scope: str = "mine"):
        self.svc = service
        self.agent = agent
        self.scope = scope
        self._cursor = 0
        self._last_radar: dict = {}

    def prime(self) -> None:
        """Establish baselines so existing history isn't replayed on the first poll."""
        self._cursor = self.svc.store.max_activity_id()
        self._last_radar = self._radar_keys()

    def poll(self) -> list[dict]:
        """Return new channel events ``[{content, meta}]`` since the last poll."""
        events: list[dict] = []
        for a in self.svc.store.activities_since(self._cursor):
            self._cursor = a.id
            if a.actor == self.agent:
                continue  # don't notify the session about its own writes
            try:
                ticket = self.svc.get_ticket(a.ticket_id)
            except Exception:
                continue
            if self.scope == "mine" and ticket.assignee != self.agent:
                continue
            events.append(self._activity_event(a, ticket))

        current = self._radar_keys()
        for key, ov in current.items():
            if key not in self._last_radar:
                events.append(self._overlap_event(ov))
        self._last_radar = current
        return events

    # --- formatting (channel meta keys must be [A-Za-z0-9_]; values are strings) ---

    def _activity_event(self, a, ticket) -> dict:
        meta = {
            "ticket_id": ticket.id,
            "project": ticket.project,
            "kind": a.kind,
            "actor": a.actor,
        }
        am = a.meta or {}
        if a.kind == "state_change":
            frm, to = str(am.get("from", "")), str(am.get("to", ticket.state))
            meta["from_state"], meta["to_state"] = frm, to
            content = f"{ticket.id} ({ticket.title}) moved {frm} → {to} — by {a.actor}"
        elif a.kind == "comment":
            content = f"New comment on {ticket.id} ({ticket.title}) by {a.actor}: {_trim(a.body)}"
        elif a.kind == "assignment":
            content = f"{ticket.id}: {a.body} — by {a.actor}"
        elif a.kind == "review":
            content = f"{ticket.id} ({ticket.title}): review result recorded by {a.actor}"
        elif a.kind == "criteria":
            content = f"{ticket.id} acceptance criteria — {a.body} — by {a.actor}"
        else:
            content = f"{ticket.id}: {a.body or a.kind} — by {a.actor}"
        return {"content": content, "meta": meta}

    def _radar_keys(self) -> dict:
        if self.svc.github is None:
            return {}
        try:
            report = self.svc.compute_radar()
        except Exception:
            return {}
        out: dict = {}
        for ov in report.get("overlaps", []):
            out[(ov["a"]["branch"], ov["b"]["branch"], tuple(ov["files"]))] = ov
        return out

    @staticmethod
    def _overlap_event(ov: dict) -> dict:
        a = ov["a"]["ticket"] or ov["a"]["branch"]
        b = ov["b"]["ticket"] or ov["b"]["branch"]
        return {
            "content": f"Overlap: {a} ⇄ {b} now touch {', '.join(ov['files'])}",
            "meta": {
                "kind": "overlap",
                "project": ov["project"],
                "branch_a": ov["a"]["branch"],
                "branch_b": ov["b"]["branch"],
            },
        }


async def _run_channel_async(fastmcp, service, agent, scope, poll_interval) -> None:
    import anyio
    from contextlib import AsyncExitStack

    from mcp.server.session import ServerSession
    from mcp.server.stdio import stdio_server

    lowlevel = fastmcp._mcp_server
    init_options = lowlevel.create_initialization_options(
        experimental_capabilities={"claude/channel": {}}
    )
    watcher = ChannelWatcher(service, agent=agent, scope=scope)
    watcher.prime()

    async def watch_loop(session) -> None:
        while True:
            await anyio.sleep(poll_interval)
            try:
                events = await anyio.to_thread.run_sync(watcher.poll)
            except Exception:
                events = []
            for ev in events:
                try:
                    await session.send_notification(
                        ChannelNotification(params={"content": ev["content"], "meta": ev["meta"]})
                    )
                except Exception:
                    pass  # notifications are fire-and-forget; never crash the server

    async with stdio_server() as (read_stream, write_stream):
        async with AsyncExitStack() as stack:
            lifespan_context = await stack.enter_async_context(lowlevel.lifespan(lowlevel))
            session = await stack.enter_async_context(
                ServerSession(read_stream, write_stream, init_options)
            )
            async with anyio.create_task_group() as tg:
                tg.start_soon(watch_loop, session)
                try:
                    async for message in session.incoming_messages:
                        tg.start_soon(
                            lowlevel._handle_message, message, session, lifespan_context, False
                        )
                finally:
                    tg.cancel_scope.cancel()


def run_channel_server(
    service: Service, *, agent: str = "claude", scope: str = "mine", poll_interval: float = 3.0
) -> None:
    """Run the SoloPM MCP server in channel mode (stdio + a background activity watcher)."""
    import anyio

    from .server import build_server

    fastmcp = build_server(service, agent=agent)
    anyio.run(_run_channel_async, fastmcp, service, agent, scope, poll_interval)
