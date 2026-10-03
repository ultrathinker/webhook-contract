"""Example: the naive handler the harness is designed to catch.

Replace replay_hooks.py with this file and the replay run FAILS all six
scenarios: duplicate and retry_after_failure (no dedupe - effects fire
twice), out_of_order and cross_key_interleave (last write wins by arrival -
stale state survives), unknown_type and malformed (both answered 500, which
makes the provider retry, and an endpoint that keeps failing risks being
disabled).

This is how most first webhook handlers are written: optimistic, happy-path
only. Providers deliver at-least-once, so this code double-sends emails,
resurrects stale state and 500s on bodies it does not understand.
"""

import json


class Probe:
    def __init__(self):
        self.calls = []
        self.state = {}

    def reset(self):
        self.calls.clear()
        self.state.clear()

    def record(self, effect, detail=None):
        self.calls.append((effect, detail))

    def counts(self):
        result = {}
        for name, _ in self.calls:
            result[name] = result.get(name, 0) + 1
        return result

    def snapshot(self):
        return {"invoices": dict(self.state)}


PROBE = Probe()


def handle(raw_body, headers):
    event = json.loads(raw_body)  # crashes on malformed bodies -> 500

    if event.get("type") != "invoice.paid":
        return 500  # unknown types become "errors" -> the provider retries; a failing endpoint risks being disabled

    invoice = event["data"]["object"]

    # No dedupe, no ordering check: every delivery applies everything again.
    PROBE.record("mark_invoice_paid", invoice["id"])
    PROBE.state[invoice["id"]] = {
        "status": "paid",
        "amount_paid": invoice["amount_paid"],
        "attempt": invoice["attempt"],
    }
    PROBE.record("send_receipt_email", invoice["id"])

    return 200
