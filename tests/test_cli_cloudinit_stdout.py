"""Unit tests for the experimental ``lvlab cloudinit --stdout`` flag.

`--stdout` renders to a tmpdir and prints the three NoCloud documents
(meta-data, user-data, network-config) to stdout with clear separators,
so an operator can inspect what cloud-init would receive without
touching the per-VM directory under ``/var/lib/libvirt/images/...``
(which is root-owned by default).
"""

from __future__ import annotations

from unittest import mock

from typer.testing import CliRunner

from tkc_lvlab import cli
from tkc_lvlab.cli import app


def _machine() -> dict:
    return {
        "vm_name": "web01",
        "hostname": "web01",
        "os": "debian12",
        "interfaces": [{"name": "eth0", "ip4": "10.0.0.5/24"}],
        "cloud_init": {
            "user": "debian",
            "pubkey": "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5testkey user@host",
        },
    }


def _invoke(argv: list[str]):
    runner = CliRunner()
    parse_return = (
        {"name": "test-env", "libvirt_uri": "qemu:///system"},
        {"debian12": {"image_url": "https://example/debian12.qcow2"}},
        {"interfaces": {}, "domain": "test.local"},
        [_machine()],
    )

    # Make Machine.cloud_init write deterministic file content to the
    # config_fpath that the test asserts on — bypass the real Jinja/IO
    # stack so the test stays unit-level.
    def _fake_cloud_init(
        self, cloud_image, config_defaults, machines, password_hash=None
    ):
        import os

        os.makedirs(self.config_fpath, exist_ok=True)
        meta_path = os.path.join(self.config_fpath, "meta-data")
        user_path = os.path.join(self.config_fpath, "user-data")
        net_path = os.path.join(self.config_fpath, "network-config")
        with open(meta_path, "w") as f:
            f.write(
                "instance-id: iid-web01_test-env\nlocal-hostname: web01.test.local\n"
            )
        with open(user_path, "w") as f:
            f.write("#cloud-config\nmanage_etc_hosts: true\nhostname: web01\n")
        with open(net_path, "w") as f:
            f.write("version: 2\nethernets:\n  eth0:\n    dhcp4: true\n")
        return meta_path, user_path, net_path

    with (
        mock.patch.object(cli, "parse_config", return_value=parse_return),
        mock.patch("tkc_lvlab.utils.libvirt.Machine.cloud_init", _fake_cloud_init),
    ):
        return runner.invoke(app, ["cloudinit", *argv])


def test_cloudinit_stdout_prints_all_three_documents() -> None:
    """--stdout emits meta-data, user-data, and network-config to stdout."""
    result = _invoke(["--stdout", "web01"])
    assert result.exit_code == 0, result.output
    assert "instance-id: iid-web01_test-env" in result.output
    assert "manage_etc_hosts: true" in result.output
    assert "dhcp4: true" in result.output


def test_cloudinit_stdout_uses_clear_separators() -> None:
    """Each file is preceded by a labelled separator so it's scannable."""
    result = _invoke(["--stdout", "web01"])
    assert result.exit_code == 0
    # Separator format documented to the operator
    assert "--- meta-data ---" in result.output
    assert "--- user-data ---" in result.output
    assert "--- network-config ---" in result.output
    # Order is meta-data, user-data, network-config
    out = result.output
    assert (
        out.find("meta-data ---")
        < out.find("user-data ---")
        < out.find("network-config ---")
    )


def test_cloudinit_stdout_does_not_write_to_per_vm_dir() -> None:
    """--stdout uses a tmpdir; the per-VM /var/lib/libvirt path is NOT touched."""
    import os
    import shutil

    per_vm_dir = "/tmp/lvlab-test-novm/test-env/web01"
    if os.path.exists(per_vm_dir):
        shutil.rmtree(per_vm_dir)
    # Force the resolved per-VM path through env var (would normally come
    # from config_defaults.disk_image_basedir). We assert below that
    # nothing under it is created — which means --stdout went via the
    # tmpdir route and never touched the configured per-VM dir.
    runner = CliRunner()
    parse_return = (
        {"name": "test-env", "libvirt_uri": "qemu:///system"},
        {"debian12": {"image_url": "https://example/debian12.qcow2"}},
        {
            "interfaces": {},
            "domain": "test.local",
            "disk_image_basedir": "/tmp/lvlab-test-novm",
        },
        [_machine()],
    )

    def _fake_cloud_init(
        self, cloud_image, config_defaults, machines, password_hash=None
    ):
        os.makedirs(self.config_fpath, exist_ok=True)
        for name, body in [
            ("meta-data", "iid\n"),
            ("user-data", "#cloud-config\n"),
            ("network-config", "version: 2\n"),
        ]:
            with open(os.path.join(self.config_fpath, name), "w") as f:
                f.write(body)
        return tuple(
            os.path.join(self.config_fpath, n)
            for n in ("meta-data", "user-data", "network-config")
        )

    with (
        mock.patch.object(cli, "parse_config", return_value=parse_return),
        mock.patch("tkc_lvlab.utils.libvirt.Machine.cloud_init", _fake_cloud_init),
    ):
        result = runner.invoke(app, ["cloudinit", "--stdout", "web01"])

    assert result.exit_code == 0, result.output
    # The per-VM dir under the configured disk_image_basedir was never created.
    assert not os.path.exists(
        per_vm_dir
    ), f"--stdout should not touch {per_vm_dir}, but it exists"


def test_cloudinit_default_still_writes_to_per_vm_dir() -> None:
    """No --stdout → existing behaviour (writes under config_fpath)."""
    import os
    import shutil

    per_vm_dir = "/tmp/lvlab-test-writes/test-env/web01"
    if os.path.exists(per_vm_dir):
        shutil.rmtree(per_vm_dir)

    runner = CliRunner()
    parse_return = (
        {"name": "test-env", "libvirt_uri": "qemu:///system"},
        {"debian12": {"image_url": "https://example/debian12.qcow2"}},
        {
            "interfaces": {},
            "domain": "test.local",
            "disk_image_basedir": "/tmp/lvlab-test-writes",
        },
        [_machine()],
    )

    def _fake_cloud_init(
        self, cloud_image, config_defaults, machines, password_hash=None
    ):
        os.makedirs(self.config_fpath, exist_ok=True)
        for name in ("meta-data", "user-data", "network-config"):
            with open(os.path.join(self.config_fpath, name), "w") as f:
                f.write(f"## {name}\n")
        return tuple(
            os.path.join(self.config_fpath, n)
            for n in ("meta-data", "user-data", "network-config")
        )

    with (
        mock.patch.object(cli, "parse_config", return_value=parse_return),
        mock.patch("tkc_lvlab.utils.libvirt.Machine.cloud_init", _fake_cloud_init),
    ):
        result = runner.invoke(app, ["cloudinit", "web01"])

    assert result.exit_code == 0, result.output
    assert os.path.exists(os.path.join(per_vm_dir, "meta-data"))
    shutil.rmtree(per_vm_dir, ignore_errors=True)
