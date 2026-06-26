# SoloPM — Product Brief

*Draft v0.6*

## Summary

SoloPM is an AI-first project management system for solo developers. It takes the parts of Linear that matter to one person shipping software — a fast, opinionated ticket tracker with a clean Kanban flow — and strips out everything built for teams. Its distinguishing feature is that AI coding agents are first-class actors in the system: a ticket can be assigned to an agent the same way it's assigned to a person, and the agent works the ticket in a live, observable session.

The human works through a website. Agents work through a command-line tool that exposes the same functionality with structured, machine-readable output.

## Problem & motivation

Tools like Linear and Jira are built around the assumption of a team: multiple humans coordinating, with permissions, assignment routing, review gates, and reporting to manage the coordination overhead. A solo developer carries none of that overhead, so most of the surface area is dead weight that slows the tool down and clutters the interface.

At the same time, the way solo developers actually work is changing. A growing share of implementation is delegated to coding agents (Claude, Codex). But today those agents are invoked ad hoc from a terminal, with no shared source of truth for what work exists, what's in flight, and what's been completed. The developer ends up being the integration layer between "my backlog" and "what the agent is doing right now."

SoloPM closes that gap. It is a single source of truth for the work, where the agent is a real assignee with an execution loop, and where the human can watch, steer, and review without leaving the tracker.

## Target user

A solo developer — or a small team operating in a solo-style, low-process way — who:

- delegates a meaningful fraction of implementation to coding agents,
- wants one place to track what needs doing across one or more projects,
- values speed and keyboard-driven flow over configurability and governance.

## Goals

- A fast, Linear-like ticket tracker that feels good for one person.
- AI agents as first-class assignees, not a bolt-on integration.
- Full functional parity between the human (web) and agent (CLI) interfaces.
- Live observability into agent work: is it running, and what is it doing?
- A frictionless review loop: agent does the work, human reviews and accepts.

## Non-goals

Explicitly out of scope, because they exist to manage team coordination:

- Permissions and roles
- User management / multi-user accounts
- SLAs, due-date escalation, and reporting against them
- Billing, plans, seats
- Sprint/velocity reporting and team analytics

These can be revisited if the product ever expands beyond the solo use case, but they should not shape v1.

## Core concepts

### Projects

SoloPM supports **multiple projects**. A solo dev typically juggles several repos/efforts, so tickets are scoped to a project. Each project has its own ticket sequence (e.g. `SOLO-42`, `BLOG-7`) and its own board. The web app and CLI both operate within a selected project context.

**A project maps 1:1 to a git repository.** Project configuration settings hold everything SoloPM needs to drive work in that repo:

- **Project name** and ticket key/prefix.
- **Repository location** (local path to the repo).
- **Master branch name** (the base for new branches and the merge target).
- **Branch naming convention** — SoloPM creates and owns ticket branches from this.
- **Default review prompt** — the per-project custom Claude review prompt (overridable per ticket).
- Default implementer/reviewer agents, and other per-project defaults.

### Tickets

The atomic unit of work. A ticket has at minimum:

- **ID** — short, stable, human-typable, project-scoped (e.g. `SOLO-42`).
- **Project** — the project it belongs to.
- **Title** and **description** (markdown).
- **State** — one of the workflow states below.
- **Assignee** — the human, a configured agent (Claude / Codex), or unassigned.
- **Activity / comments** — a chronological log of changes and notes, including agent session transcripts.
- **Git branch** — the SoloPM-named branch the work lives on, created when work starts (see git lifecycle).
- **Commits & PR** — tracked commits on the branch and the associated pull request link.
- **Agent session ID(s)** — the resumable session identifier(s) for the agent work on this ticket, so a session can be re-attached or resumed (e.g. `claude --resume <session id>`).
- **Timestamps** — created, updated.

Likely useful, to decide during iteration: **priority**, **labels**, **sub-tasks / parent links**, **estimate**.

### Workflow & states

Tickets move through a Kanban flow with a **two-stage review** — an AI review pass followed by a human review pass:

```
Backlog → Todo → In Progress → In AI Review → In Human Review → Done
```

with **Cancelled** as an additional terminal state reachable from anywhere.

| State | Meaning |
|---|---|
| **Backlog** | Captured but not yet committed to. The holding pen. |
| **Todo** | Committed to and ready to be picked up. |
| **In Progress** | Actively being worked (by the human or a running agent session). |
| **In AI Review** | Implementation is complete; an agent reviews the work before it reaches the human. |
| **In Human Review** | The work has passed AI review and is awaiting human acceptance. |
| **Done** | Accepted and closed; the GitHub PR is merged into master, and the branch/worktree are torn down. |
| **Cancelled** | Abandoned; will not be done. The branch/worktree are torn down. Terminal. |

**The AI review pass** is a distinct quality gate, not just the implementing agent declaring itself done. SoloPM ships a **built-in "start review" action** that launches a review session with fresh eyes. The mechanism is per agent:

- **Codex** — runs `codex review`.
- **Claude** — runs a custom SoloPM review prompt (a fresh-context, max-thinking review). We deliberately do **not** use `claude ultrareview` for this, due to its extremely high cost.

Reviewing from a clean context matters: it avoids the implementing session rubber-stamping its own work. **By default the reviewer is a different agent than the implementer** — the default pairing is *implement with Claude, review with Codex* — but this is not strictly required, and the human can override the reviewer on a **per-ticket** basis.

**Entering AI review.** The implementing agent can **self-transition In Progress → In AI Review** when it believes the work is done. Doing so requires **committing the work to a git branch**, and the ticket records that branch name in its metadata. The branch is what the review session then examines.

**Who can transition what:**

- The implementing agent (by committing to a branch) or the human can move a ticket into *In AI Review*.
- A review session (via the built-in review action) moves *In AI Review → In Human Review*.
- Only the **human** moves *In Human Review → Done*. An agent cannot close a ticket.
- The human can **request changes** from *In Human Review*, which sends the ticket back to *In Progress* (see below). Any actor can send a ticket to *Cancelled*.

**When AI review finds problems**, the review session reports its outcome by **calling `solopm` with a verdict and comments** — the review notes are written to the ticket's comments. The ticket is kicked back to *In Progress* and the implementing agent is triggered to read those review comments and address them (resuming its implementation session, which holds the relevant context). Once the agent re-submits, a **re-review starts a new session** rather than resuming the prior review — each review pass examines the work fresh.

## Interfaces

SoloPM has two interfaces over one backend. They are deliberately at parity — anything the human can do on the website, an agent can do via the CLI, and vice versa.

### Human interface — web app

The primary surface for the human. A fast, keyboard-driven Kanban board and ticket detail view. This is where you triage the backlog, create and edit tickets, assign work, watch agent sessions live, and review completed work.

### Agent interface — command-line tool

The surface for AI agents (and power-user humans). Same operations as the web app — create, read, update, transition, assign, comment — but with **structured, AI-friendly output** (e.g. JSON) so an agent can parse results reliably rather than scraping prose. The CLI is how an agent reads its assigned ticket, reports progress, and moves the ticket through states.

**Identity & auth.** Because SoloPM is single-user and the backend runs locally, no authentication is required. Actions taken through the web app are assumed to be the human. The CLI accepts an `--agent <agent name>` flag so an agent can self-identify (e.g. `solopm ticket update SOLO-42 --state in-ai-review --agent codex`); CLI calls without the flag are attributed to the human. This is purely for attribution in the activity log, not access control.

Design principle: the CLI is the canonical API. The web app is a client of the same operations. This keeps parity honest and avoids the two interfaces drifting.

## Agent integration

This is the part that makes SoloPM distinct, so it deserves the most detail.

### Configured agents

For v1, the supported agents are **Claude** and **Codex**. Each is a configured backend with whatever command/environment is needed to launch it.

### Assignment vs. starting work

These are two separate steps:

- **Assignment** sets who owns the ticket (the human or a configured agent). Either a human (web) or an agent (CLI) can assign. Assignment alone does **not** start any work.
- **Starting a session** is an explicit action. A human or an agent explicitly starts a work session on a ticket; only then does an agent begin executing. This keeps the developer in control of when (and how many) agent processes spin up.

### Sessions & execution via tmux

When work is started on an agent-assigned ticket, the agent runs inside a **tmux session**. tmux gives us a real, persistent terminal the agent operates in, the ability to attach/observe without interrupting, and straightforward capture of the output as a transcript.

**Sessions run in git worktrees.** SoloPM creates a worktree for the ticket's branch so each session has an isolated working tree. This is what makes the no-cap concurrency safe: multiple tickets can have live sessions at once without stepping on each other's checkout. There is **no guard against two tickets touching overlapping code** — that's left to the developer, consistent with the no-concurrency-cap stance. Worktrees and branches are torn down when the ticket reaches *Done* or *Cancelled*. When launching the *In Progress* session, SoloPM seeds the agent with a **suggested CLI command to fetch the ticket details** (e.g. `solopm ticket show SOLO-42`) so it can pull its own context rather than being pre-loaded with it.

**Git automation is agent-only, and so is the review pipeline.** The branch / worktree / PR machinery and the AI-review → human-review flow are designed around *agent* work. A human is free to work a ticket too, but SoloPM does not create branches, worktrees, or PRs on their behalf, and the automated review pipeline does not apply — the human manages git themselves and can **manually trigger an AI review** if they want one.

**One session per ticket per state.** A ticket has at most one live session within a given state. The session that does the implementation (*In Progress*) is distinct from the session that does the AI review (*In AI Review*) — each state gets its own session. Transcripts from each session accumulate on the ticket so the full history across states is preserved.

**Sessions are resumable.** Each session's resumable ID is stored on the ticket. Whenever a ticket returns to *In Progress* — whether the human **requested changes** from In Human Review or **AI review kicked it back** — the existing implementation session is **resumed** rather than started fresh, since the prior session carries the most relevant context (e.g. `claude --resume <session id>`). Note the asymmetry: implementation sessions resume, but each *review* pass runs as a new session.

Reconciling a resumed session against a working tree that has drifted (branch moved on, dependencies changed) is left to the agent/human, not handled by SoloPM.

The agent reports progress and drives state purely by **calling `solopm` from within its session** (e.g. updating the ticket, transitioning state, committing the branch, leaving comments). The transcript is the raw record of what happened; the CLI calls are the structured signal.

### Live status & transcript

On the ticket, the human can see:

- **A clear visual indicator of whether the ticket is actively being worked on by an agent** — i.e. is there a live session running right now — distinct from a ticket that merely sits in an "In ..." state with no active session.
- **A transcript of the work session(s)** — the **raw terminal stream** from the agent's tmux session, surfaced in the ticket view (to start; structured turn parsing is a later refinement).

This turns the ticket into the place you watch the work happen, not just a record of it.

### Lifecycle (proposed)

1. Ticket created (human or agent), lands in Backlog/Todo.
2. Ticket assigned to an agent (default implementer: Claude).
3. A human or agent **explicitly starts a session**; SoloPM creates the ticket's branch (per the project naming convention) and a worktree, the ticket moves to **In Progress**, and the active-work indicator turns on. The agent is given a `solopm ticket show` command to fetch its context; the session ID is recorded.
4. The agent works, streaming a raw transcript to the ticket and calling `solopm` to update progress and commit.
5. When done, the agent self-transitions to **In AI Review** (commits tracked on the branch); the implementation session ends (indicator off).
6. The built-in **start-review action** launches a *new* review session — `codex review`, or the custom Claude review prompt — using the configured reviewer (default Codex, overridable per ticket). The review session reports its verdict and notes via `solopm` (notes land in ticket comments). On passing, the ticket moves to **In Human Review**; on failing, it returns to **In Progress** and the implementing agent is triggered to address the comments (loop back to step 4).
7. The human reviews and either accepts → **Done** (the GitHub PR is squash-merged into master; branch and worktree torn down), **requests changes** → back to *In Progress* (resuming the implementation session), or sends it to **Cancelled** (GitHub PR closed; branch and worktree torn down). If a session is still live at Done/Cancelled, SoloPM shuts it down cleanly or kills it before teardown.

## Rough architecture

SoloPM is **local-first**: the backend runs on the developer's own machine, alongside tmux and the agent processes it orchestrates. A likely shape:

- **Backend / store** — runs locally; holds projects, tickets, and state; exposes the canonical operations.
- **CLI** — thin client over the backend; structured output; `--agent` flag for attribution.
- **Web app** — client over the same backend, served locally.
- **Session orchestrator** — launches/monitors tmux sessions, tracks whether a session is live (the active-work indicator), and pipes the raw transcript back to the ticket (e.g. via `tmux pipe-pane` / `capture-pane`).
- **GitHub integration** — uses the **`gh` CLI** to talk to GitHub. Owns the ticket branch (created from the project's master branch per the naming convention), manages a worktree per session, pushes the branch and opens/tracks the **GitHub PR** and its commits. At *Done* it **squash-merges** the PR into master; at *Done* and *Cancelled* it tears down the branch and worktree, and *Cancelled* also **closes the GitHub PR**. If a session is still live when a ticket reaches Done/Cancelled, SoloPM cleanly shuts it down if possible, or kills it if necessary, before teardown. GitHub is the only supported forge to start. This git automation is **agent-only** — see note below.

**Session recovery.** On a clean shutdown, sessions exit cleanly and are resumable later via their stored session ID. On restart, the orchestrator re-attaches to any sessions still alive; sessions that ended can be resumed on demand. There is **no cap** on how many agent sessions run concurrently on the machine — that's left to the developer.

Because it's single-user and local, there is no authentication layer; identity is attribution-only (web = human, CLI `--agent` = the named agent).

## MVP scope (proposed)

A first version that proves the core loop:

- Multiple projects, each with a Kanban board (web).
- Tickets with the full seven-state workflow (incl. two-stage review + Cancelled).
- Create / edit / transition / assign via both web and CLI, with `--agent` attribution.
- CLI with structured JSON output.
- Local backend + locally served web app.
- Assign-to-agent and explicitly start a session for **one** agent (Claude), launching a tmux session.
- Active-work visual indicator + raw transcript on the ticket.
- AI review pass (fresh-context review) → In Human Review → manual human accept → Done.

Deferred past MVP: second agent (Codex), labels/priority/sub-tasks, structured (non-raw) transcripts, richer activity feed, anything in Non-goals.

## Resolved decisions

1. **Local-first.** The backend runs on the developer's machine.
2. **Two-stage review.** Workflow is Backlog → Todo → In Progress → In AI Review → In Human Review → Done (+ Cancelled). Agents move In AI Review → In Human Review via a fresh-context review (agent-specific review command or a max-thinking review prompt); `claude ultrareview` is excluded on cost grounds. Only the human moves to Done.
3. **One session per ticket per state.**
4. **Raw terminal stream** for transcripts to start.
5. **Agents report progress by calling `solopm`.**
6. **Multiple projects.**
7. **Sessions are explicitly started** by a human or agent (not auto-started on assignment), with a clear visual indicator of active agent work.
8. **No auth.** Web actions = human; CLI uses `--agent <name>` for self-identification. Attribution only.
9. **Built-in "start review" action.** Codex uses `codex review`; Claude uses a custom SoloPM review prompt (not `claude ultrareview`).
10. **Reviewer defaults to a different agent.** Default pairing: implement with Claude, review with Codex; human can override per ticket.
11. **Self-transition to AI review via git.** The implementing agent moves In Progress → In AI Review by committing to a git branch, which is recorded on the ticket.
12. **Request changes → In Progress, resuming the existing session.** Sessions are resumable via a stored session ID; no fresh session required.
13. **Clean-shutdown sessions are resumable; re-attach on restart.** No cap on concurrent local sessions.
14. **AI review failure → In Progress.** The implementing agent is triggered to read and address the feedback (resuming its session); a re-review starts a new session.
15. **SoloPM owns git.** It creates/owns the branch naming convention, tracks commits and PR links, and merges the PR at *Done*.
16. **Project config lives in project settings** — repository location, master branch name, project name, branch convention, and the per-project default review prompt (overridable per ticket).
17. **Project ↔ repo is 1:1**, and sessions run in **git worktrees**.
18. **Session launch seeds a fetch command.** The In Progress session is launched with a suggested `solopm ticket show` command so the agent pulls its own context.
19. **Resume reconciliation is on the agent/human**, not SoloPM.
20. **GitHub only** as the forge to start: branches pushed, PRs opened/tracked, merged at Done.
21. **Worktrees and branches are torn down at Done and Cancelled.**
22. **No guard against overlapping code** between concurrent tickets — the developer's responsibility.
23. **Review verdict via `solopm`; notes live in ticket comments.**
24. **Git automation is agent-only.** Human-worked tickets get no SoloPM-managed branch/worktree/PR.
25. **Backlog refine/scope is manual prompting**, not a native SoloPM feature.
26. **GitHub via `gh` CLI; squash-merge at Done.**
27. **Cancelled closes the GitHub PR** and tears down the branch/worktree; a live session is cleanly shut down or killed before teardown.
28. **The full pipeline is agent-only.** Human-worked tickets bypass git automation and the automated review flow; the human can manually trigger an AI review if desired.

## Open questions / decisions to make

The product loop is fully specified — there are no remaining open product questions at the brief level. What's left is implementation detail to settle during the design/build phase, e.g.: exact CLI command surface and JSON schema, branch-naming format, transcript storage/rotation, PR-conflict handling at merge time, and the on-disk data store choice. None of these change the product definition.

