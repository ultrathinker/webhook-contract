"""Side-effect probe + handler adapter for the webhook contract replay harness.

This is the ONLY file you need to edit. It is the bridge between the synthetic
deliveries in fixtures/ and your real webhook code.

What the harness does with this file, per scenario:
  1. calls PROBE.reset()
  2. replays each fixture delivery through handle()
  3. asserts the returned status codes and PROBE.counts() against the fixture
     expectations, and (for order scenarios) compares PROBE.snapshot() with a
     natural-order reference run.

Wiring guide:
  * handle() should call your real entry point (parse body, verify signature
    with your provider SDK, dispatch, apply effects) and return the HTTP
    status code you would respond with. It may be a plain function or an
    `async def`; the harness awaits awaitable results either way.
  * make your side-effect layer (db wrapper, mailer, queue client) call
    PROBE.record(effect_name) so the harness can count applications.
  * PROBE.snapshot() must return a JSON-serializable view of the FINAL state
    the effects produced (e.g. {"invoices": {...}}), as a FRESH object - not
    a live reference to internal state. Order-equivalence compares snapshots
    between two runs, so it must reflect state, not calls.
  * the retry scenario's first delivery is an ordinary call: the harness
    discards that response to simulate the lost response, so handle() needs
    no special casing for it.

Signature verification is intentionally left to you: do it inside handle()
with your provider SDK before any contract logic runs.
"""

import copy

_MARKER = "WEBHOOK-CONTRACT:TODO"  # check warns until this marker disappears


class Probe:
    """Records side-effect calls and the final state they produced."""

    def __init__(self):
        self.calls = []
        self.state = {}

    def reset(self):
        self.calls.clear()
        self.state.clear()
        # If you keep a dedupe store at module level, reset it here too.

    def record(self, effect, detail=None):
        self.calls.append((effect, detail))

    def counts(self):
        result = {}
        for name, _ in self.calls:
            result[name] = result.get(name, 0) + 1
        return result

    def snapshot(self):
        # A COPY, on purpose: the harness serializes the snapshot of the
        # violated run BEFORE the natural-order run; a live reference would
        # be reset() under it and both sides would compare equal forever.
        return copy.deepcopy(self.state)


PROBE = Probe()


def handle(raw_body, headers):
    """Run ONE delivery through your real webhook logic.

    Return the HTTP status code you would respond with (int). May be a plain
    function or `async def` - the harness awaits awaitable results.
    """
    raise NotImplementedError(
        "Wire your handler here. " + _MARKER + " See the docstring at the top of this file."
    )
