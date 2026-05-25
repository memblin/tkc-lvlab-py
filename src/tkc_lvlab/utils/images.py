"""Cloud-image download, verification, and on-disk lookup.

:class:`CloudImage` represents one entry from the manifest's ``images:``
block (or from a hardcoded catalog in the standalone scripts). It owns
the URLs to fetch the image, its checksum manifest, and the optional GPG
keyring used to verify that manifest. Verification is two-layered:

1. **GPG verification** of the checksum file. When a ``checksum_url_gpg``
    URL is configured, the keyring is downloaded, the checksum file is
    treated as a clearsigned document, and the verified plaintext is
    written to ``<checksum>.verified`` so subsequent operations prefer
    it over the original. Skipping this leaves the trust chain anchored
    only at the HTTPS layer.
1. **Hash verification** of the image itself against the (verified) checksum
    manifest. Both Fedora's ``SHA256 (file) = hash`` and Debian's
    ``hash  file`` formats are recognized.

Debian's ``SHA512SUMS`` file uses the same filename across releases, so
when the image's filename matches the Debian pattern (``debian-N-...``)
the on-disk checksum filename is prefixed with the image's own basename
to prevent a Debian 11 ``SHA512SUMS`` from clobbering Debian 12's.
"""

from __future__ import annotations

import hashlib
import os
import re
from typing import Any
from urllib.parse import urlparse

import gnupg
import requests
from tqdm import tqdm

from .._logging import get_logger
from .catalog import derive_os_variant, derive_username


logger = get_logger(__name__)

VERIFIED_SUFFIX = ".verified"


class CloudImage:  # pylint: disable=too-many-instance-attributes
    """A cloud image definition resolved to on-disk paths and remote URLs.

    Attributes:
        name: Manifest-side key (e.g. ``fedora40``, ``debian12``).
        image_url: URL of the qcow2 cloud image.
        checksum_url: URL of the checksum manifest. May be ``None`` when
            no checksum is configured (verification is then skipped).
        checksum_type: Hash algorithm — ``sha256`` or ``sha512``.
            Required when ``checksum_url`` is set.
        checksum_url_gpg: URL of the GPG keyring for clearsign-verifying
            the checksum file. ``None`` when no GPG verification is
            configured.
        network_version: cloud-init network-config schema version
            (``1`` ENI-style or ``2`` netplan-style). Selects the
            Jinja template at render time.
        filename: Basename of the image, derived from ``image_url``.
        image_dir: Directory where images are cached on disk
            (``<cloud_image_basedir>/cloud-images``).
        image_fpath: Full path to the on-disk image (cached download).
        checksum_fpath: Full path to the on-disk checksum file, with
            the Debian-name-collision workaround applied when the
            image is a Debian release.
        checksum_gpg_fpath: Full path to the on-disk GPG keyring file.
    """

    def __init__(
        self,
        name: str,
        config: dict[str, Any],
        environment: dict[str, Any],
        config_defaults: dict[str, Any],
    ) -> None:
        """Resolve URLs and on-disk paths for one cloud-image entry.

        Args:
            name: Manifest-side key for the image (used by callers when
                logging or composing per-image cache subdirectories).
            config: One entry from the manifest's ``images`` dict. Honors
                ``image_url``, ``checksum_url``, ``checksum_type``,
                ``checksum_url_gpg``, and ``network_version``.
            environment: The manifest's ``environment[0]`` dict. Unused
                in the current implementation but kept in the signature
                so callers in cli.py can pass it without a special case.
            config_defaults: The manifest's ``config_defaults`` dict.
                Honors ``cloud_image_basedir`` (defaults to
                ``/var/lib/libvirt/images/lvlab``). The actual cache
                directory is conventionally
                ``<cloud_image_basedir>/cloud-images/`` — but if the
                user already pointed ``cloud_image_basedir`` at a
                directory whose tail is ``cloud-images`` (for example
                to share a cache with the standalone ``createvm``
                script, which writes to ``/var/lib/libvirt/images/cloud-images/``),
                the suffix is NOT doubled. The 2026-05-23 destructive
                smoke test surfaced the double-append.
        """
        self.name = name
        self.image_url = config.get("image_url", None)
        self.checksum_url = config.get("checksum_url", None)
        self.checksum_type = config.get("checksum_type", None)
        self.checksum_url_gpg = config.get("checksum_url_gpg", None)
        self.network_version = config.get("network_version", 1)
        # Shared image-entry resolution (see utils/catalog): derived from
        # the image key, override via the entry's os_variant/username.
        # Both deploy paths read these so a manifest image resolves its
        # --os-variant and first-boot user the same way createvm does.
        self.os_variant = derive_os_variant(name, config.get("os_variant"))
        self.default_username = derive_username(name, config.get("username"))
        self.filename = os.path.basename(urlparse(self.image_url).path)

        configured_basedir = config_defaults.get(
            "cloud_image_basedir", "/var/lib/libvirt/images/lvlab"
        )
        # Idempotent ``/cloud-images`` suffix. The 2026-05-23 smoke test
        # set ``cloud_image_basedir: /var/lib/libvirt/images/cloud-images``
        # (to point at the standalone createvm script's cache) and got
        # ``/var/lib/libvirt/images/cloud-images/cloud-images/...`` because
        # the suffix was appended unconditionally. Tail-aware append
        # handles both the legacy parent-dir style and the
        # already-the-cache-dir style without ambiguity.
        if os.path.basename(configured_basedir.rstrip(os.sep)) == "cloud-images":
            self.image_dir = configured_basedir
        else:
            self.image_dir = os.path.join(configured_basedir, "cloud-images")
        self.image_fpath = os.path.join(
            os.path.expanduser(self.image_dir), self.filename
        )

        if self.checksum_url:
            # Debian 10/11/12/13 all publish SHA512SUMS — same filename
            # across releases. Without this prefix, switching between
            # Debian versions would silently overwrite the checksum file
            # belonging to the other release. Tested via the
            # parse_checksum_file fixture suite.
            match = re.search(r"debian-(\d+)", self.filename.lower())
            if match:
                checksum_filename = (
                    f"{self.filename.replace('qcow2', '')}"
                    + os.path.basename(urlparse(self.checksum_url).path)
                )
                self.checksum_fpath = os.path.join(
                    os.path.expanduser(self.image_dir), checksum_filename
                )
            else:
                self.checksum_fpath = os.path.join(
                    os.path.expanduser(self.image_dir),
                    os.path.basename(urlparse(self.checksum_url).path),
                )
        else:
            self.checksum_fpath = None

        if self.checksum_url_gpg:
            self.checksum_gpg_fpath = os.path.join(
                os.path.expanduser(self.image_dir),
                os.path.basename(urlparse(self.checksum_url_gpg).path),
            )
        else:
            self.checksum_gpg_fpath = None

    @staticmethod
    def _download_file(url: str, destination: str) -> bool:
        """Stream a URL to a local file with a tqdm progress bar.

        Args:
            url: HTTP(S) URL to download.
            destination: Local filesystem path to write to.

        Returns:
            ``True`` on a complete write (content-length matched bytes
            written, or the server didn't advertise a length).
            ``False`` if the advertised content-length didn't match
            what we wrote.
        """
        logger.info("downloading to: %s", destination)
        response = requests.get(url, stream=True, timeout=10)

        total_size = int(response.headers.get("content-length", 0))
        block_size = 1024

        with tqdm(total=total_size, unit="B", unit_scale=True) as progress_bar:
            with open(destination, "wb") as file:
                for data in response.iter_content(block_size):
                    progress_bar.update(len(data))
                    file.write(data)

        if total_size not in (0, progress_bar.n):
            return False

        return True

    def _manage_image_dir(self) -> None:
        """Ensure the cloud-images directory exists, expanding ``~`` if needed."""
        if "~" in self.image_dir:
            image_dir = os.path.expanduser(self.image_dir)
        else:
            image_dir = self.image_dir

        if not os.path.isdir(image_dir):
            logger.info("CloudImage creating image directory: %s", image_dir)
            os.makedirs(image_dir, exist_ok=True)

    def download_image(self) -> bool:
        """Download the cloud image to :attr:`image_fpath`.

        Creates the cache directory if needed. Returns ``True`` on
        successful download, ``False`` on content-length mismatch.
        """
        self._manage_image_dir()
        if self._download_file(self.image_url, self.image_fpath):
            return True
        return False

    def download_checksum(self) -> bool:
        """Download the checksum manifest to :attr:`checksum_fpath`.

        Returns:
            ``True`` on successful download, ``False`` on content-length
            mismatch.
        """
        if self._download_file(self.checksum_url, self.checksum_fpath):
            return True
        return False

    def download_checksum_gpg(self) -> bool:
        """Download the GPG keyring to :attr:`checksum_gpg_fpath`.

        Returns:
            ``True`` on successful download, ``False`` on content-length
            mismatch.
        """
        if self._download_file(self.checksum_url_gpg, self.checksum_gpg_fpath):
            return True
        return False

    def exists_locally(self, file_type: str = "image") -> bool:
        """Check whether one of the on-disk artifacts is already cached.

        Args:
            file_type: One of ``"image"``, ``"checksum"``, or
                ``"checksum_gpg"``.

        Returns:
            ``True`` when the corresponding on-disk file exists.

        Raises:
            ValueError: ``file_type`` is not one of the three recognized
                names.
        """
        file_map = {
            "image": self.image_fpath,
            "checksum": self.checksum_fpath,
            "checksum_gpg": self.checksum_gpg_fpath,
        }

        file_to_check = file_map.get(file_type)

        if not file_to_check:
            raise ValueError(f"Unknown file type: {file_type}")

        return os.path.exists(file_to_check)

    def gpg_verify_checksum_file(self) -> bool:
        """Clearsign-verify the checksum file with the imported GPG keyring.

        Reads the GPG keyring from :attr:`checksum_gpg_fpath`, imports
        it, then verifies the checksum file at :attr:`checksum_fpath`.
        On success, writes the verified plaintext to
        ``<checksum>.verified`` so :meth:`checksum_verify_image`
        prefers it over the original.

        Returns:
            ``True`` when verification succeeded and the .verified
            sidecar was written. ``False`` when either input file is
            missing or verification did not produce a ``valid`` result.
        """
        if os.path.isfile(self.checksum_gpg_fpath) and os.path.isfile(
            self.checksum_fpath
        ):
            logger.info(
                "CloudImage %s GPG verification of %s", self.name, self.checksum_fpath
            )

            gpg = gnupg.GPG()
            with open(self.checksum_gpg_fpath, "rb") as keyring_file:
                gpg.import_keys(keyring_file.read())

            with open(self.checksum_fpath, "rb") as signed_file:
                verified = gpg.decrypt(signed_file.read())

            if verified.valid:
                verified_checksum_fpath = self.checksum_fpath + VERIFIED_SUFFIX
                with open(verified_checksum_fpath, "wb") as verified_file:
                    verified_file.write(verified.data)
                    return True

        return False

    @staticmethod
    def _parse_checksum_file(checksum_fpath: str) -> dict[str, str]:
        """Parse a checksum manifest into ``{filename: hash}``.

        Recognizes two upstream formats:

        - **Fedora**: ``SHA256 (filename) = hex``
        - **Debian**: ``hex  filename`` (two-space separator,
            tolerated as ``\\s+``)

        When a ``<checksum_fpath>.verified`` file exists (post-GPG),
        it is preferred over the raw path.

        Args:
            checksum_fpath: On-disk path to the checksum file.

        Returns:
            Dict mapping image filename to its hex hash.
        """
        checksums: dict[str, str] = {}

        # Tightened from .+ to [^)]+/\S+ to remove ReDoS backtracking (python:S5852).
        fedora_pattern = re.compile(r"^SHA\d+ \(([^)]+)\) = (\S+)$")
        debian_pattern = re.compile(r"(\w+)\s+(\S+)")

        if os.path.exists(checksum_fpath + VERIFIED_SUFFIX):
            checksum_fpath = checksum_fpath + VERIFIED_SUFFIX

        with open(checksum_fpath, "r", encoding="utf-8") as checksum_file:
            lines = checksum_file.readlines()

        for line in lines:
            match = fedora_pattern.match(line)
            if match:
                filename = match.group(1)
                checksum = match.group(2)
                checksums[filename] = checksum

            match = debian_pattern.match(line)
            if match:
                checksum = match.group(1)
                filename = match.group(2)
                checksums[filename] = checksum

        return checksums

    def checksum_verify_image(self) -> bool:
        """Hash the on-disk image and compare to the manifest entry.

        Uses the configured :attr:`checksum_type` (``sha256`` /
        ``sha512``) and prefers the ``.verified`` sidecar when one
        exists.

        Returns:
            ``True`` when the computed hash matches the manifest entry
            for the image filename. ``False`` on mismatch OR when
            either the image or the checksum file is missing locally.

        Raises:
            SystemExit: ``checksum_type`` is missing or names an
                unsupported algorithm. Kept as a hard-fail to avoid
                silently skipping verification.
        """
        hash_algorithms = {"sha256": hashlib.sha256, "sha512": hashlib.sha512}

        if self.checksum_type is None:
            raise SystemExit(
                "Please configure a checksum_type if you configure a checksume_url for an image."
            )

        if self.checksum_type in hash_algorithms:
            sha = hash_algorithms[self.checksum_type]()
        else:
            raise SystemExit(f"Unsupported checksum algorithm {self.checksum_type}")

        checksum_fpath = self.checksum_fpath
        if os.path.isfile(self.checksum_fpath + VERIFIED_SUFFIX):
            checksum_fpath += VERIFIED_SUFFIX

        if os.path.isfile(checksum_fpath) and os.path.isfile(self.image_fpath):
            checksums = self._parse_checksum_file(checksum_fpath)
            expected_checksum = checksums.get(os.path.basename(self.image_fpath))

            with open(self.image_fpath, "rb") as verify_file:
                sha.update(verify_file.read())
                caclulated_checksum = sha.hexdigest()

            if caclulated_checksum == expected_checksum:
                return True

        return False
