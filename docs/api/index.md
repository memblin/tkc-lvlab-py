# API Reference

Auto-generated from Google-style docstrings + type hints in the source tree
via [mkdocstrings](https://mkdocstrings.github.io/).

## Modules with migrated docs

### Top-level

- [`tkc_lvlab._logging`](logging.md) — centralized project logger configuration.
- [`tkc_lvlab.cli`](cli.md) — Click-based `lvlab` console-script entry point.
- [`tkc_lvlab.config`](config.md) — manifest loading + `/etc/hosts` rendering.

### `tkc_lvlab.utils`

- [`tkc_lvlab.utils.virsh`](utils/virsh.md) — `virsh` subprocess wrapper, `VirshError`, lifecycle/snapshot/capabilities helpers.
- [`tkc_lvlab.utils.ssh_keys`](utils/ssh_keys.md) — SSH public-key discovery + validation.
- [`tkc_lvlab.utils.passwords`](utils/passwords.md) — password phrase generator + SHA-512-crypt hashing.
- [`tkc_lvlab.utils.requirements`](utils/requirements.md) — `createvm` host-binary dependency check.
- [`tkc_lvlab.utils.network`](utils/network.md) — libvirt network introspection + static-IP validation.
- [`tkc_lvlab.utils.cloud_init`](utils/cloud_init.md) — manifest-side cloud-init artifacts (`UserData`, `MetaData`, `NetworkConfig`, `CloudInitIso`).
- [`tkc_lvlab.utils.standalone_cloud_init`](utils/standalone_cloud_init.md) — cloud-init artifacts for the standalone workflow.
- [`tkc_lvlab.utils.snapshot_cleanup`](utils/snapshot_cleanup.md) — snapshot deletion + `undefine` fallback.
- [`tkc_lvlab.utils.vdisk`](utils/vdisk.md) — per-VM qcow2 creation for the manifest workflow.
- [`tkc_lvlab.utils.images`](utils/images.md) — cloud-image download + GPG/checksum verification.
- [`tkc_lvlab.utils.libvirt`](utils/libvirt.md) — manifest-side `Machine` class + lookup helper.

### `tkc_lvlab.scripts`

- [`tkc_lvlab.scripts.createvm`](scripts/createvm.md) — standalone one-off VM creation.
- [`tkc_lvlab.scripts.destroyvm`](scripts/destroyvm.md) — standalone one-off VM removal.

Phase 7 (legacy docstring + type-hint conversion) is now complete —
every module in the source tree is rendered above.
