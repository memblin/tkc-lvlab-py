"""Unit tests for :class:`tkc_lvlab.utils.libvirt.Machine` methods that have
been ported to ``virsh`` (Phase 2).

These tests patch the ``virsh_*`` collaborators at the
``tkc_lvlab.utils.libvirt`` import boundary so nothing here actually shells
out. The ``Machine`` object is constructed without running ``__init__`` —
the methods under test depend only on ``libvirt_vm_name``, and the real
constructor has unrelated filesystem side effects.
"""

from __future__ import annotations

import re
from unittest import mock

import pytest
import yaml

from tkc_lvlab.config import NetworkDefaults
from tkc_lvlab.utils.cloud_init import NetworkConfig
from tkc_lvlab.utils.libvirt import Machine, _virt_install_network_arg
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
# __init__ — layered networks: bridge gateway/DNS fill (#138 Phase 3)
# ---------------------------------------------------------------------------


def _vlan10_networks() -> dict[str, NetworkDefaults]:
    """A ``networks:`` map with a bridge-style vlan10 entry."""
    return {
        "vlan10": NetworkDefaults(
            gateway="100.64.10.1",
            dns=["100.64.10.10", "100.64.10.11"],
            search=["lab.example"],
        )
    }


def test_init_fills_bridge_gateway_from_networks() -> None:
    """A static interface on a configured network inherits its gateway (#138)."""
    config_defaults = _minimal_config_defaults()
    machine_cfg = {
        "vm_name": "web01",
        "interfaces": [{"name": "eth0", "network": "vlan10", "ip4": "100.64.10.50/24"}],
    }
    m = Machine(
        machine_cfg, {"name": "lab"}, config_defaults, networks=_vlan10_networks()
    )
    assert m.interfaces[0]["ip4gw"] == "100.64.10.1"


def test_init_explicit_ip4gw_wins_over_networks() -> None:
    """An explicit interface ``ip4gw`` is never overwritten by the networks map."""
    config_defaults = _minimal_config_defaults()
    machine_cfg = {
        "vm_name": "web01",
        "interfaces": [
            {
                "name": "eth0",
                "network": "vlan10",
                "ip4": "100.64.10.50/24",
                "ip4gw": "100.64.10.254",
            }
        ],
    }
    m = Machine(
        machine_cfg, {"name": "lab"}, config_defaults, networks=_vlan10_networks()
    )
    assert m.interfaces[0]["ip4gw"] == "100.64.10.254"


def test_init_networks_skips_dhcp_interface() -> None:
    """A DHCP interface (no ip4) gets no gateway even if its network is configured."""
    config_defaults = _minimal_config_defaults()
    machine_cfg = {
        "vm_name": "web01",
        "interfaces": [{"name": "eth0", "network": "vlan10"}],  # no ip4 -> DHCP
    }
    m = Machine(
        machine_cfg, {"name": "lab"}, config_defaults, networks=_vlan10_networks()
    )
    assert "ip4gw" not in m.interfaces[0]


def test_init_fills_nameservers_from_networks_when_absent() -> None:
    """With no machine/defaults nameservers, the interface's network supplies DNS."""
    config_defaults = _minimal_config_defaults()  # interfaces.nameservers == {}
    machine_cfg = {
        "vm_name": "web01",
        "interfaces": [{"name": "eth0", "network": "vlan10", "ip4": "100.64.10.50/24"}],
    }
    m = Machine(
        machine_cfg, {"name": "lab"}, config_defaults, networks=_vlan10_networks()
    )
    assert m.nameservers == {
        "addresses": ["100.64.10.10", "100.64.10.11"],
        "search": ["lab.example"],
    }


def test_init_explicit_nameservers_win_over_networks() -> None:
    """A machine's own nameservers are never overridden by the networks map."""
    config_defaults = _minimal_config_defaults()
    machine_cfg = {
        "vm_name": "web01",
        "nameservers": {"addresses": ["9.9.9.9"], "search": ["own.example"]},
        "interfaces": [{"name": "eth0", "network": "vlan10", "ip4": "100.64.10.50/24"}],
    }
    m = Machine(
        machine_cfg, {"name": "lab"}, config_defaults, networks=_vlan10_networks()
    )
    assert m.nameservers == {"addresses": ["9.9.9.9"], "search": ["own.example"]}


def test_init_no_networks_leaves_interface_and_nameservers_untouched() -> None:
    """Default (networks=None): no gateway/DNS filling — backward compatible."""
    config_defaults = _minimal_config_defaults()
    machine_cfg = {
        "vm_name": "web01",
        "interfaces": [{"name": "eth0", "network": "vlan10", "ip4": "100.64.10.50/24"}],
    }
    m = Machine(machine_cfg, {"name": "lab"}, config_defaults)
    assert "ip4gw" not in m.interfaces[0]
    assert m.nameservers == {}


# ---------------------------------------------------------------------------
# __init__ — IPv6 dual-stack (#137)
# ---------------------------------------------------------------------------


def test_init_preserves_dual_stack_interface_fields() -> None:
    """A manifest interface with both ip4/ip4gw and ip6/ip6gw round-trips intact.

    Machine.__init__ does field-by-field merge of defaults + per-machine
    interface dicts. The IPv6 fields must travel through unmodified so
    the cloud-init render pipeline sees them.
    """
    config_defaults = _minimal_config_defaults()
    machine_cfg = {
        "vm_name": "web01",
        "interfaces": [
            {
                "name": "eth0",
                "network": "default-dualstack",
                "ip4": "192.168.130.50/24",
                "ip4gw": "192.168.130.1",
                "ip6": "2001:db8:130::50/64",
                "ip6gw": "2001:db8:130::1",
            }
        ],
    }
    m = Machine(machine_cfg, {"name": "lab"}, config_defaults)
    iface = m.interfaces[0]
    assert iface["ip4"] == "192.168.130.50/24"
    assert iface["ip4gw"] == "192.168.130.1"
    assert iface["ip6"] == "2001:db8:130::50/64"
    assert iface["ip6gw"] == "2001:db8:130::1"


def test_init_merges_ip6_defaults_into_interface() -> None:
    """An ``interfaces.ip6gw`` declared at config_defaults level merges into
    each per-machine interface that lacks one.

    Mirrors the existing pattern where ``config_defaults['interfaces']``
    seeds per-interface defaults. Lets a manifest say "every machine on
    this network uses this v6 gateway" without repeating per-VM.
    """
    config_defaults = {
        "interfaces": {
            "nameservers": {},
            "ip6gw": "2001:db8:130::1",
        }
    }
    machine_cfg = {
        "vm_name": "web01",
        "interfaces": [
            {
                "name": "eth0",
                "ip6": "2001:db8:130::50/64",
            }
        ],
    }
    m = Machine(machine_cfg, {"name": "lab"}, config_defaults)
    assert m.interfaces[0]["ip6gw"] == "2001:db8:130::1"


def test_init_rejects_user_network_with_static_ip6() -> None:
    """User-mode networking + ip6 is contradictory; refuse at __init__ time.

    SLIRP/passt don't honour static IPv6 any more than they honour static
    IPv4 — surface the misconfiguration loudly before any state is created.
    """
    config_defaults = _minimal_config_defaults()
    machine_cfg = {
        "vm_name": "web01",
        "interfaces": [
            {"name": "eth0", "network_type": "user", "ip6": "2001:db8::5/64"}
        ],
    }
    with pytest.raises(ValueError, match="does not honour static IPs"):
        Machine(machine_cfg, {"name": "lab"}, config_defaults)


def test_init_rejects_passt_network_with_static_ip6() -> None:
    """Same invariant for passt: static v6 is rejected."""
    config_defaults = _minimal_config_defaults()
    machine_cfg = {
        "vm_name": "web01",
        "interfaces": [
            {"name": "eth0", "network_type": "passt", "ip6": "2001:db8::5/64"}
        ],
    }
    with pytest.raises(ValueError, match="does not honour static IPs"):
        Machine(machine_cfg, {"name": "lab"}, config_defaults)


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


def test_virt_install_network_arg_threads_pinned_mac_managed() -> None:
    """A pinned ``macaddress`` is threaded into the managed-network arg as
    ``mac=`` so it matches the cloud-init ``match: macaddress`` selector."""
    arg = _virt_install_network_arg(
        {"name": "eth0", "network": "default", "macaddress": "52:54:00:de:ad:be"}
    )
    assert arg.startswith("network=default,model=virtio,mac=52:54:00:de:ad:be,")
    assert "address.type=pci" in arg


def test_virt_install_network_arg_threads_pinned_mac_user_mode() -> None:
    """User-mode carries the pinned MAC too — the guest NIC still has one
    and the NM renderer still matches by it."""
    arg = _virt_install_network_arg(
        {"name": "eth0", "network_type": "user", "macaddress": "52:54:00:de:ad:be"}
    )
    assert arg == "user,model=virtio,mac=52:54:00:de:ad:be"


# ---------------------------------------------------------------------------
# Machine MAC pinning — cross-renderer NIC matching (Fedora static fix)
# ---------------------------------------------------------------------------


def test_machine_pins_qemu_oui_mac_per_interface() -> None:
    """Machine.__init__ assigns a QEMU-OUI MAC to each interface that omits
    one, so the network-config can match by MAC."""
    m = Machine(
        {"vm_name": "web01", "interfaces": [{"name": "eth0", "network": "default"}]},
        {"name": "lab"},
        _minimal_config_defaults(),
    )
    mac = m.interfaces[0]["macaddress"]
    assert mac.startswith("52:54:00:")
    assert re.fullmatch(r"(?:[0-9a-f]{2}:){5}[0-9a-f]{2}", mac)


def test_machine_respects_manifest_supplied_macaddress() -> None:
    """A manifest-provided macaddress is preserved, not overwritten."""
    m = Machine(
        {
            "vm_name": "web01",
            "interfaces": [{"name": "eth0", "macaddress": "52:54:00:00:00:01"}],
        },
        {"name": "lab"},
        _minimal_config_defaults(),
    )
    assert m.interfaces[0]["macaddress"] == "52:54:00:00:00:01"


def test_machine_virt_install_mac_matches_network_config_mac() -> None:
    """The MAC in the ``--network`` arg equals the ``match: macaddress`` in
    the rendered network-config.

    This is the invariant that makes the NetworkManager-renderer (Fedora)
    static config bind to the right NIC. If the deploy arg and the cloud-init
    config ever pinned different MACs, the guest profile would match no
    device and fall back to NM's default DHCP — exactly the bug this guards.
    """
    m = Machine(
        {
            "vm_name": "web01",
            "interfaces": [
                {
                    "name": "eth0",
                    "network": "default",
                    "ip4": "192.168.122.50/24",
                    "ip4gw": "192.168.122.1",
                }
            ],
        },
        {"name": "lab"},
        _minimal_config_defaults(),
    )
    pinned = m.interfaces[0]["macaddress"]

    arg = _virt_install_network_arg(m.interfaces[0])
    assert f"mac={pinned}" in arg

    rendered = yaml.safe_load(
        NetworkConfig(2, m.interfaces, m.nameservers).render_config()
    )
    assert rendered["network"]["ethernets"]["eth0"]["match"] == {"macaddress": pinned}


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


def test_machine_deploy_honours_os_variant_override(tmp_path) -> None:
    """``deploy`` resolves the os-variant from the passed value (the
    image entry's override) rather than deriving from ``machine.os``.

    Guards the convergence fix: a custom image keyed ``ubuntu2204`` (which
    would derive the osinfo-unknown ``ubuntu2204``) can pin ``ubuntu22.04``
    via its catalog/manifest entry, and the manifest deploy path honours it
    — the same override createvm already respected.
    """
    from unittest import mock

    from tkc_lvlab.utils.libvirt import Machine

    m = object.__new__(Machine)
    m.libvirt_vm_name = "u_lab"
    m.memory = 1024
    m.cpu = 1
    m.os = "ubuntu2204"
    m.interfaces = [{"name": "eth0", "network": "default"}]
    m.shared_directories = []

    with (
        mock.patch(
            "tkc_lvlab.utils.libvirt.resolve_os_variant",
            return_value=("ubuntu22.04", None),
        ) as rov,
        mock.patch("tkc_lvlab.utils.libvirt.subprocess.run"),
    ):
        m.deploy(str(tmp_path), {}, "qemu:///session", os_variant="ubuntu22.04")

    # The override is what gets resolved, NOT machine.os.split('-')[0].
    rov.assert_called_once_with("ubuntu22.04")
