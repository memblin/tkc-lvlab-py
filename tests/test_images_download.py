"""Unit tests for ``CloudImage._download_file`` resilience (issue #87).

Cloud images are ~400 MB and lab hosts often pull from flaky community
mirrors. The pre-#87 download did a single ``requests.get(timeout=10)`` with
no read timeout and no retry, so a momentary mid-transfer stall became a hard
failure and a 404 body could be written to the final path and pass as
"downloaded". These tests lock in the three behaviors that fix that:

- **Transient retry.** A ``ReadTimeout`` on the first attempt followed by a
    clean second attempt must complete — no exception out of ``_download_file``,
    and the final file present with the full body.
- **Fail fast on genuine HTTP errors.** A 404 must raise immediately with no
    retry (``requests.get`` called exactly once) and must NOT leave a file at
    the final destination.
- **Resume.** When a ``.partial`` exists and the first response advertised
    ``Accept-Ranges: bytes``, the retry must send a ``Range: bytes=<n>-``
    header and append rather than restart.

All tests are pure: ``requests.get`` is mocked, ``time.sleep`` is patched to a
no-op so the backoff doesn't slow the suite, and every byte lands under
``tmp_path``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Iterable
from unittest.mock import patch

import pytest
import requests

from tkc_lvlab.utils.images import CloudImage


class _FakeResponse:
    """Minimal stand-in for a streamed ``requests`` response.

    Mimics only the surface ``_stream_to_partial`` touches:
    ``raise_for_status``, ``headers.get``, ``status_code``, and
    ``iter_content``.
    """

    def __init__(
        self,
        *,
        body: bytes = b"",
        status_code: int = 200,
        accept_ranges: bool = False,
        http_error: bool = False,
    ) -> None:
        self._body = body
        self.status_code = status_code
        self.headers: dict[str, str] = {"content-length": str(len(body))}
        if accept_ranges:
            self.headers["accept-ranges"] = "bytes"
        self._http_error = http_error

    def raise_for_status(self) -> None:
        if self._http_error:
            raise requests.HTTPError(f"{self.status_code} error")

    def iter_content(self, block_size: int) -> Iterable[bytes]:
        for i in range(0, len(self._body), block_size):
            yield self._body[i : i + block_size]


def test_readtimeout_then_success_completes(tmp_path: Path) -> None:
    """A ReadTimeout on attempt 1 followed by success on attempt 2 completes.

    Real-bug surface: pre-#87 a single mid-transfer stall raised straight out
    of ``_download_file``. The bounded retry must swallow the transient
    ReadTimeout, retry, and land the full body atomically at the destination.
    """
    destination = tmp_path / "image.qcow2"
    payload = b"x" * 4096

    # First call raises ReadTimeout (mid-transfer stall); second succeeds.
    side_effects = [
        requests.exceptions.ReadTimeout("read timed out"),
        _FakeResponse(body=payload),
    ]

    with patch("tkc_lvlab.utils.images.requests.get", side_effect=side_effects) as get:
        with patch("tkc_lvlab.utils.images.time.sleep") as sleep:
            result = CloudImage._download_file(
                "https://mirror.example/image.qcow2", str(destination)
            )

    assert result is True
    assert destination.read_bytes() == payload
    # No partial left behind after the atomic rename.
    assert not (tmp_path / "image.qcow2.partial").exists()
    assert get.call_count == 2
    # Backoff slept exactly once (before the second attempt).
    sleep.assert_called_once()


def test_http_404_fails_fast_with_no_retry(tmp_path: Path) -> None:
    """A genuine 404 fails fast: raises, no retry, no file at destination.

    Real-bug surface: retrying a 404 wastes ~35s of backoff for a URL that
    will never resolve, and (pre-#87) the error body could be written to the
    final path and silently pass downstream verification as a "downloaded"
    image. The fix must raise ``requests.HTTPError`` after a single GET and
    leave nothing at the destination.
    """
    destination = tmp_path / "image.qcow2"

    not_found = _FakeResponse(status_code=404, http_error=True)

    with patch("tkc_lvlab.utils.images.requests.get", return_value=not_found) as get:
        with patch("tkc_lvlab.utils.images.time.sleep") as sleep:
            with pytest.raises(requests.HTTPError):
                CloudImage._download_file(
                    "https://mirror.example/missing.qcow2", str(destination)
                )

    assert get.call_count == 1  # no retry on a genuine HTTP error
    sleep.assert_not_called()
    assert not destination.exists()
    assert not (tmp_path / "image.qcow2.partial").exists()


def test_resume_sends_range_header_when_partial_exists(tmp_path: Path) -> None:
    """Retry resumes with a Range header when a partial + Accept-Ranges exist.

    Real-bug surface: without resume, a stall near the end of a ~400 MB
    transfer re-downloads the whole image every retry. When the first response
    advertised ``Accept-Ranges: bytes`` and a ``.partial`` survives, the retry
    must send ``Range: bytes=<already>-`` and APPEND the remaining bytes rather
    than restart.
    """
    destination = tmp_path / "image.qcow2"
    partial = tmp_path / "image.qcow2.partial"

    head = b"h" * 2048  # first chunk delivered before the stall
    tail = b"t" * 2048  # remaining bytes delivered on resume
    full = head + tail

    # Attempt 1: advertise Accept-Ranges and content-length for the full body,
    # deliver the head, then stall (ChunkedEncodingError) leaving a partial.
    # Attempt 2: a 206 carrying only the tail (content-length = remaining).
    first = _FakeResponse(body=full, accept_ranges=True)
    second = _FakeResponse(body=tail, status_code=206)

    def fake_get(url, *, stream, timeout, headers):  # noqa: ARG001
        # Write the head + raise to simulate a stall after partial delivery,
        # but only on the first call.
        if fake_get.calls == 0:
            fake_get.calls += 1
            fake_get.first_headers = headers
            partial.write_bytes(head)
            raise requests.exceptions.ChunkedEncodingError("connection dropped")
        fake_get.calls += 1
        fake_get.second_headers = headers
        return second

    fake_get.calls = 0
    fake_get.first_headers = None
    fake_get.second_headers = None

    with patch("tkc_lvlab.utils.images.requests.get", side_effect=fake_get):
        with patch("tkc_lvlab.utils.images.time.sleep"):
            result = CloudImage._download_file(
                "https://mirror.example/image.qcow2", str(destination)
            )

    assert result is True
    # First attempt sent no Range header (nothing on disk yet).
    assert "Range" not in (fake_get.first_headers or {})
    # Second attempt resumed from the partial's size with a Range header.
    assert fake_get.second_headers.get("Range") == f"bytes={len(head)}-"
    # Final file is head + tail, atomically renamed, no leftover partial.
    assert destination.read_bytes() == full
    assert not partial.exists()


def test_connection_refused_fails_fast_with_no_retry(tmp_path: Path) -> None:
    """A connection-refused ConnectionError is fatal, not transient.

    Real-bug surface: a refused connection (nothing listening) will not fix
    itself within three attempts, so retrying just burns the backoff. It must
    propagate immediately like a 404.
    """
    destination = tmp_path / "image.qcow2"

    refused = requests.exceptions.ConnectionError("Connection refused")

    with patch("tkc_lvlab.utils.images.requests.get", side_effect=refused) as get:
        with patch("tkc_lvlab.utils.images.time.sleep") as sleep:
            with pytest.raises(requests.exceptions.ConnectionError):
                CloudImage._download_file(
                    "https://mirror.example/image.qcow2", str(destination)
                )

    assert get.call_count == 1
    sleep.assert_not_called()
