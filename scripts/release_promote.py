"""Checks for promoting a release candidate's images to a final release (release.yml).

A final tag (v1.2.3) doesn't build images: the release workflow promotes the images of
the newest release candidate of that version (v1.2.3-rc.N), the exact ones the
maintainer tested. This script holds the decisions, so they are tested; the workflow
runs the registry and cosign commands around it::

    python3 scripts/release_promote.py rc-tag v1.2.3
        the rc tag to promote: the highest N among the repository's v1.2.3-rc.N tags

    python3 scripts/release_promote.py check-source v1.2.3-rc.2 v1.2.3
        the final tag's commit is the rc's commit, or a descendant whose only changes are
        release metadata (README.md, CHANGELOG.md, and deploy/compose.standalone.yml with
        nothing but its Vantage image tags and the backend's VANTAGE_RELEASE changed);
        prints the rc's commit

    python3 scripts/release_promote.py digest < inspect.json
        the manifest digest from `docker buildx imagetools inspect --format '{{json .Manifest}}'`

    python3 scripts/release_promote.py check-dry-run sha256:... < dry-run.json
        `docker buildx imagetools create --dry-run` output is that exact manifest, so
        the tags it would add point at the rc's digest

    python3 scripts/release_promote.py check-signature cosign.json sha256:... v1.2.3-rc.2
        `cosign verify --output json` holds a signature on that digest made by the
        release workflow run for that rc tag at the rc's commit

Standard library only (plus deploy/make_bundle.py from this repository).
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import re
import subprocess
import sys
from pathlib import Path
from types import ModuleType

REPO = Path(__file__).resolve().parent.parent
MAKE_BUNDLE = REPO / "deploy" / "make_bundle.py"
FINAL_TAG_RE = re.compile(r"v(?P<version>\d+\.\d+\.\d+)")
DIGEST_RE = re.compile(r"sha256:[0-9a-f]{64}")
SHA_RE = re.compile(r"[0-9a-f]{40}")
STANDALONE = "deploy/compose.standalone.yml"
# Files that may change between the tested rc and the final tag: none of them is in an
# image (the build contexts are backend/, frontend/, LICENSE and NOTICE) or in the
# install bundle, and the standalone compose file is rewritten to the release's version
# by make_bundle.py anyway. In the standalone file only the image tags and VANTAGE_RELEASE
# may change (the version the promoted images report, bumped with the tags).
# deploy/docker-compose.yml is not listed: it names no version (the bundle gets its
# image tags and VANTAGE_RELEASE from make_bundle.py), so it never needs a change.
RELEASE_METADATA = ("README.md", "CHANGELOG.md", STANDALONE)


class PromoteError(ValueError):
    pass


def _make_bundle() -> ModuleType:
    spec = importlib.util.spec_from_file_location("make_bundle", MAKE_BUNDLE)
    if spec is None or spec.loader is None:
        raise PromoteError(f"cannot load {MAKE_BUNDLE}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def final_version(final_tag: str) -> str:
    match = FINAL_TAG_RE.fullmatch(final_tag)
    if not match:
        raise PromoteError(f"not a final release tag: {final_tag!r} (want vX.Y.Z)")
    return match["version"]


def pick_rc(final_tag: str, tags: list[str]) -> str:
    """The rc tag to promote: the highest N among ``v<version>-rc.N``."""
    version = final_version(final_tag)
    rc_re = re.compile(rf"v{re.escape(version)}-rc\.(?P<n>0|[1-9]\d*)")
    candidates = [(int(m["n"]), tag) for tag in tags if (m := rc_re.fullmatch(tag.strip()))]
    if not candidates:
        raise PromoteError(
            f"no release candidate v{version}-rc.N to promote: a final release ships the "
            f"images of a tested rc, so tag and test v{version}-rc.1 first (docs/RELEASING.md)"
        )
    return max(candidates)[1]


def check_changes(changed: list[str], standalone_at_rc: str, standalone_at_final: str) -> None:
    """Refuse a change between the rc and the final commit that could alter what ships."""
    other = sorted(set(changed) - set(RELEASE_METADATA))
    if other:
        raise PromoteError(
            "the final tag's commit changes more than release metadata since the rc: "
            f"{', '.join(other)}. Tag a new rc from this commit and test that instead."
        )
    if STANDALONE in changed:
        bundle = _make_bundle()
        try:
            same = bundle.standalone_compose(
                standalone_at_rc, "0.0.0"
            ) == bundle.standalone_compose(standalone_at_final, "0.0.0")
        except ValueError as exc:
            raise PromoteError(f"{STANDALONE}: {exc}") from exc
        if not same:
            raise PromoteError(
                f"{STANDALONE} changes more than its Vantage image tags and VANTAGE_RELEASE "
                "since the rc. "
                "Tag a new rc from this commit and test that instead."
            )


def check_dry_run(manifest: bytes, digest: str) -> None:
    """Refuse unless `imagetools create --dry-run` would push the source manifest as is.

    The dry run prints the manifest it would push followed by one newline; its digest
    is the SHA-256 of those bytes.
    """
    body = manifest.removesuffix(b"\n")
    got = "sha256:" + hashlib.sha256(body).hexdigest()
    if not body or got != digest:
        raise PromoteError(
            f"imagetools create would push {got}, not the rc's {digest}: nothing was tagged"
        )


def parse_digest(inspect_json: str) -> str:
    """The digest of `imagetools inspect --format '{{json .Manifest}}'` output."""
    try:
        digest = json.loads(inspect_json).get("digest")
    except (json.JSONDecodeError, AttributeError) as exc:
        raise PromoteError("unreadable imagetools output") from exc
    if not isinstance(digest, str) or not DIGEST_RE.fullmatch(digest):
        raise PromoteError(f"not a manifest digest: {digest!r}")
    return digest


def check_signature(cosign_json: str, digest: str, accepted_shas: set[str]) -> str:
    """The workflow commit of a verified signature on ``digest``; refuses any other.

    ``cosign verify`` has already checked the certificate identity (the release workflow
    at the rc tag) and issuer; this adds the commit the signing run was started for.
    For an annotated tag that may be the tag object instead of the commit, so the
    workflow passes both.
    """
    try:
        entries = json.loads(cosign_json)
    except json.JSONDecodeError as exc:
        raise PromoteError("unreadable cosign output") from exc
    if not isinstance(entries, list):
        raise PromoteError("unreadable cosign output")
    seen: list[str] = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        critical = entry.get("critical") or {}
        optional = entry.get("optional") or {}
        signed = (critical.get("image") or {}).get("docker-manifest-digest")
        sha = optional.get("githubWorkflowSha") or optional.get("1.3.6.1.4.1.57264.1.3")
        if signed != digest or not isinstance(sha, str):
            continue
        if sha in accepted_shas:
            return sha
        seen.append(sha)
    raise PromoteError(
        f"no signature on {digest} from the rc's commit "
        f"(signed at: {', '.join(seen) or 'none'}; want one of {', '.join(sorted(accepted_shas))})"
    )


def _run_git(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603 - fixed git argv, no shell
        ["git", *args],  # noqa: S607 - git from PATH, as every other repo tool
        cwd=REPO,
        check=check,
        capture_output=True,
        text=True,
    )


def _git(*args: str) -> str:
    return _run_git(*args).stdout


def _commit(ref: str) -> str:
    sha = _git("rev-parse", "--verify", f"refs/tags/{ref}^{{commit}}").strip()
    if not SHA_RE.fullmatch(sha):
        raise PromoteError(f"cannot resolve {ref}")
    return sha


def check_source(rc_tag: str, final_tag: str) -> str:
    rc_commit, final_commit = _commit(rc_tag), _commit(final_tag)
    if rc_commit != final_commit:
        if _run_git("merge-base", "--is-ancestor", rc_commit, final_commit, check=False).returncode:
            raise PromoteError(
                f"{rc_tag} ({rc_commit}) is not an ancestor of {final_tag} ({final_commit})"
            )
        changed = _git("diff", "--name-only", "--no-renames", rc_commit, final_commit).splitlines()

        def standalone(commit: str) -> str:
            return _git("show", f"{commit}:{STANDALONE}")

        check_changes(changed, standalone(rc_commit), standalone(final_commit))
    return rc_commit


def main(argv: list[str]) -> None:
    match argv:
        case ["rc-tag", final_tag]:
            print(pick_rc(final_tag, _git("tag", "--list", "v*-rc.*").splitlines()))
        case ["check-source", rc_tag, final_tag]:
            print(check_source(rc_tag, final_tag))
        case ["digest"]:
            print(parse_digest(sys.stdin.read()))
        case ["check-dry-run", digest]:
            check_dry_run(sys.stdin.buffer.read(), digest)
            print(f"ok: the copy keeps {digest}")
        case ["check-signature", path, digest, rc_tag]:
            accepted = {
                _commit(rc_tag),
                _git("rev-parse", "--verify", f"refs/tags/{rc_tag}").strip(),
            }
            print(check_signature(Path(path).read_text(), digest, accepted))
        case _:
            sys.exit(__doc__)


if __name__ == "__main__":
    try:
        main(sys.argv[1:])
    except (PromoteError, subprocess.CalledProcessError) as exc:
        sys.exit(f"error: {exc}")
