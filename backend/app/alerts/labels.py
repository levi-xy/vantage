"""Alert identity: which labels of a series make it "the same alert" (P0-3a).

An alert is identified by (rule id, fingerprint). The fingerprint hashes the series'
*identity labels*: every label Prometheus returned, minus

- ``__name__`` and anything starting with ``__``;
- ``job`` and ``instance`` (scrape plumbing that can change without the device changing);
- ``vantage_instance``, ``vantage_site``, ``vantage_integration`` (renaming a device or
  moving it to another site must not split its alert; ``vantage_instance_id`` stays: it is
  the device identity);
- the display labels integrations attach to their info series (:data:`DISPLAY_LABELS`);
- sensitive-looking label names (:func:`is_sensitive`), which are never hashed and are
  stored only as ``<redacted>``.

Canonical form: the remaining (name, value) pairs sorted by the UTF-8 bytes of the name,
values exactly as returned, as compact JSON after a ``"v1\\n"`` version prefix; SHA-256,
first 32 hex characters.

What an event stores (:func:`stored_labels`) is for people, not for identity: the identity
labels plus ``vantage_instance`` and the display labels, sensitive values redacted, names
checked, values cleaned and capped, URLs stripped of credentials and query strings, at most
:data:`MAX_STORED_LABELS`. Summaries and notification texts are built from these only.
"""

import hashlib
import json
import re
import unicodedata
from collections.abc import Mapping

FINGERPRINT_VERSION = "v1"
FINGERPRINT_LENGTH = 32  # hex characters kept of the SHA-256
LEGACY_FINGERPRINT = "legacy"  # events opened before migration 0006

MAX_STORED_LABELS = 16
MAX_VALUE_LENGTH = 128
REDACTED = "<redacted>"

INSTANCE_ID_LABEL = "vantage_instance_id"
INSTANCE_NAME_LABEL = "vantage_instance"

EXCLUDED_LABELS = frozenset(
    {"job", "instance", INSTANCE_NAME_LABEL, "vantage_site", "vantage_integration"}
)

# Free-form text integrations put on their ``*_info`` series (INTEGRATIONS.md section 9):
# names, descriptions and versions that can change while the thing they describe doesn't.
# ``test_display_labels_cover_every_builtin_integration`` keeps this list complete.
DISPLAY_LABELS: tuple[str, ...] = (
    "if_descr",
    "if_alias",
    "if_label",
    "if_type",
    "connector",
    "zone_name",
    "tunnel_name",
    "guest_name",
    "guest_tags",
    "interface_name",
    "server_name",
    "version",
    "sys_name",
    "sys_descr",
    "sys_object_id",
)

_SENSITIVE_PARTS = (
    "password",
    "passwd",
    "secret",
    "token",
    "key",
    "auth",
    "credential",
    "cookie",
    "session",
    "bearer",
    "signature",
    "community",
)
_NAME_RE = re.compile(r"[a-zA-Z_][a-zA-Z0-9_]{0,63}")
_URL_RE = re.compile(r"[a-zA-Z][a-zA-Z0-9+.-]*://\S*")


def is_sensitive(name: str) -> bool:
    lowered = name.lower()
    return any(part in lowered for part in _SENSITIVE_PARTS)


def _excluded(name: str) -> bool:
    return name.startswith("__") or name in EXCLUDED_LABELS or name in DISPLAY_LABELS


def identity_labels(labels: Mapping[str, str]) -> dict[str, str]:
    """The labels that make up an alert's identity (unfiltered values)."""
    return {k: v for k, v in labels.items() if not _excluded(k) and not is_sensitive(k)}


def fingerprint(labels: Mapping[str, str]) -> str:
    pairs = sorted(identity_labels(labels).items(), key=lambda kv: kv[0].encode("utf-8"))
    canonical = json.dumps([list(p) for p in pairs], separators=(",", ":"), ensure_ascii=False)
    digest = hashlib.sha256(f"{FINGERPRINT_VERSION}\n{canonical}".encode()).hexdigest()
    return digest[:FINGERPRINT_LENGTH]


def clean(text: str, limit: int = MAX_VALUE_LENGTH) -> str:
    """Printable text on one line, capped."""
    # Control, format (bidi overrides), surrogate and line-separator characters become spaces.
    text = "".join(" " if unicodedata.category(c)[0] in "CZ" and c != " " else c for c in text)
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _strip_url(match: re.Match[str]) -> str:
    url = match.group(0)
    scheme, rest = url.split("://", 1)
    rest = re.split(r"[?#]", rest, maxsplit=1)[0]
    authority, slash, path = rest.partition("/")
    if "@" in authority:  # user:password@host; up to the last "@" of the authority only
        authority = authority[authority.rfind("@") + 1 :]
    return f"{scheme}://{authority}{slash}{path}"


# "user:password@host..." without a scheme.
_BARE_USERINFO_RE = re.compile(r"^[^\s/@]+@")


def clean_value(value: str) -> str:
    value = clean(value, 4096)
    if "://" in value:
        value = _URL_RE.sub(_strip_url, value)
    else:
        value = _BARE_USERINFO_RE.sub("", value)
    return clean(value)


def stored_labels(labels: Mapping[str, str]) -> dict[str, str]:
    """The labels an event keeps: safe to show, log and send."""
    identity = identity_labels(labels)
    display = {k: labels[k] for k in (INSTANCE_NAME_LABEL, *DISPLAY_LABELS) if k in labels}
    redacted = {k: REDACTED for k in labels if is_sensitive(k) and not k.startswith("__")}
    # Priority when capping: the device, then identity, display, and redacted names last.
    ordered: list[tuple[str, str]] = []
    if INSTANCE_ID_LABEL in identity:
        ordered.append((INSTANCE_ID_LABEL, identity.pop(INSTANCE_ID_LABEL)))
    if INSTANCE_NAME_LABEL in display:
        ordered.append((INSTANCE_NAME_LABEL, display.pop(INSTANCE_NAME_LABEL)))
    ordered += sorted(identity.items()) + sorted(display.items())
    out: dict[str, str] = {}
    for name, value in ordered:
        if len(out) == MAX_STORED_LABELS:
            break
        if _NAME_RE.fullmatch(name):
            out[name] = clean_value(value)
    for name in sorted(redacted):
        if len(out) == MAX_STORED_LABELS:
            break
        if _NAME_RE.fullmatch(name):
            out[name] = REDACTED
    return out
