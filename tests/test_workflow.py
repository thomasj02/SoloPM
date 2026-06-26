"""Tests for the state-machine: legal transitions + actor rules."""

import pytest

from solopm.core import workflow
from solopm.core.errors import ForbiddenTransitionError, InvalidTransitionError, ValidationError
from solopm.core.models import STATES


def test_all_states_have_transition_entries():
    for state in STATES:
        assert state in workflow.TRANSITIONS


def test_terminal_states_have_no_outgoing():
    assert workflow.TRANSITIONS["done"] == ()
    assert workflow.TRANSITIONS["cancelled"] == ()


def test_forward_happy_path_is_legal_for_human():
    chain = [
        ("backlog", "todo"),
        ("todo", "in-progress"),
        ("in-progress", "in-ai-review"),
        ("in-ai-review", "in-human-review"),
        ("in-human-review", "done"),
    ]
    for src, dst in chain:
        # Should not raise.
        workflow.validate_transition(src, dst, actor="human")


def test_cancel_is_reachable_from_every_nonterminal_state():
    for state in STATES:
        if state in ("done", "cancelled"):
            continue
        workflow.validate_transition(state, "cancelled", actor="human")


def test_only_human_can_reach_done():
    workflow.validate_transition("in-human-review", "done", actor="human")
    for agent in ("claude", "codex"):
        with pytest.raises(ForbiddenTransitionError):
            workflow.validate_transition("in-human-review", "done", actor=agent)


def test_agent_can_self_transition_to_ai_review():
    workflow.validate_transition("in-progress", "in-ai-review", actor="claude")


def test_agent_can_move_ai_review_to_human_review():
    workflow.validate_transition("in-ai-review", "in-human-review", actor="codex")


def test_illegal_transition_raises():
    with pytest.raises(InvalidTransitionError):
        workflow.validate_transition("backlog", "done", actor="human")
    with pytest.raises(InvalidTransitionError):
        workflow.validate_transition("backlog", "in-human-review", actor="human")


def test_no_transition_out_of_terminal_states():
    with pytest.raises(InvalidTransitionError):
        workflow.validate_transition("done", "in-progress", actor="human")
    with pytest.raises(InvalidTransitionError):
        workflow.validate_transition("cancelled", "todo", actor="human")


def test_kickback_paths_are_legal():
    # AI review failure and human request-changes both go back to in-progress.
    workflow.validate_transition("in-ai-review", "in-progress", actor="codex")
    workflow.validate_transition("in-human-review", "in-progress", actor="human")


def test_unknown_state_raises_validation():
    with pytest.raises(ValidationError):
        workflow.validate_transition("backlog", "nonsense", actor="human")
    with pytest.raises(ValidationError):
        workflow.validate_transition("nonsense", "todo", actor="human")


def test_unknown_actor_raises_validation():
    with pytest.raises(ValidationError):
        workflow.validate_transition("backlog", "todo", actor="robot")


def test_is_same_state_helper():
    assert workflow.is_noop("todo", "todo") is True
    assert workflow.is_noop("todo", "backlog") is False
