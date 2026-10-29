"""At-most-once processing per event id, on DynamoDB.

EventBridge and Lambda async invokes are at-least-once, and producers may retry,
so every adapter claims the event id before acting. A claim that is still
IN_PROGRESS after LOCK_SECONDS (a crashed attempt) can be taken over.
"""

from __future__ import annotations

import time

from botocore.exceptions import ClientError

LOCK_SECONDS = 300
KEEP_SECONDS = 30 * 24 * 3600


class AlreadyInProgress(RuntimeError):
    """Another attempt holds a fresh claim on this event; retry later."""


class DynamoIdempotency:
    def __init__(self, table_name: str, client=None, clock=time.time):
        if client is None:
            import boto3

            client = boto3.client("dynamodb")
        self.table = table_name
        self.client = client
        self.clock = clock

    def claim(self, event_id: str, consumer: str) -> bool:
        """True if the caller should process the event; False if it was already done."""
        now = int(self.clock())
        try:
            self.client.put_item(
                TableName=self.table,
                Item={
                    "pk": {"S": f"{consumer}#{event_id}"},
                    "status": {"S": "IN_PROGRESS"},
                    "locked_until": {"N": str(now + LOCK_SECONDS)},
                    "expires_at": {"N": str(now + KEEP_SECONDS)},
                },
                ConditionExpression="attribute_not_exists(pk) OR (#s = :ip AND locked_until < :now)",
                ExpressionAttributeNames={"#s": "status"},
                ExpressionAttributeValues={":ip": {"S": "IN_PROGRESS"}, ":now": {"N": str(now)}},
                ReturnValuesOnConditionCheckFailure="ALL_OLD",
            )
            return True
        except ClientError as error:
            if error.response["Error"]["Code"] != "ConditionalCheckFailedException":
                raise
            old = error.response.get("Item", {})
            if old.get("status", {}).get("S") == "COMPLETED":
                return False
            raise AlreadyInProgress(event_id) from error

    def complete(self, event_id: str, consumer: str, outcome: str) -> None:
        self.client.update_item(
            TableName=self.table,
            Key={"pk": {"S": f"{consumer}#{event_id}"}},
            UpdateExpression="SET #s = :c, outcome = :o REMOVE locked_until",
            ExpressionAttributeNames={"#s": "status"},
            ExpressionAttributeValues={":c": {"S": "COMPLETED"}, ":o": {"S": outcome[:1000]}},
        )

    def release(self, event_id: str, consumer: str) -> None:
        """Drops a failed attempt's claim so a retry can process the event."""
        self.client.delete_item(TableName=self.table, Key={"pk": {"S": f"{consumer}#{event_id}"}})
