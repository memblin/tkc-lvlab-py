# TKC Labs : Libvirt Labs - Walkthrough

This document describes what each `lvlab` command does, the side effects it
has on your hypervisor, and the bits worth knowing before you reach for it.

The CLI shells out to `virsh` for hypervisor operations and to `qemu-img` /
`virt-install` for disk and domain creation. There is no `libvirt-python`
C-extension dependency. Lab functionality requires the `virsh` binary at
runtime (Debian/Ubuntu: `libvirt-clients`; Fedora/RHEL: `libvirt-client`).

The libvirt URI is configurable per-environment in `Lvlab.yml`
(`environment[0].libvirt_uri`); the project's example uses `qemu:///system`,
but `qemu:///session` works as well.

## Manifest

Every command starts by reading `Lvlab.yml` from the current working
directory. See [docs/Lvlab.example.yml](Lvlab.example.yml) for a worked
example.

The libvirt domain name `lvlab` actually uses on the hypervisor is
`<vm_name>_<environment_name>`, not the bare `vm_name`. That namespacing
is what lets multiple lvlab environments coexist on one hypervisor. The
walkthrough below uses `vm_name` for brevity, but on the hypervisor side
you'll see `<vm_name>_<env>`.

## Verbosity

All commands accept `-v` / `-vv` (more info / debug logs) and `-q`
(errors only). Verbosity is set on the `lvlab` group, before the
subcommand:

```bash
lvlab -vv up salt.local
```

## capabilities

Print the hypervisor's raw `capabilities` XML for `qemu:///session`.

```bash
lvlab capabilities
```

This is a thin wrapper over `virsh -c qemu:///session capabilities` —
useful for confirming that `virsh` is reachable from your shell and that
the user is in the `libvirt` group. Not used elsewhere in the application.

## cloudinit

Re-render the cloud-init `meta-data`, `user-data`, and `network-config`
files for one machine from the current `Lvlab.yml`.

```bash
lvlab cloudinit salt.local
```

Useful when debugging cloud-init template rendering — you can inspect the
rendered files in the per-VM config directory under your
`disk_image_basedir`. This does **not** rebuild the `cidata.iso`; if you
need that on disk for a re-deploy, the relevant step happens during
`lvlab up`.

## destroy

Force-stop and undefine a virtual machine.

```bash
lvlab destroy salt.local            # prompts for confirmation
lvlab destroy salt.local --force    # skip the prompt
```

Sequence on a running VM:

1. (Optional) prompt unless `--force` is passed.
1. Delete any snapshots the domain owns (necessary before undefine).
1. `virsh destroy` — force-power off.
1. `virsh undefine` with `--remove-all-storage` semantics, scoped to the
    qcow2 disks the VM owns.

`destroy` only gates file cleanup on a successful undefine — if `virsh undefine` fails, the files stay so the operator can inspect them.

## down

Attempt a graceful shutdown of a running VM.

```bash
lvlab down salt.local
```

Equivalent to `virsh shutdown <domain>` plus a small poll loop. The VM
stays defined; only the running domain is shut off.

## hosts

Render an `/etc/hosts` snippet for every machine in the manifest that
has a static IPv4 address on its first interface.

```bash
# Print snippet to stdout (default — safe, no system changes)
lvlab hosts

# Render as a heredoc you can paste into a shell session
lvlab hosts --heredoc

# Append the snippet to /etc/hosts on this machine — needs root
sudo $(which lvlab) hosts --append
```

The `--append` mode is intended for an ephemeral test machine where it's
OK to mutate `/etc/hosts`; it skips entries that are already present.

The same snippet is also injected into the **guest's** `/etc/hosts` and
`/etc/cloud/templates/hosts.{debian,redhat}.tmpl` automatically at
first-boot via `runcmd`, so VMs come up able to resolve each other by
hostname.

## init

Initialize the environment defined in `Lvlab.yml`.

```bash
lvlab init
```

For each image referenced under the manifest's `images` block, `init`:

1. Creates `cloud_image_basedir/cloud-images` if missing.
1. Downloads the image's `image_url`, `checksum_url`, and (when defined)
    `checksum_url_gpg`.
1. GPG-verifies the checksum file when a `checksum_url_gpg` is set —
    Fedora is the example in the repo; other distros that publish a
    detached signature work too if you wire one up. When verification
    succeeds the verified plaintext is written to `<checksum>.verified`
    and later operations prefer it.
1. Checksum-verifies the downloaded image. Both Fedora's
    `SHA256 (file) = hash` and Debian's `hash  file` formats are
    handled.

Debian images get a per-image-prefix on the checksum filename because
Debian publishes `SHA512SUMS` (same filename across releases). The
prefix prevents a Debian 11 manifest from clobbering a Debian 12
checksum and vice versa.

The cloud-image directory can be shared between environments — there's
no need to duplicate images for multiple environments. Cleanup of the
image cache is currently manual.

### Image Naming

Custom images, and multiple versions of the same OS, need to follow a
naming convention: `<os_variant>-<anything>`.

We split the image's manifest key on the first `-` and pass the
`os_variant` segment to `virt-install` as the `--os-variant` parameter.
That same segment also picks the right `/etc/cloud/templates/hosts.*`
template (Debian vs RHEL family) so `/etc/hosts` changes made during
cloud-init persist.

Valid examples:

- `debian12-CustomImage`
- `debian12-generic-amd64-20240717-1811`
- `fedora40-idM-v0.1.3`

You can list the valid `--os-variant` values for your hypervisor with:

```bash
virt-install --os-variant list

# or directly
osinfo-query os
```

## snapshot

Manage qcow2 snapshots of an existing VM.

```bash
lvlab snapshot list salt.local
lvlab snapshot create salt.local Base
lvlab snapshot create salt.local Base "Description after first boot"
lvlab snapshot delete salt.local Base
lvlab snapshot delete salt.local Base --force
```

Backed by `virsh snapshot-list`, `virsh snapshot-create` (XML handed
off via a tempfile, not stdin), and `virsh snapshot-delete`. On
failure each command raises and reports the underlying `virsh` stderr
rather than swallowing the error.

`delete` prompts by default; pass `--force` to skip the prompt —
useful in `runcmd`-style scripted teardowns.

## ssh-config

Print SSH config snippets you can append to `~/.ssh/config`.

```bash
# Snippet for every machine in the manifest
lvlab ssh-config

# Just one machine
lvlab ssh-config salt.local
```

Output goes to stdout — redirect or append it to `~/.ssh/config`
yourself. No file is mutated.

## status

Show the configured environment, every machine in the manifest along
with its current libvirt state, and the cloud images the manifest
references.

```bash
lvlab status
```

Machines not present on the hypervisor are reported as `undeployed`.
Present machines show the lowercase `virsh domstate` string
(`running`, `shut off`, `paused`, `crashed`, etc.). The
parenthesized state-reason suffix previous releases printed (e.g.
`is the machine is running (normal startup from boot)`) was dropped
in 0.2.x to avoid an N+1 `virsh domstate --reason` call per machine.

## up

Start a virtual machine defined in `Lvlab.yml`.

```bash
lvlab up salt.local
```

If the VM already exists in libvirt:

- ...and it's shut off / crashed: `virsh start <domain>`.
- ...and it's running: no-op, exit clean.

If the VM does not yet exist:

1. Create the primary qcow2 vdisk via `qemu-img create -b <cloud_image>`
    (backing-file mode — fast, low disk usage).
1. Render `meta-data`, `user-data`, and `network-config` from the
    Jinja2 templates in `tkc_lvlab/templates/`.
1. Pack the three files into `cidata.iso` in-process with `pycdlib`
    (no external `genisoimage` dependency).
1. Shell out to `virt-install` to define and launch the domain. The
    `cidata.iso` is attached as a cdrom; cloud-init's NoCloud
    datasource picks it up at first boot.

The `--os-variant` virt-install needs is derived from the image
name's first hyphenated segment (see "Image Naming" above), which
is why custom images must follow that naming convention.

## One-off VMs: `createvm` and `destroyvm`

The same wheel ships two additional console scripts for single-VM,
no-manifest use cases:

- `createvm <vm_name> --distro <key>` creates a libvirt domain named
    `oneoff-<vm_name>` (the prefix is the collision-prevention against
    manifest VMs), provisioned from a built-in cloud image.
- `destroyvm <vm_name>` removes it.

Both scripts are intentionally separate from `lvlab`:

- They do not read `Lvlab.yml`.
- They will not list, start, stop, or destroy manifest VMs. `lvlab   status` reciprocally does not show one-off VMs in its inventory.
- They default to `qemu:///system` (most lab setups). Pass `--uri` to
    override.

### createvm flags

| Flag             | Purpose                                                                                                                                                                                                                     |
| ---------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `--distro`       | Required. Key into the built-in image catalog (`debian12`, `debian13`).                                                                                                                                                     |
| `--memory`       | RAM in MiB. Default 2048.                                                                                                                                                                                                   |
| `--cpu`          | vCPU count. Default 2.                                                                                                                                                                                                      |
| `--disk-size`    | qcow2 disk size. Default `20G`.                                                                                                                                                                                             |
| `--network`      | libvirt network name. Default `default` (the stock NAT). Only consulted with `--network-type network` (the default).                                                                                                        |
| `--network-type` | Attachment mode: `network` (default; managed libvirt network), `user` (SLIRP), or `passt`. Use `user`/`passt` for `qemu:///session` where rootless libvirt cannot manage a NAT network. `--ip4` is rejected with user-mode. |
| `--ip4`          | Optional static IPv4. Accepts `IP` (uses `--network`) or `NETWORK,IP`. Validated against the network's subnet AND DHCP range. Incompatible with `--network-type user` / `--network-type passt`.                             |
| `--public-key`   | Optional extra SSH public key file (appended after discovered defaults).                                                                                                                                                    |
| `--copy`         | Use `cp` + `qemu-img resize` instead of the default backing-file mode. Trades storage efficiency for cloud-image independence.                                                                                              |
| `--uri`          | libvirt connection URI. Default `qemu:///system`.                                                                                                                                                                           |

### Network types (`--network-type` / `interfaces.network_type`)

Both `createvm` and the manifest workflow let you pick how the guest
attaches to the network:

- `network` (default) — virt-install's managed-network form
    (`--network network=<name>,model=virtio,...`). Requires a libvirt
    network (typically `default`). This is the production-style path
    and remains the default for back-compat.
- `user` — virt-install's user-mode networking
    (`--network user,model=virtio`). SLIRP / passt under the hood
    depending on the distro. No libvirt network needed; the guest
    gets DHCP from virt-install itself. Required for
    `qemu:///session` where rootless libvirt cannot manage a NAT
    network.
- `passt` — same shape as `user` but pins the user-mode backend
    to passt (newer, faster on supported distros).

Static IPs are not honoured by SLIRP/passt — lvlab rejects the
combination (`interfaces.ip4` plus `network_type: user`/`passt`) at
manifest parse time, and `createvm` rejects `--ip4` plus
`--network-type user`/`--network-type passt` at the CLI boundary.
DHCP is the only supported configuration under user-mode.

Worked manifest example (`docs/Lvlab.example.yml`):

```yaml
machines:
  - vm_name: rootless.local
    hostname: rootless
    os: debian13
    interfaces:
      - name: eth0
        network_type: user
```

Worked createvm example for `qemu:///session`:

```bash
createvm rootless --distro debian13 --uri qemu:///session \
    --network-type user
```

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

By default, `createvm` uses the lvlab-native backing-file approach
(`qemu-img create -b <cloud_image>`): fast to create, storage-efficient
across many VMs, but ties the VM's disk to the cloud image's lifetime.
Wiping `cloud-images/` would break VMs that depend on it.

`--copy` flips this: `cp` the cloud image to the VM directory, then
`qemu-img resize` to `--disk-size`. The qcow2 is standalone. You can
wipe and re-init `cloud-images/` later without affecting any
`--copy`-created VM. The trade is that each VM takes the full image
size on disk.

The manifest workflow (`lvlab up`) always uses backing-file mode —
the trade-off is appropriate when you've explicitly committed to a
shared manifest setup.

### destroyvm

```bash
sudo destroyvm testvm.local           # prompts
sudo destroyvm testvm.local --force   # skips the prompt
```

`destroyvm` looks up `oneoff-testvm.local` on the libvirt side. If the
prefix isn't present, it errors out — it does NOT fall through to the
bare name. A manifest VM named `testvm.local_<env>` is completely
invisible to `destroyvm`; use `lvlab destroy` for those.

Snapshot fallback: if `virsh undefine` refuses because snapshots
exist, `destroyvm` deletes them (preferring `--children`, falling back
to `--metadata` for backing-chain qcow2 cases) and retries.

Storage cleanup runs only on a successful undefine; a failed undefine
leaves the VM directory in place so you can inspect what went wrong.

## Where things live on disk

Two paths are configurable in the manifest's `config_defaults`:

- `cloud_image_basedir` — where downloaded cloud images and
    checksum/GPG files are cached. Defaults to
    `/var/lib/libvirt/images/lvlab`. Shared across environments.
- `disk_image_basedir` — where per-VM qcow2 disks and rendered
    cloud-init files live. Defaults to the same path. The per-VM
    subdirectory is `<basedir>/<environment_name>/<vm_name>/`.

Both directories must be writable by your user if you want to run
`lvlab` without `sudo`. Pre-create them and `chown` them to your
user up front.
