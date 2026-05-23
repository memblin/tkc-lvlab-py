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

## Phase 6 — Standalone `createvm` / `destroyvm` scripts in the lvlab package

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

### Architecture (lock these in before implementation)

- [ ] **Library / CLI split.** The shared logic moves to a stable library
    surface (likely `tkc_lvlab.<core>` or its Phase 8 equivalent
    `tkc.lvlab.<core>`). The lvlab CLI and the standalone scripts both
    depend on it; neither imports the other's CLI module.
- [ ] **No cross-contamination.** `createvm` / `destroyvm` MUST NOT read
    `Lvlab.yml`, MUST NOT look up manifest-driven VMs, MUST NOT mutate
    lvlab's per-environment state directories. `lvlab` reciprocally
    MUST NOT enumerate or destroy domains that the standalone scripts
    created. Cross-listing in `lvlab status` is acceptable as a
    read-only convenience IF and only if it can clearly distinguish
    one-offs from manifest VMs (see naming below).
- [ ] **Naming.** Decide explicitly — affects whether the two surfaces can
    tell each other's VMs apart. The previous `_oneoff` sentinel
    environment is moot now (one-offs don't have an environment).
    Candidates:
    - [ ] **Flat:** standalone scripts use the user-provided name verbatim
        as the libvirt domain name. Simplest. Risk: name collision with a
        lvlab manifest VM whose `libvirt_vm_name` happens to match.
    - [ ] **Prefix:** standalone scripts use e.g. `oneoff-<name>`. Cleanly
        distinguishable from `<vm_name>_<env>` lvlab names. Recommended.
    - [ ] **Distinct namespace via libvirt URI:** route standalone scripts
        to a different `qemu:///...` (e.g. always `qemu:///system` for
        one-offs, `qemu:///session` for lvlab). Heavier-weight; only worth
        it if the user already wants the segmentation.
- [ ] **Distribution shape.** Same wheel ships all three commands. After
    Phase 8 (src-layout + namespace), `[project.scripts]` reads roughly:
    `toml     lvlab = "tkc.lvlab.cli:run"     createvm = "tkc.lvlab.scripts.createvm:run"     destroyvm = "tkc.lvlab.scripts.destroyvm:run"     `

### Implementation work

- [ ] Port lvscripts `createvm` / `deletevm` logic into the lvlab package as
    `tkc_lvlab/scripts/createvm.py` and `tkc_lvlab/scripts/destroyvm.py`
    (post-Phase 8: `tkc/lvlab/scripts/...`). Each module exposes a `run()`
    Click command.
- [ ] Refactor `tkc_lvlab.utils.{libvirt,cloud_init,images,vdisk}` to expose
    a clean public library API the standalone scripts can consume without
    duplication. If lvscripts has a cleaner abstraction, port it and have
    lvlab use it too — don't keep two implementations.
- [ ] Port the high-value lvscripts capabilities flagged in
    `/tmp/lvscripts-inventory.md` (SSH key discovery, password phrase
    generation, `virsh net-dumpxml` network validation, optional DHCP
    lease polling).
- [ ] Sanity-check that `lvlab status` and the standalone scripts cannot
    observe or touch each other's VMs. A test that creates a one-off via
    `createvm` and then runs `lvlab status` expecting the one-off to be
    absent is a useful regression guard.
- [ ] Tests for both surfaces share the `LVLAB_TEST_PREFIX` safety fixture.
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

## Phase 7 — Legacy docstring + type-hint conversion

The `docs/conventions-and-toolchain` PR established **MkDocs + Material +
mkdocstrings** as the documentation toolchain and codified Google-style
docstrings + type hints as the rule for new code (see CLAUDE.md's
"Documentation conventions" section). The existing codebase predates that
rule and is excluded from the API reference until this phase lands.

Modules to convert (one PR per module is fine; one PR for the whole sweep is
also fine — pick what reviews cleanly):

- [ ] `tkc_lvlab/cli.py` — biggest surface, Click commands and docstrings
    become the `--help` output. Mind that black-formatted Click decorator
    output and Google `Args:` headings interact awkwardly; verify with
    `uv run lvlab --help` after conversion.
- [ ] `tkc_lvlab/config.py` — `parse_config`, `generate_hosts`,
    `generate_hosts_entries`, `parse_hosts_file`.
- [ ] `tkc_lvlab/_logging.py` — `get_logger`, `configure_logging`.
- [ ] `tkc_lvlab/utils/cloud_init.py` — three dataclasses and `CloudInitIso`.
- [ ] `tkc_lvlab/utils/images.py` — `CloudImage` and its parsers.
- [ ] `tkc_lvlab/utils/libvirt.py` — `Machine` and its methods. **Wait until
    Phase 2 ports finish** — the methods change shape and types in Phase 2,
    so converting here too early just creates rework.
- [ ] `tkc_lvlab/utils/vdisk.py` — `VirtualDisk` (small).

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

## Phase 9 — Migrate CLI from Click to Typer

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

### Work this implies

- [ ] Add `typer>=0.12` to `[project] dependencies` in `pyproject.toml`.
    Keep `click` as a transitive (Typer pulls it in) — do **not** add it
    as a direct dep just to use a couple of helpers; if you need
    `click.confirm`, use `typer.confirm` instead.
- [ ] Replace `@click.group()` on `run` with `typer.Typer(...)`. Pass
    `context_settings={"help_option_names": ["-h", "--help"]}` so `-h`
    still works. Set `no_args_is_help=True` to match current behavior
    (Click's group shows help by default; Typer needs this opt-in).
- [ ] Replace `@click.command()` with `@app.command()` for every
    subcommand (`up`, `down`, `destroy`, `init`, `status`, `hosts`,
    `ssh-config`, `cloudinit`, `capabilities`, `snapshot list/create/delete`).
- [ ] Replace `@click.argument("vm_name")` with a typed positional:
    `vm_name: str`. For optional args that default to None
    (`snapshot_description`), use
    `snapshot_description: Optional[str] = typer.Argument(None)`.
- [ ] Replace `@click.option("--force", is_flag=True, ...)` with
    `force: bool = typer.Option(False, "--force", help="...")`. Make
    sure both short and long forms (where present today) are listed.
- [ ] Replace verbosity flags (`-v/--verbose count` and `-q/--quiet`)
    with `verbose: int = typer.Option(0, "-v", "--verbose", count=True)`
    and `quiet: bool = typer.Option(False, "-q", "--quiet")` on the
    main callback. Typer's callback is the analog of Click's group
    body. **Verify the call to `configure_logging(verbosity=verbose, quiet=quiet)` still fires before any subcommand body runs** — Typer's
    callback semantics differ slightly from Click's group.
- [ ] Replace `click.echo` / `click.confirm` with `typer.echo` /
    `typer.confirm`. Audit each: a few cli.py call sites currently
    write to stderr via `click.echo(..., err=True)` — Typer's
    equivalent is `typer.echo(..., err=True)`. The status command's
    table output should look identical char-for-char.
- [ ] Subcommand groups (`snapshot list/create/delete`) become
    `snapshot_app = typer.Typer()` + `app.add_typer(snapshot_app, name="snapshot")`.
- [ ] **Help-text parity check.** Run `uv run lvlab --help` and
    `uv run lvlab <each command> --help` before and after; the
    rendered text should match (or you must justify each diff). Rich
    rendering can be turned off with
    `app = typer.Typer(rich_markup_mode=None)` if it shifts column
    widths or color in ways that break user expectations.
- [ ] **Exit-code parity check.** Verify failure paths still
    `sys.exit(1)` exactly where they did before. Typer translates
    raised exceptions differently than raw Click; double-check
    `VirshError` / `TypeError` handling in commands that have
    explicit try/except blocks.
- [ ] **Test migration.** Click's `CliRunner` works on Typer apps
    (since Typer uses Click under the hood) — call
    `CliRunner().invoke(app, [...])` against the Typer app object
    the same way you do today. **Do not rewrite the test surface as
    part of this PR** unless a test breaks for a real reason; just
    swap the app instance.
- [ ] **CLAUDE.md update**: Architecture section mentions "Click-based
    CLI" — change to "Typer-based CLI (Click under the hood)." Note
    that subcommand bodies should still grow via methods on
    `Machine` / `CloudImage` / etc., not by bloating the command
    function.
- [ ] Manual smoke test the full happy path (`lvlab init`, `lvlab up`,
    `lvlab status`, `lvlab snapshot create`, `lvlab snapshot list`,
    `lvlab destroy`) before merging. Type-checker and test suite
    catch a lot but not help-text drift.

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
