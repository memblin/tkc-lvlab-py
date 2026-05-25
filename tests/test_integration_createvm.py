"""Integration tests for the standalone ``createvm`` / ``deletevm`` round-trip.

Gated by ``LVLAB_INTEGRATION=1``. The default test run skips every
function in this module via the ``integration`` marker. See
``tests/conftest.py`` and the "Integration tests" safety rules in
``CLAUDE.md`` before adding to this file.

``createvm`` / ``deletevm`` are faithful ports of the ``lvscripts``
commands: positional ``VM_NAME`` / ``VM_DISTRO``, always-copy disk, and a
**qemu:///system** target (no ``--uri`` / user-mode networking). These
tests therefore run on the system URI only; the parametrized session URI
is skipped.

**Matrix.** Every image in ``createvm``'s ``BUILTIN_IMAGES`` catalog is
exercised in both addressing modes — DHCP on the default NAT network and a
static IP on the same network — and each case verifies real SSH login as
the cloud-init default user. The catalog is imported directly so the matrix
stays in sync with the shipped image list automatically.

**Serial by design.** Cases run one at a time (no ``pytest-xdist``): each
test creates a VM, verifies it, and tears it down (``deletevm --force`` in a
``finally`` plus :func:`wait_for_no_domain`) before the next begins, so at
most one test VM is live at any moment. Subset the run on a constrained host
with ``LVLAB_TEST_DISTROS`` / ``LVLAB_TEST_MODES`` (comma-separated), or with
pytest ``-k`` selection.

Every libvirt domain, qcow2 file, and cloud-init ISO this module creates
is named via :func:`make_test_name` so the session reaper can clean up
after a crashing test. Because the scripts use raw libvirt domain names,
the domain name IS the prefixed ``make_test_name`` value. Per-VM storage
lives under the dedicated ``/var/lib/libvirt/images/lvlab-test/`` directory
(via ``--storage-root``), distinct from the production
``/var/lib/libvirt/images/lvlab/oneoff/``.

The cloud-image cache at ``/var/lib/libvirt/images/lvlab/cloud-images/`` is
intentionally shared with normal lvlab usage — the cache is read-only after
download, and re-downloading a 400 MB qcow2 every run would make the suite
hostile to iterate.
"""

from __future__ import annotations

import os
import subprocess
from collections.abc import Sequence
from pathlib import Path

import pytest

from tkc_lvlab.scripts.createvm import BUILTIN_IMAGES, derive_username

from tests.conftest import assert_owned_by_test, make_test_name
from tests.integration_helpers import (
    find_entry_point,
    list_domains,
    pick_static_ip,
    ssh_run,
    wait_for_dhcp_lease,
    wait_for_no_domain,
    wait_for_ssh,
)

# First run downloads + verifies a ~400 MB cloud image, then runs
# virt-install and waits for a NAT DHCP lease; be generous.
_CREATEVM_TIMEOUT_SECONDS = 600
_DELETEVM_TIMEOUT_SECONDS = 120

# createvm copies the base image then runs ``qemu-img resize <disk> <size>``.
# That resize must be a *grow*: qemu-img refuses to shrink below the base
# image's virtual size without ``--shrink``. The largest base we test is
# AlmaLinux 10 GenericCloud (10 GiB virtual), so the test disk size has to
# exceed that for every distro. qcow2 grow-resize is metadata-only (sparse),
# so a larger virtual size costs nothing on disk beyond the copied base.
_TEST_DISK_SIZE = "12G"


def _env_subset(env_var: str, allowed: Sequence[str]) -> list[str]:
    """Return ``allowed`` filtered by a comma-separated env var (or all).

    Lets a resource-constrained host narrow the matrix without editing
    code, e.g. ``LVLAB_TEST_DISTROS=debian13 LVLAB_TEST_MODES=dhcp``.

    Args:
        env_var: Environment variable holding a comma-separated subset.
        allowed: The full set of valid values.

    Returns:
        The requested subset (preserving request order), or ``list(allowed)``
        when the env var is unset/empty.

    Raises:
        ValueError: A requested value is not in ``allowed`` — fail loudly
            rather than silently run nothing.
    """
    raw = os.environ.get(env_var, "").strip()
    if not raw:
        return list(allowed)
    requested = [item.strip() for item in raw.split(",") if item.strip()]
    unknown = [item for item in requested if item not in allowed]
    if unknown:
        raise ValueError(
            f"{env_var}: unknown value(s) {unknown}; valid: {sorted(allowed)}"
        )
    return requested


_DISTROS = _env_subset("LVLAB_TEST_DISTROS", list(BUILTIN_IMAGES))
_MODES = _env_subset("LVLAB_TEST_MODES", ["dhcp", "static"])


@pytest.mark.integration
@pytest.mark.parametrize("mode", _MODES)
@pytest.mark.parametrize("distro", _DISTROS)
def test_createvm_connectivity_and_deletevm(
    distro: str,
    mode: str,
    integration_uri: str,
    lvlab_integration_storage_root: Path,
    test_ssh_pubkey_path: Path,
    test_ssh_privkey_path: Path,
) -> None:
    """``createvm`` boots a reachable VM; ``deletevm --force`` removes it.

    End-to-end for one ``(image, addressing-mode)`` cell: real
    ``virt-install``, real standalone qcow2, real cloud-init ISO, real
    libvirt domain. After definition the test resolves the guest IP — from
    the NAT DHCP lease (``dhcp``) or the address it assigned (``static``) —
    waits for first-boot SSH, and asserts ``id -un`` equals the image's
    cloud-init default user. That single assertion proves connectivity AND
    that ``createvm`` seeded the right user and public key.

    Args:
        distro: A ``BUILTIN_IMAGES`` key (parametrized).
        mode: ``"dhcp"`` or ``"static"`` (parametrized).
        integration_uri: URI from the ``integration_uri`` fixture;
            session URIs are skipped (createvm is system-only).
        lvlab_integration_storage_root: libvirt-readable test storage root.
        test_ssh_pubkey_path: Public key seeded via ``--public-key``.
        test_ssh_privkey_path: Private key used for the SSH probe.
    """
    if "session" in integration_uri:
        pytest.skip("createvm targets qemu:///system only")

    createvm = find_entry_point("createvm")
    deletevm = find_entry_point("deletevm")

    expected_user = derive_username(distro, BUILTIN_IMAGES[distro].get("username"))

    vm_name = make_test_name(f"createvm-{distro}-{mode}")
    # Raw-name contract: the libvirt domain is exactly vm_name (which
    # already carries LVLAB_TEST_PREFIX from make_test_name).
    expected_domain = vm_name
    assert_owned_by_test(expected_domain)

    storage_root = lvlab_integration_storage_root

    argv = [
        createvm,
        vm_name,
        distro,
        "--storage-root",
        str(storage_root),
        "--public-key",
        str(test_ssh_pubkey_path),
        "--memory",
        "1024",
        "--cpu",
        "1",
        "--disk-size",
        _TEST_DISK_SIZE,
    ]
    static_ip = ""
    if mode == "static":
        picked = pick_static_ip(integration_uri)
        if picked is None:
            pytest.skip(
                "static-IP test skipped: createvm rejects an --ip4 inside the "
                "DHCP range, and the default network's range spans the whole "
                "subnet. The suite will NOT modify your 'default' network. To "
                "test static addressing, narrow its DHCP range yourself (e.g. "
                ".2-.199, freeing .200-.254) and re-run, or use a dedicated "
                "test network. Opt-in transient auto-narrow is a tracked "
                "future enhancement."
            )
        static_ip, netmask = picked
        argv += ["--ip4", static_ip, "--netmask", netmask]

    create_result = subprocess.run(
        argv,
        capture_output=True,
        text=True,
        check=False,
        timeout=_CREATEVM_TIMEOUT_SECONDS,
    )

    try:
        assert create_result.returncode == 0, (
            f"createvm failed (exit {create_result.returncode}):\n"
            f"stdout:\n{create_result.stdout}\n"
            f"stderr:\n{create_result.stderr}"
        )

        assert expected_domain in list_domains(integration_uri), (
            f"createvm reported success but domain {expected_domain!r} "
            f"is not in virsh list on {integration_uri}"
        )

        per_vm_dir = storage_root / vm_name
        assert per_vm_dir.is_dir(), (
            f"createvm reported success but storage dir {per_vm_dir} "
            f"was not created"
        )
        assert (
            per_vm_dir / "disk0.qcow2"
        ).is_file(), (
            f"createvm reported success but {per_vm_dir / 'disk0.qcow2'} is missing"
        )
        assert (
            per_vm_dir / "cidata.iso"
        ).is_file(), (
            f"createvm reported success but {per_vm_dir / 'cidata.iso'} is missing"
        )

        if mode == "dhcp":
            ip = wait_for_dhcp_lease(integration_uri, expected_domain)
        else:
            ip = static_ip

        wait_for_ssh(ip, expected_user, test_ssh_privkey_path)
        whoami = ssh_run(ip, expected_user, test_ssh_privkey_path, "id -un")
        assert whoami == expected_user, (
            f"expected cloud-init default user {expected_user!r} on "
            f"{distro}/{mode} at {ip}, got {whoami!r}"
        )
    finally:
        # deletevm must always run — even if the assertions above failed,
        # we want to leave the host clean. The session reaper is a safety
        # net, not a substitute for explicit cleanup.
        destroy_result = subprocess.run(
            [
                deletevm,
                vm_name,
                "--force",
                "--storage-root",
                str(storage_root),
            ],
            capture_output=True,
            text=True,
            check=False,
            timeout=_DELETEVM_TIMEOUT_SECONDS,
        )

    assert destroy_result.returncode == 0, (
        f"deletevm failed (exit {destroy_result.returncode}):\n"
        f"stdout:\n{destroy_result.stdout}\n"
        f"stderr:\n{destroy_result.stderr}"
    )

    wait_for_no_domain(integration_uri, expected_domain)
