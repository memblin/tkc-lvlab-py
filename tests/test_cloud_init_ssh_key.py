"""Unit tests for ``UserData._is_valid_ssh_public_key`` and the
``UserData.__post_init__`` resolution path that consumes it.

The validator returns a ``(bool, str)`` tuple — ``(True, key_type)`` on
a match, ``(False, "")`` on a miss. The Phase 9 destructive smoke test
on 2026-05-23 surfaced two bugs in the prior implementation:

- The regex required at most a single non-whitespace token as a
  trailing comment (``(?:[^\\s]+)?$``). SSH key comments are
  free-form, so a key generated with a multi-word ``-C`` comment
  (e.g. ``-C "user@host (created for ...)"``) failed to match.
- The validator used to return bare ``False`` on miss, but the
  ``__post_init__`` caller unconditionally tuple-unpacked the result
  — so the miss-on-multi-word-comment turned into a hard
  ``TypeError: cannot unpack non-iterable bool object`` crash
  during ``lvlab up``.

These tests pin the post-fix contract: any string starting with a
recognized key-type prefix plus a base64 blob matches (anything after
the blob is ignored); other strings return ``(False, "")``; and
``__post_init__`` no longer crashes on a non-key file.

Real-bug surface: if the regex stops matching ``ssh-ed25519``,
``__post_init__`` quietly leaves ``cloud_init["pubkey"]`` as a path
string and the rendered cloud-init user-data goes out with a literal
path where the key should be. The VM then comes up without the
developer's key — a long debug session.
"""

from __future__ import annotations

from pathlib import Path

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

    If the patterns ever start requiring it, every key generated via
    ``ssh-keygen`` with ``-C ""`` would fall through and the file-read
    path in ``__post_init__`` would silently leave the path string in
    place.
    """
    key = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIexampleexample"
    result = UserData._is_valid_ssh_public_key(key)
    assert result == (True, "ssh-ed25519"), result


def test_key_with_multi_word_comment_still_matches() -> None:
    """A key with a multi-word comment (the Phase 9 smoke-test bug)
    still validates.

    The prior regex required ``(?:[^\\s]+)?$``, so any comment with
    spaces, parens, or other whitespace fell through. ``ssh-keygen
    -C "user@host (created for X)"`` produced exactly that shape and
    crashed ``lvlab up`` on a one-line key file.
    """
    key = (
        "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIexampleexample "
        "tkcadmin@host (created for lvlab smoke test 2026-05-23)"
    )
    result = UserData._is_valid_ssh_public_key(key)
    assert result == (True, "ssh-ed25519"), result


def test_key_with_trailing_newline_still_matches() -> None:
    """A file read of a pubkey ends with a newline — that must not
    block the match.

    ``open(...).read()`` returns the file content including any
    trailing ``\\n``. The validator runs against that raw content.
    """
    key = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIexampleexample user@host\n"
    result = UserData._is_valid_ssh_public_key(key)
    assert result == (True, "ssh-ed25519"), result


def test_garbage_string_returns_false_tuple() -> None:
    """A string that doesn't begin with a known key type returns
    ``(False, "")`` so callers can safely unpack."""
    result = UserData._is_valid_ssh_public_key("hello world")
    assert result == (False, ""), result


def test_empty_string_returns_false_tuple() -> None:
    """An empty string returns ``(False, "")``."""
    result = UserData._is_valid_ssh_public_key("")
    assert result == (False, ""), result


def test_path_string_returns_false_tuple() -> None:
    """A path string (as ``cloud_init.pubkey`` might be) returns
    ``(False, "")``.

    This is the case ``__post_init__`` detects via ``"~" in s or "/"
    in s`` so the path branch runs instead. Locking it ensures the
    validator does not accidentally match a path that happens to
    contain ``ssh-rsa`` etc.
    """
    result = UserData._is_valid_ssh_public_key("~/.ssh/id_ed25519.pub")
    assert result == (False, ""), result


def test_wrong_keytype_prefix_returns_false_tuple() -> None:
    """``sk-ssh-ed25519@openssh.com`` (a hardware-backed key) is not yet supported.

    Today's validator only handles the three classic types. If support
    is added later (per the Phase 6 follow-up to share
    ``tkc_lvlab.utils.ssh_keys.validate_public_key``), this test
    should be updated, not silently removed.
    """
    key = (
        "sk-ssh-ed25519@openssh.com AAAAGnNrLXNzaC1lZDI1NTE5QG9wZW5zc2guY29t user@host"
    )
    result = UserData._is_valid_ssh_public_key(key)
    assert result == (False, ""), result


# ---------------------------------------------------------------------------
# UserData.__post_init__ — regression: no crash on non-key file content
# ---------------------------------------------------------------------------


def test_post_init_does_not_crash_on_invalid_pubkey_file(tmp_path: Path) -> None:
    """The Phase 9 smoke-test crash: a pubkey file whose content the
    validator rejects must NOT take down ``UserData(...)``.

    Reproduction: any file the regex misses (formerly: multi-word
    comments; now: anything that doesn't start with a recognized
    key-type prefix). Before the fix, this raised
    ``TypeError: cannot unpack non-iterable bool object`` because
    ``__post_init__`` tuple-unpacked a bare-``False`` return.
    """
    fake_pubkey = tmp_path / "not_a_pubkey.pub"
    fake_pubkey.write_text("this is not an SSH key\n", encoding="utf-8")

    # Should construct cleanly — no exception. The manifest value
    # stays as the original path string because the validator
    # rejected the file content.
    ud = UserData(
        cloud_init={"pubkey": str(fake_pubkey)},
        hostname="h",
        domain="local",
        fqdn="h.local",
    )
    assert ud.cloud_init["pubkey"] == str(fake_pubkey)


def test_post_init_replaces_path_with_key_on_valid_file(tmp_path: Path) -> None:
    """Happy path: a real pubkey file gets its content read in and
    the path string in the manifest gets rewritten in-place to the
    literal key (trailing whitespace stripped).
    """
    real_pubkey_content = (
        "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIexampleexample user@host\n"
    )
    pubkey_file = tmp_path / "id_ed25519.pub"
    pubkey_file.write_text(real_pubkey_content, encoding="utf-8")

    ud = UserData(
        cloud_init={"pubkey": str(pubkey_file)},
        hostname="h",
        domain="local",
        fqdn="h.local",
    )
    assert ud.cloud_init["pubkey"] == real_pubkey_content.strip()
