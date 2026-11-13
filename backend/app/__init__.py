"""Vantage backend."""

import os

# The release version, set by the image build (Dockerfile: VANTAGE_VERSION build arg ->
# ENV). "dev" for local runs, tests and CI builds. Shown to logged-in users only.
__version__ = os.environ.get("VANTAGE_VERSION", "").strip() or "dev"
# When the image was built (ISO date, set by the release workflow); None for dev builds.
BUILD_DATE: str | None = os.environ.get("VANTAGE_BUILD_DATE", "").strip() or None
