"""Unit tests for ``UserData._is_valid_ssh_public_key``.

The function decides whether a string read off disk (or passed inline)
looks like an SSH public key. It returns ``(True, key_type)`` on a
match and ``False`` (bare bool, not ``(False, None)``) on a miss —
the inconsistency in return shape is a known quirk; these tests pin
the current behavior so any future refactor is deliberate.

Real-bug surface: if the regex ever stops matching ``ssh-ed25519``,
``__post_init__`` quietly leaves ``cloud_init["pubkey"]`` as a path
string and the rendered cloud-init user-data goes out with a literal
path where the key should be. The VM then comes up without the
developer's key — a long debug session.
"""

from __future__ import annotations

from tkc_lvlab.utils.cloud_init import UserData


def test_valid_rsa_key_returns_type() -> None:
    """A typical ssh-rsa public key returns ``(True, "ssh-rsa")``."""
    key = "ssh-rsa AAAAB3NzaC1yc2EAAAADAQABAAABAQDexample== user@host"
    result = UserData._is_valid_ssh_public_key(key)
    assert result == (True, "ssh-rsa"), result


def test_valid_ed25519_key_returns_type() -> None:
    """A typical ssh-ed25519 public key returns ``(True, "ssh-ed25519"``)."""
    key = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIexampleexample user@host"
    result = UserData._is_valid_ssh_public_key(key)
    assert result == (True, "ssh-ed25519"), result


def test_valid_dss_key_returns_type() -> None:
    """A ssh-dss key returns ``(True, "ssh-dss"``); kept for legacy parity."""
    key = "ssh-dss AAAAB3NzaC1kc3MAAACBAexample= user@host"
    result = UserData._is_valid_ssh_public_key(key)
    assert result == (True, "ssh-dss"), result


def test_key_without_comment_still_matches() -> None:
    """A key string without the trailing ``user@host`` comment still validates.

    The patterns allow the comment as optional. If they ever start
    requiring it, every key generated via ``ssh-keygen`` with ``-C ""``
    would fall through and the file-read path in ``__post_init__``
    would silently leave the path string in place.
    """
    key = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIexampleexample"
    result = UserData._is_valid_ssh_public_key(key)
    assert result == (True, "ssh-ed25519"), result


def test_garbage_string_returns_false() -> None:
    """A string that doesn't begin with a known key type returns bare ``False``.

    Pins the inconsistent-return-shape quirk: ``False``, not
    ``(False, None)``. Callers in cloud_init.py rely on this.
    """
    assert UserData._is_valid_ssh_public_key("hello world") is False


def test_empty_string_returns_false() -> None:
    """An empty string returns bare ``False``."""
    assert UserData._is_valid_ssh_public_key("") is False


def test_path_string_returns_false() -> None:
    """A path string (as ``cloud_init.pubkey`` might be) returns ``False``.

    This is the case ``__post_init__`` detects via ``"~" in s or "/"
    in s`` so the path branch runs instead. Locking it ensures the
    validator does not accidentally match a path that happens to
    contain ``ssh-rsa`` etc.
    """
    assert UserData._is_valid_ssh_public_key("~/.ssh/id_ed25519.pub") is False


def test_wrong_keytype_prefix_returns_false() -> None:
    """``sk-ssh-ed25519@openssh.com`` (a hardware-backed key) is not yet supported.

    Today's validator only handles the three classic types. If support
    is added later, this test should be updated, not silently removed.
    """
    key = (
        "sk-ssh-ed25519@openssh.com AAAAGnNrLXNzaC1lZDI1NTE5QG9wZW5zc2guY29t user@host"
    )
    assert UserData._is_valid_ssh_public_key(key) is False
