# webhook-contract (Claude Code plugin)

[![ci](https://github.com/ultrathinker/webhook-contract/actions/workflows/ci.yml/badge.svg)](https://github.com/ultrathinker/webhook-contract/actions/workflows/ci.yml)
[![license: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)

Make your webhook handler survive how providers really deliver events.

If you receive webhooks from Stripe, GitHub, Shopify, Slack or anything
similar and you wrote the handler yourself, you live with two facts: delivery
is **at-least-once** (duplicates and retries are normal, guaranteed to happen),
and **ordering is not guaranteed** (events for the same invoice can arrive in
any order). The happy-path tests from the provider's quickstart pass while
these two facts break your code in production: a second receipt email, a
stale `payment_failed` overwriting a newer `paid`, an endpoint the provider
may disable because unknown event types made it return 500.

This plugin makes the handler's implicit assumptions explicit and then attacks
them - offline, in-process, with synthetic data.

## What it does

1. **Delivery contract.** You describe, per event type: the ordering key, the
   event id field, every side effect, and - the part that requires actual
   thinking - the guard that makes repeating each side effect safe. This lives
   in your repo as `webhook-contract.json`, next to your tests.

2. **Synthetic fixtures.** From the contract it deterministically generates
   replay fixtures for the six classic delivery failures:

   | Scenario | What it simulates |
   | --- | --- |
   | `duplicate` | same event delivered twice - a new delivery id each time, or the same id redelivered when the delivery id is the event identity (GitHub-style) |
   | `retry_after_failure` | first delivery's response is lost, provider retries - the handler must not apply twice |
   | `out_of_order` | two events for one entity, newest arrives first |
   | `cross_key_interleave` | two entities interleaved, one of them out of order |
   | `unknown_type` | an event type your handler has never seen |
   | `malformed` | a body that is not valid JSON |

   Synthetic values stay your job; `check` backs you up by rejecting
   real-looking emails (anything outside the reserved example/test domains),
   card numbers that pass the Luhn checksum without being a documented test
   card, live-key shapes and other secret-like strings.

3. **In-process replay.** Generated harnesses (Python `unittest`, zero
   dependencies; Node built-in test runner, zero dependencies) replay the
   fixtures by calling **your handler function directly** - sync or async -
   with no server, no listener, no tunnel, no network. A side-effect probe
   you wire to your real write paths lets the harness assert *properties*,
   not snapshots of outputs:

   - every delivery is answered with a status the provider accepts (a
     non-2xx answer makes providers retry, and an endpoint that keeps
     failing risks being disabled; an unhandled crash counts as a 500),
   - each side effect fires within per-scenario bounds: at least its floor
     (a handler that silently drops events cannot pass) and at most its
     budget (once per event for duplicate and retry scenarios; zero for
     unknown/malformed),
   - **order-equivalence**: for order scenarios the harness re-runs the same
     deliveries in natural order against a fresh probe and requires the final
     state to be identical - the same trick database tests use for
     commutativity, applied to webhook handlers.

   Failures name the root cause: which effect fired twice, which ordering
   assumption resurrected stale state, with the compared snapshots.

4. **Read-only audit.** An auditor agent checks the handler against the
   contract: effects whose claimed guard does not exist in the code, dedupe
   markers not committed atomically with the effects they guard, global state
   shared across entities, unknown types falling into error branches.

## Install

In Claude Code:

```text
/plugin marketplace add ultrathinker/webhook-contract
/plugin install webhook-contract@webhook-contract
```

Then restart the session (or run `/reload-plugins`). You need Python 3.9 or newer on your PATH (as `python` or
`python3`), and Node 18 or newer only if you write your handler in JavaScript.

## Quick start

In your project:

```
/webhook-contract init
```

or drive the CLI directly. Point `SCRIPT` at the plugin's
`scripts/webcontract.py` - `init` prints the resolved path (use `python`
instead of `python3` on Windows):

```bash
SCRIPT=<path-to>/webcontract.py

python3 "$SCRIPT" init --dir tests/webhooks --lang python
# edit tests/webhooks/webhook-contract.json (one entry per event type)
python3 "$SCRIPT" gen   --dir tests/webhooks
python3 "$SCRIPT" check --dir tests/webhooks
# wire tests/webhooks/replay_hooks.py to your handler, then:
cd tests/webhooks && python3 test_replay.py
```

Node handlers: `--lang node`, edit `replay_hooks.mjs`, run
`node --test test_replay.mjs`. Both harnesses also run under pytest / any
Node test UI that collects the standard runners.

A complete worked example (contract, a delivery-safe adapter, and the naive
handler it replaces) is in [`examples/`](examples/) - the naive one fails all
six scenarios, each failure pointing at the missing guard. A GitHub-style
contract (`webhook-github.json`) shows a header-keyed provider. Async variants
of the safe adapter (`replay_hooks_golden_async.*`) show the wiring for
`async def` / async handlers.

## What it is not (read this)

- **Not a delivery tester.** Nothing is sent anywhere. Replays call your
  function with synthetic payloads; no server binds a port; no data leaves
  your machine.
- **Not a signature library.** Verifying HMACs stays with your provider's
  SDK. The adapter's `handle()` is the marked place where that check runs.
- **Not a concurrency or timing tester.** Wall-clock delays are reduced to
  ordering: a redelivery five minutes late is, in-process, the next delivery
  in line. Parallel in-flight requests are out of scope.
- **Not an ordering oracle.** The order scenarios compare against one strict
  model: per ordering key, ascending integer sequence. No major provider
  ships such a sequence (the example contract invents `attempt`), so if
  yours has none, skip the order scenarios per event - the GitHub example
  does exactly that - rather than pretend.
- **Not a platform.** No dashboards, no storage of your events, no
  integration with the Hookdeck/Inngest-style services - those products
  inspect *real* traffic; this plugin hardens *your code* against it.
- The probe is wired by hand (one wrap per side-effect site). If a side
  effect cannot be observed, that is a finding, not a nuisance: an effect you
  cannot count is an effect you cannot guard.

## Requirements

- The CLI: Python 3.9+ (standard library only, nothing to install).
- The harness for your language: Python 3.9+, or Node 18+ for the `.mjs`
  variant. You only need the language you write your handler in.
- Claude Code for the skill/command/agent; the CLI also works standalone.

## Repository layout

```
.claude-plugin/plugin.json        plugin manifest
commands/webhook-contract.md      /webhook-contract entry point
skills/webhook-contract/          methodology + schema/provider references
agents/webhook-contract-auditor   read-only contract audit
scripts/webcontract.py            init / gen / check CLI (stdlib only)
templates/                        generated harnesses, adapter stubs, folder README
examples/                         worked example: contract + adapters
tests/                            plugin's own tests (synthetic data)
```

## Development

Run the plugin's own tests from the repository root:

```bash
python3 -m unittest discover -s tests -t .   # python on Windows
```

Tests build their workspaces in a system temporary directory (via
`tempfile`) and remove it when the run ends. To regenerate fixtures after
changing a contract, just run `gen` - output is deterministic, so diffs mean
your contract changed.

## Safety

Read-only by default: nothing is written until you run `init` or `gen`, both
of which write only inside the folder you point them at. `init` creates
missing files only - it never overwrites anything you already have. `gen`
rewrites only its own fixture files under `fixtures/` (files of the same name
are replaced when you regenerate) and never touches your contract or your
`replay_hooks` file. Nothing is ever deleted. No
network calls anywhere; no telemetry; no execution of provider payloads
outside your own test run. Privacy: `PRIVACY.md`. Security reports:
`SECURITY.md`.

## License

MIT - see [LICENSE](LICENSE).
