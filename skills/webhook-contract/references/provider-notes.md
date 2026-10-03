# Provider delivery behavior (cheat sheet)

How the big providers actually deliver events, and how to map that onto the
contract. Each claim below was checked against the provider's docs at some
point, but docs change - re-verify a detail before betting production on it.
The safe posture for every provider is the same:

> Assume at-least-once delivery with no ordering guarantee. Dedupe on the
> event id, guard every side effect, ack unknown types, and never 5xx on
> payloads you cannot parse.

## Stripe (`provider: "stripe"`)

- Retries failed deliveries for up to **3 days** with exponential backoff in
  live mode (in sandbox: a few retries over a few hours).
- Anything outside 2xx counts as failure - including redirects (3xx), 4xx and
  timeouts. Their docs recommend returning 2xx quickly, before any complex
  logic.
- Duplicates come in two classes. Class 1: the redeliveries above. Class 2:
  Stripe occasionally generates **two separate Event objects** for one
  occurrence - to catch those, dedupe on the id of the object in
  `data.object` together with `event.type`, not only on the event id.
- Ordering is not guaranteed. Stripe explicitly says not to use `created` to
  determine event order - track event ids instead.
- Stripe documents only `Stripe-Signature` on deliveries; there is
  **no per-attempt delivery-id header**. (`Stripe-Context` and
  `Stripe-Version` belong to API requests and responses, not to deliveries.)
- Contract mapping: `fields.id: body.id`, `fields.type: body.type`,
  `ordering_key: body.data.object.id`. Set `fields.delivery_id:
  header.x-delivery-id` explicitly - it is a synthetic header, since
  Stripe's real envelope has no delivery id to map: the harness mints a
  fresh attempt id per delivery, which is exactly the behavior Stripe's
  real envelope does not let you observe. Your handler must not key
  anything on a delivery-attempt id for Stripe.

## GitHub (`provider: "github"`)

- The event name arrives in the `X-GitHub-Event` header; the body carries an
  `action` field (e.g. event `pull_request`, action `opened`). There is no
  event-type field in the body unless you add one.
- `X-GitHub-Delivery` is a GUID that identifies **the event**, not the
  attempt: a redelivery of the same event carries the same GUID. It is the
  natural dedupe key.
- GitHub does not automatically redeliver failed deliveries - but manual
  redelivery (UI/API, past 3 days of deliveries) and replay tooling are
  routine operator actions, so dedupe is still mandatory.
- Contract mapping: `fields.id: header.x-github-delivery`,
  `fields.type: header.x-github-event`,
  `fields.delivery_id: header.x-github-delivery` (same path - the harness
  then models a redelivery as the same id, matching the provider).
  `ordering_key`: usually `body.repository.id` plus the entity id - pick the
  entity the handler mutates.
- GitHub ships no per-entity sequence field, so the order scenarios have
  nothing honest to order by - skip them per event (`skip_scenarios`) and
  say why in `notes`. See `examples/webhook-github.json`.

## Shopify (`provider: "shopify"`)

- Shopify does not guarantee ordering **within a topic, or across topics for
  the same resource** - their docs' own example is `products/update`
  arriving before `products/create`. If you need order, compare
  `X-Shopify-Triggered-At` or the resource's `updated_at`.
- Shopify recommends ignoring duplicate deliveries using
  `X-Shopify-Webhook-Id`. Topic in `X-Shopify-Topic`, HMAC in
  `X-Shopify-Hmac-Sha256`.
- The delivery page no longer states retry conditions or a retry duration;
  the widely-circulated numbers (~19 attempts over ~2 days) are historical
  and community-documented. Verify against your own app before relying on
  them.
- Contract mapping: `fields.id: header.x-shopify-webhook-id`,
  `fields.type: header.x-shopify-topic`,
  `fields.delivery_id: header.x-shopify-webhook-id` (same path as
  `fields.id` - a redelivery keeps its id, matching the provider),
  `ack.malformed: [400]`.

## Slack (`provider: "slack"`)

- The Events API retries a failed request **up to 3 times**: nearly
  immediately, after ~1 minute, after ~5 minutes. The `x-slack-retry-num`
  and `x-slack-retry-reason` headers tell you which attempt you are on.
  Anything outside the 200 series counts as failure (up to two redirects
  are followed).
- `body.event_id` is documented as globally unique. Dedupe on it - the docs
  do not spell the recommendation out, but a redelivery after a failed
  request is exactly the situation that makes it necessary. (The retry
  window is minutes, unlike Stripe's days.)
- The first call after subscribing is a `url_verification` challenge your
  endpoint must answer - a second "unknown type" you must ack without side
  effects.
- Contract mapping: `fields.id: body.event_id`, `fields.type: body.event.type`,
  `ordering_key`: e.g. `body.event.channel` + entity - usually weak; if there
  is no meaningful key, disable the order scenarios for those events and say
  so in `notes`.

## Generic / custom senders

If you control the sender: deliver at-least-once on purpose (safer to
duplicate than to lose), put a UUID `id` in the body, a delivery-attempt id
in a header, document retry + ordering in the contract `notes`, and add a
`sequence`/`version` int per entity so consumers can order.

## What this plugin deliberately does NOT do

- Signature verification (HMAC per provider SDK) - it stays in your handler,
  in the adapter's `handle()`, before any contract logic.
- Sending anything anywhere: no test deliveries, no tunnel, no replay to a
  live endpoint. Replays call your function directly.
