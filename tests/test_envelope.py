import json

import pytest
from conftest import lead_data

from still import emit, envelope
from still.envelope import InvalidEvent


def test_build_valid_lead_event():
    env = envelope.build("triaxis.callismatic", "lead.triaged.v1", lead_data(), key="msg-0001")
    assert env["specversion"] == "1.0"
    assert env["time"].endswith("Z")


def test_id_is_stable_per_item_so_retries_dedupe():
    a = envelope.build("triaxis.callismatic", "lead.triaged.v1", lead_data(), key="msg-0001")
    b = envelope.build("triaxis.callismatic", "lead.triaged.v1", lead_data(), key="msg-0001")
    c = envelope.build("triaxis.callismatic", "lead.triaged.v1", lead_data(), key="msg-0002")
    assert a["id"] == b["id"] != c["id"]


def test_vendored_emitter_uses_the_same_ids():
    built = envelope.build("triaxis.callismatic", "lead.triaged.v1", lead_data(), key="k")
    vendored = emit.envelope("triaxis.callismatic", "lead.triaged.v1", lead_data(), key="k")
    assert built["id"] == vendored["id"]
    envelope.validate(vendored)


@pytest.mark.parametrize("mutate, message", [
    (lambda d: d["triage"].update(category="maybe"), "category"),
    (lambda d: d["triage"].pop("summary"), "summary"),
    (lambda d: d.update(triaged_at="yesterday"), "date-time"),
    (lambda d: d["triage"].update(key_facts={"n": 3}), "key_facts"),
])
def test_bad_data_is_rejected(mutate, message):
    data = lead_data()
    mutate(data)
    with pytest.raises(InvalidEvent, match=message):
        envelope.build("triaxis.callismatic", "lead.triaged.v1", data, key="x")


def test_unregistered_event_type_is_rejected():
    with pytest.raises(InvalidEvent, match="not in the STILL registry"):
        envelope.build("triaxis.callismatic", "call.blocked.v1", {}, key="x")


def test_source_must_be_a_triaxis_product():
    with pytest.raises(InvalidEvent, match="source"):
        envelope.build("acme.crm", "lead.triaged.v1", lead_data(), key="x")


def test_from_eventbridge_checks_routing_fields_match_envelope():
    env = envelope.build("triaxis.callismatic", "lead.triaged.v1", lead_data(), key="x")
    event = {"source": "triaxis.callismatic", "detail-type": "lead.triaged.v1", "detail": env}
    assert envelope.from_eventbridge(event) == env
    with pytest.raises(InvalidEvent, match="do not match"):
        envelope.from_eventbridge({**event, "detail-type": "other.v1"})


def test_every_registered_event_has_a_schema_and_every_schema_is_registered():
    registered = {
        f"{p['source']}/{t}.json"
        for p in envelope.registry()["products"].values() for t in p["produces"]
    }
    on_disk = {
        str(path.relative_to(envelope.SCHEMA_DIR)).replace("\\", "/")
        for path in envelope.SCHEMA_DIR.rglob("*.json") if path.name != "envelope.v1.json"
    }
    assert registered == on_disk
    for name in on_disk:
        json.loads((envelope.SCHEMA_DIR / name).read_text(encoding="utf-8"))


def test_registry_sources_match_product_keys():
    for key, product in envelope.registry()["products"].items():
        assert product["source"] == f"triaxis.{key}"
