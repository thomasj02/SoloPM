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
    def submit_review(
        ticket_id: str,
        verdict: str,
        comment: str | None = None,
        criteria_results: list[dict] | None = None,
    ) -> dict:
        """Report an AI-review verdict on a ticket in 'in-ai-review'. 'pass' advances it to
        in-human-review; 'fail' records your notes as a comment and returns it to
        in-progress for the implementer to address. Optionally pass `criteria_results` —
        a list of {criterion_id, verdict ('pass'|'fail'), note} — to record a per-criterion
        assessment in the activity log (the overall verdict still gates the transition)."""
        return tools.submit_review(
            ticket_id, verdict, comment=comment, criteria_results=criteria_results
        )

    @mcp.tool()
    def radar(project: str | None = None) -> dict:
        """Overlap/conflict radar — report active worktrees touching the same files
        (informational; never blocks). Returns {overlaps: [{a:{ticket,branch},
        b:{ticket,branch}, files:[...]}]} so you can warn before two tickets collide."""
        return tools.radar(project=project)

    @mcp.tool()
    def list_review_memory(project: str, status: str | None = None) -> dict:
        """List a project's review-memory items (the learning review gate), optionally
        filtered by status: candidate | active | retired."""
        return tools.list_review_memory(project, status=status)

    @mcp.tool()
    def add_review_memory(project: str, text: str, status: str = "active") -> dict:
        """Add a review-memory checklist item to a project (default 'active')."""
        return tools.add_review_memory(project, text, status=status)

    @mcp.tool()
    def update_review_memory(
        project: str, item_id: str, text: str | None = None, status: str | None = None
    ) -> dict:
        """Curate a review-memory item: edit its text and/or set status — candidate→active
        to promote a captured candidate, →retired to drop it."""
        return tools.update_review_memory(project, item_id, text=text, status=status)

    @mcp.tool()
    def review_prompt(project: str, record_hit: bool = False) -> dict:
        """The assembled review prompt for a project: the base review_prompt plus the
        ACTIVE review-memory checklist. Fetch this when starting a fresh-context review so
        the reviewer checks this project's accumulated standards; pass record_hit=true to
        count the review (bumps each active item's hit count)."""
        return tools.review_prompt(project, record_hit=record_hit)

    @mcp.tool()
    def add_criterion(ticket_id: str, text: str) -> dict:
        """Add an acceptance criterion (definition-of-done checklist item) to a ticket."""
        return tools.add_criterion(ticket_id, text)

    @mcp.tool()
    def check_criterion(ticket_id: str, criterion_id: str, done: bool = True) -> dict:
        """Mark an acceptance criterion done (or not-done with done=False)."""
        return tools.check_criterion(ticket_id, criterion_id, done=done)

    @mcp.tool()
    def edit_criterion(ticket_id: str, criterion_id: str, text: str) -> dict:
        """Edit the text of an acceptance criterion."""
        return tools.edit_criterion(ticket_id, criterion_id, text)

    @mcp.tool()
    def remove_criterion(ticket_id: str, criterion_id: str) -> dict:
        """Remove an acceptance criterion from a ticket."""
        return tools.remove_criterion(ticket_id, criterion_id)

    @mcp.tool()
    def link_ticket(ticket_id: str, type: str, other_id: str) -> dict:
        """Relate two tickets. `type` is one of blocks | related | duplicate | parent, read
        as "<ticket_id> <type> <other_id>": blocks → ticket_id blocks other_id; duplicate →
        ticket_id is a duplicate of other_id; parent → other_id becomes ticket_id's parent
        (ticket_id is the sub-ticket); related is symmetric. The inverse shows on the other
        ticket. Re-linking an identical pair is a no-op (deduped). Rejects self-links, a
        second parent, and parent cycles. Relations appear in show_ticket's `relations`."""
        return tools.link_ticket(ticket_id, type, other_id)

    @mcp.tool()
    def unlink_ticket(
        ticket_id: str,
        other_id: str,
        type: str | None = None,
        direction: str | None = None,
    ) -> dict:
        """Remove the relationship(s) between two tickets. Pass `type` to remove only that
        relation type; omit it to remove every link between the pair. `direction` ('out' =
        ticket_id is the stored from, 'in' = it is the to) pins one orientation — only needed
        to disambiguate a pair that holds opposing directional links (e.g. A blocks B and B
        blocks A)."""
        return tools.unlink_ticket(ticket_id, other_id, type=type, direction=direction)

    return mcp
