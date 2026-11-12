"""deploy/compose.standalone.yml: the install that needs no host files (Portainer, Dockge).

Checked against the setup.sh bundle's docker-compose.yml so the two keep the same
hardening, settings and network, against the paths the init command writes to and the
containers read from, and against the copy shown in the README.
"""

from __future__ import annotations

import re
from pathlib import Path, PurePosixPath
from typing import Any

import pytest
import yaml

from app.cli import standalone_init

REPO = Path(__file__).resolve().parents[2]
STANDALONE = REPO / "deploy" / "compose.standalone.yml"
BUNDLE = REPO / "deploy" / "docker-compose.yml"
INIT_VOLUMES = ("master-key", "prometheus-token", "tls", "prometheus-config")


def _load(path: Path) -> dict[str, Any]:
    data: dict[str, Any] = yaml.safe_load(path.read_text(encoding="utf-8"))
    return data


@pytest.fixture(scope="module")
def standalone() -> dict[str, Any]:
    return _load(STANDALONE)


@pytest.fixture(scope="module")
def bundle() -> dict[str, Any]:
    return _load(BUNDLE)


def _mounts(service: dict[str, Any]) -> dict[str, tuple[str, bool]]:
    """volume name -> (container path, read-only)."""
    out: dict[str, tuple[str, bool]] = {}
    for spec in service.get("volumes", []):
        parts = spec.split(":")
        out[parts[0]] = (parts[1], parts[2:] == ["ro"])
    return out


def test_needs_no_host_files(standalone: dict[str, Any]) -> None:
    assert "secrets" not in standalone and "configs" not in standalone
    declared = set(standalone["volumes"])
    for name, service in standalone["services"].items():
        assert not {"build", "secrets", "configs", "env_file"} & set(service), name
        for volume in _mounts(service):
            assert volume in declared, f"{name}: {volume} is a host path, not a named volume"


def test_vantage_images_are_pinned_to_one_published_version(standalone: dict[str, Any]) -> None:
    images = [s["image"] for s in standalone["services"].values()]
    vantage = [i for i in images if i.startswith("ghcr.io/levi-xy/vantage-")]
    assert len(vantage) == 3  # init (the backend image), backend, frontend
    tags = {i.rsplit(":", 1)[1] for i in vantage}
    assert len(tags) == 1 and re.fullmatch(r"\d+\.\d+\.\d+(-[0-9A-Za-z.]+)?", tags.pop())
    assert standalone["services"]["init"]["image"] == standalone["services"]["backend"]["image"]


def test_every_service_keeps_the_hardening(
    standalone: dict[str, Any], bundle: dict[str, Any]
) -> None:
    for name, service in standalone["services"].items():
        assert service["read_only"] is True, name
        assert service["security_opt"] == ["no-new-privileges:true"], name
        assert service["cap_drop"] == ["ALL"], name
        if name != "init":
            assert service["restart"] == "unless-stopped", name
            assert "cap_add" not in service, name
            assert "user" not in service, name
            assert "privileged" not in service, name
    for name in ("backend", "frontend"):
        assert standalone["services"][name]["tmpfs"] == bundle["services"][name]["tmpfs"]
    assert standalone["services"]["backend"]["sysctls"] == bundle["services"]["backend"]["sysctls"]
    assert (
        standalone["services"]["backend"]["stop_grace_period"]
        == bundle["services"]["backend"]["stop_grace_period"]
    )


def test_init_is_a_one_shot_without_network_and_only_chown_rights(
    standalone: dict[str, Any],
) -> None:
    init = standalone["services"]["init"]
    assert init["command"] == ["python", "-m", "app.cli", "standalone-init"]
    assert init["network_mode"] == "none"
    assert init["restart"] == "no"
    assert init["user"] == "0:0"
    assert sorted(init["cap_add"]) == ["CHOWN", "FOWNER"]
    assert "ports" not in init and "networks" not in init


def test_init_writes_where_the_command_expects(standalone: dict[str, Any]) -> None:
    mounts = _mounts(standalone["services"]["init"])
    layout = standalone_init.Layout()
    expected = {
        "master-key": layout.master_key.path.parent,
        "prometheus-token": layout.token.path.parent,
        "tls": layout.tls_key.path.parent,
        "prometheus-config": layout.prometheus_yml.path.parent,
    }
    for volume, folder in expected.items():
        assert mounts[volume] == (str(folder), False), volume
    assert layout.tls_cert.path.parent == layout.tls_key.path.parent
    # The data volume: read-only, at the backend's own path (see Layout.data).
    assert mounts["backend-data"] == (str(layout.data), True)
    backend_mounts = _mounts(standalone["services"]["backend"])
    assert mounts["backend-data"][0] == backend_mounts["backend-data"][0]


def test_services_mount_the_init_volumes_read_only_and_wait_for_init(
    standalone: dict[str, Any],
) -> None:
    for name, service in standalone["services"].items():
        if name == "init":
            continue
        assert service["depends_on"]["init"] == {"condition": "service_completed_successfully"}
        for volume, (_, read_only) in _mounts(service).items():
            if volume in INIT_VOLUMES:
                assert read_only, f"{name} mounts {volume} writable"


def test_each_service_mounts_only_the_files_it_reads(standalone: dict[str, Any]) -> None:
    services = standalone["services"]
    used = {name: set(_mounts(s)) & set(INIT_VOLUMES) for name, s in services.items()}
    assert used["backend"] == {"master-key", "prometheus-token"}
    assert used["frontend"] == {"tls"}
    assert used["prometheus"] == {"prometheus-config", "prometheus-token"}


def _file_in(service: dict[str, Any], volume: str, name: str) -> str:
    return str(PurePosixPath(_mounts(service)[volume][0]) / name)


def test_containers_read_the_files_where_init_puts_them(standalone: dict[str, Any]) -> None:
    services = standalone["services"]
    layout = standalone_init.Layout()
    backend_env = services["backend"]["environment"]
    assert backend_env["VANTAGE_MASTER_KEY_FILE"] == _file_in(
        services["backend"], "master-key", layout.master_key.path.name
    )
    assert backend_env["VANTAGE_PROMETHEUS_TOKEN_FILE"] == _file_in(
        services["backend"], "prometheus-token", layout.token.path.name
    )
    # The frontend image's nginx reads fixed paths.
    tls_conf = (REPO / "frontend" / "docker" / "tls.conf").read_text()
    for f in (layout.tls_cert, layout.tls_key):
        assert f"{_file_in(services['frontend'], 'tls', f.path.name)};" in tls_conf
    # Prometheus: its config from the config volume, the token where that config says.
    config = _file_in(services["prometheus"], "prometheus-config", "prometheus.yml")
    assert f"--config.file={config}" in services["prometheus"]["command"]
    token_path = _file_in(services["prometheus"], "prometheus-token", layout.token.path.name)
    assert f"credentials_file: {token_path}" in standalone_init.PROMETHEUS_YML


def test_settings_match_the_bundle_compose_file(
    standalone: dict[str, Any], bundle: dict[str, Any]
) -> None:
    ours, theirs = standalone["services"], bundle["services"]
    backend_env = dict(ours["backend"]["environment"])
    bundle_env = dict(theirs["backend"]["environment"])
    # Only the key's path differs (a volume folder instead of a compose secret).
    assert backend_env.pop("VANTAGE_MASTER_KEY_FILE") == "/run/vantage/key/master_key"
    assert bundle_env.pop("VANTAGE_MASTER_KEY_FILE") == "/run/secrets/master_key"
    assert backend_env == bundle_env
    assert ours["frontend"]["environment"] == theirs["frontend"]["environment"]
    assert ours["frontend"]["networks"] == theirs["frontend"]["networks"]
    assert standalone["networks"] == bundle["networks"]
    assert ours["prometheus"]["image"] == theirs["prometheus"]["image"]
    assert ours["prometheus"]["command"][1:] == theirs["prometheus"]["command"][1:]
    # Same port and address variables; the old VANTAGE_HTTP_PORT name is bundle-only.
    assert ours["frontend"]["ports"] == [
        "${VANTAGE_BIND_ADDRESS:-0.0.0.0}:${VANTAGE_HTTPS_PORT:-8443}:8443"
    ]


def test_prometheus_config_copy_matches_the_bootstrap_file() -> None:
    """Contract 4: the config init writes is deploy/prometheus/prometheus.yml, unchanged."""
    bootstrap = (REPO / "deploy" / "prometheus" / "prometheus.yml").read_text()
    assert standalone_init.PROMETHEUS_YML == bootstrap


def test_readme_shows_the_file_exactly() -> None:
    readme = (REPO / "README.md").read_text()
    blocks = re.findall(r"```yaml\n(.*?)```", readme, re.DOTALL)
    assert STANDALONE.read_text() in blocks, (
        "README.md must show deploy/compose.standalone.yml unchanged in a ```yaml block"
    )
    # Collapsed under the numbered install steps.
    collapsed = re.search(
        r"<details>\n<summary>Show compose file</summary>\n\n```yaml\n(.*?)```\n\n</details>",
        readme,
        re.DOTALL,
    )
    assert collapsed and collapsed.group(1) == STANDALONE.read_text()
