"""Lambda entry points. One handler per consuming product adapter."""

from __future__ import annotations

import json
import os
import time

from still import envelope as envelopes
from still.adapters import axxess
from still.idempotency import AlreadyInProgress, DynamoIdempotency

CONSUMER_AXXESS = "axxess"

_cache: dict = {}


def log(**fields) -> None:
    # Ids and outcomes only: event data can hold names and phone numbers.
    print(json.dumps({"ts": round(time.time(), 3), **fields}))


def _idempotency() -> DynamoIdempotency:
    if "idempotency" not in _cache:
        _cache["idempotency"] = DynamoIdempotency(os.environ["STILL_IDEMPOTENCY_TABLE"])
    return _cache["idempotency"]


def _axxess_client() -> axxess.AxxessMcpClient:
    if "axxess" not in _cache:
        import boto3

        secret = boto3.client("secretsmanager").get_secret_value(
            SecretId=os.environ["AXXESS_API_KEY_SECRET_ARN"])["SecretString"]
        _cache["axxess"] = axxess.AxxessMcpClient(os.environ["AXXESS_MCP_URL"], secret.strip())
    return _cache["axxess"]


def axxess_handler(event: dict, context=None) -> dict:
    env = envelopes.from_eventbridge(event)
    ids = {"event_id": env["id"], "source": env["source"], "type": env["type"], "consumer": CONSUMER_AXXESS}

    route = (env["source"], env["type"])
    if route == ("triaxis.callismatic", "lead.triaged.v1"):
        if not axxess.wants_task(env["data"]):
            log(outcome="ignored_not_actionable", **ids)
            return {"outcome": "ignored"}
        arguments = axxess.task_arguments
    elif route == ("triaxis.omnidamus", "conversation.completed.v1"):
        arguments = axxess.completion_task_arguments
    else:
        log(outcome="ignored_unknown_type", **ids)
        return {"outcome": "ignored"}

    store = _idempotency()
    try:
        if not store.claim(env["id"], CONSUMER_AXXESS):
            log(outcome="duplicate", **ids)
            return {"outcome": "duplicate"}
    except AlreadyInProgress:
        log(outcome="in_progress_elsewhere", **ids)
        raise

    try:
        payload = _axxess_client().call_tool("create_task", arguments(env), env["id"])
    except axxess.AxxessError as error:
        store.release(env["id"], CONSUMER_AXXESS)
        log(outcome="failed", retryable=error.retryable, error=str(error), **ids)
        raise

    if payload.get("status") == "pending_approval":
        outcome = f"pending_approval:{payload.get('approvalRequestId')}"
    else:
        outcome = f"task:{payload.get('id')}"
    store.complete(env["id"], CONSUMER_AXXESS, outcome)
    log(outcome=outcome, **ids)
    return {"outcome": outcome}
