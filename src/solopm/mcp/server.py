"""The SoloPM MCP server (stdio) built on FastMCP.

`solopm mcp` launches this. Each tool is a thin wrapper over :class:`SoloPMTools`,
which operates the same canonical service the CLI and web app use. Writes are
attributed to the configured agent.
"""

from mcp.server.fastmcp import FastMCP

from ..core.service import Service
from .tools import SoloPMTools

INSTRUCTIONS = (
    "SoloPM is an AI-first Kanban tracker for solo developers. These tools let you read "
    "and drive tickets: list projects and tickets, fetch a ticket's full context, create "
    "and edit tickets, comment, assign, and move tickets through the workflow "
    "(backlog → todo → in-progress → in-ai-review → in-human-review → done, plus "
    "cancelled). Your writes are attributed to your agent identity. Call workflow_info "
    "for the legal states and transition rules. Note: only the human may move a ticket "
    "to 'done' — you cannot close a ticket."
)


def build_server(service: Service, agent: str = "claude") -> FastMCP:
    """Build a FastMCP server exposing SoloPM's operations, attributed to ``agent``."""
    tools = SoloPMTools(service, agent=agent)
    mcp = FastMCP("solopm", instructions=INSTRUCTIONS)

    @mcp.tool()
    def list_projects() -> dict:
        """List all SoloPM projects with their configuration and ticket counts."""
        return tools.list_projects()

    @mcp.tool()
    def workflow_info() -> dict:
        """The SoloPM workflow: valid states, labels, transitions, assignees, and the
        actor rules (only the human may close a ticket to 'done')."""
        return tools.workflow_info()

    @mcp.tool()
    def list_tickets(
        project: str | None = None,
        state: str | None = None,
        assignee: str | None = None,
    ) -> dict:
        """List tickets (the board), optionally filtered by project key, state, or
        assignee. Returns trimmed ticket summaries."""
        return tools.list_tickets(project=project, state=state, assignee=assignee)

    @mcp.tool()
    def show_ticket(ticket_id: str) -> dict:
        """Full detail for one ticket (e.g. SOLO-42): fields, description, comments, and
        the chronological activity log. Use this to fetch your own context on a ticket."""
        return tools.show_ticket(ticket_id)

    @mcp.tool()
    def create_ticket(
        project: str,
        title: str,
        description: str = "",
        state: str = "backlog",
        assignee: str = "unassigned",
    ) -> dict:
        """Create a ticket in a project. Returns the new ticket including its id."""
        return tools.create_ticket(
            project=project, title=title, description=description, state=state, assignee=assignee
        )

    @mcp.tool()
    def edit_ticket(
        ticket_id: str, title: str | None = None, description: str | None = None
    ) -> dict:
        """Update a ticket's title and/or description."""
        return tools.edit_ticket(ticket_id, title=title, description=description)

    @mcp.tool()
    def comment_ticket(ticket_id: str, body: str) -> dict:
        """Append a comment to a ticket — progress notes or review notes."""
        return tools.comment_ticket(ticket_id, body=body)

    @mcp.tool()
    def move_ticket(ticket_id: str, state: str, branch: str | None = None) -> dict:
        """Transition a ticket to a new state (validated against the workflow). Only the
        human may move a ticket to 'done'. When self-transitioning to 'in-ai-review',
        pass `branch` to record your committed branch and (if GitHub automation is on)
        push it and open/refresh the PR."""
        return tools.move_ticket(ticket_id, state=state, branch=branch)

    @mcp.tool()
    def assign_ticket(ticket_id: str, assignee: str) -> dict:
        """Assign a ticket to one of: human, claude, codex, unassigned."""
        return tools.assign_ticket(ticket_id, assignee=assignee)

    @mcp.tool()
    def submit_review(ticket_id: str, verdict: str, comment: str | None = None) -> dict:
        """Report an AI-review verdict on a ticket in 'in-ai-review'. 'pass' advances it to
        in-human-review; 'fail' records your notes as a comment and returns it to
        in-progress for the implementer to address."""
        return tools.submit_review(ticket_id, verdict, comment=comment)

    return mcp
