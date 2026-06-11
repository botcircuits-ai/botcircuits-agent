"""Workflow subpackage — load on-disk workflows and expose them as tools.

Workflows are loaded from a local directory (`$BOTCIRCUITS_WORKFLOWS_DIR`
or `.botcircuits/workflows`) and driven by the embedded flow engine in
`engine/`.

Public surface:

    from botcircuits.agent.workflow import fetch_workflows, run_workflow
"""

from __future__ import annotations

from botcircuits.agent.tools import LocalTool, ToolRegistry
from botcircuits.providers.base import LLMProvider
from botcircuits.agent.workflow import local
from botcircuits.agent.workflow.cli_commands import (
    compose_workflow_empty_action,
    compose_workflow_step_directive,
)
from botcircuits.agent.workflow.local import LocalWorkflowError


async def fetch_workflows() -> list[dict]:
    """Return the workflow records discovered on disk.

    Each record has at least: `id`, `name`, `description`, plus the full
    flow definition under `flow`.
    """
    return await local.fetch_workflows()


async def run_workflow(
    workflow_name: str,
    args: dict,
    *,
    session_id: str | None = None,
    provider: LLMProvider | None = None,
    last_assistant_message: str = "",
    last_user_message: str = "",
    normalize_enabled: bool = True,
) -> dict:
    """Execute a workflow by name and return its result.

    `provider` + `last_assistant_message` + `last_user_message` are
    forwarded to the LLM-side variable normalizer (Layer B). They are
    optional; when omitted, normalization falls back to deterministic
    type coercion only.
    """
    return await local.run_workflow(
        workflow_name, args,
        session_id=session_id,
        provider=provider,
        last_assistant_message=last_assistant_message,
        last_user_message=last_user_message,
        normalize_enabled=normalize_enabled,
    )


_EMPTY_SCHEMA: dict = {"type": "object", "properties": {}}

# flow.variables dataType → JSON-schema type for the tool's input_schema.
_JSON_TYPE_BY_DATATYPE = {"number": "number", "boolean": "boolean"}


def _branch_input_schema(branch_variables: list[dict]) -> dict:
    """Build the tool input_schema exposing a pending branch's variables.

    The model sees these as ordinary optional tool arguments — passing
    them on the re-call is how the slots ride the main loop's tool call
    instead of being re-derived from the transcript inside the tool.
    """
    properties: dict[str, dict] = {}
    for v in branch_variables:
        name = v.get("variableName")
        if not isinstance(name, str) or not name:
            continue
        dtype = (v.get("dataType") or "string").lower()
        prop: dict = {"type": _JSON_TYPE_BY_DATATYPE.get(dtype, "string")}
        desc = v.get("description")
        if isinstance(desc, str) and desc:
            prop["description"] = desc
        properties[name] = prop
    if not properties:
        return dict(_EMPTY_SCHEMA)
    return {"type": "object", "properties": properties}


def workflow_tool(
    record: dict,
    *,
    provider: LLMProvider | None = None,
    normalize_enabled: bool = True,
) -> LocalTool:
    """Wrap a workflow record into a `LocalTool` the agent can call.

    A workflow is multi-turn: one call returns one `AGENT_ACTION` for the
    LLM to act on, and the engine keeps the saved session in memory keyed
    by `session_id` until the workflow ends. We keep that session_id in
    the tool's closure so the next invocation re-enters the same
    workflow conversation instead of starting a new one.

    When the engine pauses on a *branching* step, the result carries the
    filtered variable schema the branch references (`branch_variables`).
    The handler then (a) widens the tool's `input_schema` to expose those
    variables and (b) appends a directive line asking the model to
    re-call the tool with the observed values — so the slots arrive as
    plain tool-call args (the slot resolver's highest-priority source)
    instead of being re-derived from a transcript snapshot. The agent
    loop's empty-args auto-recall remains as the fallback when the model
    doesn't re-call.

    `provider` enables Layer B (LLM-driven variable normalization) on
    workflow re-entry. The tool's handler accepts an optional `context`
    dict (filled by the agent loop) carrying `last_assistant_message`
    and `last_user_message`, which Layer B uses as part of its
    hallucination-guard source.
    """
    wf_name = record["name"]
    wf_desc = record.get("description") or f"Run workflow {wf_name}."

    state: dict[str, object] = {"session_id": None, "branch_variables": []}

    async def _handler(args: dict, context: dict | None = None) -> str:
        ctx = context or {}
        result = await run_workflow(
            wf_name, args,
            session_id=state["session_id"],
            provider=provider,
            last_assistant_message=ctx.get("last_assistant_message", ""),
            last_user_message=ctx.get("last_user_message", ""),
            normalize_enabled=normalize_enabled,
        )
        action = result.get("action")
        done = bool(result.get("done"))
        kind = result.get("kind")
        branch_variables = result.get("branch_variables") or []

        # Reset the closure's session_id as soon as the workflow finishes
        # so the next user request starts a fresh run. We do this even
        # when `action` is set because the engine returns `done=True`
        # together with the final state's action payload.
        if done:
            state["session_id"] = None
            branch_variables = []
        else:
            state["session_id"] = result.get("session_id")

        # Track the pending branch's variables and mirror them onto the
        # tool's input_schema so the next provider call advertises them.
        # `tool` is the LocalTool constructed below; the closure cell
        # resolves at call time, after construction.
        state["branch_variables"] = branch_variables
        tool.input_schema = _branch_input_schema(branch_variables)

        # No action and not done shouldn't happen with a well-formed STM,
        # but guard against it so the LLM gets a clear signal instead of
        # an empty string.
        if not action:
            return compose_workflow_empty_action(wf_name)

        # Frame the action as a directive, not a status update — the LLM
        # has to perform it (tool call, human_feedback question, message,
        # skill, etc.) before the workflow can advance. A `question`-kind
        # step forces a `human_feedback` call (which pauses the loop);
        # a branching step asks the model to re-call this tool with the
        # branch variables once the step is done; anything else
        # auto-advances via the agent loop's recall. Wording is shared
        # with the out-of-process tool wrapper (Hermes) via cli_commands.
        directive = compose_workflow_step_directive(
            wf_name, done=done, kind=kind,
            branch_variables=branch_variables,
        )
        return directive.as_plain_text(action)

    tool = LocalTool(
        name=wf_name,
        description=wf_desc,
        input_schema=dict(_EMPTY_SCHEMA),
        handler=_handler,
    )
    # Expose the session state so the agent loop can detect that this
    # workflow is mid-execution and remind the model to re-enter it.
    tool._workflow_state = state  # type: ignore[attr-defined]
    return tool


def workflow_branch_variables(reg: ToolRegistry, name: str) -> list[dict]:
    """The pending branch's variable schema for the named workflow tool.

    Non-empty only while the workflow is paused on a branching step.
    The agent loop reads this to tell the model, via the `[Active
    workflow]` reminder, to re-call the tool with those values once the
    current step is done.
    """
    for tool in reg.all():
        if tool.name != name:
            continue
        state = getattr(tool, "_workflow_state", None)
        if isinstance(state, dict):
            variables = state.get("branch_variables")
            if isinstance(variables, list):
                return variables
    return []


def active_workflow_names(reg: ToolRegistry) -> list[str]:
    """Names of workflow tools on `reg` that have a live session_id.

    A workflow tool is "active" between its first call (which returns a
    pending step) and the call that finishes the workflow. The agent
    loop reads this to inject a per-turn reminder pushing the model to
    re-enter the workflow.
    """
    active: list[str] = []
    for tool in reg.all():
        state = getattr(tool, "_workflow_state", None)
        if isinstance(state, dict) and state.get("session_id"):
            active.append(tool.name)
    return active


async def register_workflows(
    reg: ToolRegistry,
    *,
    provider: LLMProvider | None = None,
    normalize_enabled: bool = True,
) -> tuple[list[str], list[str]]:
    """Discover workflows on disk and register each as a LocalTool on `reg`.

    Built-in tools take precedence: a workflow whose name collides with
    an already-registered tool is skipped (not registered) so user-defined
    workflows can never override built-ins.

    Pass `provider` (typically the same one the agent's `LLMProvider` is
    built from) to enable Layer B variable normalization on workflow
    re-entry. Set `normalize_enabled=False` to register the tools but
    skip B even when a provider is available.

    Returns `(registered, skipped)` — both lists of workflow names.
    Names already bound to a non-workflow tool (a builtin or MCP tool)
    are reported as `skipped`. Names already bound to an EARLIER
    workflow tool are re-registered: this is what lets the CLI re-run
    `register_workflows` after the user edits a workflow on disk and
    have the agent pick up the new description / state map on the
    very next turn.
    """
    records = await fetch_workflows()
    registered: list[str] = []
    skipped: list[str] = []
    for record in records:
        tool = workflow_tool(
            record,
            provider=provider,
            normalize_enabled=normalize_enabled,
        )
        if reg.has(tool.name):
            existing = next(
                (t for t in reg.all() if t.name == tool.name), None,
            )
            # Only skip when colliding with a non-workflow tool (i.e. a
            # builtin or MCP tool). Workflow tools are tagged with
            # `_workflow_state` by `workflow_tool()`; overwriting one
            # with the freshly-loaded record is the whole point of
            # re-running this function.
            if existing is None or getattr(
                existing, "_workflow_state", None,
            ) is None:
                skipped.append(tool.name)
                continue
        reg.register(tool)
        registered.append(tool.name)
    return registered, skipped


__all__ = [
    "LocalWorkflowError",
    "fetch_workflows",
    "run_workflow",
    "workflow_tool",
    "active_workflow_names",
    "workflow_branch_variables",
    "register_workflows",
]
