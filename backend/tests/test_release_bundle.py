"""deploy/make_bundle.py: the release's install files pull the published images."""

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


@pytest.mark.parametrize("top", ["vantage-deploy-0.1.0-rc.1", "vantage-deploy"])
def test_bundle_holds_what_an_install_needs_and_no_secret(tmp_path: Path, top: str) -> None:
    paths = bundle.build("0.1.0-rc.1", tmp_path)
    path = tmp_path / f"{top}.tar.gz"
    assert path in paths
    with tarfile.open(path) as tar:
        names = set(tar.getnames())
        compose = tar.extractfile(f"{top}/docker-compose.yml")
        assert compose is not None
        assert "vantage-backend:${VANTAGE_VERSION:-0.1.0-rc.1}" in compose.read().decode()
        setup = tar.getmember(f"{top}/setup.sh")
        assert setup.mode & 0o111  # still executable
    for needed in (
        "setup.sh",
        ".env.example",
        "prometheus/prometheus.yml",
        "LICENSE",
        "NOTICE",
        "docker-compose.yml",
    ):
        assert f"{top}/{needed}" in names
    for name in names:
        assert name.startswith(f"{top}/")
        assert not name.endswith((".key", ".crt", "/.env", ".pyc", "make_bundle.py"))
        assert "/secrets/" not in name and "/certs/" not in name


def test_build_writes_both_bundles_and_the_standalone_compose(tmp_path: Path) -> None:
    paths = bundle.build("1.2.3-rc.1", tmp_path)
    assert [p.name for p in paths] == [
        "vantage-deploy-1.2.3-rc.1.tar.gz",
        "vantage-deploy.tar.gz",
        "compose.yml",
    ]
    assert sorted(p.name for p in tmp_path.iterdir()) == sorted(p.name for p in paths)


def test_standalone_compose_gets_the_release_version(tmp_path: Path) -> None:
    source = (REPO / "deploy" / "compose.standalone.yml").read_text()
    bundle.build("1.2.3-rc.9", tmp_path)
    out = (tmp_path / "compose.yml").read_text()
    assert out.count("ghcr.io/levi-xy/vantage-backend:1.2.3-rc.9\n") == 2  # init, backend
    assert out.count("ghcr.io/levi-xy/vantage-frontend:1.2.3-rc.9\n") == 1
    assert "prom/prometheus:v3.2.1" in out
    # Nothing else changes.
    strip = re.compile(r"(vantage-(?:backend|frontend)):\S+")
    assert strip.sub(r"\1", out) == strip.sub(r"\1", source)


def test_standalone_compose_without_the_three_images_is_refused() -> None:
    with pytest.raises(ValueError, match="3 Vantage images"):
        bundle.standalone_compose("image: ghcr.io/levi-xy/vantage-backend:1.0.0\n", "1.2.3")


def test_a_final_release_must_match_the_standalone_compose_version(tmp_path: Path) -> None:
    named = bundle.source_version()
    bundle.check_release_version(named)  # the version in the file: fine
    major, minor, patch = (int(n) for n in named.split("-")[0].split("."))
    newer = f"{major}.{minor}.{patch + 1}"
    with pytest.raises(ValueError, match=f"names {re.escape(named)}"):
        bundle.check_release_version(newer)
    with pytest.raises(ValueError, match="names"):
        bundle.build(newer, tmp_path)
    assert not list(tmp_path.iterdir())  # refused before writing anything


def test_pre_releases_skip_the_version_check() -> None:
    bundle.check_release_version("99.0.0-rc.1")


def test_the_standalone_compose_names_one_version() -> None:
    assert bundle.VERSION_RE.fullmatch(bundle.source_version())
