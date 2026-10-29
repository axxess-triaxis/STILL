# Plugging a new product into STILL

A product can produce events, consume them, or both. Nothing in the other
products changes when a new one joins.

## To produce events

1. Pick a source name `triaxis.<product>` and add the product to
   `src/still/registry.json` with the event types it produces.
2. Add one JSON Schema per event type at
   `src/still/schemas/triaxis.<product>/<event>.v<N>.json`. Event names are lower-case
   dotted, past tense, versioned: `booking.confirmed.v1`. A breaking change is a new
   version (`.v2`), published alongside the old one until consumers move.
3. `pytest` fails if a registered event has no schema or a schema is not registered.
4. In the product, copy `src/still/emit.py` and call `emit(source, type, data, key=...)`
   where `key` identifies the product's own item (so retries dedupe). Set
   `STILL_EVENT_BUS_ARN` and attach the stack's `ProducerPolicyArn` to its role.
   Products outside AWS (AXXESS on Vercel) need an IAM role or user with that policy.

## To consume events

1. Write an adapter in `src/still/adapters/<product>.py` that turns an envelope into
   a call on the product's existing API. Classify failures as retryable or not.
2. Add a handler in `src/still/handlers.py`: validate with `envelope.from_eventbridge`,
   re-check the filter, claim the event id with `DynamoIdempotency`, call the adapter,
   log ids and outcomes only (never event data).
3. In `template.yaml`, add a function, a rule with an event pattern, a `RetryPolicy`
   and `DeadLetterConfig` on the target, and an `OnFailure` SQS destination.
   `tests/test_template.py` enforces those.
4. Add a flow test like `tests/test_flow.py`.
