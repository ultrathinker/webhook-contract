# Contract schema reference (`webhook-contract.json` v1)

All paths are **delivery-rooted**: they start at the delivery object and
descend through `body.<field>` or `header.<name>`. Example: `body.data.object.id`
on a Stripe-shaped payload, `header.x-github-delivery` for a GitHub delivery id.

## Top level

| Field | Type | Meaning |
| --- | --- | --- |
| `contract_version` | `1` | Schema version. |
| `provider` | string | Free text: `stripe`, `github`, `shopify`, or anything. |
| `notes` | string | Anything worth remembering about this endpoint. |
| `fields.id` | path | Where the provider's event id lives (dedupe key). Default `body.id`. |
| `fields.type` | path | Where the event type lives. Default `body.type`. |
| `fields.delivery_id` | path | Delivery-attempt id (differs on every retry). When it is the same path as `fields.id`, the event identity IS the delivery identity and a redelivery keeps the value (GitHub-style). Default `header.x-delivery-id`. |
| `ack.success` | int list | Statuses a successfully processed event may return. Default `[200]`. |
| `ack.ignored` | int list | Statuses for unknown-but-valid events. Default `[200]`. |
| `ack.malformed` | int list | Statuses for unparseable bodies. Default `[400]`. |
| `sequence_field` | body path or null | Field that orders events for one entity (monotonic int), e.g. `body.data.object.attempt`. Required when order scenarios are enabled. |
| `scenarios` | string list | Which scenarios to generate (subset of the six below). |
| `events` | list | One entry per event type (see below). |

## Event entry

| Field | Type | Meaning |
| --- | --- | --- |
| `type` | string | The event type name as the provider sends it, e.g. `invoice.paid`. Unique. |
| `description` | string | When the provider fires this event. |
| `ordering_key` | body path | Which entity the event belongs to, e.g. `body.data.object.id`. Required for order scenarios. |
| `payload` | object | ONE synthetic sample body. |
| `ordered_payloads` | list of 1-2 objects or null | The older and the newer sample body, in natural order (ascending `sequence_field`); used by order scenarios so the "older"/"newer" events differ in content, not just sequence. More than two entries is an error - the scenarios need exactly a pair. If null, gen clones `payload` and bumps the sequence value. The two entries may carry different `type` values: that is how you cover cross-type state transitions - e.g. a stale `payment_failed` that must not overwrite a newer `paid` - by declaring them as one event's ordered pair with `ordering_key` set to the shared entity. |
| `side_effects` | list | Every write the handler performs for this event. |
| `skip_scenarios` | list | Scenarios to skip for this event (rarely justified; each skip is a blind spot). |

### Side effect entry

```json
{
  "name": "send_receipt_email",
  "idempotent": false,
  "guard": "Sent only when the status transition row updates; the WHERE clause prevents a second email.",
  "optional": true
}
```

- `name` must match what `PROBE.record()` is called with in the adapter -
  the harness budgets are keyed by these names.
- `idempotent` states whether repeating the call is inherently safe.
- `guard` states the MECHANISM that makes repeating it safe (or why none is
  needed). If you cannot name a mechanism, the handler is not ready.
- `optional: true` marks an effect that fires only under a condition (a
  threshold, a status branch) and may legitimately not fire in a scenario:
  the generated floors become 0 for it while the budgets still cap it.
  Without it, every declared effect must fire at least once per scenario.

## Scenarios (what `gen` produces per event)

| Scenario | Deliveries | Catches |
| --- | --- | --- |
| `duplicate` | same event twice, different delivery ids (same id when the delivery id is the event identity, GitHub-style) | missing event-id dedupe |
| `retry_after_failure` | same event, first attempt flagged `fail_attempt` - the harness discards that response, the provider "never saw" it | a redelivery after a lost response repeating effects - no dedupe marker, or checked after the effects |
| `out_of_order` | two events, same key, newest arrives first | stale writes overwriting newer state |
| `cross_key_interleave` | four events, two keys, one key inverted | global state, key cross-talk |
| `unknown_type` | one event with an unregistered type | unknown types crashing or erroring (a failing endpoint risks being disabled by the provider) |
| `malformed` | one delivery whose body is not JSON | parse errors escaping as 5xx, partial effects before parse |

### The ordering model, and its limits

The order scenarios define "natural order" as: group deliveries by
`ordering_key`, sort each group ascending by the integer in
`sequence_field`. That is all the model knows - no timestamps, no
provider-specific partial order, no tiebreak beyond delivery order in the
fixture.

Be honest about where the sequence comes from. None of the documented
providers ships an integer per-entity sequence you can order by (Stripe
explicitly says not to use `created`; GitHub, Shopify and Slack have
nothing). The example contract invents `body.data.object.attempt` - fine for
a synthetic exercise, and something you must earn in production (your own
version column, an outbox sequence). If you cannot name an integer field
that is monotonic per entity, skip the order scenarios for that event and
say so in `notes`.

## Expectations written into each fixture

- `statuses`: allowed status codes (from the `ack` table).
- `effect_min`: minimum `PROBE.record()` calls per effect for the whole
  scenario run - the floor that stops a do-nothing handler from passing
  (1 for an effect the scenario must apply, 0 when it may legitimately not
  fire). Every effect with a positive budget carries a positive floor.
- `effect_budget`: maximum `PROBE.record()` calls per effect for the whole
  scenario run (1 for duplicate/retry, 0 for unknown/malformed, n for order
  scenarios).
- `reference: {"order": "natural"}` on order scenarios: the harness re-runs
  the same deliveries in natural (key, sequence) order against a fresh probe
  and requires the final `PROBE.snapshot()` to be identical.

## Delivery metadata (generated, do not hand-edit)

Each delivery carries `seq`, `delay_ms`, `headers`, `body` / `raw_body`,
`fail_attempt`, `order_key`, `order_seq`. `delay_ms` documents realistic
gaps (e.g. 5 minutes between duplicate deliveries) but the harness does not
sleep: in-process, a delayed redelivery is just the next delivery in line.
