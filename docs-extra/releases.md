# Release Process

Releases are cut directly from `main`. The repo is solo-maintained
and `main` is the live branch — there is no PR-then-merge step for a
version bump.

`.github/workflows/build-release.yml` triggers on a tag push matching
`X.Y.Z`, builds a wheel with `uv build`, and uploads the artifact to
a GitHub release. The package version is derived from the tag itself
by `uv-dynamic-versioning` (Hatchling build backend) — there is **no
`version` field in `pyproject.toml` to keep in sync**. Two things to
know:

- The tag **is** the version. Building from the exact tagged commit
    yields a clean version (`0.3.1` from tag `0.3.1`), so the workflow
    uploads `tkc_lvlab-<tag>-py3-none-any.whl`. This relies on the
    release workflow checking out full history (`fetch-depth: 0`,
    already configured); a shallow checkout would hide the tag and
    produce a `0.0.0.devN` fallback version instead.
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

1. **Commit the notes file on `main`** and push. There is **no
    version bump** — the version comes from the tag at build time, so
    this commit carries only the notes file:

    ```bash
    git add .github/release-notes/0.3.1.md
    git commit -m "docs: add 0.3.1 release notes"
    git push
    ```

1. **Tag and push the tag.** The tag name *is* the version — nothing
    in `pyproject.toml` to bump. The semver decision is made here when
    you choose the tag: patch (`0.3.0` → `0.3.1`), minor (`0.3.0` →
    `0.4.0`), major (`0.3.0` → `1.0.0`). Tag a commit on `main` that
    already contains the notes file.

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

The workflow accepts PEP 440 pre-release suffixes alongside stable
tags. Supported tag shapes:

- `X.Y.Z` — stable release.
- `X.Y.ZrcN` — release candidate (e.g. `0.4.0rc1`).
- `X.Y.ZaN` — alpha (e.g. `0.4.0a1`).
- `X.Y.ZbN` — beta (e.g. `0.4.0b1`).

The workflow detects anything that isn't strict `X.Y.Z` and passes
`--prerelease` to `gh release create`. GitHub marks the release as a
pre-release in the UI, and `pip install tkc-lvlab` (without
`--pre`) will not promote it over the latest stable.

Procedure is identical to a stable release — same per-tag notes file
under `.github/release-notes/`, same commit-then-tag. The pre-release
suffix lives only in the tag name:

```bash
cp .github/release-notes/_template.md .github/release-notes/0.4.0rc1.md
$EDITOR .github/release-notes/0.4.0rc1.md
git add .github/release-notes/0.4.0rc1.md
git commit -m "docs: add 0.4.0rc1 release notes"
git push
git tag -m 'v0.4.0rc1' 0.4.0rc1
git push --tags
```

The wheel artifact name follows the tag exactly:
`tkc_lvlab-0.4.0rc1-py3-none-any.whl`.
