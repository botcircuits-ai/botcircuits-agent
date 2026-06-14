"""S4-exec — deterministic per-item fact gathering (no LLM).

The listDecision primitive (S3) already makes the per-item DECISION deterministic
(`evaluate_choices` per item). But the per-item FACTS were still gathered by the
model (read the order, run the pricer, report sku/stock/line_total) — and a model
occasionally misreports a fact (wrong SKU, garbled list), which is the residual
accuracy gap.

This module lets the ENGINE gather those facts in code: read the item list from a
file, run a deterministic command (e.g. the pricer script) per item, parse its
output, and derive each fact field with small declarative rules. When a
listDecision step carries both `itemSource` and `itemFacts`, the engine skips the
LLM entirely for that segment — the whole order is processed with zero LLM calls,
fully deterministically.

Step fields consumed (all optional; absent → fall back to the model/Tier-1 path):

    "itemSource": {"file": "data/current_order.json", "path": "items"}
        Where the list of input items comes from (each an object, e.g.
        {"sku": ..., "qty": ...}).

    "itemFacts": {
        "kind": "exec",
        "command": ["python3", "bin/price.py", "{sku}", "{qty}"],
        "parse": "json",                       # parse stdout as JSON
        "derive": {
            "sku":              {"from_item": "sku"},
            "sku_found":        {"from_output": "found"},
            "line_total":       {"from_output": "line_total", "default": 0},
            "stock_sufficient": {"ge": ["output.stock", "item.qty"]}
        }
    }

`derive` value rules (deterministic, tiny by design):
    {"from_item": "k"}             — the item's field k
    {"from_output": "k", "default": d} — the parsed command output's field k
    {"literal": v}                 — a constant
    {"ge": [a, b]}                 — a >= b, where a/b are "item.x" / "output.y"
                                     refs or literals (numeric compare)

Execution is sandboxed to the run cwd and best-effort: a command failure or
parse error yields `found=false`-style facts for that item (engine still decides
deterministically), and any structural problem returns None so the caller falls
back to the model path. Never raises into the run.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

_EXEC_TIMEOUT_S = 30


def _read_items(spec: dict, base: Path) -> list[dict] | None:
    src = spec.get("itemSource")
    if not isinstance(src, dict):
        return None
    f = src.get("file")
    if not isinstance(f, str):
        return None
    try:
        data = json.loads((base / f).read_text())
    except Exception:
        return None
    path = src.get("path")
    cur = data
    if path:
        for part in str(path).split("."):
            if isinstance(cur, dict):
                cur = cur.get(part)
            else:
                return None
    return cur if isinstance(cur, list) else None


def _fmt(token: str, item: dict) -> str:
    """Interpolate `{field}` from the item into a command token."""
    out = token
    for k, v in item.items():
        out = out.replace("{" + k + "}", str(v))
    return out


def _ref(ref: Any, item: dict, output: dict) -> Any:
    """Resolve an "item.x" / "output.y" reference, or pass a literal through."""
    if isinstance(ref, str) and ref.startswith("item."):
        return item.get(ref[len("item."):])
    if isinstance(ref, str) and ref.startswith("output."):
        return output.get(ref[len("output."):])
    return ref


def _derive_field(rule: dict, item: dict, output: dict) -> Any:
    if "from_item" in rule:
        return item.get(rule["from_item"], rule.get("default"))
    if "from_output" in rule:
        val = output.get(rule["from_output"])
        return val if val is not None else rule.get("default")
    if "literal" in rule:
        return rule["literal"]
    if "ge" in rule:
        a, b = rule["ge"]
        try:
            return float(_ref(a, item, output)) >= float(_ref(b, item, output))
        except (TypeError, ValueError):
            return False
    return None


def resolve_item_facts(
    step: dict,
    *,
    base_dir: Path,
) -> list[dict] | None:
    """Deterministically gather the per-item fact list for a listDecision step.

    Returns one fact dict per input item (ready for `_decide_list`), or None
    when the step doesn't declare `itemSource`+`itemFacts` (caller then runs the
    model path). Never raises."""
    facts_spec = step.get("itemFacts")
    if not isinstance(facts_spec, dict) or facts_spec.get("kind") != "exec":
        return None
    items = _read_items(step, base_dir)
    if items is None:
        return None
    command = facts_spec.get("command")
    derive = facts_spec.get("derive")
    if not isinstance(command, list) or not isinstance(derive, dict):
        return None

    out_facts: list[dict] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        argv = [_fmt(tok, item) for tok in command]
        output: dict = {}
        try:
            proc = subprocess.run(
                argv, cwd=str(base_dir), capture_output=True, text=True,
                timeout=_EXEC_TIMEOUT_S,
            )
            if facts_spec.get("parse") == "json" and proc.stdout.strip():
                parsed = json.loads(proc.stdout)
                if isinstance(parsed, dict):
                    output = parsed
        except Exception:
            output = {}
        fact = {name: _derive_field(rule, item, output)
                for name, rule in derive.items()
                if isinstance(rule, dict)}
        out_facts.append(fact)
    return out_facts
