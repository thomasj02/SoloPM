"""The canonical SoloPM operations.

This is the single source of business logic. The HTTP server and (through it) the web
app and CLI are all clients of these operations — keeping the two interfaces honestly at
parity, as the product brief requires.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path

from . import workflow
from .errors import DuplicateError, ForbiddenTransitionError, NotFoundError, ValidationError
from .github import (
    GitHubClient,
    GitHubError,
    normalize_remote_url,
    validate_branch_name,
    validate_github_repo,
)
from .models import (
    ASSIGNEES,
    ACTORS,
    DEFAULT_BRANCH_CONVENTION,
    DEFAULT_REVIEW_PROMPT,
    LINK_TYPES,
    RELATION_GROUP_ORDER,
    STATES,
    TAGS_MAX_COUNT,
    Activity,
    Link,
    Project,
    Relation,
    Ticket,
    normalize_project_key,
    normalize_tag,
    normalize_tags,
    normalize_ticket_id,
    relation_view,
)
from .store import Store

# Fields editable via ``set_project_field`` / project PATCH.
_PROJECT_SETTABLE = frozenset(
    {
        "name",
        "repo",
        "github_repo",
        "master_branch",
        "branch_convention",
        "default_implementer",
        "default_reviewer",
        "review_prompt",
    }
)


# Sentinel distinguishing "caller gave no position hint" (→ bottom of column) from an
# explicit ``after=None`` (→ top of column).
_UNSET = object()


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _pr_url_matches_slug(url: str | None, slug: str) -> bool:
    """Whether a recorded PR URL belongs to the GitHub repo ``slug``.

    PR NUMBERS are only meaningful within one repository — after a project's
    ``github_repo`` is re-pointed, a recorded number could name an unrelated PR in the
    new repo, so acting on it must be gated on the URL's owner/name matching the slug.
    An absent or unparseable URL fails the check: identity that can't be confirmed is
    not identity."""
    if not url:
        return False
    m = re.search(r"://[^/]+/([^/]+/[^/]+)/pull/\d+", url.strip(), re.IGNORECASE)
    return bool(m) and m.group(1).lower() == slug.lower()


def _canonical_path(path: str) -> str:
    """One comparable form per repo path — trailing slashes, `~`, and symlinks must not
    let two project rows point at the same repository unnoticed."""
    try:
        return str(Path(path).expanduser().resolve())
    except (OSError, RuntimeError, ValueError):
        # expanduser raises RuntimeError for an unresolvable ~user, resolve can too on
        # symlink loops, and an embedded NUL raises ValueError — canonicalization
        # degrades to the raw path, never errors.
        return path


def _require_actor(actor: str) -> str:
    if actor not in ACTORS:
        raise ValidationError(
            f"Unknown actor {actor!r}: expected one of {', '.join(ACTORS)}."
        )
    return actor


class _RepoOps:
    """One PR-lifecycle call surface, bound to where the project's repo actually is
    (SOLO-29): cwd-based git/gh for a local checkout, ``gh --repo <slug>`` (API mode)
    for a remote project — ``github_repo`` set — whose checkout lives on another
    machine and must never be touched (or assumed) from the backend."""

    def __init__(self, github: GitHubClient, project: Project):
        self._gh = github
        self.slug = project.github_repo
        self.repo = project.repo
        self.remote = bool(self.slug)

    def ensure_branch_on_origin(self, branch: str) -> None:
        """Local: push the branch (the backend's checkout has the commits). Remote: the
        SoloPM client already pushed from the dev machine — verify, never trust."""
        if not self.remote:
            self._gh.push_branch(self.repo, branch)
            return
        validate_branch_name(branch)
        if not self._gh.api_branch_exists(self.slug, branch):
            raise GitHubError(
                f"Branch {branch!r} is not on origin ({self.slug}). This project's "
                "checkout lives on another machine, so SoloPM's client half (the HTTP "
                "MCP or CLI) must push the branch from there before the move — run the "
                "move through the SoloPM client on that machine, or push the branch "
                "and retry."
            )

    def find_pr(self, branch: str):
        if self.remote:
            return self._gh.api_find_pr(self.slug, branch)
        return self._gh.find_pr(self.repo, branch)

    def list_open_prs(self):
        if self.remote:
            return self._gh.api_list_open_prs(self.slug)
        return self._gh.list_open_prs(self.repo)

    def open_or_refresh_pr(self, branch: str, base: str, title: str, body: str):
        if self.remote:
            return self._gh.api_open_or_refresh_pr(self.slug, branch, base, title, body)
        return self._gh.open_or_refresh_pr(self.repo, branch, base, title, body)

    def pr_head(self, number: int):
        if self.remote:
            return self._gh.api_pr_head(self.slug, number)
        return self._gh.pr_head(self.repo, number)

    def merge_pr(self, number: int, branch: str | None):
        if self.remote:
            return self._gh.api_merge_pr(self.slug, number, branch)
        return self._gh.merge_pr(self.repo, number, branch)

    def close_pr(self, number: int, branch: str | None):
        if self.remote:
            return self._gh.api_close_pr(self.slug, number, branch)
        return self._gh.close_pr(self.repo, number, branch)


class Service:
    def __init__(self, store: Store, github: GitHubClient | None = None):
        self.store = store
        # Optional GitHub automation (Tier-1). When set, agent-managed tickets (those
        # with a SoloPM branch) drive PRs on transition; absent it, moves are pure.
        self.github = github

    @classmethod
    def open(cls, db_path) -> "Service":
        store = Store(db_path)
        return cls(store)

    # --- projects -----------------------------------------------------------

    def add_project(
        self,
        *,
        key: str,
        name: str,
        repo: str | None = None,
        github_repo: str | None = None,
        master: str = "main",
        branch_convention: str = DEFAULT_BRANCH_CONVENTION,
        default_implementer: str = "claude",
        default_reviewer: str = "codex",
        review_prompt: str = DEFAULT_REVIEW_PROMPT,
    ) -> Project:
        key = normalize_project_key(key)
        if not name or not name.strip():
            raise ValidationError("Project name is required.")
        # The duplicate-key check runs BEFORE the repo claim so any create against an
        # existing key is the documented 409 — even when the request also names a repo
        # owned by a different project. insert_project's atomic check stays the backstop.
        try:
            self.get_project(key)
        except NotFoundError:
            pass
        else:
            raise DuplicateError(f"Project {key!r} already exists.")
        github_repo = self._prepare_github_repo(github_repo, exclude_key=key)
        if not github_repo:
            # Local project: claim the checkout path. A remote project claims its slug
            # instead — its path names a directory on another machine (SOLO-29).
            self._require_repo_unclaimed(repo, exclude_key=key)
        now = _now()
        project = Project(
            key=key,
            name=name.strip(),
            repo=repo,
            github_repo=github_repo,
            master_branch=master or "main",
            branch_convention=branch_convention or DEFAULT_BRANCH_CONVENTION,
            default_implementer=default_implementer,
            default_reviewer=default_reviewer,
            review_prompt=review_prompt,
            seq_counter=0,
            created_at=now,
            updated_at=now,
        )
        self.store.insert_project(project)  # raises DuplicateError on conflict
        return self.get_project(key)

    def list_projects(self) -> list[Project]:
        return self.store.list_projects()

    def get_project(self, key: str) -> Project:
        project = self.store.get_project(normalize_project_key(key))
        if project is None:
            raise NotFoundError(f"Project {key!r} not found.")
        return project

    def update_project(self, key: str, fields: dict) -> Project:
        key = normalize_project_key(key)
        current = self.get_project(key)  # existence check
        unknown = set(fields) - _PROJECT_SETTABLE
        if unknown:
            raise ValidationError(
                f"Cannot set field(s) {', '.join(sorted(unknown))}. "
                f"Editable: {', '.join(sorted(_PROJECT_SETTABLE))}."
            )
        if "name" in fields and not str(fields["name"]).strip():
            raise ValidationError("Project name cannot be blank.")
        if "github_repo" in fields:
            fields = {
                **fields,
                "github_repo": self._prepare_github_repo(fields["github_repo"], exclude_key=key),
            }
        # Both identity checks reason about the project's EFFECTIVE post-update state:
        # a multi-field patch may change repo and github_repo together, and validating
        # the OLD repo would let its matching origin vouch for an unrelated new one.
        effective_slug = fields["github_repo"] if "github_repo" in fields else current.github_repo
        effective_repo = fields["repo"] if "repo" in fields else current.repo
        if "github_repo" in fields:
            self._require_no_stale_repo_identities(current, effective_slug, effective_repo)
        # Path claims: a remote project's path is not a local identity, but a project
        # going (or staying) local must hold the claim — including when clearing
        # github_repo re-localizes a path some other local project has since taken.
        if not effective_slug and ("repo" in fields or "github_repo" in fields):
            self._require_repo_unclaimed(effective_repo, exclude_key=key)
        self.store.update_project(key, fields, _now())
        return self.get_project(key)

    def _require_no_stale_repo_identities(
        self, project: Project, new_slug: str | None, effective_repo: str | None
    ) -> None:
        """Refuse re-pointing a project's GitHub identity while live tickets still
        carry branch/PR records tied to the OLD identity (SOLO-29). PR numbers and
        branch names are only meaningful within one repository — after a re-point, a
        later done/cancel would act on same-named/numbered strangers in the new repo.
        Gating the TRANSITION closes the whole class; the action-time pr_url check
        stays only as the legacy-store backstop.

        Ungated: a case-only rename (same repo), and a LOCAL↔REMOTE conversion whose
        checkout origin verifiably IS the slug side of the transition — the routine
        migration paths, where the recorded identities agree by construction. A
        remote→remote re-point gets no such escape hatch: the project's checkout lives
        on another machine, so nothing local can vouch for it."""
        old_slug = project.github_repo
        if (old_slug or "").lower() == (new_slug or "").lower():
            return  # no identity change (covers case-only renames and no-op clears)
        if bool(old_slug) != bool(new_slug) and effective_repo:
            # local→remote: the checkout must be the NEW slug's repo; remote→local
            # (clearing): the checkout must be the OLD slug's repo. Always the
            # EFFECTIVE (post-update) repo — the same patch may re-point it.
            anchor = (new_slug or old_slug or "").lower()
            origin = self._normalized_remote(effective_repo)
            if origin is not None:
                parts = origin.split("/")
                if len(parts) >= 2 and "/".join(parts[-2:]) == anchor:
                    return  # converting in place: the checkout IS that repo
        live = sorted(
            t.id
            for t in self.store.list_tickets(project=project.key)
            if t.state not in self._CLOSED_STATES and (t.branch or t.pr_number is not None)
        )
        if live:
            raise ValidationError(
                f"Cannot point {project.key} at {new_slug!r}: ticket(s) "
                f"{', '.join(live)} still carry branch/PR records from the project's "
                "previous repository identity — a PR number or branch name is only "
                "meaningful within one repo. Resolve those tickets (or clear their "
                "records) first."
            )

    def _prepare_github_repo(self, value, *, exclude_key: str) -> str | None:
        """Validate + claim a ``github_repo`` slug before it is stored (SOLO-29).

        A falsy value clears the field (back to local mode). Otherwise the slug must be
        a well-formed ``owner/name``, unclaimed by any other project (same 1:1 rule as
        repo paths — case-insensitive, since GitHub slugs are), and — when a GitHub
        client is configured — visible to this machine's ``gh``, so an access problem
        surfaces at config time with a clear fix instead of on the first move."""
        if not value:
            return None
        slug = validate_github_repo(str(value).strip())
        lowered = slug.lower()
        for p in self.list_projects():
            if p.key != exclude_key and p.github_repo and p.github_repo.lower() == lowered:
                raise ValidationError(
                    f"GitHub repo {slug!r} is already mapped to project {p.key} — "
                    "project ↔ repo is 1:1."
                )
        if self.github is not None:
            try:
                self.github.api_check_repo(slug)
            except GitHubError as exc:
                raise ValidationError(
                    f"The backend's gh cannot access {slug!r} ({exc}) — grant its GitHub "
                    "account access to the repo (or fix gh auth / the slug) and retry."
                ) from exc
        return slug

    def _normalized_remote(self, repo: str) -> str | None:
        """The repo's origin identity as ``host/path`` (see
        :func:`~solopm.core.github.normalize_remote_url`), or ``None``."""
        if self.github is None:
            return None
        return normalize_remote_url(self.github.remote_url(repo))

    def _repo_identities(self, project: Project) -> set[str]:
        """Comparable identities for a project's repository (the shared-repo guard).

        A remote project (SOLO-29) is identified by its GitHub slug; a local one by its
        canonical checkout path plus — best-effort — its normalized origin URL AND that
        origin's host-independent ``owner/name``: SSH host aliases (git@github-work:…)
        are how multi-account setups routinely address one GitHub repo, so the slug
        comparison must not depend on the transport host. Over-matching (two different
        hosts with the same owner/name) errs toward the ambiguity DECLINE, the safe
        direction. A remote project's ``repo`` PATH is deliberately NOT an identity: it
        names a directory on another machine, where an equal string need not be the
        same repository."""
        if project.github_repo:
            return {f"slug:{project.github_repo.lower()}"}
        idents: set[str] = set()
        if project.repo:
            idents.add(f"path:{_canonical_path(project.repo)}")
            remote = self._normalized_remote(project.repo)
            if remote is not None:
                idents.add(f"remote:{remote}")
                parts = remote.split("/")
                if len(parts) >= 3:  # host + at least owner/name
                    idents.add(f"slug:{'/'.join(parts[-2:])}")
        return idents

    def _require_repo_unclaimed(self, repo: str | None, *, exclude_key: str | None = None) -> None:
        """Enforce the documented project ↔ repo 1:1 mapping. Every repo-scoped feature
        (PR ownership/discovery, prune, radar, status) reasons per-project and would
        silently cross wires if two projects shared a repository.

        Path claims hold between LOCAL projects only, matching ``_repo_identities``:
        a remote project's ``repo`` names a directory on another machine, where an
        equal path string need not be the same repository — remote projects claim
        their GitHub slug instead (``_prepare_github_repo``)."""
        if not repo:
            return
        target = _canonical_path(repo)
        for p in self.list_projects():
            if exclude_key is not None and p.key == exclude_key:
                continue
            if p.github_repo:
                continue  # remote project: its path is not a local identity (SOLO-29)
            if p.repo and _canonical_path(p.repo) == target:
                raise ValidationError(
                    f"Repo {repo!r} is already mapped to project {p.key} — "
                    "project ↔ repo is 1:1."
                )

    def set_project_field(self, key: str, field: str, value) -> Project:
        return self.update_project(key, {field: value})

    def delete_project(self, key: str, *, force: bool = False) -> dict:
        """Delete a project; with ``force``, everything filed under it goes too.

        Refuses to delete a project that still has tickets unless ``force`` is set — a
        guard against erasing a whole board by accident. With ``force``, the project and
        all its tickets, their activity, and their relationship links (including
        cross-project links to/from those tickets) are cascade-deleted.

        The existence check, the non-empty guard, and the delete are performed atomically
        by :meth:`Store.delete_project` (one transaction), so a ticket created concurrently
        between the check and the delete can't slip past the guard. ``tickets_deleted`` is
        the count actually removed, read inside that same transaction.

        Returns ``{"key", "deleted": True, "tickets_deleted"}``. Raises ``NotFoundError``
        for an unknown project and ``ValidationError`` for a non-empty project without
        ``force``.
        """
        key = normalize_project_key(key)
        tickets_deleted = self.store.delete_project(key, force=force)
        return {"key": key, "deleted": True, "tickets_deleted": tickets_deleted}

    def project_status(self, key: str) -> dict:
        """Live git/PR health for a project: ``{open_prs, unpushed_commits}``.

        *Open PRs* counts this project's tickets whose recorded PR is still ``open`` —
        SoloPM is the system of record for the PRs it drives, so this needs no network call
        and reflects exactly what's in flight on the board. *Unpushed commits* is a git
        query (commits on local branches not on any remote) against the project repo.

        Degrades gracefully: a project with no ``repo``, no GitHub client, or an
        unreachable/odd git repo reports ``unpushed_commits = 0`` rather than erroring, so
        the board's status strip never turns a transient git hiccup into a 500.
        """
        project = self.get_project(key)  # 404 (not 500) for an unknown project
        open_prs = sum(
            1
            for t in self.store.list_tickets(project=project.key)
            if t.pr_state == "open"
        )
        unpushed = 0
        # A remote project's checkout (SOLO-29) is on another machine — there is no
        # local repo to count against, so its unpushed signal honestly stays 0.
        if self.github is not None and project.repo and not project.github_repo:
            try:
                unpushed = self.github.count_unpushed_commits(project.repo)
            except GitHubError:
                # A non-git path, missing git, or a timeout must not fail the status read.
                unpushed = 0
        return {"open_prs": open_prs, "unpushed_commits": unpushed}

    def prune_merged_branches(self, key: str, *, apply: bool = False) -> dict:
        """Clean up local branches whose work is *verifiably* merged (SOLO-23).

        A branch is force-deleted only when its merge is verified — either **reachable-merged**
        into the project's master (git-proven), or recorded on a **done** ticket whose **PR
        merged** *and* whose merged head OID still equals the branch's local tip (so a branch
        advanced or reused after the merge is never deleted; ``queued`` PRs, which haven't
        landed, don't qualify). A **gone upstream** is reported as context but is NOT sufficient
        on its own (a remote can be deleted for unmerged work), so an unverified branch is
        surfaced in ``skipped`` rather than deleted — this keeps ``git branch -D`` from
        orphaning committed-but-unmerged commits. The **current** branch and **master** are
        always protected.

        Dry-run by default — returns what *would* be pruned. With ``apply``, deletes them: a
        branch held by a *clean* worktree has the worktree removed first (``git worktree
        remove``), then the branch (``git branch -D``); a worktree with **uncommitted changes**
        is skipped and reported so no work is discarded. Per-branch git failures are caught and
        reported in ``skipped`` rather than aborting the whole prune.

        Degrades gracefully like radar/status (no repo, no git client, or a git error → an empty
        result, never a 500). Returns
        ``{project, applied, pruned:[{branch, reasons, worktree}], skipped:[{branch, reason}]}``.
        """
        project = self.get_project(key)
        result: dict = {"project": project.key, "applied": apply, "pruned": [], "skipped": []}
        if project.github_repo:
            # Remote project (SOLO-29): its checkout — and every local branch prune
            # would act on — lives on the dev machine, out of the backend's reach.
            # Decline honestly instead of reporting a clean-looking empty prune.
            result["note"] = (
                "remote project (github_repo set): its checkout and local branches live "
                "on the dev machine — SoloPM cannot prune them from the backend; delete "
                "merged branches there."
            )
            return result
        if self.github is None or not project.repo:
            return result
        repo, master = project.repo, project.master_branch
        tickets = self.store.list_tickets(project=project.key)
        # Branches backing an ACTIVE (non-terminal) ticket are in use and protected like the
        # current branch — e.g. a freshly-created in-progress branch still equal to master has
        # no commits yet, so `git branch --merged` would otherwise flag it as a prune candidate.
        active_branches = {
            t.branch for t in tickets if t.branch and t.state not in self._CLOSED_STATES
        }
        # Branches recorded on a done ticket whose PR actually MERGED (not just `queued`, which
        # hasn't landed) → the PR number, so we can confirm the branch's tip still matches the
        # merged PR head before force-deleting. Plain `state == done` isn't enough.
        done_merged_prs = {
            t.branch: t.pr_number
            for t in tickets
            if t.branch and t.state == "done" and t.pr_state == "merged" and t.pr_number
        }
        try:
            branches = self.github.local_branches(repo, master)
            worktrees = {
                wt.branch: wt.path for wt in self.github.list_worktrees(repo) if wt.branch
            }
        except GitHubError:
            return result

        for b in branches:
            if b.is_current or b.name == master or b.name in active_branches:
                continue  # never delete the checked-out branch, master, or active-ticket work
            reasons: list[str] = []
            on_done_merged = b.name in done_merged_prs
            if on_done_merged:
                reasons.append("done")
            if b.upstream_gone:
                reasons.append("gone-upstream")
            if b.merged:
                reasons.append("merged")
            if not reasons:
                continue  # no merge signal at all — leave it alone

            # Force-delete (`git branch -D`) only when the merge is VERIFIED: reachable into
            # master (git-proven), or on a done ticket whose PR merged AND whose merged head OID
            # still equals this branch's tip (so a branch advanced/reused after the merge isn't
            # deleted). A gone upstream alone is never sufficient — the remote could have been
            # deleted for *unmerged* work. Anything not verified is surfaced but never deleted,
            # so committed-but-unmerged commits are never orphaned.
            verified = b.merged
            if not verified and on_done_merged:
                verified = self._branch_tip_matches_pr(repo, b.name, done_merged_prs[b.name])
            if not verified:
                result["skipped"].append(
                    {
                        "branch": b.name,
                        "reason": f"not verified merged ({', '.join(reasons)}) — delete manually if sure",
                    }
                )
                continue

            wt_path = worktrees.get(b.name)
            if wt_path:
                try:
                    dirty = self.github.worktree_is_dirty(wt_path)
                except GitHubError:
                    dirty = True  # unverifiable → don't risk removing it
                if dirty:
                    result["skipped"].append(
                        {"branch": b.name, "reason": f"worktree has uncommitted changes ({wt_path})"}
                    )
                    continue
                if apply:
                    try:
                        self.github.remove_worktree(repo, wt_path)
                    except GitHubError as exc:
                        result["skipped"].append(
                            {"branch": b.name, "reason": f"could not remove worktree: {exc}"}
                        )
                        continue
            if apply:
                try:
                    self.github.delete_local_branch(repo, b.name)
                except GitHubError as exc:
                    result["skipped"].append(
                        {"branch": b.name, "reason": f"delete failed: {exc}"}
                    )
                    continue
            result["pruned"].append(
                {"branch": b.name, "reasons": reasons, "worktree": wt_path}
            )
        return result

    def _branch_tip_matches_pr(self, repo: str, branch: str, pr_number: int) -> bool:
        """True when the PR is **actually merged on GitHub** and ``branch``'s local tip still
        equals that merged head OID.

        Confirming the live merge (not just the stored ticket ``pr_state``) avoids deleting a
        branch for an unlanded PR, and the tip comparison guards against a branch advanced or
        reused after the merge (new commits move the tip past the merged head). Any lookup
        failure or a non-merged live state returns ``False`` — we never treat an unverifiable
        branch as safe.
        """
        try:
            head = self.github.pr_merged_head(repo, pr_number)
            tip = self.github.branch_tip(repo, branch)
        except GitHubError:
            return False
        return bool(head and tip and head == tip)

    # --- tickets ------------------------------------------------------------

    def create_ticket(
        self,
        *,
        project: str,
        title: str,
        description: str = "",
        state: str = "backlog",
        assignee: str = "unassigned",
        actor: str = "human",
    ) -> Ticket:
        _require_actor(actor)
        key = normalize_project_key(project)
        self.get_project(key)  # raises NotFoundError if missing
        if not title or not title.strip():
            raise ValidationError("Ticket title is required.")
        if state not in STATES:
            raise ValidationError(f"Unknown state {state!r}.")
        if assignee not in ASSIGNEES:
            raise ValidationError(
                f"Unknown assignee {assignee!r}: expected one of {', '.join(ASSIGNEES)}."
            )
        # The "only the human reaches done" invariant also covers creation: an agent
        # cannot mint a ticket that is already closed.
        if state in workflow.HUMAN_ONLY_TARGETS and actor != "human":
            raise ForbiddenTransitionError(
                f"Only the human may create a ticket directly in {state}."
            )

        ticket = self.store.create_ticket(
            project_key=key,
            title=title.strip(),
            description=description or "",
            state=state,
            assignee=assignee,
            actor=actor,
            created_at=_now(),
        )
        return self.get_ticket(ticket.id)

    def list_tickets(
        self,
        *,
        project: str | None = None,
        state: str | None = None,
        assignee: str | None = None,
        tags: list[str] | None = None,
    ) -> list[Ticket]:
        if state is not None and state not in STATES:
            raise ValidationError(f"Unknown state {state!r}.")
        if assignee is not None and assignee not in ASSIGNEES:
            raise ValidationError(f"Unknown assignee {assignee!r}.")
        if project is not None:
            project = normalize_project_key(project)
        tickets = self.store.list_tickets(project=project, state=state, assignee=assignee)
        # Group by workflow state, then by manual position within each column.
        rank = {s: i for i, s in enumerate(STATES)}
        tickets.sort(key=lambda t: (rank.get(t.state, 99), t.position, t.seq))
        # Tag filter (SOLO-21): keep tickets carrying ALL requested tags (AND). The match is
        # case-insensitive and lenient — blank entries are ignored and an odd filter value
        # simply matches nothing rather than erroring (a filter, not a mutation).
        if tags:
            wanted = {t.strip().lower() for t in tags if t and t.strip()}
            if wanted:
                tickets = [t for t in tickets if wanted <= set(t.tags)]
        self._attach_relations(tickets)
        return tickets

    def _require_ticket(self, ticket_id: str) -> Ticket:
        """Fetch a ticket (no relations attached) or raise ``NotFoundError``.

        Used for existence checks where the full relation/derived-field assembly of
        :meth:`get_ticket` would be wasted work.
        """
        ticket = self.store.get_ticket(ticket_id)
        if ticket is None:
            raise NotFoundError(f"Ticket {ticket_id!r} not found.")
        return ticket

    def get_ticket(self, ticket_id: str) -> Ticket:
        ticket = self._require_ticket(ticket_id)
        self._attach_relations([ticket])
        return ticket

    def edit_ticket(
        self,
        ticket_id: str,
        *,
        title: str | None = None,
        description: str | None = None,
        actor: str = "human",
    ) -> Ticket:
        _require_actor(actor)
        self.get_ticket(ticket_id)  # existence check
        fields: dict = {}
        changed: list[str] = []
        if title is not None:
            if not title.strip():
                raise ValidationError("Ticket title cannot be blank.")
            fields["title"] = title.strip()
            changed.append("title")
        if description is not None:
            fields["description"] = description
            changed.append("description")
        if fields:
            self.store.change_ticket(
                ticket_id,
                fields,
                actor=actor,
                kind="edit",
                body="edited " + " and ".join(changed),
                meta={"fields": changed},
                when=_now(),
            )
        return self.get_ticket(ticket_id)

    def comment_ticket(self, ticket_id: str, *, body: str, actor: str = "human") -> Activity:
        _require_actor(actor)
        self.get_ticket(ticket_id)  # existence check
        if not body or not body.strip():
            raise ValidationError("Comment body is required.")
        # No column changes — change_ticket just bumps updated_at and logs the comment.
        return self.store.change_ticket(
            ticket_id,
            {},
            actor=actor,
            kind="comment",
            body=body.strip(),
            meta={},
            when=_now(),
        )

    def _position_in_column(self, project: str, state: str, after, *, exclude_id=None) -> float:
        """A position value placing a ticket within (``project``, ``state``).

        ``after`` is tri-state:
          * :data:`_UNSET` → bottom of the column;
          * ``None``       → top of the column;
          * a ticket id    → directly below it (midpoint to the next card — fractional
                             indexing, so siblings aren't renumbered).

        ``exclude_id`` drops a ticket from the neighbour calculation (used when
        reordering a ticket already in the column).
        """
        column = [
            t for t in self.store.list_tickets(project=project, state=state)
            if t.id != exclude_id
        ]
        column.sort(key=lambda t: (t.position, t.seq))

        if after is _UNSET:
            return (column[-1].position + 1.0) if column else 1.0
        if after is None:
            return (column[0].position - 1.0) if column else 1.0

        target = self.store.get_ticket(after)
        if target is None:
            raise NotFoundError(f"Ticket {after!r} not found.")
        if target.project != project or target.state != state:
            raise ValidationError(
                f"Cannot position after {after!r}: it is not in the same column."
            )
        idx = next(i for i, t in enumerate(column) if t.id == after)
        nxt = column[idx + 1] if idx + 1 < len(column) else None
        return (target.position + nxt.position) / 2 if nxt else target.position + 1.0

    def move_ticket(
        self,
        ticket_id: str,
        state: str,
        *,
        after=_UNSET,
        branch: str | None = None,
        actor: str = "human",
    ) -> Ticket:
        """Transition a ticket and place it in the target column.

        ``after`` is the position hint (see :meth:`_position_in_column`): omit it to land
        at the bottom, pass ``None`` for the top, or a ticket id to drop directly below
        that card. A move to the ticket's current state is a no-op — except with an
        explicit ``after``, which delegates to :meth:`reorder_ticket` instead of being
        silently dropped. ``branch`` records the SoloPM branch when an agent
        self-transitions to ``in-ai-review``. State-transition and actor rules are
        enforced regardless.

        If GitHub automation is configured and the ticket has a SoloPM branch, the
        transition drives the PR: → in-ai-review pushes + opens/refreshes the PR;
        → done squash-merges it; → cancelled closes it. The GitHub side effects run
        **before** the state change, so a failure aborts the move.
        """
        _require_actor(actor)
        ticket = self.get_ticket(ticket_id)
        if workflow.is_noop(ticket.state, state):
            if after is _UNSET:
                return ticket
            # A same-state move with an explicit placement hint is a reorder —
            # returning early would silently drop (and never validate) the hint.
            return self.reorder_ticket(ticket_id, after=after, actor=actor)
        workflow.validate_transition(ticket.state, state, actor=actor)
        if branch:
            validate_branch_name(branch)
            # Once a PR has been opened, its branch is the PR head and is pinned: a differing
            # branch on any later move is rejected. Otherwise the recorded branch could drift
            # from the PR head, and a later done/cancelled would clean up `refs/heads/<that
            # branch>` — an unrelated branch — while leaving the real PR head behind. (See
            # SOLO-16: branch deletion is split out of `gh pr merge` and resolved from the
            # recorded head, so that head must not drift.) Before a PR exists the branch is
            # still free to be set/changed (e.g. an agent recording its worktree branch on
            # → in-progress for the overlap radar).
            if ticket.pr_number is not None and branch != ticket.branch:
                raise ValidationError(
                    "This ticket's branch is pinned to its open PR's head and cannot be "
                    f"changed (PR #{ticket.pr_number} on branch {ticket.branch!r})."
                )
        # All local validation (transition, branch, position/after) runs BEFORE any
        # external GitHub side effect, so a bad request can never push/merge/close a PR
        # and then fail the move.
        new_pos = self._position_in_column(ticket.project, state, after, exclude_id=ticket_id)

        pr_fields, pr_note = self._git_side_effects(ticket, state, branch or ticket.branch, actor)

        # The move's timestamp doubles as the new state-entry time (SOLO-13), so the
        # state-change activity and state_entered_at can't drift apart.
        when = _now()
        fields: dict = {"state": state, "position": new_pos, "state_entered_at": when}
        if branch:
            fields["branch"] = branch
        fields.update(pr_fields)
        self.store.change_ticket(
            ticket_id,
            fields,
            actor=actor,
            kind="state_change",
            body=f"moved {ticket.state} → {state}",
            meta={"from": ticket.state, "to": state},
            when=when,
        )
        # Record the merge/close confirmation as a comment AFTER the state change, so the
        # activity log reads "moved … → done" then the merge note. Attributed to the actor
        # who performed the transition (done is human-only; cancelled may be an agent).
        if pr_note:
            self.store.change_ticket(
                ticket_id, {}, actor=actor, kind="comment", body=pr_note, meta={}, when=_now()
            )
        # Learning gate: a human kicking back AI-passed work means the reviewer missed
        # something — capture a high-priority candidate to curate into the review memory.
        if ticket.state == "in-human-review" and state == "in-progress":
            self._capture_review_memory(
                ticket.project,
                f"AI review passed but the human requested changes on {ticket_id} — "
                "capture the standard the reviewer missed.",
                "human_miss",
                ticket_id,
            )
        return self.get_ticket(ticket_id)

    def _git_side_effects(
        self, ticket: Ticket, to_state: str, branch: str | None, actor: str
    ) -> tuple[dict, str | None]:
        """Run GitHub PR side effects for a transition.

        Returns ``(fields, note)``: ``fields`` are ticket columns to persist with the
        state change; ``note`` is an optional confirmation comment to append afterwards
        (the merge/close record), or ``None``.

        A no-op unless GitHub automation is configured and the project has a repo
        (a local checkout path, or a `github_repo` slug for a remote project — the
        latter runs the whole lifecycle through the GitHub API, never a local cwd).
        → in-ai-review additionally needs a SoloPM branch; → done/cancelled works from
        the recorded PR/branch, falling back to convention-based discovery (SOLO-27).
        Raises on a git/gh failure so the caller aborts the transition — except while
        *discovering* an unrecorded PR, which is best-effort and only produces a note.
        """
        if self.github is None:
            return {}, None
        project = self.get_project(ticket.project)
        if not project.repo and not project.github_repo:
            return {}, None
        ops = _RepoOps(self.github, project)
        base = project.master_branch
        if to_state == "in-ai-review":
            # Git automation is agent-only: a human reaching in-ai-review (or supplying a
            # branch) must not push or open a PR. Without a branch there is nothing to push.
            if actor == "human" or not branch:
                return {}, None
            ops.ensure_branch_on_origin(branch)
            pr = ops.open_or_refresh_pr(
                branch, base, f"{ticket.id}: {ticket.title}", ticket.description or ""
            )
            return {"pr_number": pr.number, "pr_url": pr.url, "pr_state": pr.state}, None
        if to_state in ("done", "cancelled"):
            # Merge/close the recorded PR; if none was recorded, resolve it by branch
            # (SoloPM owns the branch, so any PR on it is this ticket's). With no branch
            # recorded at all, fall back to convention-based discovery (SOLO-27). Either
            # way, when nothing ends up merged/closed the caller gets a note — a repo
            # project with automation on must never skip silently (AW-62).
            #
            # Exception: cancelling a ticket straight out of planning (backlog/todo)
            # with nothing recorded is routine triage of ideas that never had work —
            # probing GitHub there could close a coincidentally-named in-flight PR,
            # and a note per cancel is pure noise. (done can't arrive from planning.)
            if ticket.state in ("backlog", "todo") and ticket.pr_number is None and not branch:
                return {}, None
            extra: dict = {}
            number = ticket.pr_number
            url = ticket.pr_url
            # A recorded PR NUMBER is only meaningful within one repository. If the
            # project's github_repo was re-pointed after this PR was recorded, the same
            # number in the NEW repo is an unrelated PR — merging/closing it would act
            # on someone else's work. Branch-lookup and discovery below are immune
            # (they query the current slug), so only the recorded-number path is gated.
            if number is not None and ops.remote and not _pr_url_matches_slug(url, ops.slug):
                return {}, self._nothing_note(
                    to_state,
                    f"recorded PR #{number} ({url or 'no URL recorded'}) does not belong "
                    f"to this project's github_repo ({ops.slug}) — the slug changed "
                    "after the PR was recorded; resolve manually",
                )
            if number is None and branch:
                found = ops.find_pr(branch)
                if found is None:
                    return {}, self._nothing_note(
                        to_state, f"branch `{branch}` is recorded but has no PR"
                    )
                number, url = found.number, found.url
                extra = {"pr_number": found.number, "pr_url": found.url}
            elif number is None:
                # The implementer drove `gh` by hand and never recorded a branch: an open
                # PR whose head names this ticket in the DEFAULT `<ID>[-slug]` shape is
                # this ticket's. Discovery supports only the default branch convention —
                # under a custom one, sibling heads can land on this ticket's default
                # shape (e.g. {key}-{seq:x}-{slug} makes ticket 16 branch as SOLO-10-*),
                # so not even the default matcher can be trusted; modelling arbitrary
                # format templates safely is an open-ended problem, declined instead.
                # Failing to LOOK must not block the (human-driven) move — note it.
                if project.branch_convention != DEFAULT_BRANCH_CONVENTION:
                    return {}, self._nothing_note(
                        to_state,
                        "automatic PR discovery supports only the default branch "
                        f"convention — this project uses "
                        f"{project.branch_convention!r}, resolve manually",
                    )
                try:
                    open_prs = ops.list_open_prs()
                except GitHubError as exc:
                    return {}, self._nothing_note(to_state, f"PR discovery failed ({exc})")
                # Only same-repo PRs targeting the project's master are candidates: a
                # different base would merge into THAT branch while the note claims
                # master, and a fork PR's bare head name is a ref in the fork — branch
                # cleanup could delete an unrelated same-named origin ref. A PR whose
                # number or head is already recorded on ANOTHER ticket is that ticket's
                # (a branch override can legally record any name).
                siblings = [
                    t for t in self.list_tickets(project=ticket.project)
                    if t.id != ticket.id
                ]
                claimed_numbers = {t.pr_number for t in siblings if t.pr_number is not None}
                claimed_heads = {t.branch.lower() for t in siblings if t.branch}
                # Legacy stores can predate the project ↔ repo 1:1 enforcement, and two
                # CHECKOUTS (worktrees/clones) of one repository defeat path comparison
                # entirely — the origin remote URL / GitHub slug is the shared-identity
                # signal there (best-effort: identity checks degrade, they don't block).
                # Either way, ownership can't be reasoned about per-project — decline
                # discovery.
                mine = self._repo_identities(project)
                shared = sorted(
                    p.key
                    for p in self.list_projects()
                    if p.key != project.key and (mine & self._repo_identities(p))
                )
                if shared:
                    return {}, self._nothing_note(
                        to_state,
                        f"project(s) {', '.join(shared)} share this repo — PR ownership "
                        "is ambiguous across projects, resolve manually",
                    )
                matches = [
                    p for p in open_prs
                    if not p.cross_repo
                    and p.base == base
                    and p.number not in claimed_numbers
                    and p.head.lower() not in claimed_heads
                    and self._head_names_ticket(p.head, ticket)
                ]
                if not matches:
                    return {}, self._nothing_note(
                        to_state,
                        "no PR is recorded on this ticket and no open PR head matches "
                        f"`{ticket.id}[-…]`",
                    )
                if len(matches) > 1:
                    heads = ", ".join(f"#{p.number} (`{p.head}`)" for p in matches)
                    return {}, self._nothing_note(
                        to_state,
                        f"multiple open PRs match its branch convention: {heads} — "
                        "resolve manually",
                    )
                found = matches[0]
                number, url, branch = found.number, found.url, found.head
                # Adopt the discovered PR onto the ticket BEFORE acting on it: if the
                # merge/close lands remotely but the client fails afterwards (timeout),
                # the PR is no longer open and a retry could never rediscover it — with
                # the identity recorded, the retry takes the recorded-PR path instead.
                self.store.change_ticket(
                    ticket.id,
                    {
                        "branch": found.head,
                        "pr_number": found.number,
                        "pr_url": found.url,
                        "pr_state": "open",
                    },
                    actor=actor,
                    kind="comment",
                    body=f"Adopted open PR #{found.number} ({found.url}) on branch "
                    f"`{found.head}` — discovered by branch convention.",
                    meta={},
                    when=_now(),
                )
                extra = {"pr_number": found.number, "pr_url": found.url, "branch": found.head}
            # Branch cleanup must target the PR's *own* head, resolved fresh from GitHub —
            # not any stored ticket field, which could have drifted from the real head (a
            # caller override, or a row from before branch pinning). If the head can't be
            # confirmed, ``cleanup_head`` is None and the client skips deletion rather than
            # risk removing an unrelated branch. ``note_branch`` is display-only.
            cleanup_head = ops.pr_head(number)
            note_branch = cleanup_head or ticket.branch or branch
            if to_state == "done":
                result = ops.merge_pr(number, cleanup_head)
                if result.state == "queued":
                    # On a merge-queue-protected branch the PR was only enqueued, not
                    # landed — record that honestly instead of a false merge confirmation.
                    note = self._queued_note(number, url, base, note_branch)
                    return {**extra, "pr_state": "queued"}, note
                note = self._merge_note(
                    number, url, base, note_branch, result.sha,
                    remote=ops.remote, remote_branch_deleted=result.branch_deleted,
                )
                return {**extra, "pr_state": "merged"}, note
            result = ops.close_pr(number, cleanup_head)
            note = self._close_note(
                number, url, note_branch, result.branch_deleted, remote=ops.remote
            )
            return {**extra, "pr_state": "closed"}, note
        return {}, None

    @staticmethod
    def _nothing_note(to_state: str, reason: str) -> str:
        """The never-skip-silently record (SOLO-27): automation ran on done/cancelled
        but had nothing to act on — say so on the card instead of looking merged."""
        action = "merged" if to_state == "done" else "closed"
        return f"GitHub automation: no PR was {action} — {reason}."

    @staticmethod
    def _head_names_ticket(head: str, ticket: Ticket) -> bool:
        """Default-convention ownership: a head that is exactly ``<ID>`` or starts with
        ``<ID>-`` is this ticket's branch. Case-insensitive (hand-made branches are
        routinely lowercase); the trailing ``-`` boundary keeps SOLO-1 from matching
        SOLO-10's branches. Only ever consulted for default-convention projects —
        discovery declines outright on custom conventions."""
        h = head.lower()
        tid = ticket.id.lower()
        return h == tid or h.startswith(tid + "-")

    @staticmethod
    def _branch_cleanup_note(branch: str, branch_deleted: bool) -> str:
        """How the merge/close note describes the best-effort branch cleanup outcome.

        Honest either way: a branch checked out in a worktree (the normal SoloPM workflow)
        is *retained*, not deleted, so the note must not claim a deletion that didn't happen.
        """
        if branch_deleted:
            return f"Branch `{branch}` deleted."
        return f"Branch `{branch}` retained (checked out in a worktree or cleanup failed)."

    @staticmethod
    def _merge_note(
        number: int,
        url: str | None,
        base: str,
        branch: str,
        sha: str | None,
        *,
        remote: bool = False,
        remote_branch_deleted: bool = False,
    ) -> str:
        """A self-contained record of a squash-merge for the ticket's activity log.

        Local mode: the local branch is intentionally retained [SOLO-18] — it's checked
        out in the developer's worktree, which SoloPM leaves in place, so the note never
        claims a local deletion. (The remote branch is cleaned up separately, best-effort.)
        Remote mode (SOLO-29): the checkout is on another machine SoloPM can't touch, so
        the note reports the origin ref's fate and points the cleanup there.
        """
        where = f" ({url})" if url else ""
        commit = f"squash commit `{sha}`" if sha else "squash-merged"
        if remote:
            ref = (
                f"Origin branch `{branch}` deleted."
                if remote_branch_deleted
                else f"Origin branch `{branch}` retained (cleanup failed — remove it manually)."
            )
            return (
                f"Merged PR #{number}{where} into `{base}` — {commit}. {ref} The checkout "
                f"on the dev machine is untouched — clean up its local branch there."
            )
        return (
            f"Merged PR #{number}{where} into `{base}` — {commit}. Local branch `{branch}` "
            f"left in place for its worktree — delete it when you remove the worktree."
        )

    @staticmethod
    def _queued_note(number: int, url: str | None, base: str, branch: str) -> str:
        """A record that a PR was added to GitHub's merge queue rather than merged yet.

        The gating merge no longer carries ``--delete-branch`` (it would abort on a branch
        held by a worktree), and SoloPM gets no callback when the queue finally lands the
        merge — so this note does not promise a SoloPM-driven branch deletion. Branch `{branch}`
        is cleaned up by GitHub's auto-delete-on-merge (if enabled) or manually afterwards.
        """
        where = f" ({url})" if url else ""
        return (
            f"PR #{number}{where} was added to the merge queue for `{base}` — it will "
            f"squash-merge once required checks pass. Branch `{branch}` remains until then "
            f"(removed by GitHub auto-delete if enabled, otherwise clean up manually)."
        )

    @staticmethod
    def _close_note(
        number: int, url: str | None, branch: str, branch_deleted: bool, *, remote: bool = False
    ) -> str:
        """A record of a PR closed when a ticket is cancelled."""
        where = f" ({url})" if url else ""
        if remote:
            ref = (
                f"Origin branch `{branch}` deleted."
                if branch_deleted
                else f"Origin branch `{branch}` retained (cleanup failed — remove it manually)."
            )
            cleanup = f"{ref} The checkout on the dev machine is untouched."
        else:
            cleanup = Service._branch_cleanup_note(branch, branch_deleted)
        return f"Closed PR #{number}{where}. {cleanup}"

    def reorder_ticket(self, ticket_id: str, *, after: str | None = None, actor: str = "human") -> Ticket:
        """Reposition a ticket within its current column (cosmetic; no state change).

        ``after`` is the id of the ticket this one should sit immediately below, or
        ``None`` to move it to the top.
        """
        _require_actor(actor)
        ticket = self.get_ticket(ticket_id)
        if after == ticket_id:
            return ticket  # dropped onto itself
        new_pos = self._position_in_column(
            ticket.project, ticket.state, after, exclude_id=ticket_id
        )
        self.store.set_position(ticket_id, new_pos)
        return self.get_ticket(ticket_id)

    def assign_ticket(self, ticket_id: str, assignee: str, *, actor: str = "human") -> Ticket:
        _require_actor(actor)
        if assignee not in ASSIGNEES:
            raise ValidationError(
                f"Unknown assignee {assignee!r}: expected one of {', '.join(ASSIGNEES)}."
            )
        ticket = self.get_ticket(ticket_id)
        if ticket.assignee == assignee:
            return ticket
        self.store.change_ticket(
            ticket_id,
            {"assignee": assignee},
            actor=actor,
            kind="assignment",
            body=f"assigned {ticket.assignee} → {assignee}",
            meta={"from": ticket.assignee, "to": assignee},
            when=_now(),
        )
        return self.get_ticket(ticket_id)

    def submit_review(
        self,
        ticket_id: str,
        verdict: str,
        *,
        comment: str | None = None,
        criteria_results: list[dict] | None = None,
        actor: str = "human",
    ) -> Ticket:
        """Report an AI-review verdict on a ticket that is in ``in-ai-review``.

        ``pass`` advances the ticket to ``in-human-review``; ``fail`` records the review
        notes as a comment and kicks the ticket back to ``in-progress`` for the
        implementing agent to address. An optional ``comment`` carries the review notes.

        ``criteria_results`` is an optional per-criterion result set — a list of
        ``{criterion_id, verdict, note}`` — recorded to the activity log (it does not
        change the overall verdict, which still gates the transition).
        """
        _require_actor(actor)
        if verdict not in ("pass", "fail"):
            raise ValidationError(f"Unknown verdict {verdict!r}: expected 'pass' or 'fail'.")
        ticket = self.get_ticket(ticket_id)
        if ticket.state != "in-ai-review":
            raise ValidationError(
                f"Ticket {ticket_id} is not in AI review (state: {ticket.state})."
            )
        results = self._validate_criteria_results(
            criteria_results, {c.id for c in ticket.acceptance_criteria}
        )
        if results:
            self.store.change_ticket(
                ticket_id,
                {},
                actor=actor,
                kind="review",
                body=f"recorded {len(results)} per-criterion review result(s)",
                meta={"results": results},
                when=_now(),
            )
        if verdict == "pass":
            # Pass is move-only — review notes are a fail/kickback concept (per the brief).
            return self.move_ticket(ticket_id, "in-human-review", actor=actor)
        # Fail: record the review notes (if any), then kick back to the implementer.
        if comment and comment.strip():
            self.comment_ticket(ticket_id, body=comment, actor=actor)
            # Learning gate: the finding becomes a review-memory candidate for this project.
            self._capture_review_memory(ticket.project, comment.strip(), "ai_fail", ticket_id)
        return self.move_ticket(ticket_id, "in-progress", actor=actor)

    @staticmethod
    def _validate_criteria_results(results: list[dict] | None, valid_ids: set[str]) -> list[dict]:
        if not results:
            return []
        clean: list[dict] = []
        for r in results:
            cid = r.get("criterion_id")
            verdict = r.get("verdict")
            if not cid:
                raise ValidationError("Each criteria result needs a 'criterion_id'.")
            if cid not in valid_ids:
                # Audit data must reference a real criterion on this ticket — a typo or
                # stale id would otherwise be recorded silently.
                raise ValidationError(f"Unknown criterion {cid!r} for this ticket.")
            if verdict not in ("pass", "fail"):
                raise ValidationError(
                    f"Criterion {cid} result verdict must be 'pass' or 'fail', got {verdict!r}."
                )
            note = r.get("note")
            if note is not None and not isinstance(note, str):
                # The HTTP API's schema rejects this (422 validation); reject it here
                # too so the two MCP modes can't fork on malformed input.
                raise ValidationError(f"Criterion {cid} result 'note' must be a string.")
            clean.append({"criterion_id": cid, "verdict": verdict, "note": note})
        return clean

    # --- acceptance criteria ------------------------------------------------
    #
    # Each mutation is applied through ``store.mutate_criteria`` so the read-modify-write
    # happens inside one write transaction — concurrent CLI/web/MCP edits to the same
    # ticket serialize and can't lose each other's updates. Input validation (actor, text)
    # runs here, up front; the closure does the id allocation / lookup atomically.

    @staticmethod
    def _next_criterion_id(criteria: list[dict]) -> str:
        nums = [int(c["id"][1:]) for c in criteria if str(c["id"])[1:].isdigit()]
        return f"c{(max(nums) + 1) if nums else 1}"

    @staticmethod
    def _criterion(criteria: list[dict], criterion_id: str, ticket_id: str) -> dict:
        for c in criteria:
            if c["id"] == criterion_id:
                return c
        raise NotFoundError(f"Criterion {criterion_id!r} not found on {ticket_id}.")

    def add_criterion(self, ticket_id: str, text: str, *, actor: str = "human") -> Ticket:
        _require_actor(actor)
        if not text or not text.strip():
            raise ValidationError("Criterion text is required.")
        text = text.strip()

        def mutate(criteria: list[dict]):
            cid = self._next_criterion_id(criteria)
            criteria.append({"id": cid, "text": text, "done": False})
            return criteria, "criteria", f"added acceptance criterion: {text}", {"op": "add", "id": cid}

        self.store.mutate_criteria(ticket_id, mutate, actor=actor, when=_now())
        return self.get_ticket(ticket_id)

    def update_criterion(
        self,
        ticket_id: str,
        criterion_id: str,
        *,
        text: str | None = None,
        done: bool | None = None,
        actor: str = "human",
    ) -> Ticket:
        """Edit a criterion's text and/or its done flag in a single atomic mutation.

        Applying both fields in one ``mutate_criteria`` keeps a combined update from
        partially landing (text committed, then the flag failing) under concurrency.
        """
        _require_actor(actor)
        if text is not None and not text.strip():
            raise ValidationError("Criterion text cannot be blank.")
        if text is None and done is None:
            raise ValidationError("Provide 'text' and/or 'done' to update a criterion.")
        text_clean = text.strip() if text is not None else None

        def mutate(criteria: list[dict]):
            crit = self._criterion(criteria, criterion_id, ticket_id)
            if text_clean is not None:
                crit["text"] = text_clean
            if done is not None:
                crit["done"] = bool(done)
            if text_clean is not None and done is not None:
                body = f"updated acceptance criterion {criterion_id}"
            elif text_clean is not None:
                body = f"edited acceptance criterion {criterion_id}"
            else:
                body = f"{'checked' if crit['done'] else 'unchecked'} acceptance criterion: {crit['text']}"
            return criteria, "criteria", body, {"op": "update", "id": criterion_id, "done": crit["done"]}

        self.store.mutate_criteria(ticket_id, mutate, actor=actor, when=_now())
        return self.get_ticket(ticket_id)

    def edit_criterion(
        self, ticket_id: str, criterion_id: str, text: str, *, actor: str = "human"
    ) -> Ticket:
        return self.update_criterion(ticket_id, criterion_id, text=text, actor=actor)

    def remove_criterion(self, ticket_id: str, criterion_id: str, *, actor: str = "human") -> Ticket:
        _require_actor(actor)

        def mutate(criteria: list[dict]):
            removed = self._criterion(criteria, criterion_id, ticket_id)
            kept = [c for c in criteria if c["id"] != criterion_id]
            return kept, "criteria", f"removed acceptance criterion: {removed['text']}", {"op": "remove", "id": criterion_id}

        self.store.mutate_criteria(ticket_id, mutate, actor=actor, when=_now())
        return self.get_ticket(ticket_id)

    def check_criterion(
        self, ticket_id: str, criterion_id: str, done: bool = True, *, actor: str = "human"
    ) -> Ticket:
        return self.update_criterion(ticket_id, criterion_id, done=done, actor=actor)

    # --- ticket tags (SOLO-21) ----------------------------------------------
    #
    # Tags are normalized (lowercase, validated, sorted, unique) and persisted as a JSON
    # column. Mutations go through ``store.mutate_tags`` so the read-modify-write is atomic;
    # adding an already-present tag or removing an absent one is an idempotent no-op (no
    # activity), so the log only records real changes.

    def add_tags(self, ticket_id: str, tags: list[str], *, actor: str = "human") -> Ticket:
        _require_actor(actor)
        self._require_ticket(ticket_id)  # existence check (clean not_found)
        incoming = normalize_tags(tags)  # validates + dedupes + sorts
        if not incoming:
            raise ValidationError("Provide at least one tag to add.")

        def mutate(current: list[str]):
            merged = list(current)
            added = [t for t in incoming if t not in merged]
            if not added:
                return current, "tags", "", {}  # all present → idempotent no-op
            merged.extend(added)
            if len(merged) > TAGS_MAX_COUNT:
                raise ValidationError(f"A ticket may have at most {TAGS_MAX_COUNT} tags.")
            plural = "s" if len(added) != 1 else ""
            return sorted(merged), "tags", f"added tag{plural}: {', '.join(added)}", {
                "op": "add",
                "tags": added,
            }

        self.store.mutate_tags(ticket_id, mutate, actor=actor, when=_now())
        return self.get_ticket(ticket_id)

    def remove_tag(self, ticket_id: str, tag: str, *, actor: str = "human") -> Ticket:
        _require_actor(actor)
        self._require_ticket(ticket_id)
        target = normalize_tag(tag)

        def mutate(current: list[str]):
            if target not in current:
                return current, "tags", "", {}  # absent → idempotent no-op
            kept = [t for t in current if t != target]
            return kept, "tags", f"removed tag: {target}", {"op": "remove", "tag": target}

        self.store.mutate_tags(ticket_id, mutate, actor=actor, when=_now())
        return self.get_ticket(ticket_id)

    # --- ticket relationships (SOLO-10) -------------------------------------
    #
    # Links are stored once in a canonical direction (see ``models.LINK_TYPES``); the
    # inverse view is derived per endpoint on read by ``_attach_relations`` so a link made
    # from either side shows correctly on both tickets. Cross-project links are allowed —
    # ids are resolved against the whole ticket space, not the project.

    _CLOSED_STATES = frozenset({"done", "cancelled"})

    def link_tickets(
        self, ticket_id: str, link_type: str, other_id: str, *, actor: str = "human"
    ) -> Ticket:
        """Create a relationship from ``ticket_id`` to ``other_id``.

        ``link_type`` ∈ ``blocks | related | duplicate | parent`` (read as
        "``ticket_id`` <type> ``other_id``"): blocks → ``ticket_id`` is the blocker;
        duplicate → ``ticket_id`` is the duplicate of ``other_id``; parent → ``other_id``
        becomes ``ticket_id``'s parent (``ticket_id`` is the sub-ticket); related is
        symmetric. Re-creating an identical link is an idempotent no-op (deduped). Rejects
        self-links, parent cycles, and a second parent. Returns the (refreshed) acting ticket.
        """
        _require_actor(actor)
        if link_type not in LINK_TYPES:
            raise ValidationError(
                f"Unknown relation type {link_type!r}: expected one of {', '.join(LINK_TYPES)}."
            )
        a_id = normalize_ticket_id(ticket_id)
        b_id = normalize_ticket_id(other_id)
        if a_id == b_id:
            raise ValidationError("A ticket cannot be linked to itself.")
        a = self._require_ticket(a_id)  # raises NotFoundError if missing
        b = self._require_ticket(b_id)

        from_id, to_id = self._canonical(a, b, link_type)
        if link_type == "parent":
            # Canonical parent storage is child→parent, so ``from`` is the child.
            self._check_parent_link(child=from_id, parent=to_id)

        _, from_label = relation_view(link_type, True)
        _, to_label = relation_view(link_type, False)
        body_from = f"linked {to_id} ({from_label.lower()})"
        body_to = f"linked {from_id} ({to_label.lower()})"
        self.store.add_link(
            from_id,
            to_id,
            link_type,
            actor=actor,
            when=_now(),
            body_from=body_from,
            body_to=body_to,
        )
        return self.get_ticket(a_id)

    def unlink_tickets(
        self,
        ticket_id: str,
        other_id: str,
        *,
        type: str | None = None,
        direction: str | None = None,
        actor: str = "human",
    ) -> Ticket:
        """Remove the link(s) between ``ticket_id`` and ``other_id``.

        ``type`` optionally narrows to one relation type; without it, every link between the
        pair is removed. ``direction`` (``"out"`` = ``ticket_id`` is the stored ``from``,
        ``"in"`` = it is the ``to``) pins which orientation to remove — needed when a pair
        holds opposing directional links (e.g. ``A blocks B`` *and* ``B blocks A``) so the
        UI's per-row removal deletes exactly the relation clicked, not its mirror. Omitting
        it removes the link(s) in either order (order-independent). Raises ``NotFoundError``
        if no matching link exists. Returns the (refreshed) acting ticket.
        """
        _require_actor(actor)
        if type is not None and type not in LINK_TYPES:
            raise ValidationError(
                f"Unknown relation type {type!r}: expected one of {', '.join(LINK_TYPES)}."
            )
        if direction is not None and direction not in ("out", "in"):
            raise ValidationError(
                f"Unknown direction {direction!r}: expected 'out' or 'in'."
            )
        a_id = normalize_ticket_id(ticket_id)
        b_id = normalize_ticket_id(other_id)
        self._require_ticket(a_id)  # existence checks (clean not_found, not an empty unlink)
        self._require_ticket(b_id)

        def body_for(link: Link, this_id: str) -> str:
            is_from = link.from_ticket == this_id
            other = link.to_ticket if is_from else link.from_ticket
            _, label = relation_view(link.type, is_from)
            return f"unlinked {other} ({label.lower()})"

        removed = self.store.remove_links(
            a_id, b_id, link_type=type, direction=direction, actor=actor, when=_now(),
            body_for=body_for,
        )
        if removed == 0:
            qualifier = f"{type} " if type else ""
            raise NotFoundError(f"No {qualifier}link between {a_id} and {b_id}.")
        return self.get_ticket(a_id)

    @staticmethod
    def _canonical(a: Ticket, b: Ticket, link_type: str) -> tuple[str, str]:
        """The canonical (from, to) ids to store for a link between ``a`` and ``b``.

        Directional types keep the caller's order (``a`` is the subject). ``related`` is
        symmetric, so it is stored in a stable (project, seq) order — that way a link added
        from either side maps to the same row and dedupes.
        """
        if link_type == "related" and (b.project, b.seq) < (a.project, a.seq):
            return b.id, a.id
        return a.id, b.id

    def _check_parent_link(self, *, child: str, parent: str) -> None:
        """Enforce the parent invariants: at most one parent, and no cycles."""
        existing = self.store.get_parent(child)
        if existing is not None and existing != parent:
            raise ValidationError(
                f"{child} already has a parent ({existing}); a ticket may have only one parent."
            )
        # A cycle forms iff ``child`` is already an ancestor of ``parent`` — walk up from
        # ``parent`` and reject if we reach ``child``.
        cursor: str | None = parent
        seen: set[str] = set()
        while cursor is not None and cursor not in seen:
            if cursor == child:
                raise ValidationError(
                    f"Cannot set {parent} as the parent of {child}: it would create a cycle."
                )
            seen.add(cursor)
            cursor = self.store.get_parent(cursor)

    def _attach_relations(self, tickets: list[Ticket]) -> None:
        """Populate each ticket's ``relations`` plus the derived ``blocked`` / ``sub_*`` fields.

        Resolves the full link graph and the linked tickets' briefs in a fixed number of
        queries (regardless of how many tickets are passed), so list/board reads stay cheap.
        A ticket is ``blocked`` when it has an *open* (non-done/cancelled) blocker; the
        sub-ticket rollup counts its children (incoming parent links) and how many are done.
        """
        if not tickets:
            return
        # One ticket (the detail/get path) needs only its own links; the board path loads
        # the whole graph once and slices it per card.
        if len(tickets) == 1:
            links = self.store.links_for_ticket(tickets[0].id)
        else:
            links = self.store.list_links()
        if not links:
            return  # defaults already mean: no relations, not blocked, no sub-tickets
        by_ticket: dict[str, list[Link]] = {}
        for link in links:
            by_ticket.setdefault(link.from_ticket, []).append(link)
            by_ticket.setdefault(link.to_ticket, []).append(link)

        needed: set[str] = set()
        for ticket in tickets:
            for link in by_ticket.get(ticket.id, []):
                needed.add(link.to_ticket if link.from_ticket == ticket.id else link.from_ticket)
        briefs = self.store.ticket_briefs(needed)

        for ticket in tickets:
            relations: list[Relation] = []
            sub_done = sub_total = 0
            blocked = False
            for link in by_ticket.get(ticket.id, []):
                is_from = link.from_ticket == ticket.id
                other = link.to_ticket if is_from else link.from_ticket
                key, label = relation_view(link.type, is_from)
                brief = briefs.get(other, {})
                other_state = brief.get("state", "")
                relations.append(
                    Relation(
                        type=link.type,
                        key=key,
                        label=label,
                        direction="out" if is_from else "in",
                        other_id=other,
                        other_title=brief.get("title", other),
                        other_state=other_state,
                        created_by=link.created_by,
                        created_at=link.created_at,
                    )
                )
                if link.type == "blocks" and not is_from and other_state not in self._CLOSED_STATES:
                    blocked = True  # an open blocker → this ticket is blocked
                if link.type == "parent" and not is_from:
                    sub_total += 1  # this ticket is the parent; ``other`` is a child
                    if other_state == "done":
                        sub_done += 1
            relations.sort(key=lambda r: (RELATION_GROUP_ORDER.index(r.key), r.other_id))
            ticket.relations = relations
            # A finished (done/cancelled) ticket is never "blocked" — only open work can be
            # held up — so don't flag it even if a blocker is still open (cf. the radar
            # excluding finished tickets in SOLO-17).
            ticket.blocked = blocked and ticket.state not in self._CLOSED_STATES
            ticket.sub_done = sub_done
            ticket.sub_total = sub_total

    # --- dependency graph (SOLO-14) -----------------------------------------
    #
    # A read-only projection of the SOLO-10 ``ticket_links`` data into a node/edge graph.
    # No new storage and no new relation semantics — just shaping for visualization and
    # topological reasoning. Edges keep SOLO-10's canonical direction.

    _GRAPH_NODE_LIMIT = 500

    def build_graph(
        self,
        *,
        project: str | None = None,
        around: str | None = None,
        depth: int = 1,
        active_only: bool = False,
        types: list[str] | None = None,
        limit: int | None = None,
    ) -> dict:
        """Build a relationship graph (nodes + typed edges) over ``ticket_links``.

        Scope (``around`` takes precedence over ``project``):
          * ``around`` (+ ``depth``) — the ego-graph reachable within ``depth`` hops of a
            ticket, following links of the selected ``types`` in either direction;
          * ``project`` — the project's *connected* tickets (those in ≥1 link) plus their
            linked neighbours (cross-project neighbours included, so an edge always has both
            endpoints); isolated tickets are omitted — a relationship graph has nothing to
            show for them;
          * neither — the whole store's relational graph.

        ``types`` restricts which relation types appear (and, for the ego-graph, which are
        traversed). ``active_only`` drops done/cancelled nodes and their edges. Edges use
        SOLO-10's canonical ``from``→``to`` direction. Each node carries the same derived
        ``blocked`` / ``subtickets`` signals as the board, computed from the full link graph
        (independent of the view filters). Cycles in the ``blocks`` sub-graph are detected
        and returned in ``cycles`` — never fatal (SOLO-10 forbids parent cycles, not blocks).
        """
        type_filter: set[str] | None = None
        if types:
            unknown = set(types) - set(LINK_TYPES)
            if unknown:
                raise ValidationError(
                    f"Unknown relation type(s) {', '.join(sorted(unknown))}: "
                    f"expected from {', '.join(LINK_TYPES)}."
                )
            type_filter = set(types)
        node_limit = self._GRAPH_NODE_LIMIT if limit is None else max(1, limit)

        all_links = self.store.list_links()
        view_links = (
            [ln for ln in all_links if ln.type in type_filter] if type_filter else all_links
        )

        # Undirected adjacency over in-view links, for ego-graph traversal / connectivity.
        adjacency: dict[str, set[str]] = {}
        for ln in view_links:
            adjacency.setdefault(ln.from_ticket, set()).add(ln.to_ticket)
            adjacency.setdefault(ln.to_ticket, set()).add(ln.from_ticket)

        scope_project: str | None = None
        dist: dict[str, int] = {}  # BFS distance from the ego root, for distance-aware capping
        if around is not None:
            around = normalize_ticket_id(around)
            self._require_ticket(around)  # NotFoundError if missing
            if depth < 0:
                raise ValidationError("Graph depth must be >= 0.")
            node_ids = {around}
            dist[around] = 0
            frontier = {around}
            for hop in range(1, depth + 1):
                nxt: set[str] = set()
                for node in frontier:
                    nxt |= adjacency.get(node, set()) - node_ids
                if not nxt:
                    break
                for m in nxt:
                    dist[m] = hop
                node_ids |= nxt
                frontier = nxt
        elif project is not None:
            scope_project = normalize_project_key(project)
            self.get_project(scope_project)  # NotFoundError for an unknown project
            project_ids = {t.id for t in self.store.list_tickets(project=scope_project)}
            node_ids = set()
            for ln in view_links:
                if ln.from_ticket in project_ids or ln.to_ticket in project_ids:
                    node_ids.add(ln.from_ticket)
                    node_ids.add(ln.to_ticket)
        else:
            node_ids = set(adjacency.keys())

        # Resolve briefs for the node set plus any off-graph blocker/child referenced by the
        # full (unfiltered) link set, so derived signals match the board exactly.
        ref_ids = set(node_ids)
        for ln in all_links:
            if ln.type in ("blocks", "parent") and ln.to_ticket in node_ids:
                ref_ids.add(ln.from_ticket)
        briefs = self.store.ticket_briefs(ref_ids)

        if active_only:
            node_ids = {
                n
                for n in node_ids
                if briefs.get(n, {}).get("state") not in self._CLOSED_STATES
            }

        truncated = False
        if len(node_ids) > node_limit:
            if around is not None:
                # ego-graph: keep the nodes nearest the root (the root, at distance 0, never
                # dropped).
                order = lambda i: (dist.get(i, 1 << 30), *self._node_sort_key(i, briefs))
            elif scope_project is not None:
                # project scope: keep the requested project's own tickets before neighbours,
                # so the cap can't crowd them out with cross-project nodes that sort earlier.
                order = lambda i: (
                    0 if briefs.get(i, {}).get("project") == scope_project else 1,
                    *self._node_sort_key(i, briefs),
                )
            else:
                order = lambda i: self._node_sort_key(i, briefs)
            node_ids = set(sorted(node_ids, key=order)[:node_limit])
            truncated = True

        # Edges among the current node set (canonical direction); rebuilt after any prune.
        edges = sorted(
            (
                {"from": ln.from_ticket, "to": ln.to_ticket, "type": ln.type}
                for ln in view_links
                if ln.from_ticket in node_ids and ln.to_ticket in node_ids
            ),
            key=lambda e: (e["from"], e["to"], e["type"]),
        )

        # Project scope: a foreign (cross-project) node earns its place ONLY via an edge to a
        # surviving in-project node — not via a foreign↔foreign edge. If active-only filtering
        # dropped its in-project anchor, prune it and rebuild the edge list so dangling
        # foreign↔foreign edges go too. In-project nodes are kept even if they end up isolated.
        if scope_project is not None:
            in_project = {
                n for n in node_ids if briefs.get(n, {}).get("project") == scope_project
            }
            keep_foreign: set[str] = set()
            for e in edges:
                if e["from"] in in_project and e["to"] not in in_project:
                    keep_foreign.add(e["to"])
                elif e["to"] in in_project and e["from"] not in in_project:
                    keep_foreign.add(e["from"])
            node_ids = in_project | keep_foreign
            edges = [e for e in edges if e["from"] in node_ids and e["to"] in node_ids]

        # Derived blocked / sub-ticket rollup from the FULL link set (view-independent).
        blocked_ids: set[str] = set()
        sub_total: dict[str, int] = {}
        sub_done: dict[str, int] = {}
        for ln in all_links:
            if ln.to_ticket not in node_ids:
                continue
            src_state = briefs.get(ln.from_ticket, {}).get("state")
            if ln.type == "blocks" and src_state not in self._CLOSED_STATES:
                blocked_ids.add(ln.to_ticket)
            elif ln.type == "parent":
                sub_total[ln.to_ticket] = sub_total.get(ln.to_ticket, 0) + 1
                if src_state == "done":
                    sub_done[ln.to_ticket] = sub_done.get(ln.to_ticket, 0) + 1

        nodes = []
        for nid in sorted(node_ids, key=lambda i: self._node_sort_key(i, briefs)):
            brief = briefs.get(nid, {})
            state = brief.get("state", "")
            nodes.append(
                {
                    "id": nid,
                    "project": brief.get("project", ""),
                    "title": brief.get("title", nid),
                    "state": state,
                    "assignee": brief.get("assignee", ""),
                    "blocked": nid in blocked_ids and state not in self._CLOSED_STATES,
                    "subtickets": {"done": sub_done.get(nid, 0), "total": sub_total.get(nid, 0)},
                }
            )
        cycles = self._blocks_cycles(node_ids, [e for e in edges if e["type"] == "blocks"])
        return {
            "nodes": nodes,
            "edges": edges,
            "cycles": cycles,
            "scope": {
                "project": scope_project,
                "around": around,
                "depth": depth if around is not None else None,
                "active_only": active_only,
                "types": sorted(type_filter) if type_filter else list(LINK_TYPES),
            },
            "truncated": truncated,
        }

    @staticmethod
    def _node_sort_key(node_id: str, briefs: dict) -> tuple:
        """Deterministic node ordering: by project then sequence number."""
        brief = briefs.get(node_id, {})
        try:
            seq = int(node_id.rsplit("-", 1)[-1])
        except ValueError:
            seq = 0
        return (brief.get("project", ""), seq, node_id)

    @staticmethod
    def _blocks_cycles(node_ids: set[str], blocks_edges: list[dict]) -> list[list[str]]:
        """Strongly-connected components (size > 1) of the ``blocks`` sub-graph — i.e. the
        groups of tickets that block each other in a loop. Iterative Tarjan, so a long chain
        can't blow the recursion stack. Returns each cycle as a sorted id list."""
        adj: dict[str, list[str]] = {n: [] for n in node_ids}
        for e in blocks_edges:
            if e["from"] in adj and e["to"] in adj:
                adj[e["from"]].append(e["to"])

        index = {}
        lowlink = {}
        on_stack: set[str] = set()
        stack: list[str] = []
        counter = 0
        sccs: list[list[str]] = []

        for root in adj:
            if root in index:
                continue
            work = [(root, 0)]  # (node, next-neighbour-index)
            while work:
                node, pi = work[-1]
                if pi == 0:
                    index[node] = lowlink[node] = counter
                    counter += 1
                    stack.append(node)
                    on_stack.add(node)
                recursed = False
                neighbours = adj[node]
                i = pi
                while i < len(neighbours):
                    w = neighbours[i]
                    if w not in index:
                        work[-1] = (node, i + 1)
                        work.append((w, 0))
                        recursed = True
                        break
                    if w in on_stack:
                        lowlink[node] = min(lowlink[node], index[w])
                    i += 1
                if recursed:
                    continue
                if lowlink[node] == index[node]:
                    comp = []
                    while True:
                        w = stack.pop()
                        on_stack.discard(w)
                        comp.append(w)
                        if w == node:
                            break
                    if len(comp) > 1:
                        sccs.append(sorted(comp))
                work.pop()
                if work:
                    parent = work[-1][0]
                    lowlink[parent] = min(lowlink[parent], lowlink[node])
        return sorted(sccs)

    # --- overlap / conflict radar -------------------------------------------

    _RADAR_ACTIVE_STATES = frozenset({"in-progress", "in-ai-review", "in-human-review"})

    def compute_radar(self, project: str | None = None) -> dict:
        """Warn (don't block) when active worktrees touch the same files.

        Reads each project repo's live worktrees straight from git, computes each one's
        changed-file set vs. the master branch (committed + uncommitted), and reports every
        pair whose sets intersect. A branch is annotated with the active ticket that records
        it; branches whose ticket has gone inactive (done/merged, cancelled, or back in the
        backlog) are skipped so a lingering worktree can't conflict against live work — except
        a → done whose PR is only enqueued (not yet landed on master), which stays live. Genuinely
        unmapped branches (no ticket at all) are still reported. A no-op without a git client /
        repo, so it degrades gracefully rather than erroring.
        """
        projects = [self.get_project(project)] if project else self.list_projects()
        overlaps: list[dict] = []
        skipped: list[dict] = []
        for proj in projects:
            if proj.github_repo:
                # Remote project (SOLO-29): its worktrees are on the dev machine. Say so
                # instead of silently reporting "no overlaps" for repos never scanned.
                skipped.append(
                    {
                        "project": proj.key,
                        "reason": "remote project — its worktrees live on the dev machine, not scanned",
                    }
                )
                continue
            if self.github is None or not proj.repo:
                continue
            # A branch is mapped to its ticket while that ticket's changes are still live.
            # Branches whose ticket has gone inactive (done/merged, cancelled, or sent back
            # to the backlog) are recorded separately so their lingering worktrees can be
            # skipped — a merged ticket's leftover worktree must not raise a conflict against
            # live work. A → done whose PR only *enqueued* (pr_state "queued") is the
            # exception: its changes have not landed on master yet, so it is still live and
            # can genuinely conflict during the merge-queue window.
            by_branch: dict[str, str] = {}
            inactive_branches: set[str] = set()
            for t in self.list_tickets(project=proj.key):
                if not t.branch:
                    continue
                # `pr_state == "queued"` is read from stored state, not refreshed live: once
                # the merge queue lands the PR there is no callback to flip it to "merged",
                # so a queued-done branch can stay radar-live longer than strictly necessary.
                # That is deliberate — the radar is a cheap, local, best-effort scan and must
                # not make a GitHub API call per ticket. The cost is at worst a non-blocking
                # spurious warning, bounded by the worktree's lifetime (cleaning up the merged
                # worktree drops the entry), which is the safe direction for a conflict radar.
                if t.state in self._RADAR_ACTIVE_STATES or (
                    t.state == "done" and t.pr_state == "queued"
                ):
                    by_branch[t.branch] = t.id
                else:
                    inactive_branches.add(t.branch)
            # An active ticket wins a shared branch: branch names aren't unique until a PR
            # pins them, so a branch reused by live work must not be skipped just because an
            # older done/cancelled/backlogged ticket also recorded it.
            inactive_branches -= set(by_branch)
            entries: list[dict] = []
            try:
                for wt in self.github.list_worktrees(proj.repo):
                    if not wt.branch or wt.branch == proj.master_branch:
                        continue
                    if wt.branch in inactive_branches:
                        continue
                    files = self.github.worktree_changed_files(wt.path, proj.master_branch)
                    if files:
                        entries.append(
                            {"branch": wt.branch, "ticket": by_branch.get(wt.branch), "files": files}
                        )
            except GitHubError:
                # Best-effort: a stale/non-git repo path skips this project rather than
                # failing the whole radar (which can scan every project).
                continue
            for i in range(len(entries)):
                for j in range(i + 1, len(entries)):
                    shared = sorted(entries[i]["files"] & entries[j]["files"])
                    if shared:
                        overlaps.append(
                            {
                                "project": proj.key,
                                "a": {"ticket": entries[i]["ticket"], "branch": entries[i]["branch"]},
                                "b": {"ticket": entries[j]["ticket"], "branch": entries[j]["branch"]},
                                "files": shared,
                            }
                        )
        return {"overlaps": overlaps, "skipped": skipped}

    # --- review memory (the learning review gate) ---------------------------

    _MEMORY_STATUSES = frozenset({"candidate", "active", "retired"})
    _MEMORY_SOURCES = frozenset({"ai_fail", "human_miss", "manual"})

    @staticmethod
    def _next_memory_id(items: list[dict]) -> str:
        nums = [int(i["id"][1:]) for i in items if str(i["id"])[1:].isdigit()]
        return f"m{(max(nums) + 1) if nums else 1}"

    @staticmethod
    def _find_memory(items: list[dict], item_id: str, project_key: str) -> dict:
        for i in items:
            if i["id"] == item_id:
                return i
        raise NotFoundError(f"Review-memory item {item_id!r} not found in {project_key}.")

    def list_review_memory(self, project: str, *, status: str | None = None) -> list[dict]:
        items = self.get_project(project).review_memory
        return [i.to_dict() for i in items if status is None or i.status == status]

    def add_review_memory(
        self,
        project: str,
        text: str,
        *,
        source: str = "manual",
        status: str = "active",
        ticket: str | None = None,
    ) -> dict:
        key = self.get_project(project).key
        if not text or not text.strip():
            raise ValidationError("Review-memory text is required.")
        if status not in self._MEMORY_STATUSES:
            raise ValidationError(f"Unknown status {status!r}.")
        if source not in self._MEMORY_SOURCES:
            raise ValidationError(f"Unknown source {source!r}.")
        item = {
            "id": "", "text": text.strip(), "source": source, "status": status,
            "hits": 0, "ticket": ticket, "created_at": _now(),
        }

        def mutate(items: list[dict]):
            item["id"] = self._next_memory_id(items)
            items.append(item)
            return items

        self.store.mutate_review_memory(key, mutate, when=_now())
        return item

    def update_review_memory(
        self, project: str, item_id: str, *, text: str | None = None, status: str | None = None
    ) -> dict:
        key = self.get_project(project).key
        if status is not None and status not in self._MEMORY_STATUSES:
            raise ValidationError(f"Unknown status {status!r}.")
        if text is not None and not text.strip():
            raise ValidationError("Review-memory text cannot be blank.")
        if text is None and status is None:
            raise ValidationError("Provide 'text' and/or 'status' to update.")
        result: dict = {}

        def mutate(items: list[dict]):
            item = self._find_memory(items, item_id, key)
            if text is not None:
                item["text"] = text.strip()
            if status is not None:
                item["status"] = status
            result.update(item)
            return items

        self.store.mutate_review_memory(key, mutate, when=_now())
        return result

    def assembled_review_prompt(self, project: str, *, record_hit: bool = False) -> str:
        """The base ``review_prompt`` plus the project's ACTIVE review-memory checklist —
        what a fresh-context reviewer should fetch. ``record_hit`` bumps each active item's
        ``hits`` (call it when actually starting a review)."""
        proj = self.get_project(project)
        active = [i for i in proj.review_memory if i.status == "active"]
        if record_hit and active:
            ids = {i.id for i in active}

            def mutate(items: list[dict]):
                for it in items:
                    if it["id"] in ids:
                        it["hits"] = int(it.get("hits", 0)) + 1
                return items

            self.store.mutate_review_memory(proj.key, mutate, when=_now())
        parts: list[str] = []
        if proj.review_prompt.strip():
            parts.append(proj.review_prompt.strip())
        # ACCEPTED-RISK items are adjudications, not checks: putting them under the
        # report-per-item checklist would instruct the reviewer to report the very
        # findings the convention says not to re-raise (SOLO-28).
        checks = [i for i in active if not i.text.lstrip().upper().startswith("ACCEPTED-RISK:")]
        adjudicated = [i for i in active if i.text.lstrip().upper().startswith("ACCEPTED-RISK:")]
        if checks:
            parts.append(
                "Project review checklist (accumulated review memory — verify each and "
                "report per item):\n" + "\n".join(f"- {i.text}" for i in checks)
            )
        if adjudicated:
            parts.append(
                "Adjudicated risks (explicitly ACCEPTED by the project owner — do NOT "
                "re-raise these unless the change introduces genuinely new evidence that "
                "alters the risk):\n" + "\n".join(f"- {i.text}" for i in adjudicated)
            )
        return "\n\n".join(parts)

    def _capture_review_memory(self, project_key: str, text: str, source: str, ticket: str) -> None:
        # Best-effort: capturing a learning candidate must never break the transition.
        try:
            self.add_review_memory(
                project_key, text, source=source, status="candidate", ticket=ticket
            )
        except Exception:
            pass
