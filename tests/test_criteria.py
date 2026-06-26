"""Acceptance criteria as first-class, machine-checkable ticket fields (SOLO-6)."""

import pytest

from solopm.core.errors import NotFoundError, ValidationError


def _crit(ticket):
    return ticket.to_dict()["acceptance_criteria"]


def test_new_ticket_has_empty_criteria(service, project):
    t = service.create_ticket(project="SOLO", title="x")
    assert _crit(t) == []


def test_add_criterion(service, project):
    t = service.create_ticket(project="SOLO", title="x")
    out = service.add_criterion(t.id, "Tests pass", actor="human")
    crit = _crit(out)
    assert len(crit) == 1
    assert crit[0]["text"] == "Tests pass"
    assert crit[0]["done"] is False
    assert crit[0]["id"]


def test_criteria_keep_insertion_order_with_unique_ids(service, project):
    t = service.create_ticket(project="SOLO", title="x")
    service.add_criterion(t.id, "first", actor="human")
    service.add_criterion(t.id, "second", actor="human")
    out = service.add_criterion(t.id, "third", actor="human")
    assert [c["text"] for c in _crit(out)] == ["first", "second", "third"]
    assert len({c["id"] for c in _crit(out)}) == 3


def test_check_and_uncheck_criterion(service, project):
    t = service.create_ticket(project="SOLO", title="x")
    cid = _crit(service.add_criterion(t.id, "build green", actor="claude"))[0]["id"]
    assert _crit(service.check_criterion(t.id, cid, actor="claude"))[0]["done"] is True
    assert _crit(service.check_criterion(t.id, cid, done=False, actor="claude"))[0]["done"] is False


def test_edit_criterion(service, project):
    t = service.create_ticket(project="SOLO", title="x")
    cid = _crit(service.add_criterion(t.id, "old", actor="human"))[0]["id"]
    out = service.edit_criterion(t.id, cid, "new text", actor="human")
    assert _crit(out)[0]["text"] == "new text"


def test_remove_criterion(service, project):
    t = service.create_ticket(project="SOLO", title="x")
    cid = _crit(service.add_criterion(t.id, "a", actor="human"))[0]["id"]
    service.add_criterion(t.id, "b", actor="human")
    out = service.remove_criterion(t.id, cid, actor="human")
    assert [c["text"] for c in _crit(out)] == ["b"]


def test_concurrent_adds_do_not_lose_updates(service, project):
    # Atomic read-modify-write in the store: parallel adds to one ticket must all land
    # (this fails on a plain read-then-write-blob implementation — lost updates).
    import threading

    t = service.create_ticket(project="SOLO", title="x")
    n = 25
    errors: list[Exception] = []

    def add(i: int) -> None:
        try:
            service.add_criterion(t.id, f"crit {i}", actor="claude")
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=add, args=(i,)) for i in range(n)]
    for th in threads:
        th.start()
    for th in threads:
        th.join()

    assert not errors, errors
    crit = service.get_ticket(t.id).acceptance_criteria
    assert len(crit) == n  # none dropped
    assert len({c.id for c in crit}) == n  # ids stayed unique


def test_criteria_changes_are_logged_and_attributed(service, project):
    t = service.create_ticket(project="SOLO", title="x")
    service.add_criterion(t.id, "x", actor="claude")
    assert any(a.kind == "criteria" and a.actor == "claude" for a in service.get_ticket(t.id).activity)


def test_unknown_criterion_raises(service, project):
    t = service.create_ticket(project="SOLO", title="x")
    with pytest.raises(NotFoundError):
        service.check_criterion(t.id, "c999", actor="human")


def test_blank_criterion_text_rejected(service, project):
    t = service.create_ticket(project="SOLO", title="x")
    with pytest.raises(ValidationError):
        service.add_criterion(t.id, "   ", actor="human")


def test_summary_reports_criteria_progress(service, project):
    t = service.create_ticket(project="SOLO", title="x")
    cid = _crit(service.add_criterion(t.id, "a", actor="human"))[0]["id"]
    service.add_criterion(t.id, "b", actor="human")
    service.check_criterion(t.id, cid, actor="human")
    summary = service.get_ticket(t.id).to_summary()
    assert summary["acceptance"] == {"done": 1, "total": 2}


# --- review submit per-criterion results -----------------------------------


def _into_ai_review(service, title="x"):
    t = service.create_ticket(project="SOLO", title=title)
    service.move_ticket(t.id, "in-progress")
    service.move_ticket(t.id, "in-ai-review", actor="claude")
    return t.id


def test_submit_review_records_per_criterion_results(service, project):
    tid = _into_ai_review(service)
    cid = _crit(service.add_criterion(tid, "crit one", actor="claude"))[0]["id"]
    out = service.submit_review(
        tid,
        "pass",
        criteria_results=[{"criterion_id": cid, "verdict": "pass", "note": "verified in test_x"}],
        actor="codex",
    )
    assert out.state == "in-human-review"  # overall verdict still gates the transition
    review = [a for a in service.get_ticket(tid).activity if a.kind == "review"]
    assert review, "a review activity should be recorded"
    results = review[-1].meta["results"]
    assert results[0] == {"criterion_id": cid, "verdict": "pass", "note": "verified in test_x"}
    assert review[-1].actor == "codex"


def test_submit_review_without_criteria_results_unchanged(service, project):
    tid = _into_ai_review(service)
    out = service.submit_review(tid, "pass", actor="codex")
    assert out.state == "in-human-review"
    assert not [a for a in service.get_ticket(tid).activity if a.kind == "review"]


def test_submit_review_rejects_unknown_criterion_id(service, project):
    tid = _into_ai_review(service)
    service.add_criterion(tid, "real one", actor="claude")
    with pytest.raises(ValidationError):
        service.submit_review(
            tid, "pass",
            criteria_results=[{"criterion_id": "c999", "verdict": "pass"}],
            actor="codex",
        )


def test_submit_review_rejects_bad_criterion_verdict(service, project):
    tid = _into_ai_review(service)
    cid = _crit(service.add_criterion(tid, "c", actor="claude"))[0]["id"]
    with pytest.raises(ValidationError):
        service.submit_review(
            tid, "pass",
            criteria_results=[{"criterion_id": cid, "verdict": "maybe"}],
            actor="codex",
        )
