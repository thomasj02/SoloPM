# SoloPM — CLI Command Surface

*Draft v0.1 — companion to the product brief*

## Purpose & strategy

The CLI is SoloPM's canonical API: the web app and the agents are both clients of the same operations. This document specifies the command surface, and orders it around one goal — **reach a dogfoodable POC as fast as possible, so SoloPM can be used to build SoloPM.**

The key observation that shapes the ordering: SoloPM becomes useful as a tracker the moment ticket CRUD and state transitions work — *before* any tmux/worktree/PR automation exists. At that point you can already run SoloPM's own backlog through SoloPM, launching agents by hand and having them call `solopm` to read tickets, post progress, and move state. The heavy machinery (sessions, worktrees, GitHub) can then be built as tickets *inside* SoloPM.

So the surface is split into three tiers:

- **Tier 0 — POC core.** The tracker. Hand-drivable. This is the POC; everything here comes first.
- **Tier 1 — Agent execution.** Sessions, worktrees, GitHub PRs, the review flow. Builds the automation.
- **Tier 2 — Deferred.** Nice-to-haves explicitly punted (consistent with the brief's non-goals).

## Global conventions

**Invocation.** `solopm <noun> <verb> [args] [flags]`. Nouns: `project`, `ticket`, `session`, `review`, plus top-level `init` / `serve`.

**Backend.** The CLI is a thin client of the local backend. `solopm serve` runs the backend (and serves the web app); `solopm init` creates the local store. The CLI assumes the backend is reachable locally — no auth (single-user, local).

**Output.** Human-readable by default. `--json` emits a single structured JSON object on stdout. **Agents should always pass `--json`.** This is the AI-friendly contract.

**Attribution.** `--agent <name>` (e.g. `--agent claude`, `--agent codex`) marks the actor on any write. Absent the flag, the actor is the human. Attribution only — not access control.

**Resolution / context.** Inside a session worktree, SoloPM sets `SOLOPM_PROJECT` and `SOLOPM_TICKET` in the environment, so commands can omit the project key and ticket ID and they'll be inferred. Outside a session, pass `--project <key>` (or rely on the configured default); ticket IDs are always explicit. Project may also be inferred from the cwd's git repo, since project ↔ repo is 1:1.

**Exit & errors.** `0` on success, non-zero on failure. With `--json`, failures emit `{"error": {"code": "...", "message": "..."}}` and a non-zero exit, so agents can branch on it reliably.

**State identifiers.** `backlog`, `todo`, `in-progress`, `in-ai-review`, `in-human-review`, `done`, `cancelled`.

---

## Tier 0 — POC core

Everything needed to use SoloPM as a tracker, hand-drivable today. **Build this first; this is the POC.**

### Setup

| Command | Description |
|---|---|
| `solopm init` | Create the local store. Run once per machine. |
| `solopm serve` | Run the local backend + web app. |
| `solopm project add` | Register a project. Flags: `--key SOLO`, `--name "..."`, `--repo <path>`, `--master main`. (Branch convention, default agents, and review prompt have sane defaults; editable later.) |
| `solopm project list` | List projects. |
| `solopm project show <key>` | Show a project's full configuration. |
| `solopm project set <key> <field> <value>` | Edit one config field (e.g. the review prompt, default agents, branch convention). |
| `solopm project delete <key>` | Delete a project. Refused if it still has tickets unless `--force`, which cascade-deletes all of them (and their activity/relationships). Irreversible. |

### Tickets

| Command | Description |
|---|---|
| `solopm ticket create` | Create a ticket. Flags: `--title` (req), `--description`/`-d`, `--project`, `--state` (default `backlog`), `--assignee`. Prints the new ID. |
| `solopm ticket list` | The board query. Filters: `--state`, `--assignee`, `--project`, `--tag` (repeatable; AND across tags). `--json` for agents. |
| `solopm ticket show <id>` | Full ticket detail — fields, branch/PR, comments, session refs. **This is the command agents are seeded with to fetch their own context.** |
| `solopm ticket edit <id>` | Update `--title` / `--description`. |
| `solopm ticket comment <id>` | Append a comment. Flag: `--body`/`-b`. Used for progress notes *and* review notes. |
| `solopm ticket move <id> <state>` | Transition state. Validates legal transitions and the actor rules from the brief (only the human may reach `done`; agents may reach `in-ai-review`/`in-human-review`). In Tier 0 this is a pure state change; Tier 1 attaches git side effects (below). |
| `solopm ticket assign <id> <assignee>` | Assign to `human` \| `claude` \| `codex` \| `unassigned`. |
| `solopm ticket tag <id> <tags...>` | Add one or more free-form tags/labels (normalized to lowercase). |
| `solopm ticket untag <id> <tag>` | Remove a tag from a ticket. |

With just the above, the dogfood loop works manually: you create tickets for SoloPM's own features, assign one to an agent you launch by hand, the agent runs `solopm ticket show <id> --json` to read it, posts progress with `ticket comment`, and walks the ticket through states with `ticket move`. No orchestration required yet.

---

## Tier 1 — Agent execution

Adds the automation from the brief: explicit sessions in tmux + worktrees, GitHub PRs via `gh`, and the two-stage review flow. Each of these can itself be a SoloPM ticket.

### Sessions

| Command | Description |
|---|---|
| `solopm session start <id>` | Start an **implementation** session: create the ticket's branch (per project convention) + a git worktree, launch a tmux session running the assigned agent, seed it with the `solopm ticket show` command, record the session ID, set the active-work indicator, move ticket → `in-progress`. |
| `solopm session list` | Active/known sessions and their live status — the data behind the active-work indicator. `--json`. |
| `solopm session attach <id>` | Attach to the ticket's live tmux session (observe without interrupting). |
| `solopm session logs <id>` | The raw transcript stream for the ticket's session(s). |
| `solopm session stop <id>` | End the session: clean shutdown if possible, kill if necessary. |

One session per ticket per state; sessions are resumable via the stored session ID, and a ticket returning to `in-progress` (from either kickback path) resumes the existing implementation session rather than starting fresh.

### Git side effects on transition

In Tier 1, `solopm ticket move` (and the agent's self-transition) carry the GitHub automation, all via the `gh` CLI:

- → `in-ai-review`: push the branch, open/refresh the GitHub PR. (The agent self-transitions here once its work is committed.)
- → `done`: **squash-merge** the PR into master, then tear down branch + worktree (stopping any live session first).
- → `cancelled`: **close** the PR, tear down branch + worktree (stopping any live session first).

### Review

| Command | Description |
|---|---|
| `solopm review start <id>` | The built-in "start review" action. Launches a fresh **review** session with the configured reviewer (default: the other agent — implement Claude, review Codex; overridable per ticket). Codex runs `codex review`; Claude runs the project's custom review prompt (never `claude ultrareview`). |
| `solopm review submit <id>` | The reviewer reports its outcome. Flags: `--verdict pass\|fail`, `--comment`/`-c`. On `pass` → ticket moves to `in-human-review`. On `fail` → review notes are written to ticket comments, ticket returns to `in-progress`, and the implementing agent is triggered to address them. A re-review is always a new session. |

---

## Tier 2 — Deferred

Punted to keep the POC lean; revisit only if needed. Includes: labels, priority, sub-tasks/parent links, estimates; structured (non-raw) transcript parsing; bulk operations; multiple-forge support beyond GitHub; anything in the brief's non-goals (permissions, users, SLAs, billing).

---

## JSON contract (key reads)

Agents code against these shapes. Illustrative, to be firmed up in the design doc.

`solopm ticket show SOLO-42 --json`:

```json
{
  "id": "SOLO-42",
  "project": "SOLO",
  "title": "Add session attach command",
  "description": "…markdown…",
  "state": "in-progress",
  "assignee": "claude",
  "branch": "solo-42-add-session-attach",
  "pr": { "number": 17, "url": "https://github.com/…/pull/17", "state": "open" },
  "session": { "id": "claude-9f2a…", "active": true },
  "comments": [
    { "author": "claude", "body": "Pushed first pass.", "at": "2026-05-31T18:04:11Z" }
  ],
  "created_at": "2026-05-31T17:50:02Z",
  "updated_at": "2026-05-31T18:04:11Z"
}
```

`solopm ticket list --state todo --json` returns `{ "tickets": [ … ] }` with each entry a trimmed ticket (id, title, state, assignee, branch/pr summary).

---

## The dogfood loop, concretely

Once Tier 0 exists (agents launched by hand):

```
solopm init
solopm project add --key SOLO --name SoloPM --repo ~/code/solopm --master main
solopm ticket create --project SOLO --title "Build session start command" -d "…"
solopm ticket assign SOLO-3 claude
# launch Claude by hand in the repo, then inside that session:
solopm ticket show SOLO-3 --json --agent claude
solopm ticket comment SOLO-3 -b "Implemented; opening for review." --agent claude
solopm ticket move SOLO-3 in-ai-review --agent claude
```

Once Tier 1 lands, the manual launch collapses into:

```
solopm session start SOLO-3          # branch + worktree + tmux + Claude, → in-progress
# …agent works, self-transitions → in-ai-review (push + PR)…
solopm review start SOLO-3           # Codex reviews in a fresh session
# reviewer: solopm review submit SOLO-3 --verdict pass --agent codex  → in-human-review
solopm ticket move SOLO-3 done       # squash-merge PR, tear down
```

## Build order recommendation

1. `init`, `serve`, `project add/list/show`, store + data model.
2. `ticket create/list/show/edit/comment`, `ticket move`, `ticket assign` (+ `--json`, `--agent`). **← POC is dogfoodable here.**
3. `session start/list/attach/logs/stop` with tmux + worktrees.
4. GitHub side effects on transition (`gh`: push, PR, squash-merge, close) + teardown.
5. `review start` / `review submit` and the kickback wiring.

## Open questions

1. **`move` vs. dedicated verbs.** I folded git side effects onto `ticket move <state>` to keep the surface small. Alternative: explicit verbs (`solopm ticket open-pr`, `solopm ticket merge`) so the side effects are legible at the call site. Which do you prefer for the agent contract?
2. **Self-transition trigger.** Does the agent reach `in-ai-review` via `ticket move … in-ai-review` (push+PR as a side effect), or should there be an explicit `solopm ticket ready <id>` that bundles commit-check + push + PR + transition?
3. **`session start` for review.** I split implementation (`session start`) from review (`review start`). Acceptable, or would you rather one `session start --review`?
4. **Backend transport.** Local HTTP, a unix socket, or the CLI reads/writes the store directly? Affects whether `serve` must be running for the CLI to work.
