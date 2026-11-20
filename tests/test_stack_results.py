"""Stack org routing (2026-11-20): results for jobs AXXESS started go to POST /api/stack/results,
where AXXESS files them into the organization that owns the job. Never through the Tenant 0 agent key."""

import pytest
from test_agent_calls import JOB, agent_call, call_data, tool_calls  # noqa: F401 -- agent_call is a fixture
from test_flow import completion, completion_data, wired  # noqa: F401 -- completion and wired are fixtures

from still import handlers
from still.adapters.axxess import AxxessError, AxxessResultsClient

MSG_JOB = "44444444-5555-6666-7777-888888888888"
ORG = "33333333-4444-5555-6666-777777777777"


@pytest.fixture
def results_route(wired):
    """Configures the results client against the same fake AXXESS server, as the deployed adapter
    would with AXXESS_STACK_RESULTS_URL and STACK_RESULTS_SECRET_ARN set."""
    _, axxess, _ = wired
    handlers._cache["results"] = AxxessResultsClient(axxess["url"].replace("/api/agents/mcp", "/api/stack/results"), "results-secret")
    return axxess


def routed(axxess):
    return [r for r in axxess["requests"] if "params" not in r["body"]]


def test_a_call_result_goes_to_the_results_route_not_the_agent_key(agent_call, results_route):
    deliver, axxess = agent_call
    result = deliver(call_data(accepted_slot=None))
    assert result["outcome"].startswith("routed:")
    assert not any("params" in r["body"] for r in axxess["requests"]), "no MCP call with the Tenant 0 key"
    [request] = routed(axxess)
    assert request["auth"] == "Bearer results-secret"
    assert request["body"]["type"] == "call.completed.v1"
    assert request["body"]["data"]["job_id"] == JOB


def test_an_accepted_demo_is_reported_not_booked_in_the_wrong_organization(agent_call, results_route):
    deliver, axxess = agent_call
    result = deliver(call_data())
    assert result["outcome"].endswith(",meeting_not_booked")
    assert all("params" not in r["body"] for r in axxess["requests"]), "no create_meeting through the Tenant 0 key"


def test_a_job_conversation_goes_to_the_results_route_without_contact_details(completion, results_route):
    deliver, axxess = completion
    result = deliver(completion_data(job_id=MSG_JOB, organization_id=ORG))
    assert result["outcome"].startswith("routed:")
    [request] = axxess["requests"]
    body = request["body"]
    assert body["type"] == "conversation.completed.v1"
    assert body["data"]["job_id"] == MSG_JOB
    assert "customer_phone" not in body["data"] and "customer_email" not in body["data"]


def test_a_redelivered_job_result_is_a_duplicate(completion, results_route):
    deliver, axxess = completion
    deliver(completion_data(job_id=MSG_JOB))
    assert deliver(completion_data(job_id=MSG_JOB)) == {"outcome": "duplicate"}
    assert len(axxess["requests"]) == 1


def test_a_job_conversation_is_refused_when_the_route_is_not_configured(completion, wired):
    deliver, axxess = completion
    handlers._cache["results"] = None
    with pytest.raises(AxxessError) as error:
        deliver(completion_data(job_id=MSG_JOB))
    assert error.value.retryable is False
    assert axxess["requests"] == [], "a customer's result must never fall back to the Tenant 0 key"


def test_a_conversation_without_a_job_keeps_the_tenant_0_path(completion, results_route):
    deliver, axxess = completion
    assert deliver(completion_data())["outcome"].startswith("task:")
    [request] = axxess["requests"]
    assert request["body"]["params"]["name"] == "create_task"


def test_without_the_route_a_call_result_keeps_the_agent_key_path(agent_call, wired):
    deliver, axxess = agent_call
    handlers._cache["results"] = None
    deliver(call_data(accepted_slot=None))
    assert [name for name, _ in tool_calls(axxess)] == ["log_agent_call"]


@pytest.mark.parametrize("status, retryable", [(404, False), (409, False), (403, False), (401, False), (503, True), (429, True)])
def test_refusals_are_final_and_outages_are_retried(completion, results_route, wired, status, retryable):
    deliver, axxess = completion
    _, _, store = wired
    axxess["status"] = status
    with pytest.raises(AxxessError) as error:
        deliver(completion_data(job_id=MSG_JOB))
    assert error.value.retryable is retryable
    assert store.items == {}, "the claim is released so a retry or redrive can run again"


def test_a_malformed_job_id_is_rejected_by_the_schema(completion, results_route):
    deliver, axxess = completion
    from still.envelope import InvalidEvent
    with pytest.raises(InvalidEvent):
        deliver(completion_data(job_id="not-a-uuid"))
    assert axxess["requests"] == []
