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
