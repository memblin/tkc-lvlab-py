"""Render-level tests for ``user-data.j2`` via :class:`UserData`.

Focused on the issue #106 template change: a one-time console password
hash (``cloud_init.passwd``) renders as ``users[*].passwd`` with
``lock_passwd: false``, and is absent when no password is configured (so
key-only VMs don't grow a spurious password field).
"""

from __future__ import annotations

from tkc_lvlab.utils.cloud_init import UserData

# A slash-free literal key so UserData.__post_init__ treats it as a literal
# (a real base64 key can contain ``/`` and would be mistaken for a path).
_LITERAL_PUBKEY = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5testkeymaterial user@host"


def _user_data(**cloud_init_extra: object) -> str:
    cloud_init = {
        "user": "debian",
        "pubkey": _LITERAL_PUBKEY,
        "sudo": "ALL=(ALL) NOPASSWD:ALL",
        "shell": "/bin/bash",
        **cloud_init_extra,
    }
    return UserData(
        cloud_init, "web01", "test.local", "web01.test.local"
    ).render_config()


def test_user_data_renders_passwd_and_unlocks_when_present() -> None:
    """A configured passwd hash renders with lock_passwd: false."""
    rendered = _user_data(passwd="$6$rounds=4096$salt$hash")
    assert "passwd: $6$rounds=4096$salt$hash" in rendered
    assert "lock_passwd: false" in rendered


def test_user_data_omits_passwd_when_absent() -> None:
    """No passwd configured -> no passwd / lock_passwd lines (key-only VM)."""
    rendered = _user_data()
    assert "passwd:" not in rendered
    assert "lock_passwd" not in rendered


# --- #120: cloud_init.manage_etc_hosts opt-out --------------------------------


def test_user_data_renders_manage_etc_hosts_true_by_default() -> None:
    """No flag → today's behaviour: manage_etc_hosts: true (#120)."""
    rendered = _user_data()
    assert "manage_etc_hosts: true" in rendered
    assert "manage_etc_hosts: false" not in rendered


def test_user_data_renders_manage_etc_hosts_false_when_disabled() -> None:
    """cloud_init.manage_etc_hosts: false → manage_etc_hosts: false in user-data."""
    rendered = _user_data(manage_etc_hosts=False)
    assert "manage_etc_hosts: false" in rendered
    assert "manage_etc_hosts: true" not in rendered


def test_user_data_renders_manage_etc_hosts_true_when_explicitly_enabled() -> None:
    """Explicit cloud_init.manage_etc_hosts: true renders as such (no double-render)."""
    rendered = _user_data(manage_etc_hosts=True)
    assert "manage_etc_hosts: true" in rendered
    assert "manage_etc_hosts: false" not in rendered
