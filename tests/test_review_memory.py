"""Learning review gate — per-project review memory (SOLO-7)."""

import pytest

from solopm.core.errors import NotFoundError, ValidationError


def _into_ai_review(service, title="x"):
    t = service.create_ticket(project="SOLO", title=title)
    service.move_ticket(t.id, "in-progress")
    service.move_ticket(t.id, "in-ai-review", actor="claude")
    return t.id


# --- capture hooks ----------------------------------------------------------


def test_ai_fail_captures_candidate_from_comment(service, project):
    tid = _into_ai_review(service)
    service.submit_review(tid, "fail", comment="Validate branch names against injection.", actor="codex")
    cands = service.list_review_memory("SOLO", status="candidate")
    assert len(cands) == 1
    assert cands[0]["source"] == "ai_fail"
    assert cands[0]["ticket"] == tid
    assert "branch names" in cands[0]["text"]


def test_ai_fail_without_comment_captures_nothing(service, project):
    tid = _into_ai_review(service)
    service.submit_review(tid, "fail", actor="codex")  # no notes → nothing to learn
    assert service.list_review_memory("SOLO") == []


def test_human_miss_after_ai_pass_captures_candidate(service, project):
    tid = _into_ai_review(service)
    service.submit_review(tid, "pass", actor="codex")  # → in-human-review (AI passed)
    service.move_ticket(tid, "in-progress", actor="human")  # human requests changes
    miss = service.list_review_memory("SOLO", status="candidate")
    assert len(miss) == 1
    assert miss[0]["source"] == "human_miss"
    assert miss[0]["ticket"] == tid


def test_normal_ai_pass_does_not_capture(service, project):
    tid = _into_ai_review(service)
    service.submit_review(tid, "pass", actor="codex")  # passes; stays in human review
    assert service.list_review_memory("SOLO") == []


# --- curation ---------------------------------------------------------------


def test_add_is_active_by_default(service, project):
    item = service.add_review_memory("SOLO", "Check error handling on every endpoint")
    assert item["status"] == "active"
    assert item["source"] == "manual"


def test_promote_and_retire(service, project):
    tid = _into_ai_review(service)
    service.submit_review(tid, "fail", comment="raw finding", actor="codex")
    cid = service.list_review_memory("SOLO")[0]["id"]
    promoted = service.update_review_memory("SOLO", cid, text="Validate all external input", status="active")
    assert promoted["status"] == "active"
    assert promoted["text"] == "Validate all external input"
    retired = service.update_review_memory("SOLO", cid, status="retired")
    assert retired["status"] == "retired"


def test_update_unknown_item_raises(service, project):
    with pytest.raises(NotFoundError):
        service.update_review_memory("SOLO", "m999", status="active")


def test_blank_add_rejected(service, project):
    with pytest.raises(ValidationError):
        service.add_review_memory("SOLO", "   ")


def test_bad_status_rejected(service, project):
    item = service.add_review_memory("SOLO", "x")
    with pytest.raises(ValidationError):
        service.update_review_memory("SOLO", item["id"], status="bogus")


def test_unknown_source_rejected(service, project):
    with pytest.raises(ValidationError):
        service.add_review_memory("SOLO", "x", source="totally-made-up")


# --- assembled prompt -------------------------------------------------------


def test_assembled_prompt_includes_only_active_items(service, project):
    service.add_review_memory("SOLO", "ACTIVE-CHECK", status="active")
    service.add_review_memory("SOLO", "CAND-CHECK", status="candidate")
    service.add_review_memory("SOLO", "RETIRED-CHECK", status="retired")
    prompt = service.assembled_review_prompt("SOLO")
    assert "ACTIVE-CHECK" in prompt
    assert "CAND-CHECK" not in prompt
    assert "RETIRED-CHECK" not in prompt
    assert project.review_prompt.strip()[:20] in prompt  # base prompt is included


def test_accepted_risk_items_render_as_exclusions_not_checklist(service, project):
    # [SOLO-28] ACCEPTED-RISK items are adjudications: rendering them under "verify
    # each and report per item" would instruct the reviewer to report the very
    # findings the convention says not to re-raise. They get their own do-NOT-re-raise
    # section instead.
    service.add_review_memory("SOLO", "NORMAL-CHECK bounds on clamps", status="active")
    service.add_review_memory(
        "SOLO", "ACCEPTED-RISK: TOCTOU on claim snapshots", status="active"
    )
    prompt = service.assembled_review_prompt("SOLO")
    checklist, _, adjudicated = prompt.partition("Adjudicated risks")
    assert adjudicated, prompt  # the exclusion section exists
    assert "ACCEPTED-RISK: TOCTOU" in adjudicated
    assert "do NOT re-raise" in adjudicated
    assert "NORMAL-CHECK" in checklist  # ordinary items stay on the checklist
    assert "ACCEPTED-RISK" not in checklist  # and adjudications are NOT report-per-item


def test_record_hit_increments_active_item_hits(service, project):
    item = service.add_review_memory("SOLO", "x", status="active")
    service.assembled_review_prompt("SOLO", record_hit=True)
    after = service.list_review_memory("SOLO")[0]
    assert after["hits"] == 1
    # a plain (non-recording) assembly does not bump hits
    service.assembled_review_prompt("SOLO")
    assert service.list_review_memory("SOLO")[0]["hits"] == 1
