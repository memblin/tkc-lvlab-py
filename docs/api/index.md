# API Reference

Auto-generated from Google-style docstrings + type hints in the source tree
via [mkdocstrings](https://mkdocstrings.github.io/).

## Modules with migrated docs

### Top-level

- [`tkc_lvlab._logging`](logging.md) — centralized project logger configuration.
- [`tkc_lvlab.config`](config.md) — manifest loading + `/etc/hosts` rendering.

### `tkc_lvlab.utils`

- [`tkc_lvlab.utils.virsh`](utils/virsh.md) — `virsh` subprocess wrapper, `VirshError`, lifecycle/snapshot/capabilities helpers.
- [`tkc_lvlab.utils.ssh_keys`](utils/ssh_keys.md) — SSH public-key discovery + validation.
- [`tkc_lvlab.utils.passwords`](utils/passwords.md) — password phrase generator + SHA-512-crypt hashing.
- [`tkc_lvlab.utils.requirements`](utils/requirements.md) — `createvm` host-binary dependency check.
- [`tkc_lvlab.utils.network`](utils/network.md) — libvirt network introspection + static-IP validation.
- [`tkc_lvlab.utils.standalone_cloud_init`](utils/standalone_cloud_init.md) — cloud-init artifacts for the standalone workflow.
- [`tkc_lvlab.utils.snapshot_cleanup`](utils/snapshot_cleanup.md) — snapshot deletion + `undefine` fallback.
- [`tkc_lvlab.utils.vdisk`](utils/vdisk.md) — per-VM qcow2 creation for the manifest workflow.

### `tkc_lvlab.scripts`

- [`tkc_lvlab.scripts.createvm`](scripts/createvm.md) — standalone one-off VM creation.
- [`tkc_lvlab.scripts.destroyvm`](scripts/destroyvm.md) — standalone one-off VM removal.

## Modules pending migration

These exist in the source tree but their docstrings/type hints have not
yet been brought up to the new convention. They are excluded from this
reference until the legacy-docstring conversion lands (see `TODO.md`
Phase 7).

- `tkc_lvlab.cli`
- `tkc_lvlab.utils.cloud_init` *(the manifest workflow's `UserData` /
    `MetaData` / `NetworkConfig` / `CloudInitIso` — see
    [`tkc_lvlab.utils.standalone_cloud_init`](utils/standalone_cloud_init.md)
    for the post-convention sibling.)*
- `tkc_lvlab.utils.images`
- `tkc_lvlab.utils.libvirt` *(partially migrated during Phase 2;
    `Machine` methods that were ported to `virsh` carry the new
    convention, the rest predate it.)*
