"""Unit tests for :mod:`tkc_lvlab.utils.ssh_keys`.

These tests lock in the Phase 6 SSH-key validator contract:

- The 7-type whitelist accepts every modern OpenSSH public-key form (Ed25519
    incl. hardware-backed sk-, RSA, three NIST ECDSA curves, hardware-backed
    NIST ECDSA), and rejects everything else (notably ``ssh-dss``).
- The validator catches malformed base64 bodies and missing tokens.
- Discovery walks ``Path.home``, ``$SUDO_USER``'s home, and ``$HOME`` in that
    order, de-duplicating by resolved path.
- Discovery never raises on a malformed key it finds — it skips and moves on.
- :func:`dedupe_public_keys` preserves first-occurrence order.
"""

from __future__ import annotations

import os
import pwd
from pathlib import Path
from unittest import mock

import pytest

from tkc_lvlab.utils.ssh_keys import (
    PublicKeyError,
    SUPPORTED_KEY_TYPES,
    dedupe_public_keys,
    discover_default_public_keys,
    load_public_key,
    validate_public_key,
)

# Sample bodies chosen for stable base64 (no padding ambiguity).
_ED25519_BODY = "AAAAC3NzaC1lZDI1NTE5AAAAIBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBB"
_RSA_BODY = "AAAAB3NzaC1yc2EAAAADAQABAAABAQCCCCCCCCCCCCCCCCCCCCCCCCCCCC"


# ---------------------------------------------------------------------------
# validate_public_key
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "key_type",
    sorted(SUPPORTED_KEY_TYPES),
)
def test_validate_accepts_every_whitelisted_key_type(key_type: str) -> None:
    """Every type in SUPPORTED_KEY_TYPES must validate when paired with a sane body.

    Regression guard: if the whitelist ever shrinks or a typo creeps in,
    every key of the dropped type stops working for createvm.
    """
    raw = f"{key_type} {_ED25519_BODY} user@host"
    normalized = validate_public_key(raw)
    assert normalized.startswith(key_type + " ")
    assert normalized.endswith(" user@host")


def test_validate_rejects_ssh_dss() -> None:
    """``ssh-dss`` is intentionally NOT in the whitelist — OpenSSH dropped it.

    The narrower legacy validator in cloud_init.py still accepts it; that's
    a separate code path. This module is the modern whitelist.
    """
    raw = f"ssh-dss {_RSA_BODY} legacy@host"
    with pytest.raises(PublicKeyError, match="Unsupported SSH public key type"):
        validate_public_key(raw)


def test_validate_rejects_bogus_key_type() -> None:
    """An obviously invalid type ('nope') trips the whitelist guard."""
    with pytest.raises(PublicKeyError, match="Unsupported SSH public key type"):
        validate_public_key(f"nope {_ED25519_BODY}")


def test_validate_rejects_single_token() -> None:
    """A string with only one whitespace token has no key body to validate."""
    with pytest.raises(PublicKeyError, match="type and key body"):
        validate_public_key("ssh-ed25519")


def test_validate_rejects_bad_base64_body() -> None:
    """A body with non-base64 characters fails — that's the forgery guard.

    If validation ever stops checking the body, a string like
    ``"ssh-ed25519 NOTBASE64@@@@ user@host"`` would silently propagate
    into the rendered cloud-init user-data.
    """
    raw = "ssh-ed25519 NOT_BASE64_@@@ user@host"
    with pytest.raises(PublicKeyError, match="not valid base64"):
        validate_public_key(raw)


def test_validate_normalizes_whitespace_and_keeps_comment() -> None:
    """Multiple spaces / tabs between tokens collapse; the comment is preserved."""
    raw = f"ssh-ed25519   {_ED25519_BODY}\tcomment-with-extra   stuff"
    normalized = validate_public_key(raw)
    assert normalized == f"ssh-ed25519 {_ED25519_BODY} comment-with-extra stuff"


def test_validate_handles_unpadded_base64() -> None:
    """OpenSSH sometimes emits unpadded base64; the validator re-pads.

    Without the re-pad step, a key body whose length isn't a multiple of 4
    would fail validation even though it's perfectly legitimate. Strip the
    trailing equals from a known-good body to exercise this path.
    """
    # _ED25519_BODY happens to need no padding adjustment; trim a char to
    # force the unpadded path.
    unpadded = _ED25519_BODY.rstrip("=")[:-1]  # known-shortened body
    raw = f"ssh-ed25519 {unpadded}"
    # Must not raise.
    normalized = validate_public_key(raw)
    assert normalized.startswith("ssh-ed25519 ")


def test_validate_key_without_comment() -> None:
    """A bare ``type body`` (no comment) validates and round-trips without a trailing space."""
    raw = f"ssh-ed25519 {_ED25519_BODY}"
    normalized = validate_public_key(raw)
    assert normalized == f"ssh-ed25519 {_ED25519_BODY}"
    # No double trailing space, no orphan whitespace.
    assert not normalized.endswith(" ")


# ---------------------------------------------------------------------------
# load_public_key
# ---------------------------------------------------------------------------


def test_load_public_key_missing_file_raises(tmp_path: Path) -> None:
    """An absent path produces a clear PublicKeyError, not a bare OSError."""
    missing = tmp_path / "id_ed25519.pub"
    with pytest.raises(PublicKeyError, match="does not exist"):
        load_public_key(missing)


def test_load_public_key_reads_and_validates(tmp_path: Path) -> None:
    """A real file roundtrips through validate_public_key."""
    pubfile = tmp_path / "id_ed25519.pub"
    pubfile.write_text(f"ssh-ed25519 {_ED25519_BODY} user@host\n")

    loaded = load_public_key(pubfile)
    assert loaded == f"ssh-ed25519 {_ED25519_BODY} user@host"


def test_load_public_key_propagates_validation_error(tmp_path: Path) -> None:
    """If the file's content is invalid, the validation error surfaces."""
    pubfile = tmp_path / "id_ed25519.pub"
    pubfile.write_text("ssh-dss bogusbody\n")
    with pytest.raises(PublicKeyError, match="Unsupported SSH public key type"):
        load_public_key(pubfile)


# ---------------------------------------------------------------------------
# discover_default_public_keys
# ---------------------------------------------------------------------------


def test_discover_walks_path_home_only_when_set(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With no SUDO_USER or HOME override, discovery checks just Path.home()."""
    home = tmp_path / "alice"
    ssh = home / ".ssh"
    ssh.mkdir(parents=True)
    (ssh / "id_ed25519.pub").write_text(f"ssh-ed25519 {_ED25519_BODY} alice@laptop\n")

    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    monkeypatch.delenv("SUDO_USER", raising=False)
    monkeypatch.delenv("HOME", raising=False)

    keys = discover_default_public_keys()
    assert keys == [f"ssh-ed25519 {_ED25519_BODY} alice@laptop"]


def test_discover_walks_sudo_user_home_when_set(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """SUDO_USER's home is searched alongside Path.home() — the sudo-discovery promise.

    Without this, ``sudo createvm ...`` would only pick up root's keys
    (often none), defeating the whole point of the discovery walk.
    """
    root_home = tmp_path / "root"
    user_home = tmp_path / "alice"
    (root_home / ".ssh").mkdir(parents=True)
    (user_home / ".ssh").mkdir(parents=True)
    # Only alice has a key; root has none.
    (user_home / ".ssh" / "id_ed25519.pub").write_text(
        f"ssh-ed25519 {_ED25519_BODY} alice@laptop\n"
    )

    fake_pw = mock.Mock(pw_dir=str(user_home))
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: root_home))
    monkeypatch.setenv("SUDO_USER", "alice")
    monkeypatch.delenv("HOME", raising=False)
    monkeypatch.setattr(pwd, "getpwnam", lambda name: fake_pw)

    keys = discover_default_public_keys()
    assert keys == [f"ssh-ed25519 {_ED25519_BODY} alice@laptop"]


def test_discover_handles_missing_sudo_user_gracefully(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A SUDO_USER that doesn't exist in /etc/passwd must not crash discovery."""
    home = tmp_path / "root"
    (home / ".ssh").mkdir(parents=True)
    (home / ".ssh" / "id_ed25519.pub").write_text(
        f"ssh-ed25519 {_ED25519_BODY} root@host\n"
    )

    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    monkeypatch.setenv("SUDO_USER", "ghost-user-that-does-not-exist")
    monkeypatch.delenv("HOME", raising=False)

    def boom(_name: str) -> None:
        raise KeyError("no such user")

    monkeypatch.setattr(pwd, "getpwnam", boom)

    # Must not raise — the ghost SUDO_USER falls through, root's key still found.
    keys = discover_default_public_keys()
    assert keys == [f"ssh-ed25519 {_ED25519_BODY} root@host"]


def test_discover_prefers_ed25519_over_rsa_when_both_exist(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Per the file-order tuple, id_ed25519.pub is checked first; both are returned in that order."""
    home = tmp_path / "alice"
    ssh = home / ".ssh"
    ssh.mkdir(parents=True)
    (ssh / "id_ed25519.pub").write_text(f"ssh-ed25519 {_ED25519_BODY} alice@ed25519\n")
    (ssh / "id_rsa.pub").write_text(f"ssh-rsa {_RSA_BODY} alice@rsa\n")

    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    monkeypatch.delenv("SUDO_USER", raising=False)
    monkeypatch.delenv("HOME", raising=False)

    keys = discover_default_public_keys()
    assert keys == [
        f"ssh-ed25519 {_ED25519_BODY} alice@ed25519",
        f"ssh-rsa {_RSA_BODY} alice@rsa",
    ]


def test_discover_skips_invalid_keys_silently(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A malformed key file in the discovery path must not abort discovery.

    The user might have a stale or corrupted ``.pub`` from years ago. Failing
    the whole walk would silently produce a VM with zero keys.
    """
    home = tmp_path / "alice"
    ssh = home / ".ssh"
    ssh.mkdir(parents=True)
    # id_ed25519.pub is corrupted, id_rsa.pub is fine.
    (ssh / "id_ed25519.pub").write_text("ssh-ed25519 NOT_BASE64_@@@ broken\n")
    (ssh / "id_rsa.pub").write_text(f"ssh-rsa {_RSA_BODY} alice@host\n")

    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    monkeypatch.delenv("SUDO_USER", raising=False)
    monkeypatch.delenv("HOME", raising=False)

    keys = discover_default_public_keys()
    # Only the good key returns; no exception propagates.
    assert keys == [f"ssh-rsa {_RSA_BODY} alice@host"]


def test_discover_deduplicates_across_resolved_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If Path.home() and $HOME resolve to the same dir, we don't double-emit keys."""
    home = tmp_path / "alice"
    ssh = home / ".ssh"
    ssh.mkdir(parents=True)
    (ssh / "id_ed25519.pub").write_text(f"ssh-ed25519 {_ED25519_BODY} alice@host\n")

    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    monkeypatch.delenv("SUDO_USER", raising=False)
    monkeypatch.setenv("HOME", str(home))

    keys = discover_default_public_keys()
    # Exactly one entry, not two.
    assert keys == [f"ssh-ed25519 {_ED25519_BODY} alice@host"]


# ---------------------------------------------------------------------------
# dedupe_public_keys
# ---------------------------------------------------------------------------


def test_dedupe_preserves_first_occurrence_order() -> None:
    """Duplicates dropped, order of first occurrence preserved.

    Real-bug surface: if dedupe ever reorders, a user-provided
    ``--public-key`` (which is supposed to come after discovered keys)
    could end up first, changing which key cloud-init writes as primary.
    """
    keys = [
        f"ssh-ed25519 {_ED25519_BODY} first",
        f"ssh-rsa {_RSA_BODY} second",
        f"ssh-ed25519 {_ED25519_BODY} first",  # duplicate of #1
        f"ssh-rsa {_RSA_BODY} second",  # duplicate of #2
        f"ssh-ed25519 {_ED25519_BODY} third",  # different comment → different string
    ]
    deduped = dedupe_public_keys(keys)
    assert deduped == [
        f"ssh-ed25519 {_ED25519_BODY} first",
        f"ssh-rsa {_RSA_BODY} second",
        f"ssh-ed25519 {_ED25519_BODY} third",
    ]


def test_dedupe_on_empty_list() -> None:
    """Empty in, empty out — no crash."""
    assert dedupe_public_keys([]) == []
