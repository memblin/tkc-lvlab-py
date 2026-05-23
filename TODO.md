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

## Phase 2 — Replace `libvirt-python` with `virsh` subprocess calls

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
- [ ] Remove `continue-on-error: true` for Python 3.14 in the test workflow
    (Phase 3); 3.14 should be a first-class matrix entry once libvirt-python
    is gone.

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

## Phase 3 — Test infrastructure (depends on Phase 1 matrix + Phase 2 port)

Land after Phase 2 — the conftest is dramatically simpler with no
`libvirt-python` C extension to mock around.

- [ ] Add `.github/workflows/test.yml`:
    - [ ] Matrix: `python-version: ['3.11', '3.12', '3.13', '3.14']`.
    - [ ] Job step: `uv sync --all-extras && uv run pytest -m "not integration"`.
    - [ ] 3.14 is a first-class entry, **not** `continue-on-error` (libvirt-python
        is gone by now).
- [ ] Create `tests/conftest.py` with the safety scaffolding (see
    "Cross-cutting safety rules" below).
- [ ] Seed a small set of pure-unit tests (no libvirt, no qemu-img) to prove
    the matrix runs end-to-end before we start writing real coverage:
    - [ ] `parse_config()` happy / missing-file / bad-yaml cases.
    - [ ] `parse_file_from_url()`.
    - [ ] `CloudImage._parse_checksum_file()` — both Fedora and Debian formats,
        including the `.verified` swap.
    - [ ] `UserData._is_valid_ssh_public_key()`.
    - [ ] `Machine.libvirt_vm_name` construction (`vm_name_environment`).
    - [ ] `run_virsh()` wrapper — stub `subprocess.run`, verify args / env /
        error translation.
- [ ] Mark anything that touches `virsh` for real, `qemu-img`, or libvirt
    with `@pytest.mark.integration`; require `LVLAB_INTEGRATION=1` to opt
    in and never enable it in CI.

______________________________________________________________________

## Phase 4 — Documentation pass after uv + virsh migrations

- [ ] `docs/Walkthrough.md` — replace any `poetry build` / pip-install lines
    with uv equivalents. Remove any "needs libvirt-dev" notes that no
    longer apply.
- [ ] `docs/CONTRIBUTING.md` (if it references Poetry — verify).
- [ ] `README.md` Requirements section: replace `libvirt-python` build
    requirements with the runtime `libvirt-clients` requirement. Confirm
    no Poetry remnants.

______________________________________________________________________

## Phase 5 — Survey `lvscripts-py` (blocked on session restart)

The sibling repo at `/home/tkcadmin/repos/github/memblin/lvscripts-py` becomes
readable after the next Claude restart (the `additionalDirectories` grant we
just added).

- [ ] Read `lvscripts-py/CLAUDE.md` and `README.md` to understand intent.
- [ ] Inventory the public surface: what scripts ship, what flags they take,
    what they do top-to-bottom.
- [ ] Map functional overlap with `tkc-lvlab`:
    - cloud-image download/verify
    - cloud-init ISO build
    - qcow2 backing-disk creation
    - virt-install invocation
    - libvirt domain lifecycle (define / start / shutdown / destroy / undefine)
- [ ] Identify capabilities lvlab does **not** have today, especially:
    - [ ] One-off `createvm <name> --os ... --memory ... --disk ...` style entry
        without needing an `Lvlab.yml`.
    - [ ] Anything around image building / customization, NAT/bridge wiring,
        or post-create provisioning.
- [ ] Decide on per-feature disposition: **port**, **adapt**, **skip**, or
    **leave to lvscripts**.

______________________________________________________________________

## Phase 6 — Merge `createvm` / `destroyvm` into `lvlab`

Outcome: one package providing both the lab manifest workflow and one-off VM
creation, sharing the same `Machine` / `CloudImage` / `VirtualDisk` /
`CloudInitIso` machinery.

- [ ] CLI shape:
    - [ ] `lvlab vm create <name> [--os …] [--memory …] [--cpu …] [--disk …] [--network …] [--pubkey …]` — manifest-less one-off.
    - [ ] `lvlab vm destroy <name>` — one-off teardown (no Lvlab.yml lookup).
    - [ ] Existing `lvlab up` / `lvlab destroy` / `lvlab status` continue to be
        manifest-driven and unchanged.
    - [ ] Expose `createvm` and `destroyvm` as additional `[project.scripts]`
        thin shims to the same Click commands (for muscle memory from users
        coming from lvscripts).
- [ ] Refactor where the merge exposes duplication:
    - [ ] If lvscripts has a cleaner abstraction we want, port it and make
        manifest-driven commands use it too — don't keep two implementations.
- [ ] Naming guard: one-off VMs should still be namespaced. Either reuse
    `{vm_name}_{environment}` with a sentinel environment like `_oneoff`,
    or introduce a distinct prefix like `oneoff-{vm_name}`. **Decide
    explicitly** — this affects how `status`/listing finds them.
- [ ] Tests for both code paths (manifest and one-off) share the same safety
    fixture (see below).
- [ ] Docs: extend `README.md` and `docs/Walkthrough.md` with the one-off
    workflow. Add a section explaining when to use which.

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

## Decisions still open (call these out before Phase 6 lands)

1. ~~Build backend~~ — decided: `uv_build`. No hatchling, no setuptools.
1. ~~Python floor~~ — decided: drop 3.10, `requires-python = ">=3.11"`.
    Phase 2 makes this consequence-free since libvirt-python is gone.
1. ~~One-off VM namespacing~~ — decided in Phase 2 design doc
    (`/tmp/phase2-design.md` §5): sentinel environment `_oneoff`.
1. Whether to keep `createvm` / `destroyvm` as separate console_scripts, or
    only expose `lvlab vm create` / `lvlab vm destroy`.
1. ~~Phase 2: snapshot XML handoff~~ — decided: tempfile. Stdin path
    reserved for future use.
1. ~~Phase 2: `down --force`~~ — decided: yes, with **different** semantics
    than `destroy --force` (force-off without undefine; calls `virsh destroy <name>`). Documented asymmetry in command help text at implementation
    time.
