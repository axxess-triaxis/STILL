import json

import pytest
from conftest import lead_data

from still import envelope
from still.adapters import axxess
from still.adapters.axxess import AxxessError, AxxessMcpClient


def lead_envelope(**overrides):
    return envelope.build("triaxis.callismatic", "lead.triaged.v1", lead_data(**overrides), key="msg-0001")


@pytest.mark.parametrize("category, needs_decision, expected", [
    ("lead", True, True), ("important", True, True),
    ("lead", False, False), ("routine", True, False), ("scam", True, False),
])
def test_only_leads_and_important_items_needing_a_decision_become_tasks(category, needs_decision, expected):
    assert axxess.wants_task(lead_data(category=category, needs_decision=needs_decision)) is expected


def test_task_arguments_carry_action_context_and_trace():
    env = lead_envelope(urgency="high")
    args = axxess.task_arguments(env)
    assert args["title"] == "Call Daniel Ortiz back about the kitchen remodel quote."
    assert args["priority"] == "high"
    assert "Daniel Ortiz wants a quote" in args["description"]
    assert "- project: kitchen remodel" in args["description"]
    assert "Caller: +15550001111" in args["description"]
    assert env["id"] in args["description"]
    assert "msg-0001" in args["description"]


def test_title_falls_back_to_summary_and_is_truncated():
    args = axxess.task_arguments(lead_envelope(suggested_action=None, summary="x" * 500))
    assert len(args["title"]) == axxess.TITLE_LIMIT
    assert args["title"].endswith("...")


@pytest.mark.parametrize("urgency, priority", [("none", "low"), ("low", "low"), ("medium", "medium"), ("high", "high")])
def test_priority_mapping(urgency, priority):
    assert axxess.task_arguments(lead_envelope(urgency=urgency))["priority"] == priority


def test_client_sends_bearer_json_rpc_tools_call(fake_axxess):
    client = AxxessMcpClient(fake_axxess["url"], "agent-key")
    payload = client.call_tool("create_task", {"title": "T"}, "evt-1")
    [request] = fake_axxess["requests"]
    assert request["auth"] == "Bearer agent-key"
    assert request["body"] == {"jsonrpc": "2.0", "id": "evt-1", "method": "tools/call",
                               "params": {"name": "create_task", "arguments": {"title": "T"}}}
    assert payload["id"] == "task-1"


def test_pending_approval_payload_is_returned(fake_axxess):
    fake_axxess["reply"] = {"jsonrpc": "2.0", "id": "x", "result": {"content": [{"type": "text", "text": json.dumps(
        {"status": "pending_approval", "approvalRequestId": "ap-1", "pendingToolCallId": "pc-1"})}]}}
    payload = AxxessMcpClient(fake_axxess["url"], "k").call_tool("create_task", {"title": "T"}, "x")
    assert payload["status"] == "pending_approval"


@pytest.mark.parametrize("status, retryable", [(401, False), (413, False), (429, True), (502, True)])
def test_http_errors_are_classified(fake_axxess, status, retryable):
    fake_axxess["status"] = status
    with pytest.raises(AxxessError) as caught:
        AxxessMcpClient(fake_axxess["url"], "k").call_tool("create_task", {"title": "T"}, "x")
    assert caught.value.retryable is retryable


@pytest.mark.parametrize("code, retryable", [(-32001, False), (-32602, False), (-32603, True)])
def test_rpc_errors_are_classified(fake_axxess, code, retryable):
    fake_axxess["reply"] = {"jsonrpc": "2.0", "id": "x", "error": {"code": code, "message": "m"}}
    with pytest.raises(AxxessError) as caught:
        AxxessMcpClient(fake_axxess["url"], "k").call_tool("create_task", {"title": "T"}, "x")
    assert caught.value.retryable is retryable


def test_tool_error_result_is_not_retried(fake_axxess):
    fake_axxess["reply"] = {"jsonrpc": "2.0", "id": "x", "result": {"isError": True, "content": [{"type": "text", "text": "title is required."}]}}
    with pytest.raises(AxxessError, match="title is required") as caught:
        AxxessMcpClient(fake_axxess["url"], "k").call_tool("create_task", {"title": "T"}, "x")
    assert caught.value.retryable is False


def test_unreachable_is_retryable():
    with pytest.raises(AxxessError) as caught:
        AxxessMcpClient("http://127.0.0.1:9/api/agents/mcp", "k", timeout=2).call_tool("create_task", {}, "x")
    assert caught.value.retryable is True


def test_plain_http_is_refused_outside_local_tests():
    with pytest.raises(ValueError):
        AxxessMcpClient("http://example.com/api/agents/mcp", "k")
