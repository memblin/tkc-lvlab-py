"""Unit tests for the ``lvlab init`` CLI command.

These tests lock the per-image fetch+verify pipeline by mocking
:class:`tkc_lvlab.utils.images.CloudImage` at the cli import boundary
and asserting on (a) which methods were called and (b) status lines
emitted on stdout. They exist primarily to give the cognitive-
complexity refactor of ``init`` a safety net — coverage on the
command body was previously zero.
"""

from __future__ import annotations

from contextlib import ExitStack
from unittest import mock

from typer.testing import CliRunner

from tkc_lvlab import cli
from tkc_lvlab.cli import app


def _mock_image(name: str, *, has_gpg: bool, has_checksum: bool) -> mock.MagicMock:
    """Build a CloudImage stand-in with realistic attribute shape."""
    img = mock.MagicMock()
    img.name = name
    img.image_fpath = f"/tmp/{name}/image.qcow2"
    img.image_url = f"https://example.invalid/{name}/image.qcow2"
    img.checksum_url_gpg = (
        f"https://example.invalid/{name}/keyring.gpg" if has_gpg else None
    )
    img.checksum_gpg_fpath = f"/tmp/{name}/keyring.gpg" if has_gpg else None
    img.checksum_url = (
        f"https://example.invalid/{name}/SHA256SUM" if has_checksum else None
    )
    img.checksum_fpath = f"/tmp/{name}/SHA256SUM" if has_checksum else None
    img.exists_locally.return_value = False
    img.download_image.return_value = True
    img.download_checksum.return_value = True
    img.download_checksum_gpg.return_value = True
    img.gpg_verify_checksum_file.return_value = True
    img.checksum_verify_image.return_value = True
    return img


def _run_init(images: list[mock.MagicMock]) -> "object":
    """Invoke ``lvlab init`` with ``parse_config`` and ``CloudImage`` patched."""
    runner = CliRunner()
    parse_return = (
        {"name": "test-env"},
        {f"img{idx}": {} for idx, _ in enumerate(images)},
        {},
        [],
    )
    with ExitStack() as stack:
        stack.enter_context(
            mock.patch.object(cli, "parse_config", return_value=parse_return)
        )
        stack.enter_context(mock.patch.object(cli, "CloudImage", side_effect=images))
        return runner.invoke(app, ["init"])


def test_init_downloads_all_artefacts_when_nothing_local() -> None:
    """Nothing on disk → every downloader runs; verifiers stay idle."""
    img = _mock_image("fedora44", has_gpg=True, has_checksum=True)
    result = _run_init([img])

    assert result.exit_code == 0, result.output
    img.download_image.assert_called_once()
    img.download_checksum_gpg.assert_called_once()
    img.download_checksum.assert_called_once()
    img.gpg_verify_checksum_file.assert_not_called()
    img.checksum_verify_image.assert_not_called()
    assert "Initializing Libvirt Lab Environment: test-env" in result.output
    assert "CloudImage downloaded to /tmp/fedora44/image.qcow2" in result.output


def test_init_verifies_when_all_artefacts_already_local() -> None:
    """All on disk → no downloads; both verifiers fire and report OK."""
    img = _mock_image("fedora44", has_gpg=True, has_checksum=True)
    img.exists_locally.return_value = True
    result = _run_init([img])

    assert result.exit_code == 0, result.output
    img.download_image.assert_not_called()
    img.download_checksum.assert_not_called()
    img.download_checksum_gpg.assert_not_called()
    img.gpg_verify_checksum_file.assert_called_once()
    img.checksum_verify_image.assert_called_once()
    assert "CloudImage fedora44 exists locally" in result.output
    assert "CloudImage fedora44 checksum file exists locally" in result.output
    assert "CloudImage fedora44 checksum GPG file exists locally" in result.output
    assert "CloudImage fedora44 checksum file GPG validation OK" in result.output
    assert "CloudImage fedora44 checksum verification OK" in result.output


def test_init_skips_gpg_branch_when_image_has_no_gpg_url() -> None:
    """Debian-style image: no checksum GPG keyring → skip GPG download/verify."""
    img = _mock_image("debian12", has_gpg=False, has_checksum=True)
    img.exists_locally.return_value = True
    result = _run_init([img])

    assert result.exit_code == 0, result.output
    img.download_checksum_gpg.assert_not_called()
    img.gpg_verify_checksum_file.assert_not_called()
    img.checksum_verify_image.assert_called_once()
    assert "checksum GPG" not in result.output


def test_init_skips_checksum_branch_when_image_has_no_checksum_url() -> None:
    """Image without ``checksum_url`` → both checksum branches go quiet."""
    img = _mock_image("custom", has_gpg=False, has_checksum=False)
    img.exists_locally.return_value = True
    result = _run_init([img])

    assert result.exit_code == 0, result.output
    img.download_checksum.assert_not_called()
    img.checksum_verify_image.assert_not_called()
    assert "checksum" not in result.output


def test_init_logs_error_when_image_download_fails() -> None:
    """``download_image`` returning False emits the error log line."""
    img = _mock_image("fedora44", has_gpg=False, has_checksum=False)
    img.download_image.return_value = False
    with mock.patch.object(cli, "logger") as mocked_logger:
        result = _run_init([img])

    assert result.exit_code == 0, result.output
    error_messages = [
        call.args[0] % call.args[1:] if len(call.args) > 1 else call.args[0]
        for call in mocked_logger.error.call_args_list
    ]
    assert any(
        "CloudImage download failed" in msg for msg in error_messages
    ), error_messages


def test_init_handles_parse_failure_via_typeerror() -> None:
    """``parse_config`` raising TypeError → logger.error then sys.exit()."""
    runner = CliRunner()
    with (
        mock.patch.object(cli, "parse_config", side_effect=TypeError),
        mock.patch.object(cli, "logger") as mocked_logger,
    ):
        result = runner.invoke(app, ["init"])

    assert result.exit_code == 0
    mocked_logger.error.assert_called_with("Could not parse config file.")
