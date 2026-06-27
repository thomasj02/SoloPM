# SoloPM — HTTP API Contract (MVP / Tier 0)

The backend exposes one canonical HTTP API. **Both** clients — the web app and the
`solopm` CLI — are thin clients of this API. This document is the frozen contract for
the MVP; the web frontend and CLI are built against it.

- Base URL (default): `http://127.0.0.1:8787`
- All API routes are under `/api`. The web SPA is served from `/`.
- Request/response bodies are JSON (`Content-Type: application/json`).
- Timestamps are ISO-8601 UTC strings, e.g. `2026-06-25T18:04:11Z`.

## Attribution (no auth)

SoloPM is single-user and local — there is **no authentication**. Writes are attributed
to an actor purely for the activity log:

- Header `X-SoloPM-Actor: <name>` where `<name>` ∈ `human | claude | codex`.
- If the header is absent, the actor is **`human`** (the web app never sends it).
- The CLI sets it from `--agent <name>`; absent the flag, the CLI omits it → `human`.

## Errors

Failures return a non-2xx status and this body shape:

```json
{ "error": { "code": "invalid_transition", "message": "Cannot move from done to todo." } }
```

Error codes: `not_found`, `validation`, `invalid_transition`, `forbidden_transition`,
`duplicate`, `conflict`.

## Enumerations

- **States:** `backlog`, `todo`, `in-progress`, `in-ai-review`, `in-human-review`, `done`, `cancelled`
- **Assignees:** `human`, `claude`, `codex`, `unassigned`
- **Actors:** `human`, `claude`, `codex` (activity may also show `system`)
- **Activity kinds:** `created`, `comment`, `state_change`, `assignment`, `edit`, `criteria`, `review`, `link`, `unlink`
- **Relation types:** `blocks`, `related`, `duplicate`, `parent` (see *Ticket relationships*)

### Legal state transitions

```
backlog          → todo, in-progress, cancelled
todo             → backlog, in-progress, cancelled
in-progress      → backlog, todo, in-ai-review, cancelled
in-ai-review     → in-progress, in-human-review, cancelled
in-human-review  → in-progress, done, cancelled
done             → (terminal)
cancelled        → (terminal)
```

Actor rules layered on top:
- Only `human` may transition a ticket **into `done`** (agents cannot close a ticket).
- Any actor may transition into `cancelled`.
- Moving a ticket to the state it is already in is an idempotent no-op (200, no activity logged).

---

## Resources

### Meta

`GET /api/meta` → static enums + version, for client bootstrapping.

```json
{
  "version": "0.1.0",
  "states": ["backlog","todo","in-progress","in-ai-review","in-human-review","done","cancelled"],
  "state_labels": {"backlog":"Backlog","todo":"Todo","in-progress":"In Progress","in-ai-review":"In AI Review","in-human-review":"In Human Review","done":"Done","cancelled":"Cancelled"},
  "assignees": ["human","claude","codex","unassigned"],
  "transitions": {"backlog":["todo","in-progress","cancelled"], "...": []}
}
```

### Projects

`GET /api/projects` → `{ "projects": [ <project>, ... ] }`

`POST /api/projects` body:
```json
{ "key": "SOLO", "name": "SoloPM", "repo": "/path/to/repo", "master": "main" }
```
- `key` (required, uppercased, `[A-Z][A-Z0-9]*`), `name` (required), `repo` (optional),
  `master` (optional, default `main`). Returns `201` with `<project>`.
- Duplicate key → `409 duplicate`.

`GET /api/projects/{key}` → `<project>` (404 if missing).

`GET /api/projects/{key}/status` → `{ "open_prs": <int>, "unpushed_commits": <int> }` —
live git/PR health for the board header. `open_prs` counts the project's tickets whose
recorded PR is still `open` (SoloPM is the system of record). `unpushed_commits` counts
commits on local branches not on any remote (`git log --branches --not --remotes`).
Degrades gracefully: a project with no `repo`, no git client, or an unreachable git repo
reports `unpushed_commits: 0` rather than erroring (404 only for an unknown project).

`PATCH /api/projects/{key}` body `{ "field": "<name>", "value": "<value>" }` (sets one
field) **or** a partial object `{ "review_prompt": "..." }`. Editable fields:
`name`, `repo`, `master_branch`, `branch_convention`, `default_implementer`,
`default_reviewer`, `review_prompt`. Returns the updated `<project>`.

**`<project>` shape:**
```json
{
  "key": "SOLO",
  "name": "SoloPM",
  "repo": "/path/to/repo",
  "master_branch": "main",
  "branch_convention": "{key}-{seq}-{slug}",
  "default_implementer": "claude",
  "default_reviewer": "codex",
  "review_prompt": "…",
  "ticket_count": 12,
  "created_at": "…",
  "updated_at": "…"
}
```

### Tickets

`GET /api/tickets?project=SOLO&state=todo&assignee=claude` → `{ "tickets": [ <ticket-summary>, ... ] }`
All filters optional. Ordered by project then sequence.

**`<ticket-summary>`:**
```json
{
  "id": "SOLO-42", "project": "SOLO", "title": "…", "state": "in-progress",
  "assignee": "claude", "branch": "solo-42-…", "session_active": false,
  "pr": { "number": 17, "url": "…", "state": "open" },
  "comment_count": 3,
  "blocked": false,
  "subtickets": { "done": 2, "total": 5 },
  "state_entered_at": "…", "time_in_state_seconds": 18240,
  "created_at": "…", "updated_at": "…"
}
```
- `blocked` is true when the ticket has an **open** (non-done/cancelled) blocker;
  `subtickets` rolls up its children (done / total) when it is a parent. Both are derived
  from *Ticket relationships* (below). See also `relations` on the full `<ticket>`.
- `state_entered_at` is when the ticket entered its **current** state (set at creation,
  refreshed on every move; reorder/edit/comment/assign leave it). `time_in_state_seconds`
  is the live elapsed seconds, computed per request. Both also appear on the full
  `<ticket>` from `GET /api/tickets/{id}`.

`POST /api/tickets` body:
```json
{ "project": "SOLO", "title": "…", "description": "…", "state": "backlog", "assignee": "unassigned" }
```
- `project` + `title` required; `description` default `""`; `state` default `backlog`;
  `assignee` default `unassigned`. Returns `201` with full `<ticket>`.

`GET /api/tickets/{id}` → full `<ticket>` (404 if missing). **This is what `solopm ticket show` returns.**

`PATCH /api/tickets/{id}` body `{ "title": "…", "description": "…" }` (either/both). Edit. Returns `<ticket>`.

`POST /api/tickets/{id}/comments` body `{ "body": "…" }` → returns the created `<activity>` entry, plus updated ticket is implied. `201`.

`POST /api/tickets/{id}/move` body `{ "state": "in-ai-review", "after": <hint> }` →
returns `<ticket>`. Validates the transition + actor rules. The optional `after`
position hint controls where the ticket lands in the **target** column:
- `after` **omitted** → bottom of the column;
- `after: null` → top of the column;
- `after: "<id>"` → directly below that ticket (which must already be in the target column).

The optional `branch` records the SoloPM branch on the ticket (used when an agent
self-transitions to `in-ai-review` after committing its work).

Errors: `invalid_transition`, `forbidden_transition`, `validation` (if `after` is not in
the target column), `not_found` (unknown `after`).

**GitHub PR side effects (Tier-1, agent-only).** When the backend is run with GitHub
automation enabled (`solopm serve` / `solopm mcp`) and the ticket has a SoloPM `branch`
and its project has a `repo`, transitions drive the PR via `gh`/`git`:
- → `in-ai-review`: push the branch and open (or refresh) the PR; the ticket's `pr` is
  recorded.
- → `done`: squash-merge the PR into the project's master branch (deletes the branch) and
  append a confirmation comment naming the PR, squash commit sha, base, and branch deletion.
  On a merge-queue-protected branch the PR is only *enqueued*, not merged — the ticket then
  records `pr.state` `queued` (not `merged`) with a "merge queued" note.
- → `cancelled`: close the PR (deletes the branch) and append a "closed PR #N" note.

These run **before** the state change, so a `gh`/`git` failure (`github` error) aborts
the transition. Branch-less / human-worked tickets are unaffected. `pr.state` is one of
`open`, `merged`, `queued`, or `closed`.

`POST /api/tickets/{id}/reorder` body `{ "after": <id>|null }` → returns `<ticket>`.
Repositions a ticket **within its current column** (cosmetic — no state change, no
activity logged, `updated_at` untouched). `after: null` = top; `after: "<id>"` = below
that ticket (same column). Errors: `validation` (cross-column `after`), `not_found`.

`POST /api/tickets/{id}/review` body
`{ "verdict": "pass"|"fail", "comment": <string?>, "criteria_results": <list?> }` →
returns `<ticket>`. The AI-review gate: the ticket must be in `in-ai-review`. `pass` →
`in-human-review`; `fail` → records `comment` as a review note and returns the ticket to
`in-progress` (kickback). `comment` (the review notes) is optional. The optional
`criteria_results` is a list of `{ "criterion_id", "verdict": "pass"|"fail", "note"? }`
recorded to the activity log (a `review` activity, in `meta.results`); the overall
`verdict` still gates the transition. Errors: `validation` (not in `in-ai-review`, bad
`verdict`, or a bad per-criterion verdict), `not_found`.

### Acceptance criteria

A ticket carries `acceptance_criteria` — an ordered list of `{ "id", "text", "done" }`
(its `id` is stable, e.g. `c1`). The summary carries `acceptance: { done, total }`.

- `POST /api/tickets/{id}/criteria` body `{ "text": <string> }` → `<ticket>` (appends a
  criterion). `201`.
- `PATCH /api/tickets/{id}/criteria/{cid}` body `{ "text"?, "done"? }` → `<ticket>`
  (edit text and/or tick).
- `DELETE /api/tickets/{id}/criteria/{cid}` → `<ticket>` (remove).

Errors: `validation` (blank text / nothing to update), `not_found` (unknown ticket or
criterion). Each change is recorded as a `criteria` activity.

### Ticket relationships

Tickets can reference each other (like Linear's issue relations). A link is stored once in
a **canonical direction** and the inverse is derived for the other ticket, so a link made
from either side shows correctly on both. Relation types (read `link <id> <type> <other>`):

| `type`      | canonical storage           | `<id>` is…        | shows on `<other>` as |
|-------------|-----------------------------|-------------------|-----------------------|
| `blocks`    | blocker → blocked           | the blocker       | "Blocked by"          |
| `related`   | symmetric (stable order)    | —                 | "Related"             |
| `duplicate` | duplicate → canonical       | the duplicate     | "Duplicated by"       |
| `parent`    | child → parent              | the **sub-ticket**| "Sub-tickets"         |

So `link A parent B` sets **B as A's parent** (A is the sub-ticket). Cross-project links
are allowed (ids resolve against the whole ticket space). Validation: no self-links;
identical links are deduped (idempotent); a ticket may have at most one parent; parent
cycles are rejected.

- `POST /api/tickets/{id}/links` body `{ "type": "<type>", "other": "<other-id>" }` →
  full `<ticket>` (`201`). Idempotent on an identical link. Errors: `validation`
  (self-link, unknown type, second parent, or a cycle), `not_found` (unknown ticket),
  `duplicate` (409 — only under a concurrent conflicting second-parent insert that races
  past the validation check; the one-parent DB index is the backstop).
- `DELETE /api/tickets/{id}/links/{other-id}[?type=<type>][&direction=out|in]` → full
  `<ticket>`. Removes the link(s) between the pair; `type` narrows to one relation, otherwise
  all links between the two are removed. `direction` pins the stored orientation relative to
  `{id}` (`out` = `{id}` is the `from`, `in` = it is the `to`) — needed only to disambiguate a
  pair holding *opposing* directional links (e.g. `A blocks B` and `B blocks A`), so the web's
  per-row remove deletes exactly the relation shown. Errors: `validation` (bad `direction`),
  `not_found` (no such link / unknown ticket).

Link/unlink are each recorded as a `link` / `unlink` activity **on both tickets**. The full
`<ticket>` carries a `relations` array (the derived per-perspective view, grouped+sorted):

```json
{
  "type": "blocks",            // canonical link type
  "key": "blocked_by",         // perspective group: blocks | blocked_by | related |
                               //   duplicate_of | duplicated_by | parent | sub
  "label": "Blocked by",       // human label for the group
  "direction": "in",           // "out" when this ticket is the canonical `from`, else "in"
  "ticket": { "id": "SOLO-3", "title": "…", "state": "in-progress" },
  "created_by": "claude",
  "created_at": "…"
}
```

The derived board signals — `blocked` and `subtickets` on `<ticket-summary>` — come from
these links: `blocked` ⇐ an open `blocked_by` relation; `subtickets` ⇐ the `sub` group's
done/total. Links to a `cancelled` ticket still render (tickets are never hard-deleted).

### Overlap radar

`GET /api/radar[?project=<key>]` → `{ "overlaps": [ <overlap>, … ] }`. Informational only
(it never blocks anything). Reads the project repo's live worktrees from git, computes each
one's changed files vs. the master branch (committed diff + uncommitted `status`), and
reports every pair whose file sets intersect:

```
<overlap> = {
  "project": "SOLO",
  "a": { "ticket": "SOLO-9"|null, "branch": "solo-9-foo" },
  "b": { "ticket": null,         "branch": "solo-2-bar" },
  "files": ["src/x.py", …]
}
```

A branch is annotated with the active ticket (`in-progress` / `in-ai-review`) that records
it; unmapped branches are still reported. The master worktree is excluded. Degrades to
`{ "overlaps": [] }` when the project has no repo or GitHub automation is off (no error).

### Review memory (the learning review gate)

A project carries `review_memory` — a list of `{ id, text, source, status, hits, ticket,
created_at }`. `source` ∈ `ai_fail | human_miss | manual`; `status` ∈
`candidate | active | retired`. **Candidates** are auto-captured: an AI-review `fail` with
notes (→ `ai_fail`) and a human kickback of AI-passed work (in-human-review → in-progress,
→ `human_miss`). You curate them; only **active** items feed the review prompt.

- `GET /api/projects/{key}/review-memory[?status=…]` → `{ "items": [ … ] }`.
- `POST /api/projects/{key}/review-memory` body `{ "text", "source"?, "status"? }` → the new
  item (`201`; defaults `source=manual`, `status=active`).
- `PATCH /api/projects/{key}/review-memory/{id}` body `{ "text"?, "status"? }` → the item
  (promote `candidate`→`active`, retire, or edit). Errors: `validation`, `not_found`.
- `GET /api/projects/{key}/review-prompt[?record_hit=true]` → `{ "prompt": "…" }` — the base
  `review_prompt` plus the **active** review-memory checklist, for a reviewer to fetch.
  `record_hit=true` bumps each active item's `hits`.

Ordering: within a column, tickets are ordered by an internal `position` (fractional
indexing). `GET /api/tickets` returns tickets grouped by workflow state, then by
position. `position` itself is not exposed in the payload.

`POST /api/tickets/{id}/assign` body `{ "assignee": "claude" }` → returns `<ticket>`.

**`<ticket>` (full) shape** — matches the CLI spec's JSON contract:
```json
{
  "id": "SOLO-42",
  "project": "SOLO",
  "seq": 42,
  "title": "Add session attach command",
  "description": "…markdown…",
  "state": "in-progress",
  "assignee": "claude",
  "branch": "solo-42-add-session-attach",
  "pr": { "number": 17, "url": "https://github.com/…/pull/17", "state": "open" },
  "session": { "id": "claude-9f2a…", "active": true },
  "relations": [
    { "type": "blocks", "key": "blocks", "label": "Blocks", "direction": "out",
      "ticket": { "id": "SOLO-43", "title": "…", "state": "todo" },
      "created_by": "claude", "created_at": "…" }
  ],
  "comments": [
    { "author": "claude", "body": "Pushed first pass.", "at": "2026-05-31T18:04:11Z" }
  ],
  "activity": [
    { "id": 1, "actor": "human", "kind": "created", "body": "created ticket", "meta": {}, "at": "…" },
    { "id": 2, "actor": "claude", "kind": "comment", "body": "Pushed first pass.", "meta": {}, "at": "…" }
  ],
  "state_entered_at": "2026-05-31T18:02:00Z",
  "time_in_state_seconds": 18240,
  "created_at": "2026-05-31T17:50:02Z",
  "updated_at": "2026-05-31T18:04:11Z"
}
```
- `pr` is `null` until a PR exists (Tier 1). `session` is `null` until a session exists (Tier 1).
- `comments` is the subset of `activity` with `kind == "comment"`, projected to `{author, body, at}`.
- `activity` is the full chronological log (oldest first).
