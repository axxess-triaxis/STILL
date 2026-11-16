"""Callismatic emit -> EventBridge rule -> AXXESS adapter -> AXXESS MCP, all local.

The bus itself is replaced by matching the rule's pattern from template.yaml and
wrapping the put entry the way EventBridge delivers it to Lambda.
"""

import json

import pytest
from cfnlint.decode import decode
from conftest import lead_data

from still import emit, handlers
from still.adapters.axxess import AxxessError, AxxessMcpClient
from still.idempotency import AlreadyInProgress

TEMPLATE = __import__("pathlib").Path(__file__).resolve().parents[1] / "template.yaml"
BUS = "arn:aws:events:ap-southeast-2:123456789012:event-bus/still"


def rule_pattern(name="AxxessLeadRule"):
    template, matches = decode(str(TEMPLATE))
    assert not matches
    pattern = template["Resources"][name]["Properties"]["EventPattern"]
    return __import__("json").loads(pattern) if isinstance(pattern, str) else pattern


def matches(pattern, event):
    for key, expected in pattern.items():
        if key == "$or":
            if not any(matches(option, event) for option in expected):
                return False
            continue
        if key not in event:
            return False
        if isinstance(expected, dict):
            if not isinstance(event[key], dict) or not matches(expected, event[key]):
                return False
        elif event[key] not in expected:
            return False
    return True


class MemoryIdempotency:
    def __init__(self):
        self.items = {}

    def claim(self, event_id, consumer):
        status = self.items.get((consumer, event_id))
        if status == "COMPLETED":
            return False
        if status == "IN_PROGRESS":
            raise AlreadyInProgress(event_id)
        self.items[(consumer, event_id)] = "IN_PROGRESS"
        return True

    def complete(self, event_id, consumer, outcome):
        self.items[(consumer, event_id)] = "COMPLETED"

    def release(self, event_id, consumer):
        self.items.pop((consumer, event_id), None)


class CapturingEvents:
    def __init__(self):
        self.entries = []

    def put_events(self, Entries):
        self.entries.extend(Entries)
        return {"FailedEntryCount": 0, "Entries": [{"EventId": "x"}]}


@pytest.fixture
def wired(monkeypatch, fake_axxess):
    monkeypatch.setenv(emit.BUS_ENV, BUS)
    store = MemoryIdempotency()
    monkeypatch.setattr(handlers, "_cache", {
        "idempotency": store, "axxess": AxxessMcpClient(fake_axxess["url"], "agent-key")})
    events = CapturingEvents()

    def deliver(data, key="msg-0001"):
        emit.emit("triaxis.callismatic", "lead.triaged.v1", data, key=key, client=events)
        entry = events.entries[-1]
        event = {"version": "0", "id": "eb-1", "source": entry["Source"], "detail-type": entry["DetailType"],
                 "account": "123456789012", "region": "ap-southeast-2", "resources": [],
                 "detail": json.loads(entry["Detail"])}
        if not matches(rule_pattern(), event):
            return None
        return handlers.axxess_handler(event)

    return deliver, fake_axxess, store


def test_lead_becomes_one_axxess_task(wired):
    deliver, axxess, _ = wired
    assert deliver(lead_data()) == {"outcome": "task:task-1"}
    [request] = axxess["requests"]
    assert request["body"]["params"]["name"] == "create_task"
    assert request["body"]["params"]["arguments"]["title"].startswith("Call Daniel Ortiz back")


def test_redelivery_does_not_create_a_second_task(wired):
    deliver, axxess, _ = wired
    deliver(lead_data())
    assert deliver(lead_data()) == {"outcome": "duplicate"}
    assert len(axxess["requests"]) == 1


@pytest.mark.parametrize("overrides", [{"category": "scam"}, {"category": "routine"}, {"category": "important", "needs_decision": False}])
def test_rule_filters_out_items_that_do_not_need_the_founder(wired, overrides):
    deliver, axxess, _ = wired
    assert deliver(lead_data(**overrides)) is None
    assert axxess["requests"] == []


def test_adapter_rechecks_even_if_rule_is_bypassed(wired, monkeypatch):
    _, axxess, _ = wired
    from still import envelope
    env = envelope.build("triaxis.callismatic", "lead.triaged.v1", lead_data(category="spam"), key="z")
    event = {"source": env["source"], "detail-type": env["type"], "detail": env}
    assert handlers.axxess_handler(event) == {"outcome": "ignored"}
    assert axxess["requests"] == []


def test_pending_approval_is_recorded_as_done(wired):
    deliver, axxess, _ = wired
    axxess["reply"] = {"jsonrpc": "2.0", "id": "x", "result": {"content": [{"type": "text", "text": json.dumps(
        {"status": "pending_approval", "approvalRequestId": "ap-9"})}]}}
    assert deliver(lead_data()) == {"outcome": "pending_approval:ap-9"}


def test_failure_releases_the_claim_so_a_retry_can_succeed(wired):
    deliver, axxess, store = wired
    axxess["status"] = 503
    with pytest.raises(AxxessError):
        deliver(lead_data())
    assert store.items == {}
    axxess["status"] = 200
    assert deliver(lead_data())["outcome"].startswith("task:")


def test_invalid_detail_is_rejected_before_any_call(wired):
    _, axxess, _ = wired
    from still.envelope import InvalidEvent
    with pytest.raises(InvalidEvent):
        handlers.axxess_handler({"source": "triaxis.callismatic", "detail-type": "lead.triaged.v1",
                                 "detail": {"specversion": "1.0"}})
    assert axxess["requests"] == []


def test_logs_carry_ids_not_personal_data(wired, capsys):
    deliver, _, _ = wired
    deliver(lead_data())
    out = capsys.readouterr().out
    assert "Daniel" not in out and "kitchen" not in out and "+1555" not in out
    assert json.loads(out.strip().splitlines()[-1])["outcome"] == "task:task-1"


def completion_data(**overrides):
    data = {
        "conversation_id": "+15550002222",
        "completed_at": "2026-10-28T16:28:12Z",
        "channel": "whatsapp",
        "context": "Inbound lead",
        "summary": "Demo confirmed for tomorrow 6 pm IST.",
        "key_facts": {"demo_time": "tomorrow, 6 pm IST"},
        "customer_phone": "+15550002222",
        "customer_email": "cust@example.com",
        "receipt_sent": True,
    }
    data.update(overrides)
    return data


@pytest.fixture
def completion(wired):
    _, axxess, store = wired
    events = CapturingEvents()

    def deliver(data, key="+15550002222:wamid.9"):
        emit.emit("triaxis.omnidamus", "conversation.completed.v1", data, key=key, client=events)
        entry = events.entries[-1]
        event = {"source": entry["Source"], "detail-type": entry["DetailType"], "detail": json.loads(entry["Detail"])}
        if matches(rule_pattern("AxxessLeadRule"), event):
            raise AssertionError("an Omnidamus event must not match the Callismatic lead rule")
        if not matches(rule_pattern("AxxessCompletionRule"), event):
            return None
        return handlers.axxess_handler(event)

    return deliver, axxess


def test_completed_conversation_becomes_one_axxess_task(completion, capsys):
    deliver, axxess = completion
    assert deliver(completion_data())["outcome"].startswith("task:")
    assert deliver(completion_data()) == {"outcome": "duplicate"}
    [request] = axxess["requests"]
    args = request["body"]["params"]["arguments"]
    assert args["title"] == "Omnidamus follow-up: demo time: tomorrow, 6 pm IST"
    assert "Conversation context: Inbound lead" in args["description"]
    assert "Customer: +15550002222, cust@example.com" in args["description"]
    assert "Receipt email sent: yes" in args["description"]
    assert args["priority"] == "medium"
    out = capsys.readouterr().out
    assert "+1555" not in out and "cust@" not in out


def test_completion_without_context_or_facts_still_has_a_title(completion):
    deliver, axxess = completion
    deliver(completion_data(context="", key_facts={}, customer_email=None, receipt_sent=False))
    args = axxess["requests"][0]["body"]["params"]["arguments"]
    assert args["title"] == "Omnidamus: conversation completed"
    assert "Conversation context" not in args["description"]
    assert "Customer: +15550002222\n" in args["description"]
    assert "Receipt email sent: no" in args["description"]


def test_long_context_no_longer_hides_the_facts_in_the_title(completion):
    # Regression from the first live run (2026-10-29): the title led with a ~190-character
    # context and the 200-character cut dropped demo_time.
    deliver, axxess = completion
    long_context = ("Inbound demo request for Omnidamus from the Triaxis Ventures website. Help the customer "
                    "pick a demo time, confirm it, then close the conversation with an email receipt.")
    deliver(completion_data(context=long_context))
    args = axxess["requests"][0]["body"]["params"]["arguments"]
    assert args["title"] == "Omnidamus follow-up: demo time: tomorrow, 6 pm IST"
    assert f"Conversation context: {long_context}" in args["description"]


def test_invalid_completion_is_rejected(completion):
    deliver, axxess = completion
    from still.envelope import InvalidEvent
    with pytest.raises(InvalidEvent):
        deliver(completion_data(channel="fax"))
    assert axxess["requests"] == []


# --- Automation plan Phase 2 (2026-11-16): Callismatic leads into the AXXESS CRM -------------------

LEAD_ID = "11111111-2222-3333-4444-555555555555"


@pytest.fixture
def lead_sync(wired, monkeypatch):
    monkeypatch.setenv("STILL_AXXESS_LEAD_SYNC", "true")
    deliver, axxess, store = wired
    axxess["by_tool"]["upsert_lead"] = {"leadId": LEAD_ID, "created": True, "changed": [], "stage": "new", "status": "open"}
    axxess["by_tool"]["log_conversation"] = {"leadId": LEAD_ID, "conversationId": "conv-1", "duplicate": False}
    return deliver, axxess, store


def tools_called(axxess):
    return [request["body"]["params"]["name"] for request in axxess["requests"]]


def test_lead_sync_is_off_by_default(wired):
    deliver, axxess, _ = wired
    assert deliver(lead_data()) == {"outcome": "task:task-1"}
    assert tools_called(axxess) == ["create_task"]


def test_lead_becomes_a_crm_lead_with_the_message_on_its_history_then_a_task(lead_sync, capsys):
    deliver, axxess, _ = lead_sync
    outcome = deliver(lead_data(key_facts={"Name": "Daniel Ortiz", "company": "Ortiz Builders", "project": "kitchen remodel"}))
    assert tools_called(axxess) == ["upsert_lead", "log_conversation", "create_task"]
    assert outcome["outcome"].startswith(f"lead:{LEAD_ID},task:")
    upsert, conversation, task = (request["body"]["params"]["arguments"] for request in axxess["requests"])
    assert upsert == {"phoneE164": "+15550001111", "source": "callismatic_text", "contactName": "Daniel Ortiz", "organizationName": "Ortiz Builders"}
    assert conversation["leadId"] == LEAD_ID
    assert conversation["channel"] == "sms" and conversation["direction"] == "inbound" and conversation["source"] == "callismatic"
    assert conversation["externalId"] == "callismatic:msg-0001"
    assert conversation["occurredAt"] == "2026-10-28T10:00:00Z"
    assert "kitchen remodel" in conversation["summary"]
    assert f"AXXESS lead: {LEAD_ID}" in task["description"]
    assert "+15550001111" not in capsys.readouterr().out


def test_important_items_and_items_without_a_callable_number_stay_task_only(lead_sync):
    deliver, axxess, _ = lead_sync
    deliver(lead_data(category="important"), key="a")
    data = lead_data()
    data["caller_number"] = "unknown"
    deliver(data, key="b")
    assert tools_called(axxess) == ["create_task", "create_task"]


def test_missing_capability_or_older_axxess_still_creates_the_task(lead_sync):
    deliver, axxess, _ = lead_sync
    axxess["by_tool"]["upsert_lead"] = {"error": {"code": -32001, "message": "This agent connection does not have the upsert_lead capability."}}
    assert deliver(lead_data())["outcome"].startswith("task:")
    assert tools_called(axxess) == ["upsert_lead", "create_task"]


def test_a_failed_task_after_the_lead_is_retried_without_losing_the_event(lead_sync):
    deliver, axxess, store = lead_sync
    axxess["by_tool"]["create_task"] = {"error": {"code": -32603, "message": "handler exploded"}}
    with pytest.raises(AxxessError):
        deliver(lead_data())
    assert store.items == {}
    del axxess["by_tool"]["create_task"]
    assert deliver(lead_data())["outcome"].startswith(f"lead:{LEAD_ID},task:")
    assert tools_called(axxess).count("upsert_lead") == 2


def test_a_tool_refusal_on_the_lead_is_not_swallowed(lead_sync):
    deliver, axxess, store = lead_sync
    axxess["by_tool"]["log_conversation"] = {"error": {"code": -32602, "message": "summary is required."}}
    with pytest.raises(AxxessError):
        deliver(lead_data())
    assert "create_task" not in tools_called(axxess)
    assert store.items == {}
