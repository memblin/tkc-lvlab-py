# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

`tkc-lvlab` (binary: `lvlab`) is a Click-based CLI that manages local libvirt+QEMU lab VMs from a single declarative YAML manifest (`Lvlab.yml`). It is meant for end-to-end integration testing of configuration-management code (Salt, Ansible, etc.) on a developer workstation — not for production VM management.

A run of `lvlab init` followed by `lvlab up <vm_name>` will:
1. Download and verify a cloud image (checksum + optional GPG of the checksum file).
2. Create a qcow2 disk that uses the cloud image as a backing file (via `qemu-img`).
3. Render cloud-init `meta-data`, `user-data`, and `network-config` from Jinja2 templates, pack them into a `cidata.iso` (built in-process with `pycdlib`), and attach it as a cdrom.
4. Shell out to `virt-install` to define and launch the domain.

Because state lives in libvirt + on-disk qcow2 files, **bugs here can damage real VMs the developer cares about.** Treat destructive paths (`destroy`, `down`, snapshot `delete`) with care — there is no separate test hypervisor.

## Build / dev / lint commands

This project uses [uv](https://docs.astral.sh/uv/) (PEP 517 backend: `uv_build`). There is **no test suite yet** — `tests/__init__.py` is empty. Don't claim something is "tested" because CI is green; CI only runs pre-commit.

```bash
# Sync deps into .venv (needs libvirt-dev / pkg-config on the host until Phase 2
# of TODO.md lands — libvirt-python is a C extension that compiles against them)
uv sync

# Sync with dev tools (pytest etc. from the [dependency-groups] dev table)
uv sync --group dev

# Run the CLI from a checkout without installing
uv run lvlab --help

# Build a wheel and sdist into ./dist
uv build

# Format + basic hygiene (black, trailing whitespace, EOF, yaml check)
pre-commit run --all-files
```

Python floor is `>=3.11` in `pyproject.toml`. The release workflow builds with 3.13.

## Architecture

The code is organized around the `Lvlab.yml` manifest. Read `parse_config()` first — every command starts there.

`Lvlab.yml` has two top-level sections:
- `environment[0]` — exactly one environment with `name`, `libvirt_uri`, `config_defaults` (cpu/memory/disks/interfaces/cloud_init that apply to every machine), and a list of `machines`.
- `images` — a dict of named cloud images (URL + checksum + GPG + cloud-init network schema version).

### Core flow (cli.py → utils/)

`tkc_lvlab/cli.py` defines the Click command group. Every command follows the same shape:

1. `parse_config()` returns `(environment, images, config_defaults, machines)`.
2. `get_machine_by_vm_name(machines, vm_name)` finds the manifest entry.
3. `Machine(machine_config, environment, config_defaults)` merges defaults into the machine and exposes operations against libvirt.
4. For `up`, a `CloudImage` + `VirtualDisk` + `CloudInitIso` are constructed alongside the `Machine`.

`tkc_lvlab/utils/libvirt.py` — `Machine` is the central object. Key things to know:
- The libvirt domain name is **not** `vm_name`; it's `f"{vm_name}_{environment_name}"` (see `self.libvirt_vm_name`). This namespacing is what lets multiple lvlab environments coexist on one hypervisor. Anything that looks up a domain by name must use `libvirt_vm_name`.
- `Machine.__init__` merges `config_defaults` into the machine dict (interfaces, disks, and top-level keys). When adding a new configurable field, follow that same pattern instead of reading from `config_defaults` at call sites.
- `Machine.deploy()` shells out to `virt-install`. The `--os-variant` value is derived by splitting `self.os` on `-` and taking the first segment — this is why custom images must be named `{os_variant}-{anything}` (see `docs/Walkthrough.md` "Image Naming").
- `Machine.cloud_init()` is also where per-machine `runcmd` gets composed with the defaults (`runcmd_ignore_defaults: true` skips defaults), and where the manifest-wide `/etc/hosts` snippet is injected at the **top** of `runcmd` so it lands before anything that does DNS-ish work.

`tkc_lvlab/utils/cloud_init.py` — three dataclasses (`NetworkConfig`, `MetaData`, `UserData`) each render one Jinja template from `tkc_lvlab/templates/`. `CloudInitIso` uses `pycdlib` to build an ISO9660 + Joliet + Rock Ridge image with the three files at the names cloud-init's NoCloud datasource expects (`meta-data`, `user-data`, `network-config`). `UserData.__post_init__` will read an SSH public key from disk if `cloud_init.pubkey` looks like a path; otherwise it treats the value as a literal key.

`tkc_lvlab/utils/images.py` — `CloudImage` knows how to download, GPG-verify the checksum file, and checksum-verify the image. Two non-obvious bits:
- Debian's `SHA512SUMS` file is the **same filename** across releases, so Debian images get a per-image-prefix checksum filename to avoid clobber when multiple Debian versions are configured. The detector is a regex on `debian-(\d+)` in the image filename.
- The checksum file parser handles both Fedora's `SHA256 (file) = hash` format and Debian's `hash  file` format.
- When GPG verification succeeds, the verified plaintext is written to `<checksum>.verified` and subsequent operations prefer that file.

`tkc_lvlab/utils/vdisk.py` — `VirtualDisk` is a thin wrapper around `qemu-img create -b <cloud_image>` for qcow2 backing-file disks. One disk per entry in `machine.disks`, named `disk{index}.qcow2`.

`tkc_lvlab/config.py` — `parse_config()` (manifest loader) and `generate_hosts()` (renders `templates/hosts.j2`, used both for stdout output by the `hosts` command and for the in-VM `/etc/hosts` cloud-init snippet — see `heredoc` parameter for the dual-mode rendering).

### Templates

`tkc_lvlab/templates/` contains the Jinja2 templates loaded via `PackageLoader("tkc_lvlab")`. They are packaged into the wheel through the `include = ["tkc_lvlab/templates/*.j2"]` entry in `pyproject.toml` — if you add a new template, make sure it matches that glob or it won't ship.

- `network-config.v1.j2` and `network-config.v2.j2` — selected by `image.network_version` (1 = ENI-style, 2 = netplan-style). Each image entry in the manifest pins which version cloud-init should emit.
- `hosts.j2` renders both stdout-friendly output and a `cat <<EOF` heredoc form for runcmd injection.

### Host /etc/hosts handling inside the guest

`Machine.cloud_init` appends two heredocs to `runcmd`: one for `/etc/hosts` and one for the distro-appropriate `/etc/cloud/templates/hosts.{debian,redhat}.tmpl`. The template choice is a startswith-match on `self.os` against `template_file_mapping` (lowercased). If you add support for a new distro family, extend that mapping or `cloud_init()` will raise `ValueError`.

## Conventions and gotchas

- **Line length is 150** (`.pylintrc`). black is configured by default (88) via pre-commit; both are in effect — black formats, pylint just won't yell about long lines. If you see a line over 88, black either accepted it (string literal, URL, etc.) or it hasn't been run.
- **No type-hint discipline yet.** `docs/Design.md` calls this out as a known inconsistency; don't refactor existing signatures just to add annotations unless asked.
- The CLI mixes business logic into `cli.py` (e.g. orchestration of vdisk creation, ISO writing, deploy). When extending, prefer adding methods to the relevant `Machine` / `CloudImage` / etc. class rather than growing the command body.
- `parse_config()` is called repeatedly (e.g. once in the command, again inside `Machine.cloud_init` to regenerate the hosts list). Cheap because it's just a file read, but keep that in mind if you ever cache state.
- Several `destroy`/cleanup paths leave files behind on purpose or by oversight — see `docs/Walkthrough.md`. Don't "fix" this without checking whether the user relied on it.
- A sibling project `lvscripts-py` (allowed via `.claude/settings.local.json`) is referenced for porting advanced features into this repo. Don't import from it; read it and adapt.

## Branching

**Never start work directly on `main`.** This project requires PRs into `main`; release tags on `main` trigger `.github/workflows/build-release.yml`, so anything that lands on `main` outside a reviewed PR risks ending up in a release. Before making code changes, check the current branch — if it is `main`, stop and confirm with the user which topic branch to use (or which to create). Do not invent a branch name and create it yourself without checking first.

## Git pushes

**Pushes via `gh`'s authenticated PAT are allowed when the user has asked for them**, scoped to the PAT's `contents:write` + `pull-requests:write`. That means `git push` of feature/topic branches and `gh pr create` against `main` are fine in those cases.

The remote `origin` is configured for SSH (`git@github.com:...`) but the gh PAT only authenticates HTTPS. Two consequences:
- For pushes, push to the HTTPS URL explicitly (`git push -u https://github.com/memblin/tkc-lvlab-py.git <branch>`) — `gh auth git-credential` is wired into the global gitconfig and supplies the token. Don't rewrite `origin`; the user uses SSH from their own terminal.
- Fetches in this environment will also need the HTTPS URL (e.g. `git pull https://github.com/memblin/tkc-lvlab-py.git main`) because no SSH key here has read access.

**Still off-limits without an explicit, scoped request:**
- Force-push of any kind (`--force`, `--force-with-lease`).
- Pushing tags — tag pushes on `main` trigger `.github/workflows/build-release.yml` and cut a real GitHub release. See "Releasing".
- Pushes directly to `main` (the "Branching" rule still applies — work goes through PRs).
- `gh pr merge`, `gh pr close`, branch deletion on the remote, or any write to issues/discussions/releases the user hasn't asked for.

If a PR is already open on a branch, prefer adding follow-up commits over force-pushing.

## Releasing

Tagging `X.Y.Z` on `main` triggers `.github/workflows/build-release.yml`, which runs `poetry build` and uploads the wheel to a GitHub release. Bump `version` in `pyproject.toml` to match the tag before pushing it, or the artifact filename won't line up with the release.
