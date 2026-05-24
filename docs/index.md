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

## Quick start

See the project README on GitHub for setup and a worked example.

## API reference

Auto-generated from Google-style docstrings + type hints via
[mkdocstrings](https://mkdocstrings.github.io/):

- [API Reference](api/index.md)

## Where the rest of the docs live

This site hosts the API reference and short user-facing notes.

Contributor and maintainer reference (CONTRIBUTING, release procedure,
host-validation matrix, design notes) intentionally lives in the
repo's [`docs-extra/`](https://github.com/memblin/tkc-lvlab-py/tree/main/docs-extra)
directory — a sibling of this `docs/` source root that the
doc-builder doesn't scan. Those files are written for reading on
GitHub, where the surrounding repo context (PR templates, source
links, issue cross-references) is right next to them.

A future doc-polish effort will fold the user-facing pieces
(`Walkthrough.md`, `Why.md`, `Libvirt.md`) into the rendered site
so end users see a single nav; project/contributor docs will stay
in `docs-extra/` by design.
