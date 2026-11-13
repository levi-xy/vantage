"""Build a release's install files: the bundle for setup.sh and the standalone compose file.

    python deploy/make_bundle.py 0.1.0 dist/

Writes into ``dist/``:

* ``vantage-deploy-0.1.0.tar.gz``: ``vantage-deploy-0.1.0/`` holding setup.sh,
  .env.example, prometheus/, the optional exporters/ and pfsense/ files, LICENSE, NOTICE
  and a docker-compose.yml whose ``build:`` blocks are replaced by the published images,
  ``ghcr.io/levi-xy/vantage-<service>:${VANTAGE_VERSION:-<version>}``, and the backend's
  ``VANTAGE_RELEASE`` set to the same value: the version the UI shows (a promoted final
  release runs its rc's images, whose baked build version is the rc's). Generated from
  the source compose file, so the two never drift.
* ``vantage-deploy.tar.gz``: the same bundle under ``vantage-deploy/``, for the
  version-free ``/releases/latest/download/`` link.
* ``compose.yml``: deploy/compose.standalone.yml with this version in its image tags and
  its ``VANTAGE_RELEASE``.

A final release (a version without ``-pre``) must match the version already in
deploy/compose.standalone.yml (its image tags and its ``VANTAGE_RELEASE``), which the
README shows (a test keeps the two equal), so the copy people paste never names an old
release. The source deploy/docker-compose.yml builds from source and names no release:
its ``VANTAGE_RELEASE`` exists only in the bundle, written next to the image tag. The
release workflow checks both before it builds any image::

    python deploy/make_bundle.py --check 0.1.0

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
# The backend's release version in compose.standalone.yml (exactly one, quoted).
RELEASE_RE = re.compile(r'^(?P<key>      VANTAGE_RELEASE: )"(?P<version>[^"\n]*)"$', re.MULTILINE)
# The backend service in the source compose file, up to its "environment:" line: every
# line of the service is indented by at least 4 spaces (or blank).
BACKEND_ENV_RE = re.compile(
    r"^  backend:\n(?:(?:    |\s*$).*\n)*?    environment:\n", re.MULTILINE
)
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

    if "VANTAGE_RELEASE" in source:
        raise ValueError(
            "deploy/docker-compose.yml builds from source and must not set VANTAGE_RELEASE: "
            "the bundle gets it next to the image tag"
        )
    out = BUILD_RE.sub(image, source)
    if sorted(found) != sorted(SERVICES) or re.search(r"^\s+build:", out, re.MULTILINE):
        raise ValueError(
            f"expected one build block per service {SERVICES}, found {found}"
        )
    out, count = BACKEND_ENV_RE.subn(
        lambda m: m[0]
        + "      # The release these images are, shown in the UI (follows VANTAGE_VERSION).\n"
        + f"      VANTAGE_RELEASE: ${{VANTAGE_VERSION:-{version}}}\n",
        out,
    )
    if count != 1:
        raise ValueError("expected one environment block in the backend service")
    return out


def standalone_compose(source: str, version: str) -> str:
    """The standalone compose file with ``version`` as the tag of every Vantage image and
    as the backend's ``VANTAGE_RELEASE``."""
    out, count = IMAGE_TAG_RE.subn(lambda m: f"{m['image']}:{version}", source)
    if count != STANDALONE_IMAGES:
        raise ValueError(f"expected {STANDALONE_IMAGES} Vantage images, found {count}")
    out, count = RELEASE_RE.subn(lambda m: f'{m["key"]}"{version}"', out)
    if count != 1:
        raise ValueError(f"expected one VANTAGE_RELEASE, found {count}")
    return out


def source_version() -> str:
    """The version deploy/compose.standalone.yml names: one for its three images and the
    backend's VANTAGE_RELEASE."""
    source = (DEPLOY / "compose.standalone.yml").read_text()
    tags = {m[0].rsplit(":", 1)[1] for m in IMAGE_TAG_RE.finditer(source)}
    releases = [m["version"] for m in RELEASE_RE.finditer(source)]
    if len(releases) != 1:
        raise ValueError(
            f"compose.standalone.yml needs one VANTAGE_RELEASE, found {len(releases)}"
        )
    named = tags | set(releases)
    if len(named) != 1:
        raise ValueError(
            "compose.standalone.yml names more than one version (image tags and "
            f"VANTAGE_RELEASE): {sorted(named)}"
        )
    return named.pop()


def check_release_version(version: str) -> None:
    """Refuse a final release whose version isn't the one compose.standalone.yml names
    (its image tags and VANTAGE_RELEASE), and any release whose bundle compose file can't
    get its image tags and VANTAGE_RELEASE.

    Pre-releases (``1.2.0-rc.1``) skip the version match: the README keeps showing the
    last final release.
    """
    if not VERSION_RE.fullmatch(version):
        raise ValueError(f"not a version: {version!r} (want X.Y.Z or X.Y.Z-pre)")
    image_compose((DEPLOY / "docker-compose.yml").read_text(), version)
    if "-" in version:
        return
    named = source_version()
    if named != version:
        raise ValueError(
            f"the tag is {version} but deploy/compose.standalone.yml (and the README copy) "
            f"names {named}. Bump both to {version} in a PR, merge it, then tag that commit."
        )


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
    check_release_version(version)
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
    try:
        if len(sys.argv) == 3 and sys.argv[1] == "--check":
            tag = sys.argv[2].removeprefix("v")
            check_release_version(tag)
            if "-" in tag:
                print(f"ok: {tag} is a pre-release; the README version isn't checked")
            else:
                print(f"ok: {tag} matches deploy/compose.standalone.yml")
        elif len(sys.argv) == 3:
            for written in build(sys.argv[1].removeprefix("v"), Path(sys.argv[2])):
                print(written)
        else:
            sys.exit("usage: make_bundle.py <version> <out-dir> | --check <version>")
    except ValueError as exc:
        sys.exit(f"error: {exc}")
