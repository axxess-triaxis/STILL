import boto3
import pytest
from botocore.exceptions import ClientError
from botocore.stub import ANY, Stubber

from still.idempotency import LOCK_SECONDS, AlreadyInProgress, DynamoIdempotency


@pytest.fixture
def stubbed():
    client = boto3.client("dynamodb", region_name="ap-southeast-2")
    with Stubber(client) as stub:
        yield DynamoIdempotency("table", client=client, clock=lambda: 1000), stub


def conditional_failure(item):
    return {"service_error_code": "ConditionalCheckFailedException", "service_message": "failed",
            "http_status_code": 400, "response_meta": {}, "modeled_fields": {"Item": item}}


def test_first_claim_wins_with_lock_and_ttl(stubbed):
    store, stub = stubbed
    stub.add_response("put_item", {}, {
        "TableName": "table",
        "Item": {"pk": {"S": "axxess#e1"}, "status": {"S": "IN_PROGRESS"},
                 "locked_until": {"N": str(1000 + LOCK_SECONDS)}, "expires_at": {"N": ANY}},
        "ConditionExpression": ANY, "ExpressionAttributeNames": ANY,
        "ExpressionAttributeValues": {":ip": {"S": "IN_PROGRESS"}, ":now": {"N": "1000"}},
        "ReturnValuesOnConditionCheckFailure": "ALL_OLD",
    })
    assert store.claim("e1", "axxess") is True


def test_completed_event_is_a_duplicate(stubbed):
    store, stub = stubbed
    stub.add_client_error("put_item", **conditional_failure({"pk": {"S": "axxess#e1"}, "status": {"S": "COMPLETED"}}))
    assert store.claim("e1", "axxess") is False


def test_fresh_in_progress_claim_raises_so_lambda_retries(stubbed):
    store, stub = stubbed
    stub.add_client_error("put_item", **conditional_failure({"pk": {"S": "axxess#e1"}, "status": {"S": "IN_PROGRESS"}}))
    with pytest.raises(AlreadyInProgress):
        store.claim("e1", "axxess")


def test_other_errors_propagate(stubbed):
    store, stub = stubbed
    stub.add_client_error("put_item", service_error_code="ProvisionedThroughputExceededException")
    with pytest.raises(ClientError):
        store.claim("e1", "axxess")


def test_complete_and_release(stubbed):
    store, stub = stubbed
    stub.add_response("update_item", {}, {
        "TableName": "table", "Key": {"pk": {"S": "axxess#e1"}}, "UpdateExpression": ANY,
        "ExpressionAttributeNames": ANY,
        "ExpressionAttributeValues": {":c": {"S": "COMPLETED"}, ":o": {"S": "task:t1"}},
    })
    stub.add_response("delete_item", {}, {"TableName": "table", "Key": {"pk": {"S": "axxess#e2"}}})
    store.complete("e1", "axxess", "task:t1")
    store.release("e2", "axxess")
