"""The --json contract must be plain, valid JSON on stdout — never rich-highlighted.

Agents run the CLI inside a PTY (tmux); rich would emit ANSI color codes there,
producing bytes that are not valid JSON. These tests pin the plain-output contract.
"""

import json

from solopm.cli import output


def test_print_json_is_plain_and_parseable(capsys):
    obj = {"id": "SOLO-1", "n": 1, "x": None, "nested": {"a": [1, 2]}}
    output.print_json(obj)
    out = capsys.readouterr().out
    assert "\x1b[" not in out  # no ANSI escapes
    assert out == json.dumps(obj, indent=2, ensure_ascii=False) + "\n"
    assert json.loads(out) == obj


def test_print_error_json_is_plain_and_parseable(capsys):
    err = {"error": {"code": "not_found", "message": "Ticket 'SOLO-9' not found."}}
    output.print_error_json(err)
    out = capsys.readouterr().out
    assert "\x1b[" not in out
    assert json.loads(out) == err


def test_fmt_age_compact_units():
    """SOLO-13: time-in-state renders with m/h/d granularity (None/negative → dash)."""
    assert output.fmt_age(None) == "—"
    assert output.fmt_age(-5) == "—"
    assert output.fmt_age(0) == "just now"
    assert output.fmt_age(59) == "just now"
    assert output.fmt_age(60) == "1m"
    assert output.fmt_age(45 * 60) == "45m"
    assert output.fmt_age(3 * 3600) == "3h"
    assert output.fmt_age(50 * 3600) == "2d"
