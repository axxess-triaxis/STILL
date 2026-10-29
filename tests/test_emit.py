import json

import pytest
from conftest import lead_data

from still import emit

BUS = "arn:aws:events:ap-southeast-2:123456789012:event-bus/still"


class FakeEvents:
    def __init__(self, failed=False):
        self.calls = []
        self.failed = failed

    def put_events(self, Entries):
        self.calls.append(Entries)
        if self.failed:
            return {"FailedEntryCount": 1, "Entries": [{"ErrorCode": "AccessDenied", "ErrorMessage": "no"}]}
        return {"FailedEntryCount": 0, "Entries": [{"EventId": "e-1"}]}


def test_noop_when_still_is_not_configured():
    client = FakeEvents()
    assert emit.emit("triaxis.callismatic", "lead.triaged.v1", lead_data(), key="k", client=client) is None
    assert client.calls == []


def test_puts_one_entry_with_routing_fields(monkeypatch):
    monkeypatch.setenv(emit.BUS_ENV, BUS)
    client = FakeEvents()
    event_id = emit.emit("triaxis.callismatic", "lead.triaged.v1", lead_data(), key="k", client=client)
    [[entry]] = client.calls
    assert entry["EventBusName"] == BUS
    assert entry["Source"] == "triaxis.callismatic"
    assert entry["DetailType"] == "lead.triaged.v1"
    assert json.loads(entry["Detail"])["id"] == event_id


def test_rejected_entry_raises(monkeypatch):
    monkeypatch.setenv(emit.BUS_ENV, BUS)
    with pytest.raises(emit.EmitFailed, match="AccessDenied"):
        emit.emit("triaxis.callismatic", "lead.triaged.v1", lead_data(), key="k", client=FakeEvents(failed=True))


def test_bus_region_comes_from_the_arn():
    assert emit._region_of(BUS) == "ap-southeast-2"
    with pytest.raises(ValueError):
        emit._region_of("arn:aws:sns:ap-southeast-2:123456789012:topic")
