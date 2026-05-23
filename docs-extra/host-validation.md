# Host-distro validation

`tkc-lvlab` is a developer tool that runs on a workstation and drives
local libvirt+QEMU lab VMs. The set of host distros where we
explicitly exercise the full unit + integration suite — and which we
therefore consider validated targets — is fixed:

| Host distro      | Codename | System Python | Notes                              |
| ---------------- | -------- | ------------- | ---------------------------------- |
| Debian 12        | bookworm | 3.11          | Matches our `>=3.11` floor exactly |
| Debian 13        | trixie   | 3.13          | Current Debian stable              |
| Ubuntu 24.04 LTS | noble    | 3.12          |                                    |
| AlmaLinux 10     | —        | 3.12          | RHEL-equivalent LTS                |
| Fedora 44        | —        | 3.13          | Current Fedora release             |

Out of scope, explicitly:

- **Debian 11** (bullseye) — oldoldstable, ships Python 3.9.
- **Ubuntu 22.04 LTS** (jammy) — system Python 3.10, below our 3.11 floor.
- **AlmaLinux 9 / RHEL 9 / Rocky 9** — system Python 3.9, same reason.
- **Older Fedora releases** — only current Fedora is in matrix.

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

# 6. Unit tests (fast, no libvirt activity)
uv run pytest -q

# 7. Integration tests (creates real prefixed VMs under
#    /var/lib/libvirt/images/lvlab-test/, tears them down at the end)
LVLAB_INTEGRATION=1 uv run pytest tests/test_integration_*.py -v
```

The unit suite should report `272 passed, 9 skipped` (Python 3.11–3.14;
exact count drifts as tests are added — match the AlmaLinux 10
baseline from the same git SHA). The integration suite should report
`8 passed` on a host that has both `qemu:///session` and `qemu:///system`
reachable; URIs that aren't ready are skipped with a clear reason
rather than failing the run.

## What to record from each host run

For each newly validated host, capture:

1. `uname -r` and `cat /etc/os-release | grep PRETTY_NAME`
1. `python3 --version`
1. The pytest summary lines from both runs (unit + integration).
1. Any URI skipped, with the skip reason printed by the test.

When the matrix is fully green, update the "Supported host distros"
section at the top of this file with the validation date and the git
SHA the run was made against.

## Why these specific distros

Two constraints set the matrix:

- **Python 3.11 floor.** Set in `pyproject.toml` and used for type
    hints we wrote against 3.11+. Distros whose default system Python
    is older are excluded because forcing them through an alternate
    Python (deadsnakes PPA, dnf module switches, etc.) drifts the host
    away from the realistic developer-workstation surface we're
    trying to validate.
- **Realistic developer workstations.** The matrix covers the LTS
    Debian/Ubuntu releases that contributors are most likely to run, a
    current Debian stable, RHEL family via AlmaLinux 10, and current
    Fedora. Distros further afield (Arch, NixOS, openSUSE Tumbleweed,
    macOS via virt-install caveats) are not refused by the bootstrap,
    but they are not part of the validated set and may need manual
    package fixes.

The bootstrap script is the source of truth for the "what package on
which distro" mapping. Keep it in sync with this list.
