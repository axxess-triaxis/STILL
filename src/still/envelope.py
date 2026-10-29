"""The STILL event envelope and its schemas.

Every event on the bus carries one envelope as its EventBridge detail. The
envelope's source/type are copied to EventBridge's Source/DetailType so rules
can route on them without parsing data.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path

from jsonschema import Draft202012Validator, FormatChecker

SCHEMA_DIR = Path(__file__).parent / "schemas"
REGISTRY_PATH = Path(__file__).parent / "registry.json"

# Fixed namespace so the same producer item always yields the same event id,
# which is what makes consumer-side deduplication work across producer retries.
ID_NAMESPACE = uuid.UUID("5f1d6c7e-2b8a-4c43-9d55-7a1e0c3b9f21")


class InvalidEvent(ValueError):
    """The envelope or its data does not match the registered schema."""


@lru_cache(maxsize=None)
def _validator(relative_path: str) -> Draft202012Validator:
    schema = json.loads((SCHEMA_DIR / relative_path).read_text(encoding="utf-8"))
    return Draft202012Validator(schema, format_checker=FormatChecker())


def registry() -> dict:
    return json.loads(REGISTRY_PATH.read_text(encoding="utf-8"))


def schema_path(source: str, event_type: str) -> str:
    return f"{source}/{event_type}.json"


def is_registered(source: str, event_type: str) -> bool:
    for product in registry()["products"].values():
        if product["source"] == source and event_type in product["produces"]:
            return True
    return False


def _errors(validator: Draft202012Validator, instance: object) -> list[str]:
    return sorted(
        f"{'/'.join(str(p) for p in e.absolute_path) or '<root>'}: {e.message}"
        for e in validator.iter_errors(instance)
    )


def validate(envelope: dict) -> None:
    """Raises InvalidEvent unless the envelope and its data match their schemas."""
    problems = _errors(_validator("envelope.v1.json"), envelope)
    if problems:
        raise InvalidEvent("; ".join(problems))
    source, event_type = envelope["source"], envelope["type"]
    if not is_registered(source, event_type):
        raise InvalidEvent(f"{source} {event_type} is not in the STILL registry")
    problems = _errors(_validator(schema_path(source, event_type)), envelope["data"])
    if problems:
        raise InvalidEvent("; ".join(problems))


def stable_id(source: str, event_type: str, key: str) -> str:
    return str(uuid.uuid5(ID_NAMESPACE, f"{source}|{event_type}|{key}"))


def build(source: str, event_type: str, data: dict, *, key: str, subject: str | None = None,
          time: datetime | None = None) -> dict:
    """Builds and validates an envelope. key identifies the producer's item."""
    envelope = {
        "specversion": "1.0",
        "id": stable_id(source, event_type, key),
        "source": source,
        "type": event_type,
        "time": (time or datetime.now(timezone.utc)).isoformat().replace("+00:00", "Z"),
        "data": data,
    }
    if subject:
        envelope["subject"] = subject
    validate(envelope)
    return envelope


def from_eventbridge(event: dict) -> dict:
    """Returns the envelope from an EventBridge-delivered event, after checking it."""
    envelope = event.get("detail")
    if not isinstance(envelope, dict):
        raise InvalidEvent("EventBridge event has no detail object")
    if envelope.get("source") != event.get("source") or envelope.get("type") != event.get("detail-type"):
        raise InvalidEvent("EventBridge source/detail-type do not match the envelope")
    validate(envelope)
    return envelope
