# TODO

Roadmap for the next few sessions. Phases are ordered by dependency, not strict
calendar. Phase 5 onward is blocked until the Claude session is restarted so
the `.claude/settings.local.json` grant for `lvscripts-py` takes effect.

______________________________________________________________________

## Phase 1 — Migrate Poetry → uv, refresh deps, add Python matrix ✅ COMPLETE

**Status:** landed in commits `1359e7f` (uv migration), `b6d1bff`
(supply-chain hygiene follow-up), `7a84e78` (pysonar dev dep). All 23
historical Dependabot alerts closed as a side effect of removing
`poetry.lock` + `requirements.txt`. The Python test matrix moved out to
its own Phase 3 — it depends on Phase 2 landing first so the conftest
doesn't have to mock around `libvirt-python`.

- [x] Use `uv_build` as the PEP 517 build backend.
    `requires = ["uv_build>=0.5,<0.12"]` with `build-backend = "uv_build"`.
- [x] Rewrite `pyproject.toml` to PEP 621:
    - [x] `[project]` with name/version/description/authors/readme and
        `requires-python = ">=3.11"`. License/classifiers skipped (no
        `LICENSE` file in repo yet — revisit later).
    - [x] `[project.dependencies]` and `[project.scripts]` populated.
    - [x] Poetry-only `include = [...]` dropped. Templates verified shipping
        via `unzip -l dist/*.whl | grep templates`. Layout config set
        under `[tool.uv.build-backend]` (`module-root = ""`,
        `module-name = "tkc_lvlab"`).
    - [x] `[dependency-groups]` `dev` table with `pytest`, `pytest-cov`,
        `pysonar`. Installed via `uv sync --group dev`.
- [x] `poetry.lock` removed, `uv.lock` generated and committed.
- [x] `requirements.txt` removed.
- [x] Dep floors refreshed; post-migration hardening raised
    `requests>=2.33` and `jinja2>=3.1.6` to lock in the Dependabot fixes
    against future fresh resolves. `libvirt-python` kept pinned (drops
    entirely in Phase 2).
- [x] `.github/workflows/build-release.yml` rewritten:
    - [x] `astral-sh/setup-uv@v3` + `uv build` replaces poetry install +
        `poetry build`.
    - [x] Dropped the `apt install libvirt-dev` step — `uv build` does not
        install runtime deps. Developers still need it until Phase 2 lands.
- [x] `README.md` install instructions updated to `uv tool install`
    (release path) and `uv sync && uv run lvlab` (dev path).
- [x] `CLAUDE.md` "Build / dev / lint commands" section updated to uv.
- [x] `pre-commit` verified working after migration.
- [x] `.github/dependabot.yml` added — uv weekly, github-actions monthly.

______________________________________________________________________

## Phase 2 — Replace `libvirt-python` with `virsh` subprocess calls ✅ COMPLETE

**Goal:** eliminate the `libvirt-python` C-extension dependency entirely.
lvlab already shells out to `virt-install`; mirror that pattern for every
other libvirt operation via `virsh -c <uri> ...`. This removes our biggest
supply-chain trap (no more `libvirt-dev` + `pkg-config` build requirement,
no more waiting for wheels on new Python versions) and unblocks Python 3.14
in the matrix.

Audit scope (the ~15 call sites we're replacing): every `libvirt.*` /
`conn.*` / `vm.*` / `snapshot.*` reference in `tkc_lvlab/utils/libvirt.py`
plus one stray `conn.getCapabilities()` in `tkc_lvlab/cli.py`.

### Implementation

- [x] Build a small subprocess wrapper at `tkc_lvlab/utils/virsh.py`:
    - [x] `run_virsh(uri, args, check=True, capture=True) -> CompletedProcess`
        — wraps `subprocess.run(["virsh", "-c", uri, *args], env={..., "LC_ALL": "C"})`
        so output is locale-stable.
    - [x] Custom `VirshError(returncode, stderr)` so callers can `except VirshError`
        without leaking `subprocess.CalledProcessError` everywhere.
- [x] Port the `Machine` methods in `tkc_lvlab/utils/libvirt.py`:
    - [x] `Machine.exists_in_libvirt` → `virsh list --all --name`, exact match.
    - [x] `Machine.destroy` / `Machine.shutdown` / `Machine.poweron` →
        `virsh destroy|shutdown|start <name>`.
    - [x] `Machine.list_snapshots` → `virsh snapshot-list <name> --name`.
    - [x] `Machine.create_snapshot` → `virsh snapshot-create <name> --xmlfile <path>`.
        **Sharp edge:** `--xmlfile` needs a real path. Use `tempfile.NamedTemporaryFile`
        (delete after) rather than relying on stdin redirection — virsh's stdin
        handling for `--xmlfile -` varies by version.
    - [x] `Machine.delete_snapshot` → `virsh snapshot-delete <name> <snapshot_name>`.
    - [x] `Machine.hasCurrentSnapshot` equivalent — `virsh snapshot-list <name> --name`
        and check non-empty (used in `destroy()` cleanup path).
    - [x] `capabilities` command (`cli.py:30-35`) → `virsh capabilities` stdout.
    - [x] `status` command (`cli.py:385-422`) → single `virsh list --all` parse,
        not one `domstate` call per declared VM (cuts startup overhead).
- [x] Replace dynamic state-constant reflection. Drop `get_machine_state`
    (libvirt.py:546) and `_humanize_machine_status` (libvirt.py:493) in
    their current form:
    - [x] `virsh domstate <name>` returns a human string: `running`, `idle`,
        `paused`, `in shutdown`, `shut off`, `crashed`, `pmsuspended`.
        Replace the integer-keyed dynamic dict with a hardcoded `str → human`
        map keyed on those exact strings.
    - [x] Use `virsh domstate <name> --reason` for the reason string. Build the
        analogous reason map.
    - [x] Audit every caller that compares against `"VIR_DOMAIN_*"` strings
        (`cli.py:126`, `cli.py:137`, `cli.py:443`, `cli.py:447`,
        `libvirt.py:301`, `libvirt.py:307`, `libvirt.py:440`, `libvirt.py:463`)
        and switch them to the new lowercase virsh state strings.
- [x] Drop `libvirt-python` from `[project.dependencies]` in `pyproject.toml`.
    Regenerate `uv.lock`. Update `CLAUDE.md`'s "Install deps" note — at
    build time we no longer need `libvirt-dev` / `pkg-config`, only
    `libvirt-clients` (or distro equivalent) at runtime.
- [x] After regenerating `uv.lock`, diff it against the previous lock and
    confirm the only changes are `libvirt-python` removal plus any
    transitives uniquely brought in by it. Surprise shifts in
    `requests` / `urllib3` / `idna` / `jinja2` / `cryptography` resolved
    versions should be investigated before merging — those are the
    packages Dependabot has historically flagged.
- [x] Update `CLAUDE.md` Architecture section: remove `libvirt-python` API
    references, document the `virsh` subprocess pattern and the
    `tkc_lvlab/utils/virsh.py` helper.
- [x] ~~Remove `continue-on-error: true` for Python 3.14 in the test workflow~~ —
    moot: no test workflow with a Python matrix existed at the time
    libvirt-python was dropped. When Phase 3 wires up the pytest CI matrix,
    Python 3.14 will be a first-class entry from day one.

### Standardize destructive-path UX (do it here, not as a separate pass)

We're rewriting all destructive code paths in this phase anyway. Land the
hardening at the same time so we don't churn these files twice.

- [x] `snapshot delete` gets a `--force` flag + confirmation prompt, mirroring
    `destroy` (already done in the Phase 0 incidentals commit, but verify
    consistency once the underlying call is virsh-based).
- [x] Every destructive command surfaces the actual `virsh` stderr on failure
    paths — no more silent "deletion failed" with no detail.
- [x] Audit `down` for whether it should consume the same `--force` semantics
    as `destroy` (decision pending).

### Phase 2 risks / sharp edges

- **Snapshot XML handoff:** confirm tempfile approach works on all target
    distros' `virsh` versions. Backup plan: keep XML as a Python heredoc string
    and write to a temp file inside `run_virsh`.
- **Locale:** force `LC_ALL=C` for every `virsh` invocation; output parsing
    depends on it.
- **Performance:** each `virsh` call pays subprocess startup overhead
    (~50ms). Fine for interactive CLI use; `status` should batch via a single
    `virsh list --all` rather than N `domstate` calls.
- **Error surface change:** existing code catches `libvirt.libvirtError`.
    After the port it catches `VirshError`. Don't broaden to bare `except Exception`.
- **Pre-port incidentals already landed:** dead `delete_vdisks()` removed,
    unused snapshot params cleaned, `snapshot delete` confirm/`--force` added
    in the pre-Phase-1 housekeeping commit. Don't re-do these.

______________________________________________________________________

## Phase 3 — Test infrastructure ✅ COMPLETE (scaffolding) — 2026-05-23

Scaffolding landed across three local commits on `main` (`defeea0`,
`3ec17bf`, `493a9cf`). The unit-test surface and CI matrix are live;
integration test **bodies** themselves are a follow-up effort.

- [x] Add `.github/workflows/test.yml`:
    - [x] Matrix: `python-version: ['3.11', '3.12', '3.13', '3.14']`.
    - [x] Job step: `uv sync --group dev && uv run pytest -m "not integration"`.
        (Used `--group dev` rather than `--all-extras` — `pyproject.toml`
        has no `[project.optional-dependencies]` table, only
        `[dependency-groups].dev`. Same effect.)
    - [x] 3.14 is a first-class entry, **not** `continue-on-error`.
- [x] Create `tests/conftest.py` with the safety scaffolding (see
    "Cross-cutting safety rules" below).
- [x] Seed a small set of pure-unit tests (no libvirt, no qemu-img):
    - [x] `parse_config()` happy / missing-file / bad-yaml cases.
    - [x] `parse_file_from_url()`.
    - [x] `CloudImage._parse_checksum_file()` — Fedora + Debian formats
        - `.verified` swap.
    - [x] `UserData._is_valid_ssh_public_key()`.
    - [x] `Machine.libvirt_vm_name` construction (`vm_name_environment`).
    - [x] `run_virsh()` — already covered in Phase 2 (47 tests, 100%
        coverage on `virsh.py`).
- [x] Marker registration: `@pytest.mark.integration` registered in
    both `tests/conftest.py` and `pyproject.toml`. Skipped by default;
    opt in with `LVLAB_INTEGRATION=1`. CI never sets it.

### Phase 3 follow-ups (not blocking)

- [ ] Write actual integration test bodies that exercise real `virsh` /
    `qemu-img`. The scaffolding is in place; the bodies are not.
    Cover at least: `Machine.deploy` happy path on `qemu:///session`,
    full `up`-`status`-`destroy` lifecycle, snapshot create/list/delete
    against a real domain.
- [ ] Add the lint/grep check that fails CI if any test calls
    `virsh destroy` / `virsh undefine` / `os.remove` on a name that
    didn't come from `make_test_name`. (See "Cross-cutting safety
    rules" below — the runtime guard exists; the static guard does
    not yet.)

**Suite as of Phase 3 completion: 132 passed, 1 skipped (integration). Coverage 42%.**

______________________________________________________________________

## Phase 4 — Documentation pass after uv + virsh migrations ✅ COMPLETE — 2026-05-23

- [x] `docs/Walkthrough.md` — full rewrite to match the current CLI
    (capabilities, cloudinit, destroy, down, hosts, init, snapshot,
    ssh-config, status, up) and current toolchain (virsh, pycdlib —
    no `libvirt-python`, no `genisoimage`).
- [x] `docs/CONTRIBUTING.md` — Tools section rewritten from Poetry to uv
    (sync/run/add/build), added a Unit tests subsection that documents
    `uv run pytest` and the `LVLAB_INTEGRATION=1` gate. Kept the
    branch-naming example and the manual end-to-end smoke checklist.
- [x] `docs/releases.md` — `poetry version` commands replaced with a
    direct `pyproject.toml` edit + `uv lock` regeneration. Workflow
    trigger note made explicit (tag-on-main → wheel build).
- [x] `README.md` Requirements section: `libvirt-python` C-extension
    reference dropped; apt install line refreshed to
    `libvirt-clients` + `virtinst` (no more `libvirt-dev` /
    `pkg-config`). Title widened to "Ubuntu 22.04 / 24.04".

### Phase 4 follow-ups (deferred to Phase 7)

- API reference pages (`docs/api/*.md`) and mkdocs nav entries for
    migrated modules belong to Phase 7 (legacy docstring + type-hint
    conversion). `tkc_lvlab.utils.virsh` was written to the new
    convention from day one; it's a natural first entry when Phase 7
    starts.
- `docs/Why.md` / `docs/Design.md` / `docs/Libvirt.md` / `docs/WIP.md`
    were skimmed; they are author-narrative or notes-style and don't
    carry stale toolchain claims. They stay in `exclude_docs` until
    individually modernized as part of Phase 7's user-facing-docs
    sweep.

______________________________________________________________________

## Phase 5 — Survey `lvscripts-py` ✅ COMPLETE — 2026-05-23

Output landed as [`docs/lvscripts-survey.md`](lvscripts-survey.md) (in
`exclude_docs`; internal planning artifact). It supersedes the older
scratch inventory at `/tmp/lvscripts-inventory.md`, which pre-dated
Phase 2 completion.

- [x] Read `lvscripts-py/CLAUDE.md` and `README.md` to understand intent.
    lvscripts is a Typer-based CLI that wraps host binaries to create
    one-off VMs against `qemu:///system`. Two console scripts: `createvm`,
    `deletevm`.
- [x] Inventory the public surface (`createvm <vm_name> <vm_distro>` with
    `--ip4/--memory/--cpu/--disk-size/--network/--public-key/--init-cloud-images/--config`;
    `deletevm <vm_name> [--force]`) and the module split (config /
    libvirt / cloud_init / cloud_images / ssh_keys / passwords /
    requirements).
- [x] Map functional overlap — both projects now use `virsh`; lvlab's
    image verification, in-process `pycdlib` ISO build, and backing-file
    qcow2 strategy are superior. lvscripts is ahead on SSH key
    discovery, password generation, network validation, DHCP lease
    polling, and dependency precheck with package-manager hints.
- [x] Identify lvscripts-exclusive capabilities — section 4 of the
    survey.
- [x] Per-feature disposition — section 5 of the survey. **Port:**
    one-off VM creation (Phase 6 deliverable), SSH key discovery,
    password generation, network validation, dependency precheck,
    snapshot-fallback in deletevm. **Adapt:** key-type whitelist
    (broaden lvlab's validator), config discovery seam, image-init
    bootstrap. **Skip:** `genisoimage` ISO build, `cp +qemu-img resize`
    disk strategy. **Leave to lvscripts:** their `libvirt.run()`
    wrapper (lvlab's is more robust now; lvscripts could adopt
    lvlab's pattern).

Recap of Phase 6 design questions that the survey resolved or sharpened:

- Namespacing: the `_oneoff` sentinel-environment idea from
    `/tmp/lvscripts-inventory.md` is **moot** — Phase 6 architecture
    (createvm/destroyvm are separate console scripts, no Lvlab.yml
    dependency) eliminated the environment concept entirely. Remaining
    sub-question: bare name vs `oneoff-<name>` prefix vs distinct URI
    routing.
- Storage path: standalone scripts should namespace under
    `/var/lib/libvirt/images/oneoff/<name>/` so they can't collide
    with lvlab manifest VMs sharing the default `disk_image_basedir`.
- Image verification: do NOT port lvscripts' download logic — use
    lvlab's `CloudImage` GPG/checksum path.
- Phase 9 reference: lvscripts' `commands/createvm.py` is a working
    Typer example to reread when Phase 9 starts.

______________________________________________________________________

## Phase 6 — Standalone `createvm` / `destroyvm` scripts ✅ COMPLETE — 2026-05-23

All six steps landed across six local commits on `main`:

- `6151d2e` step 1 — `tkc_lvlab/utils/{ssh_keys,passwords,requirements}.py` ports.
- `73943d2` step 2 — `tkc_lvlab/utils/network.py` (virsh net-dumpxml parser + validation + forward-mode policy).
- `fbc6033` step 3 — `tkc_lvlab/utils/standalone_cloud_init.py` + `user-data.oneoff.j2` / `meta-data.oneoff.j2`.
- `ec44d96` step 4 — `tkc_lvlab/scripts/createvm.py` (headline deliverable; `--copy` flag; cleanup-on-failure).
- `a7b11a4` step 5 — `tkc_lvlab/scripts/destroyvm.py` + `tkc_lvlab/utils/snapshot_cleanup.py` (`--children` → `--metadata` fallback; gated storage cleanup).
- step 6 (this commit) — README + `docs/Walkthrough.md` additions documenting the one-off workflow; TODO closeout.

**Suite: 248 passed, 1 skipped (integration).** Per-module 100% coverage on `ssh_keys`, `passwords`, `network`, `standalone_cloud_init`, `snapshot_cleanup`. 80% on the orchestrator `createvm.py` (uncovered paths are real-network image download — integration test deferred).

**Decision (locked):** `createvm` and `destroyvm` ship as **separate console
scripts with their own `[project.scripts]` entry points**. They are NOT
subcommands of `lvlab` (no `lvlab vm create`). They live in the same
distribution so the library code is shared, but the two CLIs are
intentionally separate at the command-line surface.

Outcome:

- `lvlab ...` stays focused on the manifest workflow (`Lvlab.yml`-driven
    groups of VMs, environments, defaults, lifecycle).
- `createvm` / `destroyvm` are for **one-off VM creation in the same
    environment that also runs lvlab**, without reading `Lvlab.yml` or
    touching lvlab-managed VMs.
- Both surfaces share `Machine` / `CloudImage` / `VirtualDisk` /
    `CloudInitIso` and any new helpers ported from lvscripts (SSH key
    discovery, network validation, etc.) via a clean public library API
    that doesn't know whether its caller is reading `Lvlab.yml` or accepting
    CLI args.

### Architecture (locked 2026-05-23)

- [x] **Library / CLI split.** Shared logic in `tkc_lvlab.utils.*` (and the
    higher-level `Machine` / `CloudImage` / `VirtualDisk` / `CloudInitIso`
    classes already there). Both the lvlab CLI and the standalone scripts
    depend on this surface; neither imports the other's CLI module.
- [x] **No cross-contamination.** `createvm` / `destroyvm` MUST NOT read
    `Lvlab.yml`, MUST NOT look up manifest-driven VMs, MUST NOT mutate
    lvlab's per-environment state directories. `lvlab` reciprocally MUST
    NOT enumerate or destroy domains that the standalone scripts created.
- [x] **Naming: `oneoff-<name>` prefix.** Standalone-script domains become
    `oneoff-<vm_name>` so they're trivially distinguishable from lvlab's
    `<vm_name>_<env>` (underscore-separated) names. Storage path:
    `/var/lib/libvirt/images/oneoff/<name>/`. Regression-guard test:
    `createvm testvm`, then `lvlab status` must not see it.
- [x] **Distribution shape.** Same wheel ships all three commands. While
    Phase 8 is deferred, entry points read:
    `toml     lvlab = "tkc_lvlab.cli:run"     createvm = "tkc_lvlab.scripts.createvm:run"     destroyvm = "tkc_lvlab.scripts.destroyvm:run"     `
    Phase 8 will rename to `tkc.lvlab.*` later.
- [x] **Layout ordering: Phase 8 deferred.** Phase 6 lands in the current
    `tkc_lvlab/` tree. Phase 8 picks up the new `scripts/*.py` files
    when its sweep runs.
- [x] **Disk strategy (locked 2026-05-23):** default = current
    `qemu-img create -b` backing-file behavior. Standalone `createvm`
    grows an opt-in `--copy`-style flag (name TBD) that flips to
    lvscripts-style `cp` + `qemu-img resize`, producing a standalone
    qcow2 with no cloud-images dependency. Lets the operator wipe and
    re-init `cloud-images/` without breaking VMs that were created with
    the flag. Manifest workflow (`lvlab up`) stays backing-file always.

### Implementation work

**Step 1 ✅ COMPLETE** (commit `6151d2e` on `main`, 2026-05-23) —
ported the three pure lvscripts helpers into `tkc_lvlab/utils/` as the
CLI-agnostic library foundation:

- `tkc_lvlab/utils/ssh_keys.py` — 7-type key whitelist incl. hardware sk-,
    home-walk discovery (including `$SUDO_USER`), validation, dedupe.
- `tkc_lvlab/utils/passwords.py` — 4-word phrase generator with mixed-case
    floor, `openssl passwd -6` shellout via stdin.
- `tkc_lvlab/utils/requirements.py` — `check_createvm_tooling()` with
    apt/dnf/zypper/pacman install hints. Required-binary set adapted to
    lvlab's reality (no `genisoimage`, no `cp` — pycdlib + qemu-img -b).
- 56 new tests across the three modules. No new pyproject.toml deps.

**Step 2** (next branch `phase6/02-network-validation`):

- [ ] Port lvscripts `libvirt.get_network_info()` →
    `tkc_lvlab/utils/network.py` (new module). Parse `virsh net-dumpxml`
    via stdlib ElementTree; expose `LibvirtNetworkInfo` dataclass with
    forward_mode, gateway_ip, netmask, dhcp_start, dhcp_end, and a
    `.subnet` property.
- [ ] Add `validate_static_ip(ip, info)` that rejects IPs outside the
    subnet AND inside the DHCP range.
- [ ] Forward-mode policy helper: NAT → derive gateway/DNS from the
    network XML; bridge → require explicit gateway/DNS at the call
    site or raise `LibvirtNetworkError`.
- [ ] Tests with `virsh net-dumpxml` XML fixtures for NAT and bridge
    networks, IPv4 boundary cases for `validate_static_ip`.

**Step 3** — cloud-init template adapter for standalone use (no manifest
context). Either generalize `tkc_lvlab.utils.cloud_init.UserData` to
accept an explicit dict (no `cloud_init.pubkey` indirection through a
Machine), or add a thin helper that constructs a UserData-compatible
dict from raw `createvm` arguments.

**Step 4** — `tkc_lvlab/scripts/createvm.py`:

- [ ] Click-based command. Typer migration is Phase 9 for the whole suite.
- [ ] Args: `vm_name`, `--distro`, `--memory`, `--cpu`, `--disk-size`,
    `--network`, `--ip4`, `--public-key`, `--copy` (disk strategy
    opt-in, see locked decision above), `--uri` (default
    `qemu:///system`).
- [ ] Calls `check_createvm_tooling()` first; then network validation;
    then SSH key discovery + optional `--public-key` merge; then
    password generation + hash; then cloud-init render; then disk
    create (backing-file by default, `cp`+resize when `--copy`);
    then `virt-install`; then optional DHCP-lease polling (NAT only).
- [ ] Domain name = `oneoff-<vm_name>` per locked naming. Storage path
    = `/var/lib/libvirt/images/oneoff/<vm_name>/`.
- [ ] `[project.scripts] createvm = "tkc_lvlab.scripts.createvm:run"`.

**Step 5** — `tkc_lvlab/scripts/destroyvm.py`:

- [ ] Click-based command, takes `vm_name` and `--force`.
- [ ] Translates the user-supplied name to `oneoff-<vm_name>` before
    touching libvirt — operator types `destroyvm testvm`, the script
    operates on domain `oneoff-testvm`.
- [ ] Snapshot fallback: lvscripts pattern (try undefine, on "has
    snapshots" failure delete them with `--children` or `--metadata`,
    retry undefine). Lift into `tkc_lvlab/utils/libvirt.py` as a
    helper so `Machine.destroy` can use it too.
- [ ] `[project.scripts] destroyvm = "tkc_lvlab.scripts.destroyvm:run"`.

**Step 6** — tests + docs:

- [ ] Regression-guard integration test: `createvm` a oneoff and verify
    `lvlab status` does NOT see it; reciprocally a manifest VM stays
    invisible to `destroyvm` lookups.
- [ ] Tests for both surfaces share the `LVLAB_TEST_PREFIX` safety
    fixture so the session-scoped reaper covers oneoff resources.
- [ ] Docs: extend `README.md` and `docs/Walkthrough.md` with the one-off
    workflow. Explain when to use `lvlab` vs `createvm`/`destroyvm` —
    including the explicit "they don't see each other's VMs" property.

______________________________________________________________________

## Cross-cutting safety rules (apply to every phase that writes tests)

**Hard rule: no test, fixture, or teardown step ever touches a libvirt
domain, qcow2 file, or ISO whose name does not start with the per-run test
prefix.** Existing developer VMs on the same hypervisor must be invisible to
the test suite.

- [ ] `tests/conftest.py` exports:
    - [ ] `LVLAB_TEST_PREFIX` — generated once per session:
        `f"lvlab-test-{epoch_ms}-{random4}-"` (epoch + short random to avoid
        collisions across parallel runs).
    - [ ] `make_test_name(base) -> str` — the only sanctioned way for tests to
        name a resource. Returns `f"{LVLAB_TEST_PREFIX}{base}"`.
    - [ ] `assert_owned_by_test(name) -> None` — raises if `name` does not
        start with `LVLAB_TEST_PREFIX`. Called before every destructive op
        in test helpers.
    - [ ] A session-scoped teardown that runs `virsh list --all --name`
        *filtered by the prefix* and reaps any that survived a crashing test.
        **Never list all domains; only ones matching the prefix.**
- [ ] Same prefix applies to:
    - [ ] On-disk paths (`disk_image_basedir` for tests must be a temp dir,
        not the developer's shared `~/.local/lvlab/...`).
    - [ ] Cloud-init ISOs and the per-VM config directory.
- [ ] Add a lint/grep check (or pytest plugin) that fails CI if a test calls
    `virsh destroy` / `virsh undefine` / `os.remove()` on a name that
    didn't come from `make_test_name`.
- [ ] Integration tests **must** use a dedicated `libvirt_uri` or at least a
    dedicated network and storage pool so cleanup can be scoped further.

______________________________________________________________________

## Phase 7 — Legacy docstring + type-hint conversion (COMPLETE — 2026-05-23)

API reference pages for every already-converted module landed first
(commit `1228c85`), then per-module conversions.

Status:

- [x] `tkc_lvlab/_logging.py` — already conformant before Phase 7;
    added to API ref in `1228c85`.
- [x] `tkc_lvlab/config.py` — converted in `a5957ba`.
- [x] `tkc_lvlab/utils/vdisk.py` — converted in `6cbd74a`.
- [x] `tkc_lvlab/utils/images.py` — converted in `4ac789e`.
- [x] `tkc_lvlab/utils/cloud_init.py` — converted in `2398d96`.
- [x] `tkc_lvlab/utils/libvirt.py` — converted in `5acfd0a`. The
    Phase 2-ported methods (`virsh_*` callers — `exists_in_libvirt`,
    `destroy`, `poweron`, `shutdown`, list/create/delete snapshot)
    already carried the new convention; this commit added it to the
    pre-Phase-2 methods (`__init__`, `cloud_init`, `create_vdisks`,
    `deploy`) plus the `get_machine_by_vm_name` helper.
- [x] `tkc_lvlab/cli.py` — converted. Docstrings stay short (one-line
    summary + 1-3 sentence narrative) to keep `lvlab --help` output
    clean — Click renders the full docstring verbatim, so the
    Google-style `Args:` / `Raises:` sections would bleed in. The
    signature type hints are the source of truth for mkdocstrings;
    per-argument doc lives in the `@click.option(..., help="...")`
    decorators.

After each module is converted, remove its name from the "Modules pending
migration" list in `docs/api/index.md` and add the corresponding
`docs/api/.../*.md` page that does `::: tkc_lvlab.<dotted.path>` for
mkdocstrings to pick up. Add it to `nav` in `mkdocs.yml`.

The legacy user-facing docs (`docs/Walkthrough.md`, `Design.md`, `Why.md`,
`Libvirt.md`, `CONTRIBUTING.md`, `releases.md`, `WIP.md`,
`Lvlab.example.yml`, `cloud-init.examples/`) are in `exclude_docs` in
`mkdocs.yml` — they don't render in the site yet. Move them into nav (and
out of `exclude_docs`) as each one gets its turn at modernization. That's a
separate effort from the docstring conversion.

______________________________________________________________________

## Phase 8 — Repo restructure: src-layout (COMPLETE — 2026-05-23)

**Scope (2026-05-23 revision):** move the package into a `src/`
directory to align with modern PEP 621 / uv convention. **No
namespace migration** — the import name stays `tkc_lvlab`. This is
purely a directory move plus a handful of config edits; no Python
code or tests change.

### Why src-layout (and not just leaving it at the repo root)

- The PEP 621 / uv-recommended layout is `src/<pkg>/...`, which
    prevents accidental imports of the repo-root copy when running
    tests against an installed wheel — a real "this worked locally
    but failed on PyPI" hazard.
- Aligns with the convention used by every modern Python project the
    user will compare against; lowers cognitive friction for anyone
    landing in the repo for the first time.

### Why not the `tkc.lvlab` namespace (decision 2026-05-23)

The earlier plan paired the src-layout move with a PEP 420 namespace
migration (`tkc.lvlab`). That half is **dropped**. Two findings from
the
[PyPI namespace-packages guide](https://packaging.python.org/en/latest/guides/packaging-namespace-packages/)
and the
[uv_build namespace-packages docs](https://docs.astral.sh/uv/concepts/build-backend/#namespace-packages)
made the cost/benefit unfavorable:

1. A `tkc-core` distribution shipping a shared `tkc/__init__.py` to
    hold helpers **cannot also be namespace-compatible** — the PyPI
    guide is explicit that any code in a `pkgutil.extend_path`
    `__init__.py` is "inaccessible." Cross-distribution sharing has
    to go through a regular sub-package like `tkc.core.helpers`
    anyway, which works just as well as a sibling `tkc_shared`
    distribution.
1. The PyPI guide explicitly endorses the prefix-instead-of-namespace
    pattern: *"A simple alternative is to use a prefix on all your
    distributions such as `import mynamespace_subpackage_a` — this
    avoids namespace package complexity entirely."* `tkc_lvlab` /
    `tkc_lvscripts` / future `tkc_shared` is that pattern; it's not
    a workaround.

The namespace's only delta would be import aesthetics
(`from tkc import lvlab` vs `import tkc_lvlab`); not worth claiming
a top-level name we can't easily back out of.

### Work this implies

This is much smaller than the old plan now that imports don't
change. The wheel filename is also unchanged
(`tkc_lvlab-X.Y.Z-py3-none-any.whl`) because `[project] name` and
the module name both stay `tkc-lvlab` / `tkc_lvlab`.

- [x] `git mv tkc_lvlab src/tkc_lvlab`. 26 files relocated, git
    tracked them all as 100%-similarity renames.
- [x] `pyproject.toml`:
    - [x] `[tool.uv.build-backend] module-root`: `""` → `"src"`.
        Wheel still ships `tkc_lvlab/...` flat (confirmed with
        `unzip -l dist/*.whl`).
    - [x] No `include` glob needed — `uv_build` auto-includes
        every file under the module root. Pre-existing CLAUDE.md
        claim about an include glob was a stale doc bug; fixed in
        the same commit.
    - [x] `[project.scripts]` unchanged — still `tkc_lvlab.cli:run`
        etc.
- [x] `sonar-project.properties`: `sonar.sources=tkc_lvlab` →
    `sonar.sources=src/tkc_lvlab`.
- [x] `[tool.coverage.run]` / `[tool.pytest.ini_options]`:
    unchanged — `source = ["tkc_lvlab"]` and `--cov=tkc_lvlab` use
    the import name and resolve to the new path via Python's
    import machinery.
- [x] `mkdocs.yml` mkdocstrings handler: added `paths: [src]` so
    the handler finds the package source.
- [x] `CLAUDE.md`: 8 file-path references bumped throughout. While
    here, fixed a pre-existing doc bug claiming pyproject.toml has
    an `include = ["tkc_lvlab/templates/*.j2"]` glob (no such
    entry exists; uv_build auto-includes).
- [x] `.github/workflows/build-release.yml`: scanned, no path
    changes needed. The only `tkc_lvlab` references are the wheel
    filename (`tkc_lvlab-${{ github.ref_name }}-py3-none-any.whl`,
    unchanged) and the cosmetic step name.
- [x] Verified: `uv build` + `unzip -l` shows templates ship;
    `uv run lvlab --help` renders identically; `uv run pytest -q`
    → 260 passed, 1 skipped (coverage paths now show
    `src/tkc_lvlab/...`); `uv run mkdocs build --strict` clean;
    `uv run pre-commit run --all-files` clean.

Landed in commit `74b472e` on branch `main.phase8_src_layout`.

### Risk flags (much smaller now)

- The wheel filename is unchanged — no release-notes asterisk needed.
- No import rewrites means no `from tkc_lvlab...` → `from tkc.lvlab...`
    churn across the test suite or `docs/api/.../*.md` mkdocstrings
    directives. Tests should pass without modification.
- The only user-visible side effect would be downstream tooling that
    has hard-coded the `tkc_lvlab/` path at the repo root rather
    than the import. Unlikely for this project — verify with a
    `grep -rn 'tkc_lvlab/' .github/ docs/ scripts/` sweep during
    the move.

### When to schedule

After the open Phase 9 smoke-test follow-ups are addressed and any
in-flight catalog refresh has landed. Combine it with the
`pyproject.toml` version bump for the next release if convenient,
so the layout move and the version bump ride the same wheel
rebuild. Don't combine with anything else that touches imports or
test files.

______________________________________________________________________

## Phase 9 — Migrate CLI from Click to Typer (COMPLETE — 2026-05-23)

Done before Phase 8 because the UX-preservation contract was simpler to
verify against a stable layout. Phase 8 (src-layout) can now slot in
without dragging two refactors together.

**Hard constraint: preserve current user experience.** Every command name,
positional argument, option name, default value, exit code, and visible
behavior must stay identical. Users who scripted against `lvlab` today
should not need to change a single character. The migration is a refactor
under the hood, not a CLI redesign.

### Why

Typer is a thin layer over Click that:

- Drives argument parsing from type hints, which aligns with the
    Phase 7 docstring + typing convention already in flight.
- Reduces decorator boilerplate (`@click.option(...)` repeated once per
    parameter becomes a typed function-arg default).
- Gives us richer `--help` output (Rich-rendered) and shell-completion
    generation as freebies — opt-in, not forced.
- Plays nicely with mkdocstrings since command functions look like
    normal typed Python functions, not stacks of decorators.

### What landed

- [x] Added `typer>=0.12` to `[project] dependencies`.
- [x] Replaced `@click.group()` on `run` with `app = typer.Typer(...)`
    plus `context_settings={"help_option_names": ["-h", "--help"]}`
    and `no_args_is_help=True`.
- [x] Replaced every `@click.command()` with `@app.command()` /
    `@snapshot_app.command()`.
- [x] Replaced `@click.argument` with typed positional parameters.
    Optional `snapshot_description` is
    `snapshot_description: str = typer.Argument(None)`. `ssh-config`
    keeps its hyphenated name via `@app.command("ssh-config")`.
- [x] Replaced `@click.option` with typed parameters carrying
    `typer.Option(default, "--flag", help="...")`.
- [x] Replaced verbosity flags with
    `verbose: int = typer.Option(0, "-v", "--verbose", count=True)`
    and `quiet: bool = typer.Option(False, "-q", "--quiet")` on
    `_root`, the `@app.callback()`. `configure_logging` runs before
    every subcommand body.
- [x] Replaced `click.echo` / `click.confirm` with `typer.echo` /
    `typer.confirm`. Error output keeps `err=True`.
- [x] Snapshot subgroup: `snapshot_app = typer.Typer(...)` +
    `app.add_typer(snapshot_app, name="snapshot")`. Backwards-compat
    aliases `run = app` and `snapshot = snapshot_app` keep the
    pyproject entry-point and test imports working unchanged.
- [x] **Help-text:** Rich rendering left enabled (lvscripts uses the
    same default look). `--help` output preserves command names,
    positional argument names, option names and defaults, and the
    short summary line. Long-form Click help text bodies are kept as
    docstrings on the function — Typer + Rich render them in a
    cleaner panel layout but the content is unchanged.
- [x] **Exit codes:** failure paths now `raise typer.Exit(code=1)`
    where the Click code called `sys.exit(1)`. `hosts` and `init`'s
    bare `sys.exit()` (no code) is preserved as a known quirk noted
    in earlier phases.
- [x] **Tests:** `click.testing.CliRunner` does NOT work directly
    against a Typer instance (no `.name` attribute), so the three
    affected tests (`test_cli_capabilities.py`, `test_cli_status.py`,
    `test_cli_snapshots.py`) switched to `typer.testing.CliRunner`.
    `test_cli_capabilities` and `test_cli_status` now invoke via the
    full `app` (`runner.invoke(app, ["status"])`), which triggers the
    `_root` callback and `configure_logging`. To keep `caplog` working
    across tests, a new autouse fixture in `tests/conftest.py`
    restores `tkc_lvlab` logger's `propagate=True` after every test
    (otherwise the `configure_logging`-set `propagate=False` would
    leak into later tests).
- [x] **CLAUDE.md update**: "What this is" section + the cli.py
    Architecture note both call out the Typer migration. `docs/index.md`
    and `docs/api/{cli,index}.md` updated to match.
- [x] Manual smoke test (2026-05-23, no-VM path). Verified clean on
    the manifest in the repo (`qemu:///system`, 4 undeployed
    machines): `lvlab --help`, `capabilities`, `status`, `hosts`,
    `hosts --heredoc`, `ssh-config`, `ssh-config <vm>`, `snapshot`
    (`--help` for the group + every subcommand), `snapshot list <vm>`
    against a not-deployed VM. All exit 0; output matches the Click
    UX modulo Typer/Rich panel rendering.
- [x] Manual smoke test the destructive happy path
    (2026-05-23, second pass on `podman01.tkclabs.io` with sudo +
    libvirt-group access). Both standalone (`createvm smoke.test   --distro debian13` → `destroyvm smoke.test --force`) and
    manifest (`lvlab up vault.local` → `status` → `snapshot   create/list/delete` → `destroy --force`) round-trips
    succeeded end-to-end. The smoke run surfaced the following
    issues, addressed in the follow-up section below:
    - **Real bugs (fixed):** `UserData._is_valid_ssh_public_key`
        regex rejected multi-word comments AND
        `UserData.__post_init__` crashed on the validator's
        bare-`False` miss return — both fixed in commit `bbf6141`.
    - **Stale catalog (fixed):** `BUILTIN_IMAGES["fedora40"]` URL
        returned a 404 HTML page; the checksum-verification path
        defensively rejected it but the catalog itself was broken.
        Dropped in commit `626f272`.
    - **Defenses-in-depth that fired correctly (positive
        validations):** `requirements.py` precheck caught missing
        `virt-install` and gave the exact install command;
        checksum verification refused the 404 HTML; SSH-key
        discovery refused to create a VM with no login keys.

### Smoke-test follow-ups (Typer UX deltas vs. Click)

Three Typer UX deltas surfaced in the 2026-05-23 smoke test. Each
is a deliberate decision — neither blocking nor a bug, just a
delta from Click. Recording so we make the call rather than
drifting:

- [x] **`--install-completion` / `--show-completion` are new options
    on every Typer command.** Decided 2026-05-23: **leave on**. Low
    cost, modern UX feature; matches lvscripts-py. To suppress later
    if needed: pass `add_completion=False` to the top-level
    `typer.Typer(...)`.
- [x] **Help output uses Rich Unicode panel boxes** (`╭ │ ╰`).
    Decided 2026-05-23: **leave Rich enabled**. Matches lvscripts-py
    - every other modern Typer CLI; content is preserved. Revisit
        only if a user reports terminal issues (piping to `less` without
        `-R`, etc.); suppress with `rich_markup_mode=None`.
- [x] **`[default: 0]` shown for the `-v` count flag.** Decided
    2026-05-23: **accept as-is**. Slightly noisy but informative;
    no-op fix.

### Smoke-test follow-ups (other findings, deferred for review)

The 2026-05-23 destructive smoke test also surfaced these items.
None are blockers for the next release — but each represents a
real UX or documentation gap worth tracking so we don't drift.

- [x] **`cloud_image_basedir` doubles `/cloud-images/` internally** —
    fixed 2026-05-23. `CloudImage.__init__` now does a tail-aware
    append: if the configured basedir already ends in `cloud-images`,
    it's used as-is; otherwise the `/cloud-images` suffix is appended.
    Both behaviors are now intentional (legacy parent-dir style still
    works; pointing at an existing cache dir also works without
    doubling). Docstring updated to call out both shapes. Regression
    tests under `tests/test_images_basedir.py` pin all 4 cases
    (default basedir, parent-dir style, already-cache style, trailing
    slash, ~ expansion).
- [x] **Stale-group-session footgun after `usermod -aG libvirt`** —
    documented 2026-05-23. `docs/CONTRIBUTING.md` now has a "Host
    setup" subsection under "End-to-End Testing" covering the
    one-time `sudo usermod -aG libvirt` step, the re-login
    requirement, and the `sg libvirt -c "..."` workaround for
    single-command use without re-login. Smoke-test failure mode
    (`PermissionError` on `/var/lib/libvirt/images/`) is called
    out so it doesn't look like a code bug.
- [x] **Refresh the catalog with a current Fedora release + audit
    pinned Debian dated paths.** Closed by the
    `refresh-cloud-images` skill run on 2026-05-23:
    - Added `fedora44` (`Fedora-Cloud-Base-Generic-44-1.7.x86_64.qcow2`)
        to `BUILTIN_IMAGES` + both YAML manifests.
        `os_variant=fedora44`, `default_username=fedora`,
        `network_version=2`, GPG
        `https://fedoraproject.org/fedora.gpg`.
    - Bumped `debian12` and `debian11` dated paths
        (`20240717-1811` → `20260518-2482`) across the three-file
        catalog. `debian13` continues to use `trixie/latest/`
        (stable redirect path, no URL change needed).
    - Removed `debian10` from `docs/Lvlab.example.yml`. No new
        cloud-image builds since 2024-07; Debian 10 is LTS-only
        and the entry was at risk of becoming the next fedora40-
        style stale paste source.
    - `forky` (debian14) noted as not-yet-PROPOSE-ADD — upstream
        directory exists but holds only `daily/`, no `latest/` or
        stable dated builds yet. Next refresh run will re-check.

### Follow-up: migrate the standalone scripts to Typer too (COMPLETE — 2026-05-23)

Landed in two commits on `main`:

- `15e89b6` — destroyvm port. Single-command Typer app + the same
    `_fail()` helper pattern. UX preserved (positional + 3 options,
    same defaults, same confirmation-prompt routing to stderr).
    `tests/test_destroyvm.py` switched to `typer.testing.CliRunner`;
    6 tests pass unchanged.
- `b79aee0` — createvm port. Single-command Typer app, 10 options +
    positional, full Typer Option/Argument signatures. UX preserved:
    `--distro` keeps `case_sensitive=False` via
    `click_type=click.Choice(...)` (the only reason this module
    still imports click); `--public-key` keeps `exists=True,   dir_okay=False`; `--copy` keeps the Python-param alias
    (`copy_strategy`). Error reporting refactored to a `_fail()`
    helper covering every prior `click.ClickException`.
    `tests/test_createvm.py` switched to `typer.testing.CliRunner`;
    16 tests pass unchanged.

Done outcomes:

- [x] `tkc_lvlab/scripts/createvm.py` — Typer app with 10 options +
    positional. `click.Choice(case_sensitive=False)` preserved via
    `click_type=`. `--copy` → `copy_strategy` alias clean.
- [x] `tkc_lvlab/scripts/destroyvm.py` — Typer app, 3 options +
    positional. `typer.confirm(..., err=True)` for the prompt.
- [x] `tests/test_createvm.py` (16) and `tests/test_destroyvm.py` (6)
    switched to `typer.testing.CliRunner`. Both files still import
    `run` thanks to the `run = app` backwards-compat alias.
- [x] `click.ClickException` raises replaced everywhere with a
    `_fail()` helper that mirrors Click's `Error: <msg>` stderr
    format + exit 1 behavior. Internal helpers (`_run_subprocess`,
    `_create_disk`, `_virt_install`, `_ensure_image_available`) use
    `_fail()` too so the format stays consistent.
- [x] CLAUDE.md "What this is" updated — no longer says "the
    standalone one-off scripts still use Click directly".

Suite: 248 passed, 1 skipped. `uv run createvm --help` +
`uv run destroyvm --help` both render Typer-styled panels with every
option/default preserved.

### Optimization opportunities (do only if they don't shift UX)

- Replace ad-hoc `parse_config()` + `get_machine_by_vm_name()` boilerplate
    at the top of every subcommand with a Typer `Depends`-style helper
    (Typer doesn't have native DI, but a simple callable injected as a
    default works). Saves ~10 lines per subcommand.
- Enable shell completion (`lvlab --install-completion`) as an opt-in
    flag. Don't auto-install.
- Use `typer.style` / `rich` for `lvlab status` color formatting —
    **only** if the user opts in via `--color/--no-color` or a config
    flag. Default must remain plain to match today.

### When to schedule

- **After Phase 8 (src-layout)** is the natural slot — Phase 8 rewrites
    every import path anyway, and Typer's callback semantics are
    sensitive to module structure. Doing both at once would tangle two
    unrelated diffs; doing Phase 9 right after means a clean rebase on
    the new layout.
- **Not during Phase 2/3** — too much CLI surface still moving.

### Risk flags

- **Help-text drift is the easiest way to break UX without noticing.**
    Snapshot `--help` output for every command before/after and diff it
    in the PR description.
- **`typer.Argument` vs `typer.Option` defaulting for positional with default**:
    Click allows `@click.argument(..., default=None, required=False)` cleanly.
    Typer is stricter about Optional. Confirm
    `lvlab snapshot create <vm> <snap>` (without description) still works.
- **`no_args_is_help=True`** behavior differs from Click's group default
    — Click shows help with exit 0; Typer with this flag also exits 0
    but the message routing changes. Test with `uv run lvlab` (no args)
    and confirm exit code + stderr/stdout split matches today.
- **Rich rendering may swallow ANSI in some terminals** (e.g. piped to
    `less` without `-R`). Disable with `rich_markup_mode=None` if any
    user-reported regression.

______________________________________________________________________

## Phase 10 — Cut the next release (version target paused — 2026-05-23)

**Status:** the 2026-05-23 destructive smoke test surfaced two real
bugs (now fixed in commits `bbf6141` and `626f272`) plus several
follow-up items captured in the "Smoke-test follow-ups (other
findings, deferred for review)" section above. Per the user's call,
the previously-queued 1.0.0 target is **paused** — we'll work out
the version number later, once the follow-ups have been reviewed
and either addressed or explicitly waived. The pre-tag gate and
bump procedure documented below still applies whenever the version
target lands, whether that's 1.0.0 or another number (0.3.0 is also
a viable signal for "substantial migration work, some loose ends
acknowledged").

The post-0.2 work landed a lot of substantive change:

- Phase 2 removed `libvirt-python` (the C-extension dependency).
- Phase 3 wired up the pytest matrix + the test-prefix safety
    scaffolding.
- Phase 4 rewrote the user-facing docs.
- Phase 5 surveyed `lvscripts-py` into a docs/lvscripts-survey.md
    artifact.
- Phase 6 added the standalone `createvm` / `destroyvm` console
    scripts.
- Phase 7 converted every legacy module to Google-style docstrings +
    type hints.
- Phase 9 (and its follow-up) ported the entire CLI surface — both
    `lvlab` and the standalone scripts — from Click to Typer with
    UX preservation.

Interfaces are stable, the suite is green (260 passed, 1 skipped as
of the 2026-05-23 catalog refresh), mkdocs `--strict` builds clean.

### Pre-tag gate (do all of these before bumping the version)

- [x] **Destructive smoke test** — completed 2026-05-23 on
    `podman01.tkclabs.io`. Both manifest (`lvlab up vault.local` →
    `status` → `snapshot create/list/delete` → `destroy --force`)
    and standalone (`createvm smoke.test --distro debian13` →
    `destroyvm --force`) round-trips landed cleanly. Findings are
    captured in the "Smoke-test follow-ups (other findings,
    deferred for review)" section above; the two real-bug findings
    are already fixed (commits `bbf6141` and `626f272`).
- [ ] **Tag-name dry run**: open `.github/workflows/build-release.yml`
    and confirm the wheel filename glob (`tkc_lvlab-${{ github.ref_name }}-py3-none-any.whl`) still matches what `uv build` produces. Should
    be a no-op since we're staying on the prefix layout and the
    distribution name (`tkc-lvlab`) isn't changing — but verify with
    a `workflow_dispatch` against an arbitrary test tag (e.g.
    `0.99.0-rc1`) if you want belt-and-suspenders confidence.
- [ ] **Suite + docs final pass** on the candidate commit:
    `uv run pytest -q`, `uv run mkdocs build --strict`,
    `uv run pre-commit run --all-files`.

### Bump procedure

Per `docs/releases.md` — the process is already documented; the
1.0.0 bump just follows it:

- [ ] Edit `pyproject.toml`: `version = "0.2.4"` → `version = "1.0.0"`.
- [ ] `uv lock` to refresh the lockfile against the new version.
- [ ] PR the version bump in a small, focused commit (no other
    changes — keeps the release commit auditable).
- [ ] Merge to `main`.
- [ ] Pull `main` locally, create tag `1.0.0`, push it. The tag
    push (matching the `[0-9]+.[0-9]+.[0-9]+` glob in
    `.github/workflows/build-release.yml`) triggers the build +
    GitHub Release with `gh release create ... --generate-notes`.

### Release notes — what to surface

`--generate-notes` will summarize merged PRs since the previous tag.
Worth adding a hand-written summary at the top of the release body
covering the post-0.2 themes:

- **Goodbye libvirt-python**: no more `libvirt-dev` / `pkg-config`
    build prereq. `uv sync` works on any host with `virsh` available
    at runtime.
- **New standalone scripts**: `createvm` and `destroyvm` (one-off
    libvirt VMs, no `Lvlab.yml` required).
- **CLI is Typer-based** (UX preserved char-for-char, but the
    rendered help is Rich-panel styled now).
- **Full Google-style docstring + type-hint coverage** — first
    release where mkdocstrings can render the entire API surface.
- **Python matrix**: 3.11 / 3.12 / 3.13 / 3.14 on CI.

### Risk flags

- **Tag push is live the moment it lands**. There is no
    "draft release" gate in the current workflow — push tag, build
    runs, release is published with the wheel asset. If anything's
    wrong (e.g. version mismatch between tag and `pyproject.toml`),
    catch it BEFORE pushing the tag. The mismatch failure mode is
    silent — the wheel builds with the pyproject version, the
    release is created with the tag name, and the wheel filename
    won't match the release body. Recoverable but ugly.
- **No PyPI publish in the workflow today** — releases are
    GitHub-only artifacts. `uv tool install` from a Git URL or a
    direct wheel URL still works; `uv tool install tkc-lvlab`
    against PyPI does **not** (there's no PyPI publish step). If
    that's a 1.0 expectation, it needs to be added to the workflow
    as a separate prerequisite item.
- **`--no-verify` is off-limits for the version-bump commit** per
    project rules. Pre-commit's mdformat / black hooks have caught
    real issues before; let them run.

______________________________________________________________________

## Decisions log

1. ~~Build backend~~ — decided: `uv_build`. No hatchling, no setuptools.
1. ~~Python floor~~ — decided: drop 3.10, `requires-python = ">=3.11"`.
    Phase 2 makes this consequence-free since libvirt-python is gone.
1. ~~One-off VM namespacing~~ — decided in Phase 2 design doc
    (`/tmp/phase2-design.md` §5): sentinel environment `_oneoff`.
1. ~~Whether to keep `createvm` / `destroyvm` as separate console_scripts~~ —
    decided 2026-05-23: **separate console scripts**, with the explicit
    constraint that they do not read `Lvlab.yml` or interact with lvlab-managed
    VMs. They live in the same package so library code is shared via a clean
    public API. See Phase 6 above for the full architectural notes.
1. ~~Phase 2: snapshot XML handoff~~ — decided: tempfile. Stdin path
    reserved for future use.
1. ~~Phase 2: `down --force`~~ — decided: yes, with **different** semantics
    than `destroy --force` (force-off without undefine; calls `virsh destroy <name>`). Documented asymmetry in command help text at implementation
    time.
