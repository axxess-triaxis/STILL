"""Callismatic call.completed.v1 -> AXXESS log_agent_call (+ create_meeting for an accepted demo)."""

import json

import pytest
from test_flow import CapturingEvents, matches, rule_pattern, wired  # noqa: F401 -- wired is a fixture

from still import emit, handlers
from still.envelope import InvalidEvent

JOB = "11111111-2222-3333-4444-555555555555"


def call_data(**overrides):
    data = {
        "job_id": JOB,
        "lead_id": "22222222-3333-4444-5555-666666666666",
        "organization_id": "33333333-4444-5555-6666-777777777777",
        "title": "Acme - Asha",
        "status": "completed",
        "outcome": "interested",
        "summary": "Wants a demo next week.",
        "action_items": ["Send the deck"],
        "accepted_slot": {"starts_at": "2026-11-17T05:30:00+00:00", "ends_at": "2026-11-17T06:00:00+00:00"},
        "follow_up_at": None,
        "opted_out": False,
        "failure_reason": None,
    }
    data.update(overrides)
    return data


@pytest.fixture
def agent_call(wired):
    _, axxess, store = wired
    events = CapturingEvents()

    def deliver(data, key=JOB):
        emit.emit("triaxis.callismatic", "call.completed.v1", data, key=key, client=events)
        entry = events.entries[-1]
        event = {"source": entry["Source"], "detail-type": entry["DetailType"], "detail": json.loads(entry["Detail"])}
        assert not matches(rule_pattern("AxxessLeadRule"), event), "a call result must not match the lead rule"
        assert not matches(rule_pattern("AxxessCompletionRule"), event)
        if not matches(rule_pattern("AxxessAgentCallRule"), event):
            return None
        return handlers.axxess_handler(event)

    return deliver, axxess


def tool_calls(axxess):
    return [(r["body"]["params"]["name"], r["body"]["params"]["arguments"]) for r in axxess["requests"]]


def test_an_accepted_demo_logs_the_call_then_asks_for_the_meeting(agent_call, capsys):
    deliver, axxess = agent_call
    result = deliver(call_data())
    assert result["outcome"].startswith("call_logged:completed,meeting")
    [(log_name, log_args), (meeting_name, meeting_args)] = tool_calls(axxess)
    assert log_name == "log_agent_call"
    assert log_args == {"jobId": JOB, "status": "completed", "outcome": "interested", "summary": "Wants a demo next week.",
                        "actionItems": ["Send the deck"], "optedOut": False}
    assert meeting_name == "create_meeting"
    assert meeting_args["title"] == "Demo: Acme - Asha"
    assert meeting_args["startsAt"] == "2026-11-17T05:30:00+00:00"
    assert meeting_args["notes"].startswith("Wants a demo next week.\nBooked by the voice agent")
    assert "Acme" not in capsys.readouterr().out


def test_a_redelivered_result_is_a_duplicate(agent_call):
    deliver, axxess = agent_call
    deliver(call_data())
    assert deliver(call_data()) == {"outcome": "duplicate"}
    assert len(axxess["requests"]) == 2


def test_no_slot_means_no_meeting_and_a_follow_up_is_passed_on(agent_call):
    deliver, axxess = agent_call
    deliver(call_data(outcome="call_back", accepted_slot=None, follow_up_at="2026-11-20T05:30:00+00:00"))
    [(name, args)] = tool_calls(axxess)
    assert name == "log_agent_call"
    assert args["followUpAt"] == "2026-11-20T05:30:00+00:00"


def test_an_opt_out_never_books_a_meeting(agent_call):
    deliver, axxess = agent_call
    deliver(call_data(outcome="not_interested", opted_out=True))
    [(name, args)] = tool_calls(axxess)
    assert (name, args["optedOut"]) == ("log_agent_call", True)


def test_a_failed_job_is_logged_as_failed(agent_call):
    deliver, axxess = agent_call
    deliver(call_data(status="failed", outcome=None, summary=None, action_items=[], accepted_slot=None,
                      failure_reason="provider_unavailable"))
    assert tool_calls(axxess) == [("log_agent_call", {"jobId": JOB, "status": "failed", "failureReason": "provider_unavailable"})]


def test_a_failed_meeting_call_releases_the_claim_for_a_retry(agent_call, wired):
    deliver, axxess = agent_call
    _, _, store = wired
    calls = {"n": 0}
    original = handlers._axxess_client().call_tool

    def flaky(name, arguments, request_id):
        calls["n"] += 1
        if name == "create_meeting" and calls["n"] == 2:
            from still.adapters.axxess import AxxessError
            raise AxxessError("AXXESS MCP HTTP 503", retryable=True)
        return original(name, arguments, request_id)

    handlers._cache["axxess"].call_tool = flaky
    with pytest.raises(Exception):
        deliver(call_data())
    assert store.items == {}
    assert deliver(call_data())["outcome"].startswith("call_logged:completed,meeting")


def test_a_result_carrying_a_transcript_or_phone_is_rejected(agent_call):
    deliver, axxess = agent_call
    with pytest.raises(InvalidEvent):
        deliver({**call_data(), "transcript": "hello"})
    with pytest.raises(InvalidEvent):
        deliver({**call_data(), "phone_e164": "+919876543210"})
    assert axxess["requests"] == []
