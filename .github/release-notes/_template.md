<!--
    TEMPLATE: replace this file's content before tagging. The
    build-release.yml workflow will fail the release if the
    "TEMPLATE: replace this file" sentinel above is still present
    when the tag is pushed. See .github/release-notes/README.md for
    the full procedure and the tag-shuffle recovery path.

    Trim sections that don't apply — the release page reads better as
    a short focused note than a long template-shaped one.
-->

## Highlights

- `<one-line summary of the most important change in this release>`
- `<another headline change>`

## Breaking changes

`<None.>` — or describe each break, what it affects, and what
operators should do to migrate. Include the exact commands or config
changes when possible.

## Install / upgrade

```bash
uv tool install --upgrade \
  https://github.com/memblin/tkc-lvlab-py/releases/download/X.Y.Z/tkc_lvlab-X.Y.Z-py3-none-any.whl
```

## Supported hosts

Validated end-to-end on:

- `<distro 1>` (`<system python>`)
- `<distro 2>` (`<system python>`)

See `docs-extra/host-validation.md` for the procedure and the
current matrix.

## Acknowledgments

`<Optional — contributors, issue reporters, downstream tooling authors that made the release possible.>`

## Full commit log

Generate with:

```bash
git log --oneline <previous-tag>..<this-tag>
```

Paste the output below.
