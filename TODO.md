# TODO

Roadmap for the next few sessions. Phases are ordered by dependency, not strict
calendar. Phase 3 onward is blocked until the Claude session is restarted so
the `.claude/settings.local.json` grant for `lvscripts-py` takes effect.

---

## Phase 1 — Migrate Poetry → uv, refresh deps, add Python matrix

**Goal:** one-command env setup with `uv`, a real `uv.lock`, and a build/test
matrix that runs on Python 3.11, 3.12, 3.13, and 3.14.

- [ ] Use `uv_build` as the PEP 517 build backend. **No hatchling, no
      setuptools — uv all the way.** `[build-system]` becomes
      `requires = ["uv_build>=0.5"]` / `build-backend = "uv_build"` (pin to
      whatever uv ships current at migration time).
- [ ] Rewrite `pyproject.toml`:
  - [ ] Replace `[tool.poetry]` with PEP 621 `[project]` (name, version,
        description, authors, readme, license, classifiers, `requires-python = ">=3.11"`).
  - [ ] Move `[tool.poetry.dependencies]` → `[project.dependencies]`.
  - [ ] Move `[tool.poetry.scripts]` → `[project.scripts]`.
  - [ ] Drop the Poetry-only `include = ["tkc_lvlab/templates/*.j2"]`.
        `uv_build` ships package data by default for files inside the import
        package, so `tkc_lvlab/templates/*.j2` is picked up automatically.
        Verify with `uv build && unzip -l dist/*.whl | grep templates`.
        If anything needs explicit inclusion, configure it under
        `[tool.uv.build-backend]` per uv docs — **do not** reach for hatch
        or setuptools tables.
  - [ ] Add a `[dependency-groups]` entry (PEP 735) with `pytest`,
        `pytest-cov`, and any other test/lint extras. Install via
        `uv sync --group dev`.
- [ ] Remove `poetry.lock`; generate `uv.lock` via `uv lock`.
- [ ] Remove `requirements.txt` (only used by old CI). If we still want a
      pinned export, generate it with `uv export --format requirements-txt`.
- [ ] Refresh all dep versions to current latest-compatible. Spot-check:
  - [ ] `libvirt-python` — confirm a wheel exists for 3.13 / 3.14.
  - [ ] `pycdlib`, `python-gnupg` — same.
  - [ ] `click`, `jinja2`, `requests`, `tqdm`, `pyyaml` — bump as resolver allows.
- [ ] Update `.github/workflows/build-release.yml`:
  - [ ] Replace `pip install --user poetry; poetry build` with
        `uv build` (using `astral-sh/setup-uv` action).
  - [ ] Drop `pip install --user -r requirements.txt`.
- [ ] Update `README.md` install instructions:
  - [ ] `uv tool install tkc-lvlab` as the recommended path.
  - [ ] Or local dev: `uv sync && uv run lvlab --help`.
  - [ ] Drop the manual venv + pip recipe (or move it to an "Alternatives"
        section).
- [ ] Update `CLAUDE.md` "Build / dev / lint commands" section to show `uv`.
- [ ] Verify `pre-commit` still works after the migration (it's independent
      of the build backend; mostly checking nothing references Poetry).

### Phase 1.5 — Test infrastructure (lands together with Phase 1 matrix)

- [ ] Add `.github/workflows/test.yml`:
  - [ ] Matrix: `python-version: ['3.11', '3.12', '3.13', '3.14']`.
  - [ ] Job step: `uv sync --all-extras && uv run pytest -m "not integration"`.
  - [ ] Mark Python 3.14 `continue-on-error: true` until libvirt-python ships
        a 3.14 wheel (revisit when it does).
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
- [ ] Mark anything that touches libvirt or `qemu-img` with
      `@pytest.mark.integration`; require `LVLAB_INTEGRATION=1` to opt in
      and never enable it in CI.

---

## Phase 2 — Documentation pass after uv migration

- [ ] `docs/Walkthrough.md` — replace any `poetry build` / pip-install lines
      with uv equivalents.
- [ ] `docs/CONTRIBUTING.md` (if it references Poetry — verify).
- [ ] `README.md` Requirements section is libvirt-focused (good) but confirm
      no Poetry remnants.

---

## Phase 3 — Survey `lvscripts-py` (blocked on session restart)

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

---

## Phase 4 — Merge `createvm` / `destroyvm` into `lvlab`

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

---

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
  - [ ] A session-scoped teardown that lists libvirt domains *filtered by
        the prefix* and reaps any that survived a crashing test. **Never
        list all domains; only ones matching the prefix.**
- [ ] Same prefix applies to:
  - [ ] On-disk paths (`disk_image_basedir` for tests must be a temp dir,
        not the developer's shared `~/.local/lvlab/...`).
  - [ ] Cloud-init ISOs and the per-VM config directory.
- [ ] Add a lint/grep check (or pytest plugin) that fails CI if a test calls
      `virDomain.undefine()` / `destroy()` / `os.remove()` on a name that
      didn't come from `make_test_name`.
- [ ] Integration tests **must** use a dedicated `libvirt_uri` or at least a
      dedicated network and storage pool so cleanup can be scoped further.

---

## Decisions still open (call these out before Phase 4 lands)

1. ~~Build backend~~ — decided: `uv_build`. No hatchling, no setuptools.
2. One-off VM namespacing: sentinel environment `_oneoff` vs distinct prefix.
3. Whether to keep `createvm` / `destroyvm` as separate console_scripts, or
   only expose `lvlab vm create` / `lvlab vm destroy`.
4. Whether to drop Python 3.10 (currently `^3.10` in `pyproject.toml`) when
   moving to 3.11+ as the floor. Tentative: yes, drop it.
