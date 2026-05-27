# One-off VMs: `createvm` and `deletevm`

The `tkc-lvlab` wheel installs two extra console scripts for the
single-VM, no-manifest case: when you want one throwaway VM and don't
want to write an `Lvlab.yml`. They are **faithful ports of the sibling
`lvscripts-py` commands** (`createvm` / `deletevm`) — same positional
arguments, colored output, and operations — adapted for lvlab only in
where images are stored and how the image catalog is sourced.

For manifest-managed VMs, use `lvlab up` / `lvlab destroy` instead (see
the [Walkthrough](walkthrough.md)). The quick mapping:

| You want to…        | Manifest workflow         | One-off workflow            |
| ------------------- | ------------------------- | --------------------------- |
| Create / start a VM | `lvlab up <vm_name>`      | `createvm <name> <distro>`  |
| Remove a VM         | `lvlab destroy <vm_name>` | `deletevm <domain_name>`    |
| Source of truth     | `Lvlab.yml`               | CLI args + built-in catalog |
| Libvirt domain name | `<vm_name>_<env>`         | exactly the name you pass   |

## How they relate to `lvlab`

- `createvm` resolves `<vm_distro>` against its built-in catalog merged
    with the `images:` section of an `Lvlab.yml` in the current directory
    — or one named with `--config <path>` — if present (manifest entries
    win on a name collision). `deletevm` does not read `Lvlab.yml`.
- They share the cloud-image cache
    (`/var/lib/libvirt/images/lvlab/cloud-images`) with `lvlab up`, so
    an image fetched by either path is reused by the other. Per-VM state
    lands under `/var/lib/libvirt/images/lvlab/oneoff/<vm_name>/`.
- `deletevm` acts on the raw libvirt domain name with no `Lvlab.yml`
    translation: a short manifest name like `web01` won't resolve (the
    real domain is `web01_<env>`), but a manifest VM's full
    `<vm_name>_<env>` domain name WILL be removed if you pass it — its
    disks live nested under `<basedir>/<env>/<vm>/`, so they're left
    behind and the undefine is the operative effect. Use `lvlab destroy`
    for manifest VMs.
- They target `qemu:///system`. Rootless `qemu:///session` and user-mode
    networking are not supported by these scripts today — that, plus a fix
    for lvlab's existing user-mode path, is a tracked follow-up.

## createvm

```bash
# Create a one-off VM. The libvirt domain is the raw name you pass.
sudo createvm testvm.local debian12

# Static IP — validated against the network's subnet + DHCP range,
# then rendered into the guest's cloud-init network-config.
sudo createvm testvm.local debian13 --ip4 192.168.122.50

# Pre-download every catalog image (built-ins + any cwd Lvlab.yml).
# DEPRECATED: prefer `lvlab init`, which initializes the built-in
# defaults when there's no Lvlab.yml. This flag still works for now.
sudo createvm --init-cloud-images
```

`createvm <vm_name> <vm_distro>` creates a libvirt domain named exactly
`<vm_name>` — a raw domain name, no prefix. Both arguments are positional
and must be given together.

### Flags

| Flag                     | Purpose                                                                                                                                                                                                                                                                                                |
| ------------------------ | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| `VM_NAME` (positional)   | FQDN / domain name for the VM. Required together with `VM_DISTRO`.                                                                                                                                                                                                                                     |
| `VM_DISTRO` (positional) | Image key, matched case-insensitively against the built-in catalog (`debian11`, `debian12`, `debian13`, `almalinux9`, `almalinux10`, `ubuntu2204`, `ubuntu2404`, `fedora44`) merged with any `images:` in a cwd `Lvlab.yml`. Required together with `VM_NAME`.                                         |
| `--ip4`                  | Optional static IPv4. Accepts `IP` (uses `--network`), `NETWORK,IP`, or a bare `NETWORK` name (DHCP on that network). Validated against the network's subnet AND DHCP range, then rendered into the guest's cloud-init network-config. For DHCP, pass `dhcp` (or `default` / `auto`) or omit the flag. |
| `--netmask`              | CIDR prefix appended to `--ip4` when it lacks one. Default `24`.                                                                                                                                                                                                                                       |
| `--disk-size`            | qcow2 disk size. Default `35G`.                                                                                                                                                                                                                                                                        |
| `--cpu`                  | vCPU count. Default `2`.                                                                                                                                                                                                                                                                               |
| `--memory`               | RAM, optional unit suffix (`2048`, `2G`, `512M`). Default `2048` (MiB).                                                                                                                                                                                                                                |
| `--network`              | libvirt network name. Falls back to the config `default_network`, then the stock NAT `default`.                                                                                                                                                                                                        |
| `--gateway`              | Gateway IP for a static `--ip4` on a **bridge** network. Required (with `--dns`) for a bridge unless a `networks:` entry supplies it; ignored for NAT (self-derived).                                                                                                                                  |
| `--dns`                  | Comma-separated DNS server(s) for a static `--ip4` on a **bridge** network. Required (with `--gateway`) for a bridge unless a `networks:` entry supplies it; ignored for NAT.                                                                                                                          |
| `--search-domain`        | Comma-separated DNS search domain(s). Honored on both NAT and bridge.                                                                                                                                                                                                                                  |
| `--public-key`           | Optional extra SSH public key file (appended after discovered defaults).                                                                                                                                                                                                                               |
| `--init-cloud-images`    | **Deprecated** — prefer `lvlab init` (the single image-init path; it initializes the built-in defaults with no `Lvlab.yml`). Still works: downloads every catalog image that isn't cached. With no positional args, exits after; with them, pre-fetches then creates.                                  |
| `--config`               | Path to a specific `Lvlab.yml` layered on top of the cwd `./Lvlab.yml`, the per-user `~/.Lvlab.yml`, and host-wide `/etc/Lvlab.yml` (see *Host-wide config* below). Its `images:`, `networks:`, and `default_network` win on a clash.                                                                  |
| `--no-color`             | Disable colored output. Also honors the `NO_COLOR` environment variable. Useful on terminals that render ANSI poorly, or to keep captured logs clean.                                                                                                                                                  |
| `--version` / `-V`       | Print the installed `tkc-lvlab` version and exit.                                                                                                                                                                                                                                                      |

`createvm` attaches the guest to a managed libvirt network
(`--network network=<name>,model=virtio`), defaulting to the stock NAT
`default`, with spice graphics on the loopback. With `--ip4` it renders a
static address (plus the NAT gateway as resolver) into the guest's
network-config; without it the guest uses DHCP and `createvm` waits up to
20s for the NAT lease, then prints the discovered SSH target.

### Host-wide config (`/etc/Lvlab.yml`)

A static `--ip4` on a **bridge** network needs an explicit gateway and DNS
(a bridge has no libvirt-managed values to self-derive). Rather than retype
`--gateway`/`--dns` on every run, declare per-network defaults once. `createvm`
reads config from four layers, lowest precedence first — `/etc/Lvlab.yml`
(host-wide), then `~/.Lvlab.yml` (your per-user defaults), then `./Lvlab.yml`
(current directory), then any `--config` path — deep-merged so a higher layer
overrides a single nested field while inheriting the rest. So a per-host bridge
map can live in `/etc`, your personal default network in `~/.Lvlab.yml`, and a
project override in the directory you run from.

```yaml
# /etc/Lvlab.yml — host-wide defaults for every createvm run on this host
# (the same schema works in ~/.Lvlab.yml and a project ./Lvlab.yml)
default_network: vlan10            # used when --network / --ip4 NETWORK is omitted
default_vm_username: labadmin      # first-boot account when an image doesn't pin one
networks:
  vlan10:
    gateway: 100.64.10.1
    dns: [100.64.10.10, 100.64.10.11]
    search: [tkclabs.io]
  vlan20:
    gateway: 100.64.20.1
    dns: [100.64.20.10]
```

With that in place, a static IP on the `vlan10` bridge needs no networking
flags:

```bash
sudo createvm web01.tkclabs.io ubuntu2404 --ip4 vlan10,100.64.10.50
```

Resolution precedence per value:

- **Network name** — `--ip4 NETWORK,IP` → `--network` → config `default_network`
    → the built-in `default`.
- **gateway / dns / search** — the matching flag → the resolved network's
    `networks:` entry → NAT self-derivation → otherwise the "bridge needs
    gateway+dns" error. So a configured bridge just works; an unconfigured one
    still fails clearly.
- **First-boot username** — an explicit per-image `username:` (in an `images:`
    entry) → config `default_vm_username` → the key-derived family name (e.g.
    `debian`, `fedora`). So `default_vm_username` gives every VM one login
    account unless a specific image pins its own.

(`images:` layers the same way — host-wide image keys merge with a project
manifest's, the higher layer winning on a name clash.)

### SSH keys

`createvm` walks the invoking user's `~/.ssh/id_ed25519.pub` and
`~/.ssh/id_rsa.pub`. Under `sudo` it also walks `$SUDO_USER`'s home
(so `sudo createvm ...` picks up your keys, not root's). Validates
each key (Ed25519, RSA, NIST ECDSA, and hardware-backed `sk-` variants
are all accepted), de-duplicates, and writes them to the VM's
`user-data` as `ssh_authorized_keys`. If none are discovered and no
`--public-key` was provided, `createvm` refuses to create the VM —
that's the no-way-to-log-in guard.

### Password

`createvm` generates a memorable 4-word phrase from a curated wordlist
(mixed-case enforced), hashes it via `openssl passwd -6`, and writes
the hash to `user-data` as the first-boot user's password. The
plaintext phrase is printed to stdout on success — copy it before
losing the terminal output.

### Disk strategy

`createvm` always produces a **standalone** qcow2: it `cp`s the cloud
image into the VM directory, then `qemu-img resize`s it to `--disk-size`.
The disk has no dependency on the shared `cloud-images/` cache, so you can
wipe and re-init that cache later without breaking a one-off VM. The trade
is that each VM takes the full image size on disk — the right default for
throwaway one-off VMs, and what the `lvscripts` reference does.

The manifest workflow (`lvlab up`) instead uses backing-file mode
(`qemu-img create -b <cloud_image>`): storage-efficient across many VMs,
but ties each disk to the cached image's lifetime — appropriate when
you've committed to a shared manifest setup.

## deletevm

```bash
sudo deletevm testvm.local                          # tier-1 prompt, then tier-2 if snapshots
sudo deletevm testvm.local --force                  # skip tier-1; tier-2 still fires if snapshots
sudo deletevm testvm.local --force --snapshots-too  # fully non-interactive
```

`deletevm` looks up exactly the libvirt domain name you pass — no
prefixing, no `Lvlab.yml` translation. If no domain of that name is
defined, it errors. Otherwise it force-offs, undefines, and removes the
per-VM storage directory under the one-off root **if one exists**. A
manifest VM passed by its full `<vm>_<env>` domain name is removed too —
its disks live nested elsewhere, so the missing one-off dir is expected
and undefine is the operative effect. (Use `lvlab destroy` for manifest
VMs, which resolves names against the current manifest.)

**Confirmation tiers:** snapshot presence is detected up front (before any
destructive step) so the prompts can branch correctly:

| Flags                     | Tier-1 ("irreversible, all data lost")          | Tier-2 ("snapshots present; remove them?") |
| ------------------------- | ----------------------------------------------- | ------------------------------------------ |
| _(none)_                  | Prompted                                        | Prompted if snapshots exist                |
| `--force`                 | Skipped if **no** snapshots; prompted otherwise | Prompted if snapshots exist                |
| `--force --snapshots-too` | Skipped                                         | Skipped                                    |

The rationale for `--force` not fully suppressing tier-2: deleting
snapshots is an extra-destructive step that `--force` alone does not
consent to. Pass `--force --snapshots-too` only when you are certain the
VM and all its snapshots should be removed without any interactive
confirmation — for example in a scripted teardown.

Storage cleanup runs only on a successful undefine; a failed undefine
leaves the VM directory in place so you can inspect what went wrong.

`deletevm` also accepts `--no-color` (and honors the `NO_COLOR`
environment variable) to disable styled output, matching `createvm` and
`lvlab`.
