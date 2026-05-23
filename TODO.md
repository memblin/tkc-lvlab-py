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

- [ ] Build a small subprocess wrapper at `tkc_lvlab/utils/virsh.py`:
    - [ ] `run_virsh(uri, args, check=True, capture=True) -> CompletedProcess`
        — wraps `subprocess.run(["virsh", "-c", uri, *args], env={..., "LC_ALL": "C"})`
        so output is locale-stable.
    - [ ] Custom `VirshError(returncode, stderr)` so callers can `except VirshError`
        without leaking `subprocess.CalledProcessError` everywhere.
- [ ] Port the `Machine` methods in `tkc_lvlab/utils/libvirt.py`:
    - [ ] `Machine.exists_in_libvirt` → `virsh list --all --name`, exact match.
    - [ ] `Machine.destroy` / `Machine.shutdown` / `Machine.poweron` →
        `virsh destroy|shutdown|start <name>`.
    - [ ] `Machine.list_snapshots` → `virsh snapshot-list <name> --name`.
    - [ ] `Machine.create_snapshot` → `virsh snapshot-create <name> --xmlfile <path>`.
        **Sharp edge:** `--xmlfile` needs a real path. Use `tempfile.NamedTemporaryFile`
        (delete after) rather than relying on stdin redirection — virsh's stdin
        handling for `--xmlfile -` varies by version.
    - [ ] `Machine.delete_snapshot` → `virsh snapshot-delete <name> <snapshot_name>`.
    - [ ] `Machine.hasCurrentSnapshot` equivalent — `virsh snapshot-list <name> --name`
        and check non-empty (used in `destroy()` cleanup path).
    - [ ] `capabilities` command (`cli.py:30-35`) → `virsh capabilities` stdout.
    - [ ] `status` command (`cli.py:385-422`) → single `virsh list --all` parse,
        not one `domstate` call per declared VM (cuts startup overhead).
- [ ] Replace dynamic state-constant reflection. Drop `get_machine_state`
    (libvirt.py:546) and `_humanize_machine_status` (libvirt.py:493) in
    their current form:
    - [ ] `virsh domstate <name>` returns a human string: `running`, `idle`,
        `paused`, `in shutdown`, `shut off`, `crashed`, `pmsuspended`.
        Replace the integer-keyed dynamic dict with a hardcoded `str → human`
        map keyed on those exact strings.
    - [ ] Use `virsh domstate <name> --reason` for the reason string. Build the
        analogous reason map.
    - [ ] Audit every caller that compares against `"VIR_DOMAIN_*"` strings
        (`cli.py:126`, `cli.py:137`, `cli.py:443`, `cli.py:447`,
        `libvirt.py:301`, `libvirt.py:307`, `libvirt.py:440`, `libvirt.py:463`)
        and switch them to the new lowercase virsh state strings.
- [ ] Drop `libvirt-python` from `[project.dependencies]` in `pyproject.toml`.
    Regenerate `uv.lock`. Update `CLAUDE.md`'s "Install deps" note — at
    build time we no longer need `libvirt-dev` / `pkg-config`, only
    `libvirt-clients` (or distro equivalent) at runtime.
- [ ] After regenerating `uv.lock`, diff it against the previous lock and
    confirm the only changes are `libvirt-python` removal plus any
    transitives uniquely brought in by it. Surprise shifts in
    `requests` / `urllib3` / `idna` / `jinja2` / `cryptography` resolved
    versions should be investigated before merging — those are the
    packages Dependabot has historically flagged.
- [ ] Update `CLAUDE.md` Architecture section: remove `libvirt-python` API
    references, document the `virsh` subprocess pattern and the
    `tkc_lvlab/utils/virsh.py` helper.
- [x] ~~Remove `continue-on-error: true` for Python 3.14 in the test workflow~~ —
    moot: no test workflow with a Python matrix existed at the time
    libvirt-python was dropped. When Phase 3 wires up the pytest CI matrix,
    Python 3.14 will be a first-class entry from day one.

### Standardize destructive-path UX (do it here, not as a separate pass)

We're rewriting all destructive code paths in this phase anyway. Land the
hardening at the same time so we don't churn these files twice.

- [ ] `snapshot delete` gets a `--force` flag + confirmation prompt, mirroring
    `destroy` (already done in the Phase 0 incidentals commit, but verify
    consistency once the underlying call is virsh-based).
- [ ] Every destructive command surfaces the actual `virsh` stderr on failure
    paths — no more silent "deletion failed" with no detail.
- [ ] Audit `down` for whether it should consume the same `--force` semantics
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

## Phase 8 — Repo restructure: src-layout + `tkc.lvlab` namespace

Reshape the package so:

- All source lives under `src/` (PEP 621 "src-layout"). Currently the package
    is at the repo root (`tkc_lvlab/`); after this phase it would be at
    `src/tkc/lvlab/`.
- The top-level Python name becomes a **PEP 420 implicit namespace package**
    named `tkc`, with `lvlab` underneath it. Imports change from
    `from tkc_lvlab... import ...` to `from tkc.lvlab... import ...`. The
    console script entry point becomes `tkc.lvlab.cli:run`.
- The reason for the namespace: sibling tools in the `tkc` family
    (`lvscripts-py`, future ones) can ship as separate distributions but live
    in a shared `tkc.*` import hierarchy. PEP 420 makes that work without an
    `__init__.py` at the `tkc` level.

### Work this implies

- [ ] Move `tkc_lvlab/` → `src/tkc/lvlab/`. Delete the root-level `__init__.py`
    and **do not** add one at `src/tkc/` (that's what makes it a namespace
    package). `src/tkc/lvlab/__init__.py` stays.
- [ ] Rewrite every import: `tkc_lvlab.X` → `tkc.lvlab.X`. Affected files
    include all of `src/tkc/lvlab/**/*.py`, every `tests/test_*.py`, and
    `docs/api/.../*.md` (the `:::` directives).
- [ ] `pyproject.toml`:
    - [ ] `[project.scripts]`: `lvlab = "tkc.lvlab.cli:run"`.
    - [ ] `[tool.uv.build-backend]`: verify `uv_build` supports the new
        layout. Recent uv has improved namespace-package handling; confirm
        against the current `uv_build` release notes. The two relevant
        knobs are `module-root` (set to `"src"`) and `module-name` (set to
        `"tkc.lvlab"` or `"tkc"` with a `namespace-packages` flag,
        depending on what uv expects).
    - [ ] Templates `include` glob: update `tkc_lvlab/templates/*.j2` to
        `src/tkc/lvlab/templates/*.j2` (or whatever path the new layout
        uses); confirm with `unzip -l dist/*.whl | grep templates` after a
        rebuild.
- [ ] `sonar-project.properties`: `sonar.sources=src/tkc/lvlab` (or
    whatever the new root is).
- [ ] `[tool.coverage.run] source` and `[tool.pytest.ini_options] addopts`
    (`--cov=tkc_lvlab`) → `--cov=tkc.lvlab`.
- [ ] `mkdocs.yml` mkdocstrings handler: paths to the new tree if needed.
- [ ] `mkdocs.yml` `repo_url` / `edit_uri` unchanged but verify links from
    `docs/api/.../*.md` still resolve.
- [ ] `CLAUDE.md`: update every file-path reference (e.g.
    `tkc_lvlab/utils/libvirt.py` → `src/tkc/lvlab/utils/libvirt.py`) and
    mention the namespace decision in "Architecture."
- [ ] Bump the wheel filename guard in `.github/workflows/build-release.yml`
    — `tkc_lvlab-${{ github.ref_name }}-py3-none-any.whl` will become
    something like `tkc_lvlab-...whl` or `tkc.lvlab-...whl` depending on
    how `uv_build` produces it. Verify on a `workflow_dispatch` dry-run
    before tagging a real release.
- [ ] After all the above, `uv build` and confirm the wheel still contains
    the templates and the entry point still works (`uv run lvlab --help`).

### When to schedule

- **Not during Phase 2.** The virsh port already touches half the source
    tree; mixing in a rename would make the diff unreviewable.
- **Not during Phase 3** — the new tests are still being added; rename
    later when there are fewer test files to update.
- **Right before Phase 6** is a candidate — Phase 6 introduces new code
    (`lvlab vm create`) that should land in the new layout from day one.
    Or wait until after Phase 6 if a release goes out in between (avoids
    changing the wheel's package name twice in close succession).

### Risk flags

- Wheel-filename change is **user-visible**: existing `uv tool install tkc-lvlab` continues to work but the underlying wheel asset name in
    GitHub Releases changes. Document in the release notes.
- The `tkc` namespace becomes a soft commitment — once we publish a wheel
    with `tkc.lvlab`, removing the namespace later is a breaking change for
    importers.
- Verify `pip install tkc-lvlab` (or `uv tool install tkc-lvlab`) doesn't
    conflict with any other PyPI package claiming the `tkc` namespace before
    publishing.

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
- [ ] Manual smoke test the full happy path against a real libvirt
    URI before pushing (`uv run lvlab init`, `up`, `status`,
    `snapshot create/list/delete`, `destroy`). Unit tests cover
    parsing + flow but not the actual hypervisor side.

### Follow-up: migrate the standalone scripts to Typer too

The Click → Typer migration only touched `tkc_lvlab/cli.py` — the
standalone one-off entry points (`tkc_lvlab/scripts/createvm.py`,
`tkc_lvlab/scripts/destroyvm.py`) still use Click directly. They were
left out of Phase 9 because the surface is substantial (createvm.py
alone is ~622 LOC with 11 options) and the migration has its own UX
contract to preserve. Doing it in the same commit as cli.py would have
ballooned the diff and tangled two independent verifications.

Scope when picked up:

- [ ] `tkc_lvlab/scripts/createvm.py` — Click command + 11 options
    (`--distro`, `--memory`, `--cpu`, `--disk-size`, `--network`,
    `--ip4`, `--public-key`, `--copy`, `--uri`, `--storage-root`).
    Watch the `click.Choice(case_sensitive=False)` for `--distro` —
    Typer's `Enum` support is the natural replacement but the
    case-insensitive matching must be preserved. The `--copy` flag
    maps to a `copy_strategy` Python parameter via `@click.option`
    aliasing — Typer's parameter-name-to-flag mapping works
    differently; verify the alias still lands cleanly.
- [ ] `tkc_lvlab/scripts/destroyvm.py` — smaller (3 options:
    `--force`, `--uri`, `--storage-root`). Confirmation prompt uses
    `click.confirm(..., err=True)` — Typer equivalent is
    `typer.confirm(...)`, default stdout (not stderr) but accepts
    `err=True` via kwargs.
- [ ] `tests/test_createvm.py` (16 tests) and `tests/test_destroyvm.py`
    (7 tests) currently import `from click.testing import CliRunner`
    and `from tkc_lvlab.scripts.createvm import run`. They'll need
    the same swap to `typer.testing.CliRunner` and `from tkc_lvlab.scripts.createvm import app` (with backwards-compat `run = app` alias
    matching the cli.py pattern).
- [ ] `click.ClickException` raises in the script bodies translate
    to `typer.BadParameter` / explicit `typer.echo(..., err=True);   raise typer.Exit(code=1)` — pick whichever preserves the existing
    error-message format and exit-code semantics.
- [ ] Update CLAUDE.md "What this is" — currently calls out that
    "The standalone one-off scripts (`createvm`, `destroyvm` in
    `tkc_lvlab/scripts/`) still use Click directly." That sentence
    goes away.

This is a candidate for a single bundled PR (createvm + destroyvm +
their tests together) since they share helpers in `tkc_lvlab/utils/`
and the test infra is parallel.

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

## Decisions still open (call these out before Phase 6 lands)

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
