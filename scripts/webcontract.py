#!/usr/bin/env python3
"""webcontract: provider-neutral delivery contracts and synthetic webhook fixtures.

Zero-dependency CLI (Python 3.9+, standard library only).

Commands
--------
init   Scaffold a delivery contract, replay harness and fixture directory in the
       target project. Creates missing files only; never overwrites.
gen    Expand the contract into deterministic synthetic fixture files
       (one JSON file per event type x delivery scenario).
check  Validate the contract and fixtures: structure, scenario invariants,
       completeness, and data hygiene (no secret-like or real-looking values).

All paths are relative to --dir (default: tests/webhooks inside the target repo).

Exit codes: 0 = clean (warnings allowed), 1 = validation errors, 2 = usage error.

This tool is offline: it reads and writes only local files.
"""

from __future__ import annotations

import argparse
import copy
import json
import re
import sys
from pathlib import Path

VERSION = "0.1.0"
CONTRACT_NAME = "webhook-contract.json"
FIXTURES_DIRNAME = "fixtures"
README_NAME = "README.md"

HARNESS_PY = "test_replay.py"
HARNESS_MJS = "test_replay.mjs"
HOOKS_PY = "replay_hooks.py"
HOOKS_MJS = "replay_hooks.mjs"

# Marker that init leaves in the hook stubs; check warns while it is present.
TODO_MARKER = "WEBHOOK-CONTRACT:TODO"

PLUGIN_ROOT = Path(__file__).resolve().parent.parent
TEMPLATES_DIR = PLUGIN_ROOT / "templates"

# The six delivery scenarios the generator knows about. Each targets one classic
# production failure mode.
SCENARIOS = [
    "duplicate",
    "retry_after_failure",
    "out_of_order",
    "cross_key_interleave",
    "unknown_type",
    "malformed",
]

SCENARIO_DESCRIPTIONS = {
    "duplicate": "The same event is delivered twice. Handler must apply it once.",
    "retry_after_failure": "Response to the first delivery is lost, the provider retries. Handler must not apply twice.",
    "out_of_order": "Two events on the same ordering key arrive newest-first. Final state must match natural order.",
    "cross_key_interleave": "Events on different ordering keys interleave; one key arrives out of order. Keys must not corrupt each other.",
    "unknown_type": "An event type the handler does not know. Must be acknowledged without side effects.",
    "malformed": "Body is not valid JSON. Must fail in a controlled way with no side effects.",
}

# Scenarios that need more than one distinct payload per event type.
SCENARIOS_NEED_KEY = ("out_of_order", "cross_key_interleave")
SCENARIOS_NEED_SEQ = ("out_of_order", "cross_key_interleave")

CANONICAL_JSON = {"sort_keys": True, "separators": (",", ":")}


# ---------------------------------------------------------------------------
# Small utilities
# ---------------------------------------------------------------------------


class Problem:
    """One validation finding. severity is 'error' or 'warning'."""

    def __init__(self, severity: str, file: str, path: str, message: str) -> None:
        self.severity = severity
        self.file = file
        self.path = path
        self.message = message

    def to_dict(self) -> dict:
        return {
            "severity": self.severity,
            "file": self.file,
            "path": self.path,
            "message": self.message,
        }

    def to_line(self) -> str:
        return "{} {}: {}".format(self.severity.upper(), self.file, self.message)


def error(file: str, message: str, path: str = "") -> Problem:
    return Problem("error", file, path, message)


def warning(file: str, message: str, path: str = "") -> Problem:
    return Problem("warning", file, path, message)


def load_json(path: Path):
    """Parse a JSON file; raise ValueError with a readable message on failure."""
    try:
        with path.open("r", encoding="utf-8") as fh:
            return json.load(fh)
    except json.JSONDecodeError as exc:
        raise ValueError("invalid JSON at line {}, column {}: {}".format(exc.lineno, exc.colno, exc.msg))
    except OSError as exc:
        raise ValueError("cannot read file: {}".format(exc))


def canonical(body) -> str:
    return json.dumps(body, **CANONICAL_JSON)


def path_segments(path: str) -> list:
    """Split a delivery-rooted path like 'body.data.object.id' into segments."""
    return [seg for seg in path.split(".") if seg]


def as_list(value) -> list:
    """Optional list-valued contract keys may be null (\"not set\"); iterate nothing.

    validate_contract reports the shape problem; the consumers here must not
    crash on it (a .get(key, []) default does not help when the key is present
    with an explicit null).
    """
    return value if isinstance(value, list) else []


def path_or(value, default: str) -> str:
    """A null or non-string field path falls back to the default.

    validate_contract reports the shape problem; the generator and the fixture
    checks must still run (and for a null delivery_id the fallback is exactly
    what the contract's own warning promises).
    """
    return value if isinstance(value, str) and value else default


def get_path(delivery: dict, path: str):
    """Resolve a delivery-rooted path ('body.x.y' or 'header.x-delivery-id').

    Returns (found, value). Header names are matched case-insensitively.
    """
    segments = path_segments(path)
    if not segments:
        return False, None
    root, rest = segments[0], segments[1:]
    if root == "body":
        node = delivery.get("body")
        if node is None:
            return False, None
        for seg in rest:
            if not isinstance(node, dict) or seg not in node:
                return False, None
            node = node[seg]
        return True, node
    if root in ("header", "headers"):
        headers = delivery.get("headers") or {}
        if not rest:
            return False, None
        name = ".".join(rest).lower()
        for key, value in headers.items():
            if key.lower() == name:
                return True, value
        return False, None
    return False, None


def set_path(delivery: dict, path: str, value) -> bool:
    """Set a value at a delivery-rooted path. Returns False if the path is absent."""
    segments = path_segments(path)
    if not segments:
        return False
    root, rest = segments[0], segments[1:]
    if root == "body":
        node = delivery.get("body")
    elif root in ("header", "headers"):
        headers = delivery.setdefault("headers", {})
        if not rest:
            return False
        # Normalize to the exact key case already present, if any.
        name = ".".join(rest)
        for key in list(headers.keys()):
            if key.lower() == name.lower():
                headers[key] = value
                return True
        headers[name] = value
        return True
    else:
        return False
    if node is None or not isinstance(node, dict):
        return False
    for seg in rest[:-1]:
        nxt = node.get(seg)
        if not isinstance(nxt, dict):
            return False
        node = nxt
    if rest and rest[-1] not in node:
        return False
    if rest:
        node[rest[-1]] = value
    return True


def walk_strings(node, prefix="$"):
    """Yield (json_path, string_value) for every string in a JSON structure."""
    if isinstance(node, dict):
        for key, value in node.items():
            yield from walk_strings(value, "{}.{}".format(prefix, key))
    elif isinstance(node, list):
        for idx, value in enumerate(node):
            yield from walk_strings(value, "{}[{}]".format(prefix, idx))
    elif isinstance(node, str):
        yield prefix, node


# ---------------------------------------------------------------------------
# Data hygiene: secret and personal-data scanning
# ---------------------------------------------------------------------------

SECRET_RULES = [
    (re.compile(r"sk_live_[A-Za-z0-9]{10,}"), "possible Stripe live secret key"),
    (re.compile(r"rk_live_[A-Za-z0-9]{10,}"), "possible Stripe live restricted key"),
    (re.compile(r"whsec_[A-Za-z0-9]{12,}"), "possible webhook signing secret"),
    (re.compile(r"gh[pousr]_[A-Za-z0-9]{20,}"), "possible GitHub token"),
    (re.compile(r"github_pat_[A-Za-z0-9_]{20,}"), "possible GitHub fine-grained token"),
    (re.compile(r"xox[abprs]-[A-Za-z0-9-]{10,}"), "possible Slack token"),
    (re.compile(r"AKIA[0-9A-Z]{16}"), "possible AWS access key id"),
    (re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"), "private key material"),
    (re.compile(r"eyJ[A-Za-z0-9_-]{15,}\.[A-Za-z0-9_-]{10,}"), "possible JWT"),
]

EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@([A-Za-z0-9.-]+)")
SAFE_EMAIL_SUFFIXES = ("example.com", "example.org", "example.net", ".test", ".example", ".invalid", ".localhost")


def email_domain_is_safe(domain: str) -> bool:
    """True for reserved example/test domains and their subdomains.

    The boundary matters: `notexample.com` must NOT pass as `example.com`.
    """
    for suffix in SAFE_EMAIL_SUFFIXES:
        if suffix.startswith("."):
            if domain == suffix[1:] or domain.endswith(suffix):
                return True
        elif domain == suffix or domain.endswith("." + suffix):
            return True
    return False


def luhn_ok(digits: str) -> bool:
    """Standard Luhn checksum over an ASCII digit string."""
    total = 0
    for i, ch in enumerate(reversed(digits)):
        d = ord(ch) - 48
        if i % 2 == 1:
            d *= 2
            if d > 9:
                d -= 9
        total += d
    return total % 10 == 0


def looks_like_card_number(digits: str) -> bool:
    """Luhn-valid AND inside a range a real card network issues.

    The checksum alone passes roughly one arbitrary digit string in ten
    (snowflake ids, timestamps), which would make the guard block `gen` on
    ordinary ids. Requiring an IIN the major networks actually assign keeps
    the flag on card-shaped values: Visa (4), Mastercard (51-55, 2221-2720),
    Amex (34, 37), Discover (6011, 65), JCB (35).
    """
    if not luhn_ok(digits):
        return False
    if digits.startswith(("4", "34", "37", "35", "65", "6011")):
        return True
    two = int(digits[:2])
    if 51 <= two <= 55:
        return True
    return 2221 <= int(digits[:4]) <= 2720


# Card numbers published as test cards by payment providers (Stripe's set is
# the widest known, plus the standard JCB test cards - the network-range gate
# below would otherwise flag them). They pass Luhn on purpose, so they cannot
# be caught by the checksum alone - they are the allowlist; any other value
# that looks like a card is treated as a possible real card.
TEST_CARD_NUMBERS = frozenset(
    {
        "4242424242424242",
        "4111111111111111",
        "4222222222222220",
        "4000056655665556",
        "5555555555554444",
        "5200828282828210",
        "5105105105105100",
        "378282246310005",
        "371449635398431",
        "6011111111111117",
        "6011000990139424",
        "30569309025904",
        "4005519200000004",
        "4003440000000007",
        "3530111333300000",
        "3566002020360505",
    }
)
CARD_VALUE_RE = re.compile(r"^[0-9][0-9 -]{6,}$")

LONG_TOKEN_RE = re.compile(r"^[A-Za-z0-9+/=_-]{40,}$")
SENSITIVE_KEYS = {
    "password",
    "secret",
    "token",
    "api_key",
    "apikey",
    "client_secret",
    "signing_secret",
    "webhook_secret",
    "private_key",
}


def scan_data_hygiene(data, label: str) -> list:
    """Scan all strings in a JSON structure for secret-like or real-looking values."""
    found = []
    for path, text in walk_strings(data):
        for pattern, what in SECRET_RULES:
            if pattern.search(text):
                found.append(error(label, "{} at {}".format(what, text[:24]), path))
        for match in EMAIL_RE.finditer(text):
            domain = match.group(1).lower()
            if not email_domain_is_safe(domain):
                found.append(
                    error(
                        label,
                        "email address outside reserved example/test domains: {}".format(match.group(0)),
                        path,
                    )
                )
        # A value that is (almost) nothing but digits: if it looks like a
        # card number (Luhn checksum plus a card-network range), only
        # documented test cards may pass.
        compact = text.strip().replace(" ", "").replace("-", "")
        if compact.isdigit() and 13 <= len(compact) <= 19:
            if looks_like_card_number(compact) and compact not in TEST_CARD_NUMBERS:
                found.append(
                    error(
                        label,
                        "number looks like a card (Luhn checksum and card-network range) and is not a documented test card: {}...".format(compact[:6]),
                        path,
                    )
                )
        stripped = text.strip()
        if len(stripped) >= 40 and LONG_TOKEN_RE.match(stripped):
            has_digit = any(c.isdigit() for c in stripped)
            has_alpha = any(c.isalpha() for c in stripped)
            marker = any(m in stripped.lower() for m in ("test", "example", "placeholder", "dummy", "todo"))
            if has_digit and has_alpha and not marker:
                found.append(
                    warning(label, "long random-looking string (may be a real token): {}...".format(stripped[:16]), path)
                )
    # Key-name heuristic: sensitive key names holding non-placeholder values.
    def walk_keys(node, prefix="$"):
        if isinstance(node, dict):
            for key, value in node.items():
                if (
                    isinstance(value, str)
                    and key.lower() in SENSITIVE_KEYS
                    and len(value) > 8
                    and not any(m in value.lower() for m in ("test", "example", "placeholder", "dummy"))
                ):
                    found.append(warning(label, "sensitive key '{}' holds a non-placeholder value".format(key), "{}.{}".format(prefix, key)))
                walk_keys(value, "{}.{}".format(prefix, key))
        elif isinstance(node, list):
            for idx, value in enumerate(node):
                walk_keys(value, "{}[{}]".format(prefix, idx))

    walk_keys(data)
    return found


# ---------------------------------------------------------------------------
# Contract validation
# ---------------------------------------------------------------------------


def validate_contract(contract) -> list:
    """Validate a parsed contract document. Returns a list of Problem."""
    problems = []
    label = CONTRACT_NAME

    if not isinstance(contract, dict):
        return [error(label, "contract must be a JSON object")]

    if contract.get("contract_version") != 1:
        problems.append(error(label, "contract_version must be 1", "contract_version"))

    provider = contract.get("provider")
    if not isinstance(provider, str) or not provider.strip():
        problems.append(error(label, "provider must be a non-empty string", "provider"))

    fields = contract.get("fields")
    if not isinstance(fields, dict):
        problems.append(error(label, "fields must be an object with 'id' and 'type' paths", "fields"))
        fields = {}
    else:
        for required in ("id", "type"):
            path = fields.get(required)
            if not isinstance(path, str) or not path.startswith(("body.", "header.")):
                problems.append(
                    error(label, "fields.{} must be a path starting with 'body.' or 'header.'".format(required), "fields.{}".format(required))
                )
        delivery = fields.get("delivery_id")
        if delivery is None:
            problems.append(warning(label, "fields.delivery_id is not set; duplicate-delivery checks fall back to 'header.x-delivery-id'", "fields.delivery_id"))
        elif not isinstance(delivery, str) or not delivery.startswith(("body.", "header.")):
            problems.append(error(label, "fields.delivery_id must start with 'body.' or 'header.'", "fields.delivery_id"))

    ack = contract.get("ack")
    if not isinstance(ack, dict):
        problems.append(error(label, "ack must be an object with 'success', 'ignored' and 'malformed' lists", "ack"))
        ack = {}
    else:
        # Providers treat anything outside 2xx as a failed delivery (Stripe
        # counts redirects and 4xx as failures), so a success/ignored list
        # that promises e.g. 500 would encode "succeed by failing" - the
        # provider would retry forever. ack.malformed is different: a body
        # that never parses can never succeed on retry, so teams that ack
        # unparseable bodies with 200 are making a defensible choice - say
        # so, but do not block them.
        ack_ranges = {"success": (200, 299), "ignored": (200, 299), "malformed": (400, 499)}
        for name in ("success", "ignored", "malformed"):
            value = ack.get(name)
            if not isinstance(value, list) or not value or not all(isinstance(v, int) and not isinstance(v, bool) for v in value):
                problems.append(error(label, "ack.{} must be a non-empty list of HTTP status integers".format(name), "ack.{}".format(name)))
                continue
            low, high = ack_ranges[name]
            outside = [v for v in value if not low <= v <= high]
            if outside:
                message = "ack.{} statuses are usually within {}-{}; got {}".format(name, low, high, ", ".join(str(v) for v in outside))
                if name == "malformed":
                    message += (
                        " - 400-499 tells the provider the delivery failed; answering 200 is a defensible"
                        " choice (a body that never parses can never succeed on retry), so this is only a warning"
                    )
                else:
                    message += " (providers treat anything else as failure and retry)"
                severity = error if name != "malformed" else warning
                problems.append(severity(label, message, "ack.{}".format(name)))

    scenarios = contract.get("scenarios")
    if scenarios is not None and not isinstance(scenarios, list):
        problems.append(error(label, "scenarios must be a list of scenario names", "scenarios"))
        scenarios = []
    elif not isinstance(scenarios, list) or not scenarios:
        problems.append(warning(label, "scenarios is empty; nothing will be generated", "scenarios"))
        scenarios = []
    for name in scenarios:
        if name not in SCENARIOS:
            problems.append(error(label, "unknown scenario '{}' (known: {})".format(name, ", ".join(SCENARIOS)), "scenarios"))

    sequence_field = contract.get("sequence_field")
    if sequence_field is not None and (not isinstance(sequence_field, str) or not sequence_field.startswith("body.")):
        problems.append(error(label, "sequence_field must be a 'body.' path or null", "sequence_field"))

    events = contract.get("events")
    if not isinstance(events, list) or not events:
        problems.append(error(label, "events must be a non-empty list", "events"))
        events = []

    # Fixture directories are derived from event type names. Two events whose
    # sanitized names agree would write into the same folder and clobber each
    # other's fixtures - compare case-insensitively, since the common disks
    # (Windows, macOS default) treat the names as equal anyway.
    by_dir = {}
    for event in events:
        if isinstance(event, dict) and isinstance(event.get("type"), str):
            by_dir.setdefault(safe_dirname(event["type"]).lower(), []).append(event["type"])
    for dirname, etypes in sorted(by_dir.items()):
        if len(etypes) > 1:
            problems.append(
                error(
                    label,
                    "event types {} all map to fixture directory '{}' - rename one event type so the directories differ".format(
                        ", ".join("'{}'".format(t) for t in etypes), dirname
                    ),
                    "events",
                )
            )

    seen_types = set()
    for idx, event in enumerate(events):
        where = "events[{}]".format(idx)
        if not isinstance(event, dict):
            problems.append(error(label, "event must be an object", where))
            continue
        etype = event.get("type")
        if not isinstance(etype, str) or not etype.strip():
            problems.append(error(label, "event.type must be a non-empty string", where + ".type"))
            etype = None
        elif etype in seen_types:
            problems.append(error(label, "duplicate event type '{}'".format(etype), where + ".type"))
        else:
            seen_types.add(etype)

        payload = event.get("payload")
        if not isinstance(payload, dict):
            problems.append(error(label, "event.payload must be an object", where + ".payload"))
            payload = {}

        # The sample body must look like the event it claims to be: when the
        # type/id fields are body-rooted, they must resolve here and the type
        # must agree - otherwise the fixture delivers a different event than
        # the one declared. ordered_payloads entries are exempt from the type
        # agreement: variants may deliberately carry another type.
        if isinstance(etype, str):
            type_path = fields.get("type") if isinstance(fields, dict) else None
            if isinstance(type_path, str) and type_path.startswith("body."):
                found, value = get_path({"body": payload}, type_path)
                if not found:
                    problems.append(
                        error(label, "contract field type '{}' does not resolve in the event payload".format(type_path), where + ".payload")
                    )
                elif value != etype:
                    problems.append(
                        error(
                            label,
                            "payload {} is {!r} but the event type is {!r} - the fixture would deliver a different event than declared".format(
                                type_path, value, etype
                            ),
                            where + ".payload",
                        )
                    )
            id_path = fields.get("id") if isinstance(fields, dict) else None
            if isinstance(id_path, str) and id_path.startswith("body.") and not get_path({"body": payload}, id_path)[0]:
                problems.append(
                    error(label, "contract field id '{}' does not resolve in the event payload".format(id_path), where + ".payload")
                )

        ordered = event.get("ordered_payloads")
        if ordered is not None:
            if not isinstance(ordered, list) or not ordered or not all(isinstance(p, dict) for p in ordered):
                problems.append(error(label, "ordered_payloads must be a non-empty list of payload objects", where + ".ordered_payloads"))
                ordered = None
            elif len(ordered) > 2:
                problems.append(
                    error(
                        label,
                        "ordered_payloads supports two entries - the older and the newer body; got {}".format(len(ordered)),
                        where + ".ordered_payloads",
                    )
                )

        ordering_key = event.get("ordering_key")
        skips = event.get("skip_scenarios", [])
        skips = skips if isinstance(skips, list) else []
        event_scenarios = [s for s in scenarios if s not in skips]
        needs_key = any(s in SCENARIOS_NEED_KEY for s in event_scenarios)
        if needs_key:
            if not isinstance(ordering_key, str) or not ordering_key.startswith("body."):
                problems.append(
                    error(label, "ordering_key must be a 'body.' path when key-based scenarios are enabled", where + ".ordering_key")
                )
            else:
                targets = ordered if ordered is not None else [payload]
                for pidx, p in enumerate(targets):
                    found, _ = get_path({"body": p}, ordering_key)
                    if not found:
                        problems.append(
                            error(
                                label,
                                "ordering_key path does not resolve in {}".format("ordered_payloads[{}]".format(pidx) if ordered is not None else "payload"),
                                where + ".ordering_key",
                            )
                        )
                        break

        needs_seq = any(s in SCENARIOS_NEED_SEQ for s in event_scenarios)
        if needs_seq:
            if not isinstance(sequence_field, str):
                problems.append(
                    error(
                        label,
                        "sequence_field must be set at the top level when out_of_order/cross_key_interleave are enabled",
                        "sequence_field",
                    )
                )
            else:
                targets = ordered if ordered is not None else [payload]
                for pidx, p in enumerate(targets):
                    found, value = get_path({"body": p}, sequence_field)
                    if not found or not isinstance(value, int):
                        problems.append(
                            error(
                                label,
                                "sequence_field must resolve to an integer in {}".format(
                                    "ordered_payloads[{}]".format(pidx) if ordered is not None else "payload"
                                ),
                                "sequence_field",
                            )
                        )
                        break

        effects = event.get("side_effects", [])
        if not isinstance(effects, list):
            problems.append(error(label, "side_effects must be a list", where + ".side_effects"))
            effects = []
        if not effects:
            problems.append(
                warning(
                    label,
                    "{} declares no side effects - nothing to count; the harness can only check statuses and ordering".format(
                        etype if isinstance(etype, str) else where
                    ),
                    where + ".side_effects",
                )
            )
        names = set()
        for eidx, eff in enumerate(effects):
            ewhere = "{}.side_effects[{}]".format(where, eidx)
            if not isinstance(eff, dict) or not isinstance(eff.get("name"), str) or not eff["name"].strip():
                problems.append(error(label, "side effect needs a non-empty 'name'", ewhere))
                continue
            name = eff["name"]
            if name in names:
                problems.append(error(label, "duplicate side effect name '{}'".format(name), ewhere))
            names.add(name)
            if not isinstance(eff.get("idempotent"), bool):
                problems.append(warning(label, "side effect '{}' should declare idempotent: true or false".format(name), ewhere))
            optional_flag = eff.get("optional")
            if optional_flag is not None and not isinstance(optional_flag, bool):
                problems.append(error(label, "side effect '{}' optional must be a boolean".format(name), ewhere))
            guard = eff.get("guard")
            if not isinstance(guard, str) or not guard.strip():
                problems.append(warning(label, "side effect '{}' has no guard description".format(name), ewhere))
            elif "TODO" in guard:
                problems.append(warning(label, "side effect '{}' still has a TODO guard".format(name), ewhere))

        skips = event.get("skip_scenarios", [])
        if not isinstance(skips, list) or any(s not in SCENARIOS for s in skips):
            problems.append(error(label, "skip_scenarios must be a list of known scenario names", where + ".skip_scenarios"))

    for path, text in walk_strings(contract):
        if TODO_MARKER not in text and "TODO" in text:
            problems.append(warning(label, "contract still contains a TODO", path))
            break  # one reminder is enough; guards get their own messages

    problems.extend(scan_data_hygiene(contract, label))
    return problems


# ---------------------------------------------------------------------------
# Fixture generation
# ---------------------------------------------------------------------------


def safe_dirname(event_type: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]", "_", event_type)


def delivery_id_header(contract: dict) -> str:
    path = (contract.get("fields") or {}).get("delivery_id") or "header.x-delivery-id"
    segments = path_segments(path)
    if segments and segments[0] in ("header", "headers") and len(segments) > 1:
        return ".".join(segments[1:])
    return "x-delivery-id"


class IdAllocator:
    """Deterministic synthetic id generator: evt_test_0001, del_test_0001, ..."""

    def __init__(self) -> None:
        self.events = 0
        self.deliveries = 0

    def event(self, event_idx: int, variant: int) -> str:
        self.events += 1
        return "evt_test_{:04d}_{:02d}".format(event_idx + 1, variant)

    def delivery(self) -> str:
        self.deliveries += 1
        return "del_test_{:04d}".format(self.deliveries)


def set_body_path(body: dict, path: str, value) -> bool:
    """Set a value at a body-rooted path like 'body.type'. Returns False if absent."""
    segments = path_segments(path)
    if not segments or segments[0] != "body" or len(segments) < 2:
        return False
    node = body
    for seg in segments[1:-1]:
        nxt = node.get(seg) if isinstance(node, dict) else None
        if not isinstance(nxt, dict):
            return False
        node = nxt
    node[segments[-1]] = value
    return True


def make_delivery(
    ids: IdAllocator,
    body,
    delay_ms: int,
    contract: dict,
    fail_attempt: bool = False,
    event_type: str = "",
    event_id=None,
    delivery_id=None,
) -> dict:
    """One synthetic delivery.

    Every header-rooted ``fields.*`` path is populated on EVERY delivery, so
    header-keyed providers (GitHub, Shopify) present a complete envelope in
    all scenarios - not only in unknown_type. A broken body still arrives
    with its headers (malformed), exactly like a real delivery.
    """
    fields = contract.get("fields") or {}
    delivery = {
        "seq": 0,  # filled by the caller
        "delay_ms": delay_ms,
        "headers": {
            "content-type": "application/json",
            delivery_id_header(contract): delivery_id if delivery_id is not None else ids.delivery(),
        },
        "body": body,
        "raw_body": None,
        "fail_attempt": fail_attempt,
    }
    id_field = fields.get("id")
    if isinstance(id_field, str) and id_field.startswith("header.") and event_id is not None:
        set_path(delivery, id_field, event_id)
    type_field = fields.get("type")
    if isinstance(type_field, str) and type_field.startswith("header.") and event_type:
        set_path(delivery, type_field, event_type)
    return delivery


def event_payloads(event: dict) -> list:
    """Payload list used for multi-variant scenarios (ordered_payloads or [payload])."""
    ordered = event.get("ordered_payloads")
    if isinstance(ordered, list) and ordered:
        return ordered
    return [event.get("payload") or {}]


def build_fixtures_for_event(event: dict, event_idx: int, contract: dict, ids: IdAllocator, notes: list) -> dict:
    """Return {scenario_name: fixture_document} for one event."""
    etype = event["type"]
    scenarios = [s for s in as_list(contract.get("scenarios")) if s not in as_list(event.get("skip_scenarios"))]
    ordering_key = event.get("ordering_key")
    ack = contract.get("ack") or {}
    ack_success = ack.get("success", [200])
    ack_ignored = ack.get("ignored", ack_success)
    ack_malformed = ack.get("malformed", [400])
    effect_entries = [eff for eff in as_list(event.get("side_effects")) if isinstance(eff, dict) and eff.get("name")]
    effects = [eff["name"] for eff in effect_entries]
    # An optional effect fires only under a condition (a threshold, a status
    # branch), so it may legitimately not fire in a scenario: its floor is 0
    # everywhere while the budget still caps it.
    optional_effects = {eff["name"] for eff in effect_entries if eff.get("optional") is True}
    base_payload = copy.deepcopy(event.get("payload") or {})
    payloads = event_payloads(event)
    header = delivery_id_header(contract)
    out = {}

    fields_map = contract.get("fields") or {}
    id_path = path_or(fields_map.get("id"), "body.id")
    # When the provider's delivery id IS the event identity (GitHub: the
    # X-GitHub-Delivery GUID is both), a redelivery keeps the same value.
    # Handing the duplicate a fresh id would make a handler that dedupes on
    # that header - the provider-documented way - apply twice: a false failure.
    redelivery_keeps_delivery_id = id_path.lower() == path_or(fields_map.get("delivery_id"), "header.x-delivery-id").lower()

    def identity_id(variant: int) -> str:
        """Value for fields.id when header-rooted. Where the delivery id IS the
        event identity, one delivery-shaped value serves both roles."""
        if redelivery_keeps_delivery_id:
            return ids.delivery()
        return ids.event(event_idx, variant)

    def new_body(payload, variant: int) -> dict:
        body = copy.deepcopy(payload)
        if isinstance(body, dict):
            set_body_path(body, id_path, ids.event(event_idx, variant))
        return body

    def key_value_of(payload) -> str:
        found, value = get_path({"body": payload}, ordering_key) if ordering_key else (False, None)
        return str(value) if found else ""

    def fixture(
        scenario: str, deliveries: list, statuses: list, budget: dict, reference, extra_note: str = "", minimum: dict = None
    ) -> dict:
        finalize = []
        for seq, d in enumerate(deliveries, start=1):
            d["seq"] = seq
            found, kvalue = get_path(d, ordering_key) if ordering_key else (False, None)
            oseq = seq
            if contract.get("sequence_field"):
                sfound, svalue = get_path(d, contract["sequence_field"])
                if sfound and isinstance(svalue, int):
                    oseq = svalue
            d["order_key"] = str(kvalue) if found else ""
            d["order_seq"] = oseq
            finalize.append(d)
        description = SCENARIO_DESCRIPTIONS[scenario]
        if extra_note:
            description += " " + extra_note
        return {
            "fixture_version": 1,
            "generated_by": "webcontract/{}".format(VERSION),
            "scenario": scenario,
            "event_type": etype,
            "description": description,
            "deliveries": finalize,
            "expectations": {
                "statuses": statuses,
                "effect_min": dict(minimum) if minimum else {},
                "effect_budget": budget,
                "reference": reference,
            },
        }

    budget_once = {name: 1 for name in effects}
    budget_zero = {name: 0 for name in effects}
    floor_once = {name: (0 if name in optional_effects else 1) for name in effects}
    floor_two = {name: (0 if name in optional_effects else 2) for name in effects}

    if "duplicate" in scenarios:
        body = new_body(base_payload, 1)
        event_id = identity_id(1)
        shared_delivery = event_id if redelivery_keeps_delivery_id else None
        d1 = make_delivery(ids, body, 0, contract, event_type=etype, event_id=event_id, delivery_id=shared_delivery)
        d2 = make_delivery(
            ids, copy.deepcopy(body), 300000, contract, event_type=etype, event_id=event_id, delivery_id=shared_delivery
        )
        duplicate_note = (
            "(The delivery id is the event identity here, so the redelivery keeps the same id - the provider-documented dedupe key.)"
            if redelivery_keeps_delivery_id
            else "(The two deliveries carry different delivery ids - a new attempt id each time.)"
        )
        out["duplicate"] = fixture(
            "duplicate", [d1, d2], ack_success, budget_once, None, extra_note=duplicate_note, minimum=floor_once
        )

    if "retry_after_failure" in scenarios:
        body = new_body(base_payload, 1)
        event_id = identity_id(1)
        shared_delivery = event_id if redelivery_keeps_delivery_id else None
        d1 = make_delivery(
            ids, body, 0, contract, fail_attempt=True, event_type=etype, event_id=event_id, delivery_id=shared_delivery
        )
        d2 = make_delivery(
            ids, copy.deepcopy(body), 5000, contract, event_type=etype, event_id=event_id, delivery_id=shared_delivery
        )
        out["retry_after_failure"] = fixture(
            "retry_after_failure",
            [d1, d2],
            ack_success,
            budget_once,
            None,
            extra_note="(The harness discards the first attempt's response - the lost response that triggers the retry.)",
            minimum=floor_once,
        )

    sequence_field = contract.get("sequence_field")

    def seq_value_of(body):
        if not sequence_field:
            return None
        found, value = get_path({"body": body}, sequence_field)
        return value if found and isinstance(value, int) else None

    if "out_of_order" in scenarios:
        if len(payloads) < 2:
            notes.append(
                "{}: out_of_order uses one payload clone; add ordered_payloads to vary event content across the sequence".format(etype)
            )
        p1 = payloads[0]
        p2 = payloads[1] if len(payloads) > 1 else payloads[0]
        body_new = new_body(p2, 1)  # newest event, arrives first
        body_old = new_body(p1, 2)  # older event, arrives second
        new_seq, old_seq = seq_value_of(body_new), seq_value_of(body_old)
        if new_seq is not None and old_seq is not None and new_seq <= old_seq:
            set_body_path(body_new, sequence_field, old_seq + 1)
            notes.append(
                "{}: out_of_order payloads shared sequence value {}; bumped the newer event to {}".format(
                    etype, new_seq, old_seq + 1
                )
            )
        d1 = make_delivery(ids, body_new, 0, contract, event_type=etype, event_id=identity_id(1))
        d2 = make_delivery(ids, body_old, 1000, contract, event_type=etype, event_id=identity_id(2))
        out["out_of_order"] = fixture(
            "out_of_order",
            [d1, d2],
            ack_success,
            {name: 2 for name in effects},
            {"order": "natural"},
            minimum=floor_once,
        )

    if "cross_key_interleave" in scenarios:
        if ordering_key is None:
            notes.append("{}: cross_key_interleave skipped (no ordering_key)".format(etype))
        else:
            base_key = key_value_of(payloads[0])
            if not base_key:
                notes.append("{}: cross_key_interleave skipped (ordering key empty in payload)".format(etype))
            else:
                alt_key = "{}-b".format(base_key)

                def with_key(payload, key: str) -> dict:
                    body = new_body(payload, 0)
                    set_body_path(body, ordering_key, key)
                    return body

                newest = payloads[1] if len(payloads) > 1 else payloads[0]
                oldest = payloads[0]
                k1_old = with_key(oldest, base_key)
                k2_old = with_key(oldest, alt_key)
                k2_new = with_key(newest, alt_key)
                k1_new = with_key(newest, base_key)
                # Give each delivery a distinct synthetic event id (set_body_path
                # no-ops for a header-rooted id; make_delivery carries it then).
                k1_old_id, k2_old_id = identity_id(1), identity_id(2)
                k2_new_id, k1_new_id = identity_id(3), identity_id(4)
                for body, eid in ((k1_old, k1_old_id), (k2_old, k2_old_id), (k2_new, k2_new_id), (k1_new, k1_new_id)):
                    set_body_path(body, id_path, eid)
                # Make "new" strictly newer than "old" when the contract payloads
                # do not already provide distinct sequence values.
                for body_new, body_old in ((k1_new, k1_old), (k2_new, k2_old)):
                    new_seq, old_seq = seq_value_of(body_new), seq_value_of(body_old)
                    if new_seq is not None and old_seq is not None and new_seq <= old_seq:
                        set_body_path(body_new, sequence_field, old_seq + 1)
                # Key A arrives newest-first; key B arrives in natural order.
                d1 = make_delivery(ids, k1_new, 0, contract, event_type=etype, event_id=k1_new_id)
                d2 = make_delivery(ids, k2_old, 10, contract, event_type=etype, event_id=k2_old_id)
                d3 = make_delivery(ids, k2_new, 20, contract, event_type=etype, event_id=k2_new_id)
                d4 = make_delivery(ids, k1_old, 30, contract, event_type=etype, event_id=k1_old_id)
                out["cross_key_interleave"] = fixture(
                    "cross_key_interleave",
                    [d1, d2, d3, d4],
                    ack_success,
                    {name: 4 for name in effects},
                    {"order": "natural"},
                    minimum=floor_two,
                )

    if "unknown_type" in scenarios:
        body = new_body(base_payload, 1)
        d1 = make_delivery(ids, body, 0, contract, event_type=etype, event_id=identity_id(1))
        type_path = (contract.get("fields") or {}).get("type", "body.type")
        # fields.type may be body- or header-rooted; set_path handles both.
        if not set_path(d1, type_path, "webcontract.unknown.future_event"):
            notes.append("{}: unknown_type skipped (fields.type path '{}' does not resolve)".format(etype, type_path))
        else:
            out["unknown_type"] = fixture("unknown_type", [d1], ack_ignored, budget_zero, None, minimum=budget_zero)

    if "malformed" in scenarios:
        # Headers stay complete even though the body is broken: a real
        # delivery carries its envelope headers regardless of the payload.
        d1 = make_delivery(ids, None, 0, contract, event_type=etype, event_id=identity_id(1))
        d1["raw_body"] = '{"id": "evt_test_truncat'
        out["malformed"] = fixture("malformed", [d1], ack_malformed, budget_zero, None, minimum=budget_zero)

    return out


def write_fixtures(fixtures_by_event: dict, out_dir: Path) -> int:
    count = 0
    for event_type, scenario_map in fixtures_by_event.items():
        event_dir = out_dir / safe_dirname(event_type)
        event_dir.mkdir(parents=True, exist_ok=True)
        for scenario, document in scenario_map.items():
            target = event_dir / "{}.json".format(scenario)
            with target.open("w", encoding="utf-8", newline="\n") as fh:
                json.dump(document, fh, indent=2, sort_keys=False)
                fh.write("\n")
            count += 1
    return count


# ---------------------------------------------------------------------------
# Fixture validation
# ---------------------------------------------------------------------------


def validate_fixture(doc, rel: str, contract, event: dict, problems: list) -> None:
    if not isinstance(doc, dict):
        problems.append(error(rel, "fixture must be a JSON object"))
        return
    if doc.get("fixture_version") != 1:
        problems.append(error(rel, "fixture_version must be 1", "fixture_version"))
    scenario = doc.get("scenario")
    if scenario not in SCENARIOS:
        problems.append(error(rel, "unknown scenario '{}'".format(scenario), "scenario"))
        return
    etype = doc.get("event_type")
    deliveries = doc.get("deliveries")
    if not isinstance(deliveries, list) or not deliveries:
        problems.append(error(rel, "deliveries must be a non-empty list", "deliveries"))
        return
    expectations = doc.get("expectations") or {}
    statuses = expectations.get("statuses")
    budget = expectations.get("effect_budget") or {}
    reference = expectations.get("reference")
    fields = (contract or {}).get("fields") or {}
    delivery_path = path_or(fields.get("delivery_id"), "header.x-delivery-id")
    type_path = path_or(fields.get("type"), "body.type")
    id_path = path_or(fields.get("id"), "body.id")
    # When the provider's delivery id IS the event identity (GitHub), a
    # redelivery keeps the same value instead of arriving with a fresh one.
    delivery_is_identity = id_path.lower() == str(delivery_path).lower()
    ack = (contract or {}).get("ack") or {}
    known_statuses = set()
    for group in ack.values():
        if isinstance(group, list):
            known_statuses.update(v for v in group if isinstance(v, int))
    # name -> whether the effect is optional (may legitimately not fire).
    declared_effects = {
        eff.get("name"): eff.get("optional") is True
        for eff in as_list((event or {}).get("side_effects"))
        if isinstance(eff, dict)
    }

    if isinstance(statuses, list):
        for status in statuses:
            if contract is not None and known_statuses and status not in known_statuses:
                problems.append(
                    error(rel, "status {} is not in the contract ack table".format(status), "expectations.statuses")
                )
    else:
        problems.append(error(rel, "expectations.statuses must be a list", "expectations.statuses"))

    for name in budget:
        if declared_effects and name not in declared_effects:
            problems.append(
                error(rel, "effect '{}' is not declared in the contract for '{}'".format(name, etype), "expectations.effect_budget")
            )

    # Lower bounds are what stop a do-nothing handler from passing: any effect
    # with a positive budget must also carry a positive minimum - unless the
    # contract marks the effect optional (it fires only under a condition).
    minimum = expectations.get("effect_min")
    if declared_effects and budget:
        if not isinstance(minimum, dict):
            problems.append(
                error(
                    rel,
                    "expectations.effect_min must map effect names to minimum application counts; regenerate fixtures with `gen`",
                    "expectations.effect_min",
                )
            )
        else:
            for name, floor in minimum.items():
                if name not in declared_effects:
                    problems.append(
                        error(rel, "effect '{}' is not declared in the contract for '{}'".format(name, etype), "expectations.effect_min")
                    )
                    continue
                if not isinstance(floor, int) or isinstance(floor, bool) or floor < 0:
                    problems.append(
                        error(rel, "effect_min for '{}' must be a non-negative integer".format(name), "expectations.effect_min")
                    )
                    continue
                cap = budget.get(name)
                if cap is not None and floor > cap:
                    problems.append(
                        error(rel, "effect_min {} for '{}' exceeds its budget {}".format(floor, name, cap), "expectations.effect_min")
                    )
                if cap and floor < 1 and not declared_effects[name]:
                    problems.append(
                        error(
                            rel,
                            "effect '{}' has budget {} but effect_min {}: a handler that never applies it would pass"
                            " (declare it \"optional\": true if it only fires under a condition)".format(name, cap, floor),
                            "expectations.effect_min",
                        )
                    )

    bodies = [d.get("body") for d in deliveries if d.get("raw_body") is None]
    ids = []
    for d in deliveries:
        found, value = get_path(d, delivery_path)
        if found:
            ids.append(str(value))

    # Every contract-declared field must resolve in each delivery, so a
    # handler that routes or dedupes on a header actually receives it.
    # body-rooted paths cannot resolve where the body is null by design
    # (the malformed scenario).
    if contract is not None:
        for index, d in enumerate(deliveries):
            for role, path in (("id", id_path), ("type", type_path), ("delivery id", delivery_path)):
                if path.startswith("body.") and d.get("body") is None:
                    continue
                found, _ = get_path(d, path)
                if not found:
                    problems.append(
                        error(rel, "contract field {} '{}' does not resolve in delivery {}".format(role, path, index), "deliveries[{}]".format(index))
                    )

    if scenario == "duplicate":
        if len(deliveries) != 2:
            problems.append(error(rel, "duplicate fixture must have exactly 2 deliveries", "deliveries"))
        elif len(bodies) == 2 and canonical(bodies[0]) != canonical(bodies[1]):
            problems.append(error(rel, "duplicate deliveries must carry identical bodies", "deliveries"))
        if delivery_is_identity:
            if ids and len(set(ids)) != 1:
                problems.append(
                    error(
                        rel,
                        "duplicate deliveries must keep the same {} value: it is the event identity, and a redelivery does not mint a new one".format(delivery_path),
                        "deliveries",
                    )
                )
        elif len(set(ids)) != len(ids):
            problems.append(error(rel, "duplicate deliveries must have distinct delivery ids ({})".format(delivery_path), "deliveries"))
        for name, cap in budget.items():
            if cap != 1:
                problems.append(error(rel, "duplicate scenario expects effect '{}' at most once (budget {})".format(name, cap), "expectations.effect_budget"))
    elif scenario == "retry_after_failure":
        if len(deliveries) != 2:
            problems.append(error(rel, "retry fixture must have exactly 2 deliveries", "deliveries"))
        else:
            if not deliveries[0].get("fail_attempt"):
                problems.append(error(rel, "first retry delivery must have fail_attempt: true", "deliveries[0]"))
            if deliveries[1].get("fail_attempt"):
                problems.append(error(rel, "second retry delivery must have fail_attempt: false", "deliveries[1]"))
            if len(bodies) == 2 and canonical(bodies[0]) != canonical(bodies[1]):
                problems.append(error(rel, "retry deliveries must carry identical bodies", "deliveries"))
        if delivery_is_identity:
            if ids and len(set(ids)) != 1:
                problems.append(
                    error(
                        rel,
                        "retry deliveries must keep the same {} value: the provider re-delivers the same delivery".format(delivery_path),
                        "deliveries",
                    )
                )
        elif len(set(ids)) != len(ids):
            problems.append(error(rel, "retry deliveries must have distinct delivery ids ({})".format(delivery_path), "deliveries"))
        for name, cap in budget.items():
            if cap != 1:
                problems.append(error(rel, "retry scenario expects effect '{}' at most once (budget {})".format(name, cap), "expectations.effect_budget"))
    elif scenario == "out_of_order":
        if len(deliveries) < 2:
            problems.append(error(rel, "out_of_order fixture needs at least 2 deliveries", "deliveries"))
        else:
            arrival = [(d.get("order_key", ""), d.get("order_seq", 0)) for d in deliveries]
            if arrival == sorted(arrival):
                problems.append(error(rel, "out_of_order deliveries are already in natural order", "deliveries"))
            if len(set(arrival)) != len(arrival):
                problems.append(error(rel, "out_of_order deliveries must have distinct (order_key, order_seq)", "deliveries"))
        if not (isinstance(reference, dict) and reference.get("order") == "natural"):
            problems.append(error(rel, "out_of_order must declare reference order 'natural'", "expectations.reference"))
    elif scenario == "cross_key_interleave":
        keys = {d.get("order_key", "") for d in deliveries}
        if len(keys) < 2:
            problems.append(error(rel, "cross_key_interleave needs at least 2 distinct order_key values", "deliveries"))
        else:
            any_inverted = False
            for key in sorted(keys):
                seqs = [d.get("order_seq", 0) for d in deliveries if d.get("order_key", "") == key]
                if len(seqs) < 2:
                    problems.append(
                        error(rel, "key '{}' has fewer than 2 deliveries; add ordered_payloads".format(key), "deliveries")
                    )
                    continue
                if len(set(seqs)) != len(seqs):
                    problems.append(error(rel, "key '{}' has duplicate order_seq values".format(key), "deliveries"))
                if seqs != sorted(seqs):
                    any_inverted = True
            if not any_inverted:
                problems.append(
                    error(rel, "every key arrives in natural order; at least one key must be inverted", "deliveries")
                )
        if not (isinstance(reference, dict) and reference.get("order") == "natural"):
            problems.append(error(rel, "cross_key_interleave must declare reference order 'natural'", "expectations.reference"))
    elif scenario == "unknown_type":
        if len(deliveries) != 1:
            problems.append(error(rel, "unknown_type fixture must have exactly 1 delivery", "deliveries"))
        if contract is not None and event is not None:
            contract_types = set()
            for ev in as_list(contract.get("events")):
                if isinstance(ev, dict) and isinstance(ev.get("type"), str):
                    contract_types.add(ev["type"])
            # The unknown fixture must NOT carry a known event type. The fixture
            # stores the original event_type it was derived from; check the body.
            d = deliveries[0]
            found, value = get_path(d, type_path)
            if found and value in contract_types:
                problems.append(error(rel, "unknown_type body must carry an unregistered event type", "deliveries[0]"))
        for name, cap in budget.items():
            if cap != 0:
                problems.append(error(rel, "unknown_type must have a zero budget for '{}'".format(name), "expectations.effect_budget"))
    elif scenario == "malformed":
        if len(deliveries) != 1:
            problems.append(error(rel, "malformed fixture must have exactly 1 delivery", "deliveries"))
        else:
            raw = deliveries[0].get("raw_body")
            if not isinstance(raw, str):
                problems.append(error(rel, "malformed delivery must set raw_body to a string", "deliveries[0].raw_body"))
            else:
                try:
                    json.loads(raw)
                    problems.append(error(rel, "malformed raw_body is valid JSON; it must fail to parse", "deliveries[0].raw_body"))
                except json.JSONDecodeError:
                    pass
            if deliveries[0].get("body") is not None:
                problems.append(error(rel, "malformed delivery must have body: null", "deliveries[0].body"))
        for name, cap in budget.items():
            if cap != 0:
                problems.append(error(rel, "malformed must have a zero budget for '{}'".format(name), "expectations.effect_budget"))

    problems.extend(scan_data_hygiene(doc, rel))


def collect_problems_for_dir(work_dir: Path, contract, strict: bool) -> list:
    problems = []
    fixtures_dir = work_dir / FIXTURES_DIRNAME
    contract_path = work_dir / CONTRACT_NAME

    # Contract checks (contract is optional when --fixtures points elsewhere).
    if contract is not None:
        problems.extend(validate_contract(contract))
    elif contract_path.exists():
        try:
            contract = load_json(contract_path)
            problems.extend(validate_contract(contract))
        except ValueError as exc:
            problems.append(error(CONTRACT_NAME, str(exc)))
            contract = None

    events_by_type = {}
    if contract is not None and not isinstance(contract, dict):
        # A non-object contract was already reported by validate_contract;
        # keep checking the rest of the directory without it.
        contract = None
    if contract is not None:
        for ev in as_list(contract.get("events")):
            if isinstance(ev, dict) and isinstance(ev.get("type"), str):
                events_by_type[ev["type"]] = ev

    # Fixture files.
    fixture_files = sorted(fixtures_dir.rglob("*.json")) if fixtures_dir.exists() else []
    if contract is not None and not fixture_files:
        problems.append(warning(str(FIXTURES_DIRNAME) + "/", "no fixtures found; run the gen command first"))

    seen = set()
    for path in fixture_files:
        rel = str(path.relative_to(work_dir)).replace("\\", "/")
        try:
            doc = load_json(path)
        except ValueError as exc:
            problems.append(error(rel, str(exc)))
            continue
        event_type = doc.get("event_type") if isinstance(doc, dict) else None
        event = events_by_type.get(event_type)
        if contract is not None and event is None and isinstance(doc, dict) and doc.get("scenario") in SCENARIOS:
            problems.append(error(rel, "fixture event type '{}' is not in the contract".format(event_type), "event_type"))
        validate_fixture(doc, rel, contract, event, problems)
        if isinstance(doc, dict):
            seen.add((event_type, doc.get("scenario")))

    # Stale fixture directories: gen never deletes, so a directory for an
    # event that left the contract lingers. Flag it; removing it stays the
    # user's call.
    if contract is not None and fixtures_dir.exists():
        known = {
            safe_dirname(ev["type"]).lower()
            for ev in as_list(contract.get("events"))
            if isinstance(ev, dict) and isinstance(ev.get("type"), str)
        }
        for sub in sorted(fixtures_dir.iterdir()):
            if sub.is_dir() and sub.name.lower() not in known:
                problems.append(
                    warning(
                        str(sub.relative_to(work_dir)).replace("\\", "/"),
                        "directory matches no contract event - likely stale; remove it if unused",
                    )
                )

    # Completeness: every contract event x enabled scenario must have a fixture.
    # Before the first gen there is nothing to complete yet; the missing-fixtures
    # warning above covers that case.
    if contract is not None and fixture_files:
        for ev in as_list(contract.get("events")):
            if not isinstance(ev, dict) or not isinstance(ev.get("type"), str):
                continue
            etype = ev["type"]
            for scenario in as_list(contract.get("scenarios")):
                if scenario in (ev.get("skip_scenarios") or []):
                    continue
                if scenario not in SCENARIOS:
                    continue
                if (etype, scenario) not in seen:
                    problems.append(
                        error(str(FIXTURES_DIRNAME) + "/", "missing fixture for event '{}' scenario '{}' (run gen)".format(etype, scenario))
                    )

    # Harness and hooks presence, per language.
    contract_path_exists = contract_path.exists()
    # Path(".").name is "" - resolve so `check --dir .` still names the folder.
    dir_label = work_dir.resolve().name or str(work_dir)
    if contract_path_exists:
        for harness, hooks in ((HARNESS_PY, HOOKS_PY), (HARNESS_MJS, HOOKS_MJS)):
            has_harness = (work_dir / harness).exists()
            has_hooks = (work_dir / hooks).exists()
            if has_harness and not has_hooks:
                problems.append(warning(dir_label, "{} is missing (re-run init)".format(hooks)))
            if has_hooks and not has_harness:
                problems.append(warning(dir_label, "{} is missing (re-run init)".format(harness)))
            if has_hooks:
                try:
                    text = (work_dir / hooks).read_text(encoding="utf-8")
                    if TODO_MARKER in text:
                        problems.append(warning(hooks, "hooks are not wired yet ({})".format(TODO_MARKER)))
                except OSError:
                    pass
        if not (work_dir / HARNESS_PY).exists() and not (work_dir / HARNESS_MJS).exists():
            problems.append(warning(dir_label, "no replay harness found (re-run init)"))

    if strict:
        for idx, problem in enumerate(problems):
            if problem.severity == "warning":
                problems[idx] = Problem("error", problem.file, problem.path, problem.message)
    return problems


# ---------------------------------------------------------------------------
# init scaffolding
# ---------------------------------------------------------------------------


def cmd_init(args) -> int:
    work_dir = Path(args.dir)
    work_dir.mkdir(parents=True, exist_ok=True)
    # The generated README refers to `<webcontract.py>`; print the real path
    # so the commands work in a plain terminal, not only inside the skill.
    print("cli script: {}".format(Path(__file__).resolve()))

    langs = []
    if args.lang in ("python", "both"):
        langs.append("python")
    if args.lang in ("node", "both"):
        langs.append("node")

    wanted = [CONTRACT_NAME, README_NAME]
    if "python" in langs:
        wanted += [HOOKS_PY, HARNESS_PY]
    if "node" in langs:
        wanted += [HOOKS_MJS, HARNESS_MJS]

    # Create only what is missing; never overwrite. A user's own README.md
    # must not block scaffolding the rest.
    written = []
    skipped = []
    for name in wanted:
        path = work_dir / name
        if path.exists():
            skipped.append(name)
            continue
        if name == CONTRACT_NAME:
            with path.open("w", encoding="utf-8", newline="\n") as fh:
                json.dump(starter_contract(), fh, indent=2)
                fh.write("\n")
        elif name == README_NAME:
            copy_template("README.target.md", path)
        elif name == HOOKS_PY:
            copy_template("replay_hooks_stub.py", path)
        elif name == HOOKS_MJS:
            copy_template("replay_hooks_stub.mjs", path)
        else:
            copy_template(name, path)
        written.append(name)

    print("scaffolded {} in {}".format(", ".join(written) or "nothing (all files present)", work_dir))
    if skipped:
        print("skipped (already present, never overwritten): {}".format(", ".join(skipped)))
    print("next: edit {} then run gen".format(CONTRACT_NAME))
    return 0


def write_text_new(path: Path, text: str) -> None:
    """Write text with LF endings on every platform.

    Path.write_text grew its `newline` argument only in Python 3.10; the CLI
    promises 3.9+, so go through open() directly.
    """
    with path.open("w", encoding="utf-8", newline="\n") as fh:
        fh.write(text)


def copy_template(name: str, target: Path) -> None:
    source = TEMPLATES_DIR / name
    if not source.exists():
        raise SystemExit("internal error: template missing: {}".format(source))
    write_text_new(target, source.read_text(encoding="utf-8"))


def starter_contract() -> dict:
    return {
        "contract_version": 1,
        "provider": "TODO: e.g. stripe, github, shopify, or custom",
        "notes": "Edit this file, then run gen and check. See the plugin skill for the field reference.",
        "fields": {
            "id": "body.id",
            "type": "body.type",
            "delivery_id": "header.x-delivery-id",
        },
        "ack": {
            "success": [200],
            "ignored": [200],
            "malformed": [400],
        },
        "sequence_field": "body.data.object.attempt",
        "scenarios": SCENARIOS,
        "events": [
            {
                "type": "invoice.paid",
                "description": "TODO: replace with a real event type you receive",
                "ordering_key": "body.data.object.id",
                "ordered_payloads": None,
                "payload": {
                    "id": "evt_test_0001",
                    "type": "invoice.paid",
                    "data": {
                        "object": {
                            "id": "in_test_0001",
                            "attempt": 1,
                            "amount_paid": 4200,
                            "currency": "usd",
                        }
                    },
                },
                "side_effects": [
                    {
                        "name": "mark_invoice_paid",
                        "idempotent": True,
                        "guard": "TODO: what makes repeating this write safe (status guard, upsert)?",
                    },
                    {
                        "name": "send_receipt_email",
                        "idempotent": False,
                        "guard": "TODO: which earlier effect or dedupe key prevents a second email?",
                    },
                ],
                "skip_scenarios": [],
            }
        ],
    }


# ---------------------------------------------------------------------------
# Command entry points
# ---------------------------------------------------------------------------


def load_contract_or_problems(work_dir: Path):
    path = work_dir / CONTRACT_NAME
    if not path.exists():
        return None, [error(CONTRACT_NAME, "not found in {} (run init first)".format(work_dir))]
    try:
        contract = load_json(path)
    except ValueError as exc:
        return None, [error(CONTRACT_NAME, str(exc))]
    return contract, []


def cmd_gen(args) -> int:
    work_dir = Path(args.dir)
    contract, problems = load_contract_or_problems(work_dir)
    if contract is None:
        for problem in problems:
            print(problem.to_line())
        return 1
    problems = validate_contract(contract)
    errors = [p for p in problems if p.severity == "error"]
    if errors:
        for problem in problems:
            print(problem.to_line())
        print("gen: fix the contract errors above before generating fixtures")
        return 1

    ids = IdAllocator()
    notes = []
    fixtures_by_event = {}
    for event_idx, event in enumerate(as_list(contract.get("events"))):
        if not isinstance(event, dict) or not isinstance(event.get("type"), str):
            continue
        fixtures_by_event[event["type"]] = build_fixtures_for_event(event, event_idx, contract, ids, notes)

    out_dir = work_dir / FIXTURES_DIRNAME
    out_dir.mkdir(parents=True, exist_ok=True)
    count = write_fixtures(fixtures_by_event, out_dir)

    # Warnings do not block generation, but "generated 0 fixture(s)" alone
    # would leave the reason (e.g. an empty scenarios list) unstated.
    for problem in problems:
        if problem.severity == "warning":
            print(problem.to_line())
    for note in notes:
        print("note: {}".format(note))
    print("generated {} fixture(s) in {}".format(count, out_dir))
    if args.json:
        print(
            json.dumps(
                {
                    "generated": count,
                    "events": [event["type"] for event in fixtures_by_event],
                    "notes": notes,
                },
                separators=(",", ":"),
            )
        )
    return 0


def cmd_check(args) -> int:
    work_dir = Path(args.dir)
    if not work_dir.exists():
        print("error: directory does not exist: {}".format(work_dir))
        return 2
    contract = None
    contract_path = work_dir / CONTRACT_NAME
    if not contract_path.exists() and not (work_dir / FIXTURES_DIRNAME).exists():
        print("error: no {} and no {}/ in {}".format(CONTRACT_NAME, FIXTURES_DIRNAME, work_dir))
        print("run init first, or pass the folder that contains them via --dir")
        return 2
    if contract_path.exists():
        try:
            contract = load_json(contract_path)
        except ValueError:
            contract = None  # already reported inside collect_problems_for_dir
    problems = collect_problems_for_dir(work_dir, contract, args.strict)

    fixture_count = len(list((work_dir / FIXTURES_DIRNAME).rglob("*.json"))) if (work_dir / FIXTURES_DIRNAME).exists() else 0
    errors = [p for p in problems if p.severity == "error"]
    warnings = [p for p in problems if p.severity == "warning"]
    for problem in problems:
        print(problem.to_line())
    print(
        "check: {} fixture(s), {} error(s), {} warning(s)".format(fixture_count, len(errors), len(warnings))
    )
    if args.json:
        print(
            json.dumps(
                {
                    "fixtures": fixture_count,
                    "errors": len(errors),
                    "warnings": len(warnings),
                    "problems": [p.to_dict() for p in problems],
                },
                separators=(",", ":"),
            )
        )
    return 1 if errors else 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="webcontract",
        description="Provider-neutral webhook delivery contracts and synthetic fixtures.",
    )
    parser.add_argument("--version", action="version", version="webcontract {}".format(VERSION))
    sub = parser.add_subparsers(dest="command", required=True)

    p_init = sub.add_parser("init", help="scaffold contract, harness and hooks (creates missing files only)")
    p_init.add_argument("--dir", default="tests/webhooks", help="target folder inside the user repo")
    p_init.add_argument("--lang", choices=("python", "node", "both"), default="both", help="which harness language(s) to scaffold")
    p_init.set_defaults(func=cmd_init)

    p_gen = sub.add_parser("gen", help="generate deterministic fixtures from the contract")
    p_gen.add_argument("--dir", default="tests/webhooks", help="folder containing " + CONTRACT_NAME)
    p_gen.add_argument("--json", action="store_true", help="also print a JSON summary")
    p_gen.set_defaults(func=cmd_gen)

    p_check = sub.add_parser("check", help="validate contract and fixtures")
    p_check.add_argument("--dir", default="tests/webhooks", help="folder containing the contract and fixtures/")
    p_check.add_argument("--strict", action="store_true", help="treat warnings as errors")
    p_check.add_argument("--json", action="store_true", help="also print a JSON report")
    p_check.set_defaults(func=cmd_check)

    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except OSError as exc:
        print("error: {}".format(exc))
        return 2


if __name__ == "__main__":
    sys.exit(main())
