"""Alert identity and stored labels (P0-3a, ``app.alerts.labels``)."""

import ast
import hashlib
from pathlib import Path

import pytest

from app.alerts.labels import (
    DISPLAY_LABELS,
    MAX_STORED_LABELS,
    REDACTED,
    fingerprint,
    identity_labels,
    stored_labels,
)

DEVICE = "00000000-0000-0000-0000-000000000001"
OTHER = "00000000-0000-0000-0000-000000000002"
PORT = {
    "__name__": "vantage_snmp_device_if_oper_up",
    "vantage_instance_id": DEVICE,
    "if_index": "3",
}

# Fixed inputs, fixed fingerprints. Changing the canonical form must change FINGERPRINT_VERSION.
GOLDEN = [
    ({}, "ae8835d01b2e81960d46faa738a19a8a"),
    ({"vantage_instance_id": DEVICE, "if_index": "3"}, "b33556a8cd4679e6422eee8f7f9c3643"),
    (
        {"__name__": "up", "node": "pve-1", "vmid": "101", "vantage_instance_id": DEVICE},
        "bf156932a6c1b2a3d9059ec0c1bdff7e",
    ),
    ({"disk": "sda", "zone": "Zürich"}, "d0d16e90c68134c875dbd3a9d53181da"),
]


@pytest.mark.parametrize(("labels", "expected"), GOLDEN)
def test_golden_vectors(labels: dict[str, str], expected: str) -> None:
    assert fingerprint(labels) == expected


def test_canonical_form_is_documented() -> None:
    """SHA-256 of "v1\\n" + compact JSON of the pairs sorted by name, first 32 hex chars."""
    canonical = f'v1\n[["if_index","3"],["vantage_instance_id","{DEVICE}"]]'
    assert fingerprint(PORT) == hashlib.sha256(canonical.encode()).hexdigest()[:32]


def test_label_order_does_not_matter() -> None:
    labels = {**PORT, "a": "1", "b": "2", "z": "3"}
    items = list(labels.items())
    items = items[::-1][1:] + items[-1:]  # reversed, then rotated
    assert fingerprint(dict(items)) == fingerprint(labels)


@pytest.mark.parametrize(
    "excluded",
    [
        "__name__",
        "__meta_x",
        "job",
        "instance",
        "vantage_instance",
        "vantage_site",
        "vantage_integration",
        *DISPLAY_LABELS,
    ],
)
def test_excluded_labels_do_not_change_the_fingerprint(excluded: str) -> None:
    assert fingerprint({**PORT, excluded: "one"}) == fingerprint({**PORT, excluded: "two"})


def test_device_and_identity_labels_change_the_fingerprint() -> None:
    assert fingerprint({**PORT, "vantage_instance_id": OTHER}) != fingerprint(PORT)
    assert fingerprint({**PORT, "if_index": "4"}) != fingerprint(PORT)


def test_sensitive_label_is_not_hashed_and_stored_redacted() -> None:
    labels = {**PORT, "api_token": "example-token", "SNMP_Community": "example-community"}
    assert fingerprint(labels) == fingerprint(PORT)
    assert "api_token" not in identity_labels(labels)
    stored = stored_labels(labels)
    assert stored["api_token"] == REDACTED and stored["SNMP_Community"] == REDACTED
    assert "example-" not in str(stored)


def test_url_value_loses_credentials_and_query() -> None:
    labels = {**PORT, "target": "https://user:example-pass@192.0.2.10:8443/health?token=abc#x"}
    assert stored_labels(labels)["target"] == "https://192.0.2.10:8443/health"


def test_stored_values_are_cleaned_and_names_checked() -> None:
    stored = stored_labels({**PORT, "if_alias": "up\nlink\x00" + "x" * 300, "bad-name": "v"})
    assert "\n" not in stored["if_alias"] and len(stored["if_alias"]) == 128
    assert "bad-name" not in stored and "__name__" not in stored


def test_seventeen_labels_store_sixteen_and_hash_all() -> None:
    labels = {"vantage_instance_id": DEVICE, **{f"l{i:02d}": str(i) for i in range(16)}}
    stored = stored_labels(labels)
    assert len(stored) == MAX_STORED_LABELS and stored["vantage_instance_id"] == DEVICE
    dropped = (set(labels) - set(stored)).pop()
    assert fingerprint({**labels, dropped: "changed"}) != fingerprint(labels)


# --- every builtin integration's display labels are on DISPLAY_LABELS -------------------

BUILTIN = Path(__file__).resolve().parents[1] / "app" / "integrations" / "builtin"
_SAMPLE_CALLS = {"Sample", "_gauge", "_sample"}


def _dict_keys(node: ast.AST) -> set[str]:
    """String keys of a dict literal, including dicts spread into it."""
    keys: set[str] = set()
    for sub in ast.walk(node):
        if isinstance(sub, ast.Dict):
            keys |= {k.value for k in sub.keys if isinstance(k, ast.Constant)}
    return {k for k in keys if isinstance(k, str)}


def _is_info(name: ast.AST | None) -> bool:
    if isinstance(name, ast.Name):
        return "INFO" in name.id
    if isinstance(name, ast.JoinedStr):
        return any(
            isinstance(v, ast.Constant) and str(v.value).endswith("_info") for v in name.values
        )
    return isinstance(name, ast.Constant) and str(name.value).endswith("_info")


def _label_keys() -> tuple[set[str], set[str]]:
    """(keys on info series, keys on value series) over every builtin module."""
    info: set[str] = set()
    value: set[str] = set()
    for path in BUILTIN.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Assign) and any(
                isinstance(t, ast.Name) and t.id == "labels" for t in node.targets
            ):
                value |= _dict_keys(node.value)
            if not (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id in _SAMPLE_CALLS
            ):
                continue
            kw = {k.arg: k.value for k in node.keywords}
            metric = kw.get("name") or (
                node.args[1 if node.func.id == "_sample" else 0] if node.args else None
            )
            label_args = [kw["labels"]] if "labels" in kw else node.args[2:]
            keys = set().union(*(_dict_keys(a) for a in label_args)) if label_args else set()
            (info if _is_info(metric) else value).update(keys)
    return info, value


def test_display_labels_cover_every_builtin_integration() -> None:
    """Free text sits only on info series (INTEGRATIONS.md section 9): every label found
    there and not on a value series must be a display label, or renaming a port or a guest
    would split its alert."""
    info, value = _label_keys()
    display = info - value
    assert {"if_descr", "guest_name", "zone_name", "server_name"} <= display  # the scan works
    assert display <= set(DISPLAY_LABELS)
