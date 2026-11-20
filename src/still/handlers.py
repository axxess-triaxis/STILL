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

        secrets = boto3.client("secretsmanager")
        secret = secrets.get_secret_value(SecretId=os.environ["AXXESS_API_KEY_SECRET_ARN"])["SecretString"]
        bypass_arn = os.environ.get("AXXESS_BYPASS_SECRET_ARN")
        bypass = secrets.get_secret_value(SecretId=bypass_arn)["SecretString"].strip() if bypass_arn else None
        _cache["axxess"] = axxess.AxxessMcpClient(os.environ["AXXESS_MCP_URL"], secret.strip(), bypass_secret=bypass)
    return _cache["axxess"]


def _results_client() -> axxess.AxxessResultsClient | None:
    """The AXXESS stack results route (2026-11-20), or None while it is not configured.
    Off by default: both AXXESS_STACK_RESULTS_URL and STACK_RESULTS_SECRET_ARN must be set."""
    if "results" not in _cache:
        url = os.environ.get("AXXESS_STACK_RESULTS_URL", "").strip()
        secret_arn = os.environ.get("STACK_RESULTS_SECRET_ARN", "").strip()
        if not url or not secret_arn:
            _cache["results"] = None
        else:
            import boto3

            secrets = boto3.client("secretsmanager")
            secret = secrets.get_secret_value(SecretId=secret_arn)["SecretString"].strip()
            bypass_arn = os.environ.get("AXXESS_BYPASS_SECRET_ARN")
            bypass = secrets.get_secret_value(SecretId=bypass_arn)["SecretString"].strip() if bypass_arn else None
            _cache["results"] = axxess.AxxessResultsClient(url, secret, bypass_secret=bypass)
    return _cache["results"]


def axxess_handler(event: dict, context=None) -> dict:
    env = envelopes.from_eventbridge(event)
    ids = {"event_id": env["id"], "source": env["source"], "type": env["type"], "consumer": CONSUMER_AXXESS}

    route = (env["source"], env["type"])
    if route == ("triaxis.callismatic", "call.completed.v1"):
        if _results_client() is not None:
            return _routed_result(env, ids)
        return _agent_call_result(env, ids)
    if route == ("triaxis.omnidamus", "conversation.completed.v1") and env["data"].get("job_id"):
        # Started by an AXXESS job, possibly for a customer: never file it with the Tenant 0 key.
        if _results_client() is None:
            log(outcome="refused_results_route_not_configured", **ids)
            raise axxess.AxxessError("A job result needs the AXXESS stack results route, which is not configured",
                                     retryable=False)
        return _routed_result(env, ids)
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


def _agent_call_result(env: dict, ids: dict) -> dict:
    """An AXXESS "Call with agent" job finished: log it, then ask for the demo meeting if one was
    accepted. log_agent_call is idempotent on the job id, so a retry after a failed create_meeting
    does not log the call twice."""
    store = _idempotency()
    try:
        if not store.claim(env["id"], CONSUMER_AXXESS):
            log(outcome="duplicate", **ids)
            return {"outcome": "duplicate"}
    except AlreadyInProgress:
        log(outcome="in_progress_elsewhere", **ids)
        raise

    client = _axxess_client()
    try:
        client.call_tool("log_agent_call", axxess.agent_call_arguments(env), env["id"])
        outcome = f"call_logged:{env['data']['status']}"
        meeting = axxess.demo_meeting_arguments(env)
        if meeting:
            payload = client.call_tool("create_meeting", meeting, f"{env['id']}:meeting")
            if payload.get("status") == "pending_approval":
                outcome += f",meeting_pending_approval:{payload.get('approvalRequestId')}"
            else:
                outcome += f",meeting:{payload.get('id')}"
    except axxess.AxxessError as error:
        store.release(env["id"], CONSUMER_AXXESS)
        log(outcome="failed", retryable=error.retryable, error=str(error), **ids)
        raise

    store.complete(env["id"], CONSUMER_AXXESS, outcome)
    log(outcome=outcome, **ids)
    return {"outcome": outcome}


def _routed_result(env: dict, ids: dict) -> dict:
    """A result for a job that AXXESS started: AXXESS files it into the organization that owns
    the job. The route is idempotent per job, and STILL's claim stops duplicate deliveries here."""
    store = _idempotency()
    try:
        if not store.claim(env["id"], CONSUMER_AXXESS):
            log(outcome="duplicate", **ids)
            return {"outcome": "duplicate"}
    except AlreadyInProgress:
        log(outcome="in_progress_elsewhere", **ids)
        raise

    try:
        reply = _results_client().file_result(axxess.results_payload(env))
    except axxess.AxxessError as error:
        store.release(env["id"], CONSUMER_AXXESS)
        log(outcome="failed", retryable=error.retryable, error=str(error), **ids)
        raise

    outcome = "routed:already_logged" if reply.get("alreadyLogged") else f"routed:{reply.get('status') or 'filed'}"
    if reply.get("taskId"):
        outcome += f",task:{reply['taskId']}"
    data = env["data"]
    # Booking the demo meeting needs approval in the job's organization; the results route does not
    # do that yet, so say so instead of booking it in the wrong organization.
    if env["type"] == "call.completed.v1" and data.get("accepted_slot") and not data.get("opted_out"):
        outcome += ",meeting_not_booked"
    store.complete(env["id"], CONSUMER_AXXESS, outcome)
    log(outcome=outcome, **ids)
    return {"outcome": outcome}
