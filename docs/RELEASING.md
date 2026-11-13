# Releasing Vantage

For the maintainer. A release goes out in two steps:

1. **A release candidate** (`v0.1.1-rc.1`): the release workflow (`.github/workflows/release.yml`) builds the backend and frontend images from the tagged commit (amd64 and arm64), signs them with cosign (keyless), attaches an SBOM and build provenance, pushes them to GHCR as `0.1.1-rc.1` and drafts a pre-release with the install files.
2. **The final release** (`v0.1.1`): the workflow builds nothing. It **promotes** the newest release candidate of that version: it adds the tags `0.1.1`, `0.1` and `latest` to the very image digests the rc pushed, the ones you tested, and drafts the release.

So the final release ships byte for byte what was tested. A final tag with no rc of its version fails.

What the promotion checks before it tags anything (`scripts/release_promote.py`, tested in `backend/tests/test_release_promote.py`):

- the final version matches `deploy/compose.standalone.yml` (the existing guard, first job, same as for any build);
- the rc it promotes is the highest `vX.Y.Z-rc.N` tag of that version;
- the final tag's commit is the rc's commit, or a descendant whose only changes since the rc are release metadata: `README.md`, `CHANGELOG.md`, and `deploy/compose.standalone.yml` with nothing but its three Vantage image tags changed. None of those is in an image or in the setup.sh bundle, so the install files of the final release are the ones you tested too (apart from the version they name). Anything else (a fix, a docs page, a workflow change) is refused: tag a new rc;
- each rc image's digest carries a cosign signature from this workflow, run for that exact rc tag (`release.yml@refs/tags/vX.Y.Z-rc.N`), started at the rc's commit.

Then it adds a second signature from the final tag's run (so `release.yml@refs/tags/vX.Y.Z` verifies too), makes sure (with a dry run) that copying the tags keeps the rc's digest, adds `X.Y.Z` to both images and only then moves `X.Y` and `latest`, and checks that every new tag resolves to the rc's digest. Both images are checked before either is signed or tagged, and a failure halfway never leaves `latest` on a backend and frontend from different releases. The SBOM and provenance are part of the image index, so they come along unchanged.

You need `git`, `gh`, `docker` and [cosign](https://docs.sigstore.dev/cosign/system_config/installation/) on your machine. The examples use 0.1.1; replace it with the version you release.

## 1. Tag a release candidate

The CHANGELOG entry for the version can land in any PR before this; date it in the bump PR (step 3).

```bash
git switch main && git pull --ff-only
git tag -a v0.1.1-rc.1 -m "Vantage 0.1.1-rc.1"
git push origin v0.1.1-rc.1
```

Watch the run (`gh run watch`). It ends with a **draft pre-release**. Publish it as a pre-release (leave "Set as the latest release" off; GitHub never makes a pre-release "latest", so the README's `/releases/latest/download/` links keep pointing at the last final release). Publishing it lets testers and the anonymous download check below fetch its files.

Write down the two digests you are about to test:

```bash
for name in backend frontend; do
  docker buildx imagetools inspect "ghcr.io/levi-xy/vantage-$name:0.1.1-rc.1" --format '{{json .Manifest}}' | jq -r .digest
done
```

## 2. Test the release candidate

On a clean machine. A throwaway Docker-in-Docker container is one: no images, no login, no volumes from earlier runs.

```bash
docker run -d --privileged -p 127.0.0.1:18443:8443 --name vantage-rc-test docker:27-dind
docker cp scripts/standalone_smoke.sh vantage-rc-test:/smoke.sh
docker exec -it vantage-rc-test sh
```

Inside it:

```bash
until docker info >/dev/null 2>&1; do sleep 1; done
apk add --no-cache curl openssl
cd /root
```

- [ ] **Anonymous pulls, both platforms.** This daemon is logged in nowhere, so a pull proves the packages are public:
  `docker pull ghcr.io/levi-xy/vantage-backend:0.1.1-rc.1` and the same for `vantage-frontend`; `docker buildx imagetools inspect ghcr.io/levi-xy/vantage-backend:0.1.1-rc.1` lists `linux/amd64` and `linux/arm64` (plus the attestation manifests).
- [ ] **Standalone install, smoke script.** `curl -fLO https://github.com/levi-xy/vantage/releases/download/v0.1.1-rc.1/compose.yml`, then `docker compose -f compose.yml pull` and `sh /smoke.sh compose.yml` (the same checks CI runs on every PR, against the published images; it removes its stack at the end).
- [ ] **setup.sh install** (after the smoke script, which frees port 8443 again). `curl -fLO https://github.com/levi-xy/vantage/releases/download/v0.1.1-rc.1/vantage-deploy.tar.gz`, `tar xzf vantage-deploy.tar.gz && cd vantage-deploy && ./setup.sh`: it ends healthy and prints a setup code. On your machine open `https://127.0.0.1:18443` (the port published above), create the admin with the code, add a device against a lab target and see its cards fill.
- [ ] **Checksums.** In the folder with the downloads: `curl -fLO https://github.com/levi-xy/vantage/releases/download/v0.1.1-rc.1/SHA256SUMS && sha256sum -c --ignore-missing SHA256SUMS`.
- [ ] **Upgrade from the last final release**, if the release touches migrations, the compose files or secrets: install the previous version, add a device, then upgrade the way [OPERATIONS.md](OPERATIONS.md#upgrading) says and check the device, its secrets and the dashboards survive.
- [ ] **What the release changes**, tried by hand (the CHANGELOG entry is the list).

On your own machine (cosign isn't in the dind image):

- [ ] **Signatures.** For both images:
  ```bash
  cosign verify ghcr.io/levi-xy/vantage-backend:0.1.1-rc.1 \
    --certificate-identity https://github.com/levi-xy/vantage/.github/workflows/release.yml@refs/tags/v0.1.1-rc.1 \
    --certificate-oidc-issuer https://token.actions.githubusercontent.com
  ```

When done: `docker rm -f -v vantage-rc-test`.

If something is wrong: fix it in a PR, merge, and tag `v0.1.1-rc.2` from the new commit. Start the checklist again for that rc; the final will promote the highest rc number.

## 3. Bump the version

A PR that changes only:

- the three `ghcr.io/levi-xy/vantage-…` image tags in `deploy/compose.standalone.yml` to `0.1.1`;
- the copy of that file in `README.md` (a test keeps the two equal);
- the date of the version's `CHANGELOG.md` entry.

Merge it **with nothing else merged since the rc tag**: anything else between the rc and the final tag makes the promotion refuse (and you'd need a new rc). The README shows the new version from this merge on, so tag the final right after.

**Re-read the README before you tag the final.** The promotion lets `README.md` change between the rc and the final without a new rc, because it isn't in an image or in the install bundle. That also means nothing tests it: the install commands, the standalone compose copy and the links people paste come from this version of the README. Check them by eye (the version in the compose copy, the download and cosign commands), and if the README changed beyond the version bump since the rc, read those parts closely.

(You may also make this PR before tagging the rc: then the rc and the final tag are on the same commit. The README then names 0.1.1 for the whole test phase, while its image doesn't exist yet, so people copying it get a pull error. The order above avoids that.)

## 4. Promote to the final release

Tag the bump PR's merge commit:

```bash
git switch main && git pull --ff-only
git log --oneline v0.1.1-rc.1..HEAD   # only the bump PR
git tag -a v0.1.1 -m "Vantage 0.1.1"
git push origin v0.1.1
```

The run: `version` (the compose guard), then `promote`, then `bundle` (the draft release); `image` is skipped. Check the `promote` job's summary: it lists each image's `0.1.1` digest, which must be the ones you wrote down in step 1.

If `promote` refuses, nothing was tagged or signed. The error says why:

- *no release candidate*: tag and test an rc first;
- *changes more than release metadata since the rc* / *not an ancestor*: tag a new rc from the final commit and test it; then delete the final tag (`git push origin :refs/tags/v0.1.1 && git tag -d v0.1.1`) and push it again once the new rc passed;
- *no signature … from the rc's commit*: the rc tag was moved after its build, or its run didn't finish; tag a new rc.

## 5. Publish

Open the draft release, edit the notes (upgrade notes from the CHANGELOG at the top), tick "Set as the latest release", publish.

## 6. Check the published release

```bash
cd "$(mktemp -d)"
for f in vantage-deploy.tar.gz compose.yml SHA256SUMS; do
  curl -fLO "https://github.com/levi-xy/vantage/releases/latest/download/$f"
done
sha256sum -c --ignore-missing SHA256SUMS
grep -c 'vantage-.*:0.1.1$' compose.yml   # 3
```

- [ ] the `/releases/latest/download/` links fetch this version's files (above);
- [ ] anonymous pulls of `0.1.1`, `0.1` and `latest` work (in a fresh dind container, as in step 2), and their digests are the rc's;
- [ ] the README's cosign command (with `--certificate-identity-regexp`) verifies `0.1.1` for both images;
- [ ] the README's "Release" badge shows 0.1.1.

## Rollback: a bad final release

Never reuse a version number and never delete a published image or release: people may have pulled or pinned it. Move people off it instead, then ship a fixed patch release.

1. **Point `latest` and `X.Y` back at the previous good release** (0.1.0 here), for both images. A single source is copied as it is, so the digest stays the previous release's:
   ```bash
   for name in backend frontend; do
     docker buildx imagetools create \
       --tag "ghcr.io/levi-xy/vantage-$name:latest" \
       --tag "ghcr.io/levi-xy/vantage-$name:0.1" \
       "ghcr.io/levi-xy/vantage-$name:0.1.0"
   done
   ```
   (Logged in to ghcr.io with a token that may write packages: `gh auth token | docker login ghcr.io -u <user> --password-stdin`, with the `write:packages` scope.) Check with `imagetools inspect` that both tags now show 0.1.0's digest. If the bad release is a new minor (0.2.0), leave the `0.2` tag alone; there is no older 0.2 to point it at.
2. **Make the previous release "latest" on GitHub**, so the README's download links fetch it again: `gh release edit v0.1.0 --latest`. Mark the bad one: `gh release edit v0.1.1 --prerelease` and put a warning at the top of its notes saying what is wrong and which version to use.
3. **README**: if the README's standalone copy names the bad version, a PR puts it back to the previous version's tags (`deploy/compose.standalone.yml` and README together).
4. **Tell users** in the release notes (and an issue if it breaks installs): what happened, how to go back (set the image tags back to 0.1.0, or `VANTAGE_VERSION=0.1.0` in `.env`), and whether a migration ran that the old version can't read: if so, going back means restoring the backup taken before the upgrade ([OPERATIONS.md](OPERATIONS.md)).
5. **Fix forward**: the fix in a PR, then `v0.1.2-rc.1` and the normal steps.

If the problem is caught before step 5 (the draft isn't published yet), the images already carry the final tags (they are added before the draft exists, as with a build): do step 1, delete the draft (`gh release delete v0.1.1`), delete the tag (`git push origin :refs/tags/v0.1.1`), and continue with a new rc. Don't tag `v0.1.1` again: the images' `0.1.1` tag still points at the bad digest, so release 0.1.2.

## The version shown in the app

Promotion doesn't change an image, so anything baked into it at build time keeps the rc's value. If the images get a build-time version stamp (a `VANTAGE_VERSION` build argument shown in the UI, in progress at the time of writing), a promoted final release would report `0.1.1-rc.1`. Reading the version from the image's tag at runtime isn't possible (a container doesn't know which tags point at its image). Options:

- **(a)** stamp only `X.Y.Z` in rc builds (`0.1.1` for `v0.1.1-rc.1`): the promoted image then reports the right version; during the test phase the rc reports the version it will become, and which rc it is shows in the image tag and digest (or add the short commit next to it, `0.1.1 (abc1234)`, which stays true after promotion);
- **(b)** keep the full rc version: honest about the build, but every final release reports "-rc.N" in the UI;
- **(c)** rebuild for finals: the shipped image is no longer the tested one, which this flow exists to avoid.

TODO(maintainer): choose (a), (b) or (c) when the version stamp lands. (a) with the short commit is the suggestion: the release workflow would pass `VANTAGE_VERSION=${tag%%-*}` (and the commit) to both image builds.
