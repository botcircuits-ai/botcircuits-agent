"""Grant-on-reply: an affirmative answer to a permission pause grants exactly
the tool(s) that pause was blocked on — nothing parsed out of free text."""

from botcircuits.runtime.run_workflow import _reply_grants_tools


def test_affirmative_reply_grants_the_pending_tool():
    assert _reply_grants_tools("yes use websearch", ["WebSearch"]) == ["WebSearch"]
    assert _reply_grants_tools("ok", ["WebSearch"]) == ["WebSearch"]
    assert _reply_grants_tools("sure, go ahead", ["WebFetch"]) == ["WebFetch"]
    assert _reply_grants_tools("allow it", ["WebSearch"]) == ["WebSearch"]


def test_grants_only_what_the_pause_asked_for():
    # We never parse tool names from the reply text — the pending set decides.
    assert _reply_grants_tools("yes", ["WebSearch", "WebFetch"]) == [
        "WebSearch", "WebFetch",
    ]
    # No pending tools → nothing to grant even on a clear yes.
    assert _reply_grants_tools("yes", []) == []


def test_negative_or_unrelated_reply_grants_nothing():
    assert _reply_grants_tools("no, use a different source", ["WebSearch"]) == []
    assert _reply_grants_tools("try the jobs API instead", ["WebSearch"]) == []
    assert _reply_grants_tools("", ["WebSearch"]) == []
    assert _reply_grants_tools(None, ["WebSearch"]) == []
