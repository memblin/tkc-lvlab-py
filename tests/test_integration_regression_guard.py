"""Integration test for the createvm/lvlab cross-surface isolation invariant.

`createvm` / `deletevm` (raw libvirt domain names) and `lvlab` (manifest
names → `<vm>_<env>` domains) are independent surfaces. The invariant that
matters now that the `oneoff-` prefix is gone is one-directional:

- **lvlab's manifest commands never see or touch a one-off VM.** `lvlab
    status` must not list a `createvm`-made domain, and `lvlab destroy
    <oneoff_name>` must be a no-op (the name isn't in the manifest), leaving
    the one-off VM defined.
- `deletevm` makes no such promise in reverse — it acts on the raw libvirt
    name, so `deletevm <vm>_<env>` would intentionally remove a manifest
    VM. That's by design and not tested here.

This test exercises the invariant on the system URI (createvm is
qemu:///system only; session is skipped):

1. `createvm` makes a one-off domain `<vm_A>`.
2. `lvlab up` makes a manifest domain `<vm_B>_<env>`.
3. Both coexist on the same URI.
4. `lvlab status` reports the manifest VM only — never the one-off.
5. `lvlab destroy <vm_A>` is a no-op (not in the manifest); the one-off
    stays defined and the manifest VM is untouched.
6. `deletevm <vm_A>` removes the one-off by its raw name.

Gated by `LVLAB_INTEGRATION=1`. Default test runs skip via the
`integration` marker.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from tests.conftest import assert_owned_by_test, make_test_name
from tests.integration_helpers import (
    find_entry_point,
    list_domains,
    render_manifest,
    wait_for_no_domain,
)

_CREATEVM_TIMEOUT_SECONDS = 300
_DELETEVM_TIMEOUT_SECONDS = 60
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
    """lvlab's manifest surface stays blind to one-off VMs.

    Creates one VM via each path so both coexist, then verifies that
    ``lvlab status`` sees only the manifest VM and that ``lvlab destroy``
    on the one-off's name is a no-op — while ``deletevm`` cleans the
    one-off up by its raw libvirt name.

    Args:
        integration_uri: libvirt URI parametrized by the ``integration_uri``
            fixture. createvm targets qemu:///system, so session is skipped.
        lvlab_integration_storage_root: libvirt-readable test storage root.
            Becomes both createvm's ``--storage-root`` and the manifest's
            ``disk_image_basedir``.
        test_ssh_pubkey_path: Path to a throwaway SSH public key.
        tmp_path: pytest scratch dir for the per-test ``Lvlab.yml``.
    """
    if "session" in integration_uri:
        pytest.skip("createvm targets qemu:///system only")

    createvm = find_entry_point("createvm")
    deletevm = find_entry_point("deletevm")
    lvlab = find_entry_point("lvlab")

    oneoff_vm_name = make_test_name("regression-oneoff")
    manifest_vm_name = make_test_name("regression-manifest")
    env_name = make_test_name("regression-env")

    expected_oneoff_domain = oneoff_vm_name  # raw name, no prefix
    expected_manifest_domain = f"{manifest_vm_name}_{env_name}"
    assert_owned_by_test(expected_oneoff_domain)
    assert_owned_by_test(expected_manifest_domain)

    manifest_path = tmp_path / "Lvlab.yml"
    manifest_path.write_text(
        render_manifest(
            env_name=env_name,
            uri=integration_uri,
            storage_root=lvlab_integration_storage_root,
            vm_name=manifest_vm_name,
            pubkey_path=test_ssh_pubkey_path,
        )
    )

    create_result = subprocess.run(
        [
            createvm,
            oneoff_vm_name,
            "debian13",
            "--storage-root",
            str(lvlab_integration_storage_root),
            "--public-key",
            str(test_ssh_pubkey_path),
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

        domains_after_setup = list_domains(integration_uri)
        assert expected_oneoff_domain in domains_after_setup, (
            f"createvm reported success but {expected_oneoff_domain!r} "
            f"missing from virsh list on {integration_uri}"
        )
        assert expected_manifest_domain in domains_after_setup, (
            f"lvlab up reported success but {expected_manifest_domain!r} "
            f"missing from virsh list on {integration_uri}"
        )

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
            f"lvlab status leaked the one-off VM name {oneoff_vm_name!r} — "
            f"the manifest surface must not see createvm-managed VMs:\n"
            f"{combined_status_output}"
        )

        # lvlab destroy on the one-off's name must be a no-op: the name is
        # not in the manifest, so lvlab refuses to resolve it and must not
        # remove the one-off domain.
        lvlab_destroy_oneoff = subprocess.run(
            [lvlab, "destroy", oneoff_vm_name, "--force"],
            capture_output=True,
            text=True,
            check=False,
            timeout=_LVLAB_DESTROY_TIMEOUT_SECONDS,
            cwd=tmp_path,
        )
        assert expected_oneoff_domain in list_domains(integration_uri), (
            f"lvlab destroy {oneoff_vm_name!r} removed a one-off VM that is "
            f"not in the manifest — the manifest surface reached across the "
            f"boundary:\nstdout:\n{lvlab_destroy_oneoff.stdout}\n"
            f"stderr:\n{lvlab_destroy_oneoff.stderr}"
        )

        # deletevm removes the one-off by its raw libvirt name.
        deletevm_result = subprocess.run(
            [
                deletevm,
                oneoff_vm_name,
                "--force",
                "--storage-root",
                str(lvlab_integration_storage_root),
            ],
            capture_output=True,
            text=True,
            check=False,
            timeout=_DELETEVM_TIMEOUT_SECONDS,
        )
        assert deletevm_result.returncode == 0, (
            f"deletevm on the one-off VM failed (exit "
            f"{deletevm_result.returncode}):\n"
            f"stdout:\n{deletevm_result.stdout}\n"
            f"stderr:\n{deletevm_result.stderr}"
        )
        wait_for_no_domain(integration_uri, expected_oneoff_domain)

        assert expected_manifest_domain in list_domains(integration_uri), (
            f"deletevm of the one-off succeeded but the manifest domain "
            f"{expected_manifest_domain!r} also disappeared — deletevm "
            f"reached across the surface boundary"
        )
    finally:
        # Best-effort cleanup of both halves; session reapers are the final
        # safety net.
        subprocess.run(
            [
                deletevm,
                oneoff_vm_name,
                "--force",
                "--storage-root",
                str(lvlab_integration_storage_root),
            ],
            capture_output=True,
            text=True,
            check=False,
            timeout=_DELETEVM_TIMEOUT_SECONDS,
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
