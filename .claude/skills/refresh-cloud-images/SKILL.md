---
name: refresh-cloud-images
description: Refresh the cloud-image catalog in this tkc-lvlab repo. Bumps existing entries to the latest upstream dated build of their current major version, and surfaces upstream major-version additions and end-of-life removals for the user to confirm. Use when the user asks to update, refresh, bump, check for new dated builds, or audit the built-in cloud images.
---

# Refresh cloud-image catalog

Maintains this repo's three-file cloud-image catalog. Modeled on the
`refresh-cloud-images` skill from
[`lvscripts-py`](https://github.com/memblin/lvscripts-py) — see that
repo's `.claude/skills/refresh-cloud-images/SKILL.md` for the original,
single-file version.

## Scope

Three classes of change; only one applies without an extra confirmation:

- **Intra-major refresh** (auto-apply). Existing entry, newer dated
    build under the same major release. Applied automatically as part
    of the procedure; the user still commits at the end.
- **New-major proposal** (`PROPOSE-ADD`, user-confirmed). Upstream
    carries a newer major release (e.g. `fedora43`) that this catalog
    doesn't list. The skill surfaces it in the diff table with the
    upstream metadata the user would need (`network_version`, checksum +
    GPG URLs; plus an explicit `os_variant`/`username` only if the
    key-based derivation would be wrong) — and **only** adds it if the
    user explicitly confirms.
- **EOL proposal** (`PROPOSE-REMOVE`, user-confirmed). An entry the
    catalog carries is no longer published on the live upstream
    mirror — codename moved to an archive host, `image_url` 404s, or
    the release directory is gone from the index. Surfaced as a
    removal candidate; **never silently removed**.

The skill never silently bumps a major or drops a major. New-major
and EOL rows always wait on the user.

## Files this skill edits

Three files stay in lockstep — every entry that appears in more than
one must be updated in all of them together. Never edit only one.

- `tkc_lvlab/scripts/createvm.py` — the `BUILTIN_IMAGES` dict for the
    standalone `createvm` console script. Currently
    `{debian12, debian13, fedora44}`. Each value uses the **same schema
    as an `Lvlab.yml` `images:` entry** (`image_url`, `checksum_url`,
    `checksum_type`, `checksum_url_gpg`, `network_version`). The
    `os_variant` and `username` keys are **optional**: `createvm` derives
    them from the entry key (e.g. `fedora44` → os_variant `fedora44`,
    user `fedora`), so only add them to a dict when the derivation would
    be wrong.
- `Lvlab.yml` (repo root) — the maintainer's working manifest, also a
    de-facto example for users who clone the repo. `images:` section.
- `docs/Lvlab.example.yml` — the canonical example for new users.
    Lives under `docs/` and is embedded into the rendered
    `example-manifest.md` page via the `pymdownx.snippets`
    extension. Wider catalog than `BUILTIN_IMAGES` (currently
    lists debian12, debian13, almalinux10, fedora44 plus two
    custom intranet images).

When a `PROPOSE-REMOVE` row is confirmed, also sweep the rendered
`docs/` user-guide pages (walkthrough.md, why.md, libvirt-notes.md,
example-manifest.md, cloud-init-examples.md, and any new pages)
plus the contributor reference under `docs-extra/` (Design.md,
CONTRIBUTING.md, host-validation.md) and repo-root `README.md` for
residual references to the removed key. A stale distro name in an example block is exactly the
kind of thing a new user pastes verbatim and then files a bug report
against. Remove or update each reference in the same edit pass and
surface them in the final summary so the user sees what shifted
beyond the catalog files.

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
    of entry keys — note which keys appear in which files.

2. **For each existing entry, identify what to fetch.** The catalog
    uses `image_url` (full URL); the parent directory is what to list
    for upstream builds. Examples:

    - `https://cloud.debian.org/images/cloud/trixie/latest/debian-13-generic-amd64.qcow2`
        → fetch `https://cloud.debian.org/images/cloud/trixie/latest/`.
    - `https://cloud.debian.org/images/cloud/bookworm/20240717-1811/debian-12-generic-amd64-20240717-1811.qcow2`
        → fetch `https://cloud.debian.org/images/cloud/bookworm/` (the
        dated `20240717-1811/` segment is what we're refreshing, so
        list its parent).

3. **Also fetch each family's release index** to detect new majors and
    EOL drift. One call per family:

    - Debian: `https://cloud.debian.org/images/cloud/` — lists current
        published codenames. A codename we carry that no longer
        appears here is a `PROPOSE-REMOVE` candidate (it's been
        archived). A new codename appearing here that we don't carry
        is a `PROPOSE-ADD` candidate.
    - Fedora: `https://download.fedoraproject.org/pub/fedora/linux/releases/`
        — lists current numbered releases. The highest number is the
        newest published Fedora. If the highest release is newer than
        the highest `fedora*` key in the catalog, queue a
        `PROPOSE-ADD`. If any `fedora*` key we carry isn't in the
        listing, queue a `PROPOSE-REMOVE`.

4. **Fetch every upstream listing in parallel** with WebFetch — one
    tool call per upstream directory (including the family release
    indexes from step 3), all in a single message. Ask the prompt to
    "return the most recent dated build filename" and to "list ALL
    dated entries (the index has many; do not truncate)".

5. **Watch for summarized listings.** WebFetch occasionally returns
    only a handful of entries even when the real index has dozens. If
    the result looks suspiciously short — e.g., the newest date is
    older than the current entry in the catalog, or older than today
    — re-fetch with a more explicit prompt that asks for the *very
    last* entry and forbids truncation. Repeat until you have a
    stable answer.

6. **Build a diff table** before editing anything:

    | key | files | current build | latest upstream (same major) | action | notes |

    Actions:

    - `UPDATE` — existing entry, newer dated build on the same major.
    - `none` — existing entry, already at the latest upstream build.
    - `skip (intranet)` — custom/intranet entry, never touched.
    - `PROPOSE-ADD` — new major not in the catalog. `current build`
        is `—`; `latest upstream` is the newest dated build of the
        new major; `notes` includes `network_version`, the checksum +
        GPG URLs the user will need, and the derived `os_variant` /
        username (flag if the key-based derivation looks wrong and an
        explicit override is needed).
    - `PROPOSE-REMOVE` — existing entry no longer on the live mirror.
        `latest upstream` is `—`; `notes` gives the reason
        (e.g. "moved to archive.debian.org", "404 on image_url",
        "codename absent from release index").

    The "files" column tracks where the entry lives (createvm,
    working manifest, example, or some subset) so the edits in
    step 8 cover everything.

    **Show this table to the user before editing.**

7. **Confirm proposals.** For every `PROPOSE-ADD` and `PROPOSE-REMOVE`
    row, ask the user explicitly — one decision per proposal. Do not
    bundle adds and removes together. Sample question shapes:

    - "Add fedora43? `network_version=2`, image at `<url>`, checksums
        at `<url>`, GPG `https://fedoraproject.org/fedora.gpg`.
        os_variant/user derive from the key as `fedora43`/`fedora`. Add
        it to all three files?"
    - "Remove fedora40? It's no longer in
        `https://download.fedoraproject.org/pub/fedora/linux/releases/`
        and the existing `image_url` 404s. Remove from `BUILTIN_IMAGES`
        + both YAMLs and sweep `docs/` for stale references?"

    Only the proposals the user accepts move forward to step 8.

8. **Edit every file listed in the row's "files" column** for
    entries marked `UPDATE` and for accepted
    `PROPOSE-ADD`/`PROPOSE-REMOVE` rows. For `UPDATE`, change only:

    - `image_url` — full URL, including any dated directory segment
    - `checksum_url` — same dated path component must bump in
        lockstep with `image_url`. If they drift, checksum
        verification will load a checksum file for an older build
        and reject the newer image as corrupt. This is the most
        common refresh-bug.

    Leave the other fields untouched on `UPDATE`: `checksum_type`,
    `checksum_url_gpg` (Fedora's `https://fedoraproject.org/fedora.gpg`
    is stable across point releases; never bump it as part of an
    intra-major refresh), `network_version`, and any explicit
    `os_variant` / `username` override that happens to be present (most
    entries omit these and rely on key-based derivation).

    For `PROPOSE-ADD`, fill all fields from the upstream answer in
    step 7 and add the entry in the same three-file shape as
    sibling entries.

    For `PROPOSE-REMOVE`, delete the entry from all three files and
    also sweep `docs-extra/` plus repo-root `README.md` for residual
    references (Walkthrough.md, Why.md, Design.md, CONTRIBUTING.md,
    and any newer pages). Remove or update each reference; surface them in the
    final summary so nothing stale remains for a user to paste from.

    The mirrored YAML entries (in `Lvlab.yml` and
    `docs/Lvlab.example.yml`) for a shared key must match exactly —
    same `image_url`, same `checksum_url`.

9. **Verify**, in this order:

    ```bash
    uv run pytest -q
    uv run pre-commit run --all-files
    ```

    Both must pass. (Optional: `uv run zensical build -s` — cheap,
    ~2s, catches any doc rebuild surprises but not strictly needed
    for a URL-only refresh; recommended when a `PROPOSE-REMOVE`
    swept docs/.)

    Then HEAD-check every URL that changed (including any added
    or removed ones — the removed ones should return non-200 from
    the live mirror, confirming the EOL signal):

    ```bash
    for url in <updated and added urls>; do
      curl -fsIL -o /dev/null -w "%{http_code} %s\n" --max-time 15 "$url" "$url"
    done
    ```

    Every updated/added URL must return 200. If any 404s, the dated
    filename was wrong — re-fetch the listing and correct it before
    reporting done.

10. **Stop. Do not commit.** Print a final summary table (`key |
    old → new | files touched`) including accepted `PROPOSE-ADD` and
    `PROPOSE-REMOVE` rows, and wait for the user to inspect the diff
    and commit themselves. Mention any proposals the user declined
    so they're written down for future runs.

## Per-distro gotchas

- **Debian (intra-major)**: dated dirs are `YYYYMMDD-NNNN` and the
    filename inside *also* encodes that exact stamp — both copies of
    the date in the URL must match.
    `debian-12-generic-amd64-20240717-1811.qcow2` must sit under
    `bookworm/20240717-1811/`. The checksum URL points at
    `SHA512SUMS` inside the same dated directory, so its path bumps
    too. **Debian 11 stays on `network_version: 1`** for the
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
- **Debian (EOL signal)**: a codename's removal from the listing at
    `https://cloud.debian.org/images/cloud/` is the trigger for
    `PROPOSE-REMOVE`. Once moved to `archive.debian.org`, the
    cloud-image URLs we carry will 404 and `lvlab up` would fail at
    download. As of 2026-05-23 the live mirror still carries
    buster/bullseye/bookworm/trixie; that may change.
- **Debian (new-major signal)**: when a new stable codename appears
    in the mirror listing (e.g. `forky`), treat it as a
    `PROPOSE-ADD`. Fields to fill in for `BUILTIN_IMAGES`:
    `network_version=2` (post-bullseye), image URL under
    `<codename>/latest/` (filename `debian-N-generic-amd64.qcow2`),
    checksum URL `SHA512SUMS` in the same directory, no GPG (Debian's
    cloud-image checksums aren't GPG-signed in the same way Fedora's
    are; our entries leave `checksum_url_gpg` unset). `os_variant` and
    `username` derive correctly from the key (`debianN` →
    `debianN`/`debian`), so leave them off the dict.
- **Fedora (intra-major)**: lives under
    `releases/<N>/Cloud/x86_64/images/`. Build filenames look like
    `Fedora-Cloud-Base-Generic-<N>-<date>.x86_64.qcow2`; the
    checksum file is `Fedora-Cloud-<N>-<date>-x86_64-CHECKSUM` in
    the same directory.
- **Fedora (new-major signal)**: the highest numbered directory at
    `https://download.fedoraproject.org/pub/fedora/linux/releases/`
    is the newest release. If we carry `fedoraN` and upstream has
    `fedoraN+M`, surface `fedoraN+M` as `PROPOSE-ADD`. Fields to fill
    in for `BUILTIN_IMAGES`: `network_version=2`,
    `checksum_url_gpg=https://fedoraproject.org/fedora.gpg` (stable
    across releases), plus the image + checksum URLs. `os_variant` and
    `username` derive from the key (`fedoraN+M` → `fedoraN+M`/`fedora`),
    so leave them off the dict.
- **Fedora (EOL signal)**: a release number's removal from the
    release index (it's moved to
    `https://archives.fedoraproject.org/pub/archive/fedora/linux/releases/`)
    is the trigger for `PROPOSE-REMOVE`. The fedora40 drop on
    2026-05-23 was the canonical example — `fedora40`'s `image_url`
    started returning 404 HTML before we noticed, because it had
    been moved to archives.
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
`docs/Lvlab.example.yml` is the documented example for new users
(relocated out of `docs/` so the published-site doc-builder doesn't scan it).
Keeping them in lockstep means a user copying the example and
clobbering it with the repo's `Lvlab.yml` (or vice versa) won't see
mysterious image-not-found errors.

The `images:` sections of those two YAML files don't always carry
the same keys — `Lvlab.example.yml` is richer for illustration
purposes (debian10, debian11, custom intranet entries). The diff
table's "files" column captures which keys live in which files;
treat them per-file rather than assuming a single union.
