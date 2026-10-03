---
name: webhook-contract
description: Build and verify a delivery contract for a webhook handler - duplicates, retries, lost responses, out-of-order events, unknown types and malformed payloads - with synthetic in-process replay fixtures. Use when a developer receives webhooks from a payment, e-commerce or Git provider and wants their handler tested against real delivery behavior without running a server.
---

# Webhook delivery contract

Providers deliver events **at-least-once**: duplicates, retries after a lost
response, and arbitrary reordering are normal operation, not incidents. The
happy path from the docs always passes; production breaks on everything else.
This skill turns a handler's implicit assumptions into an explicit contract,
then verifies the contract with synthetic in-process replays.

Everything is offline and synthetic: no server, no listener, no real provider,
no network. Signature verification stays with the provider's SDK (you leave a
marked place for it; never invent signature logic).

## Workflow

### 1. Understand the handler

Read the handler code. Find:

- the entry point (route/controller) and how the raw body and headers reach it,
- which event types it handles, and which it rejects,
- every side effect: DB writes, emails, charges, queue messages, cache updates,
- the ordering key: which entity an event belongs to (e.g. `invoice.id`),
- what storage exists for idempotency today (dedupe table, unique index, ...).

Ask the developer only what the code cannot answer.

### 2. Scaffold

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/webcontract.py" init --dir tests/webhooks --lang python   # or node / both
```

This creates `webhook-contract.json` (the contract), `replay_hooks.py`/`.mjs`
(the adapter you will wire later), generated harnesses and a folder README. It creates missing files only; existing files are never overwritten.

The command prints a `cli script:` line with the resolved script path - relay
it to the developer so they can rerun `gen` and `check` in their own terminal.
On Windows use `python` instead of `python3` (the Microsoft Store `python3`
stub is not a real interpreter).

### 3. Write the contract with the developer

One entry per event type. For each side effect, the guard is the thinking
work: **what exactly makes repeating this safe?** "It's idempotent" is not an
answer; "conditional UPDATE ... WHERE status != 'paid'" is. Fill `fields` to
match the provider's envelope (event id, type, delivery-id header) and `ack`
to match what the provider accepts. See [references/contract-schema.md](references/contract-schema.md)
and [references/provider-notes.md](references/provider-notes.md).

Rules for payloads:

- Synthetic data only, and it must look synthetic: `example.com` addresses,
  `*_test_*` ids, amounts like 4200. `check` rejects real-looking emails,
  card numbers that are not documented test cards and secret-shaped strings.
- Never paste real payloads, logs, customer data or provider dashboard values.

### 4. Generate and check

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/webcontract.py" gen --dir tests/webhooks
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/webcontract.py" check --dir tests/webhooks
```

`gen` is deterministic: the same contract always produces the same fixtures.
Six scenarios per event: `duplicate`, `retry_after_failure`,
`out_of_order`, `cross_key_interleave`, `unknown_type`, `malformed`.
`check` validates structure, scenario invariants, completeness and data
hygiene. Fix everything it reports before going on.

### 5. Wire the adapter (the one manual step)

Edit `replay_hooks.py` or `replay_hooks.mjs` so that:

- `handle(raw_body, headers)` runs the real handler path (parse, verify
  signature with the provider SDK, dispatch, apply) and returns the status
  that would be sent. Two arguments - the retry scenario's first delivery is
  an ordinary call whose response the harness discards; nothing to special-case,
- header names arrive lower-case in the `headers` dict
  (`x-github-delivery`, not `X-GitHub-Delivery`),
- every side-effect site calls `PROBE.record(effect_name)` - wrap the db
  writer, mailer or queue client so the count is real, not aspirational. If
  the handler enqueues work for a background worker (the shape Stripe
  recommends), drain the queue inside `handle()`: the replay calls the
  function directly, a background worker never runs, and the effects would
  never be counted,
- the probe methods (`reset`, `counts`, `snapshot`) may be sync or async -
  the harness awaits either,
- `PROBE.snapshot()` returns a JSON-serializable view of the FINAL state
  (what the entities look like now), not a call log - and not nothing: an
  empty or non-serializable snapshot fails the order scenarios instead of
  letting them pass vacuously,
- delete the `WEBHOOK-CONTRACT:TODO` marker line when done: a leftover
  marker keeps `check` warning "hooks are not wired" (and failing
  `--strict`) forever.

Do not stub the probe to trivially pass; that defeats the point. If wiring a
probe into a side-effect site is impossible, say so - that itself is a finding
(the effect is not observable, hence not guarded).

### 6. Replay and interpret

```bash
cd tests/webhooks && python3 test_replay.py   # or: node --test test_replay.mjs (python on Windows)
```

Read failures as root causes, not test noise:

| Failure | Root cause | Typical fix |
| --- | --- | --- |
| `duplicate` budget exceeded | effect not deduped by event id | dedupe store keyed by provider event id, checked before effects |
| `retry_after_failure` budget exceeded | the redelivery repeats effects - no dedupe marker was written, or it is not checked before the effects | check-and-set the dedupe marker and the state change in one transaction; effects outside your DB (emails, charges, queue sends) need downstream idempotency or an outbox - the marker cannot un-send them |
| `out_of_order` snapshot differs | stale event overwrites newer state | conditional update: apply only if sequence/version is newer |
| `cross_key_interleave` snapshot differs | global "last event" state; keys interfere | scope all state by the ordering key |
| `unknown_type` wrong status | unhandled type falls into an error branch | default case: ack 200, ignore |
| `malformed` wrong status | parse errors escape as 500 | catch parse errors, answer the contract's malformed ack |

What `retry_after_failure` proves - and what it cannot: a redelivery after a
lost response does not repeat effects. It cannot see a crash landing
*between* the dedupe marker and an effect - process death mid-handler is not
simulated. Close that gap in code: one transaction for marker + state change;
a marker written *before* the effects needs a processing/done state (or a
lease), so a crashed attempt is not stuck as "seen" forever.

### 7. Review and iterate

After fixes, re-run `check` and the harness. For a deeper pass, use the
read-only auditor agent (`webhook-contract-auditor`) to audit the handler
against the contract - it looks for effects without guards and unguarded
non-idempotent calls.

## Scope and honesty

- In-process only: wall-clock delays are reduced to ordering (a delayed
  redelivery IS a duplicate); concurrency within one process is not simulated.
- Signature verification, transport and provider quotas are out of scope;
  the adapter is the designated place for the SDK check.
- No data leaves the machine. Fixtures contain synthetic values only.
