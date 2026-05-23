# tkc-lvlab

A Click-based CLI for managing local libvirt+QEMU lab VMs from a single
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

## Status

This site is a work in progress. The legacy user-facing docs
(`Walkthrough.md`, `Design.md`, `Why.md`, etc.) still live in the repo's
`docs/` directory and remain authoritative until the legacy-docs conversion
phase lands. The API reference here covers only modules whose docstrings
have been migrated to the Google + type-hint convention; everything else is
out-of-nav for now.
