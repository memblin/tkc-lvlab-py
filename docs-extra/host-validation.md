# Host-distro validation

`tkc-lvlab` is a developer tool that runs on a workstation and drives
local libvirt+QEMU lab VMs. The set of host distros where we
explicitly exercise the full unit + integration suite — and which we
therefore consider validated targets — is fixed:

| Host distro  | Codename | System Python | Notes                              |
| ------------ | -------- | ------------- | ---------------------------------- |
| Debian 12    | bookworm | 3.11          | Matches our `>=3.11` floor exactly |
| Debian 13    | trixie   | 3.13          | Current Debian stable              |
| AlmaLinux 10 | —        | 3.12          | RHEL-equivalent LTS                |
| Fedora 44    | —        | 3.14          | Current Fedora release             |

Out of scope, explicitly:

- **Debian 11** (bullseye) — oldoldstable, ships Python 3.9.
- **Ubuntu 22.04 LTS** (jammy) — system Python 3.10, below our 3.11 floor.
- **Ubuntu 24.04 LTS** (noble) — dropped from validation 2026-05-23; cycle didn't fit and no contributor demand at the moment. Reintroducible if anyone needs it.
- **AlmaLinux 9 / RHEL 9 / Rocky 9** — system Python 3.9, same reason.
- **Older Fedora releases** — only current Fedora is in matrix.

## Latest validation

End-to-end validated against canonical git SHA `d9f8ec3` on 2026-05-23.
Each row is one host running `scripts/run-validation.sh` to green —
unit suite (293 passed, 9 skipped) and integration suite (8 passed
across `qemu:///session` and `qemu:///system`).

| Host                          | Distro               | Kernel                 | System Python | Integration |
| ----------------------------- | -------------------- | ---------------------- | ------------- | ----------- |
| claude-almalinux10.tkclabs.io | AlmaLinux 10.1       | 6.12.0-124.56.5.el10_1 | 3.12.12       | 33.0s       |
| claude-debian12.tkclabs.io    | Debian 12 (bookworm) | 6.1.0-47-amd64         | 3.11.2        | 34.6s       |
| claude-debian13.tkclabs.io    | Debian 13 (trixie)   | 6.12.86+deb13-amd64    | 3.13.5        | 35.2s       |
| claude-fedora44.tkclabs.io    | Fedora 44            | 7.0.9-205.fc44         | 3.14.4        | 29.8s       |

Debian 12 specifically exercises the osinfo fallback chain
(`src/tkc_lvlab/utils/osinfo.py`): its `osinfo-db` package is from
2022-11-30 and predates Debian 13's release, so virt-install would
reject `--os-variant=debian13`; the fallback resolves to an older
known debian variant and the run succeeds.

Debian 13 specifically exercises the subprocess PATH override
(`src/tkc_lvlab/utils/subprocess_env.py`): trixie's virt-install
uses `#!/usr/bin/env python3` (every other distro hard-codes
`/usr/bin/python3`), so without the override `import gi` fails
because the venv's Python shadows system Python on PATH.

The bootstrap script (`scripts/host-bootstrap.sh`) refuses to run on
anything outside the supported list, so accidentally running it on a
jammy box won't half-configure it.

## Procedure for one host

The whole loop on a fresh VM of any supported distro:

```bash
# 1. SSH in, clone the repo
git clone https://github.com/memblin/tkc-lvlab-py
cd tkc-lvlab-py

# 2. Run the bootstrap (regular user; uses sudo internally)
scripts/host-bootstrap.sh

# 3. Re-login so libvirt group membership takes effect
#    (or `exec sg libvirt newgrp` for an in-session workaround)
exit
ssh ...

# 4. Make uv visible if your shell init didn't pick up ~/.local/bin
export PATH="${HOME}/.local/bin:${PATH}"

# 5. Sync dev deps
cd tkc-lvlab-py
uv sync --group dev

# 6. Run the full validation in one shot — emits a paste-back-ready
#    block and tees to scripts/results/<distro>-<sha>.txt.
scripts/run-validation.sh

# (Or run the two suites individually if you want to iterate:)
#    uv run pytest -q
#    LVLAB_INTEGRATION=1 uv run pytest tests/test_integration_*.py -v
```

The createvm/deletevm connectivity matrix
(`tests/test_integration_createvm.py`) can be narrowed on a
resource-constrained host with `LVLAB_TEST_DISTROS` / `LVLAB_TEST_MODES`
(comma-separated). Its static-IP cases skip unless the `default`
network's DHCP range leaves room for an out-of-range address — see
"createvm / deletevm connectivity matrix" in `docs-extra/CONTRIBUTING.md`.

The unit suite should report `293 passed, 9 skipped` (Python 3.11–3.14;
exact count drifts as tests are added — match the AlmaLinux 10
baseline from the same git SHA, see "Latest validation" above).
The integration suite should report `8 passed` on a host that has
both `qemu:///session` and `qemu:///system` reachable; URIs that
aren't ready are skipped with a clear reason rather than failing
the run.

For an end-to-end check of the **declarative manifest path**
(`lvlab up`/`down`/`destroy`) across every default image — one static
and one DHCP machine per distro, with SSH verified — run the built-in
`lvlab smoke` subcommand against the manifest bundle in
`docs-extra/smoke/` (`cd docs-extra/smoke && lvlab smoke`; see its
`README.md`). It runs the same lifecycle the legacy `run-smoke.sh`
helper did, adds a fail-fast preflight, prints the batch plan, and can
emit `--format json`/`yaml` for paste-back. It complements the
createvm/deletevm suite, which covers the standalone scripts rather
than the manifest workflow.

## What to record from each host run

`scripts/run-validation.sh` already captures everything below into a
single artifact at `scripts/results/<distro>-<sha>.txt` and prints the
same block to stdout. That file is the paste-back artifact — feed its
contents back when reporting a host result.

The block includes:

1. Distro `PRETTY_NAME`, kernel, system Python, uv version.
1. Git SHA and branch the run was made against.
1. Hostname, run timestamp (UTC), invoking user.
1. Full pytest output for both unit and integration suites, including
    any per-URI skip reasons.
1. `OVERALL: PASS` or `OVERALL: FAIL` summary line.

When a fresh matrix run lands, update the "Latest validation" table
above with the new date, SHA, and per-host details.

## Why these specific distros

Two constraints set the matrix:

- **Python 3.11 floor.** Set in `pyproject.toml` and used for type
    hints we wrote against 3.11+. Distros whose default system Python
    is older are excluded because forcing them through an alternate
    Python (deadsnakes PPA, dnf module switches, etc.) drifts the host
    away from the realistic developer-workstation surface we're
    trying to validate.
- **Realistic developer workstations.** The matrix covers Debian
    stable + oldstable, RHEL family via AlmaLinux 10, and current
    Fedora. Distros further afield (Arch, NixOS, openSUSE Tumbleweed,
    macOS via virt-install caveats) are not refused by the bootstrap,
    but they are not part of the validated set and may need manual
    package fixes.

The bootstrap script is the source of truth for the "what package on
which distro" mapping. Keep it in sync with this list.
