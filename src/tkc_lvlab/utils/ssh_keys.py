"""SSH public key discovery and validation helpers.

Ported from the sibling `lvscripts-py` project (`src/lvscripts/ssh_keys.py`) as
the first Phase 6 step — see ``docs-extra/lvscripts-survey.md`` §5 "Port + adapt".
This module is a CLI-agnostic public library API: nothing here reads
``Lvlab.yml`` or shells out to ``virsh``/``qemu-img``/``virt-install``. Both
the existing ``lvlab`` manifest workflow and the upcoming standalone
``createvm`` / ``deletevm`` console scripts will depend on it.

The whitelist of accepted key types is intentionally broader than
:func:`tkc_lvlab.utils.cloud_init.UserData._is_valid_ssh_public_key`'s
current regex (which only matches ``ssh-rsa`` / ``ssh-dss`` / ``ssh-ed25519``).
Modern Ed25519 hardware-backed keys (``sk-ssh-ed25519@openssh.com``) and
NIST P-256/384/521 ECDSA keys are first-class here. The narrower legacy
validator stays in place until a later Phase 6 step migrates the
manifest workflow over.
"""

from __future__ import annotations

from base64 import b64decode
import os
from pathlib import Path
import pwd


SUPPORTED_KEY_TYPES: frozenset[str] = frozenset(
    {
        "ssh-ed25519",
        "ssh-rsa",
        "ecdsa-sha2-nistp256",
        "ecdsa-sha2-nistp384",
        "ecdsa-sha2-nistp521",
        "sk-ssh-ed25519@openssh.com",
        "sk-ecdsa-sha2-nistp256@openssh.com",
    }
)
"""Public-key type prefixes this module accepts.

Includes the modern Ed25519 hardware-backed variant (``sk-ssh-ed25519@``)
and the two NIST hardware ECDSA variants. DSA (``ssh-dss``) is deliberately
excluded — it's been deprecated by OpenSSH since 7.0 and dropped by
default since 9.x.
"""


class PublicKeyError(ValueError):
    """Raised when an SSH public key cannot be validated."""


def discover_default_public_keys() -> list[str]:
    """Walk the likely SSH-key locations and return the validated keys found.

    Searched directories (in order, de-duplicated by resolved path):

    1. ``Path.home()/.ssh`` — the current effective user. Under ``sudo`` this
        is root.
    1. ``~`` of the user named by ``$SUDO_USER`` (if set). This is what makes
        ``sudo createvm ...`` pick up the invoking user's keys rather than
        only root's.
    1. ``$HOME/.ssh`` — covers shells where ``HOME`` is set but ``Path.home()``
        resolves elsewhere.

    For each directory, ``id_ed25519.pub`` is checked first, then
    ``id_rsa.pub``. Each successfully validated key is added to the result;
    keys that fail validation are skipped (no exception propagates from a
    discovery walk — callers needing per-file errors should use
    :func:`load_public_key` directly).

    Returns:
        Deduplicated list of validated public-key strings, preserving the
        order in which they were first discovered.
    """
    keys: list[str] = []
    for home_dir in _candidate_home_directories():
        ssh_dir = home_dir / ".ssh"
        for filename in ("id_ed25519.pub", "id_rsa.pub"):
            candidate = ssh_dir / filename
            if candidate.is_file():
                try:
                    keys.append(load_public_key(candidate))
                except PublicKeyError:
                    continue
    return dedupe_public_keys(keys)


def _candidate_home_directories() -> list[Path]:
    """Return the home directories worth checking for SSH keys.

    Combines :func:`Path.home`, the home of ``$SUDO_USER`` if set, and the
    ``$HOME`` env var. Each entry is resolved (without strictness — the
    path doesn't need to exist) and duplicates are dropped while preserving
    discovery order.

    Returns:
        Ordered list of unique candidate home directories.
    """
    homes: list[Path] = []

    homes.append(Path.home())

    sudo_user = os.environ.get("SUDO_USER")
    if sudo_user:
        try:
            homes.append(Path(pwd.getpwnam(sudo_user).pw_dir))
        except KeyError:
            pass

    env_home = os.environ.get("HOME")
    if env_home:
        homes.append(Path(env_home))

    unique: list[Path] = []
    seen: set[Path] = set()
    for home in homes:
        normalized = home.expanduser().resolve(strict=False)
        if normalized in seen:
            continue
        seen.add(normalized)
        unique.append(normalized)
    return unique


def load_public_key(path: Path) -> str:
    """Load and validate an SSH public key from disk.

    Args:
        path: Filesystem path to the ``.pub`` file.

    Returns:
        The validated key string (type + body + optional comment, single-spaced).

    Raises:
        PublicKeyError: If the file does not exist or its contents fail
            :func:`validate_public_key`.
    """
    if not path.is_file():
        raise PublicKeyError(f"SSH public key file '{path}' does not exist.")
    return validate_public_key(path.read_text(encoding="utf-8"))


def validate_public_key(raw_key: str) -> str:
    """Validate an SSH public key string and return a normalized form.

    Validation steps:

    1. The string must split into at least two whitespace-separated tokens
        (type + body).
    1. The first token must appear in :data:`SUPPORTED_KEY_TYPES`.
    1. The second token (the key body) must be valid base64. OpenSSH
        sometimes emits unpadded base64; we tolerate that by re-padding
        before :func:`base64.b64decode` with ``validate=True``.

    Args:
        raw_key: The raw key string (typically the full ``.pub`` file content).

    Returns:
        ``"{type} {body} [comment]"`` with leading/trailing whitespace removed
        and internal whitespace collapsed to single spaces. The comment is
        omitted from the output when the input had none.

    Raises:
        PublicKeyError: If any validation step fails. The exception message
            names the specific failure so the operator can fix it.
    """
    key = raw_key.strip()
    parts = key.split()
    if len(parts) < 2:
        raise PublicKeyError("SSH public key must contain a type and key body.")

    key_type, key_body = parts[0], parts[1]
    if key_type not in SUPPORTED_KEY_TYPES:
        raise PublicKeyError(f"Unsupported SSH public key type '{key_type}'.")

    padding = (-len(key_body)) % 4
    try:
        b64decode(key_body + ("=" * padding), validate=True)
    except ValueError as exc:
        raise PublicKeyError("SSH public key body is not valid base64.") from exc

    comment = " ".join(parts[2:])
    return " ".join(part for part in (key_type, key_body, comment) if part)


def dedupe_public_keys(keys: list[str]) -> list[str]:
    """Remove duplicate keys while preserving first-occurrence order.

    Duplicate detection is exact string match on the already-normalized key
    output of :func:`validate_public_key`. Two physically identical keys
    saved at different paths (e.g. a user-supplied ``--public-key`` that
    duplicates a discovered ``id_ed25519.pub``) collapse to one entry; the
    earlier-discovered copy wins.

    Args:
        keys: List of validated key strings. The list itself is not mutated.

    Returns:
        A new list with duplicates removed, preserving the input order of
        the first occurrence of each unique key.
    """
    seen: set[str] = set()
    unique: list[str] = []
    for key in keys:
        if key in seen:
            continue
        seen.add(key)
        unique.append(key)
    return unique
