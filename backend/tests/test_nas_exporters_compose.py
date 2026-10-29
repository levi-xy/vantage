"""The NAS exporter bundle (M4i): ``deploy/exporters/nas/docker-compose.yml``.

It runs on a user's NAS, outside the Vantage stack, so nothing else exercises it. These
tests hold its hardening in place: pinned images, read-only root filesystems, every
capability dropped (smartctl_exporter gets back ``SYS_RAWIO`` only), no privileged
mode, no new privileges, pids limits, and only the mounts the README justifies. When
the Docker CLI is available, ``docker compose config`` validates the file as well.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml

NAS_DIR = Path(__file__).resolve().parents[2] / "deploy" / "exporters" / "nas"
COMPOSE = NAS_DIR / "docker-compose.yml"

# Exact release tags: vX.Y.Z, never "latest" or a floating "v1".
PINNED = re.compile(r"^[a-z0-9./-]+:v\d+\.\d+\.\d+$")


@pytest.fixture(scope="module")
def compose() -> dict[str, Any]:
    with COMPOSE.open(encoding="utf-8") as f:
        data: dict[str, Any] = yaml.safe_load(f)
    return data


@pytest.fixture(scope="module")
def services(compose: dict[str, Any]) -> dict[str, dict[str, Any]]:
    svc: dict[str, dict[str, Any]] = compose["services"]
    return svc


def test_exactly_the_two_exporters(services: dict[str, dict[str, Any]]) -> None:
    assert set(services) == {"node-exporter", "smartctl-exporter"}
    assert services["node-exporter"]["image"].startswith("prom/node-exporter:")
    assert services["smartctl-exporter"]["image"].startswith(
        "prometheuscommunity/smartctl-exporter:"
    )


@pytest.mark.parametrize("name", ["node-exporter", "smartctl-exporter"])
def test_hardening(services: dict[str, dict[str, Any]], name: str) -> None:
    svc = services[name]
    assert PINNED.fullmatch(svc["image"]), svc["image"]
    assert "latest" not in svc["image"]
    assert "privileged" not in svc
    assert svc["read_only"] is True
    assert svc["cap_drop"] == ["ALL"]
    assert "no-new-privileges:true" in svc["security_opt"]
    assert isinstance(svc["pids_limit"], int) and 0 < svc["pids_limit"] <= 256
    assert svc["restart"] == "unless-stopped"
    # Nothing that widens the container beyond what the README justifies.
    for key in ("devices_cgroup_rules", "sysctls", "ipc", "userns_mode", "cgroup"):
        assert key not in svc, key
    for opt in svc["security_opt"]:
        assert "unconfined" not in opt, opt


def test_node_exporter_mounts_host_root_read_only(services: dict[str, dict[str, Any]]) -> None:
    svc = services["node-exporter"]
    assert svc["volumes"] == ["/:/host:ro,rslave"]
    assert "--path.rootfs=/host" in svc["command"]
    assert svc["pid"] == "host"
    assert svc["network_mode"] == "host"
    assert "cap_add" not in svc and "devices" not in svc
    assert svc["user"] == "65534:65534"  # nobody, not root


def test_node_exporter_keeps_nas_collectors(services: dict[str, dict[str, Any]]) -> None:
    # mdadm, zfs, hwmon and filesystem are on by default; nothing may switch them off.
    for flag in services["node-exporter"]["command"]:
        assert not flag.startswith("--collector.disable-defaults"), flag
        for collector in ("mdadm", "zfs", "hwmon", "filesystem", "diskstats"):
            assert flag != f"--no-collector.{collector}", flag


def test_smartctl_exporter_gets_only_rawio_and_listed_disks(
    services: dict[str, dict[str, Any]],
) -> None:
    svc = services["smartctl-exporter"]
    assert svc["cap_add"] == ["SYS_RAWIO"]  # SYS_ADMIN stays commented out (NVMe opt-in)
    assert "volumes" not in svc
    assert "network_mode" not in svc and "pid" not in svc
    assert svc["devices"], "the example disk list the user edits"
    for device in svc["devices"]:
        host, _, inside = device.partition(":")
        assert re.fullmatch(r"/dev/(sd[a-z]+|nvme\d+)", host), device
        assert inside == host, device
    assert "/dev:/dev" not in svc["devices"]


def test_ports_are_bound_to_the_configurable_address(services: dict[str, dict[str, Any]]) -> None:
    assert services["smartctl-exporter"]["ports"] == ["${NAS_LISTEN_ADDRESS:-0.0.0.0}:9633:9633"]
    assert (
        "--web.listen-address=${NAS_LISTEN_ADDRESS:-0.0.0.0}:9100"
        in (services["node-exporter"]["command"])
    )
    assert "ports" not in services["node-exporter"]  # host network: the flag binds it


def test_readme_names_the_pinned_versions(services: dict[str, dict[str, Any]]) -> None:
    readme = (NAS_DIR / "README.md").read_text(encoding="utf-8")
    for svc in services.values():
        assert f"`{svc['image']}`" in readme
    assert "No authentication" in readme
    assert "SYS_ADMIN" in readme and "DOCKER-USER" in readme


@pytest.mark.skipif(shutil.which("docker") is None, reason="docker CLI not installed")
def test_docker_compose_config_accepts_the_file() -> None:
    docker = shutil.which("docker")
    assert docker is not None
    probe = subprocess.run(  # noqa: S603 (fixed arguments)
        [docker, "compose", "version"], capture_output=True, text=True, timeout=30, check=False
    )
    if probe.returncode != 0:
        pytest.skip("docker compose plugin not available")
    result = subprocess.run(  # noqa: S603 (fixed arguments, a repo file)
        [docker, "compose", "-f", str(COMPOSE), "config", "--quiet"],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
        cwd=NAS_DIR,
    )
    assert result.returncode == 0, result.stderr
