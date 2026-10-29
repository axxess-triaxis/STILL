"""Producer helper: put one STILL event on the bus.

Self-contained (boto3 and the standard library only) so a product can copy this
file instead of depending on the STILL package. It is a no-op unless
STILL_EVENT_BUS_ARN is set, so shipping it changes nothing until a product opts in.

    from still_emit import emit
    emit("triaxis.callismatic", "lead.triaged.v1", data, key=source_ref)
"""

from __future__ import annotations

import json
import os
import uuid
from datetime import datetime, timezone

ID_NAMESPACE = uuid.UUID("5f1d6c7e-2b8a-4c43-9d55-7a1e0c3b9f21")
BUS_ENV = "STILL_EVENT_BUS_ARN"


class EmitFailed(RuntimeError):
    """EventBridge did not accept the event."""


def _region_of(bus_arn: str) -> str:
    # arn:aws:events:<region>:<account>:event-bus/<name>
    parts = bus_arn.split(":")
    if len(parts) < 6 or parts[2] != "events" or not parts[5].startswith("event-bus/"):
        raise ValueError(f"{BUS_ENV} is not an event bus ARN")
    return parts[3]


def envelope(source: str, event_type: str, data: dict, *, key: str, subject: str | None = None) -> dict:
    env = {
        "specversion": "1.0",
        "id": str(uuid.uuid5(ID_NAMESPACE, f"{source}|{event_type}|{key}")),
        "source": source,
        "type": event_type,
        "time": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "data": data,
    }
    if subject:
        env["subject"] = subject
    return env


def emit(source: str, event_type: str, data: dict, *, key: str, subject: str | None = None,
         client=None) -> str | None:
    """Puts the event on the STILL bus and returns its id, or None when STILL is off.

    key must identify the producer's item, so a retried emit gets the same id
    and consumers can drop the duplicate.
    """
    bus_arn = os.environ.get(BUS_ENV, "").strip()
    if not bus_arn:
        return None
    env = envelope(source, event_type, data, key=key, subject=subject)
    if client is None:
        import boto3

        # The bus may live in another region than the producer.
        client = boto3.client("events", region_name=_region_of(bus_arn))
    response = client.put_events(Entries=[{
        "EventBusName": bus_arn,
        "Source": source,
        "DetailType": event_type,
        "Detail": json.dumps(env),
    }])
    if response.get("FailedEntryCount"):
        entry = response["Entries"][0]
        raise EmitFailed(f"{entry.get('ErrorCode')}: {entry.get('ErrorMessage')}")
    return env["id"]
