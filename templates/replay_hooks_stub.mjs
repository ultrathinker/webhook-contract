/**
 * Side-effect probe + handler adapter for the webhook contract replay harness.
 * This is the ONLY file you need to edit. See replay_hooks.py docstring for
 * the full wiring guide; the same rules apply here.
 *
 * Signature verification is intentionally left to you: do it inside handle()
 * with your provider SDK before any contract logic runs.
 */

const MARKER = "WEBHOOK-CONTRACT:TODO"; // check warns until this marker disappears

export class Probe {
  constructor() {
    this.calls = [];
    this.state = {};
  }
  reset() {
    this.calls.length = 0;
    this.state = {};
    // If you keep a dedupe store at module level, reset it here too.
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
    // A COPY, on purpose: the harness serializes the snapshot of the violated
    // run BEFORE the natural-order run; a live reference would be reset() under
    // it and both sides would compare equal forever.
    return JSON.parse(JSON.stringify(this.state));
  }
}

export const PROBE = new Probe();

/**
 * Run ONE delivery through your real webhook logic. May be sync or `async` -
 * the harness awaits the result either way. The retry scenario's first
 * delivery is an ordinary call: the harness discards that response to
 * simulate the lost response, so handle() needs no special casing for it.
 * @param {string} rawBody  raw request body (may be invalid JSON)
 * @param {Record<string, string>} headers
 * @returns {number|Promise<number>} the HTTP status code you would respond with
 */
export function handle(rawBody, headers) {
  throw new Error("Wire your handler here. " + MARKER + " See comments above.");
}
