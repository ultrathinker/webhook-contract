"""Example: a delivery-safe adapter for the example invoice contract.

Replace replay_hooks.py with this file to see a passing replay run.
Compare with replay_hooks_naive.py to see what the harness catches.

Contract assumptions (see examples/webhook-contract.json):
  event type   invoice.paid
  ordering key data.object.id          (one invoice = one entity)
  sequence     data.object.attempt     (higher attempt is newer)
  side effects mark_invoice_paid (idempotent), send_receipt_email (guarded)
"""

import json


class Probe:
    def __init__(self):
        self.calls = []
        self.state = {}
        self.seen_events = set()

    def reset(self):
        self.calls.clear()
        self.state.clear()
        self.seen_events.clear()

    def record(self, effect, detail=None):
        self.calls.append((effect, detail))

    def counts(self):
        result = {}
        for name, _ in self.calls:
            result[name] = result.get(name, 0) + 1
        return result

    def snapshot(self):
        return {"invoices": {key: dict(value) for key, value in self.state.items()}}


PROBE = Probe()


def handle(raw_body, headers):
    # Parse defensively: a malformed body must be a controlled 400, not a crash.
    try:
        event = json.loads(raw_body)
    except (TypeError, ValueError):
        return 400
    if not isinstance(event, dict):
        return 400

    # Unknown event types are acknowledged and ignored. Answering non-2xx
    # makes the provider retry, and an endpoint that keeps failing risks
    # being disabled.
    if event.get("type") != "invoice.paid":
        return 200

    # Idempotency boundary #1: one application per event id (duplicates, retries).
    event_id = event.get("id")
    if event_id in PROBE.seen_events:
        return 200

    invoice = event.get("data", {}).get("object", {})
    invoice_id = invoice.get("id")

    # Idempotency boundary #2: only apply an attempt newer than what is stored
    # (out-of-order arrivals). In a real handler this is a conditional UPDATE.
    current = PROBE.state.get(invoice_id)
    if current is not None and invoice.get("attempt", 0) <= current["attempt"]:
        return 200

    # Side effects, in contract order, both guarded by the checks above.
    # The marker is committed below, after the effects: in this in-process toy
    # nothing can crash between two plain writes, so it is indistinguishable
    # from atomic. In a real handler, the dedupe marker and the state change
    # belong in ONE transaction (effects outside your DB - emails, charges -
    # need downstream idempotency or an outbox: the marker cannot un-send
    # them). If you write the marker before the effects instead, give it a
    # processing/done state or a lease, so a crashed attempt is not stuck as
    # "seen" forever.
    PROBE.record("mark_invoice_paid", invoice_id)
    PROBE.state[invoice_id] = {
        "status": "paid",
        "amount_paid": invoice.get("amount_paid"),
        "attempt": invoice.get("attempt"),
    }
    PROBE.record("send_receipt_email", invoice_id)
    PROBE.seen_events.add(event_id)

    # On the retry scenario's first delivery the harness discards this 200 -
    # the lost response that makes the provider redeliver. The handler needs
    # no special casing: a retry after a lost response looks exactly like the
    # duplicate above, and the dedupe marker above handles both.
    return 200
