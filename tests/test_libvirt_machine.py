"""Unit tests for :class:`tkc_lvlab.utils.libvirt.Machine` methods that have
been ported to ``virsh`` (Phase 2).

These tests patch the ``virsh_*`` collaborators at the
``tkc_lvlab.utils.libvirt`` import boundary so nothing here actually shells
out. The ``Machine`` object is constructed without running ``__init__`` —
the methods under test depend only on ``libvirt_vm_name``, and the real
constructor has unrelated filesystem side effects.
"""

from __future__ import annotations

from unittest import mock

import pytest

from tkc_lvlab.utils.libvirt import Machine
from tkc_lvlab.utils.virsh import VirshError


@pytest.fixture
def machine() -> Machine:
    """A Machine stub whose only populated attribute is libvirt_vm_name."""
    m = object.__new__(Machine)
    m.libvirt_vm_name = "web01_lab"
    m.vm_name = "web01"
    return m


URI = "qemu:///session"


# ---------------------------------------------------------------------------
# exists_in_libvirt — return-shape and lookup behavior
# ---------------------------------------------------------------------------


def test_exists_in_libvirt_absent_returns_empty_strings(machine: Machine) -> None:
    """When the domain isn't in the list, return (False, "", "") — not the
    old (False, 0, 0) tuple — and don't call domstate at all."""
    with (
        mock.patch(
            "tkc_lvlab.utils.libvirt.virsh_list_all_names", return_value=["other_lab"]
        ) as list_mock,
        mock.patch("tkc_lvlab.utils.libvirt.virsh_domstate_reason") as state_mock,
    ):
        result = machine.exists_in_libvirt(URI)

    assert result == (False, "", "")
    list_mock.assert_called_once_with(URI)
    state_mock.assert_not_called()


def test_exists_in_libvirt_present_returns_state_and_reason(machine: Machine) -> None:
    """When the domain is present, surface the lowercase virsh state strings
    cli.py now compares against (``running``, ``shut off``, etc.)."""
    with (
        mock.patch(
            "tkc_lvlab.utils.libvirt.virsh_list_all_names",
            return_value=["web01_lab", "other_lab"],
        ),
        mock.patch(
            "tkc_lvlab.utils.libvirt.virsh_domstate_reason",
            return_value=("running", "booted"),
        ) as state_mock,
    ):
        result = machine.exists_in_libvirt(URI)

    assert result == (True, "running", "booted")
    state_mock.assert_called_once_with(URI, "web01_lab")


def test_exists_in_libvirt_namespacing_uses_libvirt_vm_name(machine: Machine) -> None:
    """The lookup must use the env-namespaced name, not the bare vm_name.
    Regression guard: ``machines[].vm_name`` of ``web01`` in two environments
    must not collide; only ``web01_<env>`` is the real domain name."""
    machine.libvirt_vm_name = "web01_prod"
    with mock.patch(
        "tkc_lvlab.utils.libvirt.virsh_list_all_names",
        return_value=["web01_dev"],  # the dev namespaced name, not prod
    ):
        exists, _, _ = machine.exists_in_libvirt(URI)
    assert exists is False


def test_exists_in_libvirt_domstate_race_treated_as_absent(machine: Machine) -> None:
    """If the domain vanishes between the list and the lookup, ``virsh``
    raises ``VirshError`` for the second call. The method should treat that
    as 'no longer present' rather than crashing the caller."""
    with (
        mock.patch(
            "tkc_lvlab.utils.libvirt.virsh_list_all_names", return_value=["web01_lab"]
        ),
        mock.patch(
            "tkc_lvlab.utils.libvirt.virsh_domstate_reason",
            side_effect=VirshError(1, "domain not found", ["domstate"]),
        ),
    ):
        result = machine.exists_in_libvirt(URI)

    assert result == (False, "", "")


def test_exists_in_libvirt_list_failure_propagates(machine: Machine) -> None:
    """A failure of the initial ``virsh list`` (URI unreachable, virsh
    missing, etc.) is an environmental problem — surface it, don't swallow."""
    with mock.patch(
        "tkc_lvlab.utils.libvirt.virsh_list_all_names",
        side_effect=VirshError(127, "virsh not found", ["list"]),
    ):
        with pytest.raises(VirshError):
            machine.exists_in_libvirt(URI)


# ---------------------------------------------------------------------------
# __init__ — shared_directories source-path expansion
# ---------------------------------------------------------------------------


def test_shared_directories_source_expands_tilde(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """``~/path`` in a manifest's shared_directories source gets expanded
    against the user's $HOME so the same manifest works across machines."""
    monkeypatch.setenv("HOME", str(tmp_path))
    config_defaults = {
        "shared_directories": [
            {"source": "~/repos", "mount_tag": "gitrepos"},
        ],
        "interfaces": {"nameservers": {}},
    }
    environment = {"name": "lab"}
    machine_cfg = {"vm_name": "web01"}

    m = Machine(machine_cfg, environment, config_defaults)

    assert m.shared_directories == [
        {"source": str(tmp_path / "repos"), "mount_tag": "gitrepos"},
    ]


def test_shared_directories_source_expands_envvar(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """``$VAR``-style references in shared_directories source are also
    expanded — matches the behavior of disk_image_basedir."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("LVLAB_TEST_SRC", str(tmp_path / "custom"))
    config_defaults = {
        "shared_directories": [
            {"source": "$LVLAB_TEST_SRC", "mount_tag": "custom"},
        ],
        "interfaces": {"nameservers": {}},
    }
    environment = {"name": "lab"}
    machine_cfg = {"vm_name": "web01"}

    m = Machine(machine_cfg, environment, config_defaults)

    assert m.shared_directories[0]["source"] == str(tmp_path / "custom")


# ---------------------------------------------------------------------------
# __init__ — Phase 12 network_type validation
# ---------------------------------------------------------------------------


def _minimal_config_defaults() -> dict:
    """Minimal config_defaults needed for Machine.__init__ to succeed.

    Real manifests carry more, but the network_type validation runs before
    most other fields are consulted, so a minimal shape is enough to
    isolate the validation behaviour.
    """
    return {"interfaces": {"nameservers": {}}}


def test_init_rejects_unknown_network_type() -> None:
    """An interface with an unknown network_type fails fast in __init__.

    Catching this at construction means the operator sees the error before
    any cloud-init / qcow2 / virt-install state is created.
    """
    config_defaults = _minimal_config_defaults()
    machine_cfg = {
        "vm_name": "web01",
        "interfaces": [{"name": "eth0", "network_type": "vlan-trunk"}],
    }
    with pytest.raises(ValueError, match="Invalid network_type 'vlan-trunk'"):
        Machine(machine_cfg, {"name": "lab"}, config_defaults)


def test_init_rejects_user_network_with_static_ip4() -> None:
    """User-mode networking + ip4 is contradictory; refuse at __init__ time.

    SLIRP/passt do not honour static IPs. If both are present the
    manifest is internally inconsistent; surfacing it at construction
    rather than at virt-install time gives a clear operator message.
    """
    config_defaults = _minimal_config_defaults()
    machine_cfg = {
        "vm_name": "web01",
        "interfaces": [
            {"name": "eth0", "network_type": "user", "ip4": "192.168.122.50"}
        ],
    }
    with pytest.raises(ValueError, match="does not honour static IPs"):
        Machine(machine_cfg, {"name": "lab"}, config_defaults)


def test_init_rejects_passt_network_with_static_ip4() -> None:
    """Same invariant as user-mode — passt also drops static IPs."""
    config_defaults = _minimal_config_defaults()
    machine_cfg = {
        "vm_name": "web01",
        "interfaces": [
            {"name": "eth0", "network_type": "passt", "ip4": "192.168.122.50"}
        ],
    }
    with pytest.raises(ValueError, match="does not honour static IPs"):
        Machine(machine_cfg, {"name": "lab"}, config_defaults)


def test_init_accepts_user_network_without_ip4() -> None:
    """User-mode + no ip4 is the supported shape; __init__ succeeds."""
    config_defaults = _minimal_config_defaults()
    machine_cfg = {
        "vm_name": "web01",
        "interfaces": [{"name": "eth0", "network_type": "user"}],
    }
    m = Machine(machine_cfg, {"name": "lab"}, config_defaults)
    assert m.interfaces[0]["network_type"] == "user"


def test_init_accepts_default_managed_network_with_ip4() -> None:
    """Managed network (the pre-Phase-12 default) + ip4 is the original
    supported combination — still works after the new validation lands."""
    config_defaults = _minimal_config_defaults()
    machine_cfg = {
        "vm_name": "web01",
        "interfaces": [{"name": "eth0", "network": "default", "ip4": "192.168.122.50"}],
    }
    m = Machine(machine_cfg, {"name": "lab"}, config_defaults)
    assert m.interfaces[0]["ip4"] == "192.168.122.50"
    # network_type omitted means the default behaviour.
    assert m.interfaces[0].get("network_type", "network") == "network"


# ---------------------------------------------------------------------------
# _virt_install_network_arg — Phase 12 virt-install argument assembly
# ---------------------------------------------------------------------------


def test_virt_install_network_arg_default_managed_network() -> None:
    """The default (no network_type) emits the same arg as pre-Phase-12.

    Regression guard: any existing manifest without network_type must
    produce the exact PCI-addressed managed-network arg that was
    hard-coded before Phase 12.
    """
    from tkc_lvlab.utils.libvirt import _virt_install_network_arg

    arg = _virt_install_network_arg({"name": "eth0", "network": "default"})
    assert arg.startswith("network=default,model=virtio,")
    assert "address.type=pci" in arg


def test_virt_install_network_arg_user_mode() -> None:
    """User-mode emits 'user,model=virtio' — no libvirt network name."""
    from tkc_lvlab.utils.libvirt import _virt_install_network_arg

    arg = _virt_install_network_arg({"name": "eth0", "network_type": "user"})
    assert arg == "user,model=virtio"


def test_virt_install_network_arg_passt() -> None:
    """passt emits 'passt,model=virtio' — no libvirt network name."""
    from tkc_lvlab.utils.libvirt import _virt_install_network_arg

    arg = _virt_install_network_arg({"name": "eth0", "network_type": "passt"})
    assert arg == "passt,model=virtio"


def test_virt_install_network_arg_user_mode_ignores_network_field() -> None:
    """An iface that happens to carry both network_type=user AND a leftover
    'network' key (e.g. inherited from config_defaults) still emits the
    user-mode arg — the network name is irrelevant for SLIRP/passt."""
    from tkc_lvlab.utils.libvirt import _virt_install_network_arg

    arg = _virt_install_network_arg(
        {"name": "eth0", "network": "default", "network_type": "user"}
    )
    assert arg == "user,model=virtio"


# ---------------------------------------------------------------------------
# Machine.deploy — subprocess env sanitization (Debian 13 portability)
# ---------------------------------------------------------------------------


def test_machine_deploy_passes_system_first_env_to_virt_install(tmp_path) -> None:
    """``Machine.deploy`` invokes virt-install with system-first PATH.

    Regression for the Debian 13 portability bug: virt-install on
    bookworm-and-newer uses ``#!/usr/bin/env python3``, so unless
    we pass an env with ``/usr/bin`` first on PATH, the venv's
    Python gets selected and ``import gi`` fails. Asserts the
    ``env=`` kwarg's PATH starts with the system bin paths.
    """
    from unittest import mock

    from tkc_lvlab.utils.libvirt import Machine

    m = object.__new__(Machine)
    m.libvirt_vm_name = "web01_lab"
    m.memory = 1024
    m.cpu = 1
    m.os = "debian13"
    m.interfaces = [{"name": "eth0", "network": "default"}]
    m.shared_directories = []

    with (
        mock.patch(
            "tkc_lvlab.utils.libvirt.resolve_os_variant",
            return_value=("debian13", None),
        ),
        mock.patch("tkc_lvlab.utils.libvirt.subprocess.run") as run,
    ):
        m.deploy(str(tmp_path), {}, "qemu:///session")

    assert run.call_count == 1
    env = run.call_args.kwargs["env"]
    assert env["PATH"].startswith(
        "/usr/bin:/usr/sbin"
    ), f"deploy must pass env with system bin paths first; got PATH={env['PATH']!r}"
