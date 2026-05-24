# Release Process

Releases are cut directly from `main`. The repo is solo-maintained
and `main` is the live branch — there is no PR-then-merge step for a
version bump.

`.github/workflows/build-release.yml` triggers on a tag push matching
`X.Y.Z`, builds a wheel with `uv build`, and uploads the artifact to
a GitHub release. Two hard requirements:

- The tag's name **must match** the `version` field in `pyproject.toml`
    exactly, or the workflow looks for an artifact filename that
    doesn't exist.
- A release-notes file **must exist** at
    `.github/release-notes/X.Y.Z.md` in the tagged commit, must be
    non-empty, and must not still contain the template sentinel. The
    workflow checks for this *before* building and fails fast if any
    of those conditions are unmet. See
    [`.github/release-notes/README.md`](../.github/release-notes/README.md)
    for the convention and the tag-shuffle recovery path.

## Steps

1. **Write the release notes.** Copy the template and edit:

    ```bash
    cp .github/release-notes/_template.md .github/release-notes/0.3.1.md
    $EDITOR .github/release-notes/0.3.1.md
    ```

    Replace the template sentinel and every `<placeholder>`. Trim
    sections that don't apply.

1. **Bump the version in `pyproject.toml`.** The `uv_build` PEP 517
    backend has no `uv version` sub-command analogous to `poetry version`
    — edit the field directly.

    ```toml
    [project]
    name = "tkc-lvlab"
    version = "0.3.1"   # bumped from 0.3.0
    ```

    Semver as a reminder: patch (`0.3.0` → `0.3.1`), minor (`0.3.0` →
    `0.4.0`), major (`0.3.0` → `1.0.0`).

1. **Regenerate the lockfile** so the wheel build picks up the new
    version cleanly:

    ```bash
    uv lock
    ```

1. **Commit on `main`** with all three files in one commit:

    ```bash
    git add pyproject.toml uv.lock .github/release-notes/0.3.1.md
    git commit -m "chore: bump version to 0.3.1"
    git push
    ```

1. **Tag and push the tag.** The tag triggers the release workflow.

    ```bash
    git tag -m 'v0.3.1' 0.3.1
    git push --tags
    ```

1. **Verify the GitHub release** appeared at
    `https://github.com/memblin/tkc-lvlab-py/releases` with the wheel
    attached. The release body is the contents of the notes file
    verbatim — no auto-generation, no categorization, no `--generate-notes`.

## Recovery: notes file missing or unfinished at tag time

If the workflow fails the notes check, the tag points at a commit
that doesn't have a usable notes file. Move the tag forward:

```bash
# Delete local + remote tag.
git tag -d 0.3.1
git push origin :refs/tags/0.3.1

# Finish the notes file, commit it on main, push.
$EDITOR .github/release-notes/0.3.1.md
git add .github/release-notes/0.3.1.md
git commit -m "docs: add 0.3.1 release notes"
git push

# Re-tag at the new HEAD.
git tag -m 'v0.3.1' 0.3.1
git push --tags
```

The workflow re-runs on the new tag push and (if the notes file is
ready) creates the release.

## Pre-release tags

Not currently supported by the workflow regex. Tag pattern at
`.github/workflows/build-release.yml` is `[0-9]+.[0-9]+.[0-9]+` —
strict three-segment — so `0.3.0rc1` or `0.3.0a1` would push without
triggering the build. If you want a pre-release lane, extend the
pattern first and add a `--prerelease` flag to the `gh release create`
step when the tag has a suffix.
