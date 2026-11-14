"""What this backend is (maintainer-approved 2026-11-13): its release, build and commit.

* ``build``: the version baked into the image (Dockerfile ``VANTAGE_VERSION`` build arg ->
  ENV), ``"dev"`` for local runs, tests and CI builds. A final release promotes its rc's
  images unchanged, so a promoted 0.1.1 is built as ``0.1.1-rc.N``.
* ``release``: what the UI shows. ``VANTAGE_RELEASE`` from the compose file (bumped with
  the image tags) when it is a plain version, else ``build``.
* ``commit``: the git commit the image was built from (``VANTAGE_COMMIT`` build arg),
  None when unknown. The same for an rc and its promoted final release; the web app
  compares it with its own to spot a stale browser tab or a half-pulled upgrade.
* ``build_date``: when the image was built (``VANTAGE_BUILD_DATE``), None when unknown.

None of these is a secret, but they are only shown to logged-in users.
"""

from __future__ import annotations

import logging
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass
from functools import cache

log = logging.getLogger(__name__)

# Same shape as the release tags (deploy/make_bundle.py): X.Y.Z or X.Y.Z-pre.
VERSION_RE = re.compile(r"\d+\.\d+\.\d+(?:-[0-9A-Za-z.]+)?")
MAX_VERSION_LENGTH = 64
COMMIT_RE = re.compile(r"[0-9a-f]{7,40}")
DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}")


@dataclass(frozen=True)
class VersionInfo:
    release: str
    build: str
    commit: str | None
    build_date: str | None


def _plain_version(value: str) -> bool:
    return len(value) <= MAX_VERSION_LENGTH and VERSION_RE.fullmatch(value) is not None


def read_version(environ: Mapping[str, str]) -> VersionInfo:
    """The version info from ``environ``. Logs a WARNING (never the value) when
    ``VANTAGE_RELEASE`` is set but isn't a plain version; it is then ignored."""
    build = environ.get("VANTAGE_VERSION", "").strip()
    if not build or (build != "dev" and not _plain_version(build)):
        build = "dev"
    release = build
    raw = environ.get("VANTAGE_RELEASE", "").strip()
    if raw:
        if _plain_version(raw):
            release = raw
        else:
            log.warning(
                "VANTAGE_RELEASE is not a version (want X.Y.Z or X.Y.Z-pre) and is ignored; "
                "showing the image's build version %s",
                build,
            )
    commit = environ.get("VANTAGE_COMMIT", "").strip().lower()
    date = environ.get("VANTAGE_BUILD_DATE", "").strip()
    return VersionInfo(
        release=release,
        build=build,
        commit=commit if COMMIT_RE.fullmatch(commit) else None,
        build_date=date if DATE_RE.fullmatch(date) else None,
    )


@cache
def current() -> VersionInfo:
    """This process's version info, read once (create_app reads it after logging is set
    up, so a WARNING about VANTAGE_RELEASE reaches the log)."""
    return read_version(os.environ)
