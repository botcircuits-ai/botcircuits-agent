# Agent evals — DeepEval Task Completion

Outcome-based evaluation of the BotCircuits agent loop using
[DeepEval's Task Completion metric](https://deepeval.com/docs/metrics-task-completion).

Task Completion is an **LLM-as-judge, reference-free** metric: it reads the
agent's *execution trace* (the LLM turns + tool calls + tool results captured
via `@observe`) and scores how well the final outcome aligns with the task. No
golden/expected output is required, which suits this agent's non-deterministic
multi-turn workflows.

## Layout

| File | Purpose |
|---|---|
| `instrument.py` | Wraps `Agent.chat` and `ToolRegistry.run` with DeepEval `@observe` spans so every run produces a trace the judge can read. Import it **before** building an Agent. |
| `harness.py` | `build_agent()` / `run_task()` — construct a real Agent (Anthropic by default) and run one task end to end, including driving a multi-turn workflow to completion within a single trace. |
| `tasks.py` | Seed tasks (`TASKS`), including one multi-turn workflow task. |
| `test_task_completion.py` | `deepeval test run` entry: Task Completion over every seed task + a Tool Correctness assertion that the workflow guardrail forces the expected tool. |

## Install

```bash
uv pip install -e ".[evals]"          # adds deepeval
# or: uv pip install deepeval
```

## Run

DeepEval's judge needs its own model key (defaults to OpenAI). The agent under
test needs its provider key. Both come from the project `.env`.

```bash
# Set the judge model (any OpenAI-compatible model deepeval supports)
export OPENAI_API_KEY=sk-...          # used by the TaskCompletionMetric judge

# Run the suite
.venv/bin/deepeval test run evals/test_task_completion.py
```

Or run a single task ad hoc and print the score + reasoning:

```bash
.venv/bin/python -m evals.harness "Add 17 and 25 and tell me the result."
```

## Notes / gotchas

- **One trace per task.** Multi-turn workflows span several `chat()` calls
  (each emits one `agentAction` then pauses). `run_task()` keeps driving the
  same `session_id` until the workflow's `session_id` clears, all inside one
  `@observe`-rooted trace, so the judge sees the whole run — not a single step.
- **Side effects.** The `workflow_demo` task writes `step_*.md` / `end.md`
  files. Tasks run in a temp cwd (see `harness.run_task(cwd=...)`) so they don't
  litter the repo.
- **Auto mode.** The harness runs with `auto=True` on gated tools (no human at
  the y/N prompt during a non-interactive eval).
- **Judge cost.** Each metric call is an extra LLM round-trip. Keep `TASKS`
  small for CI; expand locally.
