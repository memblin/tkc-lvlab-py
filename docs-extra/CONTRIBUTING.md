# Contributing to the project

We have a GitHub project to track planned work for this repo:

- [TKC Labs : Libvirt Labs](https://github.com/users/memblin/projects/3)

Pull Requests and reports of issues welcome.

## Branch naming

The branch naming approach I use when working on this repo is for reference. I won't enforce this same pattern for others interested in helping at this time. I make it available to help others who may be new to coding and perhaps a bit timid about jumping into an open project.

```bash
# Pull most recent on main branch
user@example01:~/repos/github/memblin/tkc-lvlab-py$ git pull main
On branch main
Your branch is up to date with 'origin/main'.

nothing to commit, working tree clean

# Create new development branch for Issue #37
user@example01:~/repos/github/memblin/tkc-lvlab-py$ git checkout -b main.issue37
Switched to a new branch 'main.issue37'

# Do work and stage changes for commit
git add .

# Commit with conventional commit message
# https://www.conventionalcommits.org/en/v1.0.0/#summary
git commit -m 'docs: more contributor help info'

# Push to my repo
git push -u origin main.issue37

# I usually let commit trigger pre-commit but to do-so
# manually now and then I use
pre-commit run --all-files
```

## Tools

The project uses..

- [uv](https://docs.astral.sh/uv/) for dependency management and packaging.
    Install via the [official installer](https://docs.astral.sh/uv/getting-started/installation/).
    The build backend is Hatchling with `uv-dynamic-versioning` for
    git-tag-based versions (both declared in `pyproject.toml`).

These are some common uv commands:

```bash
# Sync runtime deps into .venv
uv sync

# Sync runtime + dev tools (pytest, zensical, mkdocstrings, mdformat, etc.)
uv sync --group dev

# Run the CLI from a checkout without installing
uv run lvlab --help

# Add a runtime dependency to [project.dependencies]
uv add <package>

# Add a dev-only dependency to [dependency-groups].dev
uv add --group dev <package>

# Build wheel + sdist into ./dist
uv build
```

- [pre-commit](https://pre-commit.com/) runs the formatting + hygiene checks
    that CI also runs on every PR. Hooks: `check-yaml`, `end-of-file-fixer`,
    `trailing-whitespace`, `black`, `mdformat`.

```bash
# Install our pre-commit hooks in the repo after cloning
pre-commit install

# Run all hooks against every file (useful before opening a PR)
pre-commit run --all-files
```

- [black](https://black.readthedocs.io/en/stable/) handles Python formatting;
    pre-commit runs it for you. Line length is `150` per `.pylintrc` (black's
    default `88` still applies for new code — pylint just won't yell at lines
    that black accepted, like long string literals or URLs).

- [just](https://github.com/casey/just) is the task runner for the common
    workflows below. Every recipe just wraps the same `uv` / `pytest` /
    `zensical` commands documented here, so `just` is a convenience, not a
    requirement. Install it via your package manager (`dnf install just`,
    `apt install just`, `cargo install just`, …).

```bash
# List every recipe
just

# Unit tests: current interpreter / with coverage / across 3.11–3.14
just test
just test-cov
just test-matrix

# Formatting + hygiene (pre-commit) and the integration-safety AST gate
just lint
just test-safety

# Docs: strict build / live-reload serve
just docs
just docs-serve

# Build the wheel, plus a fresh-venv install smoke check
just build
just build-smoke

# Integration suites (libvirt host required; gated, never in CI)
just integration              # every @pytest.mark.integration test
just integration-createvm     # just the createvm/deletevm matrix
```

## Unit tests

The unit test suite runs against Python 3.11, 3.12, 3.13, and 3.14 in CI
(see `.github/workflows/test.yml`).

```bash
# Run the full suite locally — fast (subseconds), no libvirt required
uv run pytest

# Run just one file
uv run pytest tests/test_virsh.py
```

Integration tests that touch real `virsh` / `qemu-img` / libvirt are gated
by the `LVLAB_INTEGRATION=1` environment variable and are **never** enabled
in CI. See `tests/conftest.py` and the "Integration tests" safety rules
in `CLAUDE.md` before writing one — every test-owned libvirt domain,
qcow2 file, or ISO must carry the per-session `LVLAB_TEST_PREFIX`, and
the session-scoped reaper will only ever touch resources whose name
starts with that prefix.

### Integration test storage layout

Integration tests use a **dedicated** storage directory rather than the
production `/var/lib/libvirt/images/oneoff/` path that the real
`createvm` script defaults to. The test storage root is:

```text
/var/lib/libvirt/images/lvlab-test/
```

Tests must pass this path to both `createvm` and `deletevm` via
`--storage-root`. The `lvlab_integration_storage_root` session-scoped
fixture in `tests/conftest.py` exposes the path; use that, don't
hard-code the string.

Conventions:

- **No overwrite.** `createvm`'s per-VM `mkdir(exist_ok=False)` is the
    guarantee — a stale prefixed directory from a crashed prior run
    will cause the next `createvm` call for the same name to fail with
    a clear error rather than silently corrupting state. Tests must
    not work around this by removing the per-VM dir before calling
    `createvm`.
- **Auto-create.** The readiness probe in `tests/conftest.py`
    (`_uri_is_test_ready`) creates `/var/lib/libvirt/images/lvlab-test/`
    on demand with mode `0755` so the `qemu` user (under
    `qemu:///system`) can traverse it. If the directory cannot be
    created or written by the test user, the per-URI run is skipped
    with a clear message.
- **Dedicated, not shared.** Never use the production
    `/var/lib/libvirt/images/oneoff/` directory from a test — even
    with the prefix guard, sharing a parent dir with real one-off
    VMs is the kind of layout that quietly grows risky as scope
    expands. The dedicated `lvlab-test/` root keeps the blast radius
    of any reaper or cleanup helper limited to test-owned state.
- **Session reaper.** `_reap_test_prefixed_storage` (in
    `tests/conftest.py`) walks **only** `lvlab-test/` at session end
    and removes prefix-matching subdirs. It will never iterate over
    `oneoff/` or any other neighbour, even though they share a
    parent.

### Running integration tests

On a host where the libvirt-group setup from "Host setup" below is
complete **and the shell session post-dates the `usermod -aG`**, the
default invocation is plain:

```bash
LVLAB_INTEGRATION=1 uv run pytest tests/test_integration_*.py
```

No `sg`, no `sudo`, no extra wrapping. Each test is parametrized over
`qemu:///session` and `qemu:///system`; URIs that aren't ready (no
network, no storage-root write access, daemon unreachable) get
skipped per-URI with a clear reason rather than failing the test.

If integration tests start skipping with the message *"test storage
root /var/lib/libvirt/images/lvlab-test cannot be created … not
writable by test user"*, the host is set up correctly but your shell
session is from before the libvirt-group addition — see the
"Footgun after `usermod -aG`" callout below. The durable fix is to
log out and back in; `sg libvirt -c "…"` is an in-session escape
hatch, **not** the intended invocation pattern for ongoing
development work.

### createvm / deletevm connectivity matrix

`tests/test_integration_createvm.py` exercises **every image in
`createvm`'s `BUILTIN_IMAGES` catalog in both addressing modes** — DHCP
and static — on `qemu:///system` (createvm is system-only; the session
URI parameter is skipped). Each case creates the VM, resolves its IP (the
NAT DHCP lease via `virsh domifaddr` for DHCP, the assigned address for
static), waits for first-boot SSH, and asserts the cloud-init default user
(`id -un`). That one assertion proves connectivity **and** that `createvm`
seeded the right user and public key, then `deletevm --force` removes it.

The matrix is **serial by design**: each case tears its VM down (in a
`finally`, then waits for the domain to disappear) before the next starts,
so at most one test VM is ever live — no `pytest-xdist`. Run it with
`just integration-createvm` or directly:

```bash
LVLAB_INTEGRATION=1 uv run pytest tests/test_integration_createvm.py -v
```

On a resource-constrained host, subset the matrix with two comma-separated
env vars (default is the full catalog × both modes):

```bash
# One image, DHCP only — a single VM at a time
LVLAB_TEST_DISTROS=debian13 LVLAB_TEST_MODES=dhcp \
  just integration-createvm
```

**Static IP on the `default` network is gated.** `createvm` refuses an
`--ip4` that falls inside the network's DHCP range, and libvirt's stock
`default` network ranges over the whole usable subnet (`.2`–`.254`), so no
valid static address exists on it. The static cases therefore **skip**
with that explanation rather than fail, and the suite **never modifies
your `default` network**. To exercise static addressing, narrow the
network's DHCP range yourself (e.g. `.2`–`.199`, freeing `.200`–`.254`)
and re-run, or use a dedicated test network. An opt-in, auto-reverting
transient narrow is tracked in
[#86](https://github.com/memblin/tkc-lvlab-py/issues/86).

## End-to-End Testing

A smoke-test checklist for verifying CLI changes against a real
`qemu:///session` hypervisor. There is no automated end-to-end suite —
intentional, since the test resources are real VMs on the developer's
own machine.

### Host setup

For the validated host distros — Debian 12/13, Ubuntu 24.04, AlmaLinux 10,
Fedora 44 — `scripts/host-bootstrap.sh` does the package install,
libvirt group setup, and `/var/lib/libvirt/images/lvlab-test/`
creation in one go. See `docs-extra/host-validation.md` for the full
matrix and the procedure for recording a validated run. The manual
notes below remain authoritative for any host outside that set.

The smoke tests assume your user can talk to `qemu:///system` (or
`qemu:///session` if you prefer rootless). The two URIs need different
host setup:

- **`qemu:///system`** — managed libvirt network (`default`).
    Requires the libvirt-group setup below so your user can write to
    `/var/lib/libvirt/images/` and reach the system daemon.
- **`qemu:///session`** — user-mode networking (manifest
    `interfaces.network_type: user`; `createvm` itself is
    `qemu:///system`-only).
    Rootless libvirt cannot manage a NAT network, so lvlab routes
    through virt-install's SLIRP/passt user-mode networking. **No
    libvirt network needs to exist** on the session URI; `virsh -c qemu:///session net-list` may legitimately be empty.
    Storage still has to be writable by the user running lvlab, but
    no `libvirt-group` membership is needed.

Typical one-time setup for `qemu:///system` on a fresh box:

```bash
# Install host binaries the createvm/lvlab paths need at runtime
sudo dnf install -y libvirt libvirt-clients qemu-kvm virt-install   # RHEL/Fedora family
# (or) sudo apt install -y libvirt-daemon-system libvirt-clients qemu-kvm virt-install   # Debian family

# Add yourself to the libvirt group so /var/lib/libvirt/images is writable
sudo usermod -aG libvirt "$USER"
```

**Footgun after `usermod -aG`:** the new group membership doesn't take
effect in your current shell session — you need a fresh login (or
`newgrp libvirt`) before `lvlab` / `createvm` can write to
`/var/lib/libvirt/images/`. The smoke-test failure mode is a
`PermissionError` on the image cache directory, which can look like
a code bug when it's really a stale shell.

If re-logging in is inconvenient (e.g. an SSH session you don't want
to drop), you can run any single command under the new group with
`sg`:

```bash
sg libvirt -c "uv run createvm smoketest debian13"
sg libvirt -c "uv run lvlab up vault.local"
sg libvirt -c "uv run lvlab destroy vault.local --force"
```

`id -G` will show `libvirt`'s GID (typically 990 on Fedora/RHEL) once
the group is effective. Inside the `sg`-wrapped command it should
appear; in the parent shell it won't until you re-login.

```bash
# Capabilities command
lvlab capabilities

# Environment initialization
lvlab init

# Verify /etc/hosts file content rendering
lvlab hosts
lvlab hosts --heredoc

# Verify /etc/hosts file content update
# This will write to /etc/hosts so only run on ephemeral test machine
sudo lvlab hosts --append

# VM Operations
#
# Check the status
lvlab status
# Bring up salt.local
lvlab up salt.local
# Check that status agrees
lvlab status
# Verify cloud-init re-render works
lvlab cloudinit salt.local
# List snapshots when we know there aren' tany
lvlab snapshot list salt.local
# Create a snapshot
lvlab snapshot create salt.local Base
# List snapshots now that we know there is one
lvlab snapshot list salt.local
# Delete the snapshot
lvlab snapshot delete salt.local Base
# Shutdown salt.local
lvlab down salt.local
# Check that status agrees (may need to wait for shutdown)
lvlab status
# Bring salt.local up from down state
lvlab up salt.local
# Check that status agrees
lvlab status
# Destroy the running VM
lvlab destroy salt.local
# Check that status agrees
lvlab status
```
