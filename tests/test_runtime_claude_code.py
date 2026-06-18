"""CLI runtime provider end-to-end against a FAKE `claude` script.

The fake binary echoes a canned JSON object on stdout, so we exercise the
real subprocess path (`cli_exec.run_cli`) + parsing without a live agent.
Async entry points are driven with `asyncio.run`, matching the suite's
convention (see tests/test_workflow_engine_runner.py).
"""

import asyncio
import stat
import textwrap

from botcircuits.runtime.base import RuntimeConfig
from botcircuits.runtime.providers.claude_code import ClaudeCodeRuntime


def _write_fake_cli(tmp_path, stdout_text: str, *, rc: int = 0):
    """Create an executable that prints `stdout_text` and exits with `rc`."""
    script = tmp_path / "fakeclaude"
    script.write_text(textwrap.dedent(f"""\
        #!/usr/bin/env python3
        import sys
        sys.stdout.write({stdout_text!r})
        sys.exit({rc})
    """))
    script.chmod(script.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    return script


def _runtime(script):
    return ClaudeCodeRuntime(RuntimeConfig(
        name="claude-code",
        command=[str(script), "-p", "{prompt}", "--output-format", "json"],
        timeout=30.0,
    ))


def test_run_segment_captures_slots(tmp_path):
    script = _write_fake_cli(tmp_path, '{"slots": {"approved": true}, "text": "ok"}')
    rt = _runtime(script)
    res = asyncio.run(rt.run_segment(
        actions=["Decide the application"],
        branch_variables=[{"variableName": "approved", "dataType": "boolean"}],
        system_notes=[],
        slots={},
    ))
    assert res.captured_slots == {"approved": True}
    assert res.paused is False


def test_run_segment_pause(tmp_path):
    script = _write_fake_cli(tmp_path, '{"paused": true, "question": "Income?"}')
    rt = _runtime(script)
    res = asyncio.run(rt.run_segment(
        actions=["Ask for income"], branch_variables=[], system_notes=[], slots={},
    ))
    assert res.paused is True
    assert res.question == "Income?"


def test_run_segment_list_items(tmp_path):
    script = _write_fake_cli(
        tmp_path, '{"items": [{"sku": "A", "qty": 2}, {"sku": "B", "qty": 1}]}'
    )
    rt = _runtime(script)
    res = asyncio.run(rt.run_segment(
        actions=["Price each line"], branch_variables=[], system_notes=[],
        slots={}, item_variables=[{"variableName": "sku", "dataType": "string"}],
    ))
    assert res.captured_items == [{"sku": "A", "qty": 2}, {"sku": "B", "qty": 1}]


def test_missing_binary_pauses_not_crashes(tmp_path):
    rt = ClaudeCodeRuntime(RuntimeConfig(
        name="claude-code",
        command=[str(tmp_path / "does-not-exist"), "{prompt}"],
    ))
    res = asyncio.run(rt.run_segment(
        actions=["x"], branch_variables=[], system_notes=[], slots={},
    ))
    assert res.paused is True
    assert "could not be started" in res.question


def test_resolve_slots_tier0_deterministic_beats_cli(tmp_path):
    # Tier-0 should resolve a number from the last user message WITHOUT
    # invoking the CLI. The fake CLI would return 999; Tier-0 returns 42, and
    # because nothing is left unresolved the CLI is never consulted.
    script = _write_fake_cli(tmp_path, '{"normalized": {"amount": 999}}')
    rt = _runtime(script)
    flow = {
        "steps": {
            "s1": {
                "type": "question",
                "choices": [{
                    "expressionList": [
                        {"variable": "amount", "operator": "greater than", "value": "100"}
                    ],
                }],
            }
        }
    }
    out = asyncio.run(rt.resolve_slots(
        flow=flow, step_id="s1",
        variables=[{"variableName": "amount", "dataType": "number"}],
        slots={"__last_user_message__": "my amount is 42"},
    ))
    assert out == {"amount": 42}
