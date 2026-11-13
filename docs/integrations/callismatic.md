# Callismatic -> STILL (proposed, NOT applied)

Status: proposal only. Callismatic's repo and its deployed Lambda are unchanged.
Applying this needs the founder's go-ahead, a Callismatic PR, and a redeploy.

## Change

1. Copy `src/still/emit.py` from this repo to `src/callismatic/still_emit.py`
   (boto3 and the standard library only; a no-op unless `STILL_EVENT_BUS_ARN` is set).
2. In `src/callismatic/triage.py`, publish right after `record_decision`, in both
   `_decide_and_act` and `_decide_and_act_async`, best-effort like the CRM sync:

```python
from .still_emit import emit as _still_emit


def _publish_to_still(source_name: str, caller_number: str, triage: CallTriage) -> None:
    """Best-effort: STILL being down or unset never blocks triage."""
    channel = source_name.split(":", 1)[0] if source_name.startswith(("sms:", "whatsapp:")) else "voicemail"
    try:
        _still_emit(
            "triaxis.callismatic",
            "lead.triaged.v1",
            {
                "source_ref": source_name,
                "triaged_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
                "channel": channel,
                "caller_number": caller_number if caller_number != "unknown" else None,
                "triage": triage.model_dump(),
            },
            key=source_name,
        )
    except Exception as error:  # noqa: BLE001
        logger.warning("STILL publish failed for %s: %s", source_name, type(error).__name__)
```

   `key=source_name` (`sms:<message_id>`, `whatsapp:<message_id>` or the voicemail file
   name) makes a re-triage of the same item produce the same event id, so AXXESS gets
   at most one task per item.
3. Add a test that the event matches STILL's schema (vendor
   `schemas/triaxis.callismatic/lead.triaged.v1.json` or assert the field names).

## Deploy-side wiring (founder's go-ahead)

- Attach the STILL stack output `ProducerPolicyArn` to the `callismatic` Lambda's role.
- Set Lambda env var `STILL_EVENT_BUS_ARN` to the stack output `EventBusArn`. The bus is
  in ap-southeast-2; the emitter calls that region directly from us-east-1.
- Rollback: remove the env var. The emitter becomes a no-op without a redeploy.

## Voice-agent call results (`call.completed.v1`, 2026-11-13)

AXXESS's "Call with agent" button sends one job to Callismatic (`POST /api/outbound-calls`). When the
call ends, Callismatic emits `triaxis.callismatic` / `call.completed.v1` (schema in
`src/still/schemas/triaxis.callismatic/call.completed.v1.json`). It carries the job id, outcome,
summary, action items, an accepted demo slot and an opt-out flag, and never the transcript or the
phone number.

`AxxessAgentCallRule` sends it to the AXXESS adapter, which calls:

1. `log_agent_call` (auto, idempotent on the job id): records the result, moves the lead on, leaves
   a follow-up task, and suppresses the number if the contact opted out. The AXXESS connection STILL
   uses must be granted the opt-in `log_agent_call` capability in AXXESS.
2. `create_meeting`, only if a demo slot was accepted and the contact did not opt out. It is critical
   in AXXESS, so it waits for the founder's approval.

No real call can be placed yet: Callismatic has no lead-call provider for India or the UAE, and
AXXESS's gate refuses agent calls until DLT, TDRA and counsel sign-off are recorded.
