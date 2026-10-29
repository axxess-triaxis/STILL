"""Put one synthetic Callismatic lead event on the deployed STILL bus.

Checks the STILL -> AXXESS half of the flow without touching Callismatic. It
creates a real task in AXXESS, so run it only when that is wanted.

    python scripts/send_test_event.py --bus-arn <EventBusArn> [--profile triaxis-official]
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from still import emit, envelope  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--bus-arn", required=True)
    parser.add_argument("--profile")
    args = parser.parse_args()

    now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    data = {
        "source_ref": f"still-test:{now}",
        "triaged_at": now,
        "channel": "text",
        "caller_number": None,
        "triage": {
            "category": "lead",
            "summary": "STILL test event: a synthetic lead, not a real caller.",
            "key_facts": {"test": "true"},
            "needs_decision": True,
            "decision_reason": "Verifies the STILL to AXXESS flow end to end.",
            "suggested_action": "STILL test: delete this task once seen.",
            "callback_recommended": False,
            "callback_task": None,
            "block_recommended": False,
            "urgency": "low",
        },
    }
    envelope.build("triaxis.callismatic", "lead.triaged.v1", data, key=data["source_ref"])

    import boto3

    session = boto3.Session(profile_name=args.profile)
    client = session.client("events", region_name=emit._region_of(args.bus_arn))
    import os

    os.environ[emit.BUS_ENV] = args.bus_arn
    event_id = emit.emit("triaxis.callismatic", "lead.triaged.v1", data, key=data["source_ref"], client=client)
    print(f"put STILL event {event_id}")


if __name__ == "__main__":
    main()
