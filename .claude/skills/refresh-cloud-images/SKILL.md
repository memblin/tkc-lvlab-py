---
name: refresh-cloud-images
description: Refresh the cloud-image catalog in this tkc-lvlab repo to the latest upstream dated build of each existing entry. Use when the user asks to update, refresh, bump, or check for new dated builds of the built-in or example cloud images. Out of scope — adding or removing a major release (e.g. fedora40 → fedora43, debian13 → debian14) is handled separately, not by this skill.
---

# Refresh cloud-image catalog

Updates every existing entry in this repo's three-file catalog to the
most recent dated build of that entry's current major version. Modeled
on the `refresh-cloud-images` skill from
[`lvscripts-py`](https://github.com/memblin/lvscripts-py) — see that
repo's `.claude/skills/refresh-cloud-images/SKILL.md` for the original.

**Scope.** This skill refreshes intra-major dates only. It does not add
new catalog keys, remove old ones, or migrate an entry to a newer major
release (e.g. fedora40 → fedora43, debian13 → debian14). Those are
managed separately. If an upstream listing shows only newer-major builds
with nothing new for the existing entry's major, mark the row "none" and
flag it for the user — do not silently bump the major.

## Files this skill edits

Three files stay in lockstep — every entry that appears in more than one
must be updated in all of them together. Never edit only one.

- `tkc_lvlab/scripts/createvm.py` — the `BUILTIN_IMAGES` dict for the
    standalone `createvm` console script. Currently
    `{debian12, debian13}` after the 2026-05-23 fedora40 drop.
- `Lvlab.yml` (repo root) — the maintainer's working manifest, also a
    de-facto example for users who clone the repo. `images:` section.
- `docs/Lvlab.example.yml` — the canonical example shipped in the
    site `exclude_docs` set. Wider catalog than `BUILTIN_IMAGES`
    (currently lists debian10, debian11, debian12, debian13 plus two
    custom intranet images).

**Custom / intranet images.** `docs/Lvlab.example.yml` includes
`debian12-salt` and `debian12-vault` entries pointing at
`http://192.168.122.1:8080/cloud_images/...`. These are illustrative
examples of how a user can register their own pre-baked image — they
are NOT upstream URLs and **this skill must never touch them**. Flag
them in the diff table with action "skip (intranet)" and move on.

## Procedure

1. **Read the current catalog.** Parse `tkc_lvlab/scripts/createvm.py`
    `BUILTIN_IMAGES` (Python source) and the `images:` sections of
    `Lvlab.yml` and `docs/Lvlab.example.yml` (YAML). Build the union
    of entry keys — note which keys appear in which files (some
    appear in all three, some only in the example).

2. **For each entry, identify what to fetch.** The catalog uses
    `image_url` (full URL); the parent directory is what to list for
    upstream builds. Examples:

    - `https://cloud.debian.org/images/cloud/trixie/latest/debian-13-generic-amd64.qcow2`
        → fetch `https://cloud.debian.org/images/cloud/trixie/latest/`.
    - `https://cloud.debian.org/images/cloud/bookworm/20240717-1811/debian-12-generic-amd64-20240717-1811.qcow2`
        → fetch `https://cloud.debian.org/images/cloud/bookworm/`
        (the dated `20240717-1811/` segment is what we're refreshing,
        so list its parent).

3. **Fetch every upstream listing in parallel** with WebFetch — one
    tool call per upstream directory, all in a single message. Ask
    the prompt to "return the most recent dated build filename" and
    to "list ALL dated entries (the index has many; do not truncate)".

4. **Watch for summarized listings.** WebFetch occasionally returns
    only a handful of entries even when the real index has dozens. If
    the result looks suspiciously short — e.g., the newest date is
    older than the current entry in the catalog, or older than today
    — re-fetch with a more explicit prompt that asks for the *very
    last* entry and forbids truncation. Repeat until you have a
    stable answer.

5. **Build a diff table** before editing anything:

    | key | files | current build | latest upstream (same major) | action |

    Mark each row UPDATE, none, or skip (intranet). The "latest
    upstream" column compares only against builds that match the
    entry's current major version — e.g., for `debian13`, look only
    at trixie builds, not at any future `debian14`. The "files"
    column tracks where the entry lives (createvm, working manifest,
    example, or some subset) so the edits in step 6 cover everything.

    **Show this table to the user before editing.**

6. **Edit every file listed in the row's "files" column** for entries
    marked UPDATE. For each entry, change only:

    - `image_url` — full URL, including any dated directory segment
    - `checksum_url` — same dated path component must bump in
        lockstep with `image_url`. If they drift, checksum
        verification will load a checksum file for an older build
        and reject the newer image as corrupt. This is the most
        common refresh-bug.

    Leave the other fields untouched: `checksum_type`,
    `checksum_url_gpg` (Fedora's `https://fedoraproject.org/fedora.gpg`
    is stable across point releases; never bump it as part of an
    intra-major refresh), `network_version`, `os_variant`,
    `default_username` (in `BUILTIN_IMAGES`).

    The mirrored YAML entries (in `Lvlab.yml` and
    `docs/Lvlab.example.yml`) for a shared key must match exactly —
    same `image_url`, same `checksum_url`.

7. **Verify**, in this order:

    ```bash
    uv run pytest -q
    uv run pre-commit run --all-files
    ```

    Both must pass. (Optional: `uv run mkdocs build --strict` — cheap,
    ~2s, catches any doc rebuild surprises but not strictly needed
    for a URL-only refresh.)

    Then HEAD-check every URL that changed:

    ```bash
    for url in <updated urls>; do
      curl -fsIL -o /dev/null -w "%{http_code} %s\n" --max-time 15 "$url" "$url"
    done
    ```

    Every URL must return 200. If any 404s, the dated filename was
    wrong — re-fetch the listing and correct it before reporting done.

8. **Stop. Do not commit.** Print a final summary table (key | old →
    new | files touched) and wait for the user to inspect the diff
    and commit themselves. If any row was marked "none" because the
    only new upstream builds were under a different major release,
    mention it in the summary so the user can decide whether to
    schedule a separate major-version add/remove.

## Per-distro gotchas

- **Debian**: dated dirs are `YYYYMMDD-NNNN` and the filename inside
    *also* encodes that exact stamp — both copies of the date in the
    URL must match. `debian-12-generic-amd64-20240717-1811.qcow2`
    must sit under `bookworm/20240717-1811/`. The checksum URL points
    at `SHA512SUMS` inside the same dated directory, so its path
    bumps too. **Debian 11 stays on `network_version: 1`** for the
    ifupdown DHCPv6 hang documented in `CLAUDE.md` and the
    `NetworkVersion` enum docstring — never flip it to v2.
    `cloud_image_basedir` doubling is now idempotent (fix landed
    2026-05-23, commit `841cb9f`) — the skill doesn't interact with
    that knob but it's worth knowing why the surrounding code reads
    the way it does.
- **Debian `trixie/latest/`** is a stable redirect-style path — the
    URL doesn't carry a dated segment. The actual build under it
    rotates upstream; you don't see that in our URL. Treat
    `debian13` as a "no change" unless Debian moves trixie itself.
- **Fedora**: lives under `releases/<N>/Cloud/x86_64/images/`. A new
    `<N>` release is **out of scope for this skill** — note it in the
    summary so the user can handle the major bump separately. The
    catalog is Debian-only as of 2026-05-23; adding a current Fedora
    back is tracked in TODO.md.
- **Custom / intranet images** (`debian12-salt`, `debian12-vault`,
    anything under `http://192.168.122.1:8080/`): **skip entirely**.
    These are user-baked images, not upstream releases. They should
    appear in the diff table with action "skip (intranet)" so the
    user sees they were considered and deliberately left alone.

## Why three files instead of two

The lvscripts skill maintains one Python dict + one YAML example.
This repo has two YAML files because the `Lvlab.yml` at the repo root
is the maintainer's working manifest (and what the destructive smoke
test uses as a real exercise of the manifest path) while
`docs/Lvlab.example.yml` is the documented example for new users.
Keeping them in lockstep means a user copying the example and
clobbering it with the repo's `Lvlab.yml` (or vice versa) won't see
mysterious image-not-found errors.

The `images:` sections of those two YAML files don't always carry
the same keys — `Lvlab.example.yml` is richer for illustration
purposes (debian10, debian11, custom intranet entries). The diff
table's "files" column captures which keys live in which files;
treat them per-file rather than assuming a single union.
