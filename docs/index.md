# tkc-lvlab

A Typer-based CLI for managing local libvirt+QEMU lab VMs from a single
declarative YAML manifest (`Lvlab.yml`). Built for end-to-end integration
testing of configuration-management code (Salt, Ansible, etc.) on a developer
workstation — not for production VM management.

> **Not a production VM manager.** Destructive commands (`destroy`, `down`,
> snapshot `delete`) act on whatever libvirt domains match the manifest. Back
> up anything you can't afford to lose before pointing `lvlab` at a host that
> also runs VMs you care about.

## What it does

`lvlab init` followed by `lvlab up <vm_name>` will:

1. Download and verify a cloud image (checksum + optional GPG of the checksum file).
1. Create a qcow2 disk that uses the cloud image as a backing file (via `qemu-img`).
1. Render cloud-init `meta-data`, `user-data`, and `network-config` from Jinja2
    templates, pack them into a `cidata.iso` (built in-process with `pycdlib`),
    and attach it as a cdrom.
1. Shell out to `virt-install` to define and launch the domain.

## Requirements

- Libvirt with QEMU configured and functional.
- Your user is a member of the `libvirt` group.
- The `virsh`, `virt-install`, and `qemu-img` binaries on `PATH`. On
    Debian/Ubuntu these come from `libvirt-clients`, `virtinst`, and
    `qemu-utils`; on Fedora/RHEL from `libvirt-client`, `virt-install`, and
    `qemu-img`. There is no `libvirt-python` C-extension dependency, so
    `libvirt-dev` / `pkg-config` are not needed.
- `cloud_image_basedir` and `disk_image_basedir` must be writable by your user
    to run without `sudo` — pre-create and `chown` them.

Validated end-to-end on **Debian 12** (bookworm), **Debian 13** (trixie),
**AlmaLinux 10**, and **Fedora 44**.

## Install

`tkc-lvlab` is distributed as a GitHub release wheel (it is not on PyPI).
Install the latest release as an isolated tool with [uv](https://docs.astral.sh/uv/):

```bash
# Install uv if you don't already have it
curl -LsSf https://astral.sh/uv/install.sh | sh

# Install the release wheel directly — grab the URL for the latest version
# from https://github.com/memblin/tkc-lvlab-py/releases
uv tool install https://github.com/memblin/tkc-lvlab-py/releases/download/0.4.0/tkc_lvlab-0.4.0-py3-none-any.whl
```

The wheel installs three console scripts on your `PATH`: `lvlab` (manifest
workflow), plus `createvm` / `deletevm` for [one-off VMs](one-off-vms.md).
To build from a checkout instead, clone the repo and run `uv build` then
`uv tool install ./dist/tkc_lvlab-*.whl`.

## Quickstart

```bash
# 1. Drop a manifest in the current directory (start from the example).
curl -fsSL https://raw.githubusercontent.com/memblin/tkc-lvlab-py/main/docs/Lvlab.example.yml -o Lvlab.yml
$EDITOR Lvlab.yml          # set the image(s) and machine(s) you want

# 2. Download + verify the cloud image(s) the manifest references.
lvlab init

# 3. Create and launch a VM.
lvlab up salt.local

# 4. See its state, then connect.
lvlab status
lvlab ssh-config salt.local >> ~/.ssh/config
ssh salt.local
```

## Where to go next

- **Understand →** [Why lvlab](why.md): the rationale and the workflow it
    replaces.
- **Learn the commands →** [Walkthrough](walkthrough.md): what each `lvlab`
    subcommand actually does to your hypervisor.
- **Configure →** [Example manifest](example-manifest.md): a complete `Lvlab.yml`
    covering every supported cloud image, with annotations.
- **Run a one-off VM →** [createvm / deletevm](one-off-vms.md): a single VM
    without a manifest.
- **Customize cloud-init →** [Cloud-init examples](cloud-init-examples.md): the
    three files `lvlab` renders per machine, in minimum-viable form.
- **Troubleshoot the hypervisor →** [Libvirt notes](libvirt-notes.md):
    `virt-install` flags and `qemu-guest-agent` setup.
- **Contribute →** [CONTRIBUTING](https://github.com/memblin/tkc-lvlab-py/blob/main/docs-extra/CONTRIBUTING.md):
    dev-environment setup, the test suites, and the fork-and-PR workflow.

## API reference

Auto-generated from Google-style docstrings + type hints via
[mkdocstrings](https://mkdocstrings.github.io/):

- [API Reference](api/index.md)

## Where the rest of the docs live

Contributor and maintainer reference — CONTRIBUTING, release
procedure, host-validation matrix, internal design notes — lives in
the repo's
[`docs-extra/`](https://github.com/memblin/tkc-lvlab-py/tree/main/docs-extra)
directory, a sibling of this `docs/` source root that the
doc-builder doesn't scan. Those files are written for reading on
GitHub, where the surrounding repo context (PR templates, source
links, issue cross-references) is right next to them.
