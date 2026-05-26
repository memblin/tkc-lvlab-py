# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

`tkc-lvlab` (binary: `lvlab`) is a Typer-based CLI (Typer wraps Click underneath) that manages local libvirt+QEMU lab VMs from a single declarative YAML manifest (`Lvlab.yml`). It is meant for end-to-end integration testing of configuration-management code (Salt, Ansible, etc.) on a developer workstation — not for production VM management.

The standalone one-off scripts (`createvm`, `deletevm` in `src/tkc_lvlab/scripts/`) originated as **faithful ports of the sibling `lvscripts-py` commands** (`createvm` / `deletevm`) and are now the **canonical implementation** — `lvscripts-py` is being archived (see the conventions note below), so they stand on their own here and no longer need a parity-with-lvscripts justification. They kept lvscripts' positional arguments, colored output, options, and operations, adapted in three ways for lvlab — image storage paths, config source, and a pinned per-VM NIC MAC (see below). (The MAC pin is the one behavioral divergence from lvscripts beyond storage/config: a cross-distro correctness fix, not a feature — see the v2 template note below.) They are pure Typer (no `import click`; `--ip4` parsing raises `ValueError`). Both target `qemu:///system` and operate on **raw libvirt domain names** (the name you pass is the domain name — no `oneoff-` prefix). `createvm`'s positional `VM_DISTRO` resolves against a built-in image catalog (`BUILTIN_IMAGES`, same dict schema as an `Lvlab.yml` `images:` entry) merged with the `images:` section of an `Lvlab.yml` in the current directory (or `--config <path>`); manifest wins on a name collision, and `os_variant`/`username` are derived from the image key unless the entry overrides them. It shares the `/var/lib/libvirt/images/lvlab/cloud-images` cache with `lvlab up`, always produces a standalone qcow2 (`cp` + `qemu-img resize`, no backing file), renders static `--ip4`/`--netmask` addressing into the guest's network-config, pins a generated MAC (`utils/network.generate_mac`) into both the `virt-install --network ...,mac=` arg and the network-config's `match: macaddress` so the guest binds the right NIC on every distro, and waits for a NAT DHCP lease before printing the SSH hint (the lease lookup uses the pinned MAC directly — no `virsh domiflist` read-back). Per-VM state lands under `/var/lib/libvirt/images/lvlab/oneoff/<vm_name>/`. The standalone one-off deleter is the binary **`deletevm`** (originally ported from lvscripts' `deletevm`; the manifest-scoped counterpart is the `lvlab destroy` subcommand). `deletevm` does not read `Lvlab.yml` and does no name translation: it looks the libvirt domain up by the **exact raw name** passed, destroys + undefines it, and removes the one-off storage dir if present. A short manifest name like `web01` won't resolve (the real domain is `web01_<env>`), but passing a manifest VM's full `<vm_name>_<env>` domain name WILL remove it — its disks live nested under `<basedir>/<env>/<vm>/`, so they're left behind and the undefine is the operative effect.

`createvm` is `qemu:///system` only today (no `--uri`, no user-mode networking) — rootless `qemu:///session` + user-mode networking with host port-forwarding is a tracked follow-up that will also fix lvlab's existing user-mode path.

A run of `lvlab init` followed by `lvlab up <vm_name>` will:

1. Download and verify a cloud image (checksum + optional GPG of the checksum file).
1. Create a qcow2 disk that uses the cloud image as a backing file (via `qemu-img`).
1. Render cloud-init `meta-data`, `user-data`, and `network-config` from Jinja2 templates, pack them into a `cidata.iso` (built in-process with `pycdlib`), and attach it as a cdrom.
1. Shell out to `virt-install` to define and launch the domain.

Because state lives in libvirt + on-disk qcow2 files, **bugs here can damage real VMs the developer cares about.** Treat destructive paths (`destroy`, `down`, snapshot `delete`) with care — there is no separate test hypervisor.

## Build / dev / lint commands

This project uses [uv](https://docs.astral.sh/uv/) (PEP 517 backend: Hatchling, with `uv-dynamic-versioning` deriving the package version from git tags). CI runs pre-commit and the pytest matrix (Python 3.11/3.12/3.13/3.14) on every PR and push to `main`. Integration tests (`@pytest.mark.integration`) are gated by `LVLAB_INTEGRATION=1` and **never** enabled in CI — see `tests/conftest.py` and the "Integration tests" safety rules in the Testing conventions section below. Don't claim a behavior is "tested" because CI is green — that only covers what someone wrote a unit test for. For changes that touch `virsh`, also do a manual smoke test against `qemu:///session` before reporting done.

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

Common workflows are also wrapped as `just` recipes (run `just` to list them:
`test`, `test-cov`, `test-matrix`, `lint`, `test-safety`, `docs`, `docs-serve`,
`build`, `build-smoke`, `integration`, `integration-createvm`). Each recipe
just wraps the equivalent command above — see `justfile` and the createvm/deletevm
integration matrix notes in `docs-extra/CONTRIBUTING.md`.

Python floor is `>=3.11` in `pyproject.toml`. The release workflow builds with 3.13.

## Architecture

The code is organized around the `Lvlab.yml` manifest. Read `parse_config()` first — every command starts there.

`Lvlab.yml` has two top-level sections:

- `environment[0]` — exactly one environment with `name`, `libvirt_uri`, `config_defaults` (cpu/memory/disks/interfaces/cloud_init that apply to every machine), and a list of `machines`.
- `images` — a dict of named cloud images (URL + checksum + GPG + cloud-init network schema version).

### Core flow (cli.py → utils/)

`src/tkc_lvlab/cli.py` defines the Typer app + subcommands (tests use `typer.testing.CliRunner`). Every command follows the same shape:

1. `_load_config()` reads the manifest **once** and wraps it in a `ConfigManager`. `ConfigManager.from_parsed(parse_config())` is the seam CLI tests patch — they still patch the module-level `parse_config` and `Machine` at the `tkc_lvlab.cli` import boundary, so the consolidation helpers below all live **in `cli.py`** (a separate `services.py` would move the `Machine`/`parse_config` references out from under that seam and break every CliRunner test). `_load_config` maps both manifest-absence outcomes (missing file → `parse_config` returns `None`; structurally invalid → `ConfigError`/`TypeError`) to the same `logger.error` + `typer.Exit(1)`. `config.get_machine(vm_name)` finds the manifest entry; `config.as_tuple()` yields the legacy `(environment, images, config_defaults, machines)` unpacking for call sites that want it.
1. **Machine-scoped commands** (`destroy`, `down`, `snapshot create`/`list`/`delete`) share ONE prologue seam, `_resolve_machine(vm_name) -> ResolvedMachine | None`: load config → resolve manifest entry → construct `Machine` → probe libvirt, returning the `(machine, libvirt_uri, exists, state)` bundle. Commands that act only on an *already-defined* domain call the thin `_resolve_existing_machine()` wrapper (adds the not-deployed guard, collapses both non-fatal outcomes to `(None, None)`); `down` consumes the `ResolvedMachine` fields directly because it branches on state. `up` is the deliberate exception — it keeps its own prologue (needs the full parsed tuple for first-time create + pins a distinct not-found message), so it is **not** forced through the seam.
1. `Machine(machine_config, environment, config_defaults)` merges defaults into the machine and exposes operations against libvirt (it's now a thin facade over collaborators — see the `utils/libvirt.py` notes below).
1. For `up`, a `CloudImage` + `VirtualDisk` + `CloudInitIso` are constructed alongside the `Machine`.

**Error→`typer.Exit` boundary.** Library code raises the `LvlabError` hierarchy (`src/tkc_lvlab/exceptions.py`); only `cli.py` translates those into Typer exit codes. The standard pattern: manifest read/parse failures exit via `_load_config`; the libvirt *existence probe* failing (`VirshError` from `exists_in_libvirt`) is caught once in `_resolve_machine` → `Exit(1)` so no machine-scoped command leaks a traceback on an unreachable URI; narrower per-operation errors (`create_snapshot`/`delete_snapshot` raising `VirshError`, `cloud_init`/`deploy` raising `LvlabError`) stay caught in the individual command bodies, which carry an operation-specific message — `snapshot create`/`delete` log-and-exit-0 by design, the `up` cloud-init/deploy path exits 1. Do **not** convert library sentinel returns (`False`/`-1`/`None`) into raised exceptions — that "exceptions as control flow" scope was considered and declined.

`src/tkc_lvlab/utils/libvirt.py` — `Machine` is the central object. Key things to know:

- **`Machine` is a thin facade over collaborators, not a monolith.** Snapshot, destroy, and cloud-init logic were extracted into dedicated helper objects (`_DomainDestroyer`, the snapshot helper, the cloud-init renderer); `Machine.destroy()` / `list_snapshots()` / `cloud_init()` delegate to them via lazy `_get_*` accessors. Those accessors rebuild the collaborator on demand for test stubs created with `object.__new__` (which set `libvirt_vm_name` / `vm_name` / `config_fpath` but skip `__init__`). Keep new domain operations in the matching collaborator and expose them as a thin `Machine` method, rather than re-growing `Machine` itself.
- The libvirt domain name is **not** `vm_name`; it's `f"{vm_name}_{environment_name}"` (see `self.libvirt_vm_name`). This namespacing is what lets multiple lvlab environments coexist on one hypervisor. Anything that looks up a domain by name must use `libvirt_vm_name`.
- `Machine.__init__` merges `config_defaults` into the machine dict (interfaces, disks, and top-level keys). When adding a new configurable field, follow that same pattern instead of reading from `config_defaults` at call sites. `__init__` also validates `interfaces.network_type` (`network` / `user` / `passt`) and rejects the `network_type=user|passt` + static `ip4` combination — SLIRP/passt ignore static IPs, so the loud refusal happens at construction time before any state is created.
- `Machine.deploy()` shells out to `virt-install`. The `--os-variant` value comes from the resolved image entry — `utils/catalog.build_image_entry` derives it from the image key (split on `-`, first segment; hence custom images named `{os_variant}-{anything}`, see `docs/walkthrough.md` "Image Naming") **unless the `images:` entry sets an explicit `os_variant:`** (e.g. an `ubuntu2204` key pinning `ubuntu22.04`). `cli.py` passes `cloud_image.os_variant` into `deploy(..., os_variant=)`; `deploy` falls back to the `self.os` split only when none is passed. This resolution (and the first-boot `username`) is **shared with `createvm`** via `utils/catalog` — both paths honour the per-image overrides, and `cloud_init()` defaults `cloud_init.user` to the image's `default_username` when a machine omits it. The `--network` argument is built by `_virt_install_network_arg(iface)` near the top of `libvirt.py`: `network_type=network` (default) emits the managed-network form with a fixed PCI address; `network_type=user` / `passt` emit virt-install's user-mode forms (no libvirt network required) — required for `qemu:///session` where rootless libvirt cannot manage a NAT network. Every form also carries `mac=<iface.macaddress>` — `Machine.__init__` pins a generated MAC per interface (`generate_mac`, unless the manifest supplies one) so the `virt-install` arg and the cloud-init `match: macaddress` agree on the same address; see the v2 template note below for why MAC matching (not driver matching) is what makes the guest bind the right NIC. The same constants live in `utils/network.py` (`NETWORK_TYPES`, `USER_MODE_NETWORK_TYPES`); these drive only the manifest path's `interfaces.network_type` validation now — the standalone `createvm` dropped `--network-type` and is `qemu:///system`/managed-network only.
- `Machine.cloud_init()` is also where per-machine `runcmd` gets composed with the defaults (`runcmd_ignore_defaults: true` skips defaults), and where the manifest-wide `/etc/hosts` snippet is injected at the **top** of `runcmd` so it lands before anything that does DNS-ish work.

`src/tkc_lvlab/utils/cloud_init.py` — three dataclasses (`NetworkConfig`, `MetaData`, `UserData`) each render one Jinja template from `src/tkc_lvlab/templates/`. `CloudInitIso` uses `pycdlib` to build an ISO9660 + Joliet + Rock Ridge image with the three files at the names cloud-init's NoCloud datasource expects (`meta-data`, `user-data`, `network-config`). `UserData.__post_init__` will read an SSH public key from disk if `cloud_init.pubkey` looks like a path; otherwise it treats the value as a literal key.

`src/tkc_lvlab/utils/images.py` — `CloudImage` knows how to download, GPG-verify the checksum file, and checksum-verify the image. Two non-obvious bits:

- Debian's `SHA512SUMS` file is the **same filename** across releases, so Debian images get a per-image-prefix checksum filename to avoid clobber when multiple Debian versions are configured. The detector is a regex on `debian-(\d+)` in the image filename.
- The checksum file parser handles both Fedora's `SHA256 (file) = hash` format and Debian's `hash  file` format.
- When GPG verification succeeds, the verified plaintext is written to `<checksum>.verified` and subsequent operations prefer that file.

`src/tkc_lvlab/utils/vdisk.py` — `VirtualDisk` is a thin wrapper around `qemu-img create -b <cloud_image>` for qcow2 backing-file disks. One disk per entry in `machine.disks`, named `disk{index}.qcow2`.

`src/tkc_lvlab/config.py` — `parse_config()` (manifest loader) and `generate_hosts()` (renders `templates/hosts.j2`, used both for stdout output by the `hosts` command and for the in-VM `/etc/hosts` cloud-init snippet — see `heredoc` parameter for the dual-mode rendering).

### Templates

`src/tkc_lvlab/templates/` contains the Jinja2 templates loaded via `PackageLoader("tkc_lvlab")`. Hatchling ships every file inside the package directory (`src/tkc_lvlab/`, configured via `[tool.hatch.build.targets.wheel] packages = ["src/tkc_lvlab"]` in `pyproject.toml`), so new templates ship automatically — but verify with `unzip -l dist/*.whl | grep templates` after a `uv build` if you add one.

- `network-config.v1.j2` and `network-config.v2.j2` — selected by `image.network_version` (1 = ENI-style, 2 = netplan-style). The v2 template binds each NIC by `match.macaddress` (the per-interface MAC `Machine.__init__`/createvm pins and passes verbatim to `virt-install --network ...,mac=`), falling back to `match.driver: virtio_net` only for a MAC-less interface. Three load-bearing rules that are easy to undo by accident: **(1)** match by MAC, not driver or device name — MAC is the only selector cloud-init honours on *both* its netplan (Debian/Ubuntu) and NetworkManager (Fedora/RHEL) renderers; **(2)** the MAC is **quoted** in the template, or an all-numeric MAC is misparsed as a YAML 1.1 base-60 integer; **(3)** never `set-name`/rename — `iface.name` is only the netplan stanza label, never the in-guest device name (`enp1s0` / `ens3` / `eth0`); renaming leaves the NIC unconfigured under systemd-networkd. Full rationale (why driver/name matching silently failed on Fedora but not AlmaLinux) is in `docs/cloud-init-examples.md`. Multi-NIC manifests are not yet exercised end-to-end.
- `hosts.j2` renders both stdout-friendly output and a `cat <<EOF` heredoc form for runcmd injection.

### Host /etc/hosts handling inside the guest

`Machine.cloud_init` appends two heredocs to `runcmd`: one for `/etc/hosts` and one for the distro-appropriate `/etc/cloud/templates/hosts.{debian,redhat}.tmpl`. The template choice is a startswith-match on `self.os` against `template_file_mapping` (lowercased). If you add support for a new distro family, extend that mapping or `cloud_init()` will raise `ValueError`.

## Conventions and gotchas

- **Line length is 150** (`.pylintrc`). black is configured by default (88) via pre-commit; both are in effect — black formats, pylint just won't yell about long lines. If you see a line over 88, black either accepted it (string literal, URL, etc.) or it hasn't been run.
- **Type hints are required on new code; existing code is uneven.** See the "Documentation conventions" section below for the full rule. `docs-extra/Design.md` records that this used to be project-wide; the post-mkdocstrings policy supersedes that note for new work. Don't bulk-convert existing signatures as a side effect of an unrelated PR — the legacy-conversion sweep happened in 2026-05-23 as a focused effort, and ad-hoc neighbour conversion makes that history hard to read in git blame.
- The CLI mixes business logic into `cli.py` (e.g. orchestration of vdisk creation, ISO writing, deploy). When extending, prefer adding methods to the relevant `Machine` / `CloudImage` / etc. class rather than growing the command body.
- `parse_config()` is called repeatedly (e.g. once in the command, again inside `Machine.cloud_init` to regenerate the hosts list). Cheap because it's just a file read, but keep that in mind if you ever cache state.
- Several `destroy`/cleanup paths leave files behind on purpose or by oversight — see `docs/walkthrough.md`. Don't "fix" this without checking whether the user relied on it.
- A sibling project `lvscripts-py` (allowed via `.claude/settings.local.json`) was the original source for porting `createvm`/`deletevm` into this repo. **It is now being archived (2026-05-26); the port is complete and `createvm`/`deletevm` are canonical here.** Treat it as frozen historical reference only — fine to read, but it's not a sync target: don't propose mirroring lvlab's divergences upstream, and don't import from it.

## Documentation conventions

The project uses **Zensical + mkdocstrings** to generate API docs from
Google-style docstrings + type hints. Zensical is the Material-for-MkDocs
maintainer's MkDocs successor; it reads the existing `mkdocs.yml` directly.
Preview locally with:

```bash
uv sync --group dev
uv run zensical serve   # http://127.0.0.1:8000
# Strict build for CI-equivalent verification:
uv run zensical build -s
```

The site is configured in `mkdocs.yml` (Zensical reads it without a rewrite).
The `docs/` directory holds the published site files (`index.md`, `api/`).
Files that should NOT render in the public site live in the sibling
`docs-extra/` directory, which the doc-builder never scans.

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

### For existing code — leave alone

Existing docstrings are largely on the new convention (the legacy
conversion sweep completed 2026-05-23). The few free-form holdouts are
**not** sweep-convert targets in an unrelated PR — that mixes
unreviewable noise into a focused diff.

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
- Run on every developer's machine with `uv run pytest`, and unmodified
    in CI (the 3.11–3.14 matrix).

### Integration tests (opt-in only)

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

#### Test-naming and storage scaffolding

`tests/conftest.py` exports the per-session prefix and three helpers
that are the only sanctioned way to name and check test-owned
resources:

- `LVLAB_TEST_PREFIX` — generated once per session as
    `f"lvlab-test-{epoch_ms}-{random4}-"`. Epoch + short random avoids
    collisions across parallel runs.
- `make_test_name(base) -> str` — returns `f"{LVLAB_TEST_PREFIX}{base}"`.
    The only sanctioned way for tests to name a resource.
- `assert_owned_by_test(name) -> None` — raises if `name` does not
    start with `LVLAB_TEST_PREFIX`. Must be called before every
    destructive operation in test helpers. The runtime guard.
- Session-scoped teardown that runs `virsh list --all --name` *filtered
    by the prefix* and reaps any domains that survived a crashing test.
    **Never list all domains; only ones matching the prefix.**

Both the libvirt-domain name AND every on-disk path created by the test
must carry the prefix. The dedicated test storage root is
`/var/lib/libvirt/images/lvlab-test/` — exposed via the
`lvlab_integration_storage_root` session fixture. See `docs-extra/CONTRIBUTING.md`
"Integration test storage layout" for the storage conventions.

`tests/lint_test_safety.py` is an AST-based static check wired into
`.github/workflows/test.yml` that fails CI if any
`@pytest.mark.integration` function omits an `assert_owned_by_test()`
call. Complements the runtime guard.

## Supported host distros

The integration suite is exercised end-to-end against four host distros: **Debian 12** (bookworm, Python 3.11), **Debian 13** (trixie, Python 3.13), **AlmaLinux 10** (Python 3.12), and **Fedora 44** (Python 3.14). That set naturally covers our 3.11–3.14 support window via each distro's system Python. Out of scope: Debian 11, Ubuntu 22.04 (Python 3.10 < floor), Ubuntu 24.04 (dropped 2026-05-23), AlmaLinux 9 (same Python-floor reason), older Fedora.

`docs-extra/host-validation.md` holds the canonical matrix, the validation log, and the per-host procedure (`scripts/host-bootstrap.sh` for fresh hosts, then `scripts/run-validation.sh` for the unit + integration run). New-host work that touches the bootstrap or validation flow should keep that document in sync. The host matrix is manual-only — never wired to CI, since no shared runner can host the libvirt + qemu state these tests create.

## Branching

**Work directly on `main`.** This is a solo-maintained project and topic branches were adding friction without delivering PR-review benefits while the codebase has a single maintainer. Make focused, well-scoped commits directly on `main`. The user is the one who pushes, so an unintended local commit is recoverable with `git reset` before push — but that means the bar for commit quality on `main` is the same bar you'd apply to a PR head: each commit should stand on its own and pass `pre-commit run --all-files`.

Release tags on `main` still trigger `.github/workflows/build-release.yml`, so the "never push tags without an explicit, scoped request" rule (see "Git pushes" below) remains in effect — that's the actual guardrail against accidental releases.

Topic branches are still appropriate in two cases — **ask before creating one**, don't invent the name:

- A multi-commit experiment the user may want to discard wholesale.
- Work the user has explicitly asked be isolated for review or testing.

When a topic branch lands, prefer a fast-forward merge to preserve the individual commits; squash-merge only when the user asks for it.

## Spawning agents in worktrees

When the orchestrator spawns a subagent with `isolation: "worktree"`, the worktree is **not guaranteed to be branched from current `main` HEAD** — in practice it has come up branched from an older commit, which silently breaks anything that depends on recent test infrastructure, helpers, or policy changes.

**Rule for worktree-isolated agents:** the agent's first action must be to sync the worktree to current `main` HEAD before doing any other work. The orchestrator should put this literal step at the top of the prompt:

> Before doing anything else, run `git fetch origin main && git reset --hard origin/main` inside this worktree, then verify with `git log -1 --oneline`. Stop and report if the reset fails.

Alternatively the orchestrator can hand the agent the current `main` HEAD SHA in the prompt and require the agent to verify its worktree matches before starting. Either form is fine; the absence of any sync step is what fails.

For short, single-file changes that don't need a separate branch, prefer spawning the agent without `isolation: "worktree"` so it works directly on `main` — same risk profile as the orchestrator working on `main`, no stale-base trap.

## Git pushes

**Pushes via `gh`'s authenticated PAT are allowed when the user has asked for them**, scoped to the PAT's `contents:write` + `pull-requests:write`. The user normally pushes themselves from their own terminal (via the SSH remote); if the user asks you to push, use the HTTPS PAT path.

The remote `origin` is configured for SSH (`git@github.com:...`) but the gh PAT only authenticates HTTPS. Two consequences:

- For pushes, push to the HTTPS URL explicitly (`git push https://github.com/memblin/tkc-lvlab-py.git main`) — `gh auth git-credential` is wired into the global gitconfig and supplies the token. Don't rewrite `origin`; the user uses SSH from their own terminal.
- Fetches in this environment will also need the HTTPS URL (e.g. `git pull https://github.com/memblin/tkc-lvlab-py.git main`) because no SSH key here has read access.

**Still off-limits without an explicit, scoped request:**

- Force-push of any kind (`--force`, `--force-with-lease`) — `main` is the live branch, not a topic-branch sandbox.
- Pushing tags — tag pushes on `main` trigger `.github/workflows/build-release.yml` and cut a real GitHub release. See "Releasing".
- Pushes to `main` (the user pushes themselves; only push when explicitly asked).
- `gh pr merge`, `gh pr close`, branch deletion on the remote, or any write to issues/discussions/releases the user hasn't asked for.

## Releasing

Tagging `X.Y.Z` on `main` triggers `.github/workflows/build-release.yml`, which runs `uv build` and uploads the wheel to a GitHub release. The version is derived from the git tag by `uv-dynamic-versioning` (Hatchling backend) — there is **no `version` field in `pyproject.toml` to bump**; the tag name *is* the version. The release workflow checks out full history (`fetch-depth: 0`, already set) so the tag resolves to a clean version rather than a `…devN+g<hash>` fallback. Full procedure in `docs-extra/releases.md`.
