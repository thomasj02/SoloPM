"""The SoloPM MCP server (stdio) built on FastMCP.

`solopm mcp` launches this. Each tool is a thin wrapper over :class:`SoloPMTools`,
which operates the same canonical service the CLI and web app use. Writes are
attributed to the configured agent.
"""

from mcp.server.fastmcp import FastMCP

from ..core.models import DEFAULT_BRANCH_CONVENTION, DEFAULT_REVIEW_PROMPT
from ..core.service import Service
from .tools import SoloPMTools

INSTRUCTIONS = (
    "SoloPM is an AI-first Kanban tracker for solo developers. These tools let you read "
    "and drive tickets: list projects and tickets, create / edit / delete projects, fetch "
    "a ticket's full context, create and edit tickets, comment, assign, and move tickets "
    "through the workflow (backlog → todo → in-progress → in-ai-review → in-human-review → "
    "done, plus cancelled). Your writes are attributed to your agent identity. Call "
    "workflow_info for the legal states and transition rules. Note: only the human may "
    "move a ticket to 'done' — you cannot close a ticket. Deleting a project with tickets "
    "requires force=true (it cascade-deletes all of them)."
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
    def create_project(
        key: str,
        name: str,
        repo: str | None = None,
        master: str = "main",
        branch_convention: str = DEFAULT_BRANCH_CONVENTION,
        default_implementer: str = "claude",
        default_reviewer: str = "codex",
        review_prompt: str = DEFAULT_REVIEW_PROMPT,
    ) -> dict:
        """Register a new project. `key` is the uppercase ticket prefix (e.g. SOLO; lowercase
        is normalized). `repo` is an optional local git-repo path (project ↔ repo is 1:1) and
        `master` its base branch. The branch convention, default implementer/reviewer, and
        review prompt have sane defaults and are editable later. Returns the new project."""
        return tools.create_project(
            key=key,
            name=name,
            repo=repo,
            master=master,
            branch_convention=branch_convention,
            default_implementer=default_implementer,
            default_reviewer=default_reviewer,
            review_prompt=review_prompt,
        )

    @mcp.tool()
    def edit_project(
        key: str,
        name: str | None = None,
        repo: str | None = None,
        master_branch: str | None = None,
        branch_convention: str | None = None,
        default_implementer: str | None = None,
        default_reviewer: str | None = None,
        review_prompt: str | None = None,
    ) -> dict:
        """Update one or more of a project's config fields; pass only the fields to change
        (omitted fields are left as-is). Editable: name, repo, master_branch,
        branch_convention, default_implementer, default_reviewer, review_prompt. Returns the
        updated project."""
        return tools.edit_project(
            key,
            name=name,
            repo=repo,
            master_branch=master_branch,
            branch_convention=branch_convention,
            default_implementer=default_implementer,
            default_reviewer=default_reviewer,
            review_prompt=review_prompt,
        )

    @mcp.tool()
    def delete_project(key: str, force: bool = False) -> dict:
        """Delete a project. A project that still has tickets is refused unless `force=true`,
        which cascade-deletes the project and ALL its tickets, their activity, and their
        relationship links (including cross-project links to/from those tickets) — this is
        irreversible. Returns {key, deleted, tickets_deleted}."""
        return tools.delete_project(key, force=force)

    @mcp.tool()
    def prune_merged_branches(project: str, apply: bool = False) -> dict:
        """Clean up the project repo's local branches whose work is merged — recorded on a
        DONE ticket, with a gone upstream (remote deleted on merge), or reachable-merged into
        master. The current branch and master are never touched. Dry-run by default (lists
        what WOULD be pruned); pass apply=true to delete: a branch in a clean git worktree has
        the worktree removed first, while a worktree with uncommitted changes is skipped. No-op
        without a repo / git. Returns {project, applied, pruned:[{branch,reasons,worktree}],
        skipped:[{branch,reason}]}."""
        return tools.prune_merged_branches(project, apply=apply)

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
        tags: list[str] | None = None,
    ) -> dict:
        """List tickets (the board), optionally filtered by project key, state, assignee, or
        `tags` (case-insensitive; a ticket must carry ALL given tags). Returns trimmed ticket
        summaries (each includes its `tags`)."""
        return tools.list_tickets(project=project, state=state, assignee=assignee, tags=tags)

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
    def graph(
        project: str | None = None,
        around: str | None = None,
        depth: int = 1,
        active_only: bool = False,
        types: list[str] | None = None,
    ) -> dict:
        """The ticket-relationship dependency graph (read-only). Pass `around` (+ `depth`)
        for the ego-graph within N hops of a ticket, or `project` for that project's
        relational subgraph (cross-project neighbours included; isolated tickets omitted);
        neither = the whole store. `types` filters relation types (blocks|related|duplicate|
        parent); `active_only` drops done/cancelled. Returns {nodes:[{id,project,title,state,
        assignee,blocked,subtickets}], edges:[{from,to,type}] in canonical direction,
        cycles:[[ids]] (blocks loops), scope, truncated}. Useful for topological reasoning,
        e.g. which ticket unblocks the most work."""
        return tools.graph(
            project=project, around=around, depth=depth, active_only=active_only, types=types
        )

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
    def tag_ticket(ticket_id: str, tags: list[str]) -> dict:
        """Add one or more free-form tags/labels to a ticket. Tags are normalized to
        lowercase (letters/digits/'-'/'_', e.g. 'bug', 'tech-debt'); the stored set is unique
        and sorted. Adding an already-present tag is a no-op. Returns the updated ticket."""
        return tools.tag_ticket(ticket_id, tags)

    @mcp.tool()
    def untag_ticket(ticket_id: str, tag: str) -> dict:
        """Remove a tag from a ticket (case-insensitive). Removing an absent tag is a no-op.
        Returns the updated ticket."""
        return tools.untag_ticket(ticket_id, tag)

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
