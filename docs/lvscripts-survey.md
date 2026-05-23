# Phase 5 — lvscripts-py survey

**Status:** Phase 5 deliverable. Read-only inventory and per-feature
disposition for the sibling project at
`/home/tkcadmin/repos/github/memblin/lvscripts-py`. This document is the
input to Phase 6 (standalone `createvm` / `destroyvm` scripts inside
the tkc-lvlab distribution).

## Executive summary

Three lvscripts capabilities are clear ports into Phase 6 of tkc-lvlab:

1. **One-off VM creation** (`createvm`) — the headline Phase 6
    deliverable. lvscripts has a clean reference implementation already
    using Typer + `virsh` + `virt-install`.
1. **SSH key auto-discovery** (`ssh_keys.py`) — walks the invoking
    user's `~/.ssh/{id_ed25519,id_rsa}.pub` plus `$SUDO_USER`'s home,
    validates the key type and base64 body, de-duplicates while
    preserving order. Real UX win.
1. **Network validation** (`libvirt.get_network_info()`) — parses
    `virsh net-dumpxml` for forward mode, gateway, netmask, and DHCP
    range; required to safely accept a `--ip4` flag without colliding
    with the DHCP pool.

The next tier (port + adapt): password phrase generation,
package-manager-aware dependency checks, network forward-mode
gating (NAT vs bridge), DHCP-lease IP discovery polling.

Two lvscripts choices are deliberately **not** worth porting:
`genisoimage` for ISO build (lvlab's in-process `pycdlib` is
superior), and `cp` + `qemu-img resize` for disk creation (lvlab's
`qemu-img create -b` backing-file approach is more storage-efficient).

______________________________________________________________________

## 1. lvscripts public surface

Two console scripts, both Typer-based, both hardcoded to
`qemu:///system`.

### `createvm <vm_name> <vm_distro> [OPTIONS]`

Module: `src/lvscripts/commands/createvm.py`

| Argument              | Type             | Meaning                                                                                                    |
| --------------------- | ---------------- | ---------------------------------------------------------------------------------------------------------- |
| `vm_name`             | positional, FQDN | Libvirt domain name and (default) hostname                                                                 |
| `vm_distro`           | positional, str  | Key into the built-in or YAML-configured image catalog (`debian13`, `ubuntu24`, etc.)                      |
| `--ip4`               | `[NETWORK,]IP`   | Optional static IPv4 (validated against the network's subnet + DHCP range)                                 |
| `--netmask`           | CIDR             | Override the network's netmask                                                                             |
| `--memory`            | int              | RAM in MB                                                                                                  |
| `--cpu`               | int              | vCPU count                                                                                                 |
| `--disk-size`         | str              | qcow2 resize target (e.g. `20G`)                                                                           |
| `--network`           | str              | libvirt network name; defaults to `libvirt.default_network` from YAML or `default`                         |
| `--public-key`        | path             | Additional SSH public key file (appended after discovered keys)                                            |
| `--init-cloud-images` | flag             | Bootstrap mode: download every cataloged cloud image into `/var/lib/libvirt/images/cloud-images/` and exit |
| `--config`            | path             | Bypass discovery; force a specific `lvscripts.{yaml,yml}` file                                             |

Workflow:

1. Load config (`lvscripts.{yaml,yml}` discovery order: `./`, `~/.config/lvscripts/`, `/etc/`).
1. Validate required host binaries (`virsh`, `qemu-img`, `virt-install`, `cp`, `openssl`, `genisoimage`/`mkisofs`).
1. Resolve network info via `virsh net-dumpxml`. NAT networks derive gateway + DNS from the network XML; bridge networks require `libvirt.default_dns` + `libvirt.default_gateway` in YAML.
1. Validate `--ip4` against subnet + DHCP range (rejects in-range overlap).
1. Discover SSH keys (current home + `SUDO_USER` home; dedupe).
1. Generate a 4-word password phrase + SHA-512-crypt hash via `openssl passwd -6`.
1. Render `meta-data`, `user-data`, `network-config` (v2 for everything except `debian11`, which uses v1 with `dhcp6` intentionally omitted to avoid the 5-min `networking.service` hang).
1. `cp` the base cloud image to `/var/lib/libvirt/images/<vm_name>/` (full file duplicate, not backing file).
1. `qemu-img resize` the copy to `--disk-size`.
1. Build `cidata.iso` via `genisoimage`/`mkisofs` subprocess.
1. `virt-install` to define and launch the domain.
1. For NAT networks: poll `virsh net-dhcp-leases` for ≤ 20 s, prefer MAC-match (avoids stale-hostname leases).
1. Print the generated password and a ready-to-paste `ssh user@ip` command.

Cleanup-on-failure: any provisioning error `rmtree`'s the per-VM
directory before raising.

### `deletevm <vm_name> [--force]`

Module: `src/lvscripts/commands/deletevm.py`

- `virsh destroy <name>` → `virsh undefine <name>`.
- **Snapshot fallback:** if `undefine` fails because snapshots exist,
    offers to delete them with `--children` (or `--metadata` as a
    fallback) and retries.
- Final `rmtree` of `/var/lib/libvirt/images/<vm_name>/`.

______________________________________________________________________

## 2. lvscripts module inventory

| Module            | Purpose                                                                                                                                       |
| ----------------- | --------------------------------------------------------------------------------------------------------------------------------------------- |
| `config.py`       | YAML discovery + normalization. `BUILTIN_IMAGES` catalog. `normalize_distro` collapses casing/punct.                                          |
| `libvirt.py`      | `virsh_command()` / `virt_install_command()` / `run()` — always inject `--connect qemu:///system`. `get_network_info()` parses `net-dumpxml`. |
| `cloud_init.py`   | Renders `meta-data` / `user-data` / `network-config`. Supports user-supplied templates with `{placeholder}` substitution.                     |
| `cloud_images.py` | Downloads any missing images in the catalog (triggered by `createvm --init-cloud-images`).                                                    |
| `ssh_keys.py`     | Key discovery (incl. `SUDO_USER`), key-type whitelist (rsa, ed25519, three ECDSA curves, two hardware sk-types), base64 validation, dedupe.   |
| `passwords.py`    | 4-word phrase from an 80-entry wordlist with mixed-case enforcement; SHA-512-crypt via `openssl passwd -6` (configurable rounds).             |
| `requirements.py` | Validates required host binaries; emits package-manager-aware install hints (`apt`/`dnf`/`zypper`/`pacman`) detected from `/etc/os-release`.  |

Pylint design limits are deliberately loose (`max-args=11`,
`max-locals=35`, `max-statements=80`) to accommodate the
orchestrator-style command functions.

______________________________________________________________________

## 3. Functional overlap with tkc-lvlab (post-Phase-2)

| Capability                | lvscripts                                                                                                      | tkc-lvlab (current)                                                                                                    | Judgment                                                                                                                                                           |
| ------------------------- | -------------------------------------------------------------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| Cloud-image download      | `urllib` + Rich progress bar                                                                                   | `requests.get(stream=True)` + `tqdm`                                                                                   | Either fine; no port needed.                                                                                                                                       |
| Cloud-image verification  | **None** (downloads, doesn't checksum or GPG-verify)                                                           | Full GPG verification + SHA256/SHA512 checksum (Fedora/Debian formats, Debian-multi-version aware)                     | **lvlab superior — keep**                                                                                                                                          |
| Cloud-init ISO build      | `genisoimage`/`mkisofs` subprocess                                                                             | `pycdlib` in-process (no external dependency)                                                                          | **lvlab superior — do not port lvscripts approach**                                                                                                                |
| qcow2 disk creation       | `cp` (full duplicate) + `qemu-img resize`                                                                      | `qemu-img create -b <backing>` (copy-on-write backing file)                                                            | **lvlab superior (storage efficiency)**                                                                                                                            |
| `virt-install` invocation | Direct subprocess via `virt_install_command()` helper                                                          | Direct subprocess; URI from `environment.libvirt_uri` (not hardcoded)                                                  | Comparable. lvlab is more flexible (URI configurable per environment).                                                                                             |
| Libvirt lifecycle         | `virsh` subprocess via `libvirt.run()` wrapper                                                                 | `virsh` subprocess via `tkc_lvlab.utils.virsh.run_virsh()` + helpers (Phase 2)                                         | **Both projects now use virsh.** lvlab's wrapper is more robust: custom `VirshError`, `LC_ALL=C` locale lock, helper functions for `domstate`/`snapshot-list`/etc. |
| SSH key handling          | Whitelist of 7 key types (rsa, ed25519, 3× ECDSA, 2× sk-) + base64 validation + dedupe + `SUDO_USER` discovery | `UserData._is_valid_ssh_public_key` recognizes only rsa, dss, ed25519; no discovery, manifest provides path explicitly | **lvscripts richer — worth porting**                                                                                                                               |
| Password generation       | 4-word phrase, mixed-case, SHA-512-crypt via `openssl passwd -6`                                               | Not present                                                                                                            | **lvscripts only — worth porting for one-offs**                                                                                                                    |
| Network validation        | `virsh net-dumpxml` → subnet, gateway, DHCP range; `--ip4` rejected if in-range overlap                        | Not present                                                                                                            | **lvscripts only — required for safe `--ip4` on one-offs**                                                                                                         |
| DHCP lease polling        | `virsh net-dhcp-leases` for ≤ 20 s, MAC-preferred                                                              | Not present (lvlab uses static IPs from the manifest)                                                                  | **lvscripts only — optional for one-offs**                                                                                                                         |
| Dependency precheck       | Validates 6 binaries; emits package-manager-specific install hint                                              | Not present                                                                                                            | **lvscripts only — worth porting as CLI-startup sanity check**                                                                                                     |

______________________________________________________________________

## 4. lvscripts capabilities tkc-lvlab does not have

### One-off VM creation

The headline Phase 6 feature: create a VM without an `Lvlab.yml`
manifest. lvscripts already has a working implementation. Per the
[Phase 6 architecture decision](https://github.com/memblin/tkc-lvlab-py/blob/main/TODO.md#phase-6),
lvlab will expose this as a **separate console script** (`createvm`,
`destroyvm`), NOT as `lvlab vm create`, with the explicit constraint
that the standalone scripts do not read `Lvlab.yml` and do not see
manifest-managed VMs.

### SSH key auto-discovery

`ssh_keys.discover_default_public_keys()` walks:

1. `Path.home()/.ssh/` — current effective user (root under `sudo`).
1. `$SUDO_USER`'s `~/.ssh/` (so `sudo createvm ...` picks up the
    invoking user's keys, not root's).
1. `$HOME`'s `~/.ssh/`.

For each, tries `id_ed25519.pub` then `id_rsa.pub`. Validates type,
base64-decodes the body, dedupes preserving order. Currently lvlab's
`UserData.__post_init__` instead reads a single key from a path
literal in the manifest's `cloud_init.pubkey` field — no discovery.

### Password generation

`generate_password_phrase()` produces a memorable 4-word phrase from
an 80-word nature-themed wordlist, with mixed-case enforced per
word. `hash_password_sha512()` runs `openssl passwd -6` with
configurable rounds (default 4096). For one-off VMs this gives the
user a usable console password they can paste, while keeping the
hash on disk in `user-data`.

### Network validation

`get_network_info()` parses `virsh net-dumpxml` for forward mode,
gateway, netmask, and DHCP range. `_validate_static_ip` rejects IPs
outside the subnet or inside the DHCP range. Bridge networks
require explicit `libvirt.default_dns` and `libvirt.default_gateway`
in YAML or `createvm` refuses to run.

### DHCP lease polling

`_poll_dhcp_lease()` polls `virsh net-dhcp-leases` after
`virt-install` returns, for up to 20 s. **MAC-preferred matching**
(avoids stale-hostname leases that bite in repeated test cycles).
Returns the IPv4 + CIDR, then the script prints a copy-pasteable
`ssh user@ip` command.

### Dependency precheck with package hints

`resolve_createvm_tooling()` validates 6 binaries up front. On
failure, `_build_dependency_message()` reads `/etc/os-release`,
classifies the OS family as apt/dnf/zypper/pacman/unknown, and
emits the exact `sudo <pm> install ...` command. tkc-lvlab does
not check up front — failures surface only when the missing
binary is invoked.

______________________________________________________________________

## 5. Per-feature disposition

For each lvscripts capability, the recommended action for Phase 6
of tkc-lvlab. **Port** = lift into lvlab; **adapt** = port with
modifications; **skip** = explicit no, lvlab keeps its own;
**leave to lvscripts** = useful but out of lvlab's scope.

| Feature                             | Disposition            | Rationale                                                                                                                                                                                                                                                                                              |
| ----------------------------------- | ---------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| One-off VM creation (`createvm`)    | **Port**               | Phase 6 deliverable. Adapt to lvlab's existing library API (`Machine`, `CloudImage`, `VirtualDisk`, `CloudInitIso`) rather than duplicating logic.                                                                                                                                                     |
| SSH key discovery                   | **Port + adapt**       | Lift `ssh_keys.py` largely verbatim. Apply to one-offs AND the manifest workflow (treat `cloud_init.pubkey: auto` as a sentinel that triggers discovery).                                                                                                                                              |
| SSH key-type whitelist              | **Adapt**              | lvscripts accepts ed25519, rsa, 3 ECDSA curves, 2 hardware sk-types. lvlab's current `_is_valid_ssh_public_key` accepts only rsa/dss/ed25519 and rejects hardware-backed keys. Broaden lvlab's validator. Phase 7 work can replace the regex approach with the explicit whitelist + base64 validation. |
| Password phrase + hash              | **Port + adapt**       | Useful for one-offs; expose as a library function so a future `lvlab` subcommand could use it. Default-off for manifest VMs to avoid surprising long-time users.                                                                                                                                       |
| Network validation                  | **Port**               | Required for safe `--ip4` on one-offs. lvlab's `Machine` deploy path could use it too — currently lvlab trusts the manifest's IP literally.                                                                                                                                                            |
| Forward-mode policy (NAT vs bridge) | **Port**               | The "bridge networks require explicit DNS/gateway in config" gate is a real safety property worth preserving. Apply to one-offs only; manifest workflow is too opinionated to retrofit.                                                                                                                |
| DHCP lease polling                  | **Port (optional)**    | Nice UX for one-offs (print ssh-able address after creation). Make timeout configurable rather than hardcoding 20 s. Skip for manifest workflow (manifest VMs use static IPs).                                                                                                                         |
| Dependency precheck                 | **Port**               | Wire into the `createvm` / `destroyvm` startup. Optional for `lvlab` itself (defer to the failure site there for now). Package-manager detection by `/etc/os-release` can be lifted verbatim.                                                                                                          |
| Cloud-image init bootstrap          | **Adapt**              | lvscripts has `createvm --init-cloud-images`. lvlab already has `lvlab init` for the manifest case; for one-offs, the standalone `createvm` should auto-init the cataloged image on first use rather than requiring a separate command.                                                                |
| Built-in image catalog              | **Port**               | lvscripts' `BUILTIN_IMAGES` provides distro keys (`debian13`, `ubuntu24`, etc.) without YAML. The standalone scripts need this; the manifest workflow keeps `Lvlab.yml`.                                                                                                                               |
| Config discovery pattern            | **Adapt**              | `_default_search_directories()` with a `--config` override is a clean testable seam. Use the same shape for any standalone-script YAML config (separate from `Lvlab.yml`).                                                                                                                             |
| ISO build with `genisoimage`        | **Skip**               | lvlab's `pycdlib` is in-process, no external binary required, and works on every distro without package-manager hints. Phase 6 standalone scripts use `pycdlib` too.                                                                                                                                   |
| qcow2 via `cp` + `qemu-img resize`  | **Skip**               | Backing-file (`qemu-img create -b`) is more storage-efficient and faster on first boot. Phase 6 standalone scripts use the lvlab `VirtualDisk` class.                                                                                                                                                  |
| Hardcoded `qemu:///system`          | **Adapt**              | Phase 6 standalone scripts can default to `qemu:///system` (matches lvscripts intent) but should accept `--uri` override so they're usable on `qemu:///session` too.                                                                                                                                   |
| Hardcoded `/var/lib/libvirt/images` | **Adapt**              | Honor a `--basedir` flag (or env var) for standalone scripts, defaulting to `/var/lib/libvirt/images/oneoff/<name>` so they can't collide with lvlab-managed paths.                                                                                                                                    |
| Snapshot fallback in `deletevm`     | **Port**               | The "if undefine fails because of snapshots, try `--children` then `--metadata` and retry" pattern is more robust than what `Machine.destroy` does today. Lift it into a shared library helper.                                                                                                        |
| Subprocess wrapper                  | **Leave to lvscripts** | Both projects now have one. lvlab's is more robust (custom `VirshError`, `LC_ALL=C`, typed helpers). Going forward, lvscripts could adopt lvlab's pattern — opposite direction from a year ago.                                                                                                        |

______________________________________________________________________

## 6. Patterns worth borrowing for lvlab (beyond Phase 6 ports)

- **`_default_search_directories()` seam** (`config.py`) — testable
    config-discovery pattern that lets tests patch the search path.
    Worth adopting if lvlab ever grows a user-level config file.
- **Typed dataclasses for resolved state** (`LibvirtNetworkInfo`,
    `CreateVmTooling`) — keeps orchestrator functions readable when
    the call graph passes a lot of resolved state around. Phase 6's
    `createvm` will benefit from the same shape.
- **Custom exception per module** (`ConfigError`, `CloudInitError`,
    `LibvirtNetworkError`, `DependencyError`, `PasswordHashError`,
    `PublicKeyError`) — already used in both repos. Keep extending it
    in lvlab as new failure modes appear (e.g. `VirshError` already
    follows the pattern).
- **Pylint design limits scoped to orchestrator complexity** — lvscripts
    raised `max-args=11`, `max-locals=35`, `max-statements=80` for its
    command functions only. lvlab's `cli.py` is heading the same
    direction (long subcommand bodies); the right answer is the
    "move logic to library classes" rule already in CLAUDE.md, not
    raising lint limits.
- **Typer migration is a working reference for Phase 9.** lvscripts is
    already on Typer (`createvm = "lvscripts.commands.createvm:app"`).
    When Phase 9 starts, lvscripts' `commands/createvm.py` shows how
    Typer's argument typing, default values, and callback semantics
    look on a non-trivial command. Worth re-reading at the start of
    Phase 9.

______________________________________________________________________

## 7. Phase 6 design questions — refreshed

The /tmp inventory's open-questions list pre-dated the Phase 6
architecture decision. Current state:

1. **One-off VM namespacing** — _Resolved_. Per
    [project-phase6-architecture](https://github.com/memblin/tkc-lvlab-py/blob/main/TODO.md#phase-6)
    memory: `createvm` and `destroyvm` are **separate console scripts**
    (not `lvlab` subcommands), they do **not** read `Lvlab.yml`, and
    they do **not** interact with lvlab-managed VMs. The previous
    `_oneoff` sentinel-environment idea is moot — there's no environment
    to name when there's no `Lvlab.yml`. Open sub-question still:
    bare name vs `oneoff-<name>` prefix (recommended) vs distinct URI
    routing.
1. **SSH discovery scope** — Recommend applying to both surfaces, with
    the manifest workflow's `cloud_init.pubkey: auto` sentinel opting
    into discovery (existing path-literal usage stays the default).
1. **Password policy** — Auto-generate for `createvm` (lvscripts
    default behavior); skip for manifest VMs unless explicitly
    enabled in `cloud_init`.
1. **Network URI for one-offs** — Default `qemu:///system` (lvscripts
    intent), with a `--uri` flag for `qemu:///session` overrides.
1. **Dependency strictness** — Fail fast with hints in `createvm` /
    `destroyvm` (lvscripts pattern). Permissive everywhere else.

______________________________________________________________________

## 8. Risk flags — do not port verbatim

- **Root assumption.** lvscripts hardcodes `qemu:///system`, which
    requires root or `libvirt` group. Phase 6 standalone scripts should
    accept a `--uri` flag so the same binary works on `qemu:///session`
    without root. lvlab itself must continue to take the URI from the
    manifest.
- **Storage path hardcoding.** lvscripts writes to
    `/var/lib/libvirt/images/<vm_name>/` unconditionally. The Phase 6
    standalone scripts must namespace their writes
    (`/var/lib/libvirt/images/oneoff/<name>/` is the suggestion) so
    they cannot accidentally collide with lvlab manifest VMs that
    share the same default `disk_image_basedir`.
- **No image verification on lvscripts side.** lvscripts downloads
    cloud images without GPG or checksum validation. Phase 6 must
    use lvlab's `CloudImage.gpg_verify_checksum_file()` /
    `CloudImage.checksum_verify_image()` path. Do not port lvscripts'
    download logic; use lvlab's.
- **Lease polling timeout hardcoded.** lvscripts polls
    `virsh net-dhcp-leases` for 20 s. On busy hypervisors this can be
    too short; on quick boots it's wasted wall time. Expose a
    `--lease-timeout` flag if porting (default 30 s).
- **`debian11`-only v1 network config.** lvscripts intentionally omits
    `dhcp6` from the v1 template to dodge a 5-min ifupdown hang.
    lvlab's `network-config.v1.j2` template currently does emit
    `dhcp6: true` in some configurations. If Phase 6 starts producing
    Debian 11 VMs, the v1 template needs the same workaround.

______________________________________________________________________

## 9. Cross-reference

- Phase 6 architecture lock-ins:
    [project-phase6-architecture](../TODO.md#phase-6) — read first
    before implementing.
- Phase 9 (Click → Typer): lvscripts is already on Typer. Use
    `src/lvscripts/commands/createvm.py` as a working reference for
    the migration shape.
- TODO follow-up for Phase 7: the SSH key validator broadening (rsa /
    dss / ed25519 → 7 key types) is a real behavior change. Either
    bundle it into Phase 6 (since one-offs use it first) or
    Phase 7 (since it's a function-level rewrite).
