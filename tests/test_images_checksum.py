"""Unit tests for ``CloudImage._parse_checksum_file``.

The parser handles two distinct upstream checksum formats and a verified
swap. Each behavior locked in here came from a real-bug surface:

- **Fedora's** ``SHA256 (filename) = hex`` format — the format Fedora's
    ``CHECKSUM`` file uses. Parser must extract ``filename`` and ``hex``.
- **Debian's** ``hex  filename`` format — the format Debian's
    ``SHA512SUMS`` file uses. Parser must extract the same shape.
- **Verified swap.** When a ``<checksum>.verified`` companion file exists
    (post-GPG verification), the parser must prefer it over the original.
    This is what stops a forged/corrupted checksum file from passing the
    image verification even after the GPG step succeeded.
"""

from __future__ import annotations

from pathlib import Path

from tkc_lvlab.utils.images import CloudImage


FEDORA_SAMPLE = """# Comment line that should not break parsing
SHA256 (Fedora-Cloud-Base-Generic.x86_64-40-1.14.qcow2) = abc123def456
SHA256 (Fedora-Container-Base-40-1.14.x86_64.tar.xz) = 999888777666
"""

DEBIAN_SAMPLE = """deadbeefcafe  debian-12-generic-amd64-20240717-1811.qcow2
0011223344ff  debian-12-genericcloud-amd64-20240717-1811.qcow2
"""


def test_parse_checksum_file_fedora_format(tmp_path: Path) -> None:
    """Fedora's ``SHA256 (file) = hash`` format parses correctly.

    Real-bug surface: a regression here would silently mismatch every
    Fedora image (the parser returning an empty dict means
    ``checksums.get(filename)`` is None and ``checksum_verify_image``
    quietly returns False — no checksum mismatch error, just "verify
    failed" with no detail).
    """
    sums = tmp_path / "Fedora-Cloud-40-1.14-x86_64-CHECKSUM"
    sums.write_text(FEDORA_SAMPLE)

    parsed = CloudImage._parse_checksum_file(str(sums))

    assert (
        parsed["Fedora-Cloud-Base-Generic.x86_64-40-1.14.qcow2"] == "abc123def456"
    ), parsed
    assert parsed["Fedora-Container-Base-40-1.14.x86_64.tar.xz"] == "999888777666"


def test_parse_checksum_file_debian_format(tmp_path: Path) -> None:
    """Debian's ``hash  file`` format parses correctly."""
    sums = tmp_path / "SHA512SUMS"
    sums.write_text(DEBIAN_SAMPLE)

    parsed = CloudImage._parse_checksum_file(str(sums))

    assert (
        parsed["debian-12-generic-amd64-20240717-1811.qcow2"] == "deadbeefcafe"
    ), parsed
    assert parsed["debian-12-genericcloud-amd64-20240717-1811.qcow2"] == "0011223344ff"


def test_parse_checksum_file_ubuntu_binary_marker_format(tmp_path: Path) -> None:
    """Ubuntu's ``hex *filename`` (GNU coreutils binary marker) parses with
    the ``*`` stripped, so the key matches the bare image filename a caller
    looks up — without this, verification of an Ubuntu image silently
    failed (key was ``*jammy-...img``, lookup was ``jammy-...img``)."""
    sums = tmp_path / "SHA256SUMS"
    sums.write_text(
        "f6729b53d930d7f0 *jammy-server-cloudimg-amd64.img\n"
        "53fdde898feed8b0 *noble-server-cloudimg-amd64.img\n"
    )

    parsed = CloudImage._parse_checksum_file(str(sums))

    assert parsed["jammy-server-cloudimg-amd64.img"] == "f6729b53d930d7f0"
    assert parsed["noble-server-cloudimg-amd64.img"] == "53fdde898feed8b0"
    # The literal marker must not survive in the key.
    assert not any(name.startswith("*") for name in parsed)


def test_parse_checksum_file_prefers_verified_companion(tmp_path: Path) -> None:
    """When ``<path>.verified`` exists, it wins over the original file.

    This is THE security property of the GPG step. If the parser ever
    starts ignoring the .verified file, a forged checksum file in the
    cache would silently pass verification.
    """
    sums = tmp_path / "Fedora-Cloud-40-1.14-x86_64-CHECKSUM"
    sums.write_text(
        "SHA256 (Fedora-Cloud-Base-Generic.x86_64-40-1.14.qcow2) = FORGED\n"
    )
    verified = tmp_path / "Fedora-Cloud-40-1.14-x86_64-CHECKSUM.verified"
    verified.write_text(
        "SHA256 (Fedora-Cloud-Base-Generic.x86_64-40-1.14.qcow2) = REAL_HASH\n"
    )

    parsed = CloudImage._parse_checksum_file(str(sums))

    assert (
        parsed["Fedora-Cloud-Base-Generic.x86_64-40-1.14.qcow2"] == "REAL_HASH"
    ), "verified companion file must take precedence to preserve the GPG trust chain"


def test_parse_checksum_file_empty(tmp_path: Path) -> None:
    """An empty checksum file yields an empty dict (no crash, no garbage)."""
    sums = tmp_path / "EMPTY"
    sums.write_text("")
    assert CloudImage._parse_checksum_file(str(sums)) == {}


def test_parse_checksum_file_comments_and_blank_lines_ignored(tmp_path: Path) -> None:
    """Comment lines and blanks must not produce phantom entries.

    Fedora's CHECKSUM files start with PGP signature blocks and
    comment lines; the parser must skip those without inventing
    keys like ``''`` or ``'#'`` in the output dict.
    """
    sums = tmp_path / "SHA512SUMS"
    sums.write_text(
        "\n"
        "# This is a comment\n"
        "-----BEGIN PGP SIGNED MESSAGE-----\n"
        "Hash: SHA512\n"
        "\n"
        "abc123  debian-12-generic-amd64.qcow2\n"
        "\n"
        "# Another comment\n"
    )
    parsed = CloudImage._parse_checksum_file(str(sums))

    # The one real entry is present.
    assert parsed.get("debian-12-generic-amd64.qcow2") == "abc123"
    # No accidental entries with comment-ish keys.
    assert "" not in parsed
    assert "#" not in parsed
    assert "-----BEGIN" not in parsed
