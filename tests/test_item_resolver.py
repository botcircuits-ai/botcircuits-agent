"""Unit tests for S4-exec — engine-side deterministic per-item fact gathering."""

from __future__ import annotations

import json
import stat
from pathlib import Path

from botcircuits.agent.workflow.engine.item_resolver import resolve_item_facts
from botcircuits.agent.workflow.engine.runner import _decide_list


def _pricer(tmp_path: Path):
    """A tiny stand-in for bin/price.py: prints the same JSON shape."""
    (tmp_path / "bin").mkdir(exist_ok=True)
    script = tmp_path / "bin" / "price.py"
    script.write_text(
        "import sys, json\n"
        "inv={'SKU-A':{'price':10,'stock':100},'SKU-B':{'price':9000,'stock':100}}\n"
        "sku=sys.argv[1]; qty=int(sys.argv[2])\n"
        "rec=inv.get(sku)\n"
        "print(json.dumps({'found':False}) if not rec else "
        "json.dumps({'found':True,'stock':rec['stock'],"
        "'line_total':rec['price']*qty}))\n"
    )
    script.chmod(script.stat().st_mode | stat.S_IEXEC)


def _step():
    return {
        "type": "listDecision",
        "itemSource": {"file": "data/order.json", "path": "items"},
        "itemFacts": {
            "kind": "exec",
            "command": ["python3", "bin/price.py", "{sku}", "{qty}"],
            "parse": "json",
            "derive": {
                "sku": {"from_item": "sku"},
                "sku_found": {"from_output": "found"},
                "line_total": {"from_output": "line_total", "default": 0},
                "stock_sufficient": {"ge": ["output.stock", "item.qty"]},
            },
        },
        "decisionKey": "decision",
        "emit": ["sku", "decision", "line_total"],
        "nullOn": {"line_total": ["reject"]},
        "choices": [
            {"operator": "AND", "expressionList": [
                {"variable": "sku_found", "operator": "is", "value": False}],
                "next": "reject"},
            {"operator": "AND", "expressionList": [
                {"variable": "stock_sufficient", "operator": "is", "value": False}],
                "next": "backorder"},
            {"operator": "AND", "expressionList": [
                {"variable": "line_total", "operator": "greater than", "value": 5000}],
                "next": "review"},
        ],
        "next": "fulfill",
    }


def test_engine_gathers_facts_and_decides_end_to_end(tmp_path: Path):
    _pricer(tmp_path)
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "order.json").write_text(json.dumps({"items": [
        {"sku": "SKU-A", "qty": 5},    # in stock, cheap -> fulfill
        {"sku": "SKU-B", "qty": 1},    # line_total 9000 > 5000 -> review
        {"sku": "SKU-A", "qty": 200},  # qty 200 > stock 100 -> backorder
        {"sku": "SKU-X", "qty": 1},    # unknown -> reject
    ]}))
    facts = resolve_item_facts(_step(), base_dir=tmp_path)
    assert facts is not None
    decided = _decide_list("wf", _step(), facts)
    assert [d["decision"] for d in decided] == \
        ["fulfill", "review", "backorder", "reject"]
    # reject line_total nulled
    assert decided[3]["line_total"] is None
    # line_total carried for non-reject
    assert decided[1]["line_total"] == 9000


def test_returns_none_without_itemfacts(tmp_path: Path):
    step = _step()
    del step["itemFacts"]
    assert resolve_item_facts(step, base_dir=tmp_path) is None


def test_missing_order_file_returns_none(tmp_path: Path):
    _pricer(tmp_path)
    assert resolve_item_facts(_step(), base_dir=tmp_path) is None
