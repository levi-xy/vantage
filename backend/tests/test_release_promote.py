"""scripts/release_promote.py and release.yml: a final release promotes a tested rc's images."""

import hashlib
import importlib.util
import json
import os
import re
import subprocess
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
import yaml

REPO = Path(__file__).resolve().parents[2]
WORKFLOW = REPO / ".github" / "workflows" / "release.yml"
DIGEST = "sha256:" + "a" * 64
COMMIT = "1" * 40
TAG_OBJECT = "2" * 40


def load() -> ModuleType:
    path = REPO / "scripts" / "release_promote.py"
    spec = importlib.util.spec_from_file_location("release_promote", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


promote = load()


def test_the_newest_rc_of_the_version_is_promoted() -> None:
    tags = ["v0.1.1-rc.2", "v0.1.1-rc.10", "v0.1.1-rc.9", "v0.1.0-rc.12", "v0.1.10-rc.20"]
    assert promote.pick_rc("v0.1.1", tags) == "v0.1.1-rc.10"


def test_a_final_without_an_rc_is_refused() -> None:
    with pytest.raises(promote.PromoteError, match="no release candidate"):
        promote.pick_rc("v0.1.1", ["v0.1.0-rc.1", "v0.1.1-beta.1"])


@pytest.mark.parametrize("tag", ["0.1.1", "v0.1.1-rc.1", "v0.1", "latest"])
def test_only_a_final_tag_is_promoted_to(tag: str) -> None:
    with pytest.raises(promote.PromoteError, match="not a final release tag"):
        promote.pick_rc(tag, ["v0.1.1-rc.1"])


def _standalone() -> str:
    return (REPO / "deploy" / "compose.standalone.yml").read_text()


def test_release_metadata_may_change_between_rc_and_final() -> None:
    at_rc = promote._make_bundle().standalone_compose(_standalone(), "0.1.0")
    at_final = promote._make_bundle().standalone_compose(_standalone(), "0.1.1")
    promote.check_changes(
        ["README.md", "CHANGELOG.md", "deploy/compose.standalone.yml"], at_rc, at_final
    )
    promote.check_changes([], at_rc, at_rc)


@pytest.mark.parametrize(
    "changed", [["backend/app/main.py"], ["README.md", "frontend/package.json"], ["LICENSE"]]
)
def test_any_other_change_since_the_rc_is_refused(changed: list[str]) -> None:
    with pytest.raises(promote.PromoteError, match="more than release metadata"):
        promote.check_changes(changed, _standalone(), _standalone())


def test_a_vantage_release_bump_with_the_tags_is_accepted() -> None:
    """The final's commit may bump the standalone file's image tags and its
    VANTAGE_RELEASE (the version the backend reports), and nothing else."""
    bundle = promote._make_bundle()
    at_rc = bundle.standalone_compose(_standalone(), "1.2.2")
    at_final = bundle.standalone_compose(_standalone(), "1.2.3")
    assert 'VANTAGE_RELEASE: "1.2.2"' in at_rc and 'VANTAGE_RELEASE: "1.2.3"' in at_final
    promote.check_changes(["deploy/compose.standalone.yml"], at_rc, at_final)


@pytest.mark.parametrize(
    ("old", "new"),
    [
        ("VANTAGE_LOG_LEVEL: ${VANTAGE_LOG_LEVEL:-INFO}", "VANTAGE_LOG_LEVEL: DEBUG"),
        ('      VANTAGE_RELEASE: "', '      VANTAGE_EXTRA: "1"\n      VANTAGE_RELEASE: "'),
        ('      VANTAGE_RELEASE: "', '      # VANTAGE_RELEASE: "'),  # dropped
    ],
)
def test_any_other_standalone_env_change_is_refused(old: str, new: str) -> None:
    changed = _standalone().replace(old, new, 1)
    assert changed != _standalone()
    with pytest.raises(promote.PromoteError, match=r"deploy/compose\.standalone\.yml"):
        promote.check_changes(["deploy/compose.standalone.yml"], _standalone(), changed)


def test_the_source_compose_file_never_changes_between_rc_and_final() -> None:
    with pytest.raises(promote.PromoteError, match=r"deploy/docker-compose\.yml"):
        promote.check_changes(["deploy/docker-compose.yml"], _standalone(), _standalone())


def test_a_standalone_compose_change_beyond_the_tags_is_refused() -> None:
    changed = _standalone().replace("restart: unless-stopped", "restart: always", 1)
    assert changed != _standalone()
    with pytest.raises(promote.PromoteError, match="more than its Vantage image tags"):
        promote.check_changes(["deploy/compose.standalone.yml"], _standalone(), changed)


def test_digest_from_imagetools() -> None:
    raw = json.dumps({"mediaType": "application/vnd.oci.image.index.v1+json", "digest": DIGEST})
    assert promote.parse_digest(raw) == DIGEST
    for bad in ("", "[]", json.dumps({"digest": "sha256:abc"}), json.dumps({"digest": 1})):
        with pytest.raises(promote.PromoteError):
            promote.parse_digest(bad)


MANIFEST = b'{"schemaVersion":2,"manifests":[]}'


def test_a_dry_run_that_keeps_the_manifest_is_accepted() -> None:
    promote.check_dry_run(MANIFEST + b"\n", "sha256:" + hashlib.sha256(MANIFEST).hexdigest())


@pytest.mark.parametrize("output", [b"", b"\n", b'{"schemaVersion": 2, "manifests": []}\n'])
def test_a_dry_run_that_would_change_the_manifest_is_refused(output: bytes) -> None:
    with pytest.raises(promote.PromoteError, match="nothing was tagged"):
        promote.check_dry_run(output, "sha256:" + hashlib.sha256(MANIFEST).hexdigest())


def _signature(digest: str, sha: str, *, key: str = "githubWorkflowSha") -> dict[str, Any]:
    return {
        "critical": {"image": {"docker-manifest-digest": digest}},
        "optional": {key: sha},
    }


def test_a_signature_from_the_rc_commit_or_its_tag_object_is_accepted() -> None:
    accepted = {COMMIT, TAG_OBJECT}
    out = json.dumps([_signature(DIGEST, "3" * 40), _signature(DIGEST, COMMIT)])
    assert promote.check_signature(out, DIGEST, accepted) == COMMIT
    out = json.dumps([_signature(DIGEST, TAG_OBJECT, key="1.3.6.1.4.1.57264.1.3")])
    assert promote.check_signature(out, DIGEST, accepted) == TAG_OBJECT


@pytest.mark.parametrize(
    "entries",
    [
        [],
        [_signature(DIGEST, "3" * 40)],  # built from another commit
        [_signature("sha256:" + "b" * 64, COMMIT)],  # a signature on another image
        [{"critical": {}, "optional": {}}],
    ],
)
def test_a_signature_from_elsewhere_is_refused(entries: list[dict[str, Any]]) -> None:
    with pytest.raises(promote.PromoteError, match="no signature"):
        promote.check_signature(json.dumps(entries), DIGEST, {COMMIT, TAG_OBJECT})


# check_source against a real (temporary) git repository.


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    for var in ("GIT_AUTHOR", "GIT_COMMITTER"):
        monkeypatch.setenv(f"{var}_NAME", "Example")
        monkeypatch.setenv(f"{var}_EMAIL", "example@example.invalid")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setattr(promote, "MAKE_BUNDLE", promote.MAKE_BUNDLE)  # still this repo's
    monkeypatch.setattr(promote, "REPO", tmp_path)
    (tmp_path / "deploy").mkdir()
    (tmp_path / "backend").mkdir()
    (tmp_path / "deploy" / "compose.standalone.yml").write_text(_standalone())
    (tmp_path / "backend" / "app.py").write_text("print('example')\n")
    (tmp_path / "README.md").write_text("Vantage\n")
    _git(tmp_path, "init", "-q")
    _commit(tmp_path, "v1.2.3-rc.1")
    return tmp_path


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(  # noqa: S603 -- fixed argv
        ["git", *args],  # noqa: S607
        cwd=cwd,
        check=True,
        capture_output=True,
    )


def _commit(repo: Path, tag: str) -> None:
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "--allow-empty", "-m", tag)
    _git(repo, "tag", "-a", tag, "-m", tag)


def test_the_final_may_be_the_rc_commit_or_a_metadata_only_descendant(repo: Path) -> None:
    rc_commit = promote.check_source("v1.2.3-rc.1", "v1.2.3-rc.1")
    assert re.fullmatch(r"[0-9a-f]{40}", rc_commit)
    compose = repo / "deploy" / "compose.standalone.yml"
    compose.write_text(promote._make_bundle().standalone_compose(compose.read_text(), "1.2.3"))
    (repo / "README.md").write_text("Vantage 1.2.3\n")
    _commit(repo, "v1.2.3")
    assert promote.check_source("v1.2.3-rc.1", "v1.2.3") == rc_commit


def test_a_final_with_code_changes_since_the_rc_is_refused(repo: Path) -> None:
    (repo / "backend" / "app.py").write_text("print('changed')\n")
    _commit(repo, "v1.2.3")
    with pytest.raises(promote.PromoteError, match=r"backend/app\.py"):
        promote.check_source("v1.2.3-rc.1", "v1.2.3")


def test_a_final_off_the_rc_history_is_refused(repo: Path) -> None:
    _git(repo, "checkout", "-q", "--orphan", "other")
    _commit(repo, "v1.2.3")
    with pytest.raises(promote.PromoteError, match="not an ancestor"):
        promote.check_source("v1.2.3-rc.1", "v1.2.3")


# The workflow wiring.


@pytest.fixture(scope="module")
def jobs() -> dict[str, Any]:
    workflow: dict[str, Any] = yaml.safe_load(WORKFLOW.read_text())
    result: dict[str, Any] = workflow["jobs"]
    return result


def _runs(job: dict[str, Any]) -> str:
    return "\n".join(step.get("run", "") for step in job["steps"])


def test_rcs_build_and_finals_promote(jobs: dict[str, Any]) -> None:
    assert jobs["image"]["if"] == "needs.version.outputs.final == 'false'"
    assert jobs["promote"]["if"] == "needs.version.outputs.final == 'true'"
    assert "make_bundle.py --check" in _runs(jobs["version"])  # the compose guard stays
    promote_steps = " ".join(str(s.get("uses", "")) for s in jobs["promote"]["steps"])
    assert "build-push-action" not in promote_steps
    assert set(jobs["bundle"]["needs"]) == {"image", "promote"}
    assert "!failure()" in jobs["bundle"]["if"]


def test_promote_checks_before_it_signs_and_tags(jobs: dict[str, Any]) -> None:
    runs = _runs(jobs["promote"])
    order = [
        "release_promote.py check-source",
        "cosign verify",
        "release_promote.py check-signature",
        "cosign sign",
        "imagetools create --dry-run",
        "release_promote.py check-dry-run",
        '--tag "${image}:${version}"',  # X.Y.Z on both images
        '--tag "${image}:latest"',  # then X.Y and latest
        "for tag in",  # the post-check
    ]
    positions = [runs.index(needle) for needle in order]
    assert positions == sorted(positions)
    # Verified against the rc run's exact identity, never a pattern.
    assert "--certificate-identity-regexp" not in runs
    assert "release.yml@refs/tags/${RC_TAG}" in runs
    checkout = jobs["promote"]["steps"][0]["with"]
    assert checkout["fetch-depth"] == 0
    assert checkout["persist-credentials"] is False


def test_every_action_is_pinned_to_a_commit(jobs: dict[str, Any]) -> None:
    for name, job in jobs.items():
        for step in job["steps"]:
            if "uses" in step:
                assert re.fullmatch(r"[\w./-]+@[0-9a-f]{40}", step["uses"]), (name, step)


def test_rc_images_carry_their_version_and_source_revision(jobs: dict[str, Any]) -> None:
    """The build arg and the OCI revision label come from the same step output, so what
    the backend reports and the image label agree."""
    steps = {s.get("id"): s for s in jobs["image"]["steps"]}
    assert "rev-parse HEAD" in steps["version"]["run"]
    build_args = steps["build"]["with"]["build-args"]
    revision = "${{ steps.version.outputs.revision }}"
    assert f"VANTAGE_COMMIT={revision}" in build_args
    assert "VANTAGE_VERSION=${{ steps.version.outputs.value }}" in build_args
    assert f"org.opencontainers.image.revision={revision}" in steps["meta"]["with"]["labels"]
    order = [s.get("id") for s in jobs["image"]["steps"]]
    assert order.index("version") < order.index("meta") < order.index("build")
