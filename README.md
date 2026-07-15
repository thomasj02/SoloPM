# SoloPM

**An AI-first project management system for solo developers.**

SoloPM is a fast, Linear-like Kanban ticket tracker built for one person shipping
software — and its distinguishing feature is that **AI coding agents are first-class
actors**: a ticket can be assigned to an agent the same way it's assigned to a person,
and (in later tiers) the agent works the ticket in a live, observable session.

The human works through a **web app**. Agents work through a **CLI** that exposes the
same operations with structured, machine-readable (`--json`) output. Both are thin
clients of one local backend — the CLI is the canonical API; the web app is a client of
the same operations.

> This is the **MVP (Tier 0)** — the dogfoodable tracker core: projects, tickets, the
> full seven-state workflow with two-stage review, assignment, comments, and an
> activity log, over both the web and the CLI. The agent-execution automation (tmux
> sessions, git worktrees, GitHub PRs, the automated review pipeline — Tier 1) is
> deliberately deferred; the data model and state machine are built to accept it.

---

## Quick start

Requires [`uv`](https://docs.astral.sh/uv/) and Python ≥ 3.11, plus Node ≥ 18 to build
the web app.

```bash
# 1. install Python dependencies into a managed virtualenv
uv sync

# 2. build the web app (TypeScript + Vite) into src/solopm/web/dist
npm --prefix frontend install
npm --prefix frontend run build

# 3. create the local store (once per machine)
uv run solopm init

# 4. run the backend + web app
uv run solopm serve
#    → SoloPM serving on http://127.0.0.1:8787
```

Open **http://127.0.0.1:8787** in your browser for the Kanban board.

The CLI talks to that same running backend:

```bash
uv run solopm project add --key SOLO --name SoloPM --repo ~/code/solopm --master main
uv run solopm ticket create --project SOLO --title "Build session start command" -d "…"
uv run solopm ticket list --project SOLO
```

(After `uv sync`, you can drop the `uv run` prefix from inside an activated venv, or use
`uv run solopm …` from anywhere.)

---

## The dogfood loop

Once a project exists you can run SoloPM's own backlog through SoloPM, launching agents
by hand and having them call `solopm` to read tickets, post progress, and move state:

```bash
solopm ticket assign SOLO-1 claude
# launch Claude by hand in the repo; inside that session the agent runs:
solopm ticket show SOLO-1 --json --agent claude          # fetch its own context
solopm ticket comment SOLO-1 -b "Implemented; opening for review." --agent claude
solopm ticket move SOLO-1 in-ai-review --agent claude    # self-transition to review
# a reviewer agent moves it forward:
solopm ticket move SOLO-1 in-human-review --agent codex
# only the human can close it:
solopm ticket move SOLO-1 done
```

Agents cannot reach `done` — SoloPM rejects it (`forbidden_transition`). That guard, the
transition graph, and attribution are all enforced in the one shared service layer.

---

## Workflow & states

```
Backlog → Todo → In Progress → In AI Review → In Human Review → Done
```

with **Cancelled** reachable from any non-terminal state.

| State | Meaning |
|---|---|
| **Backlog** | Captured but not committed to. |
| **Todo** | Committed to, ready to pick up. |
| **In Progress** | Actively being worked. |
| **In AI Review** | Implementation done; awaiting a fresh-context agent review. |
| **In Human Review** | Passed AI review; awaiting human acceptance. |
| **Done** | Accepted and closed. **Human-only.** |
| **Cancelled** | Abandoned. Terminal. |

---

## CLI reference (Tier 0)

Global flags on write/read commands: `--json` (structured output — agents should always
pass this), `--agent <name>` (attribute the action, e.g. `claude`/`codex`; absent ⇒
`human`), `--url` (backend override).

| Command | Description |
|---|---|
| `solopm init` | Create the local store. |
| `solopm serve [--host --port]` | Run the backend + web app. |
| `solopm radar [--project]` | Overlap radar — warn when active worktrees touch the same files. |
| `solopm graph [--project｜--around <id> --depth N] [--type T --active-only]` | Dependency graph of ticket relationships (nodes + typed edges; blocks cycles flagged). |
| `solopm project add --key --name [--repo --master]` | Register a project. |
| `solopm project list` | List projects. |
| `solopm project show <key>` | Show a project's config. |
| `solopm project set <key> <field> <value>` | Edit one config field. |
| `solopm ticket create --title [-d --project --state --assignee]` | Create a ticket. |
| `solopm ticket list [--project --state --assignee]` | The board query. |
| `solopm ticket show <id>` | Full ticket detail (what agents are seeded to fetch). |
| `solopm ticket edit <id> [--title -d]` | Edit title/description. |
| `solopm ticket comment <id> -b "…"` | Append a comment. |
| `solopm ticket move <id> <state>` | Transition state (validated). |
| `solopm ticket assign <id> <assignee>` | Assign `human｜claude｜codex｜unassigned`. |
| `solopm ticket reorder <id> [--after <id>]` | Reorder within a column (drag-and-drop in the web; no state change). |
| `solopm ticket link <id> <type> <other>` | Relate two tickets (`blocks｜related｜duplicate｜parent`); `link A parent B` ⇒ B is A's parent. |
| `solopm ticket unlink <id> <other> [--type]` | Remove the link(s) between two tickets. |
| `solopm review submit <id> --verdict pass\|fail [-c notes]` | Report an AI-review verdict: pass → in-human-review, fail → kick back to in-progress. |
| `solopm ticket criteria add <id> "…"` | Add an acceptance criterion (definition-of-done checklist item). |
| `solopm ticket criteria check <id> <cid> [--uncheck]` | Tick (or untick) a criterion. |
| `solopm ticket criteria edit\|remove <id> <cid> […]` | Edit a criterion's text / remove it. |
| `solopm review memory list\|add\|set <project> […]` | Curate the per-project review memory (the learning review gate). |
| `solopm review prompt <project>` | Print the assembled review prompt (base + active review memory). |

The full JSON contract is in [`API.md`](./API.md).

---

## AI agents via MCP

SoloPM ships an **MCP (Model Context Protocol) server** so an AI agent (Claude in Claude
Code, Claude Desktop, etc.) can drive SoloPM as a set of tools — a native alternative to
the CLI.

```bash
solopm mcp                 # run the stdio MCP server (attributed to "claude")
solopm mcp --agent codex   # attribute writes to a different agent
```

It's a thin layer over the **same canonical service** the CLI and web app use, so the
workflow and actor rules hold identically — an agent still **cannot** move a ticket to
`done`. It talks to the local store directly (no `solopm serve` required), and because
that's the same SQLite file the web app reads, an agent's MCP writes show up live on the
board.

**Tools:** `list_projects`, `create_project`, `edit_project`, `delete_project`,
`workflow_info`, `list_tickets`, `show_ticket`, `create_ticket`, `edit_ticket`,
`comment_ticket`, `move_ticket`, `reorder_ticket`, `assign_ticket`, `submit_review`,
`add_criterion`, `check_criterion`, `edit_criterion`, `remove_criterion`,
`tag_ticket`, `untag_ticket`, `link_ticket`, `unlink_ticket`, `radar`, `graph`,
`prune_merged_branches`, `list_review_memory`, `add_review_memory`,
`update_review_memory`, `review_prompt`.

Register the server with your MCP client by pointing it at `uv run solopm mcp` (cwd =
repo root) — e.g. in Claude Code, `claude mcp add solopm -- uv run solopm mcp`.
Registering it at user scope makes SoloPM available across all your projects.

### HTTP mode (remote machines)

`solopm mcp --url http://host:8787` runs the same MCP server against a **remote**
backend instead of opening the local store: every tool call becomes a request to the
HTTP API served by `solopm serve`, so business rules stay server-side and results and
errors are identical to local mode (writes are attributed via the `X-SoloPM-Actor`
header, so `--agent` must be one of the API's actors: `human`, `claude`, `codex`).
Use it to give an agent on another machine access to the same board — e.g. on that
machine:

```bash
pipx install solopm   # or any install that puts `solopm` on PATH
claude mcp add solopm -s user -- solopm mcp --url http://workstation:8787
```

Notes: HTTP mode is only entered via the explicit `--url` flag (the `SOLOPM_URL` env
var never flips the MCP server to HTTP), and the backend must be reachable from the
remote machine. **The API has no authentication** (SoloPM is single-user and
local-first), so the recommended transport is an SSH tunnel — the backend keeps its
default loopback bind and SSH provides the auth:

```bash
ssh -N -L 8787:127.0.0.1:8787 workstation &   # on the remote machine
claude mcp add solopm -s user -- solopm mcp --url http://127.0.0.1:8787
```

Alternatively, on a **fully trusted network only**, bind the backend directly: set
`SOLOPM_HOST` and list the name(s) remote clients dial in `SOLOPM_ALLOWED_HOSTS`
(comma-separated; the Host-header guard rejects unknown hosts — it stops DNS
rebinding, **not** direct access, so anyone who can reach the port can read and write
the tracker, including as `human`):

```bash
SOLOPM_HOST=0.0.0.0 SOLOPM_ALLOWED_HOSTS=workstation,192.168.1.50 solopm serve
```

Repo-filesystem tools (`radar`, `prune_merged_branches`, PR automation on
`move_ticket`) execute on the backend's machine, which is where the repos live; and
`--channel` requires the local store, so it can't be combined with `--url` (the API has
no activity feed to poll — yet).

### Channel mode (proactive notifications)

`solopm mcp --channel` runs the MCP server as a Claude Code **channel** (a
[research-preview](https://code.claude.com/docs/en/channels-reference) feature, Claude Code
≥ 2.1.80): instead of only answering tool calls, it **pushes** events into the live session —
ticket transitions, review kickbacks, new comments/assignments, and overlap-radar hits made
by the web app, the CLI, or another agent. Events arrive wrapped in a
`<channel source="solopm" …>` tag, scoped (`--scope mine|all`) so the session isn't spammed
with unrelated activity.

Because custom channels aren't on the research-preview allowlist, register it and load it
with the development flag:

```jsonc
// .mcp.json (in the repo)
{ "mcpServers": { "solopm": { "command": "uv", "args": ["run", "solopm", "mcp", "--channel"] } } }
```
```bash
claude --dangerously-load-development-channels server:solopm
```

Then a change from outside the session — e.g. `solopm ticket move SOLO-1 in-progress --agent codex`
in another terminal, or a move on the web board — appears live as
`<channel source="solopm" ticket_id="SOLO-1" kind="state_change" …>…</channel>`. One-way for
now (notifications only; no reply tool).

---

## Architecture

Local-first; the backend runs on your machine.

```
src/solopm/
  core/        # the canonical operations — pure, fully tested
    models.py      # enums + value objects
    workflow.py    # the state machine (legal transitions + actor rules)
    store.py       # SQLite persistence
    service.py     # business logic — the single source of truth
    errors.py      # domain errors with stable codes
  server/      # FastAPI: a thin HTTP wrapper over core, + serves the web app
  cli/         # Typer + httpx: the canonical agent-facing client
  mcp/         # MCP server (FastMCP): SoloPM operations as tools for AI agents
  web/dist/    # built web app (generated by Vite; gitignored)
frontend/      # the web app source — TypeScript + Vite (the human Kanban interface)
  src/         # typed modules: types.ts, api.ts, store.ts, board.ts, ticket.ts, …
config.py      # store location + server binding (env-overridable)
```

The frontend is a TypeScript SPA built with Vite into `src/solopm/web/dist/`, which the
FastAPI server serves as static files. `frontend/src/types.ts` mirrors the API contract,
so the web client is type-checked against the backend's shapes.

- **One backend, two clients.** The web app and CLI both speak the HTTP API in
  [`API.md`](./API.md). All business rules live in `core/service.py`, so the interfaces
  can't drift.
- **Storage:** a single SQLite file at `~/.solopm/solopm.db` (override with
  `SOLOPM_HOME` or `SOLOPM_DB`).
- **No auth.** Single-user and local. Writes are attributed (web ⇒ `human`, CLI
  `--agent` ⇒ the named agent) purely for the activity log.

### Environment variables

| Var | Purpose | Default |
|---|---|---|
| `SOLOPM_HOME` | Directory holding the store | `~/.solopm` |
| `SOLOPM_DB` | Full path to the SQLite file | `$SOLOPM_HOME/solopm.db` |
| `SOLOPM_HOST` / `SOLOPM_PORT` | Server bind address | `127.0.0.1` / `8787` |
| `SOLOPM_ALLOWED_HOSTS` | Extra Host-header values the server accepts (comma-separated) — for remote clients | — |
| `SOLOPM_URL` | CLI → backend base URL | `http://$HOST:$PORT` |
| `SOLOPM_PROJECT` | Default project key for the CLI | — |

---

## Development

**Backend** (Python):

```bash
uv sync --extra dev
uv run pytest          # full suite
```

Tests cover the state machine, the store, the service operations, the HTTP API, and the
CLI (driven against an in-process backend). New behavior is added test-first.

**Frontend** (TypeScript):

```bash
cd frontend
npm install
npm run dev            # Vite dev server on :5173, proxying /api → :8787 (run `solopm serve` too)
npm run typecheck      # tsc --noEmit (strict)
npm run test           # vitest (markdown XSS safety)
npm run build          # type-check + bundle into ../src/solopm/web/dist
```

In dev, run `uv run solopm serve` (backend) and `npm run dev` (Vite, with HMR) together,
and open the Vite URL. For a production-style run, `npm run build` then `uv run solopm
serve` serves the built app at `:8787`.

---

## License

MIT.
