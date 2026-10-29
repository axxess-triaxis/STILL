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


def rule_pattern():
    template, matches = decode(str(TEMPLATE))
    assert not matches
    return template["Resources"]["AxxessLeadRule"]["Properties"]["EventPattern"]


def matches(pattern, event):
    for key, expected in pattern.items():
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


@pytest.mark.parametrize("overrides", [{"category": "scam"}, {"category": "routine"}, {"needs_decision": False}])
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
