"""Integration test for the Phase 6 cross-surface architectural invariant.

``createvm`` / ``destroyvm`` and ``lvlab`` are independent surfaces that
must NEVER see each other's libvirt domains. Phase 6 locked this with
two prefixes:

- Standalone one-off VMs from ``createvm`` get the libvirt domain
    ``oneoff-<vm_name>``.
- Manifest VMs from ``lvlab`` get the libvirt domain
    ``<vm_name>_<env_name>``.

This test exercises both halves of the invariant on a single
parametrized URI run:

1. Create a one-off VM via ``createvm`` so a domain
    ``oneoff-<vm_name_A>`` exists.
2. Bring up a manifest VM via ``lvlab up`` so a separate domain
    ``<vm_name_B>_<env_name>`` exists.
3. Confirm both domains coexist on the same libvirt URI.
4. ``lvlab status`` must report on the manifest VM only — it must NOT
    list the one-off VM, even though both are visible to
    ``virsh list``.
5. ``destroyvm <vm_name_B> --force`` must fail with "not defined at
    <uri>" — it's looking for ``oneoff-<vm_name_B>``, which doesn't
    exist, and it must NOT silently fall through to looking up the
    bare ``<vm_name_B>`` (which would match the manifest VM).
6. The error must include the operator-helpful "use 'lvlab destroy'
    instead" nudge.
7. ``destroyvm <vm_name_A> --force`` must succeed (it finds
    ``oneoff-<vm_name_A>``) and must NOT touch the manifest VM.
8. After the targeted destroyvm, the manifest VM must still be
    defined. ``lvlab destroy --force`` then cleans it up.

Gated by ``LVLAB_INTEGRATION=1``. Default test runs skip via the
``integration`` marker.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from tests.conftest import assert_owned_by_test, make_test_name
from tests.integration_helpers import (
    createvm_network_args,
    find_entry_point,
    list_domains,
    render_manifest,
    wait_for_no_domain,
)


_CREATEVM_TIMEOUT_SECONDS = 300
_DESTROYVM_TIMEOUT_SECONDS = 60
_LVLAB_UP_TIMEOUT_SECONDS = 300
_LVLAB_STATUS_TIMEOUT_SECONDS = 30
_LVLAB_DESTROY_TIMEOUT_SECONDS = 60


@pytest.mark.integration
def test_createvm_lvlab_cross_surface_isolation(
    integration_uri: str,
    lvlab_integration_storage_root: Path,
    test_ssh_pubkey_path: Path,
    tmp_path: Path,
) -> None:
    """Two surfaces, two prefixes, no cross-contamination.

    Creates one VM via each path so both coexist on the same libvirt
    URI, then verifies that:

    - ``lvlab status`` sees only the manifest VM (not the one-off).
    - ``destroyvm <manifest-vm-name>`` fails with "not defined" — it
        does not fall through to the manifest VM even when the bare
        ``vm_name`` matches.
    - The error message includes the "use 'lvlab destroy' instead"
        nudge from ``destroyvm.py`` so operators get an actionable
        message.
    - ``destroyvm <oneoff-vm-name>`` cleans up only the one-off and
        leaves the manifest VM untouched.

    Args:
        integration_uri: libvirt URI parametrized by the
            :func:`integration_uri` fixture (skipped per-URI if not
            test-ready).
        lvlab_integration_storage_root: libvirt-readable test storage
            root (``/var/lib/libvirt/images/lvlab-test/``). Becomes
            both createvm's ``--storage-root`` and the manifest's
            ``disk_image_basedir``.
        tmp_path: pytest-managed scratch directory for the per-test
            ``Lvlab.yml`` file.
    """
    createvm = find_entry_point("createvm")
    destroyvm = find_entry_point("destroyvm")
    lvlab = find_entry_point("lvlab")
    pubkey_path = test_ssh_pubkey_path

    uri_tag = "session" if "session" in integration_uri else "system"
    oneoff_vm_name = make_test_name(f"regression-oneoff-{uri_tag}")
    manifest_vm_name = make_test_name(f"regression-manifest-{uri_tag}")
    env_name = make_test_name(f"regression-env-{uri_tag}")

    expected_oneoff_domain = f"oneoff-{oneoff_vm_name}"
    expected_manifest_domain = f"{manifest_vm_name}_{env_name}"
    assert_owned_by_test(expected_oneoff_domain)
    assert_owned_by_test(expected_manifest_domain)

    # Manifest declares only the lvlab-side VM. The one-off is created
    # via createvm and is INTENTIONALLY not in the manifest — that's
    # the point of the invariant.
    manifest_path = tmp_path / "Lvlab.yml"
    manifest_path.write_text(
        render_manifest(
            env_name=env_name,
            uri=integration_uri,
            storage_root=lvlab_integration_storage_root,
            vm_name=manifest_vm_name,
            pubkey_path=pubkey_path,
        )
    )

    # Step 1: createvm makes the one-off VM. --copy keeps the per-VM
    # qcow2 standalone (no backing-file tie to the shared cache) and
    # --storage-root keeps artifacts inside the dedicated test root.
    create_result = subprocess.run(
        [
            createvm,
            oneoff_vm_name,
            "--distro",
            "debian13",
            "--uri",
            integration_uri,
            *createvm_network_args(integration_uri),
            "--storage-root",
            str(lvlab_integration_storage_root),
            "--public-key",
            str(test_ssh_pubkey_path),
            "--copy",
            "--memory",
            "1024",
            "--cpu",
            "1",
            "--disk-size",
            "5G",
        ],
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

        # Step 2: lvlab up brings up the manifest VM.
        up_result = subprocess.run(
            [lvlab, "up", manifest_vm_name],
            capture_output=True,
            text=True,
            check=False,
            timeout=_LVLAB_UP_TIMEOUT_SECONDS,
            cwd=tmp_path,
        )
        assert up_result.returncode == 0, (
            f"lvlab up failed (exit {up_result.returncode}):\n"
            f"stdout:\n{up_result.stdout}\n"
            f"stderr:\n{up_result.stderr}"
        )

        # Step 3: confirm both domains coexist on the same URI.
        domains_after_setup = list_domains(integration_uri)
        assert expected_oneoff_domain in domains_after_setup, (
            f"createvm reported success but {expected_oneoff_domain!r} "
            f"missing from virsh list on {integration_uri}"
        )
        assert expected_manifest_domain in domains_after_setup, (
            f"lvlab up reported success but {expected_manifest_domain!r} "
            f"missing from virsh list on {integration_uri}"
        )

        # Step 4: lvlab status sees the manifest VM, not the one-off.
        status_result = subprocess.run(
            [lvlab, "status"],
            capture_output=True,
            text=True,
            check=False,
            timeout=_LVLAB_STATUS_TIMEOUT_SECONDS,
            cwd=tmp_path,
        )
        assert status_result.returncode == 0, (
            f"lvlab status failed (exit {status_result.returncode}):\n"
            f"stdout:\n{status_result.stdout}\n"
            f"stderr:\n{status_result.stderr}"
        )
        combined_status_output = status_result.stdout + status_result.stderr
        assert manifest_vm_name in combined_status_output, (
            f"lvlab status did not include the manifest VM "
            f"{manifest_vm_name!r}:\n{combined_status_output}"
        )
        assert oneoff_vm_name not in combined_status_output, (
            f"lvlab status leaked the one-off VM name "
            f"{oneoff_vm_name!r} into its output — the manifest "
            f"surface must not see createvm-managed VMs:\n"
            f"{combined_status_output}"
        )
        assert expected_oneoff_domain not in combined_status_output, (
            f"lvlab status leaked the one-off domain "
            f"{expected_oneoff_domain!r} into its output — the "
            f"manifest surface must not see createvm-managed VMs:\n"
            f"{combined_status_output}"
        )

        # Step 5 + 6: destroyvm on the manifest VM's bare name must
        # fail (it looks for `oneoff-<name>`, finds nothing) AND must
        # include the "lvlab destroy" nudge so operators are pointed
        # at the right tool.
        destroyvm_wrong_surface = subprocess.run(
            [
                destroyvm,
                manifest_vm_name,
                "--force",
                "--uri",
                integration_uri,
                "--storage-root",
                str(lvlab_integration_storage_root),
            ],
            capture_output=True,
            text=True,
            check=False,
            timeout=_DESTROYVM_TIMEOUT_SECONDS,
        )
        assert destroyvm_wrong_surface.returncode != 0, (
            f"destroyvm should have refused the manifest VM "
            f"{manifest_vm_name!r} (no oneoff-{manifest_vm_name} domain "
            f"exists), but it exited 0:\n"
            f"stdout:\n{destroyvm_wrong_surface.stdout}\n"
            f"stderr:\n{destroyvm_wrong_surface.stderr}"
        )
        destroyvm_wrong_combined = (
            destroyvm_wrong_surface.stdout + destroyvm_wrong_surface.stderr
        )
        assert "not defined" in destroyvm_wrong_combined, (
            f"destroyvm refused the manifest VM but the error did not "
            f"say 'not defined' — the architectural error path may "
            f"have regressed:\n{destroyvm_wrong_combined}"
        )
        assert "lvlab destroy" in destroyvm_wrong_combined, (
            f"destroyvm refused the manifest VM but the error did not "
            f"point at 'lvlab destroy' — the operator-helpful nudge "
            f"may have regressed:\n{destroyvm_wrong_combined}"
        )

        # Confirm the failed destroyvm did NOT touch the manifest VM.
        assert expected_manifest_domain in list_domains(integration_uri), (
            f"destroyvm refused the manifest VM but the manifest "
            f"domain {expected_manifest_domain!r} is no longer in "
            f"virsh list — the architectural isolation has regressed "
            f"and destroyvm killed something it should not have"
        )

        # Step 7: destroyvm on the one-off's bare name succeeds.
        destroyvm_correct_surface = subprocess.run(
            [
                destroyvm,
                oneoff_vm_name,
                "--force",
                "--uri",
                integration_uri,
                "--storage-root",
                str(lvlab_integration_storage_root),
            ],
            capture_output=True,
            text=True,
            check=False,
            timeout=_DESTROYVM_TIMEOUT_SECONDS,
        )
        assert destroyvm_correct_surface.returncode == 0, (
            f"destroyvm on the one-off VM failed (exit "
            f"{destroyvm_correct_surface.returncode}):\n"
            f"stdout:\n{destroyvm_correct_surface.stdout}\n"
            f"stderr:\n{destroyvm_correct_surface.stderr}"
        )
        wait_for_no_domain(integration_uri, expected_oneoff_domain)

        # Step 8: the manifest VM survived the targeted destroyvm.
        assert expected_manifest_domain in list_domains(integration_uri), (
            f"destroyvm of the one-off succeeded but the manifest "
            f"domain {expected_manifest_domain!r} also disappeared — "
            f"the architectural isolation has regressed and "
            f"destroyvm reached across the surface boundary"
        )
    finally:
        # Best-effort cleanup of both halves. Each subprocess call
        # below tolerates non-zero exits because we want both to run
        # even if one fails; the session reapers are the final
        # safety net.
        subprocess.run(
            [
                destroyvm,
                oneoff_vm_name,
                "--force",
                "--uri",
                integration_uri,
                "--storage-root",
                str(lvlab_integration_storage_root),
            ],
            capture_output=True,
            text=True,
            check=False,
            timeout=_DESTROYVM_TIMEOUT_SECONDS,
        )
        subprocess.run(
            [lvlab, "destroy", manifest_vm_name, "--force"],
            capture_output=True,
            text=True,
            check=False,
            timeout=_LVLAB_DESTROY_TIMEOUT_SECONDS,
            cwd=tmp_path,
        )

    wait_for_no_domain(integration_uri, expected_manifest_domain)
