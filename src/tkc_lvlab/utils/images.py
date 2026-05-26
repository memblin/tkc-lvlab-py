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
import time
from typing import Any
from urllib.parse import urlparse

import gnupg
import requests
from tqdm import tqdm

from .._logging import get_logger
from .catalog import derive_os_variant, derive_username


logger = get_logger(__name__)

VERIFIED_SUFFIX = ".verified"

# Tolerant-download tuning. A cloud image is ~400 MB and lab hosts often pull
# from flaky community mirrors, so a single connect timeout + no retry (the
# pre-#87 behavior) turned a momentary mirror stall into a hard failure.
#
# Intentional, documented divergence from lvscripts-py (ref #87): lvscripts
# uses a simpler urllib path with no read timeout and no bounded retry. lvscripts
# DOES already download to a ``.partial`` file, so the resume/atomic-rename
# behavior here NARROWS the gap rather than widening it; the retry+backoff and
# the connect/read timeout split are the deliberate additions. Worth mirroring
# upstream (maintainer to push).
_CONNECT_TIMEOUT = 10  # seconds to establish the TCP/TLS connection
_READ_TIMEOUT = 60  # seconds of silence mid-transfer before giving up on a try
_DOWNLOAD_TIMEOUT = (_CONNECT_TIMEOUT, _READ_TIMEOUT)
_MAX_ATTEMPTS = 3
# Backoff before attempts 2 and 3 (index 0 is unused — there is no wait before
# the first attempt). 5/10/20s doubling, hand-rolled to avoid a new dependency.
_RETRY_BACKOFF_SECONDS = (0, 5, 10, 20)
_PARTIAL_SUFFIX = ".partial"

# Transient transport-layer failures worth retrying. A genuine HTTP error
# (404/403) surfaces via ``raise_for_status`` as ``requests.HTTPError``, which
# is deliberately NOT in this tuple — those fail fast with no retry. A
# connection-refused is a ``ConnectionError`` whose underlying error is not a
# read/stall, so it is filtered separately (see ``_is_transient``).
_TRANSIENT_EXCEPTIONS = (
    requests.exceptions.ReadTimeout,
    requests.exceptions.ConnectionError,
    requests.exceptions.ChunkedEncodingError,
)


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
            # Always prefix the local checksum filename with the image
            # filename. Many distros publish a generic, release-agnostic
            # checksum filename that is byte-for-byte the SAME name across
            # releases — Debian/Ubuntu ``SHA512SUMS``/``SHA256SUMS``,
            # AlmaLinux ``CHECKSUM`` — so two configured images of the same
            # family (e.g. almalinux9 + almalinux10, or jammy + noble)
            # would otherwise clobber each other's cached checksum file.
            # The image-filename prefix makes every checksum file unique.
            # (Fedora's name is already per-release, but prefixing it too
            # keeps the rule uniform and future-proof.)
            checksum_basename = os.path.basename(urlparse(self.checksum_url).path)
            self.checksum_fpath = os.path.join(
                os.path.expanduser(self.image_dir),
                f"{self.filename}.{checksum_basename}",
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
    def _is_transient(exc: Exception) -> bool:
        """Classify a download exception as transient (retry) or fatal (fail fast).

        ``requests.ConnectionError`` is overloaded: a mid-transfer drop is
        worth retrying, but a connection *refused* (nothing listening) is not
        going to fix itself within three attempts and should fail fast like a
        404. We treat a refused connection — recognizable by its message — as
        fatal; every other ``ConnectionError`` (DNS hiccup, reset, dropped
        socket) is transient.

        Args:
            exc: The exception raised by the download attempt.

        Returns:
            ``True`` when the failure is worth retrying.
        """
        if isinstance(exc, requests.exceptions.ConnectionError):
            return "refused" not in str(exc).lower()
        return isinstance(exc, _TRANSIENT_EXCEPTIONS)

    @classmethod
    def _stream_to_partial(cls, url: str, partial_path: str) -> bool:
        """Stream ``url`` into ``<destination>.partial``, resuming when possible.

        Performs a single attempt. If a ``.partial`` file already exists from a
        prior attempt AND the server advertised ``Accept-Ranges: bytes``, sends
        a ``Range: bytes=<already>-`` header and APPENDS to the partial;
        otherwise the partial is truncated and the transfer restarts. The
        advertised content-length completeness check is preserved (it now
        accounts for any resume offset).

        Args:
            url: HTTP(S) URL to download.
            partial_path: Path to the ``.partial`` scratch file.

        Returns:
            ``True`` when the partial is complete (advertised length matched the
            total bytes on disk, or no length was advertised).

        Raises:
            requests.exceptions.RequestException: Transport-layer failures
                (timeouts, dropped connections) propagate to the retry loop.
            requests.HTTPError: A genuine HTTP error status (404/403/...) from
                ``raise_for_status`` — fatal, not retried.
        """
        already = os.path.getsize(partial_path) if os.path.exists(partial_path) else 0

        headers = {}
        if already:
            headers["Range"] = f"bytes={already}-"

        response = requests.get(
            url, stream=True, timeout=_DOWNLOAD_TIMEOUT, headers=headers
        )
        response.raise_for_status()

        # A 206 means the server honored our Range request — append. Anything
        # else (200, or no resume requested) means we get the whole body, so
        # start the partial over to avoid a corrupt prefix.
        resuming = already and response.status_code == 206
        if not resuming:
            already = 0

        content_length = int(response.headers.get("content-length", 0))
        # content-length on a 206 is the size of the *remaining* range, so the
        # full expected size is the resume offset plus what's left to come.
        total_size = content_length + already if content_length else 0

        block_size = 1024
        mode = "ab" if resuming else "wb"
        with tqdm(
            total=total_size,
            initial=already,
            unit="B",
            unit_scale=True,
        ) as progress_bar:
            with open(partial_path, mode) as file:
                for data in response.iter_content(block_size):
                    progress_bar.update(len(data))
                    file.write(data)

        if total_size and os.path.getsize(partial_path) != total_size:
            return False

        return True

    @classmethod
    def _download_file(cls, url: str, destination: str) -> bool:
        """Download a URL to a local file, tolerant of transient mirror failures.

        Streams into ``<destination>.partial`` with a connect/read timeout
        split (``(10, 60)``) and retries transient transport failures up to
        three times with a 5/10/20s backoff, resuming via an HTTP ``Range``
        request when the server advertised ``Accept-Ranges: bytes`` and a
        partial exists. On success the partial is atomically renamed onto
        ``destination`` (``os.replace``), so a corrupt or truncated file is
        never left at the final path. Genuine HTTP errors (404/403, connection
        refused) fail fast with no retry.

        This is an intentional, documented divergence from lvscripts-py's
        simpler urllib download (ref #87) — see the module-level constants;
        because lvscripts already uses a ``.partial`` strategy, this narrows
        rather than widens the gap.

        Args:
            url: HTTP(S) URL to download.
            destination: Local filesystem path to write to on success.

        Returns:
            ``True`` on a complete, verified-length write. ``False`` if every
            attempt produced a content-length mismatch.

        Raises:
            requests.HTTPError: A genuine HTTP error status (404/403/...) —
                fails fast, no retry.
            requests.exceptions.RequestException: A transport failure that was
                still transient after the final attempt is re-raised.
        """
        logger.info("downloading to: %s", destination)
        partial_path = destination + _PARTIAL_SUFFIX

        last_exc: Exception | None = None
        for attempt in range(1, _MAX_ATTEMPTS + 1):
            if attempt > 1:
                backoff = _RETRY_BACKOFF_SECONDS[attempt - 1]
                logger.warning(
                    "download attempt %d/%d for %s failed (%s); retrying in %ds",
                    attempt - 1,
                    _MAX_ATTEMPTS,
                    url,
                    last_exc,
                    backoff,
                )
                time.sleep(backoff)

            try:
                if cls._stream_to_partial(url, partial_path):
                    os.replace(partial_path, destination)
                    return True
                # Incomplete transfer (content-length mismatch). Treat like a
                # transient failure: keep the partial and retry with Range.
                last_exc = RuntimeError("incomplete transfer (content-length mismatch)")
            except requests.HTTPError:
                # Genuine HTTP status error (404/403/...). Fail fast — no point
                # retrying, and don't leave a half-written/error-body partial.
                if os.path.exists(partial_path):
                    os.remove(partial_path)
                raise
            except requests.exceptions.RequestException as exc:
                if not cls._is_transient(exc):
                    # e.g. connection refused — nothing to retry against.
                    raise
                last_exc = exc

        logger.error(
            "download of %s failed after %d attempts: %s",
            url,
            _MAX_ATTEMPTS,
            last_exc,
        )
        # Every attempt produced a content-length mismatch (no exception to
        # re-raise): report failure via the historical ``False`` return so
        # callers' existing failure messages still fire.
        if isinstance(last_exc, requests.exceptions.RequestException):
            raise last_exc
        return False

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

        Recognizes the upstream formats lvlab's images use:

        - **Fedora**: ``SHA256 (filename) = hex``
        - **Debian**: ``hex  filename`` (two-space separator,
            tolerated as ``\\s+``)
        - **Ubuntu**: ``hex *filename`` — same as Debian but with GNU
            coreutils' binary-mode ``*`` marker prefixing the filename.
            The marker is stripped so the key matches the bare image
            filename the caller looks up.

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
                # Strip GNU coreutils' binary-mode marker (Ubuntu's
                # SHA256SUMS uses ``hex *filename``); Debian/Fedora have
                # no leading ``*`` so this is a no-op for them.
                filename = match.group(2).removeprefix("*")
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
