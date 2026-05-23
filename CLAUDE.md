# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

`tkc-lvlab` (binary: `lvlab`) is a Typer-based CLI (Typer wraps Click underneath) that manages local libvirt+QEMU lab VMs from a single declarative YAML manifest (`Lvlab.yml`). It is meant for end-to-end integration testing of configuration-management code (Salt, Ansible, etc.) on a developer workstation — not for production VM management. The standalone one-off scripts (`createvm`, `destroyvm` in `src/tkc_lvlab/scripts/`) are also Typer-based after the Phase 9 follow-up; `createvm` keeps a minimal `import click` only for `click.Choice(case_sensitive=False)` on `--distro`.

A run of `lvlab init` followed by `lvlab up <vm_name>` will:

1. Download and verify a cloud image (checksum + optional GPG of the checksum file).
1. Create a qcow2 disk that uses the cloud image as a backing file (via `qemu-img`).
1. Render cloud-init `meta-data`, `user-data`, and `network-config` from Jinja2 templates, pack them into a `cidata.iso` (built in-process with `pycdlib`), and attach it as a cdrom.
1. Shell out to `virt-install` to define and launch the domain.

Because state lives in libvirt + on-disk qcow2 files, **bugs here can damage real VMs the developer cares about.** Treat destructive paths (`destroy`, `down`, snapshot `delete`) with care — there is no separate test hypervisor.

## Build / dev / lint commands

This project uses [uv](https://docs.astral.sh/uv/) (PEP 517 backend: `uv_build`). CI runs pre-commit and the pytest matrix (Python 3.11/3.12/3.13/3.14) on every PR and push to `main`. Integration tests (`@pytest.mark.integration`) are gated by `LVLAB_INTEGRATION=1` and **never** enabled in CI — see `tests/conftest.py` and the cross-cutting safety rules in `TODO.md`. Don't claim a behavior is "tested" because CI is green — that only covers what someone wrote a unit test for. For changes that touch `virsh`, also do a manual smoke test against `qemu:///session` before reporting done.

The CLI shells out to `virsh` (and `qemu-img`, `virt-install`) for all hypervisor operations — there is no longer a `libvirt-python` C extension dependency, so `uv sync` works without `libvirt-dev` / `pkg-config` on the host. Lab functionality still requires `libvirt-clients` (or equivalent) for the `virsh` binary at runtime.

```bash
# Sync deps into .venv
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

`src/tkc_lvlab/cli.py` defines the Typer app + subcommands (Phase 9 ported it from Click; tests use `typer.testing.CliRunner`). Every command follows the same shape:

1. `parse_config()` returns `(environment, images, config_defaults, machines)`.
1. `get_machine_by_vm_name(machines, vm_name)` finds the manifest entry.
1. `Machine(machine_config, environment, config_defaults)` merges defaults into the machine and exposes operations against libvirt.
1. For `up`, a `CloudImage` + `VirtualDisk` + `CloudInitIso` are constructed alongside the `Machine`.

`src/tkc_lvlab/utils/libvirt.py` — `Machine` is the central object. Key things to know:

- The libvirt domain name is **not** `vm_name`; it's `f"{vm_name}_{environment_name}"` (see `self.libvirt_vm_name`). This namespacing is what lets multiple lvlab environments coexist on one hypervisor. Anything that looks up a domain by name must use `libvirt_vm_name`.
- `Machine.__init__` merges `config_defaults` into the machine dict (interfaces, disks, and top-level keys). When adding a new configurable field, follow that same pattern instead of reading from `config_defaults` at call sites. `__init__` also validates `interfaces.network_type` (`network` / `user` / `passt`) and rejects the `network_type=user|passt` + static `ip4` combination — SLIRP/passt ignore static IPs, so the loud refusal happens at construction time before any state is created.
- `Machine.deploy()` shells out to `virt-install`. The `--os-variant` value is derived by splitting `self.os` on `-` and taking the first segment — this is why custom images must be named `{os_variant}-{anything}` (see `docs-extra/Walkthrough.md` "Image Naming"). The `--network` argument is built by `_virt_install_network_arg(iface)` near the top of `libvirt.py`: `network_type=network` (default) emits the managed-network form with a fixed PCI address; `network_type=user` / `passt` emit virt-install's user-mode forms (no libvirt network required) — required for `qemu:///session` where rootless libvirt cannot manage a NAT network. The same constants live in `utils/network.py` (`NETWORK_TYPES`, `USER_MODE_NETWORK_TYPES`) and are reused by `createvm`'s `--network-type` option.
- `Machine.cloud_init()` is also where per-machine `runcmd` gets composed with the defaults (`runcmd_ignore_defaults: true` skips defaults), and where the manifest-wide `/etc/hosts` snippet is injected at the **top** of `runcmd` so it lands before anything that does DNS-ish work.

`src/tkc_lvlab/utils/cloud_init.py` — three dataclasses (`NetworkConfig`, `MetaData`, `UserData`) each render one Jinja template from `src/tkc_lvlab/templates/`. `CloudInitIso` uses `pycdlib` to build an ISO9660 + Joliet + Rock Ridge image with the three files at the names cloud-init's NoCloud datasource expects (`meta-data`, `user-data`, `network-config`). `UserData.__post_init__` will read an SSH public key from disk if `cloud_init.pubkey` looks like a path; otherwise it treats the value as a literal key.

`src/tkc_lvlab/utils/images.py` — `CloudImage` knows how to download, GPG-verify the checksum file, and checksum-verify the image. Two non-obvious bits:

- Debian's `SHA512SUMS` file is the **same filename** across releases, so Debian images get a per-image-prefix checksum filename to avoid clobber when multiple Debian versions are configured. The detector is a regex on `debian-(\d+)` in the image filename.
- The checksum file parser handles both Fedora's `SHA256 (file) = hash` format and Debian's `hash  file` format.
- When GPG verification succeeds, the verified plaintext is written to `<checksum>.verified` and subsequent operations prefer that file.

`src/tkc_lvlab/utils/vdisk.py` — `VirtualDisk` is a thin wrapper around `qemu-img create -b <cloud_image>` for qcow2 backing-file disks. One disk per entry in `machine.disks`, named `disk{index}.qcow2`.

`src/tkc_lvlab/config.py` — `parse_config()` (manifest loader) and `generate_hosts()` (renders `templates/hosts.j2`, used both for stdout output by the `hosts` command and for the in-VM `/etc/hosts` cloud-init snippet — see `heredoc` parameter for the dual-mode rendering).

### Templates

`src/tkc_lvlab/templates/` contains the Jinja2 templates loaded via `PackageLoader("tkc_lvlab")`. `uv_build` auto-includes every file under the module root (`src/tkc_lvlab/`, set via `[tool.uv.build-backend] module-root = "src"` in `pyproject.toml`), so new templates ship automatically — but verify with `unzip -l dist/*.whl | grep templates` after a `uv build` if you add one.

- `network-config.v1.j2` and `network-config.v2.j2` — selected by `image.network_version` (1 = ENI-style, 2 = netplan-style). Each image entry in the manifest pins which version cloud-init should emit.
- `hosts.j2` renders both stdout-friendly output and a `cat <<EOF` heredoc form for runcmd injection.

### Host /etc/hosts handling inside the guest

`Machine.cloud_init` appends two heredocs to `runcmd`: one for `/etc/hosts` and one for the distro-appropriate `/etc/cloud/templates/hosts.{debian,redhat}.tmpl`. The template choice is a startswith-match on `self.os` against `template_file_mapping` (lowercased). If you add support for a new distro family, extend that mapping or `cloud_init()` will raise `ValueError`.

## Conventions and gotchas

- **Line length is 150** (`.pylintrc`). black is configured by default (88) via pre-commit; both are in effect — black formats, pylint just won't yell about long lines. If you see a line over 88, black either accepted it (string literal, URL, etc.) or it hasn't been run.
- **Type hints are required on new code; existing code is uneven.** See the "Documentation conventions" section below for the full rule. `docs-extra/Design.md` records that this used to be project-wide; the post-mkdocstrings policy supersedes that note for new work. Don't bulk-convert existing signatures as a side effect of an unrelated PR — there's a dedicated Phase in `TODO.md` for that.
- The CLI mixes business logic into `cli.py` (e.g. orchestration of vdisk creation, ISO writing, deploy). When extending, prefer adding methods to the relevant `Machine` / `CloudImage` / etc. class rather than growing the command body.
- `parse_config()` is called repeatedly (e.g. once in the command, again inside `Machine.cloud_init` to regenerate the hosts list). Cheap because it's just a file read, but keep that in mind if you ever cache state.
- Several `destroy`/cleanup paths leave files behind on purpose or by oversight — see `docs-extra/Walkthrough.md`. Don't "fix" this without checking whether the user relied on it.
- A sibling project `lvscripts-py` (allowed via `.claude/settings.local.json`) is referenced for porting advanced features into this repo. Don't import from it; read it and adapt.

## Documentation conventions

The project uses **MkDocs + Material + mkdocstrings** to generate API docs from
Google-style docstrings + type hints. Preview locally with:

```bash
uv sync --group dev
uv run mkdocs serve   # http://127.0.0.1:8000
```

The site is configured in `mkdocs.yml`. The `docs/` directory holds both the
existing user-facing markdown (Walkthrough.md, Design.md, Why.md, etc.) and the
new mkdocs site files (`index.md`, `api/`). Existing pages remain reachable but
are out-of-nav until the legacy docs conversion lands (see `TODO.md`).

### For new code — required

- **Type hints on every public function, method, parameter, and return value.**
    mkdocstrings reads the signature as the source of truth; do not restate types
    in the docstring body.
- **Google-style docstrings on every public symbol** (module, class, function,
    method). Section order: one-line summary → blank line → optional longer
    description → `Args:` → `Returns:` (or `Yields:`) → `Raises:` → `Example:`.
    Skip sections that don't apply.

Example shape:

```python
def parse_checksum_file(path: Path) -> dict[str, str]:
    """Parse a cloud-image checksum manifest.

    Handles both Fedora's ``SHA256 (file) = hash`` syntax and Debian's
    ``hash  file`` syntax. When a ``.verified`` companion file exists
    (post-GPG verification), it takes precedence.

    Args:
        path: Filesystem path to the checksum file.

    Returns:
        A dict mapping filename to hex digest.

    Raises:
        ChecksumParseError: When neither syntax matches any line.
    """
```

For classes, document the class itself (one-liner plus an `Attributes:` block
if useful), and document each method separately. `__init__` parameters go in
the **class-level** docstring's `Args:` section, not in `__init__`'s own
docstring — mkdocstrings renders them under the class.

For modules, put a docstring at the top of the file describing what the module
provides.

### For existing code — out of scope here

Existing docstrings are free-form and largely untyped. **Do not sweep-convert
them as a side effect of unrelated PRs.** A dedicated phase in `TODO.md`
("Legacy docstring conversion") tracks that work.

If you happen to be rewriting a function for unrelated reasons and the new
shape benefits from a proper docstring + type hints, that's fine — write it
to the new convention. Don't touch neighbors.

## Testing conventions

### What tests are for

Tests exist to **catch bugs and prevent regressions** — not to hit a coverage
number. Don't write tests that merely restate the function signature
(`def test_foo_returns_int: assert isinstance(foo(), int)`); the type
checker and mkdocstrings already know that. A test that can't fail because
of a realistic bug is noise.

A useful test names a behavior, sets up the conditions that trigger it,
exercises the code, and asserts the outcome a reviewer can recognize as
right. Examples in this codebase:

- ✅ `parse_checksum_file` parses both Fedora's `SHA256 (file) = hash` AND
    Debian's `hash  file` formats from real fixtures.
- ✅ `Machine.libvirt_vm_name` namespaces VMs by environment so two
    environments with the same `vm_name` don't collide.
- ✅ `run_virsh` translates `FileNotFoundError` (missing `virsh` binary)
    into a `VirshError` rather than crashing.
- ❌ `test_run_virsh_returns_completed_process: assert isinstance(...)` —
    no realistic bug fails this.

Coverage is a **diagnostic**, not a target. If a branch is uncovered, ask
whether it has a realistic failure mode worth testing; if not, leave it
uncovered or use `# pragma: no cover` and explain why.

### Unit tests (`tests/test_*.py`)

- Pure: no `virsh`, no `qemu-img`, no libvirt daemon, no network.
- Patch `subprocess.run` with `unittest.mock.patch` when wrapping shell-outs.
- Use real fixtures (captured output samples, sample manifests) rather than
    fabricated round-trip data — fabricated data only catches bugs in the
    fabrication.
- Run on every developer's machine with `uv run pytest`. They will run
    unmodified in CI once the matrix workflow lands (Phase 3).

### Integration tests (Phase 3, opt-in only)

Integration tests actually invoke `virsh`, build cloud-init ISOs, and
exercise `qemu-img`. They **must run only on machines with libvirt
installed and no developer VMs the test could possibly clobber.** The
safety rules are non-negotiable:

1. Marked `@pytest.mark.integration` and skipped by default. Enable with
    `LVLAB_INTEGRATION=1`. Never enable in CI on shared runners.
1. Every libvirt domain, qcow2 file, and ISO the test creates **must** be
    named with the `LVLAB_TEST_PREFIX` from `tests/conftest.py`. Resources
    that don't carry the prefix are off-limits to test teardown — that
    prefix is the only guarantee that a runaway test cleanup can't destroy
    a real developer VM.
1. Use a dedicated `libvirt_uri` (or at minimum a dedicated network and
    storage pool) so cleanup can be scoped further. A `qemu:///session`
    with shared developer VMs is **not** an acceptable target.
1. The session-scoped teardown enumerates only prefixed domains via
    `virsh list --all --name | grep "^${LVLAB_TEST_PREFIX}"`. Never list
    all domains in teardown; never iterate over all qcow2 files in a
    directory unless every one starts with the prefix.

See `TODO.md` "Cross-cutting safety rules" for the full scaffolding plan.

## Branching

**Work directly on `main`.** This is a solo-maintained project and topic branches were adding friction without delivering PR-review benefits while the codebase has a single maintainer. Make focused, well-scoped commits directly on `main`. The user is the one who pushes, so an unintended local commit is recoverable with `git reset` before push — but that means the bar for commit quality on `main` is the same bar you'd apply to a PR head: each commit should stand on its own and pass `pre-commit run --all-files`.

Release tags on `main` still trigger `.github/workflows/build-release.yml`, so the "never push tags without an explicit, scoped request" rule (see "Git pushes" below) remains in effect — that's the actual guardrail against accidental releases.

Topic branches are still appropriate in two cases — **ask before creating one**, don't invent the name:

- A multi-commit experiment the user may want to discard wholesale.
- Work the user has explicitly asked be isolated for review or testing.

When a topic branch lands, prefer a fast-forward merge to preserve the individual commits; squash-merge only when the user asks for it. (Earlier guidance preferred squash-merge as the default; that was tied to the topic-branch-per-change pattern that's no longer in use.)

## Spawning agents in worktrees

When the orchestrator spawns a subagent with `isolation: "worktree"`, the worktree is **not guaranteed to be branched from current `main` HEAD** — in practice it has come up branched from an older commit, which silently breaks anything that depends on recent test infrastructure, helpers, or policy changes. This bit us during the 2026-05-23 parallel-agent run for Phases 11 and 12.

**Rule for worktree-isolated agents:** the agent's first action must be to sync the worktree to current `main` HEAD before doing any other work. The orchestrator should put this literal step at the top of the prompt:

> Before doing anything else, run `git fetch origin main && git reset --hard origin/main` inside this worktree, then verify with `git log -1 --oneline`. Stop and report if the reset fails.

Alternatively the orchestrator can hand the agent the current `main` HEAD SHA in the prompt and require the agent to verify its worktree matches before starting. Either form is fine; the absence of any sync step is what fails.

For short, single-file changes that don't need a separate branch, prefer spawning the agent without `isolation: "worktree"` so it works directly on `main` — same risk profile as the orchestrator working on `main`, no stale-base trap.

## Git pushes

**Pushes via `gh`'s authenticated PAT are allowed when the user has asked for them**, scoped to the PAT's `contents:write` + `pull-requests:write`. The user normally pushes themselves from their own terminal (via the SSH remote — see "Repo remotes" memory); if the user asks you to push, use the HTTPS PAT path.

The remote `origin` is configured for SSH (`git@github.com:...`) but the gh PAT only authenticates HTTPS. Two consequences:

- For pushes, push to the HTTPS URL explicitly (`git push https://github.com/memblin/tkc-lvlab-py.git main`) — `gh auth git-credential` is wired into the global gitconfig and supplies the token. Don't rewrite `origin`; the user uses SSH from their own terminal.
- Fetches in this environment will also need the HTTPS URL (e.g. `git pull https://github.com/memblin/tkc-lvlab-py.git main`) because no SSH key here has read access.

**Still off-limits without an explicit, scoped request:**

- Force-push of any kind (`--force`, `--force-with-lease`) — `main` is the live branch, not a topic-branch sandbox.
- Pushing tags — tag pushes on `main` trigger `.github/workflows/build-release.yml` and cut a real GitHub release. See "Releasing".
- Pushes to `main` (the user pushes themselves; only push when explicitly asked).
- `gh pr merge`, `gh pr close`, branch deletion on the remote, or any write to issues/discussions/releases the user hasn't asked for.

## Releasing

Tagging `X.Y.Z` on `main` triggers `.github/workflows/build-release.yml`, which runs `poetry build` and uploads the wheel to a GitHub release. Bump `version` in `pyproject.toml` to match the tag before pushing it, or the artifact filename won't line up with the release.
