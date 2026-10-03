# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses [Semantic Versioning](https://semver.org/).

## [Unreleased]

## [0.1.0]

First public release.

### Added
- The `/webhook-contract` command, the `webhook-contract` skill and a read-only `webhook-contract-auditor` agent for
  making webhook handlers safe against duplicate, retried and out-of-order deliveries.
- A dependency-free Python CLI (`scripts/webcontract.py`, Python 3.9 or newer) with `init` (scaffold a contract and a
  replay harness, creating missing files only), `gen` (deterministic synthetic fixtures) and `check` (contract,
  fixture and data-hygiene validation).
- Replay harnesses for Python and for Node 18+, a worked example with a safe adapter and the naive handler it
  replaces, and a GitHub-style contract.
- A test suite that runs on Windows, Linux and macOS.
