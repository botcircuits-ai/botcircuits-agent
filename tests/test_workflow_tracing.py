"""Tests for the per-session workflow trace writer."""

from __future__ import annotations

import json

import pytest

from botcircuits.agent.workflow.tracing import EventType, SessionTrace, new_session_id


@pytest.fixture
def sessions_env(tmp_path, monkeypatch):
    # Point the workflows dir at a temp location; sessions land alongside it.
    monkeypatch.setenv("BOTCIRCUITS_WORKFLOWS_DIR", str(tmp_path / "workflows"))
    return tmp_path


def test_start_writes_session_file_with_schema(sessions_env):
    t = SessionTrace.start(
        workflow_name="order_fulfillment",
        runtime="claude-code",
        initial_slots={"order_id": "X1", "__internal__": "hidden"},
    )
    path = SessionTrace.path_for(t.session_id)
    assert path.is_file()

    doc = json.loads(path.read_text())
    assert doc["session_id"] == t.session_id
    assert doc["agent"]["runtime"] == "claude-code"
    assert doc["workflow"]["name"] == "order_fulfillment"
    assert doc["workflow"]["start"]
    assert doc["workflow"]["end"] is None
    # Engine-internal slot keys are stripped from the snapshot.
    assert doc["workflow"]["initial_slots"] == {"order_id": "X1"}
    # The first event is session_start.
    assert doc["trace"][0]["type"] == EventType.SESSION_START
    assert "memory" in doc and "nodes" in doc["memory"]


def test_events_append_with_seq_and_slot_snapshots(sessions_env):
    t = SessionTrace.start(
        workflow_name="wf", runtime="self", initial_slots={},
    )
    t.event(EventType.STEP_ENTER, step="check_stock", slots={"order_id": "A"})
    t.event(
        EventType.ACTION_AFTER, step="check_stock",
        slots={"order_id": "A", "all_items_in_stock": True},
        duration_ms=12.5,
        data={"output": {"text": "checked"}},
    )

    doc = json.loads(SessionTrace.path_for(t.session_id).read_text())
    types = [e["type"] for e in doc["trace"]]
    assert types == [
        EventType.SESSION_START, EventType.STEP_ENTER, EventType.ACTION_AFTER,
    ]
    # seq is monotonic and matches index.
    assert [e["seq"] for e in doc["trace"]] == [0, 1, 2]
    after = doc["trace"][2]
    assert after["duration_ms"] == 12.5
    assert after["slots"]["all_items_in_stock"] is True
    assert after["data"]["output"]["text"] == "checked"


def test_resume_reopens_same_session_file(sessions_env):
    t = SessionTrace.start(workflow_name="wf", runtime="self", initial_slots={})
    sid = t.session_id
    t.event(EventType.STEP_ENTER, step="s1")

    reopened = SessionTrace.load(sid)
    assert reopened is not None
    assert reopened.session_id == sid
    reopened.event(EventType.STEP_ENTER, step="s2")

    doc = json.loads(SessionTrace.path_for(sid).read_text())
    steps = [e.get("step") for e in doc["trace"] if e["type"] == EventType.STEP_ENTER]
    assert steps == ["s1", "s2"]


def test_end_stamps_workflow_end_and_session_end(sessions_env):
    t = SessionTrace.start(workflow_name="wf", runtime="self", initial_slots={})
    t.end(status="done", summary="all good", slots={"k": "v"})

    doc = json.loads(SessionTrace.path_for(t.session_id).read_text())
    assert doc["workflow"]["end"]
    last = doc["trace"][-1]
    assert last["type"] == EventType.SESSION_END
    assert last["data"]["status"] == "done"
    assert last["data"]["summary"] == "all good"


def test_memory_graph_nodes_and_edges(sessions_env):
    t = SessionTrace.start(workflow_name="wf", runtime="self", initial_slots={})
    t.add_memory_node("step:check", kind="step", label="check")
    t.add_memory_node("slot:in_stock", kind="slot", label="in_stock", value=True)
    t.add_memory_edge("step:check", "slot:in_stock", kind="produces")
    # Updating an existing node merges, not duplicates.
    t.add_memory_node("slot:in_stock", kind="slot", label="in_stock", value=False)

    doc = json.loads(SessionTrace.path_for(t.session_id).read_text())
    nodes = {n["id"]: n for n in doc["memory"]["nodes"]}
    assert set(nodes) == {"step:check", "slot:in_stock"}
    assert nodes["slot:in_stock"]["value"] is False
    assert doc["memory"]["edges"] == [
        {"from": "step:check", "to": "slot:in_stock", "kind": "produces"}
    ]


def test_non_json_slot_values_are_stringified(sessions_env):
    class Weird:
        def __repr__(self):
            return "<weird>"

    t = SessionTrace.start(
        workflow_name="wf", runtime="self", initial_slots={"obj": Weird()},
    )
    doc = json.loads(SessionTrace.path_for(t.session_id).read_text())
    assert doc["workflow"]["initial_slots"]["obj"] == "<weird>"


def test_new_session_id_unique():
    assert new_session_id() != new_session_id()


def test_flow_graph_snapshot_captures_branch_topology(sessions_env):
    # A flow with a branching agentAction (conditions at step root) like the
    # built order_fulfillment workflow.
    flow = {
        "start": "start",
        "steps": {
            "start": {"type": "start", "next": "check_stock"},
            "check_stock": {
                "type": "agentAction",
                "settings": {"action": "Check stock."},
                "next": "backorder",
                "conditions": [
                    {"condition": "all items are in stock", "next": "ship"}
                ],
                "choices": [{"next": "ship", "expCondition": "in_stock is true"}],
            },
            "ship": {"type": "agentAction", "settings": {"action": "Ship."}},
            "backorder": {"type": "agentAction", "settings": {"action": "Backorder."}},
        },
    }
    t = SessionTrace.start(
        workflow_name="order_fulfillment",
        runtime="claude-code",
        initial_slots={},
        flow=flow,
    )
    doc = json.loads(SessionTrace.path_for(t.session_id).read_text())
    g = doc["workflow"]["graph"]
    assert g["start"] == "start"
    cs = g["steps"]["check_stock"]
    # Default ("otherwise") path:
    assert cs["next"] == "backorder"
    # Conditional path with its authored NL condition:
    assert cs["choices"] == [{"condition": "all items are in stock", "next": "ship"}]
    # Both branch targets exist as steps so the graph can draw them.
    assert {"ship", "backorder"} <= set(g["steps"])


def test_flow_graph_falls_back_to_choices_when_no_conditions(sessions_env):
    flow = {
        "start": "a",
        "steps": {
            "a": {
                "type": "agentAction",
                "settings": {"action": "do"},
                "next": "c",
                "choices": [{"next": "b", "expCondition": "x is 1"}],
            },
            "b": {"type": "agentAction"},
            "c": {"type": "agentAction"},
        },
    }
    t = SessionTrace.start(workflow_name="wf", runtime="self", initial_slots={}, flow=flow)
    doc = json.loads(SessionTrace.path_for(t.session_id).read_text())
    a = doc["workflow"]["graph"]["steps"]["a"]
    assert a["choices"] == [{"condition": "x is 1", "next": "b"}]


def test_flow_graph_empty_without_flow(sessions_env):
    t = SessionTrace.start(workflow_name="wf", runtime="self", initial_slots={})
    doc = json.loads(SessionTrace.path_for(t.session_id).read_text())
    assert doc["workflow"]["graph"] == {}
