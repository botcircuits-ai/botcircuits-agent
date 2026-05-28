"""Condition evaluation for agentAction branching.

There is no `choice` step type. Conditions are authored on `agentAction`
steps and evaluated on re-entry (after the LLM has had a chance to fill
variables). The executor calls `evaluate_choices` with the step's
`choices` list and the current session context, and uses the returned
next-step id to override the static `next`.

When no choice matches, the evaluator returns the supplied fallback
rather than calling an LLM — the engine itself is deterministic; the
surrounding agent loop is where any LLM-driven recovery happens.
"""

from __future__ import annotations

from botcircuits.agent.workflow.engine.utils import coerce_for_compare, fill_text_with_slots


def evaluate_choices(
    choices: list[dict],
    message: dict,
    default_next: str | None,
) -> str | None:
    """Walk `choices` in order; return the `next` of the first match, or
    `default_next` if none match (or `None` to end the workflow).
    """
    for choice in choices or []:
        if _evaluate_choice(choice, message):
            return choice.get("next")
    return default_next


def _evaluate_choice(choice: dict, message: dict) -> bool:
    operator = choice.get("operator")
    expressions = choice.get("expressionList", [])
    if operator == "OR":
        return any(_evaluate_condition(c, message) for c in expressions)
    if operator == "AND":
        return all(_evaluate_condition(c, message) for c in expressions)
    return False


def _evaluate_condition(condition: dict, message: dict) -> bool:
    if "variable" not in condition:
        return False

    session_context = message["data"]["sessionContext"]
    variable = condition["variable"]
    if variable == "{sys_input_text}":
        variable_value: object = message.get("inputText", "")
    elif variable == "{sys_channel}":
        variable_value = message.get("channel", "")
    else:
        variable_value = (session_context.get("slots") or {}).get(variable, "")

    variable_value = coerce_for_compare(variable_value)
    return _evaluate_operator(condition, variable_value, session_context)


def _evaluate_operator(condition: dict, variable_value, session_context: dict) -> bool:
    operator = condition.get("operator")
    raw_value = condition.get("value", "")
    # Only string `value`s carry slot placeholders; typed values from the
    # indexer (numbers, booleans) flow through unchanged.
    check_value = (
        fill_text_with_slots(raw_value, session_context)
        if isinstance(raw_value, str) else raw_value
    )
    # Coerce when comparing typed variable values against string literals
    # (`order_total > '500'`) so the indexer's typed output and authored
    # string-typed values both work.
    if (operator in ("greater than", "greater than or equal",
                     "less than", "less than or equal")
            and isinstance(check_value, str)
            and not isinstance(variable_value, str)):
        try:
            check_value = type(variable_value)(check_value)
        except (TypeError, ValueError):
            pass

    if operator == "is":
        return check_value == variable_value
    if operator == "is not":
        return check_value != variable_value
    if operator == "greater than":
        return variable_value > check_value
    if operator == "greater than or equal":
        return variable_value >= check_value
    if operator == "less than":
        return variable_value < check_value
    if operator == "less than or equal":
        return variable_value <= check_value
    if operator == "contains":
        return isinstance(variable_value, str) and check_value in variable_value
    if operator == "not contains":
        return isinstance(variable_value, str) and check_value not in variable_value
    if operator == "starts with":
        return isinstance(variable_value, str) and variable_value.startswith(check_value)
    if operator == "ends with":
        return isinstance(variable_value, str) and variable_value.endswith(check_value)
    if operator == "is empty":
        return variable_value == ""
    if operator == "is not empty":
        return variable_value != ""
    return False
