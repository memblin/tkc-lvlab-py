# tkc-lvlab

A Typer-based CLI for managing local libvirt+QEMU lab VMs from a single
declarative YAML manifest (`Lvlab.yml`). Built for end-to-end integration
testing of configuration-management code (Salt, Ansible, etc.) on a developer
workstation — not for production VM management.

## What it does

`lvlab init` followed by `lvlab up <vm_name>` will:

1. Download and verify a cloud image (checksum + optional GPG of the checksum file).
1. Create a qcow2 disk that uses the cloud image as a backing file (via `qemu-img`).
1. Render cloud-init `meta-data`, `user-data`, and `network-config` from Jinja2
    templates, pack them into a `cidata.iso` (built in-process with `pycdlib`),
    and attach it as a cdrom.
1. Shell out to `virt-install` to define and launch the domain.

## Install

```bash
uv tool install tkc-lvlab
```

## User guide

- [Why lvlab](why.md) — the rationale and the workflow it replaces.
- [Walkthrough](walkthrough.md) — what each `lvlab` subcommand
    actually does to your hypervisor.
- [Example manifest](example-manifest.md) — a complete `Lvlab.yml`
    covering every supported host OS, with annotations.
- [Cloud-init examples](cloud-init-examples.md) — the three files
    `lvlab` renders per machine, in minimum-viable form.
- [Libvirt notes](libvirt-notes.md) — short hypervisor-side
    reference for `virt-install` flags and `qemu-guest-agent` setup.

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
