/**
 * Example: the naive handler the harness is designed to catch (Node).
 * Fails all six scenarios - duplicate, retry_after_failure, out_of_order,
 * cross_key_interleave, unknown_type and malformed; see replay_hooks_naive.py
 * for the full breakdown.
 */

export class Probe {
  constructor() {
    this.calls = [];
    this.state = {};
  }
  reset() {
    this.calls.length = 0;
    this.state = {};
  }
  record(effect, detail = null) {
    this.calls.push([effect, detail]);
  }
  counts() {
    return this.calls.reduce((acc, [name]) => {
      acc[name] = (acc[name] ?? 0) + 1;
      return acc;
    }, {});
  }
  snapshot() {
    return { invoices: { ...this.state } };
  }
}

export const PROBE = new Probe();

export function handle(rawBody, headers) {
  const event = JSON.parse(rawBody); // crashes on malformed bodies -> 500

  if (event.type !== "invoice.paid") return 500; // unknown types become "errors" -> the provider retries; a failing endpoint risks being disabled

  const invoice = event.data.object;

  // No dedupe, no ordering check: every delivery applies everything again.
  PROBE.record("mark_invoice_paid", invoice.id);
  PROBE.state[invoice.id] = {
    status: "paid",
    amount_paid: invoice.amount_paid,
    attempt: invoice.attempt,
  };
  PROBE.record("send_receipt_email", invoice.id);

  return 200;
}
