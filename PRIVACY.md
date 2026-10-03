# Privacy Policy

Last updated: 2026-09-30

webhook-contract runs on your machine. It collects nothing, sends nothing and has no telemetry or accounts.

## What the plugin does

- Its script reads the folder you point it at with `--dir`: the contract file `webhook-contract.json`, the fixture files under `fixtures/`, and the `replay_hooks` files (only to see whether they are still the unwired stubs). `init` also reads the plugin's own templates.
- `init` creates that folder if needed and writes the contract, a README, the replay harness and the hook stub into it; it never overwrites a file that already exists. `gen` writes synthetic fixture files into `fixtures/` (it replaces fixture files of the same name when you regenerate) and never deletes anything. `check` writes no files and prints its report to your terminal.
- It makes no network requests and never sends a webhook or any other request to a URL: replays call your handler function directly inside a local process. The script starts no other programs, and the plugin declares no hooks, MCP servers or background processes.
- When `check` flags a secret-like value, a real-looking email address or a card-like number, its report quotes that value, or its first characters, in your terminal.
- The generated replay harness (`test_replay.py`, `test_replay.mjs`) runs your own handler in your own test process. The plugin adds no network access there, but whatever your handler does when it is called (a database write or an HTTP call, if you wire it that way) still happens, so wire the adapter to test doubles.

## What is shared

Nothing is shared with the author or with any third party. The author receives no data from this plugin.

## Claude

The plugin's skill, command and agent are instructions that Claude reads inside your own Claude Code session. Claude runs the script and your replay harness through its normal tools under your permission settings, and the auditor agent (Read, Grep and Glob only, so it cannot write or run anything) reads your handler code and contract. What Claude itself does with the content it handles there is governed by the terms and privacy policy that apply to your Claude account, not by this plugin.

## Questions

Open an issue in this repository's GitHub issue tracker.
