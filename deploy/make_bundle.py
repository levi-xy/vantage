"""Build the install bundle for a release: deploy/ with a compose file that pulls images.

    python deploy/make_bundle.py 0.1.0 dist/

Writes ``dist/vantage-deploy-0.1.0.tar.gz``: ``vantage-deploy-0.1.0/`` holding setup.sh,
.env.example, prometheus/, the optional exporters/ and pfsense/ files, LICENSE, NOTICE and
a docker-compose.yml whose ``build:`` blocks are replaced by the published images,
``ghcr.io/levi-xy/vantage-<service>:${VANTAGE_VERSION:-<version>}``. Generated from the
source compose file, so the two never drift. Standard library only.
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


def _skip(info: tarfile.TarInfo) -> tarfile.TarInfo | None:
    name = Path(info.name).name
    if name in ("__pycache__", ".env") or name.endswith((".pyc", ".key", ".crt")):
        return None
    info.uid = info.gid = 0
    info.uname = info.gname = ""
    return info


def build(version: str, out_dir: Path) -> Path:
    if not VERSION_RE.fullmatch(version):
        raise ValueError(f"not a version: {version!r} (want X.Y.Z or X.Y.Z-pre)")
    top = f"vantage-deploy-{version}"
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"{top}.tar.gz"
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


if __name__ == "__main__":
    if len(sys.argv) != 3:
        sys.exit("usage: make_bundle.py <version> <out-dir>")
    print(build(sys.argv[1].removeprefix("v"), Path(sys.argv[2])))
