"""The ticket state machine: legal transitions and actor rules.

Pure functions over the enums in :mod:`solopm.core.models`. The service calls
:func:`validate_transition` before mutating a ticket's state.
"""

from __future__ import annotations

from .errors import ForbiddenTransitionError, InvalidTransitionError, ValidationError
from .models import ACTORS, STATES

# Legal state -> reachable states. ``cancelled`` is reachable from every non-terminal
# state. ``done`` and ``cancelled`` are terminal (no outgoing edges).
TRANSITIONS: dict[str, tuple[str, ...]] = {
    "backlog": ("todo", "in-progress", "cancelled"),
    "todo": ("backlog", "in-progress", "cancelled"),
    "in-progress": ("backlog", "todo", "in-ai-review", "cancelled"),
    "in-ai-review": ("in-progress", "in-human-review", "cancelled"),
    "in-human-review": ("in-progress", "done", "cancelled"),
    "done": (),
    "cancelled": (),
}

# Transitions only the human actor may perform (agents cannot close a ticket).
HUMAN_ONLY_TARGETS: frozenset[str] = frozenset({"done"})


def is_noop(src: str, dst: str) -> bool:
    """A move to the state the ticket is already in is an idempotent no-op."""
    return src == dst


def validate_transition(src: str, dst: str, *, actor: str) -> None:
    """Raise if moving a ticket from ``src`` to ``dst`` as ``actor`` is illegal.

    Order of checks:
      1. states and actor are known values (``ValidationError``);
      2. the edge exists in :data:`TRANSITIONS` (``InvalidTransitionError``);
      3. the actor is permitted to make this transition (``ForbiddenTransitionError``).

    A no-op (``src == dst``) is handled by the caller and never reaches here.
    """
    if src not in STATES:
        raise ValidationError(f"Unknown state {src!r}.")
    if dst not in STATES:
        raise ValidationError(f"Unknown state {dst!r}.")
    if actor not in ACTORS:
        raise ValidationError(
            f"Unknown actor {actor!r}: expected one of {', '.join(ACTORS)}."
        )

    if dst not in TRANSITIONS[src]:
        raise InvalidTransitionError(
            f"Cannot move from {src} to {dst}."
        )

    if dst in HUMAN_ONLY_TARGETS and actor != "human":
        raise ForbiddenTransitionError(
            f"Only the human may move a ticket to {dst}; {actor} cannot close a ticket."
        )
