---
description: Build or verify a webhook delivery contract for a handler in this repo (synthetic offline replays, no server).
argument-hint: "[init|contract|fixtures|replay|review]"
---

# Webhook delivery contract

Follow the `webhook-contract` skill for the full methodology. The user's
argument selects the stage:

- `init` (or no argument): find the webhook handler in this repo, then run the
  plugin's `init` command and fill the contract together with the user.
- `contract`: review/complete the existing `webhook-contract.json` - every
  event, every side effect, a concrete guard per effect.
- `fixtures`: run `gen` and then `check`, and fix everything `check` reports.
- `replay`: verify the adapter is wired (not stubbed), then run the harness
  and interpret each failure as a root cause per the skill's table.
- `review`: spawn the `webhook-contract-auditor` agent for a read-only audit
  of the handler against the contract.

Arguments: $ARGUMENTS

Ground rules: everything is offline and synthetic; never paste real payloads,
customer data or secrets into fixtures; never invent signature verification -
leave it to the provider SDK inside the user's `handle()` adapter.
