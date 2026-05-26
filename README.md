# TKC Labs : Libvirt Labs

The Libvirt Labs project provides the `lvlab` Python application which can be
used to manage Libvirt based development environments in a familiar way.

If you are wondering why I would write this, the long [answer is here](docs/why.md).

> [!NOTE]
> `lvlab` is built for a developer workstation — driving short-lived
> lab VMs for testing configuration-management code. It is not a
> production VM manager. Destructive commands (`destroy`, `down`,
> snapshot `delete`) act on whatever libvirt domains match the
> manifest, so back up anything you can't afford to lose before
> pointing it at a host that also runs VMs you care about.

## Requirements

- Libvirt w/ QEMU configured and functional.
- The user should be a member of the `libvirt` group on the system.
- Utilities: `virsh`, `virt-install`, `qemu-img`. `lvlab` shells out
    to all three; there is no longer a `libvirt-python` C-extension
    dependency, so `libvirt-dev` / `pkg-config` are not needed at
    build time.
- `cloud_image_basedir` and `disk_image_basedir` configuration paths
    need to be writable by the user to run w/o sudo. I usually create
    these directories in advance and chown them for my user.

Validated end-to-end on **Debian 12** (bookworm), **Debian 13**
(trixie), **AlmaLinux 10**, and **Fedora 44**. See
[`scripts/host-bootstrap.sh`](scripts/host-bootstrap.sh) for the
exact apt/dnf package list per supported host.

## Installation

- Install the latest release wheel as an isolated tool with [uv](https://docs.astral.sh/uv/):

```bash
# Install uv if you don't already have it
curl -LsSf https://astral.sh/uv/install.sh | sh

# Install the release wheel directly
uv tool install https://github.com/memblin/tkc-lvlab-py/releases/download/0.3.0/tkc_lvlab-0.3.0-py3-none-any.whl

# lvlab should be ready for use
lvlab up salt.local

# Show the VM w/ virsh
virsh -c qemu:///system list
```

- Option B: Clone this repo, build a wheel with `uv build`, and `uv tool install ./dist/tkc_lvlab-*.whl`.

- Option C (for active development): Clone, then `uv sync` to create a dev env and `uv run lvlab --help` to invoke the CLI from a checkout.

The same wheel installs three console scripts on your PATH:

- `lvlab` — manifest workflow (drives `Lvlab.yml`).
- `createvm` — one-off VM creation. Does not read `Lvlab.yml`.
- `deletevm` — one-off VM removal by raw libvirt domain name (matches `createvm`). Use `lvlab destroy` for manifest VMs.

## Usage

With `lvlab` one can create VMs automatically from a YAML syntax manifest
configuration that defines the environment, the virtual machines, and
the base image information.

- [Example YAML config file](docs/Lvlab.example.yml)
- [In-depth Example Repo](https://github.com/memblin/lvlab-examples)

## One-off VMs

When you want a single VM without writing an `Lvlab.yml` manifest, use
`createvm` / `deletevm` — faithful ports of the `lvscripts-py`
`createvm` / `deletevm` commands. `createvm` resolves its positional
`VM_DISTRO` against a built-in image catalog merged with the `images:`
section of an `Lvlab.yml` in the current directory (if present), and
shares the cloud-image cache with `lvlab up`. Both target
`qemu:///system` and use raw libvirt domain names. (`lvlab destroy <vm>`
is the manifest-scoped deleter; `deletevm` is the raw-name one.)

```bash
# Create a one-off VM. The libvirt domain is the raw name you pass.
# VM_NAME and VM_DISTRO are positional.
sudo createvm testvm.local debian12

# Same, but with a static IP (validated against the network's subnet +
# DHCP range, then rendered into the guest's network-config).
sudo createvm testvm.local debian13 --ip4 192.168.122.50

# Pre-download every catalog image (built-ins + any cwd Lvlab.yml).
sudo createvm --init-cloud-images

# Delete a VM by its raw libvirt domain name (destroy + undefine +
# remove the one-off dir if present). No Lvlab.yml translation, so a
# short manifest name won't resolve — but a full <vm>_<env> domain will.
sudo deletevm testvm.local --force
```

See `createvm --help` and `deletevm --help` for the full flag list.

## Help Output

```console
Usage: lvlab [OPTIONS] COMMAND [ARGS]...

  A command-line tool for managing VMs.

Options:
  -v, --verbose  Increase log verbosity (-v for INFO, -vv for DEBUG).
  -q, --quiet    Suppress informational logs (ERROR only). Overrides -v.
  -h, --help     Show this message and exit.

Commands:
  capabilities  Print the raw hypervisor capabilities XML for qemu:///session.
  cloudinit     Render cloud-init files for a manifest VM without starting it.
  destroy       Destroy a manifest VM: force-off, undefine, remove files.
  down          Gracefully shut down a manifest VM.
  hosts         Render a /etc/hosts snippet for the manifest's machines.
  ssh-config    Print ~/.ssh/config snippet(s) for machines in the manifest.
  init          Initialize the environment: download and verify cloud images.
  status        Show the status of the environment.
  smoke         Boot every manifest VM, SSH-verify it, then tear it down (manual only).
  up            Start a machine defined in the Lvlab.yml manifest.
  snapshot      Snapshot management commands.
  global        Hypervisor-wide commands not scoped to a single Lvlab.yml machine.
  images        Cloud-image cache management commands.
```

Verbosity flags go on the `lvlab` group, before the subcommand
(e.g. `lvlab -vv up salt.local`). See the
[Walkthrough](docs/walkthrough.md) for what each command does.
