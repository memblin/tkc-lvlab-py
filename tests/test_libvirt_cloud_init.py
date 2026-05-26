"""Unit tests for :meth:`tkc_lvlab.utils.libvirt.Machine.cloud_init`.

These tests lock the two high-risk behaviours that are subtle to get
right after the cognitive-complexity refactor of ``cloud_init``:

- runcmd merge across defaults + per-machine, with the
  ``runcmd_ignore_defaults`` opt-out.
- ``/etc/cloud/templates/hosts.*.tmpl`` selection by distro family.

Filesystem writes are redirected to ``tmp_path`` via
``Machine.config_fpath`` so nothing real is touched. The Jinja-backed
:class:`NetworkConfig` / :class:`MetaData` / :class:`UserData` and the
config-file-reading :func:`parse_config` / :func:`generate_hosts` are
mocked at the ``tkc_lvlab.utils.libvirt`` import boundary so the
tests neither render Jinja nor read disk.
"""

from __future__ import annotations

from pathlib import Path
from unittest import mock

import pytest

from tkc_lvlab.utils.libvirt import Machine


def _make_machine(tmp_path: Path, *, os_value: str = "debian13") -> Machine:
    """Construct a Machine stub bypassing __init__ side effects."""
    m = object.__new__(Machine)
    m.environment = {"name": "test-env"}
    m.vm_name = "web01"
    m.libvirt_vm_name = "web01_test-env"
    m.hostname = "web01"
    m.domain = "test.local"
    m.fqdn = "web01.test.local"
    m.os = os_value
    m.interfaces = [{"name": "eth0"}]
    m.nameservers = {}
    m.cloud_init_config = {}
    m.config_fpath = str(tmp_path)
    return m


def _patch_collaborators():
    """Patch render-only collaborators at the libvirt module boundary.

    Returns a list of (target, mock_or_value) pairs callers can iterate
    with ``ExitStack`` or stack inline.
    """
    network_obj = mock.Mock()
    network_obj.render_config.return_value = "## network-config\n"
    metadata_obj = mock.Mock()
    metadata_obj.render_config.return_value = "## meta-data\n"
    userdata_obj = mock.Mock()
    userdata_obj.render_config.return_value = "## user-data\n"
    cloud_image = mock.Mock()
    cloud_image.network_version = 2
    cloud_image.default_username = "debian"
    return network_obj, metadata_obj, userdata_obj, cloud_image


def _run_cloud_init(machine, config_defaults, userdata_capture: list, **kwargs):
    """Invoke ``machine.cloud_init`` with all rendering + IO collaborators stubbed.

    ``userdata_capture`` is a list the test passes in; the patched
    ``UserData`` mock appends the cloud_init_config kwarg it was
    constructed with so the test can assert on the merged config. Extra
    ``kwargs`` (e.g. ``password_hash``) are forwarded to
    :meth:`Machine.cloud_init`.
    """
    network_obj, metadata_obj, userdata_obj, cloud_image = _patch_collaborators()

    def _capture_userdata(cloud_init_config, hostname, domain, fqdn):
        userdata_capture.append(dict(cloud_init_config))
        return userdata_obj

    with (
        mock.patch("tkc_lvlab.utils.libvirt.NetworkConfig", return_value=network_obj),
        mock.patch("tkc_lvlab.utils.libvirt.MetaData", return_value=metadata_obj),
        mock.patch("tkc_lvlab.utils.libvirt.UserData", side_effect=_capture_userdata),
        mock.patch(
            "tkc_lvlab.utils.libvirt.parse_config",
            return_value=({}, {}, {}, []),
        ),
        mock.patch(
            "tkc_lvlab.utils.libvirt.generate_hosts",
            side_effect=lambda env, defs, machines, heredoc=None: (
                f"## hosts heredoc for {heredoc}\n"
            ),
        ),
    ):
        return machine.cloud_init(cloud_image, config_defaults, **kwargs)


def test_cloud_init_writes_three_files_to_config_fpath(tmp_path: Path) -> None:
    """meta-data, user-data, network-config land under ``config_fpath``."""
    machine = _make_machine(tmp_path)
    config_defaults = {"cloud_init": {}}
    captured: list = []
    meta, user, net = _run_cloud_init(machine, config_defaults, captured)

    assert Path(net).read_text() == "## network-config\n"
    assert Path(meta).read_text() == "## meta-data\n"
    assert Path(user).read_text() == "## user-data\n"
    assert Path(net).parent == tmp_path


def test_cloud_init_resolves_debian_template_path(tmp_path: Path) -> None:
    """``os: debian13`` → hosts.debian.tmpl heredoc snippet appears."""
    machine = _make_machine(tmp_path, os_value="debian13")
    captured: list = []
    _run_cloud_init(machine, {"cloud_init": {}}, captured)

    runcmd = captured[0]["runcmd"]
    assert any(
        "/etc/cloud/templates/hosts.debian.tmpl" in line for line in runcmd
    ), runcmd


def test_cloud_init_resolves_fedora_template_path(tmp_path: Path) -> None:
    """``os: fedora44`` → hosts.redhat.tmpl heredoc snippet appears."""
    machine = _make_machine(tmp_path, os_value="fedora44")
    captured: list = []
    _run_cloud_init(machine, {"cloud_init": {}}, captured)

    runcmd = captured[0]["runcmd"]
    assert any(
        "/etc/cloud/templates/hosts.redhat.tmpl" in line for line in runcmd
    ), runcmd


def test_cloud_init_resolves_almalinux_template_path(tmp_path: Path) -> None:
    """``os: almalinux10`` also maps to hosts.redhat.tmpl."""
    machine = _make_machine(tmp_path, os_value="almalinux10")
    captured: list = []
    _run_cloud_init(machine, {"cloud_init": {}}, captured)

    runcmd = captured[0]["runcmd"]
    assert any(
        "/etc/cloud/templates/hosts.redhat.tmpl" in line for line in runcmd
    ), runcmd


def test_cloud_init_defaults_user_to_image_default_username(tmp_path: Path) -> None:
    """When the manifest omits cloud_init.user, it defaults to the image's
    resolved default_username — so a manifest needn't hardcode the distro's
    conventional account (the convergence with createvm's derivation)."""
    machine = _make_machine(tmp_path)
    machine.cloud_init_config = {}
    captured: list = []
    _run_cloud_init(machine, {"cloud_init": {}}, captured)
    assert captured[0]["user"] == "debian"  # from cloud_image.default_username


def test_cloud_init_explicit_user_overrides_image_default(tmp_path: Path) -> None:
    """An explicit cloud_init.user still wins over the image default."""
    machine = _make_machine(tmp_path)
    machine.cloud_init_config = {"user": "root"}
    captured: list = []
    _run_cloud_init(machine, {"cloud_init": {}}, captured)
    assert captured[0]["user"] == "root"


def test_cloud_init_raises_value_error_on_unknown_distro(tmp_path: Path) -> None:
    """An unknown ``os`` family raises ValueError before any file write."""
    machine = _make_machine(tmp_path, os_value="freebsd14")
    captured: list = []
    with pytest.raises(ValueError, match="Could not find a template file"):
        _run_cloud_init(machine, {"cloud_init": {}}, captured)


def test_cloud_init_merges_defaults_runcmd_before_machine_runcmd(
    tmp_path: Path,
) -> None:
    """Without ignore flag, defaults' runcmd precedes machine's runcmd."""
    machine = _make_machine(tmp_path)
    machine.cloud_init_config = {"runcmd": ["machine-cmd-1", "machine-cmd-2"]}
    config_defaults = {"cloud_init": {"user": "root", "runcmd": ["default-cmd-1"]}}
    captured: list = []
    _run_cloud_init(machine, config_defaults, captured)

    runcmd = captured[0]["runcmd"]
    # The two hosts heredoc snippets go first, then defaults' runcmd, then machine's.
    machine_section = [cmd for cmd in runcmd if "cmd-" in cmd]
    assert machine_section == [
        "default-cmd-1",
        "machine-cmd-1",
        "machine-cmd-2",
    ]


def test_cloud_init_drops_defaults_runcmd_when_ignore_defaults_set(
    tmp_path: Path,
) -> None:
    """``runcmd_ignore_defaults: true`` drops defaults' runcmd but keeps other keys."""
    machine = _make_machine(tmp_path)
    machine.cloud_init_config = {
        "runcmd_ignore_defaults": True,
        "runcmd": ["machine-cmd-only"],
    }
    config_defaults = {
        "cloud_init": {
            "user": "root",
            "shell": "/bin/bash",
            "runcmd": ["should-not-appear"],
        }
    }
    captured: list = []
    _run_cloud_init(machine, config_defaults, captured)

    cfg = captured[0]
    runcmd = cfg["runcmd"]
    assert "should-not-appear" not in runcmd
    machine_section = [cmd for cmd in runcmd if "cmd-" in cmd]
    assert machine_section == ["machine-cmd-only"]
    # Other defaults keys still merge.
    assert cfg["user"] == "root"
    assert cfg["shell"] == "/bin/bash"


def test_cloud_init_initializes_empty_runcmd_when_neither_side_has_one(
    tmp_path: Path,
) -> None:
    """No runcmd anywhere → the two hosts heredocs become the entire runcmd."""
    machine = _make_machine(tmp_path)
    machine.cloud_init_config = {}
    captured: list = []
    _run_cloud_init(machine, {"cloud_init": {"user": "root"}}, captured)

    runcmd = captured[0]["runcmd"]
    assert len(runcmd) == 2  # /etc/hosts heredoc + template heredoc
    assert all("hosts heredoc" in line for line in runcmd)


def test_cloud_init_does_not_reparse_when_machines_injected(tmp_path: Path) -> None:
    """Passing ``machines`` in means cloud_init never re-reads the manifest (#49).

    The CLI already holds the parsed machines list, so it passes it through
    and the duplicate ``parse_config`` call inside cloud_init is skipped. We
    assert ``parse_config`` is never invoked and that the injected machines
    flow into ``generate_hosts`` (the only consumer).
    """
    machine = _make_machine(tmp_path)
    machine.cloud_init_config = {}
    injected_machines = [{"vm_name": "web01", "hostname": "web01"}]

    network_obj, metadata_obj, userdata_obj, cloud_image = _patch_collaborators()
    seen_machines: list = []

    with (
        mock.patch("tkc_lvlab.utils.libvirt.NetworkConfig", return_value=network_obj),
        mock.patch("tkc_lvlab.utils.libvirt.MetaData", return_value=metadata_obj),
        mock.patch("tkc_lvlab.utils.libvirt.UserData", return_value=userdata_obj),
        mock.patch("tkc_lvlab.utils.libvirt.parse_config") as parse_config_mock,
        mock.patch(
            "tkc_lvlab.utils.libvirt.generate_hosts",
            side_effect=lambda env, defs, machines, heredoc=None: seen_machines.append(
                machines
            )
            or "## hosts heredoc\n",
        ),
    ):
        machine.cloud_init(cloud_image, {"cloud_init": {}}, injected_machines)

    parse_config_mock.assert_not_called()
    # Both generate_hosts calls (/etc/hosts + template) got the injected list.
    assert seen_machines == [injected_machines, injected_machines]


def test_cloud_init_falls_back_to_parse_config_when_machines_none(
    tmp_path: Path,
) -> None:
    """Omitting ``machines`` keeps the one-time parse_config fallback (compat).

    Callers without the parsed list handy can still call cloud_init; it reads
    the manifest exactly once via parse_config. Locks that the fallback path
    is preserved for backward compatibility.
    """
    machine = _make_machine(tmp_path)
    machine.cloud_init_config = {}
    network_obj, metadata_obj, userdata_obj, cloud_image = _patch_collaborators()

    with (
        mock.patch(
            "tkc_lvlab.utils.libvirt.parse_config",
            return_value=({}, {}, {}, []),
        ) as parse_config_mock,
        mock.patch("tkc_lvlab.utils.libvirt.NetworkConfig", return_value=network_obj),
        mock.patch("tkc_lvlab.utils.libvirt.MetaData", return_value=metadata_obj),
        mock.patch("tkc_lvlab.utils.libvirt.UserData", return_value=userdata_obj),
        mock.patch(
            "tkc_lvlab.utils.libvirt.generate_hosts",
            side_effect=lambda env, defs, machines, heredoc=None: "## hosts\n",
        ),
    ):
        # No machines arg → the one-time parse_config fallback runs.
        machine.cloud_init(cloud_image, {"cloud_init": {}})

    parse_config_mock.assert_called_once()


# ---------------------------------------------------------------------------
# One-time console password injection (issue #106)
# ---------------------------------------------------------------------------


def test_cloud_init_injects_generated_password_hash(tmp_path: Path) -> None:
    """A password_hash with no manifest passwd lands in the merged config."""
    machine = _make_machine(tmp_path)
    machine.cloud_init_config = {}
    captured: list = []
    _run_cloud_init(
        machine, {"cloud_init": {}}, captured, password_hash="$6$rounds=4096$abc$xyz"
    )
    assert captured[0].get("passwd") == "$6$rounds=4096$abc$xyz"


def test_cloud_init_manifest_passwd_wins_over_generated(tmp_path: Path) -> None:
    """An explicit manifest passwd is never overwritten by a generated one."""
    machine = _make_machine(tmp_path)
    machine.cloud_init_config = {"passwd": "$6$manifest$set"}
    captured: list = []
    _run_cloud_init(
        machine, {"cloud_init": {}}, captured, password_hash="$6$generated$ignored"
    )
    assert captured[0].get("passwd") == "$6$manifest$set"


def test_cloud_init_no_password_hash_injects_nothing(tmp_path: Path) -> None:
    """password_hash=None (opt-out / key-only) leaves passwd unset."""
    machine = _make_machine(tmp_path)
    machine.cloud_init_config = {}
    captured: list = []
    _run_cloud_init(machine, {"cloud_init": {}}, captured, password_hash=None)
    assert "passwd" not in captured[0]
