"""Unit tests for :mod:`tkc_lvlab.scripts.createvm`.

These tests use Typer's :class:`CliRunner` against the ``run`` command and
patch every external interaction at the import boundary so nothing hits the
real virsh, qemu-img, virt-install, openssl, or filesystem beyond the
test's own ``tmp_path``.

The script is a faithful port of the ``lvscripts-py`` reference ``createvm``,
adapted for lvlab's image storage + catalog. What these lock in:

- Positional ``VM_NAME`` + ``VM_DISTRO`` (both required together); a
    missing pair errors, a half pair errors.
- ``VM_DISTRO`` resolves against the built-in catalog merged with an
    ``Lvlab.yml`` ``images:`` section (manifest wins); an unknown name
    errors with the available list.
- ``--ip4`` is validated AND rendered into the guest's network-config
    (static addressing actually reaches the VM).
- The disk is always a standalone copy (cp + qemu-img resize).
- virt-install targets qemu:///system with a managed network and
    system-first PATH.
- Cleanup-on-failure wipes the partial VM dir; an existing dir or a
    pre-existing domain refuses.

All subprocess calls are intercepted; pytest never invokes a real binary.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from unittest import mock

import pytest
from typer.testing import CliRunner

from tkc_lvlab.config import HostConfig, NetworkDefaults
from tkc_lvlab.scripts import createvm as cv_mod
from tkc_lvlab.scripts.createvm import (
    BUILTIN_IMAGES,
    ensure_cidr,
    parse_disk_size_to_bytes,
    parse_ip4_option,
    parse_memory_to_mib,
    resolve_catalog,
    resolve_image_entry,
    run,
    storage_dir_for,
)
from tkc_lvlab.utils.network import LibvirtNetworkError, LibvirtNetworkInfo
from tkc_lvlab.utils.requirements import DependencyError


_GIB = 1024**3

# Virtual size that the mocked ``qemu-img info`` reports for the base image.
# Mutable so a test can dial it up (e.g. to a 10 GiB base) before invoking.
_FAKE_IMAGE_VIRTUAL_SIZE = {"bytes": 2 * _GIB}


def _fake_subprocess_run(argv, *args, **kwargs):  # type: ignore[no-untyped-def]
    """Stand-in for ``subprocess.run`` in createvm.

    Returns a JSON ``virtual-size`` payload for ``qemu-img info`` so the
    resize-skip logic has a real size to compare against; every other
    command (``qemu-img resize``, ``virt-install``, ...) gets an empty
    success result.
    """
    if argv[:2] == ["qemu-img", "info"]:
        stdout = json.dumps({"virtual-size": _FAKE_IMAGE_VIRTUAL_SIZE["bytes"]})
        return subprocess.CompletedProcess(
            args=argv, returncode=0, stdout=stdout, stderr=""
        )
    return subprocess.CompletedProcess(args=argv, returncode=0, stdout="", stderr="")


# ---------------------------------------------------------------------------
# Storage path helper
# ---------------------------------------------------------------------------


def test_storage_dir_for_under_root(tmp_path: Path) -> None:
    """Per-VM storage lands under <root>/<vm_name>/, not in <root>/ directly."""
    assert storage_dir_for("alpha", root=tmp_path) == tmp_path / "alpha"


# ---------------------------------------------------------------------------
# Catalog resolution: merge + metadata derivation
# ---------------------------------------------------------------------------


def test_resolve_catalog_merges_manifest_over_builtins() -> None:
    """Manifest images override built-ins on collision; built-ins survive otherwise."""
    manifest = {
        "debian12": {
            "image_url": "http://host/custom-debian12.qcow2",
            "network_version": 1,
        },
        "myapp": {"image_url": "http://host/myapp.qcow2"},
    }
    catalog = resolve_catalog(manifest)
    assert catalog["debian12"]["image_url"] == "http://host/custom-debian12.qcow2"
    assert "myapp" in catalog
    assert "fedora44" in catalog


def test_resolve_catalog_none_is_builtins_only() -> None:
    """With no manifest, the catalog is exactly the built-ins."""
    assert resolve_catalog(None).keys() == BUILTIN_IMAGES.keys()


def test_resolve_image_entry_is_case_insensitive() -> None:
    """VM_DISTRO matching ignores case."""
    entry = resolve_image_entry("DEBIAN12", resolve_catalog(None))
    assert entry.os_variant == "debian12"
    assert entry.default_username == "debian"


def test_resolve_image_entry_unknown_distro_lists_available() -> None:
    """An unknown VM_DISTRO raises ValueError naming the available keys."""
    with pytest.raises(ValueError, match="Unknown distro 'nope'. Available:"):
        resolve_image_entry("nope", resolve_catalog(None))


def test_resolve_image_entry_derives_metadata_for_manifest_image() -> None:
    """A manifest image with no os_variant/username gets derived values."""
    catalog = resolve_catalog({"debian12-salt": {"image_url": "http://h/x.qcow2"}})
    entry = resolve_image_entry("debian12-salt", catalog)
    assert entry.os_variant == "debian12"  # text before first '-'
    assert entry.default_username == "debian"  # family map
    assert entry.network_version == 2  # default when omitted


def test_resolve_image_entry_manifest_can_override_metadata() -> None:
    """Explicit os_variant/username in the manifest win over derivation."""
    catalog = resolve_catalog(
        {
            "debian12": {
                "image_url": "http://h/x.qcow2",
                "os_variant": "debian-custom",
                "username": "admin",
            }
        }
    )
    entry = resolve_image_entry("debian12", catalog)
    assert entry.os_variant == "debian-custom"
    assert entry.default_username == "admin"


def test_builtin_catalog_includes_known_distros() -> None:
    """The catalog has stable distro families on the shared image-entry schema."""
    assert "debian12" in BUILTIN_IMAGES
    assert "debian13" in BUILTIN_IMAGES
    for name, cfg in BUILTIN_IMAGES.items():
        assert cfg["image_url"].startswith("https://"), name
        assert cfg["checksum_type"] in {"sha256", "sha512"}, name
        assert cfg["network_version"] in {1, 2}, name


# ---------------------------------------------------------------------------
# Value normalizers
# ---------------------------------------------------------------------------


def test_parse_ip4_bare_uses_default_network() -> None:
    """Bare 'IP' uses the default network."""
    assert parse_ip4_option("192.168.122.50", "default") == (
        "default",
        "192.168.122.50",
    )


def test_parse_ip4_network_comma_ip() -> None:
    """'NETWORK,IP' splits cleanly."""
    assert parse_ip4_option("vlan10,100.64.10.50", "default") == (
        "vlan10",
        "100.64.10.50",
    )


def test_parse_ip4_rejects_empty_segments() -> None:
    """An 'IP,' or ',NETWORK' format is invalid — surfaces as ValueError."""
    with pytest.raises(ValueError):
        parse_ip4_option("vlan10,", "default")
    with pytest.raises(ValueError):
        parse_ip4_option(",192.168.122.50", "default")


@pytest.mark.parametrize("sentinel", ["dhcp", "default", "auto", "DHCP", "Auto"])
def test_parse_ip4_bare_dhcp_sentinel_means_dhcp(sentinel: str) -> None:
    """A bare DHCP sentinel resolves the IP to None (DHCP) on the default network."""
    assert parse_ip4_option(sentinel, "default") == ("default", None)


@pytest.mark.parametrize("sentinel", ["dhcp", "default", "auto"])
def test_parse_ip4_network_comma_dhcp_sentinel_keeps_network(sentinel: str) -> None:
    """'NETWORK,dhcp' keeps the chosen network but takes the DHCP path (IP None)."""
    assert parse_ip4_option(f"vlan10,{sentinel}", "default") == ("vlan10", None)


def test_parse_ip4_bare_network_name_means_dhcp_on_that_network() -> None:
    """A bare non-IP-ish token is a network name → DHCP on it (#136), e.g.
    ``--ip4 vlan10``. The default network is *not* used."""
    assert parse_ip4_option("vlan10", "default") == ("vlan10", None)


def test_parse_ip4_bare_ip_ish_value_stays_on_static_path() -> None:
    """A bare IP-ish token (digits/dots, optional CIDR) stays a static IP on the
    default network — even when out of range, so a numeric typo reaches the
    #105 'not a valid IPv4 address' validation rather than a network lookup."""
    assert parse_ip4_option("192.168.1.300", "default") == ("default", "192.168.1.300")
    assert parse_ip4_option("10.0.0.0/24", "default") == ("default", "10.0.0.0/24")


def test_ensure_cidr_appends_and_preserves() -> None:
    """ensure_cidr appends the netmask only when the IP lacks one."""
    assert ensure_cidr("192.168.122.50", "24") == "192.168.122.50/24"
    assert ensure_cidr("192.168.122.50/25", "24") == "192.168.122.50/25"


def test_parse_memory_to_mib_units() -> None:
    """Plain values and unit suffixes both convert to a MiB string."""
    assert parse_memory_to_mib("2048") == "2048"
    assert parse_memory_to_mib("2G") == "2048"
    assert parse_memory_to_mib("512M") == "512"
    with pytest.raises(ValueError):
        parse_memory_to_mib("lots")


def test_parse_disk_size_to_bytes_units() -> None:
    """Suffixes are binary (1024-based), matching qemu-img; bare = bytes."""
    assert parse_disk_size_to_bytes("1G") == 1024**3
    assert parse_disk_size_to_bytes("512M") == 512 * 1024**2
    assert parse_disk_size_to_bytes("35G") == 35 * 1024**3
    # Bare number is a raw byte count, not a unit.
    assert parse_disk_size_to_bytes("10737418240") == 10 * 1024**3
    with pytest.raises(ValueError):
        parse_disk_size_to_bytes("big")


# ---------------------------------------------------------------------------
# CLI orchestration
# ---------------------------------------------------------------------------


def _nat_network_info() -> LibvirtNetworkInfo:
    """Build a NAT LibvirtNetworkInfo matching libvirt's default network."""
    return LibvirtNetworkInfo(
        name="default",
        forward_mode="nat",
        gateway_ip="192.168.122.1",
        netmask="255.255.255.0",
        dhcp_start="192.168.122.2",
        dhcp_end="192.168.122.254",
    )


def _bridge_network_info() -> LibvirtNetworkInfo:
    """Build a bridge LibvirtNetworkInfo (no libvirt-managed DNS/gateway)."""
    return LibvirtNetworkInfo(
        name="vlan10",
        forward_mode="bridge",
        gateway_ip=None,
        netmask=None,
        dhcp_start=None,
        dhcp_end=None,
    )


@pytest.fixture
def all_external_mocked(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> dict:
    """Patch every external dependency in the createvm module.

    Returns a dict of the relevant mocks so individual tests can assert on
    call args.
    """
    mocks = {
        "tooling": mock.Mock(),
        "get_network_info": mock.Mock(return_value=_nat_network_info()),
        "validate_static_ip": mock.Mock(),
        "resolve_network_settings": mock.Mock(
            return_value=(["192.168.122.1"], "192.168.122.1", [])
        ),
        "discover_default_public_keys": mock.Mock(
            return_value=[
                "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIBBBBBBBBBBB tester@laptop"
            ]
        ),
        "generate_password_phrase": mock.Mock(return_value="correct-horse-battery"),
        "hash_password_sha512": mock.Mock(return_value="$6$rounds=4096$abc$xyz"),
        "ensure_image_available": mock.Mock(),
        "vm_exists": mock.Mock(return_value=False),
        "wait_for_dhcp_lease": mock.Mock(return_value=None),
        "subprocess_run": mock.Mock(side_effect=_fake_subprocess_run),
        "copyfile": mock.Mock(),
    }

    monkeypatch.setattr(cv_mod, "check_createvm_tooling", mocks["tooling"])
    monkeypatch.setattr(cv_mod, "get_network_info", mocks["get_network_info"])
    monkeypatch.setattr(cv_mod, "validate_static_ip", mocks["validate_static_ip"])
    monkeypatch.setattr(
        cv_mod, "resolve_network_settings", mocks["resolve_network_settings"]
    )
    monkeypatch.setattr(
        cv_mod, "discover_default_public_keys", mocks["discover_default_public_keys"]
    )
    monkeypatch.setattr(
        cv_mod, "generate_password_phrase", mocks["generate_password_phrase"]
    )
    monkeypatch.setattr(cv_mod, "hash_password_sha512", mocks["hash_password_sha512"])
    monkeypatch.setattr(
        cv_mod, "_ensure_image_available", mocks["ensure_image_available"]
    )
    monkeypatch.setattr(cv_mod, "vm_exists", mocks["vm_exists"])
    monkeypatch.setattr(cv_mod, "_wait_for_dhcp_lease", mocks["wait_for_dhcp_lease"])

    monkeypatch.setattr(subprocess, "run", mocks["subprocess_run"])
    monkeypatch.setattr("tkc_lvlab.scripts.createvm.shutil.copyfile", mocks["copyfile"])

    # Bypass osinfo-db lookup (would add a virt-install subprocess call).
    monkeypatch.setattr(cv_mod, "resolve_os_variant", lambda v: (v, None))

    # Built-ins-only by default so these tests don't depend on a cwd Lvlab.yml
    # or a host-wide /etc/Lvlab.yml (empty HostConfig => no images/networks).
    monkeypatch.setattr(
        cv_mod, "load_host_config", lambda config_path=None: HostConfig()
    )
    # Keep the completion report deterministic regardless of cwd.
    monkeypatch.setattr(cv_mod, "_manifest_path_used", lambda config_path: None)

    # Stub CloudInitIso.write so it doesn't touch real pycdlib.
    monkeypatch.setattr(
        "tkc_lvlab.scripts.createvm.CloudInitIso.write", lambda self: True
    )

    # Stub _build_cloud_image so we don't need real URLs / disk paths.
    fake_image = mock.Mock()
    fake_image.image_fpath = str(tmp_path / "fake-cloud.qcow2")
    fake_image.image_url = "https://example.invalid/img.qcow2"
    fake_image.checksum_url = "https://example.invalid/sum"
    monkeypatch.setattr(cv_mod, "_build_cloud_image", lambda *a, **kw: fake_image)
    mocks["fake_image"] = fake_image
    return mocks


def _invoke(args: list[str], tmp_path: Path) -> "object":
    """Invoke createvm with the per-VM storage root pointed at tmp_path."""
    return CliRunner().invoke(run, [*args, "--storage-root", str(tmp_path)])


def test_happy_path_positional_args(all_external_mocked: dict, tmp_path: Path) -> None:
    """Positional VM_NAME + VM_DISTRO: returns 0, virt-install called once."""
    result = _invoke(["testvm.local", "debian12"], tmp_path)
    assert result.exit_code == 0, result.output

    virt_install_calls = [
        c
        for c in all_external_mocked["subprocess_run"].call_args_list
        if c.args[0][0] == "virt-install"
    ]
    assert len(virt_install_calls) == 1
    argv = virt_install_calls[0].args[0]
    assert "--name=testvm.local" in argv
    assert f"--connect={cv_mod._SYSTEM_URI}" in argv
    assert any(arg.startswith("network=default,") for arg in argv)
    # Reference graphics: spice on loopback.
    assert "spice,listen=127.0.0.1" in argv


def test_missing_arguments_errors(all_external_mocked: dict, tmp_path: Path) -> None:
    """No positional args (and no --init-cloud-images) errors clearly."""
    result = _invoke([], tmp_path)
    assert result.exit_code != 0
    assert "Missing required arguments" in result.output


def test_half_positional_pair_errors(all_external_mocked: dict, tmp_path: Path) -> None:
    """Only VM_NAME (no VM_DISTRO) errors: they must be provided together."""
    result = _invoke(["testvm.local"], tmp_path)
    assert result.exit_code != 0
    assert "must be provided together" in result.output


def test_version_flag() -> None:
    """--version prints 'createvm <version>' and exits 0."""
    result = CliRunner().invoke(run, ["--version"])
    assert result.exit_code == 0
    assert result.output.startswith("createvm ")


def test_copies_disk_then_resizes(all_external_mocked: dict, tmp_path: Path) -> None:
    """The cloud image is copied (standalone disk) then qemu-img resize'd.

    Default ``--disk-size`` (35G) exceeds the mocked 2 GiB base virtual size,
    so the resize runs as before. The provision first probes the base size
    via ``qemu-img info``.
    """
    result = _invoke(["testvm.local", "debian12"], tmp_path)
    assert result.exit_code == 0, result.output

    all_external_mocked["copyfile"].assert_called_once()
    qemu_img_calls = [
        c
        for c in all_external_mocked["subprocess_run"].call_args_list
        if c.args[0][0] == "qemu-img"
    ]
    subcommands = [c.args[0][1] for c in qemu_img_calls]
    assert subcommands == ["info", "resize"]


def test_resize_skipped_when_disk_size_le_base(
    all_external_mocked: dict, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A --disk-size at/below the base virtual size skips resize and warns.

    qemu-img cannot shrink a qcow2, so requesting 5G against a 10 GiB base
    must not call ``qemu-img resize`` (which would crash the provision);
    instead it keeps the base size and prints a warning naming both sizes.
    """
    monkeypatch.setitem(_FAKE_IMAGE_VIRTUAL_SIZE, "bytes", 10 * _GIB)

    result = _invoke(["testvm.local", "debian12", "--disk-size", "5G"], tmp_path)
    assert result.exit_code == 0, result.output

    qemu_img_calls = [
        c
        for c in all_external_mocked["subprocess_run"].call_args_list
        if c.args[0][0] == "qemu-img"
    ]
    subcommands = [c.args[0][1] for c in qemu_img_calls]
    # info ran (to learn the base size); resize did NOT.
    assert "info" in subcommands
    assert "resize" not in subcommands
    assert "skipping resize" in result.output
    # Both sizes are named in the warning.
    assert "5G" in result.output
    assert "10G" in result.output


def test_resize_runs_when_disk_size_gt_base(
    all_external_mocked: dict, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A --disk-size above the base virtual size still grows the disk."""
    monkeypatch.setitem(_FAKE_IMAGE_VIRTUAL_SIZE, "bytes", 10 * _GIB)

    result = _invoke(["testvm.local", "debian12", "--disk-size", "35G"], tmp_path)
    assert result.exit_code == 0, result.output

    resize_calls = [
        c
        for c in all_external_mocked["subprocess_run"].call_args_list
        if c.args[0][0] == "qemu-img" and c.args[0][1] == "resize"
    ]
    assert len(resize_calls) == 1
    assert resize_calls[0].args[0][-1] == "35G"


def test_dependency_failure_exits_nonzero(
    all_external_mocked: dict, tmp_path: Path
) -> None:
    """DependencyError from the tooling check produces a clear nonzero exit."""
    all_external_mocked["tooling"].side_effect = DependencyError(
        "Missing required system binaries for createvm:\n- virsh\nInstall ..."
    )
    result = _invoke(["testvm.local", "debian12"], tmp_path)
    assert result.exit_code != 0
    assert "Missing required system binaries" in result.output


def test_bridge_network_without_defaults_fails(
    all_external_mocked: dict, tmp_path: Path
) -> None:
    """A bridge network rejected by resolve_network_settings exits nonzero."""
    all_external_mocked["resolve_network_settings"].side_effect = LibvirtNetworkError(
        "Network 'vlan10' is a bridge. Supply explicit default_dns and default_gateway."
    )
    result = _invoke(["testvm.local", "debian12", "--network", "vlan10"], tmp_path)
    assert result.exit_code != 0
    assert "bridge" in result.output.lower()


def test_no_ssh_keys_refuses(all_external_mocked: dict, tmp_path: Path) -> None:
    """Zero discovered keys + no --public-key → refusal with a clear message."""
    all_external_mocked["discover_default_public_keys"].return_value = []
    result = _invoke(["testvm.local", "debian12"], tmp_path)
    assert result.exit_code != 0
    assert "No SSH public keys" in result.output


def test_ip4_flows_into_validate_static_ip(
    all_external_mocked: dict, tmp_path: Path
) -> None:
    """A --ip4 argument is parsed and handed to validate_static_ip."""
    result = _invoke(["testvm.local", "debian12", "--ip4", "192.168.122.50"], tmp_path)
    assert result.exit_code == 0, result.output
    validate = all_external_mocked["validate_static_ip"]
    validate.assert_called_once()
    # ensure_cidr applied the default /24 before validation.
    assert validate.call_args.args[0] == "192.168.122.50/24"


def test_ip4_is_rendered_into_network_config(
    all_external_mocked: dict, tmp_path: Path
) -> None:
    """The static IP actually reaches the guest network-config (not just validated)."""
    result = _invoke(
        ["testvm.local", "debian12", "--ip4", "vlan10,100.64.10.100"], tmp_path
    )
    assert result.exit_code == 0, result.output
    network_config = (tmp_path / "testvm.local" / "network-config").read_text()
    assert "100.64.10.100/24" in network_config
    # NAT resolver (gateway) lands as a nameserver even with no search domains.
    assert "192.168.122.1" in network_config


def test_pinned_mac_threads_into_virt_install_and_network_config(
    all_external_mocked: dict, tmp_path: Path
) -> None:
    """createvm pins one MAC into BOTH the virt-install ``--network`` arg and
    the cloud-init ``match: macaddress``; the two must agree.

    If they ever drifted, the guest profile would match no NIC on the
    NetworkManager renderer (Fedora) and fall back to default DHCP — the
    static-IP bug this whole mechanism fixes.
    """
    import re

    import yaml

    result = _invoke(["testvm.local", "debian12", "--ip4", "192.168.122.50"], tmp_path)
    assert result.exit_code == 0, result.output

    virt_install_calls = [
        c
        for c in all_external_mocked["subprocess_run"].call_args_list
        if c.args[0][0] == "virt-install"
    ]
    argv = virt_install_calls[0].args[0]
    network_arg = next(a for a in argv if a.startswith("network=default,"))
    match = re.search(r"mac=((?:[0-9a-f]{2}:){5}[0-9a-f]{2})", network_arg)
    assert match, f"no mac= in {network_arg!r}"
    pinned = match.group(1)
    assert pinned.startswith("52:54:00:")

    parsed = yaml.safe_load((tmp_path / "testvm.local" / "network-config").read_text())
    assert parsed["network"]["ethernets"]["eth0"]["match"] == {"macaddress": pinned}


def test_ip4_in_dhcp_range_errors(all_external_mocked: dict, tmp_path: Path) -> None:
    """A ValueError from validate_static_ip surfaces as a clean CLI error."""
    all_external_mocked["validate_static_ip"].side_effect = ValueError(
        "IP address '192.168.122.100' falls within DHCP range "
        "'192.168.122.2-192.168.122.254' for network 'default'."
    )
    result = _invoke(["testvm.local", "debian12", "--ip4", "192.168.122.100"], tmp_path)
    assert result.exit_code != 0
    assert "DHCP range" in result.output


def test_ip4_default_sentinel_launches_dhcp_vm(
    all_external_mocked: dict, tmp_path: Path
) -> None:
    """`--ip4 default` takes the DHCP path: no static validation, dhcp4 network-config."""
    result = _invoke(["testvm.local", "debian12", "--ip4", "default"], tmp_path)
    assert result.exit_code == 0, result.output
    # DHCP path skips static-IP validation entirely.
    all_external_mocked["validate_static_ip"].assert_not_called()
    network_config = (tmp_path / "testvm.local" / "network-config").read_text()
    assert "dhcp4: true" in network_config
    # The sentinel must never be mangled into an address.
    assert "default/24" not in network_config


def test_ip4_dhcp_sentinel_launches_dhcp_vm(
    all_external_mocked: dict, tmp_path: Path
) -> None:
    """`--ip4 dhcp` is equivalent to omitting the flag."""
    result = _invoke(["testvm.local", "debian12", "--ip4", "dhcp"], tmp_path)
    assert result.exit_code == 0, result.output
    all_external_mocked["validate_static_ip"].assert_not_called()
    assert "dhcp4: true" in (tmp_path / "testvm.local" / "network-config").read_text()


def test_ip4_invalid_address_gives_clean_actionable_error(
    all_external_mocked: dict, tmp_path: Path
) -> None:
    """A genuinely invalid --ip4 yields an actionable message, not the stdlib one.

    Uses an IP-ish value (digits/dots, out of range) — a bare token with
    letters is now read as a network name + DHCP (#136), so the "invalid IP"
    path is reserved for numeric typos.
    """
    result = _invoke(["testvm.local", "debian12", "--ip4", "192.168.1.300"], tmp_path)
    assert result.exit_code != 0
    out = result.output
    # Actionable: names the bad value, suggests an example + the DHCP path.
    assert "not a valid IPv4 address" in out
    assert "--ip4 dhcp" in out
    # Must NOT leak the stdlib message nor echo the netmask-mangled form.
    assert "does not appear to be an" not in out
    assert "192.168.1.300/24" not in out


def test_bridge_static_ip_without_dns_gateway_errors(
    all_external_mocked: dict, tmp_path: Path
) -> None:
    """A static --ip4 on a bridge network without --gateway/--dns fails fast
    with a message naming the missing flags (#136)."""
    all_external_mocked["get_network_info"].return_value = _bridge_network_info()
    result = _invoke(
        ["testvm.local", "debian12", "--ip4", "vlan10,100.64.100.107"], tmp_path
    )
    assert result.exit_code != 0
    assert "bridge" in result.output
    assert "--gateway" in result.output and "--dns" in result.output


def test_bridge_static_ip_with_dns_gateway_passes_them_through(
    all_external_mocked: dict, tmp_path: Path
) -> None:
    """--gateway/--dns/--search-domain reach resolve_network_settings so a
    static --ip4 on a bridge network is accepted (#136)."""
    all_external_mocked["get_network_info"].return_value = _bridge_network_info()
    result = _invoke(
        [
            "testvm.local",
            "debian12",
            "--ip4",
            "vlan10,100.64.100.107",
            "--gateway",
            "100.64.0.1",
            "--dns",
            "1.1.1.1,8.8.8.8",
            "--search-domain",
            "lab.example",
        ],
        tmp_path,
    )
    assert result.exit_code == 0, result.output
    kwargs = all_external_mocked["resolve_network_settings"].call_args.kwargs
    assert kwargs["default_gateway"] == "100.64.0.1"
    assert kwargs["default_dns"] == ["1.1.1.1", "8.8.8.8"]
    assert kwargs["default_search"] == ["lab.example"]


def test_bare_network_name_boots_dhcp_on_that_network(
    all_external_mocked: dict, tmp_path: Path
) -> None:
    """``--ip4 vlan10`` (no IP) resolves that network and takes the DHCP path —
    no static IP validated, and the named network is the one looked up (#136)."""
    result = _invoke(["testvm.local", "debian12", "--ip4", "vlan10"], tmp_path)
    assert result.exit_code == 0, result.output
    all_external_mocked["validate_static_ip"].assert_not_called()
    assert all_external_mocked["get_network_info"].call_args.args[-1] == "vlan10"


# ---------------------------------------------------------------------------
# Host-config (networks / default_network) precedence (#138)
# ---------------------------------------------------------------------------


def test_networks_config_supplies_bridge_gateway_dns(
    all_external_mocked: dict, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A ``networks:`` entry supplies a bridge's gateway/DNS/search, so a
    static ``--ip4 vlan10,IP`` needs no ``--gateway``/``--dns`` flags (#138)."""
    all_external_mocked["get_network_info"].return_value = _bridge_network_info()
    monkeypatch.setattr(
        cv_mod,
        "load_host_config",
        lambda config_path=None: HostConfig(
            networks={
                "vlan10": NetworkDefaults(
                    gateway="100.64.10.1",
                    dns=["100.64.10.10", "100.64.10.11"],
                    search=["tkclabs.io"],
                )
            }
        ),
    )
    result = _invoke(
        ["testvm.local", "debian12", "--ip4", "vlan10,100.64.10.50"], tmp_path
    )
    assert result.exit_code == 0, result.output
    kwargs = all_external_mocked["resolve_network_settings"].call_args.kwargs
    assert kwargs["default_gateway"] == "100.64.10.1"
    assert kwargs["default_dns"] == ["100.64.10.10", "100.64.10.11"]
    assert kwargs["default_search"] == ["tkclabs.io"]


def test_cli_flags_override_networks_config(
    all_external_mocked: dict, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """CLI ``--gateway``/``--dns``/``--search-domain`` win over a ``networks:``
    entry for the same network (#138)."""
    all_external_mocked["get_network_info"].return_value = _bridge_network_info()
    monkeypatch.setattr(
        cv_mod,
        "load_host_config",
        lambda config_path=None: HostConfig(
            networks={
                "vlan10": NetworkDefaults(
                    gateway="100.64.10.1",
                    dns=["100.64.10.10"],
                    search=["tkclabs.io"],
                )
            }
        ),
    )
    result = _invoke(
        [
            "testvm.local",
            "debian12",
            "--ip4",
            "vlan10,100.64.10.50",
            "--gateway",
            "10.0.0.1",
            "--dns",
            "9.9.9.9",
            "--search-domain",
            "override.example",
        ],
        tmp_path,
    )
    assert result.exit_code == 0, result.output
    kwargs = all_external_mocked["resolve_network_settings"].call_args.kwargs
    assert kwargs["default_gateway"] == "10.0.0.1"
    assert kwargs["default_dns"] == ["9.9.9.9"]
    assert kwargs["default_search"] == ["override.example"]


def test_default_network_from_config_used_when_no_flag(
    all_external_mocked: dict, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``default_network`` from config is used when neither ``--network`` nor a
    ``NETWORK,IP`` ``--ip4`` names one (#138)."""
    all_external_mocked["get_network_info"].return_value = _bridge_network_info()
    monkeypatch.setattr(
        cv_mod,
        "load_host_config",
        lambda config_path=None: HostConfig(default_network="vlan10"),
    )
    result = _invoke(["testvm.local", "debian12"], tmp_path)
    assert result.exit_code == 0, result.output
    assert all_external_mocked["get_network_info"].call_args.args[-1] == "vlan10"


def test_network_flag_overrides_config_default_network(
    all_external_mocked: dict, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An explicit ``--network`` beats the config ``default_network`` (#138)."""
    monkeypatch.setattr(
        cv_mod,
        "load_host_config",
        lambda config_path=None: HostConfig(default_network="vlan10"),
    )
    result = _invoke(["testvm.local", "debian12", "--network", "vlan20"], tmp_path)
    assert result.exit_code == 0, result.output
    assert all_external_mocked["get_network_info"].call_args.args[-1] == "vlan20"


def test_virt_install_failure_cleans_up_vm_dir(
    all_external_mocked: dict, tmp_path: Path
) -> None:
    """If virt-install fails, the partial VM dir is removed (cleanup-on-failure)."""

    def fail_on_virt_install(argv: list[str], **kwargs):
        if argv[0] == "virt-install":
            raise subprocess.CalledProcessError(
                returncode=1, cmd=argv, stderr="virt-install boom\n"
            )
        # Delegate so `qemu-img info` still returns a real virtual-size JSON.
        return _fake_subprocess_run(argv, **kwargs)

    all_external_mocked["subprocess_run"].side_effect = fail_on_virt_install
    result = _invoke(["testvm.local", "debian12"], tmp_path)
    assert result.exit_code != 0
    assert not (tmp_path / "testvm.local").exists()


def test_collision_with_existing_storage_dir_refuses(
    all_external_mocked: dict, tmp_path: Path
) -> None:
    """If the VM dir already exists, refuse (don't clobber existing state)."""
    (tmp_path / "testvm.local").mkdir()
    result = _invoke(["testvm.local", "debian12"], tmp_path)
    assert result.exit_code != 0
    assert "already exists" in result.output


def test_existing_domain_refuses(all_external_mocked: dict, tmp_path: Path) -> None:
    """A pre-existing libvirt domain with this name refuses creation."""
    all_external_mocked["vm_exists"].return_value = True
    result = _invoke(["testvm.local", "debian12"], tmp_path)
    assert result.exit_code != 0
    assert "already exists in libvirt" in result.output


def test_unknown_distro_errors_with_available_list(
    all_external_mocked: dict, tmp_path: Path
) -> None:
    """An unknown VM_DISTRO fails at the CLI with the available names."""
    result = _invoke(["testvm.local", "nope"], tmp_path)
    assert result.exit_code != 0
    assert "Unknown distro 'nope'" in result.output
    assert "Available:" in result.output


def test_uses_manifest_image_when_present(
    all_external_mocked: dict, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A cwd Lvlab.yml contributes a manifest-only distro that resolves."""
    monkeypatch.setattr(
        cv_mod,
        "load_host_config",
        lambda config_path=None: HostConfig(
            images={
                "myapp": {"image_url": "http://h/myapp.qcow2", "network_version": 2}
            }
        ),
    )
    captured: dict = {}

    def fake_build(name, entry, image_dir):
        captured["name"] = name
        captured["image_dir"] = image_dir
        return all_external_mocked["fake_image"]

    monkeypatch.setattr(cv_mod, "_build_cloud_image", fake_build)

    result = _invoke(["testvm.local", "myapp"], tmp_path)
    assert result.exit_code == 0, result.output
    assert captured["name"] == "myapp"
    assert str(captured["image_dir"]) == "/var/lib/libvirt/images/lvlab"


def test_init_cloud_images_only(
    all_external_mocked: dict, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """--init-cloud-images with no VM args downloads the catalog and stops."""
    built = []
    monkeypatch.setattr(
        cv_mod,
        "_build_cloud_image",
        lambda name, entry, d: built.append(name) or all_external_mocked["fake_image"],
    )
    result = CliRunner().invoke(
        run, ["--init-cloud-images", "--storage-root", str(tmp_path)]
    )
    assert result.exit_code == 0, result.output
    assert "Cloud images initialized." in result.output
    # Deprecation notice steers users to `lvlab init` (issue #97), but the
    # flag still works.
    assert "deprecated" in result.output.lower()
    assert "lvlab init" in result.output
    # Every built-in image was visited for download.
    assert set(built) == set(BUILTIN_IMAGES)
    # No VM was created.
    all_external_mocked["subprocess_run"].assert_not_called()


def test_ensure_storage_root_writable_raises_with_guidance(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    """The writability precheck raises with libvirt-group / 0771 guidance."""
    import typer

    monkeypatch.setattr(cv_mod.os, "access", lambda path, mode: False)
    with pytest.raises(typer.Exit):
        cv_mod._ensure_storage_root_writable(tmp_path)
    out = capsys.readouterr().out
    assert "not writable" in out
    assert "libvirt" in out


def test_writability_precheck_runs_before_image_work(
    all_external_mocked: dict, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The storage precheck fails fast, before any image download/verify."""

    def boom(_root: Path) -> None:
        cv_mod._fail("storage root boom")

    monkeypatch.setattr(cv_mod, "_ensure_storage_root_writable", boom)
    result = _invoke(["testvm.local", "debian12"], tmp_path)
    assert result.exit_code != 0
    assert "storage root boom" in result.output
    all_external_mocked["ensure_image_available"].assert_not_called()


def test_passes_system_first_env_to_virt_install(
    all_external_mocked: dict, tmp_path: Path
) -> None:
    """createvm invokes virt-install with system-first PATH (Debian 13 fix)."""
    result = _invoke(["testvm.local", "debian12"], tmp_path)
    assert result.exit_code == 0, result.output

    virt_install_calls = [
        c
        for c in all_external_mocked["subprocess_run"].call_args_list
        if c.args[0][0] == "virt-install"
    ]
    assert len(virt_install_calls) == 1
    env = virt_install_calls[0].kwargs["env"]
    assert env["PATH"].startswith(
        "/usr/bin:/usr/sbin"
    ), f"createvm must pass system bin paths first; got PATH={env['PATH']!r}"


def test_no_color_flag_disables_color(monkeypatch: pytest.MonkeyPatch) -> None:
    """``createvm --no-color`` flips the global colour switch as the first thing
    the command body does — before argument validation, so even an early
    ``_fail`` prints plain (the shared ``secho`` wrapper honours it). Issue #131."""
    from tkc_lvlab.utils import output

    monkeypatch.delenv("NO_COLOR", raising=False)
    output.set_no_color(False)
    try:
        # No VM args -> the command exits 1 on "missing required arguments",
        # but --no-color is applied before that check runs.
        CliRunner().invoke(cv_mod.app, ["--no-color"])
        assert output.color_disabled() is True
    finally:
        output.set_no_color(False)
