"""deploy/make_bundle.py: the release's install bundle pulls the published images."""

import importlib.util
import re
import tarfile
from pathlib import Path
from types import ModuleType

import pytest

REPO = Path(__file__).resolve().parents[2]


def load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("make_bundle", REPO / "deploy" / "make_bundle.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


bundle = load()


def test_compose_build_blocks_become_pinned_images() -> None:
    source = (REPO / "deploy" / "docker-compose.yml").read_text()
    out = bundle.image_compose(source, "1.2.3")
    assert "ghcr.io/levi-xy/vantage-backend:${VANTAGE_VERSION:-1.2.3}" in out
    assert "ghcr.io/levi-xy/vantage-frontend:${VANTAGE_VERSION:-1.2.3}" in out
    assert not re.search(r"^\s+build:", out, re.MULTILINE)
    assert "additional_contexts" not in out and "../" not in out
    # Everything else is untouched: same lines once the build blocks are taken out.
    kept = [line for line in out.splitlines() if "vantage-" not in line or "image:" not in line]
    assert "    stop_grace_period: 20s" in kept
    assert out.count("\n") < source.count("\n")


def test_compose_without_the_expected_build_blocks_is_refused() -> None:
    with pytest.raises(ValueError, match="build block"):
        bundle.image_compose("services:\n  backend:\n    image: x\n", "1.2.3")


@pytest.mark.parametrize("version", ["1.2", "v1.2.3", "1.2.3; rm -rf /", "latest", ""])
def test_bad_versions_are_refused(version: str, tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="not a version"):
        bundle.build(version, tmp_path)


def test_bundle_holds_what_an_install_needs_and_no_secret(tmp_path: Path) -> None:
    path = bundle.build("0.1.0-rc.1", tmp_path)
    assert path.name == "vantage-deploy-0.1.0-rc.1.tar.gz"
    with tarfile.open(path) as tar:
        names = set(tar.getnames())
        compose = tar.extractfile("vantage-deploy-0.1.0-rc.1/docker-compose.yml")
        assert compose is not None
        assert "vantage-backend:${VANTAGE_VERSION:-0.1.0-rc.1}" in compose.read().decode()
        setup = tar.getmember("vantage-deploy-0.1.0-rc.1/setup.sh")
        assert setup.mode & 0o111  # still executable
    top = "vantage-deploy-0.1.0-rc.1/"
    for needed in (
        "setup.sh",
        ".env.example",
        "prometheus/prometheus.yml",
        "LICENSE",
        "NOTICE",
        "docker-compose.yml",
    ):
        assert top + needed in names
    for name in names:
        assert name.startswith(top)
        assert not name.endswith((".key", ".crt", "/.env", ".pyc", "make_bundle.py"))
        assert "/secrets/" not in name and "/certs/" not in name
