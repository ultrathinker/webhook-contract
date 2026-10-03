/**
 * Example: the delivery-safe adapter with an ASYNC handler (Node).
 *
 * Most real handlers are async (Express route wrapping an async service,
 * Fastify, Hapi). Replace replay_hooks.mjs with this file to see that the
 * replay harness supports async handlers exactly like sync ones - it awaits
 * the result of handle(), so a rejected promise counts as a 500, which is
 * what the provider would see. Logic is identical to replay_hooks_golden.mjs.
 */

export class Probe {
  constructor() {
    this.calls = [];
    this.state = {};
    this.seenEvents = new Set();
  }
  reset() {
    this.calls.length = 0;
    this.state = {};
    this.seenEvents.clear();
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
    // A COPY: the harness serializes the snapshot before the reference run.
    const invoices = {};
    for (const [key, value] of Object.entries(this.state)) {
      invoices[key] = { ...value };
    }
    return { invoices };
  }
}

export const PROBE = new Probe();

export async function handle(rawBody, headers) {
  // Parse defensively: a malformed body must be a controlled 400, not a crash.
  let event;
  try {
    event = JSON.parse(rawBody);
  } catch {
    return 400;
  }
  if (typeof event !== "object" || event === null) return 400;

  // Unknown event types are acknowledged and ignored; answering non-2xx
  // makes the provider retry, and an endpoint that keeps failing risks
  // being disabled.
  if (event.type !== "invoice.paid") return 200;

  // Idempotency boundary #1: one application per event id (duplicates, retries).
  if (PROBE.seenEvents.has(event.id)) return 200;

  const invoice = event.data?.object ?? {};
  const invoiceId = invoice.id;

  // Idempotency boundary #2: only apply an attempt newer than what is stored
  // (out-of-order arrivals). In a real handler this is a conditional UPDATE.
  const current = PROBE.state[invoiceId];
  if (current !== undefined && (invoice.attempt ?? 0) <= current.attempt) return 200;

  // In a real async handler the effects are awaited calls, e.g.
  //   await db.query("UPDATE invoices SET ..."); await mailer.send(receipt)
  // The probe wrap stays a plain sync call around each awaited effect.
  await Promise.resolve(); // stand-in for your real async I/O

  // Side effects, in contract order, both guarded by the checks above.
  PROBE.record("mark_invoice_paid", invoiceId);
  PROBE.state[invoiceId] = {
    status: "paid",
    amount_paid: invoice.amount_paid,
    attempt: invoice.attempt,
  };
  PROBE.record("send_receipt_email", invoiceId);
  PROBE.seenEvents.add(event.id);

  // On the retry scenario's first delivery the harness discards this 200 -
  // the lost response that makes the provider redeliver. The handler needs
  // no special casing: a retry after a lost response looks exactly like the
  // duplicate above, and the dedupe marker above handles both.
  return 200;
}
