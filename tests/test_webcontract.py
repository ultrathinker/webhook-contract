"""Tests for the webcontract CLI and the generated replay harnesses.

Synthetic data only. Each test builds a throwaway workspace under a
temporary directory provided by `tempfile` (removed when the run ends).

Run from the repository root:

    python3 -m unittest discover -s tests -t .
"""

import atexit
import itertools
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "webcontract.py"
TEMPLATES = ROOT / "templates"
EXAMPLES = ROOT / "examples"

_id = itertools.count()
_TMP_ROOT = tempfile.TemporaryDirectory(prefix="webcontract-tests-")
atexit.register(_TMP_ROOT.cleanup)


def workspace(name: str) -> Path:
    ws = Path(_TMP_ROOT.name) / "{}-{}".format(next(_id), name)
    ws.mkdir(parents=True, exist_ok=True)
    return ws


def run_cli(*argv, cwd=None):
    return subprocess.run(
        [sys.executable, str(SCRIPT), *argv],
        capture_output=True,
        text=True,
        cwd=str(cwd) if cwd else None,
    )


def run_harness(command, ws: Path):
    """Run a generated replay harness; unittest writes to stderr, so combine."""
    result = subprocess.run(command, capture_output=True, text=True, cwd=str(ws))
    result.combined = result.stdout + result.stderr
    return result


def seeded_workspace(name: str, hooks: str) -> Path:
    """Workspace with the example contract, a generated fixture set and the
    given example adapter installed as replay_hooks.<ext>."""
    ws = workspace(name)
    shutil.copyfile(EXAMPLES / "webhook-contract.json", ws / "webhook-contract.json")
    shutil.copyfile(EXAMPLES / "hooks" / ("replay_hooks_{}.py".format(hooks)), ws / "replay_hooks.py")
    shutil.copyfile(TEMPLATES / "test_replay.py", ws / "test_replay.py")
    shutil.copyfile(EXAMPLES / "hooks" / ("replay_hooks_{}.mjs".format(hooks)), ws / "replay_hooks.mjs")
    shutil.copyfile(TEMPLATES / "test_replay.mjs", ws / "test_replay.mjs")
    result = run_cli("gen", "--dir", str(ws))
    assert result.returncode == 0, result.stdout + result.stderr
    return ws


# A handler that acks everything and applies nothing. Upper bounds alone
# cannot catch it; the fixtures' effect_min floor must.
NOOP_HOOKS_PY = (
    "class Probe:\n"
    "    def reset(self): pass\n"
    "    def record(self, *a): pass\n"
    "    def counts(self): return {}\n"
    "    def snapshot(self): return {}\n\n\n"
    "PROBE = Probe()\n\n\n"
    "def handle(raw_body, headers):\n"
    "    return 200\n"
)

NOOP_HOOKS_MJS = (
    "export const PROBE = {\n"
    "  reset() {},\n"
    "  record() {},\n"
    "  counts() { return {}; },\n"
    "  snapshot() { return {}; },\n"
    "};\n\n"
    "export function handle(rawBody, headers) {\n"
    "  return 200;\n"
    "}\n"
)

# Dedupes by event id (so duplicate/retry budgets pass) but applies arrivals
# last-write-wins, and its snapshot() hands out the LIVE state object - the
# exact shape that made the old order-equivalence check vacuous: reset()
# rewrote the held reference and both runs compared equal forever.
LWW_LIVE_REFERENCE_PY = (
    "import json\n"
    "\n"
    "_CALLS = []\n"
    "_STATE = {}\n"
    "_SEEN = set()\n"
    "\n"
    "class Probe:\n"
    "    def reset(self):\n"
    "        del _CALLS[:]\n"
    "        _STATE.clear()\n"
    "        _SEEN.clear()\n"
    "    def record(self, effect, detail=None):\n"
    "        _CALLS.append(effect)\n"
    "    def counts(self):\n"
    "        result = {}\n"
    "        for name in _CALLS:\n"
    "            result[name] = result.get(name, 0) + 1\n"
    "        return result\n"
    "    def snapshot(self):\n"
    "        return _STATE  # live reference on purpose\n"
    "\n"
    "PROBE = Probe()\n"
    "\n"
    "def handle(raw_body, headers):\n"
    "    try:\n"
    "        event = json.loads(raw_body)\n"
    "    except Exception:\n"
    "        return 400\n"
    "    if event.get('type') != 'invoice.paid':\n"
    "        return 200\n"
    "    if event['id'] in _SEEN:\n"
    "        return 200\n"
    "    _SEEN.add(event['id'])\n"
    "    invoice = event['data']['object']\n"
    "    PROBE.record('mark_invoice_paid', invoice['id'])\n"
    "    PROBE.record('send_receipt_email', invoice['id'])\n"
    "    _STATE[invoice['id']] = {'attempt': invoice['attempt']}\n"
    "    return 200\n"
)

LWW_LIVE_REFERENCE_MJS = (
    "const CALLS = [];\n"
    "const STATE = {};\n"
    "const SEEN = new Set();\n"
    "\n"
    "export const PROBE = {\n"
    "  reset() { CALLS.length = 0; for (const k of Object.keys(STATE)) delete STATE[k]; SEEN.clear(); },\n"
    "  record(effect) { CALLS.push(effect); },\n"
    "  counts() { return CALLS.reduce((acc, n) => { acc[n] = (acc[n] ?? 0) + 1; return acc; }, {}); },\n"
    "  snapshot() { return STATE; },  // live reference on purpose\n"
    "};\n"
    "\n"
    "export function handle(rawBody, headers) {\n"
    "  let event;\n"
    "  try { event = JSON.parse(rawBody); } catch { return 400; }\n"
    "  if (event.type !== 'invoice.paid') return 200;\n"
    "  if (SEEN.has(event.id)) return 200;\n"
    "  SEEN.add(event.id);\n"
    "  const invoice = event.data.object;\n"
    "  PROBE.record('mark_invoice_paid', invoice.id);\n"
    "  PROBE.record('send_receipt_email', invoice.id);\n"
    "  STATE[invoice.id] = { attempt: invoice.attempt };\n"
    "  return 200;\n"
    "}\n"
)

# A handler that applies arrivals last-write-wins (so the order scenarios MUST
# fail) wired to a lazy probe whose snapshot() is always empty: both sides of
# the order-equivalence comparison serialize to "{}" and the harness must call
# the comparison vacuous instead of passing it.
EMPTY_SNAPSHOT_PY = (
    "import json\n"
    "\n"
    "_CALLS = []\n"
    "_STATE = {}\n"
    "_SEEN = set()\n"
    "\n"
    "class Probe:\n"
    "    def reset(self):\n"
    "        del _CALLS[:]\n"
    "        _STATE.clear()\n"
    "        _SEEN.clear()\n"
    "    def record(self, effect, detail=None):\n"
    "        _CALLS.append(effect)\n"
    "    def counts(self):\n"
    "        result = {}\n"
    "        for name in _CALLS:\n"
    "            result[name] = result.get(name, 0) + 1\n"
    "        return result\n"
    "    def snapshot(self):\n"
    "        return {}  # lazy: reflects no state at all\n"
    "\n"
    "PROBE = Probe()\n"
    "\n"
    "def handle(raw_body, headers):\n"
    "    try:\n"
    "        event = json.loads(raw_body)\n"
    "    except Exception:\n"
    "        return 400\n"
    "    if event.get('type') != 'invoice.paid':\n"
    "        return 200\n"
    "    if event['id'] in _SEEN:\n"
    "        return 200\n"
    "    _SEEN.add(event['id'])\n"
    "    invoice = event['data']['object']\n"
    "    PROBE.record('mark_invoice_paid', invoice['id'])\n"
    "    PROBE.record('send_receipt_email', invoice['id'])\n"
    "    _STATE[invoice['id']] = {'attempt': invoice['attempt']}\n"
    "    return 200\n"
)

# The Node twin: JSON.stringify renders a Map as "{}", so before the fix both
# sides of the comparison were "{}" and a wrong handler passed everything.
MAP_SNAPSHOT_MJS = (
    "const CALLS = [];\n"
    "const STATE = new Map();\n"
    "const SEEN = new Set();\n"
    "\n"
    "export const PROBE = {\n"
    "  reset() { CALLS.length = 0; STATE.clear(); SEEN.clear(); },\n"
    "  record(effect) { CALLS.push(effect); },\n"
    "  counts() { return CALLS.reduce((acc, n) => { acc[n] = (acc[n] ?? 0) + 1; return acc; }, {}); },\n"
    "  snapshot() { return STATE; },  // a Map: not JSON-serializable\n"
    "};\n"
    "\n"
    "export function handle(rawBody, headers) {\n"
    "  let event;\n"
    "  try { event = JSON.parse(rawBody); } catch { return 400; }\n"
    "  if (event.type !== 'invoice.paid') return 200;\n"
    "  if (SEEN.has(event.id)) return 200;\n"
    "  SEEN.add(event.id);\n"
    "  const invoice = event.data.object;\n"
    "  PROBE.record('mark_invoice_paid', invoice.id);\n"
    "  PROBE.record('send_receipt_email', invoice.id);\n"
    "  STATE.set(invoice.id, { attempt: invoice.attempt });\n"
    "  return 200;\n"
    "}\n"
)

# A correct handler (dedupe + max-attempt guard) wired to a DB-style probe
# whose methods are coroutines. The harness must await reset()/counts()/
# snapshot(); un-awaited calls made a correct handler fail and a wrong one pass.
ASYNC_PROBE_PY = (
    "import asyncio\n"
    "import copy\n"
    "import json\n"
    "\n"
    "_CALLS = []\n"
    "_STATE = {}\n"
    "_SEEN = set()\n"
    "\n"
    "class Probe:\n"
    "    async def reset(self):\n"
    "        await asyncio.sleep(0)\n"
    "        del _CALLS[:]\n"
    "        _STATE.clear()\n"
    "        _SEEN.clear()\n"
    "    def record(self, effect, detail=None):\n"
    "        _CALLS.append(effect)\n"
    "    async def counts(self):\n"
    "        await asyncio.sleep(0)\n"
    "        result = {}\n"
    "        for name in _CALLS:\n"
    "            result[name] = result.get(name, 0) + 1\n"
    "        return result\n"
    "    async def snapshot(self):\n"
    "        await asyncio.sleep(0)\n"
    "        return copy.deepcopy(_STATE)\n"
    "\n"
    "PROBE = Probe()\n"
    "\n"
    "async def handle(raw_body, headers):\n"
    "    try:\n"
    "        event = json.loads(raw_body)\n"
    "    except Exception:\n"
    "        return 400\n"
    "    if not isinstance(event, dict):\n"
    "        return 400\n"
    "    if event.get('type') != 'invoice.paid':\n"
    "        return 200\n"
    "    if event['id'] in _SEEN:\n"
    "        return 200\n"
    "    invoice = event['data']['object']\n"
    "    current = _STATE.get(invoice['id'])\n"
    "    if current is not None and invoice['attempt'] <= current['attempt']:\n"
    "        return 200\n"
    "    _SEEN.add(event['id'])\n"
    "    PROBE.record('mark_invoice_paid', invoice['id'])\n"
    "    _STATE[invoice['id']] = {'attempt': invoice['attempt']}\n"
    "    PROBE.record('send_receipt_email', invoice['id'])\n"
    "    return 200\n"
)

ASYNC_PROBE_MJS = (
    "const tick = () => new Promise((resolve) => setTimeout(resolve, 0));\n"
    "const CALLS = [];\n"
    "const STATE = new Map();\n"
    "const SEEN = new Set();\n"
    "\n"
    "export const PROBE = {\n"
    "  async reset() { await tick(); CALLS.length = 0; STATE.clear(); SEEN.clear(); },\n"
    "  record(effect) { CALLS.push(effect); },\n"
    "  async counts() { await tick(); return CALLS.reduce((acc, n) => { acc[n] = (acc[n] ?? 0) + 1; return acc; }, {}); },\n"
    "  async snapshot() {\n"
    "    await tick();\n"
    "    return Object.fromEntries([...STATE].map(([k, v]) => [k, { ...v }]));\n"
    "  },\n"
    "};\n"
    "\n"
    "export async function handle(rawBody, headers) {\n"
    "  let event;\n"
    "  try { event = JSON.parse(rawBody); } catch { return 400; }\n"
    "  if (typeof event !== 'object' || event === null) return 400;\n"
    "  if (event.type !== 'invoice.paid') return 200;\n"
    "  if (SEEN.has(event.id)) return 200;\n"
    "  const invoice = event.data.object;\n"
    "  const current = STATE.get(invoice.id);\n"
    "  if (current !== undefined && invoice.attempt <= current.attempt) return 200;\n"
    "  SEEN.add(event.id);\n"
    "  PROBE.record('mark_invoice_paid', invoice.id);\n"
    "  STATE.set(invoice.id, { attempt: invoice.attempt });\n"
    "  PROBE.record('send_receipt_email', invoice.id);\n"
    "  return 200;\n"
    "}\n"
)

# Handles the known event correctly and "acknowledges" unknown types - but
# forgets the `return`, so the handler answers None, which is not a status.
# Before the LOST sentinel, None was the harness's own lost-response marker
# and the scenario passed.
MISSING_RETURN_PY = (
    "import copy\n"
    "import json\n"
    "\n"
    "_CALLS = []\n"
    "_STATE = {}\n"
    "_SEEN = set()\n"
    "\n"
    "class Probe:\n"
    "    def reset(self):\n"
    "        del _CALLS[:]\n"
    "        _STATE.clear()\n"
    "        _SEEN.clear()\n"
    "    def record(self, effect, detail=None):\n"
    "        _CALLS.append(effect)\n"
    "    def counts(self):\n"
    "        result = {}\n"
    "        for name in _CALLS:\n"
    "            result[name] = result.get(name, 0) + 1\n"
    "        return result\n"
    "    def snapshot(self):\n"
    "        return copy.deepcopy(_STATE)\n"
    "\n"
    "PROBE = Probe()\n"
    "\n"
    "def ack_unknown(event):\n"
    "    return 200  # the intended answer for types the handler ignores\n"
    "\n"
    "def apply_paid(event):\n"
    "    if event['id'] in _SEEN:\n"
    "        return 200\n"
    "    invoice = event['data']['object']\n"
    "    current = _STATE.get(invoice['id'])\n"
    "    if current is not None and invoice['attempt'] <= current['attempt']:\n"
    "        return 200\n"
    "    _SEEN.add(event['id'])\n"
    "    PROBE.record('mark_invoice_paid', invoice['id'])\n"
    "    _STATE[invoice['id']] = {'attempt': invoice['attempt']}\n"
    "    PROBE.record('send_receipt_email', invoice['id'])\n"
    "    return 200\n"
    "\n"
    "def handle(raw_body, headers):\n"
    "    try:\n"
    "        event = json.loads(raw_body)\n"
    "    except Exception:\n"
    "        return 400\n"
    "    if event.get('type') == 'invoice.paid':\n"
    "        return apply_paid(event)\n"
    "    ack_unknown(event)  # BUG under test: the 200 is never returned\n"
)

# The Node hole was `return null`: null was the harness's lost-response marker
# and passed the status assertion.
MISSING_RETURN_MJS = (
    "const CALLS = [];\n"
    "const STATE = {};\n"
    "const SEEN = new Set();\n"
    "\n"
    "export const PROBE = {\n"
    "  reset() { CALLS.length = 0; for (const k of Object.keys(STATE)) delete STATE[k]; SEEN.clear(); },\n"
    "  record(effect) { CALLS.push(effect); },\n"
    "  counts() { return CALLS.reduce((acc, n) => { acc[n] = (acc[n] ?? 0) + 1; return acc; }, {}); },\n"
    "  snapshot() {\n"
    "    const out = {};\n"
    "    for (const [k, v] of Object.entries(STATE)) out[k] = { ...v };\n"
    "    return out;\n"
    "  },\n"
    "};\n"
    "\n"
    "export function handle(rawBody, headers) {\n"
    "  let event;\n"
    "  try { event = JSON.parse(rawBody); } catch { return 400; }\n"
    "  if (event.type !== 'invoice.paid') return null;  // BUG under test\n"
    "  if (SEEN.has(event.id)) return 200;\n"
    "  SEEN.add(event.id);\n"
    "  const invoice = event.data.object;\n"
    "  PROBE.record('mark_invoice_paid', invoice.id);\n"
    "  STATE[invoice.id] = { attempt: invoice.attempt };\n"
    "  PROBE.record('send_receipt_email', invoice.id);\n"
    "  return 200;\n"
    "}\n"
)

# An adapter wrapping an existing two-argument handle(raw, headers) - the
# natural shape around a real handler. The harness must call it with exactly
# these two arguments on the discarded first attempt too: a swallowed
# TypeError used to skip that attempt and hide a missing dedupe.
TWO_ARG_NO_DEDUPE_PY = (
    "import copy\n"
    "import json\n"
    "\n"
    "_CALLS = []\n"
    "_STATE = {}\n"
    "\n"
    "class Probe:\n"
    "    def reset(self):\n"
    "        del _CALLS[:]\n"
    "        _STATE.clear()\n"
    "    def record(self, effect, detail=None):\n"
    "        _CALLS.append(effect)\n"
    "    def counts(self):\n"
    "        result = {}\n"
    "        for name in _CALLS:\n"
    "            result[name] = result.get(name, 0) + 1\n"
    "        return result\n"
    "    def snapshot(self):\n"
    "        return copy.deepcopy(_STATE)\n"
    "\n"
    "PROBE = Probe()\n"
    "\n"
    "def handle(raw_body, headers):\n"
    "    try:\n"
    "        event = json.loads(raw_body)\n"
    "    except Exception:\n"
    "        return 400\n"
    "    if event.get('type') != 'invoice.paid':\n"
    "        return 200\n"
    "    invoice = event['data']['object']\n"
    "    PROBE.record('mark_invoice_paid', invoice['id'])\n"
    "    _STATE[invoice['id']] = {'attempt': invoice['attempt']}\n"
    "    PROBE.record('send_receipt_email', invoice['id'])\n"
    "    return 200\n"
)

# Correct handler (max-attempt guard, event-id dedupe) whose per-key state
# records the events it has seen in a dict - so the snapshot of the violated
# and the natural run differ ONLY in key insertion order. Order-equivalence
# must still compare them equal (recursive key sorting in stable()).
COMMUTATIVE_MERGE_MJS = (
    "const CALLS = [];\n"
    "const STATE = {};\n"
    "const SEEN = new Set();\n"
    "\n"
    "export const PROBE = {\n"
    "  reset() { CALLS.length = 0; for (const k of Object.keys(STATE)) delete STATE[k]; SEEN.clear(); },\n"
    "  record(effect) { CALLS.push(effect); },\n"
    "  counts() { return CALLS.reduce((acc, n) => { acc[n] = (acc[n] ?? 0) + 1; return acc; }, {}); },\n"
    "  snapshot() { return STATE; },\n"
    "};\n"
    "\n"
    "export function handle(rawBody, headers) {\n"
    "  let event;\n"
    "  try { event = JSON.parse(rawBody); } catch { return 400; }\n"
    "  if (event.type !== 'invoice.paid') return 200;\n"
    "  if (SEEN.has(event.id)) return 200;\n"
    "  SEEN.add(event.id);\n"
    "  const invoice = event.data.object;\n"
    "  PROBE.record('mark_invoice_paid', invoice.id);\n"
    "  PROBE.record('send_receipt_email', invoice.id);\n"
    "  const entry = STATE[invoice.id] ??= { attempt: 0, events: {} };\n"
    "  entry.events[event.id] = invoice.attempt;\n"
    "  if (invoice.attempt > entry.attempt) entry.attempt = invoice.attempt;\n"
    "  return 200;\n"
    "}\n"
)


class ComponentTrackingTests(unittest.TestCase):
    """Every plugin component file must be tracked: the skeleton .gitignore has
    broad case-insensitive patterns (e.g. *REVIEW*.md matches 'reviewer'), and
    a component that matches one of them silently ships empty."""

    def test_no_component_file_is_git_ignored(self):
        # The checks below ask git about tracked files; from a downloaded
        # tarball (no .git, git maybe absent) there is nothing to ask.
        if not (ROOT / ".git").exists() or shutil.which("git") is None:
            self.skipTest("not a git checkout; component tracking cannot be verified here")
        patterns = [
            "agents/*.md",
            "commands/*.md",
            "skills/*/SKILL.md",
            "skills/*/references/*.md",
            "templates/*",
            "scripts/*.py",
            "examples/*.json",
            "examples/hooks/*",
        ]
        files = []
        for pattern in patterns:
            files.extend(sorted(ROOT.glob(pattern)))
        self.assertTrue(files, "component discovery found nothing")
        for path in files:
            rel = path.relative_to(ROOT).as_posix()
            ignored = subprocess.run(
                ["git", "check-ignore", "-q", rel], cwd=str(ROOT), capture_output=True
            )
            self.assertNotEqual(
                ignored.returncode, 0, "{} matches a .gitignore pattern and would not be published".format(rel)
            )
            tracked = subprocess.run(
                ["git", "ls-files", "--error-unmatch", rel], cwd=str(ROOT), capture_output=True
            )
            self.assertEqual(
                tracked.returncode, 0, "{} exists on disk but is not tracked by git".format(rel)
            )


class InitTests(unittest.TestCase):
    def test_init_scaffolds_all_files(self):
        ws = workspace("init")
        result = run_cli("init", "--dir", str(ws / "hooks"))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        expected = [
            "webhook-contract.json",
            "README.md",
            "test_replay.py",
            "replay_hooks.py",
            "test_replay.mjs",
            "replay_hooks.mjs",
        ]
        for name in expected:
            self.assertTrue((ws / "hooks" / name).exists(), "missing {}".format(name))
        contract = json.loads((ws / "hooks" / "webhook-contract.json").read_text(encoding="utf-8"))
        self.assertEqual(contract["contract_version"], 1)

    def test_init_creates_missing_files_and_skips_existing(self):
        # A pre-existing file of the user's own (here: a README) must not
        # block scaffolding the rest, and init never overwrites anything.
        ws = workspace("init-missing-only")
        (ws / "README.md").write_text("# my own notes\n", encoding="utf-8")
        result = run_cli("init", "--dir", str(ws))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual((ws / "README.md").read_text(encoding="utf-8"), "# my own notes\n")
        self.assertIn("skipped (already present", result.stdout)
        for name in ("webhook-contract.json", "test_replay.py", "replay_hooks.py"):
            self.assertTrue((ws / name).exists(), "missing {}".format(name))
        # A second run creates nothing and still succeeds.
        again = run_cli("init", "--dir", str(ws))
        self.assertEqual(again.returncode, 0)
        self.assertIn("nothing (all files present)", again.stdout)

    def test_init_python_only(self):
        ws = workspace("init-py")
        self.assertEqual(run_cli("init", "--dir", str(ws), "--lang", "python").returncode, 0)
        self.assertTrue((ws / "test_replay.py").exists())
        self.assertFalse((ws / "test_replay.mjs").exists())


class GenTests(unittest.TestCase):
    def test_gen_is_deterministic(self):
        ws = seeded_workspace("gen-det", "golden")
        fixtures = sorted(p.relative_to(ws) for p in (ws / "fixtures").rglob("*.json"))
        self.assertEqual(len(fixtures), 6)
        before = {str(p): (ws / p).read_bytes() for p in fixtures}
        self.assertEqual(run_cli("gen", "--dir", str(ws)).returncode, 0)
        after = {str(p): (ws / p).read_bytes() for p in fixtures}
        self.assertEqual(before, after, "regeneration must be byte-identical")

    def test_gen_covers_all_scenarios(self):
        ws = seeded_workspace("gen-cover", "golden")
        names = sorted(p.name for p in (ws / "fixtures").rglob("*.json"))
        self.assertEqual(
            names,
            [
                "cross_key_interleave.json",
                "duplicate.json",
                "malformed.json",
                "out_of_order.json",
                "retry_after_failure.json",
                "unknown_type.json",
            ],
        )

    def test_duplicate_fixture_has_same_body_distinct_delivery_ids(self):
        ws = seeded_workspace("gen-dup", "golden")
        doc = json.loads((ws / "fixtures" / "invoice.paid" / "duplicate.json").read_text(encoding="utf-8"))
        d1, d2 = doc["deliveries"]
        self.assertEqual(json.dumps(d1["body"], sort_keys=True), json.dumps(d2["body"], sort_keys=True))
        self.assertNotEqual(d1["headers"]["x-delivery-id"], d2["headers"]["x-delivery-id"])
        self.assertEqual(doc["expectations"]["effect_budget"], {"mark_invoice_paid": 1, "send_receipt_email": 1})
        self.assertEqual(doc["expectations"]["effect_min"], {"mark_invoice_paid": 1, "send_receipt_email": 1})
        self.assertIn("different delivery ids", doc["description"])

    def test_duplicate_description_honest_for_identity_contracts(self):
        # GitHub-style: the delivery id IS the event identity, so a redelivery
        # keeps the same value - the description must not claim fresh ids.
        ws = workspace("gen-dup-desc")
        shutil.copyfile(EXAMPLES / "webhook-github.json", ws / "webhook-contract.json")
        self.assertEqual(run_cli("gen", "--dir", str(ws)).returncode, 0)
        doc = json.loads((ws / "fixtures" / "push" / "duplicate.json").read_text(encoding="utf-8"))
        self.assertIn("keeps the same id", doc["description"])
        self.assertNotIn("different delivery ids", doc["description"])

    def test_gen_writes_effect_min_floors(self):
        ws = seeded_workspace("gen-min", "golden")
        scenario = "invoice.paid/unknown_type.json"
        doc = json.loads((ws / "fixtures" / scenario).read_text(encoding="utf-8"))
        self.assertEqual(doc["expectations"]["effect_min"], {"mark_invoice_paid": 0, "send_receipt_email": 0})
        ooo = json.loads((ws / "fixtures" / "invoice.paid" / "out_of_order.json").read_text(encoding="utf-8"))
        self.assertEqual(ooo["expectations"]["effect_min"], {"mark_invoice_paid": 1, "send_receipt_email": 1})
        cross = json.loads((ws / "fixtures" / "invoice.paid" / "cross_key_interleave.json").read_text(encoding="utf-8"))
        self.assertEqual(cross["expectations"]["effect_min"], {"mark_invoice_paid": 2, "send_receipt_email": 2})

    def test_out_of_order_arrives_newest_first(self):
        ws = seeded_workspace("gen-ooo", "golden")
        doc = json.loads((ws / "fixtures" / "invoice.paid" / "out_of_order.json").read_text(encoding="utf-8"))
        arrival = [d["order_seq"] for d in doc["deliveries"]]
        self.assertEqual(arrival, sorted(arrival, reverse=True), "newest event must arrive first")

    def test_optional_effect_gets_zero_floors_but_keeps_budgets(self):
        # An effect that fires only under a condition (a threshold, a status
        # branch) cannot carry a floor of 1 in every scenario - the handler
        # would fail a scenario where the condition never occurs.
        ws = workspace("gen-optional")
        shutil.copyfile(EXAMPLES / "webhook-contract.json", ws / "webhook-contract.json")
        path = ws / "webhook-contract.json"
        contract = json.loads(path.read_text(encoding="utf-8"))
        contract["events"][0]["side_effects"].append(
            {"name": "flag_risk_review", "idempotent": True, "guard": "Upsert keyed by invoice id.", "optional": True}
        )
        path.write_text(json.dumps(contract, indent=2), encoding="utf-8")
        self.assertEqual(run_cli("gen", "--dir", str(ws)).returncode, 0)
        dup = json.loads((ws / "fixtures" / "invoice.paid" / "duplicate.json").read_text(encoding="utf-8"))
        self.assertEqual(dup["expectations"]["effect_min"]["flag_risk_review"], 0)
        self.assertEqual(dup["expectations"]["effect_budget"]["flag_risk_review"], 1)
        # The non-optional effects keep their floor of 1.
        self.assertEqual(dup["expectations"]["effect_min"]["mark_invoice_paid"], 1)
        result = run_cli("check", "--dir", str(ws))
        self.assertEqual(result.returncode, 0, result.stdout)

    def test_check_missing_floor_error_names_the_optional_escape(self):
        ws = workspace("gen-optional-hint")
        shutil.copyfile(EXAMPLES / "webhook-contract.json", ws / "webhook-contract.json")
        self.assertEqual(run_cli("gen", "--dir", str(ws)).returncode, 0)
        fixture_path = ws / "fixtures" / "invoice.paid" / "duplicate.json"
        fx = json.loads(fixture_path.read_text(encoding="utf-8"))
        fx["expectations"]["effect_min"]["mark_invoice_paid"] = 0
        fixture_path.write_text(json.dumps(fx, indent=2), encoding="utf-8")
        result = run_cli("check", "--dir", str(ws))
        self.assertEqual(result.returncode, 1)
        self.assertIn("never applies it would pass", result.stdout)
        self.assertIn('"optional": true', result.stdout)

    def test_gen_reports_single_payload_note(self):
        ws = workspace("gen-note")
        shutil.copyfile(EXAMPLES / "webhook-contract.json", ws / "webhook-contract.json")
        contract = json.loads((ws / "webhook-contract.json").read_text(encoding="utf-8"))
        contract["events"][0]["ordered_payloads"] = None
        (ws / "webhook-contract.json").write_text(json.dumps(contract, indent=2), encoding="utf-8")
        result = run_cli("gen", "--dir", str(ws))
        self.assertEqual(result.returncode, 0)
        self.assertIn("ordered_payloads", result.stdout)

    def test_gen_rejects_invalid_contract(self):
        ws = workspace("gen-invalid")
        (ws / "webhook-contract.json").write_text('{"contract_version": 2}', encoding="utf-8")
        result = run_cli("gen", "--dir", str(ws))
        self.assertEqual(result.returncode, 1)
        self.assertIn("contract_version", result.stdout)


class HeaderKeyedContractTests(unittest.TestCase):
    """Header-keyed providers (GitHub, Shopify): event type and delivery
    identity arrive as headers. The generator must populate them on every
    delivery, and a redelivery must keep the same delivery-id value when
    that header is also the event identity (M5)."""

    def make_ws(self, name: str) -> Path:
        ws = workspace(name)
        shutil.copyfile(EXAMPLES / "webhook-github.json", ws / "webhook-contract.json")
        result = run_cli("gen", "--dir", str(ws))
        assert result.returncode == 0, result.stdout + result.stderr
        return ws

    def fixtures(self, ws: Path, scenario: str):
        return json.loads((ws / "fixtures" / "push" / "{}.json".format(scenario)).read_text(encoding="utf-8"))

    def test_gen_skips_order_scenarios_without_a_sequence(self):
        ws = self.make_ws("github-scenarios")
        names = sorted(p.name for p in (ws / "fixtures" / "push").glob("*.json"))
        self.assertEqual(names, ["duplicate.json", "malformed.json", "retry_after_failure.json", "unknown_type.json"])

    def test_event_type_header_present_on_every_delivery(self):
        ws = self.make_ws("github-type-header")
        for path in (ws / "fixtures" / "push").glob("*.json"):
            doc = json.loads(path.read_text(encoding="utf-8"))
            for delivery in doc["deliveries"]:
                self.assertEqual(
                    delivery["headers"].get("x-github-event"),
                    "webcontract.unknown.future_event" if doc["scenario"] == "unknown_type" else "push",
                    "{} delivery {} must carry the event type header".format(path.name, delivery["seq"]),
                )

    def test_redelivery_keeps_the_delivery_id(self):
        ws = self.make_ws("github-redelivery")
        for scenario in ("duplicate", "retry_after_failure"):
            deliveries = self.fixtures(ws, scenario)["deliveries"]
            values = {d["headers"]["x-github-delivery"] for d in deliveries}
            self.assertEqual(
                len(values), 1, "{}: a redelivery keeps the same delivery id, got {}".format(scenario, values)
            )

    def test_check_accepts_github_fixtures(self):
        ws = self.make_ws("github-check")
        result = run_cli("check", "--dir", str(ws))
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertIn("0 error(s)", result.stdout)

    def test_check_flags_missing_type_header(self):
        ws = self.make_ws("github-missing-header")
        path = ws / "fixtures" / "push" / "duplicate.json"
        doc = json.loads(path.read_text(encoding="utf-8"))
        del doc["deliveries"][0]["headers"]["x-github-event"]
        path.write_text(json.dumps(doc, indent=2), encoding="utf-8")
        result = run_cli("check", "--dir", str(ws))
        self.assertEqual(result.returncode, 1)
        self.assertIn("does not resolve", result.stdout)

    def test_check_flags_new_delivery_id_on_redelivery(self):
        ws = self.make_ws("github-fresh-id")
        path = ws / "fixtures" / "push" / "duplicate.json"
        doc = json.loads(path.read_text(encoding="utf-8"))
        doc["deliveries"][1]["headers"]["x-github-delivery"] = "del_test_9999"
        path.write_text(json.dumps(doc, indent=2), encoding="utf-8")
        result = run_cli("check", "--dir", str(ws))
        self.assertEqual(result.returncode, 1)
        self.assertIn("same {} value".format("header.x-github-delivery"), result.stdout)


class CheckTests(unittest.TestCase):
    def test_check_accepts_generated_set(self):
        ws = seeded_workspace("check-ok", "golden")
        result = run_cli("check", "--dir", str(ws))
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertIn("0 error(s)", result.stdout)

    def test_check_flags_broken_duplicate_body(self):
        ws = seeded_workspace("check-dup", "golden")
        path = ws / "fixtures" / "invoice.paid" / "duplicate.json"
        doc = json.loads(path.read_text(encoding="utf-8"))
        doc["deliveries"][1]["body"]["data"]["object"]["amount_paid"] = 9999
        path.write_text(json.dumps(doc, indent=2), encoding="utf-8")
        result = run_cli("check", "--dir", str(ws))
        self.assertEqual(result.returncode, 1)
        self.assertIn("identical bodies", result.stdout)

    def test_check_flags_missing_fixture(self):
        ws = seeded_workspace("check-missing", "golden")
        contract_path = ws / "webhook-contract.json"
        contract = json.loads(contract_path.read_text(encoding="utf-8"))
        contract["events"].append(
            {
                "type": "customer.updated",
                "ordering_key": "body.data.object.id",
                "payload": {
                    "id": "evt_test_0042",
                    "type": "customer.updated",
                    "data": {"object": {"id": "cus_test_0001", "attempt": 1}},
                },
                "side_effects": [{"name": "sync_customer", "idempotent": True, "guard": "upsert"}],
            }
        )
        contract_path.write_text(json.dumps(contract, indent=2), encoding="utf-8")
        result = run_cli("check", "--dir", str(ws))
        self.assertEqual(result.returncode, 1)
        self.assertIn("missing fixture for event 'customer.updated'", result.stdout)

    def test_check_flags_live_secret_in_fixture(self):
        ws = seeded_workspace("check-secret", "golden")
        path = ws / "fixtures" / "invoice.paid" / "duplicate.json"
        text = path.read_text(encoding="utf-8").replace("4200", "4200", 1)
        doc = json.loads(text)
        doc["deliveries"][0]["headers"]["authorization"] = "Bearer sk_" + "live_abcdefghijklmnop1234"
        path.write_text(json.dumps(doc, indent=2), encoding="utf-8")
        result = run_cli("check", "--dir", str(ws))
        self.assertEqual(result.returncode, 1)
        self.assertIn("Stripe live secret key", result.stdout)

    def test_check_flags_real_looking_email_in_contract(self):
        ws = workspace("check-email")
        shutil.copyfile(EXAMPLES / "webhook-contract.json", ws / "webhook-contract.json")
        path = ws / "webhook-contract.json"
        contract = json.loads(path.read_text(encoding="utf-8"))
        contract["events"][0]["payload"]["data"]["object"]["receipt_email"] = "person@gmail.com"
        path.write_text(json.dumps(contract, indent=2), encoding="utf-8")
        result = run_cli("check", "--dir", str(ws))
        self.assertEqual(result.returncode, 1)
        self.assertIn("reserved example/test domains", result.stdout)

    def test_check_email_domain_boundary_requires_a_dot(self):
        # 'notexample.com' ends with 'example.com' but is a different domain.
        ws = workspace("check-email-boundary")
        shutil.copyfile(EXAMPLES / "webhook-contract.json", ws / "webhook-contract.json")
        path = ws / "webhook-contract.json"
        contract = json.loads(path.read_text(encoding="utf-8"))
        contract["events"][0]["payload"]["data"]["object"]["receipt_email"] = "person@notexample.com"
        path.write_text(json.dumps(contract, indent=2), encoding="utf-8")
        result = run_cli("check", "--dir", str(ws))
        self.assertEqual(result.returncode, 1)
        self.assertIn("reserved example/test domains", result.stdout)

        # A real subdomain of a reserved domain stays fine.
        contract["events"][0]["payload"]["data"]["object"]["receipt_email"] = "person@mail.example.com"
        path.write_text(json.dumps(contract, indent=2), encoding="utf-8")
        self.assertEqual(run_cli("check", "--dir", str(ws)).returncode, 0)

    def test_check_flags_luhn_passing_card_but_allows_test_cards(self):
        ws = workspace("check-card")
        shutil.copyfile(EXAMPLES / "webhook-contract.json", ws / "webhook-contract.json")
        path = ws / "webhook-contract.json"
        contract = json.loads(path.read_text(encoding="utf-8"))
        contract["events"][0]["payload"]["data"]["object"]["card_number"] = "4111111111111111"
        path.write_text(json.dumps(contract, indent=2), encoding="utf-8")
        self.assertEqual(run_cli("check", "--dir", str(ws)).returncode, 0, "documented test card must pass")

        contract["events"][0]["payload"]["data"]["object"]["card_number"] = "4111111111111112"
        path.write_text(json.dumps(contract, indent=2), encoding="utf-8")
        result = run_cli("check", "--dir", str(ws))
        self.assertEqual(result.returncode, 0, "Luhn-failing digits are not card-shaped enough to flag")

        # Luhn-valid but outside every card-network range: ordinary digit ids
        # (snowflakes, timestamps) must not block gen - the Luhn checksum
        # alone passes about one such string in ten.
        for plain_id in ("1234567890123452", "1700000000004", "820982911946154508"):
            contract["events"][0]["payload"]["data"]["object"]["card_number"] = plain_id
            path.write_text(json.dumps(contract, indent=2), encoding="utf-8")
            result = run_cli("check", "--dir", str(ws))
            self.assertEqual(result.returncode, 0, "{} is not card-shaped enough to flag".format(plain_id))

        # Luhn-valid inside a card-network range and not a documented test
        # card - that is the "possible real card" shape.
        contract["events"][0]["payload"]["data"]["object"]["card_number"] = "4000000000000002"
        path.write_text(json.dumps(contract, indent=2), encoding="utf-8")
        result = run_cli("check", "--dir", str(ws))
        self.assertEqual(result.returncode, 1)
        self.assertIn("Luhn checksum", result.stdout)

        # The standard JCB test cards stay allowed although the network gate
        # covers the 35 range.
        contract["events"][0]["payload"]["data"]["object"]["card_number"] = "3530111333300000"
        path.write_text(json.dumps(contract, indent=2), encoding="utf-8")
        self.assertEqual(run_cli("check", "--dir", str(ws)).returncode, 0)

    def test_check_strict_treats_warnings_as_errors(self):
        ws = workspace("check-strict")
        self.assertEqual(run_cli("init", "--dir", str(ws)).returncode, 0)
        result = run_cli("check", "--dir", str(ws), "--strict")
        self.assertEqual(result.returncode, 1)
        self.assertEqual(run_cli("check", "--dir", str(ws)).returncode, 0)

    def test_check_warns_when_no_side_effects_are_declared(self):
        # Side-effect counts are the anti-do-nothing signal; an event without
        # them can only be checked for statuses and ordering, so `check` must
        # say so instead of letting a green run look stronger than it is.
        ws, path = self._contract_copy("check-no-effects")
        contract = json.loads(path.read_text(encoding="utf-8"))
        contract["events"][0]["side_effects"] = []
        path.write_text(json.dumps(contract, indent=2), encoding="utf-8")
        result = run_cli("check", "--dir", str(ws))
        self.assertEqual(result.returncode, 0)
        self.assertIn("nothing to count", result.stdout)
        strict = run_cli("check", "--dir", str(ws), "--strict")
        self.assertEqual(strict.returncode, 1)

    def test_check_empty_directory_is_an_error(self):
        ws = workspace("check-empty")
        result = run_cli("check", "--dir", str(ws))
        self.assertEqual(result.returncode, 2)
        self.assertIn("run init first", result.stdout)

    def test_check_json_report(self):
        ws = seeded_workspace("check-json", "golden")
        result = run_cli("check", "--dir", str(ws), "--json")
        self.assertEqual(result.returncode, 0)
        report = json.loads(result.stdout.strip().splitlines()[-1])
        self.assertEqual(report["errors"], 0)
        self.assertEqual(report["fixtures"], 6)

    def test_check_rejects_effect_min_above_budget(self):
        ws = seeded_workspace("check-minmax", "golden")
        path = ws / "fixtures" / "invoice.paid" / "duplicate.json"
        doc = json.loads(path.read_text(encoding="utf-8"))
        doc["expectations"]["effect_min"]["mark_invoice_paid"] = 5
        path.write_text(json.dumps(doc, indent=2), encoding="utf-8")
        result = run_cli("check", "--dir", str(ws))
        self.assertEqual(result.returncode, 1)
        self.assertIn("exceeds its budget", result.stdout)

    def test_check_rejects_zero_floor_with_positive_budget(self):
        ws = seeded_workspace("check-zero-floor", "golden")
        path = ws / "fixtures" / "invoice.paid" / "duplicate.json"
        doc = json.loads(path.read_text(encoding="utf-8"))
        doc["expectations"]["effect_min"]["mark_invoice_paid"] = 0
        path.write_text(json.dumps(doc, indent=2), encoding="utf-8")
        result = run_cli("check", "--dir", str(ws))
        self.assertEqual(result.returncode, 1)
        self.assertIn("would pass", result.stdout)

    def test_check_rejects_missing_effect_min(self):
        ws = seeded_workspace("check-no-min", "golden")
        path = ws / "fixtures" / "invoice.paid" / "duplicate.json"
        doc = json.loads(path.read_text(encoding="utf-8"))
        del doc["expectations"]["effect_min"]
        path.write_text(json.dumps(doc, indent=2), encoding="utf-8")
        result = run_cli("check", "--dir", str(ws))
        self.assertEqual(result.returncode, 1)
        self.assertIn("effect_min", result.stdout)

    def test_check_rejects_third_ordered_payload(self):
        # A third ordered_payloads entry is silently ignored by the scenario
        # builder - the order scenarios need exactly an (older, newer) pair.
        ws = seeded_workspace("check-third-payload", "golden")
        contract_path = ws / "webhook-contract.json"
        contract = json.loads(contract_path.read_text(encoding="utf-8"))
        event = contract["events"][0]
        extra = json.loads(json.dumps(event["ordered_payloads"][-1]))
        extra["data"]["object"]["attempt"] = 3
        event["ordered_payloads"].append(extra)
        contract_path.write_text(json.dumps(contract, indent=2), encoding="utf-8")
        result = run_cli("check", "--dir", str(ws))
        self.assertEqual(result.returncode, 1)
        self.assertIn("two entries", result.stdout)
        gen = run_cli("gen", "--dir", str(ws))
        self.assertEqual(gen.returncode, 1, "gen must refuse it too")

    def _contract_copy(self, name):
        ws = workspace(name)
        shutil.copyfile(EXAMPLES / "webhook-contract.json", ws / "webhook-contract.json")
        return ws, ws / "webhook-contract.json"

    def test_check_rejects_payload_type_mismatch(self):
        ws, path = self._contract_copy("check-type-mismatch")
        contract = json.loads(path.read_text(encoding="utf-8"))
        contract["events"][0]["payload"]["type"] = "invoice.refunded"
        path.write_text(json.dumps(contract, indent=2), encoding="utf-8")
        result = run_cli("check", "--dir", str(ws))
        self.assertEqual(result.returncode, 1)
        self.assertIn("different event than declared", result.stdout)

    def test_check_rejects_unresolvable_body_id_in_payload(self):
        ws, path = self._contract_copy("check-payload-id")
        contract = json.loads(path.read_text(encoding="utf-8"))
        del contract["events"][0]["payload"]["id"]
        path.write_text(json.dumps(contract, indent=2), encoding="utf-8")
        result = run_cli("check", "--dir", str(ws))
        self.assertEqual(result.returncode, 1)
        self.assertIn("does not resolve in the event payload", result.stdout)

    def test_check_rejects_out_of_range_ack_statuses(self):
        ws, path = self._contract_copy("check-ack-range")
        contract = json.loads(path.read_text(encoding="utf-8"))
        contract["ack"]["success"] = [500]
        contract["ack"]["ignored"] = [301]
        path.write_text(json.dumps(contract, indent=2), encoding="utf-8")
        result = run_cli("check", "--dir", str(ws))
        self.assertEqual(result.returncode, 1)
        self.assertIn("200-299", result.stdout)
        self.assertIn("providers treat anything else as failure", result.stdout)

    def test_check_warns_but_allows_200_on_malformed(self):
        # A body that never parses can never succeed on retry, so acking
        # unparseable bodies with 200 is a defensible choice - a warning with
        # the trade-off, not a hard error.
        ws, path = self._contract_copy("check-ack-malformed-200")
        contract = json.loads(path.read_text(encoding="utf-8"))
        contract["ack"]["malformed"] = [200]
        path.write_text(json.dumps(contract, indent=2), encoding="utf-8")
        result = run_cli("check", "--dir", str(ws))
        self.assertEqual(result.returncode, 0)
        self.assertIn("400-499", result.stdout)
        strict = run_cli("check", "--dir", str(ws), "--strict")
        self.assertEqual(strict.returncode, 1)

    def test_check_rejects_scenarios_object(self):
        ws, path = self._contract_copy("check-scenarios-object")
        contract = json.loads(path.read_text(encoding="utf-8"))
        contract["scenarios"] = {"duplicate": True}
        path.write_text(json.dumps(contract, indent=2), encoding="utf-8")
        result = run_cli("check", "--dir", str(ws))
        self.assertEqual(result.returncode, 1)
        self.assertIn("must be a list", result.stdout)
        self.assertEqual(run_cli("gen", "--dir", str(ws)).returncode, 1)

    def test_check_survives_non_object_contract(self):
        ws, path = self._contract_copy("check-non-object")
        path.write_text(json.dumps([1, 2, 3]), encoding="utf-8")
        result = run_cli("check", "--dir", str(ws))
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("must be a JSON object", result.stdout)
        self.assertNotIn("Traceback", result.stderr)

    def test_check_and_gen_survive_null_values(self):
        # An explicit null is "not set" for optional keys and a shape error for
        # required ones - never a traceback. Regression: events, scenarios and
        # fields.delivery_id set to null crashed check/gen with a TypeError;
        # a null delivery_id even let gen "succeed" and then crashed the
        # follow-up check on the fixtures it had just written.
        cases = [
            (lambda c: c.update({"events": None}), 1, "events must be a non-empty list"),
            (lambda c: c.update({"scenarios": None}), 0, "scenarios is empty"),
            (
                lambda c: c["fields"].update({"delivery_id": None}),
                0,
                "fall back to 'header.x-delivery-id'",
            ),
        ]
        for index, (mutate, check_rc, needle) in enumerate(cases):
            with self.subTest(case=index):
                ws, path = self._contract_copy("check-null-{}".format(index))
                contract = json.loads(path.read_text(encoding="utf-8"))
                mutate(contract)
                path.write_text(json.dumps(contract, indent=2), encoding="utf-8")
                pre_gen = run_cli("check", "--dir", str(ws))
                self.assertEqual(pre_gen.returncode, check_rc, pre_gen.stdout + pre_gen.stderr)
                self.assertIn(needle, pre_gen.stdout)
                gen = run_cli("gen", "--dir", str(ws))
                self.assertNotIn("Traceback", gen.stderr, gen.stdout + gen.stderr)
                post_gen = run_cli("check", "--dir", str(ws))
                self.assertNotIn("Traceback", post_gen.stderr, post_gen.stdout + post_gen.stderr)
                if check_rc == 0:
                    self.assertEqual(post_gen.returncode, 0, post_gen.stdout + post_gen.stderr)

    def test_check_names_the_dir_when_run_from_cwd(self):
        ws = workspace("check-dir-label")
        self.assertEqual(run_cli("init", "--dir", str(ws)).returncode, 0)
        (ws / "test_replay.py").unlink()
        (ws / "test_replay.mjs").unlink()
        result = run_cli("check", "--dir", ".", cwd=str(ws))
        self.assertEqual(result.returncode, 0)
        self.assertIn("check-dir-label", result.stdout)

    def test_check_rejects_colliding_event_dirnames(self):
        # 'invoice/paid' and 'invoice_paid' sanitize to the same fixture
        # directory (as would 'Invoice.paid' vs 'invoice.paid' on the common
        # case-insensitive disks).
        ws, path = self._contract_copy("check-dir-collision")
        contract = json.loads(path.read_text(encoding="utf-8"))
        first = contract["events"][0]
        first["type"] = "invoice/paid"
        first["payload"]["type"] = "invoice/paid"
        second = json.loads(json.dumps(first))
        second["type"] = "invoice_paid"
        second["payload"]["type"] = "invoice_paid"
        contract["events"].append(second)
        path.write_text(json.dumps(contract, indent=2), encoding="utf-8")
        result = run_cli("check", "--dir", str(ws))
        self.assertEqual(result.returncode, 1)
        self.assertIn("map to fixture directory", result.stdout)
        self.assertEqual(run_cli("gen", "--dir", str(ws)).returncode, 1)

    def test_check_warns_on_stale_fixture_directory(self):
        ws = seeded_workspace("check-stale-dir", "golden")
        (ws / "fixtures" / "removed.event").mkdir()
        result = run_cli("check", "--dir", str(ws))
        self.assertEqual(result.returncode, 0, "a stale directory is a warning, not an error")
        self.assertIn("matches no contract event", result.stdout)


class HarnessTests(unittest.TestCase):
    def test_python_harness_passes_golden(self):
        ws = seeded_workspace("harness-py-ok", "golden")
        result = run_harness([sys.executable, "test_replay.py"], ws)
        self.assertEqual(result.returncode, 0, result.combined)
        self.assertIn("OK", result.combined)

    def test_python_harness_fails_naive(self):
        ws = seeded_workspace("harness-py-bad", "naive")
        result = run_harness([sys.executable, "test_replay.py"], ws)
        self.assertNotEqual(result.returncode, 0, "naive handler must fail the replay")
        self.assertIn("fired 2 time(s)", result.combined, "duplicate failure must name the effect")
        self.assertIn("differs from the natural-order", result.combined, "ordering failure must show snapshots")

    def test_node_harness_passes_golden(self):
        if not shutil.which("node"):
            self.skipTest("node is not installed")
        ws = seeded_workspace("harness-node-ok", "golden")
        result = run_harness(["node", "--test", "test_replay.mjs"], ws)
        self.assertEqual(result.returncode, 0, result.combined)

    def test_node_harness_fails_naive(self):
        if not shutil.which("node"):
            self.skipTest("node is not installed")
        ws = seeded_workspace("harness-node-bad", "naive")
        result = run_harness(["node", "--test", "test_replay.mjs"], ws)
        self.assertNotEqual(result.returncode, 0, "naive handler must fail the replay")

    def test_harness_reports_unwired_hooks_as_failure(self):
        ws = seeded_workspace("harness-unwired", "golden")
        (ws / "replay_hooks.py").write_text(
            "def handle(raw_body, headers):\n"
            "    raise NotImplementedError('not wired')\n\n\n"
            "class Probe:\n"
            "    def reset(self): pass\n"
            "    def record(self, *a): pass\n"
            "    def counts(self): return {}\n"
            "    def snapshot(self): return {}\n\n\n"
            "PROBE = Probe()\n",
            encoding="utf-8",
        )
        result = run_harness([sys.executable, "test_replay.py"], ws)
        self.assertNotEqual(result.returncode, 0, "unwired hooks must fail loudly, not pass silently")
        self.assertIn("not wired", result.combined, "the handler's own exception must be visible in the failure")

    def test_python_harness_fails_noop_handler(self):
        ws = seeded_workspace("harness-py-noop", "golden")
        (ws / "replay_hooks.py").write_text(NOOP_HOOKS_PY, encoding="utf-8")
        result = run_harness([sys.executable, "test_replay.py"], ws)
        self.assertNotEqual(result.returncode, 0, "a handler that applies nothing must not pass")
        self.assertIn("must apply it at least", result.combined, "failure must name the effect_min floor")

    def test_node_harness_fails_noop_handler(self):
        if not shutil.which("node"):
            self.skipTest("node is not installed")
        ws = seeded_workspace("harness-node-noop", "golden")
        (ws / "replay_hooks.mjs").write_text(NOOP_HOOKS_MJS, encoding="utf-8")
        result = run_harness(["node", "--test", "test_replay.mjs"], ws)
        self.assertNotEqual(result.returncode, 0, "a handler that applies nothing must not pass")
        self.assertIn("must apply it at least", result.combined, "failure must name the effect_min floor")

    def test_python_order_check_survives_live_reference_snapshot(self):
        ws = seeded_workspace("harness-py-alias", "golden")
        (ws / "replay_hooks.py").write_text(LWW_LIVE_REFERENCE_PY, encoding="utf-8")
        result = run_harness([sys.executable, "test_replay.py"], ws)
        self.assertNotEqual(result.returncode, 0, "last-write-wins must fail the order scenarios")
        self.assertIn(
            "differs from the natural-order",
            result.combined,
            "order-equivalence must fire even when snapshot() returns a live reference",
        )

    def test_node_order_check_survives_live_reference_snapshot(self):
        if not shutil.which("node"):
            self.skipTest("node is not installed")
        ws = seeded_workspace("harness-node-alias", "golden")
        (ws / "replay_hooks.mjs").write_text(LWW_LIVE_REFERENCE_MJS, encoding="utf-8")
        result = run_harness(["node", "--test", "test_replay.mjs"], ws)
        self.assertNotEqual(result.returncode, 0, "last-write-wins must fail the order scenarios")
        self.assertIn(
            "differs from the natural-order",
            result.combined,
            "order-equivalence must fire even when snapshot() returns a live reference",
        )

    def test_node_snapshot_comparison_ignores_key_insertion_order(self):
        if not shutil.which("node"):
            self.skipTest("node is not installed")
        ws = seeded_workspace("harness-node-keyorder", "golden")
        (ws / "replay_hooks.mjs").write_text(COMMUTATIVE_MERGE_MJS, encoding="utf-8")
        result = run_harness(["node", "--test", "test_replay.mjs"], ws)
        self.assertEqual(
            result.returncode,
            0,
            "a correct handler whose snapshot key order depends on arrival must pass: " + result.combined,
        )

    def test_python_harness_supports_async_handler(self):
        ws = seeded_workspace("harness-py-async", "golden")
        shutil.copyfile(EXAMPLES / "hooks" / "replay_hooks_golden_async.py", ws / "replay_hooks.py")
        result = run_harness([sys.executable, "test_replay.py"], ws)
        self.assertEqual(result.returncode, 0, result.combined)

    def test_node_harness_supports_async_handler(self):
        if not shutil.which("node"):
            self.skipTest("node is not installed")
        ws = seeded_workspace("harness-node-async", "golden")
        shutil.copyfile(EXAMPLES / "hooks" / "replay_hooks_golden_async.mjs", ws / "replay_hooks.mjs")
        result = run_harness(["node", "--test", "test_replay.mjs"], ws)
        self.assertEqual(result.returncode, 0, result.combined)

    def test_python_harness_refuses_empty_snapshot(self):
        ws = seeded_workspace("harness-py-emptysnap", "golden")
        (ws / "replay_hooks.py").write_text(EMPTY_SNAPSHOT_PY, encoding="utf-8")
        result = run_harness([sys.executable, "test_replay.py"], ws)
        self.assertNotEqual(
            result.returncode, 0, "a snapshot() that returns {} must not pass the order check"
        )
        self.assertIn(
            "would pass vacuously",
            result.combined,
            "the failure must say the order check was vacuous, not compare two empty snapshots",
        )

    def test_node_harness_refuses_map_snapshot(self):
        if not shutil.which("node"):
            self.skipTest("node is not installed")
        ws = seeded_workspace("harness-node-mapsnap", "golden")
        (ws / "replay_hooks.mjs").write_text(MAP_SNAPSHOT_MJS, encoding="utf-8")
        result = run_harness(["node", "--test", "test_replay.mjs"], ws)
        self.assertNotEqual(
            result.returncode, 0, "a snapshot() that returns a Map must not pass (it serializes as {})"
        )
        self.assertIn(
            "not JSON-serializable",
            result.combined,
            "the failure must name the unserializable snapshot, not silently render it as {}",
        )

    def test_python_harness_awaits_async_probe(self):
        ws = seeded_workspace("harness-py-asyncprobe", "golden")
        (ws / "replay_hooks.py").write_text(ASYNC_PROBE_PY, encoding="utf-8")
        result = run_harness([sys.executable, "test_replay.py"], ws)
        self.assertEqual(
            result.returncode, 0, "a correct handler with coroutine probe methods must pass: " + result.combined
        )

    def test_node_harness_awaits_async_probe(self):
        if not shutil.which("node"):
            self.skipTest("node is not installed")
        ws = seeded_workspace("harness-node-asyncprobe", "golden")
        (ws / "replay_hooks.mjs").write_text(ASYNC_PROBE_MJS, encoding="utf-8")
        result = run_harness(["node", "--test", "test_replay.mjs"], ws)
        self.assertEqual(
            result.returncode, 0, "a correct handler with async probe methods must pass: " + result.combined
        )

    def test_python_harness_fails_missing_return_status(self):
        ws = seeded_workspace("harness-py-nonestatus", "golden")
        (ws / "replay_hooks.py").write_text(MISSING_RETURN_PY, encoding="utf-8")
        result = run_harness([sys.executable, "test_replay.py"], ws)
        self.assertNotEqual(
            result.returncode, 0, "a handler that forgets to return a status must fail (None is not a status)"
        )
        self.assertIn("answered None", result.combined)

    def test_node_harness_fails_null_status(self):
        if not shutil.which("node"):
            self.skipTest("node is not installed")
        ws = seeded_workspace("harness-node-nullstatus", "golden")
        (ws / "replay_hooks.mjs").write_text(MISSING_RETURN_MJS, encoding="utf-8")
        result = run_harness(["node", "--test", "test_replay.mjs"], ws)
        self.assertNotEqual(
            result.returncode, 0, "a handler that returns null must fail the status assertion"
        )
        self.assertIn("answered null", result.combined)

    def test_python_two_argument_handler_runs_the_lost_attempt(self):
        ws = seeded_workspace("harness-py-twoarg", "golden")
        (ws / "replay_hooks.py").write_text(TWO_ARG_NO_DEDUPE_PY, encoding="utf-8")
        result = run_harness([sys.executable, "test_replay.py"], ws)
        self.assertNotEqual(result.returncode, 0, "a two-argument no-dedupe handler must fail the retry scenario")
        self.assertIn(
            "retry_after_failure: side effect 'mark_invoice_paid' fired 2 time(s)",
            result.combined,
            "the discarded first attempt must actually run so the retry is checked",
        )


class DataHygieneTests(unittest.TestCase):
    def test_scanner_catches_common_secret_shapes(self):
        sys.path.insert(0, str(ROOT / "scripts"))
        try:
            import webcontract

            cases = {
                "sk_" + "live_abcdefghijklmnop": "error",
                "whsec_" + "a1b2c3d4e5f6a1b2c3d4": "error",
                "ghp_" + "abcdefghijklmnopqrstuvwxyz012345": "error",
                "xoxb-" + "123456789-abcdefghijklmnop": "error",
                "AKIA" + "0123456789ABCDEF": "error",
                "-----BEGIN RSA " + "PRIVATE KEY-----": "error",
                "contact@example.com": None,
                "billing@shop.de": "error",
            }
            # Values are assembled at run time so the source holds no complete
            # secret-shaped literal; confirm the assembled header is the full one.
            pem_header = [v for v in cases if v.endswith("PRIVATE KEY-----")]
            self.assertEqual(len(pem_header), 1)
            self.assertEqual(len(pem_header[0]), 31)
            self.assertTrue(pem_header[0].startswith("-----BEGIN RSA "))
            for value, expected in cases.items():
                if expected is None:
                    continue
                problems = webcontract.scan_data_hygiene({"value": value}, "t")
                self.assertTrue(
                    any(p.severity == expected for p in problems),
                    "{} should be flagged as {}".format(value, expected),
                )
            self.assertEqual(webcontract.scan_data_hygiene({"value": "contact@example.com"}, "t"), [])
            self.assertEqual(webcontract.scan_data_hygiene({"value": "evt_test_0001"}, "t"), [])
        finally:
            sys.path.pop(0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
