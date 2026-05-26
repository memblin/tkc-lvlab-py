# Walkthrough

This document describes what each `lvlab` command does, the side effects it
has on your hypervisor, and the bits worth knowing before you reach for it.
Commands are ordered roughly by lifecycle — provision, inspect, iterate,
tear down, maintain.

The CLI shells out to `virsh` for hypervisor operations and to `qemu-img` /
`virt-install` for disk and domain creation. There is no `libvirt-python`
C-extension dependency. Lab functionality requires the `virsh` binary at
runtime (Debian/Ubuntu: `libvirt-clients`; Fedora/RHEL: `libvirt-client`).

The libvirt URI is configurable per-environment in `Lvlab.yml`
(`environment[0].libvirt_uri`); the project's example uses `qemu:///system`,
but `qemu:///session` works as well.

> For single-VM, no-manifest use, see [One-off VMs](one-off-vms.md)
> (`createvm` / `deletevm`).

## Manifest

Every command starts by reading `Lvlab.yml` from the current working
directory. See [the example manifest page](example-manifest.md) for a worked
example, or grab the raw file at [`Lvlab.example.yml`](Lvlab.example.yml).

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

## init

Initialize cloud images. With an `Lvlab.yml` in the current directory,
`init` downloads the images its `images:` section names. **With no
manifest, `lvlab init` initializes the built-in default catalog** — so a
bare `lvlab init` works without writing a manifest first. This is the
single image-init path; `createvm --init-cloud-images` is deprecated in
favour of it.

```bash
# Manifest images (cwd Lvlab.yml), or the built-in defaults if there's no manifest
lvlab init
```

For each image referenced under the manifest's `images` block (or each
built-in default when there's no manifest), `init`:

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
no need to duplicate images for multiple environments. Unreferenced
images can be removed with [`lvlab images clean`](#images-clean) (dry-run
by default).

Downloads tolerate flaky mirrors (connect/read timeouts, retry, and
resume via a `.partial` file). If a download still fails — a 404, a
refused connection, or a mirror that simply won't serve a file — `init`
stops with a clear message instead of a traceback and tells you the
exact cache path to drop the file at manually, then re-run. (This also
handles gzip-served sidecars like Fedora's GPG key, which previously
tripped a false "incomplete transfer".)

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

On first create, `up` also generates a **one-time console password**
(the same way `createvm` does), injects only its hash into cloud-init,
and prints the plaintext **once** along with an example SSH command —
a console fallback for before SSH is reachable. SSH keys remain the
primary access path. To use your own password set `cloud_init.passwd`
(a crypt hash) in the manifest; to opt out entirely (key-only VM) set
`cloud_init.password: false`. The password only takes effect at
first-boot cloud-init, so re-running `up` on an existing VM doesn't
change it.

## status

Show the configured environment, every machine in the manifest along
with its current libvirt state, and the cloud images available to you.

```bash
lvlab status
```

Output is two tables (the shared CLI table style — see
[`tkc_lvlab.utils.output`](api/utils/output.md)):

- **Machines** — each manifest VM and its state. Machines not present
    on the hypervisor are reported as `undeployed`; present machines
    show the lowercase `virsh domstate` string (`running`, `shut off`,
    `paused`, `crashed`, etc.). The parenthesized state-reason suffix
    previous releases printed (e.g. `(normal startup from boot)`) was
    dropped in 0.2.x to avoid an N+1 `virsh domstate --reason` call per
    machine.
- **Images** — the built-in default catalog merged with the manifest's
    `images:` (manifest wins on a name collision), so you see *what
    images are available to you*, not just what this manifest names.
    Each row is labelled with its `source` (`manifest` vs `default`)
    and whether the image is already `cached` on disk.

When stdout is piped or redirected the tables render as plain text
(no ANSI), widened so long image URLs aren't clipped.

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
`lvlab up`. See [Cloud-init examples](cloud-init-examples.md) for what each
rendered file looks like.

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

## down

Attempt a graceful shutdown of a running VM.

```bash
lvlab down salt.local
```

Equivalent to `virsh shutdown <domain>` plus a small poll loop. The VM
stays defined; only the running domain is shut off.

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

## images

Cloud-image cache management commands.

### images clean

Remove cloud-image files from the cache that no `images:` entry in the
current `Lvlab.yml` claims.

```bash
# Dry run — show what would be removed (default, nothing is deleted)
lvlab images clean

# Actually remove the unreferenced files
lvlab images clean --force       # --yes and --delete are accepted aliases
```

**Dry-run by default.** Without `--force` (or its aliases `--yes` /
`--delete`), `images clean` only lists the candidates it would remove. Run
with `--force` once you have reviewed the output.

**What is protected:**

- Every image listed in the manifest's `images:` section — whether or not
    any machine currently references it. Protection covers the image qcow2,
    its checksum file, the `.verified` companion written after GPG
    verification, and the GPG keyring.
- Any cache image currently used as a qcow2 backing file by an on-disk VM
    disk (detected via `qemu-img info`). This is a defense-in-depth layer on
    top of the manifest check: it prevents the cache from being cleaned while
    a VM whose manifest entry was removed still has a live disk backed by
    that image.
- Any cache image whose filename appears in a **commented-out line** of
    `Lvlab.yml`. Commenting an `images:` entry out — for example to swap in
    another version of the same OS for a quick test — keeps its cached image,
    so you don't have to re-download it when you uncomment the entry later. A
    filename mentioned anywhere in a comment (including a trailing `# …`
    comment) counts; its checksum / `.verified` / GPG sidecars are protected
    alongside it.

**Sidecar removal:** when a candidate image is removed, its sidecar files
(checksum, `.verified`, GPG keyring) are removed at the same time. You will
not be left with dangling verification artefacts after a cleanup.

**Safety model summary:**

| Scenario                                                   | Behavior                                               |
| ---------------------------------------------------------- | ------------------------------------------------------ |
| No `--force`                                               | Dry run — lists candidates, deletes nothing            |
| `--force`, no lock                                         | Removes all unreferenced files                         |
| `--force`, `prevent_cloud_image_cleanup: true` in manifest | Exits with code 1, nothing removed                     |
| Missing or unparseable `Lvlab.yml`                         | Exits with code 1 — refuses to guess what is protected |

**`prevent_cloud_image_cleanup` lock flag:** setting
`prevent_cloud_image_cleanup: true` in `config_defaults` hard-disables all
deletion, even when `--force` is passed. This is useful on a shared image
server where multiple manifests draw from the same cache and you want to
prevent accidental wipeouts. See [the config reference](#where-things-live-on-disk)
for placement.

**Example output (dry run):**

```
Cloud-image cache: /var/lib/libvirt/images/lvlab/cloud-images
Protected (defined in manifest): /var/lib/libvirt/images/lvlab/cloud-images/debian-12-generic-amd64-20240101-1234.qcow2
Protected (commented out in manifest): /var/lib/libvirt/images/lvlab/cloud-images/debian-11-genericcloud-amd64.qcow2
Would remove: /var/lib/libvirt/images/lvlab/cloud-images/fedora-39-cloud-base.qcow2
  - sidecar: /var/lib/libvirt/images/lvlab/cloud-images/fedora-39-cloud-base.qcow2.Fedora-Cloud-39-1.5-x86_64-CHECKSUM
Dry run: nothing deleted. Re-run with --force to remove the above.
```

## global

Hypervisor-wide commands that are not scoped to a single `Lvlab.yml` machine.

### global show instances

Print a cross-connection overview of every libvirt domain visible from this
workstation.

```bash
lvlab global show instances

# Include an additional remote or custom connection
lvlab global show instances --uri qemu+ssh://builder.example.com/system
```

`global show instances` always enumerates `qemu:///system` **and**
`qemu:///session` — both common local libvirt sockets — so you see rootful
and rootless VMs in one table without naming either explicitly. Pass `--uri`
one or more times to add further connections; the fixed pair always appears
first and duplicates are silently dropped.

**Columns printed:**

| Column           | What it shows                                                                                                                          |
| ---------------- | -------------------------------------------------------------------------------------------------------------------------------------- |
| Name             | Libvirt domain name                                                                                                                    |
| Connection (URI) | The connection the domain was found on                                                                                                 |
| State            | `virsh domstate` string (`running`, `shut off`, `paused`, …)                                                                           |
| vCPUs            | Allocated virtual CPU count                                                                                                            |
| Memory           | Max memory (MiB, or KiB when not a whole-MiB multiple)                                                                                 |
| Autostart        | Whether the domain auto-starts with the host                                                                                           |
| Persistent       | Whether the domain is persistent (survives reboot)                                                                                     |
| In manifest      | Present only when an `Lvlab.yml` is in the current directory — `yes` when the domain matches a manifest entry's `<vm_name>_<env>` name |

The **`In manifest`** column appears automatically when `lvlab global show instances` is run from a directory that contains a readable `Lvlab.yml`. It
compares every domain name against the namespaced domain names the manifest
would create (`<vm_name>_<environment_name>`), so you can see at a glance
which running VMs belong to the current environment. If no `Lvlab.yml` is
present (or it cannot be parsed), the column is omitted rather than showing
an all-`no` column.

**Performance guarantee:** only cheap reads are issued — one
`virsh list --all` plus one `virsh dominfo` per domain. No running guest is
paused, snapshotted, or polled for live CPU/disk/network stats.

**Unreachable connections:** a connection that cannot be reached (missing
socket, permission denied, daemon not running) is printed as a dim note and
skipped; the remaining connections are still shown.

## Network types (`network_type`)

`interfaces.network_type` picks how each guest attaches to the network in
the manifest workflow:

- `network` (default) — virt-install's managed-network form
    (`--network network=<name>,model=virtio,...`). Requires a libvirt
    network (typically `default`).
- `user` — virt-install's user-mode networking
    (`--network user,model=virtio`). No libvirt network needed; the guest
    gets DHCP from virt-install itself. Useful for `qemu:///session` where
    rootless libvirt cannot manage a NAT network.
- `passt` — same shape as `user` but pins the user-mode backend to passt.

Static IPs are not honoured by SLIRP/passt — lvlab rejects the
combination (`interfaces.ip4` plus `network_type: user`/`passt`) at
manifest parse time. DHCP is the only supported configuration under
user-mode.

> **Known limitation:** a manifest user-mode VM currently has no inbound
> port forwarding, so it can't be reached from the host over SSH yet. The
> fix (libvirt `<portForward>` / hostfwd) is tracked together with adding
> user-mode + port-forwarding support to `createvm`.

User-mode networking is **not** a `createvm` option today; it lives only
in the manifest workflow. Worked example (`docs/Lvlab.example.yml`):

```yaml
machines:
  - vm_name: rootless.local
    hostname: rootless
    os: debian13
    interfaces:
      - name: eth0
        network_type: user
```

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

## config_defaults reference

The following `config_defaults` keys are recognized. Set them under
`environment[0].config_defaults` in `Lvlab.yml`.

| Key                           | Type    | Default                         | Description                                                                                                                                                                                                                                   |
| ----------------------------- | ------- | ------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `cloud_image_basedir`         | string  | `/var/lib/libvirt/images/lvlab` | Root directory for the cloud-image cache. The actual cache lands in `<cloud_image_basedir>/cloud-images/` (the `/cloud-images` suffix is appended automatically unless the path already ends in it).                                          |
| `disk_image_basedir`          | string  | same as `cloud_image_basedir`   | Root for per-VM qcow2 disks and rendered cloud-init files. Layout: `<basedir>/<environment_name>/<vm_name>/`.                                                                                                                                 |
| `prevent_cloud_image_cleanup` | boolean | `false`                         | When `true`, `lvlab images clean --force` exits with code 1 and removes nothing. Use on shared image servers or any environment where the cache must not be pruned automatically. Has no effect unless `--force` is passed to `images clean`. |
| `cpu`                         | integer | —                               | Default vCPU count for every machine. Individual machines can override.                                                                                                                                                                       |
| `memory`                      | integer | —                               | Default RAM in MiB for every machine. Individual machines can override.                                                                                                                                                                       |
| `domain`                      | string  | —                               | DNS domain appended to each machine's hostname to form the default FQDN.                                                                                                                                                                      |
| `os`                          | string  | —                               | Default image key (must match an `images:` entry) for machines that do not specify their own `os`.                                                                                                                                            |
| `disks`                       | list    | —                               | Default disk list applied to every machine.                                                                                                                                                                                                   |
| `interfaces`                  | dict    | —                               | Default interface settings (`network`, `network_type`) applied to every machine.                                                                                                                                                              |
| `cloud_init`                  | dict    | —                               | Default cloud-init settings (`user`, `pubkey`, `sudo`, `shell`) for every machine.                                                                                                                                                            |
