"""Unit tests for :mod:`tkc_lvlab.utils.passwords`.

Locked-in contracts:

- :func:`generate_password_phrase` emits ``word_count`` words from
    :data:`WORD_LIST`, dash-joined.
- Every word in the output has at least one uppercase AND at least one
    lowercase letter — the mixed-case floor that stops the case
    randomizer from degenerating into a low-entropy output.
- :func:`hash_password_sha512` rejects ``rounds <= 0`` with ``ValueError``
    rather than silently defaulting.
- :func:`hash_password_sha512` shells out to ``openssl passwd`` via
    stdin (never via argv) so the plaintext never appears in
    ``ps aux``.
- Missing ``openssl`` binary becomes :class:`PasswordHashError` with a
    clear "not in PATH" message, not a bare ``FileNotFoundError``.
- A non-zero ``openssl`` exit becomes ``PasswordHashError`` with the
    captured stderr/stdout message folded in.
"""

from __future__ import annotations

import subprocess
from unittest import mock

import pytest

from tkc_lvlab.utils.passwords import (
    PasswordHashError,
    WORD_LIST,
    generate_password_phrase,
    hash_password_sha512,
)


# ---------------------------------------------------------------------------
# generate_password_phrase
# ---------------------------------------------------------------------------


def test_phrase_default_word_count_is_four() -> None:
    """Default ``word_count`` yields four dash-separated tokens."""
    phrase = generate_password_phrase()
    assert phrase.count("-") == 3
    assert len(phrase.split("-")) == 4


@pytest.mark.parametrize("word_count", [1, 2, 5, 8])
def test_phrase_respects_word_count(word_count: int) -> None:
    """The ``word_count`` argument controls the number of words emitted."""
    phrase = generate_password_phrase(word_count=word_count)
    assert len(phrase.split("-")) == word_count


def test_phrase_words_drawn_from_wordlist() -> None:
    """Every word in the result is one of the entries in WORD_LIST, case-insensitive.

    Regression guard: if generation ever pulls from a different source
    (a typo'd module-level reference, an inadvertent input() override
    in tests, etc.), this catches it.
    """
    phrase = generate_password_phrase(word_count=8)
    for word in phrase.split("-"):
        assert (
            word.lower() in WORD_LIST
        ), f"{word!r} not in WORD_LIST — generation is drawing from a different source"


def test_phrase_every_word_has_mixed_case() -> None:
    """The mixed-case floor: every word has at least one upper AND one lower letter.

    This is the meaningful entropy invariant. The randomizer's first pass
    might produce an all-lower or all-upper word; the helper's fixup logic
    flips one character to restore mixed case. Without it, ~1/2^(n-1)
    words would land single-case.

    Running the generator many times here exercises the randomizer; one
    bad word out of hundreds would be a real bug.
    """
    for _ in range(200):
        phrase = generate_password_phrase(word_count=4)
        for word in phrase.split("-"):
            assert any(
                ch.isupper() for ch in word
            ), f"{word!r} has no uppercase — mixed-case floor regressed"
            assert any(
                ch.islower() for ch in word
            ), f"{word!r} has no lowercase — mixed-case floor regressed"


# ---------------------------------------------------------------------------
# hash_password_sha512 — argument validation
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("bad_rounds", [0, -1, -1000])
def test_hash_rounds_must_be_positive(bad_rounds: int) -> None:
    """Non-positive ``rounds`` raises ``ValueError`` instead of silently defaulting.

    A silent default would mean an operator who typed ``--rounds 0``
    thinking they were "disabling" rounds would actually get cloud-init's
    default — surprising and indistinguishable from intended use.
    """
    with pytest.raises(ValueError, match="greater than zero"):
        hash_password_sha512("secret", rounds=bad_rounds)


# ---------------------------------------------------------------------------
# hash_password_sha512 — subprocess shape
# ---------------------------------------------------------------------------


def test_hash_invokes_openssl_with_expected_argv(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``openssl passwd -6 -salt rounds=N$<salt> -stdin`` is the argv shape we hand to subprocess.run.

    Regression guard: an argv drift (e.g. switching to ``-5`` for MD5,
    losing ``-stdin`` and passing the password as argv) is a silent
    security regression. Lock the shape.
    """
    captured: dict = {}

    def fake_run(
        cmd: list[str], *, input: str, capture_output: bool, check: bool, text: bool
    ) -> subprocess.CompletedProcess[str]:
        captured["cmd"] = cmd
        captured["input"] = input
        captured["check"] = check
        return subprocess.CompletedProcess(
            args=cmd, returncode=0, stdout="$6$rounds=4096$salt$hash\n", stderr=""
        )

    monkeypatch.setattr(subprocess, "run", fake_run)

    result = hash_password_sha512("hello-world", rounds=4096)

    assert captured["cmd"][0] == "openssl"
    assert captured["cmd"][1] == "passwd"
    assert captured["cmd"][2] == "-6"  # SHA-512
    assert captured["cmd"][3] == "-salt"
    assert captured["cmd"][4].startswith("rounds=4096$")
    assert captured["cmd"][5] == "-stdin"
    # Password goes via stdin, NOT via argv.
    assert captured["input"] == "hello-world\n"
    assert "hello-world" not in " ".join(captured["cmd"])
    # The function strips trailing whitespace from stdout.
    assert result == "$6$rounds=4096$salt$hash"


def test_hash_passes_custom_rounds(monkeypatch: pytest.MonkeyPatch) -> None:
    """A non-default ``rounds`` flows through to the openssl salt arg."""
    captured: list[list[str]] = []

    def fake_run(
        cmd: list[str], *, input: str, capture_output: bool, check: bool, text: bool
    ) -> subprocess.CompletedProcess[str]:
        captured.append(cmd)
        return subprocess.CompletedProcess(
            args=cmd, returncode=0, stdout="$6$rounds=10000$x$y\n", stderr=""
        )

    monkeypatch.setattr(subprocess, "run", fake_run)
    hash_password_sha512("pw", rounds=10000)

    assert captured[0][4].startswith("rounds=10000$")


# ---------------------------------------------------------------------------
# hash_password_sha512 — error translation
# ---------------------------------------------------------------------------


def test_hash_missing_openssl_translates_to_password_hash_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A missing ``openssl`` binary surfaces as PasswordHashError, not FileNotFoundError.

    Real-bug surface: a bare FileNotFoundError leaks subprocess internals
    into a CLI traceback. The wrapper translates it into the project's
    domain error type with an operator-actionable message.
    """

    def boom(*args, **kwargs):
        raise FileNotFoundError("[Errno 2] No such file or directory: 'openssl'")

    monkeypatch.setattr(subprocess, "run", boom)

    with pytest.raises(PasswordHashError, match="not installed or not in PATH"):
        hash_password_sha512("any-password", rounds=4096)


def test_hash_openssl_failure_preserves_stderr_in_message(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A non-zero openssl exit becomes PasswordHashError with the captured stderr text."""

    def fake_run(cmd, **kwargs) -> subprocess.CompletedProcess[str]:
        raise subprocess.CalledProcessError(
            returncode=1,
            cmd=cmd,
            stderr="openssl: invalid option -- '-7'\n",
            output="",
        )

    monkeypatch.setattr(subprocess, "run", fake_run)

    with pytest.raises(PasswordHashError, match="invalid option"):
        hash_password_sha512("any-password", rounds=4096)


def test_hash_openssl_failure_falls_back_to_stdout_when_stderr_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When stderr is empty, the error message falls back to captured stdout."""

    def fake_run(cmd, **kwargs) -> subprocess.CompletedProcess[str]:
        raise subprocess.CalledProcessError(
            returncode=2, cmd=cmd, stderr="", output="some-stdout-detail\n"
        )

    monkeypatch.setattr(subprocess, "run", fake_run)

    with pytest.raises(PasswordHashError, match="some-stdout-detail"):
        hash_password_sha512("pw", rounds=4096)
