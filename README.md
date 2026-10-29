# STILL

STILL lets Triaxis Ventures' products talk to each other: AXXESS TRIaxis, Callismatic,
Omnidamus, and whatever is built next. Products publish versioned events to one
Amazon EventBridge bus. Small adapters turn those events into calls on each
receiving product's existing API.

Design chosen by the founder on 2026-10-28 (Design A, EventBridge bus). The research and
the alternative considered (a direct API router) are in
`side-session-notes/.../STILL-design-options-2026-10-28.md`, summarized under
[Why an event bus](#why-an-event-bus).

## Where STILL fits in Triaxis Ventures

This is the founder's account of the business (2026-10-29). Unless a source is linked, it is
*Founder-stated*.

- **AXXESS TRIaxis** is the product Triaxis Ventures sells: an enterprise AI operating platform
  for regulated organizations.
- **PORT4LLEO** automates governance, security operations, builder analysis and product
  improvements (live at https://port4leo-sigma.vercel.app).
- **Callismatic** automates sales, marketing and cold calls.
- **Omnidamus** automates SMS and WhatsApp outreach and customer service.
- **STILL** (this repo) harnesses them into one stack, so what one product learns becomes
  work in another.

**Tenant 0.** Triaxis Ventures is the dogfooded tenant on every one of these apps, AXXESS
TRIaxis included. Tenant 0 is where the founder does QA, audits, iteration, UX testing,
walkthroughs, and improvements driven by pilot and survey feedback. The goal is to run Triaxis
Ventures as a roughly 80% automated startup with the founder as the human in the loop: AXXESS
builds, sells, does customer service, QA and marketing outreach on itself, through these
products. The intent is to make the company a repeatable, very low-cost, lean prototype of an
AI-native startup.

**Cost claim.** The founder states that every product except AXXESS TRIaxis was built at zero
incremental cost (*Founder-stated, source artifact needed*). Known running costs are small and
come out of AWS credits:
- STILL is estimated at under USD 2 a month (see [Cost](#cost)).
- Omnidamus keeps a Route 53 hosted zone at USD 0.50 a month.
- Bedrock (Amazon Nova) is billed per token for Callismatic's fallback and Omnidamus's agent.

The first STILL flows are Tenant 0 in practice: Triaxis's own Callismatic leads and Omnidamus
conversations become tasks in Triaxis's own AXXESS Live Beta org.

## Status, said plainly

| | State |
|---|---|
| Event envelope, schema registry, Callismatic `lead.triaged.v1` schema | **Built, tested** |
| Producer helper `src/still/emit.py` (copyable into any product) | **Built, tested** |
| AXXESS adapter: Callismatic lead -> AXXESS `create_task` over AXXESS's MCP endpoint | **Built, tested** against a local fake of the AXXESS MCP endpoint |
| Duplicate protection (DynamoDB, per event id) | **Built, tested** with a stubbed DynamoDB |
| Infrastructure as code (`template.yaml`, AWS SAM) | **Built**, passes `cfn-lint` and `sam validate --lint` |
| Deployed to AWS | **Done 2026-10-29** with the founder's go-ahead: stack `still`, ap-southeast-2, account 227214487086, CREATE_COMPLETE 10:43:47 UTC. Checked read-only afterwards: bus `still`; archive ENABLED, 30 days; rule `still-AxxessLeadRule-hm85jbYYNORD` ENABLED with the lead pattern; its target is the adapter, with 20 retries and the delivery DLQ; function Active, python3.12, x86_64, `AXXESS_MCP_URL=https://landing.triaxisventures.com/api/agents/mcp`; 2 async retries to the failure queue; DynamoDB TTL `expires_at` ENABLED; both alarms OK. **Redeployed 2026-10-29** (UPDATE_COMPLETE; function LastModified 11:24:59 UTC): added `still-AxxessCompletionRule-UBDMX3gz7n7s` (ENABLED, Omnidamus `conversation.completed.v1`, same adapter, 20 retries, delivery DLQ). The adapter's invoke permissions cover both rules |
| Callismatic publishing events | **Deployed 2026-10-29, end-to-end check pending.** callismatic#8 merged as `ae4ddcb`. STILL `ProducerPolicy` is attached to `callismatic-lambda-role`. Image `callismatic@sha256:d60b2c55...` went live at 12:21:43 UTC; the rollback is `sha256:a930e909...`. `STILL_EVENT_BUS_ARN` is set, alongside the existing `CALLISMATIC_SECRET_ARN`. `/`, `/api/digest`, `/api/todos` and `/api/blocklist` return 200 after the deploy |
| Omnidamus -> AXXESS (`conversation.completed.v1` -> follow-up task) | **Both sides deployed 2026-10-29, end-to-end check pending.** STILL: schema, `AxxessCompletionRule`, adapter mapping, tests (63 total). Omnidamus#3 merged as `25bf1b3` and stack `omnidamus` redeployed with `StillEventBusArn` (function LastModified 12:32:15 UTC; all other parameters unchanged). Its role has `events:PutEvents` on the `still` bus only. The WhatsApp topic still delivers to the function, and the WhatsApp Business account still points at that topic. Rollback image tag: `omnidamusfunction-06f1efc3808d-python3.12-v1` |
| AXXESS agent API key for STILL | **Done 2026-10-29.** AXXESS Live Beta connection `f2315c70-be23-45b0-b184-679016f91c8e`: label STILL, provider anthropic (no STILL provider value exists yet), status active, capabilities exactly `["create_task"]` (PATCH response). The key is in the stack secret (version of 16:38:20 IST, 52 characters, format `axa_live_...` checked by the paste script; the value was never shown to the agent) |
| STILL -> AXXESS half, against real resources | **Verified 2026-10-29.** `scripts/send_test_event.py` put event `bfd8eeac-f12e-5687-9c77-a51de83bb26f` at 11:10:52Z. The adapter logged `outcome: task:8c01df21-47e1-49a1-a6f7-98c86dab7e7d`. The DynamoDB record is COMPLETED, both failure queues are at 0, and the task "STILL test: delete this task once seen." shows Pending on AXXESS `/tasks` |
| One real cross-product flow verified end to end (Callismatic -> AXXESS) | **Not done.** Needs the Callismatic change (steps 4-5 below) |
| Omnidamus live delivery | WhatsApp sending is still blocked on Meta's display-name review. Inbound and SES receipts work, so a completion can still happen |

The STILL -> AXXESS half works against real resources (see the table). Callismatic does not
publish yet, so no real Callismatic lead has gone through. The AXXESS MCP endpoint at `landing.triaxisventures.com` was confirmed live on
2026-10-29: an unauthenticated POST returns 401 "Missing Bearer API key."

## The first flow: a Callismatic lead becomes an AXXESS task

```
Callismatic (Lambda, us-east-1)
  triage says lead/important + needs_decision
  -> emit("triaxis.callismatic", "lead.triaged.v1")      [proposed change]
       |
       v
EventBridge bus "still" (ap-southeast-2) --- archive (30 days, replayable)
  rule: source=triaxis.callismatic, detail-type=lead.triaged.v1,
        category in [lead, important], needs_decision=true
       |  retries 20x / 1h, then delivery DLQ
       v
AxxessAdapterFunction (Lambda)
  validate envelope + schema -> claim event id (DynamoDB) ->
  POST {AxxessMcpUrl} tools/call create_task (Bearer agent key from Secrets Manager)
       |  2 async retries, then failure queue; alarms on both queues
       v
AXXESS task in the founder's org (audited by AXXESS's agent tool audit log)
```

The task title is Callismatic's `suggested_action`. Its description holds the summary,
the reason, the caller's number and key facts, plus the Callismatic item and STILL event
id for tracing. Urgency maps to priority. The same Callismatic item never makes two tasks.
If AXXESS ever marks `create_task` as critical, the call becomes an AXXESS approval request
instead, and STILL records that.

STILL's logs hold ids and outcomes only, never names, numbers or message text (a test
checks this).

## Event envelope

Every event's EventBridge `detail` is a CloudEvents-style envelope
(`src/still/schemas/envelope.v1.json`):

```json
{"specversion": "1.0", "id": "<uuid5 of source|type|item key>", "source": "triaxis.callismatic",
 "type": "lead.triaged.v1", "time": "2026-10-28T10:00:00Z", "data": { ... }}
```

`source` and `type` are copied to EventBridge `Source` and `DetailType` for routing.
`data` must match `src/still/schemas/<source>/<type>.json`, and every event must be
listed in `src/still/registry.json`. The id is derived from the producer's item key, so a
producer retry gives the same id.

Adding a product: see [docs/ADDING_A_PRODUCT.md](docs/ADDING_A_PRODUCT.md).

## Repo layout

```
template.yaml                  SAM: bus, archive, rule, adapter, DLQs, alarms, idempotency table, secret
src/still/envelope.py          envelope build/validate, registry
src/still/emit.py              producer helper (boto3 + stdlib only, copy into products)
src/still/idempotency.py       DynamoDB claim/complete/release per event id
src/still/adapters/axxess.py   lead -> create_task mapping, AXXESS MCP client
src/still/handlers.py          Lambda handler(s)
src/still/registry.json        products, what they produce/consume
src/still/schemas/             JSON Schemas
scripts/send_test_event.py     puts one synthetic lead on the deployed bus
tests/                         pytest
```

## Run the tests

```bash
py -m venv .venv
.venv/Scripts/python -m pip install -e ".[dev]"
.venv/Scripts/python -m pytest -q
.venv/Scripts/cfn-lint template.yaml
sam validate --lint --region ap-southeast-2
```

Last run (2026-10-29, Python 3.14 locally): `63 passed`; cfn-lint clean; `sam validate --lint`
valid; `sam build --use-container` succeeded (python3.12, x86_64). Tests never use real AWS
credentials (see `tests/conftest.py`).

The function is x86_64 rather than arm64 because the arm64 container build hung under emulation
on this Windows x86 machine. At this volume the price difference is negligible.

## Deploy and verify (each step needs the founder's go-ahead)

1. **Deploy STILL** (profile `triaxis-official`, account 227214487086, ap-southeast-2):
   `sam build --use-container` then
   `sam deploy --guided --region ap-southeast-2 --stack-name still --capabilities CAPABILITY_IAM`
   with `AxxessMcpUrl=https://<live AXXESS host>/api/agents/mcp` and optionally `AlarmEmail`.
2. **AXXESS key** (founder, in the AXXESS app): create an agent connection with only the
   `create_task` capability; copy the key once. In the AWS console, open Secrets Manager in
   ap-southeast-2, find the secret named in the stack output `AxxessApiKeySecretArn`,
   choose *Retrieve secret value*, then *Edit*, and paste the key as plaintext. The
   connection's provider will show as one of AXXESS's existing values (openai, anthropic,
   microsoft_copilot). A proper `triaxis_still` value needs an AXXESS migration (not done).
3. **Check STILL -> AXXESS alone:** `python scripts/send_test_event.py --bus-arn <EventBusArn>`
   creates one task titled "STILL test: delete this task once seen". Confirm it in AXXESS
   and in the adapter's CloudWatch logs.
4. **Connect Callismatic:** apply [docs/integrations/callismatic.md](docs/integrations/callismatic.md)
   (PR, review, redeploy), attach `ProducerPolicyArn`, set `STILL_EVENT_BUS_ARN`.
5. **End to end:** `POST /api/triage/text` on Callismatic (with its `X-API-Key`,
   `place_callbacks=false`) with a synthetic lead message; confirm the AXXESS task.

Rollback: unset `STILL_EVENT_BUS_ARN` on Callismatic (publishing stops at once);
`sam delete --stack-name still` removes STILL. The secret is then scheduled for deletion.

## Cost

Approximate list prices, to confirm before deploying: EventBridge custom events USD 1.00 per
million; the archive USD 0.10/GB; Lambda, SQS and DynamoDB on-demand at this volume are
within free tier or cents; one Secrets Manager secret USD 0.40/month; CloudWatch logs cents.
Expected **under USD 2/month**, from the account's AWS credits.

## Why an event bus

Research (2026-10-28) found that none of the three products emits events today. Each has its
own way in:
- AXXESS: an MCP server with per-agent keys and approval gating.
- Callismatic: an `X-API-Key` HTTP API and an MCP server. Its stores live in Lambda `/tmp`, so
  polling them is unreliable.
- Omnidamus: a direct Lambda invoke.

A bus lets each product publish once while adapters handle each receiver's API. It also gives
fan-out to several consumers, a 30-day replay archive and one place to trace cross-product
traffic. A direct router was simpler for one flow but grows point-to-point logic with every
product.

## Not done / known limits

- Nothing deployed or verified live (see Status).
- AXXESS (on Vercel) cannot publish yet; it would need an IAM identity with the producer
  policy.
- No Omnidamus flows yet. Candidates: Omnidamus booking confirmed -> AXXESS meeting/task;
  Callismatic lead -> Omnidamus WhatsApp follow-up (blocked by Meta today).
- The Lambda runtime is python3.12; the tests ran on Python 3.14 locally because no 3.12
  interpreter is installed on this machine.
- No GitHub repo yet; this is a local git repo only.
