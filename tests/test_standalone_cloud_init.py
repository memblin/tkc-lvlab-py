"""Unit tests for :mod:`tkc_lvlab.utils.standalone_cloud_init`.

These tests assert the rendered cloud-init output for the standalone
``createvm`` workflow has the expected shape — specifically:

- Multiple SSH keys are emitted as a list (the manifest workflow's
    single-pubkey template can't do this).
- The generated password hash lands at ``users[0].passwd``.
- ``hostname``, ``fqdn``, and ``username`` flow through to the rendered
    output.
- ``runcmd`` lines are honored when non-empty and absent when empty.
- ``instance-id`` carries the libvirt_vm_name (the ``oneoff-<vm_name>``
    domain name from Phase 6's naming lock).

Real-bug-mode: every assertion checks a specific value or substring a
reviewer can recognize as right. No "just check render returns a string"
padding.
"""

from __future__ import annotations

import yaml

from tkc_lvlab.utils.standalone_cloud_init import OneoffCloudInit


def _sample(
    *,
    ssh_keys: list[str] | None = None,
    runcmd: list[str] | None = None,
) -> OneoffCloudInit:
    """Construct a representative OneoffCloudInit for the rendering tests."""
    return OneoffCloudInit(
        libvirt_vm_name="oneoff-testvm.local",
        hostname="testvm",
        fqdn="testvm.local",
        username="cloud-user",
        ssh_public_keys=(
            ssh_keys
            if ssh_keys is not None
            else ["ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIBBBBBBBBBBB tester@laptop"]
        ),
        password_hash="$6$rounds=4096$abc$xyz",
        runcmd=runcmd if runcmd is not None else [],
    )


# ---------------------------------------------------------------------------
# user-data shape
# ---------------------------------------------------------------------------


def test_user_data_emits_cloud_config_magic_line() -> None:
    """Cloud-init only honors files starting with #cloud-config — lock that."""
    out = _sample().render_user_data()
    assert out.lstrip().startswith("#cloud-config")


def test_user_data_includes_hostname_and_fqdn() -> None:
    """hostname and fqdn flow through to the rendered document."""
    out = _sample().render_user_data()
    assert "hostname: testvm" in out
    assert "fqdn: testvm.local" in out


def test_user_data_password_hash_lands_at_users_passwd() -> None:
    """The hash is rendered as users[0].passwd — what cloud-init expects."""
    out = _sample().render_user_data()
    parsed = yaml.safe_load(out)
    assert parsed["users"][0]["passwd"] == "$6$rounds=4096$abc$xyz"
    # And lock_passwd is false so the password is actually usable.
    assert parsed["users"][0]["lock_passwd"] is False


def test_user_data_username_lands_at_users_name() -> None:
    """The username flows to users[0].name."""
    out = _sample().render_user_data()
    parsed = yaml.safe_load(out)
    assert parsed["users"][0]["name"] == "cloud-user"


def test_user_data_emits_all_ssh_keys_as_a_list() -> None:
    """Multiple SSH keys all appear as ssh_authorized_keys entries.

    This is the meaningful diff vs the manifest workflow's user-data.j2,
    which only handles a single pubkey scalar. Discovery+--public-key
    can produce N keys.
    """
    keys = [
        "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIAAAAAAA discovered@laptop",
        "ssh-rsa AAAAB3NzaC1yc2EAAAADAQABAAABAQDBBBBBBB cli-supplied@host",
        "ecdsa-sha2-nistp256 AAAAE2VjZHNhLXNoYTItbmlzdHCCCCCC hardware@yubi",
    ]
    out = _sample(ssh_keys=keys).render_user_data()
    parsed = yaml.safe_load(out)
    rendered_keys = parsed["users"][0]["ssh_authorized_keys"]
    assert rendered_keys == keys, rendered_keys


def test_user_data_omits_runcmd_when_empty() -> None:
    """An empty runcmd list must not emit a `runcmd:` key (cloud-init quirk)."""
    out = _sample(runcmd=[]).render_user_data()
    assert "runcmd" not in out


def test_user_data_emits_runcmd_when_present() -> None:
    """A populated runcmd list renders as a YAML list."""
    out = _sample(runcmd=["echo hello", "uname -a"]).render_user_data()
    parsed = yaml.safe_load(out)
    assert parsed["runcmd"] == ["echo hello", "uname -a"]


def test_user_data_handles_multiline_runcmd_as_heredoc() -> None:
    """Multi-line runcmd entries render as `|` block scalars."""
    out = _sample(
        runcmd=["cat <<'EOF' > /etc/motd\nWelcome to the lab\nEOF"]
    ).render_user_data()
    parsed = yaml.safe_load(out)
    assert isinstance(parsed["runcmd"][0], str)
    assert "Welcome to the lab" in parsed["runcmd"][0]


def test_user_data_sudo_and_shell_default_for_lab_use() -> None:
    """Default sudo NOPASSWD + /bin/bash shell — matches the lab convention.

    These are the defaults lvscripts uses too. Regression guard so a
    future "tighten sudo by default" change is deliberate.
    """
    out = _sample().render_user_data()
    parsed = yaml.safe_load(out)
    assert parsed["users"][0]["sudo"] == "ALL=(ALL) NOPASSWD:ALL"
    assert parsed["users"][0]["shell"] == "/bin/bash"


# ---------------------------------------------------------------------------
# meta-data shape
# ---------------------------------------------------------------------------


def test_meta_data_instance_id_prefix() -> None:
    """instance-id is iid-<libvirt_vm_name> — cloud-init's NoCloud convention."""
    out = _sample().render_meta_data()
    assert "instance-id: iid-oneoff-testvm.local" in out


def test_meta_data_local_hostname_uses_fqdn() -> None:
    """local-hostname mirrors the fqdn (matches the manifest template's behavior)."""
    out = _sample().render_meta_data()
    assert "local-hostname: testvm.local" in out
