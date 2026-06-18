"""CLI entry point for the workflow-running SKILL.

Drives a built workflow through the deterministic engine, using the selected
agent runtime (claude-code, native, …) for action steps and non-deterministic
slot resolution. Prints a single JSON object describing the outcome so the
host agent can react programmatically.

Usage:
    python -m botcircuits.runtime.run_workflow --name <wf> \\
        [--initial-args '{"k": "v"}'] [--runtime claude-code] [--reply "..."]

Pause/resume across PROCESSES: the engine pauses on a `question` step and
yields a resume cursor + accumulated slots. A single process can't hold that
in memory between a question and the user's answer, so we persist it to
`.botcircuits/workflows/.runs/<name>.json`. Passing `--reply` on the next
invocation loads that state, seeds the reply as the freshest user context,
and continues from the same segment. On completion the state file is removed.

Output (stdout, one JSON object):
    {"status": "done",   "summary": "...", "slots": {...}}
    {"status": "paused", "question": "...", "name": "<wf>"}
    {"status": "error",  "error": "..."}
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path
from typing import Any

from botcircuits.runtime.detect import (
    NATIVE,
    detect_runtime_name as _detect,
    select_runtime,
)
from botcircuits.agent.workflow.engine.runner import run_workflow_engine
from botcircuits.agent.workflow.local import (
    LocalWorkflowError,
    _load_workflow_record,
    _resolve_workflows_dir,
)


_RUNS_DIR_NAME = ".runs"


def _runs_dir() -> Path:
    return _resolve_workflows_dir() / _RUNS_DIR_NAME


def _state_path(name: str) -> Path:
    return _runs_dir() / f"{name}.json"


def _load_state(name: str) -> dict:
    p = _state_path(name)
    if not p.is_file():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def _save_state(name: str, state: dict) -> None:
    p = _state_path(name)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(state), encoding="utf-8")


def _clear_state(name: str) -> None:
    try:
        _state_path(name).unlink()
    except OSError:
        pass


async def _run(
    name: str,
    *,
    initial_args: dict,
    runtime_name: str | None,
    reply: str | None,
) -> dict:
    record = _load_workflow_record(name)
    flow = record.get("flow")
    if not isinstance(flow, dict):
        raise LocalWorkflowError(f"workflow {name!r} is missing flow")

    # Select the runtime. The runner is for EXTERNAL/CLI hosts; `native` here
    # would need a live Agent we don't build in this entry point, so reject it
    # with a clear message (use the in-process CLI / agent loop for native).
    resolved_name = runtime_name or _detect()
    if resolved_name == NATIVE:
        raise LocalWorkflowError(
            "the native runtime has no standalone runner; run the workflow "
            "through the BotCircuits agent (botcircuits) instead, or pass "
            "--runtime claude-code."
        )
    provider = select_runtime(settings=None, name=resolved_name)

    # Resume from a persisted pause if this is a --reply continuation.
    saved = _load_state(name) if reply is not None else {}
    resume_step = saved.get("engine_paused_step")
    slots: dict[str, Any] = dict(saved.get("engine_slots") or {})
    if resume_step is None:
        slots.update({k: v for k, v in initial_args.items() if v not in (None, "")})
    if reply:
        slots["__last_user_message__"] = reply

    try:
        result = await run_workflow_engine(
            flow,
            workflow_name=name,
            run_segment=lambda **kw: provider.run_segment(**kw),
            start_step_id=resume_step,
            slots=slots,
            resolve_unfilled=lambda **kw: provider.resolve_slots(**kw),
        )
    finally:
        await provider.aclose()

    if result.paused:
        _save_state(name, {
            "engine_paused_step": result.paused_step or resume_step,
            "engine_slots": result.slots,
        })
        return {"status": "paused", "question": result.question, "name": name}

    _clear_state(name)
    clean_slots = {
        k: v for k, v in (result.slots or {}).items()
        if not k.startswith("__")
    }
    return {"status": "done", "summary": result.summary, "slots": clean_slots}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="botcircuits.runtime.run_workflow")
    parser.add_argument("--name", required=True, help="built workflow name")
    parser.add_argument("--initial-args", default="",
                        help="JSON object of initial slot values")
    parser.add_argument("--runtime", default=None,
                        help="force a runtime (claude-code, codex, …); "
                             "default = auto-detect")
    parser.add_argument("--reply", default=None,
                        help="user's answer to a prior pause; resumes the run")
    args = parser.parse_args(argv)

    initial_args: dict = {}
    if args.initial_args.strip():
        try:
            parsed = json.loads(args.initial_args)
        except json.JSONDecodeError as e:
            print(json.dumps({"status": "error",
                              "error": f"--initial-args not valid JSON: {e}"}))
            return 2
        if not isinstance(parsed, dict):
            print(json.dumps({"status": "error",
                              "error": "--initial-args must be a JSON object"}))
            return 2
        initial_args = parsed

    try:
        out = asyncio.run(_run(
            args.name,
            initial_args=initial_args,
            runtime_name=args.runtime,
            reply=args.reply,
        ))
    except LocalWorkflowError as e:
        print(json.dumps({"status": "error", "error": str(e)}))
        return 1
    except Exception as e:  # pragma: no cover - defensive top-level guard
        print(json.dumps({"status": "error",
                          "error": f"{type(e).__name__}: {e}"}))
        return 1

    print(json.dumps(out, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
