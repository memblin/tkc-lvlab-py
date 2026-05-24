# Release Process

Releases are cut directly from `main`. The repo is solo-maintained
and `main` is the live branch — there is no PR-then-merge step for a
version bump.

`.github/workflows/build-release.yml` triggers on a tag push matching
`X.Y.Z`, builds a wheel with `uv build`, and uploads the artifact to
a GitHub release. The tag's name **must match** the `version` field
in `pyproject.toml` exactly, or the workflow looks for an artifact
filename that doesn't exist.

## Steps

1. **Bump the version in `pyproject.toml`.** The `uv_build` PEP 517
    backend has no `uv version` sub-command analogous to `poetry version`
    — edit the field directly.

    ```toml
    [project]
    name = "tkc-lvlab"
    version = "0.3.0"   # bumped from 0.2.4
    ```

    Semver as a reminder: patch (`0.2.4` → `0.2.5`), minor (`0.2.4` →
    `0.3.0`), major (`0.2.4` → `1.0.0`).

1. **Regenerate the lockfile** so the wheel build picks up the new
    version cleanly:

    ```bash
    uv lock
    ```

1. **Commit on `main`** with both files (`pyproject.toml` + `uv.lock`):

    ```bash
    git add pyproject.toml uv.lock
    git commit -m "chore: bump version to 0.3.0"
    git push
    ```

1. **Tag and push the tag.** The tag triggers the release workflow.

    ```bash
    git tag -m 'v0.3.0' 0.3.0
    git push --tags
    ```

1. **Verify the GitHub release** appeared at
    `https://github.com/memblin/tkc-lvlab-py/releases` with the wheel
    attached. The workflow uses `gh release create --generate-notes`,
    so release notes are auto-derived from commits since the previous
    tag.

## Pre-release tags

Not currently supported by the workflow regex. Tag pattern at
`.github/workflows/build-release.yml` is `[0-9]+.[0-9]+.[0-9]+` —
strict three-segment — so `0.3.0rc1` or `0.3.0a1` would push without
triggering the build. If you want a pre-release lane, extend the
pattern first and add a `--prerelease` flag to the `gh release create`
step when the tag has a suffix.
