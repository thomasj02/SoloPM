"""Tests for the AI-review verdict flow (the In AI Review gate)."""

import pytest

from solopm.core.errors import NotFoundError, ValidationError


def _into_ai_review(service, title="x"):
    """Create a ticket and walk it to in-ai-review (as the implementing agent)."""
    t = service.create_ticket(project="SOLO", title=title)
    service.move_ticket(t.id, "in-progress")
    service.move_ticket(t.id, "in-ai-review", actor="claude")
    return t.id


def test_submit_review_pass_moves_to_human_review(service, project):
    tid = _into_ai_review(service)
    out = service.submit_review(tid, "pass", comment="LGTM", actor="codex")
    assert out.state == "in-human-review"
    # Pass is move-only: notes-to-comments is the fail path, so no comment is recorded
    # even if one is supplied.
    assert not any(a.kind == "comment" for a in service.get_ticket(tid).activity)


def test_submit_review_fail_kicks_back_to_in_progress(service, project):
    tid = _into_ai_review(service)
    out = service.submit_review(tid, "fail", comment="needs tests", actor="codex")
    assert out.state == "in-progress"
    comments = [a for a in service.get_ticket(tid).activity if a.kind == "comment"]
    assert comments[-1].body == "needs tests"
    assert comments[-1].actor == "codex"  # review notes attributed to the reviewer


def test_submit_review_pass_without_comment(service, project):
    tid = _into_ai_review(service)
    out = service.submit_review(tid, "pass", actor="codex")
    assert out.state == "in-human-review"
    # no spurious empty comment
    assert not any(a.kind == "comment" for a in service.get_ticket(tid).activity)


def test_submit_review_requires_in_ai_review_state(service, project):
    t = service.create_ticket(project="SOLO", title="x")  # still in backlog
    with pytest.raises(ValidationError):
        service.submit_review(t.id, "pass", actor="codex")


def test_submit_review_invalid_verdict(service, project):
    tid = _into_ai_review(service)
    with pytest.raises(ValidationError):
        service.submit_review(tid, "maybe", actor="codex")


def test_submit_review_invalid_actor(service, project):
    tid = _into_ai_review(service)
    with pytest.raises(ValidationError):
        service.submit_review(tid, "pass", actor="robot")


def test_submit_review_missing_ticket(service):
    with pytest.raises(NotFoundError):
        service.submit_review("SOLO-9", "pass", actor="codex")


def test_submit_review_logs_state_change_actor(service, project):
    tid = _into_ai_review(service)
    service.submit_review(tid, "fail", comment="redo", actor="codex")
    full = service.get_ticket(tid)
    kickback = [a for a in full.activity if a.kind == "state_change"][-1]
    assert kickback.meta == {"from": "in-ai-review", "to": "in-progress"}
    assert kickback.actor == "codex"
