"""MCP channel watcher — push SoloPM events into a Claude session (SOLO-15).

These cover the testable core (event detection / filtering / formatting + the
notification model). The live stdio/channel delivery is verified end-to-end with
`claude --dangerously-load-development-channels server:solopm`.
"""

from solopm.mcp.channel import ChannelNotification, ChannelWatcher


def _watcher(service, agent="claude", scope="mine"):
    w = ChannelWatcher(service, agent=agent, scope=scope)
    w.prime()  # establish the baseline so existing history isn't replayed
    return w


def test_notification_model_dumps_channel_method():
    n = ChannelNotification(params={"content": "hi", "meta": {"a": "b"}})
    d = n.model_dump()
    assert d["method"] == "notifications/claude/channel"
    assert d["params"] == {"content": "hi", "meta": {"a": "b"}}


def test_emits_foreign_change_on_my_ticket(service, project):
    t = service.create_ticket(project="SOLO", title="x", assignee="claude")
    w = _watcher(service)
    service.move_ticket(t.id, "in-progress", actor="codex")  # someone else moved it
    events = w.poll()
    ev = next(e for e in events if e["meta"]["ticket_id"] == t.id)
    assert ev["meta"]["kind"] == "state_change"
    assert ev["meta"]["actor"] == "codex"
    assert ev["meta"]["to_state"] == "in-progress"
    assert t.id in ev["content"]


def test_ignores_my_own_actions(service, project):
    t = service.create_ticket(project="SOLO", title="x", assignee="claude")
    w = _watcher(service)
    service.move_ticket(t.id, "in-progress", actor="claude")  # the agent itself
    assert w.poll() == []


def test_scope_mine_filters_other_tickets(service, project):
    mine = service.create_ticket(project="SOLO", title="mine", assignee="claude")
    other = service.create_ticket(project="SOLO", title="other", assignee="codex")
    w = _watcher(service, scope="mine")
    service.comment_ticket(other.id, body="hi", actor="human")
    service.comment_ticket(mine.id, body="yo", actor="human")
    tids = {e["meta"]["ticket_id"] for e in w.poll()}
    assert mine.id in tids
    assert other.id not in tids


def test_scope_all_includes_other_tickets(service, project):
    other = service.create_ticket(project="SOLO", title="other", assignee="codex")
    w = _watcher(service, scope="all")
    service.comment_ticket(other.id, body="hi", actor="human")
    assert any(e["meta"]["ticket_id"] == other.id for e in w.poll())


def test_baseline_does_not_replay_history(service, project):
    t = service.create_ticket(project="SOLO", title="x", assignee="claude")
    service.comment_ticket(t.id, body="old", actor="human")  # before priming
    w = _watcher(service)
    assert w.poll() == []


def test_meta_keys_are_channel_safe_identifiers(service, project):
    t = service.create_ticket(project="SOLO", title="x", assignee="claude")
    w = _watcher(service)
    service.comment_ticket(t.id, body="note", actor="human")
    service.move_ticket(t.id, "in-progress", actor="codex")
    events = w.poll()
    assert events
    for e in events:
        for k in e["meta"]:
            assert k and all(c.isalnum() or c == "_" for c in k), k
        assert all(isinstance(v, str) for v in e["meta"].values())


def test_comment_event_is_summarized(service, project):
    t = service.create_ticket(project="SOLO", title="x", assignee="claude")
    w = _watcher(service)
    service.comment_ticket(t.id, body="please fix the thing", actor="codex")
    ev = next(e for e in w.poll() if e["meta"]["ticket_id"] == t.id)
    assert ev["meta"]["kind"] == "comment"
    assert "codex" in ev["content"]
