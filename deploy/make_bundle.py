"""Build a release's install files: the bundle for setup.sh and the standalone compose file.

    python deploy/make_bundle.py 0.1.0 dist/

Writes into ``dist/``:

* ``vantage-deploy-0.1.0.tar.gz``: ``vantage-deploy-0.1.0/`` holding setup.sh,
  .env.example, prometheus/, the optional exporters/ and pfsense/ files, LICENSE, NOTICE
  and a docker-compose.yml whose ``build:`` blocks are replaced by the published images,
  ``ghcr.io/levi-xy/vantage-<service>:${VANTAGE_VERSION:-<version>}``. Generated from the
  source compose file, so the two never drift.
* ``vantage-deploy.tar.gz``: the same bundle under ``vantage-deploy/``, for the
  version-free ``/releases/latest/download/`` link.
* ``compose.yml``: deploy/compose.standalone.yml with this version in its image tags.

Standard library only.
"""

import io
import re
import sys
import tarfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
DEPLOY = REPO / "deploy"
REGISTRY = "ghcr.io/levi-xy"
SERVICES = ("backend", "frontend")
# Copied as they are; anything else in deploy/ (secrets/, certs/, .env, this script) is not.
INCLUDE = ("setup.sh", ".env.example", "prometheus", "exporters", "pfsense")
VERSION_RE = re.compile(r"\d+\.\d+\.\d+(?:-[0-9A-Za-z.]+)?")
# A Vantage image reference in the standalone compose file, up to the end of its tag.
IMAGE_TAG_RE = re.compile(r"(?P<image>ghcr\.io/levi-xy/vantage-(?:backend|frontend)):\S+")
# Services that run a Vantage image in compose.standalone.yml (init uses the backend's).
STANDALONE_IMAGES = 3
# A service's build block: "build:" and every line indented deeper than it.
BUILD_RE = re.compile(
    r"^    build:\n      context: \.\./(?P<service>[a-z]+)\n(?:(?:      |\s*$).*\n)*",
    re.MULTILINE,
)


def image_compose(source: str, version: str) -> str:
    """The source compose file with each build block replaced by its published image."""
    found: list[str] = []

    def image(match: re.Match[str]) -> str:
        service = match["service"]
        found.append(service)
        return (
            "    # Published image; VANTAGE_VERSION in .env picks another release.\n"
            f"    image: {REGISTRY}/vantage-{service}:${{VANTAGE_VERSION:-{version}}}\n"
        )

    out = BUILD_RE.sub(image, source)
    if sorted(found) != sorted(SERVICES) or re.search(r"^\s+build:", out, re.MULTILINE):
        raise ValueError(
            f"expected one build block per service {SERVICES}, found {found}"
        )
    return out


def standalone_compose(source: str, version: str) -> str:
    """The standalone compose file with ``version`` as the tag of every Vantage image."""
    out, count = IMAGE_TAG_RE.subn(lambda m: f"{m['image']}:{version}", source)
    if count != STANDALONE_IMAGES:
        raise ValueError(f"expected {STANDALONE_IMAGES} Vantage images, found {count}")
    return out


def _skip(info: tarfile.TarInfo) -> tarfile.TarInfo | None:
    name = Path(info.name).name
    if name in ("__pycache__", ".env") or name.endswith((".pyc", ".key", ".crt")):
        return None
    info.uid = info.gid = 0
    info.uname = info.gname = ""
    return info


def _bundle(version: str, path: Path, top: str) -> Path:
    compose = image_compose(
        (DEPLOY / "docker-compose.yml").read_text(), version
    ).encode()
    with tarfile.open(path, "w:gz") as tar:
        for name in INCLUDE:
            tar.add(DEPLOY / name, arcname=f"{top}/{name}", filter=_skip)
        for name in ("LICENSE", "NOTICE"):
            tar.add(REPO / name, arcname=f"{top}/{name}", filter=_skip)
        info = tarfile.TarInfo(f"{top}/docker-compose.yml")
        info.size, info.mode = len(compose), 0o644
        tar.addfile(info, io.BytesIO(compose))
    return path


def build(version: str, out_dir: Path) -> list[Path]:
    """Write the release's install files; returns their paths (versioned bundle first)."""
    if not VERSION_RE.fullmatch(version):
        raise ValueError(f"not a version: {version!r} (want X.Y.Z or X.Y.Z-pre)")
    out_dir.mkdir(parents=True, exist_ok=True)
    versioned = f"vantage-deploy-{version}"
    standalone = out_dir / "compose.yml"
    standalone.write_text(
        standalone_compose((DEPLOY / "compose.standalone.yml").read_text(), version)
    )
    return [
        _bundle(version, out_dir / f"{versioned}.tar.gz", versioned),
        _bundle(version, out_dir / "vantage-deploy.tar.gz", "vantage-deploy"),
        standalone,
    ]


if __name__ == "__main__":
    if len(sys.argv) != 3:
        sys.exit("usage: make_bundle.py <version> <out-dir>")
    for written in build(sys.argv[1].removeprefix("v"), Path(sys.argv[2])):
        print(written)
