"""Password phrase generation and SHA-512 hashing helpers.

Ported from the sibling `lvscripts-py` project (`src/lvscripts/passwords.py`)
as part of Phase 6 — see ``docs-extra/lvscripts-survey.md`` §5 "PORT + ADAPT".
Used by the upcoming standalone ``createvm`` script to give one-off VMs a
human-memorable console password whose ``user-data`` only ever carries the
SHA-512-crypt hash, never the plaintext.

Two functions, kept narrow:

- :func:`generate_password_phrase` returns a dash-separated multi-word
    phrase drawn from a fixed 80-word nature-themed wordlist. Each word
    has its case randomized with a mixed-case invariant enforced so a
    single all-lowercase or all-uppercase word can never slip through —
    that's the meaningful entropy floor.
- :func:`hash_password_sha512` shells out to ``openssl passwd -6`` with
    configurable rounds and returns the ``$6$rounds=...$salt$hash``
    string cloud-init expects.

Nothing here reads ``Lvlab.yml`` or talks to libvirt.
"""

from __future__ import annotations

import secrets
import subprocess

# Re-export so existing imports and isinstance checks keep working after the
# class definition moved to :mod:`tkc_lvlab.exceptions`.
from ..exceptions import PasswordHashError


WORD_LIST: list[str] = [
    "amber",
    "aspen",
    "atlas",
    "beacon",
    "birch",
    "breeze",
    "canyon",
    "cedar",
    "cirrus",
    "clover",
    "comet",
    "coral",
    "cypress",
    "delta",
    "ember",
    "falcon",
    "fern",
    "fjord",
    "flint",
    "forest",
    "frost",
    "galaxy",
    "glacier",
    "grove",
    "harbor",
    "hazel",
    "helios",
    "horizon",
    "island",
    "jasper",
    "jungle",
    "kepler",
    "lagoon",
    "lilac",
    "lotus",
    "marble",
    "meadow",
    "meteor",
    "moss",
    "nebula",
    "nova",
    "oasis",
    "onyx",
    "orchid",
    "paragon",
    "pebble",
    "phoenix",
    "pine",
    "prairie",
    "quartz",
    "quill",
    "raven",
    "reef",
    "ridge",
    "river",
    "saffron",
    "sage",
    "sierra",
    "solstice",
    "spruce",
    "summit",
    "thunder",
    "timber",
    "topaz",
    "valley",
    "velvet",
    "vertex",
    "violet",
    "willow",
    "zephyr",
]
"""The 80-entry nature-themed wordlist. Deliberately small and curated —
a familiar tone for human readers, with enough entropy that a 4-word
phrase from it (counting per-character case randomization) is fine for
ephemeral lab VMs.
"""


_SHA512_CRYPT_SALT_CHARS: str = (
    "./0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"
)


def generate_password_phrase(word_count: int = 4) -> str:
    """Return a memorable dash-separated phrase from :data:`WORD_LIST`.

    Each word's case is randomized character-by-character; the helper
    guarantees that every word in the result contains at least one
    uppercase AND at least one lowercase letter. That's the meaningful
    invariant — it stops the generator from emitting a degenerate
    all-lower or all-upper word that would silently shrink entropy.

    Args:
        word_count: Number of words in the phrase. Defaults to 4.

    Returns:
        Dash-joined phrase, e.g. ``"AmbER-faLcOn-MEAdoW-zEphYr"``.

    Example:
        >>> phrase = generate_password_phrase(word_count=4)
        >>> len(phrase.split("-"))
        4
    """
    words = [_randomize_word_case(secrets.choice(WORD_LIST)) for _ in range(word_count)]
    return "-".join(words)


def hash_password_sha512(password: str, rounds: int = 4096) -> str:
    """Hash a password using SHA-512 crypt with configurable rounds.

    Shells out to ``openssl passwd -6 -salt rounds=<rounds>$<salt> -stdin``
    and returns the resulting ``$6$rounds=...$salt$hash`` string that
    cloud-init's ``user-data`` accepts under ``users[*].passwd``.

    The plaintext is sent via stdin, never via an argv flag — so it
    never appears in ``ps aux`` output.

    Args:
        password: The plaintext to hash. Typically the output of
            :func:`generate_password_phrase`.
        rounds: SHA-512-crypt rounds parameter. Must be positive.
            Defaults to 4096 — matches lvscripts' default and is
            fine for ephemeral lab VMs.

    Returns:
        The ``$6$rounds=...$salt$hash`` crypt string.

    Raises:
        ValueError: If ``rounds <= 0``.
        PasswordHashError: If ``openssl`` is missing from ``PATH`` or
            ``openssl passwd`` exits non-zero. The message names
            which case occurred.
    """
    if rounds <= 0:
        raise ValueError("Rounds must be greater than zero.")

    salt = _generate_sha512_crypt_salt()
    full_salt = f"rounds={rounds}${salt}"

    try:
        result = subprocess.run(
            ["openssl", "passwd", "-6", "-salt", full_salt, "-stdin"],
            input=f"{password}\n",
            capture_output=True,
            check=True,
            text=True,
        )
    except FileNotFoundError as exc:
        raise PasswordHashError(
            "Unable to generate password hash: 'openssl' is not installed or "
            "not in PATH."
        ) from exc
    except subprocess.CalledProcessError as exc:
        details = (exc.stderr or exc.stdout or "unknown error").strip()
        raise PasswordHashError(
            f"Unable to generate password hash with openssl: {details}"
        ) from exc

    return result.stdout.strip()


def generate_one_time_password(rounds: int = 4096) -> tuple[str, str]:
    """Generate a one-time console password as ``(plaintext, crypt_hash)``.

    Pairs :func:`generate_password_phrase` with :func:`hash_password_sha512`
    so both deploy paths — the standalone ``createvm`` script and
    ``lvlab up`` — mint the console password identically (issue #106). The
    plaintext is meant to be printed once and never persisted; only the
    hash is written into cloud-init's ``users[*].passwd``.

    Args:
        rounds: SHA-512-crypt rounds, forwarded to
            :func:`hash_password_sha512`.

    Returns:
        A ``(plaintext, crypt_hash)`` pair.

    Raises:
        PasswordHashError: ``openssl`` is missing or ``openssl passwd``
            failed.
    """
    plaintext = generate_password_phrase()
    return plaintext, hash_password_sha512(plaintext, rounds=rounds)


def _generate_sha512_crypt_salt(length: int = 16) -> str:
    """Return a random crypt-compatible salt of the given length.

    Args:
        length: Number of salt characters. Defaults to 16, the upper
            limit for SHA-512 crypt as documented in
            ``crypt(3)``'s manpage.

    Returns:
        A string drawn from the crypt salt alphabet.
    """
    return "".join(secrets.choice(_SHA512_CRYPT_SALT_CHARS) for _ in range(length))


def _randomize_word_case(word: str) -> str:
    """Apply random per-character case to ``word`` with a mixed-case floor.

    If the random toss happens to produce an all-upper or all-lower word
    after the first pass, one character is flipped to restore the
    invariant. Without this floor, ~1/2^(len(word)-1) words would land
    all-one-case and weaken the phrase.

    Args:
        word: The lowercase source word from :data:`WORD_LIST`.

    Returns:
        The same letters with case randomized, guaranteed to contain at
        least one uppercase AND at least one lowercase character.
    """
    chars = [ch.upper() if secrets.randbelow(2) else ch.lower() for ch in word]

    if not any(ch.isupper() for ch in chars):
        index = secrets.randbelow(len(chars))
        chars[index] = chars[index].upper()
    if not any(ch.islower() for ch in chars):
        index = secrets.randbelow(len(chars))
        chars[index] = chars[index].lower()

    return "".join(chars)
