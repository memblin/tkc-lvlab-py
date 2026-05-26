"""Unit tests for ``tkc_lvlab.config.parse_config`` and ``parse_file_from_url``.

Locked-in behaviors:

- ``parse_config`` returns a 4-tuple ``(environment, images, config_defaults, machines)``.
- It picks ``environment[0]`` when the manifest has a list (the manifest schema
    is a single-element list today, but the slicing is the contract).
- ``config_defaults`` defaults to ``{}`` when missing.
- A missing file returns ``None`` (legacy soft behavior; kept distinct from
    a structural error — this test pins today's contract).
- A structurally invalid manifest (not a mapping, missing ``environment`` or
    ``images``) raises ``ConfigError``.
- Malformed YAML raises ``yaml.YAMLError``.
- ``parse_file_from_url`` is just ``basename(urlparse(url).path)`` — strips
    query strings and fragments, returns empty when no path.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from tkc_lvlab.config import parse_config, parse_file_from_url
from tkc_lvlab.exceptions import ConfigError, LvlabError


SAMPLE_MANIFEST = """---
environment:
  - name: demo
    libvirt_uri: qemu:///session
    config_defaults:
      domain: local
      cpu: 2
    machines:
      - vm_name: alpha
        hostname: alpha
      - vm_name: beta
        hostname: beta

images:
  fedora40:
    image_url: https://example.invalid/fedora.qcow2
    checksum_type: sha256
"""


def test_parse_config_happy_returns_four_tuple(tmp_path: Path) -> None:
    """A valid manifest yields (environment, images, config_defaults, machines)."""
    manifest = tmp_path / "Lvlab.yml"
    manifest.write_text(SAMPLE_MANIFEST)

    env, images, defaults, machines = parse_config(str(manifest))

    assert env["name"] == "demo"
    assert env["libvirt_uri"] == "qemu:///session"
    assert "fedora40" in images
    assert defaults == {"domain": "local", "cpu": 2}
    assert [m["vm_name"] for m in machines] == ["alpha", "beta"]


def test_parse_config_missing_file_returns_none(tmp_path: Path) -> None:
    """Legacy soft behavior: pointing at a non-existent file returns None.

    Regression guard. A *missing* file is deliberately NOT a ``ConfigError``
    — it returns ``None`` and callers translate the resulting unpack
    ``TypeError`` into a parse-error message. A *present-but-broken* manifest
    raises ``ConfigError`` instead (see the structural tests below).
    """
    missing = tmp_path / "Definitely-Not-Here.yml"
    assert parse_config(str(missing)) is None


def test_parse_config_not_a_mapping_raises_configerror(tmp_path: Path) -> None:
    """A manifest that parses to a non-mapping (e.g. a bare list) raises ConfigError."""
    manifest = tmp_path / "Lvlab.yml"
    manifest.write_text("- just\n- a\n- list\n")
    with pytest.raises(ConfigError, match="not a mapping"):
        parse_config(str(manifest))


def test_parse_config_missing_environment_raises_configerror(tmp_path: Path) -> None:
    """A manifest without a non-empty ``environment`` list raises ConfigError."""
    manifest = tmp_path / "Lvlab.yml"
    manifest.write_text("environment: []\nimages: {}\n")
    with pytest.raises(ConfigError, match="non-empty 'environment' list"):
        parse_config(str(manifest))


def test_parse_config_missing_images_raises_configerror(tmp_path: Path) -> None:
    """A manifest without an ``images`` section raises ConfigError.

    ConfigError is an LvlabError so a single boundary ``except LvlabError``
    catches it.
    """
    manifest = tmp_path / "Lvlab.yml"
    manifest.write_text("environment:\n  - name: demo\n")
    with pytest.raises(ConfigError, match="missing the 'images' section"):
        parse_config(str(manifest))
    assert issubclass(ConfigError, LvlabError)


def test_parse_config_bad_yaml_raises(tmp_path: Path) -> None:
    """Malformed YAML must surface as a yaml.YAMLError, not silently empty out."""
    bad = tmp_path / "Lvlab.yml"
    bad.write_text("environment:\n  - name: demo\n    : this is invalid yaml\n")
    with pytest.raises(yaml.YAMLError):
        parse_config(str(bad))


def test_parse_config_missing_config_defaults_yields_empty_dict(tmp_path: Path) -> None:
    """A manifest with no ``config_defaults`` key still parses; defaults={}.

    Some lvscripts-style minimal manifests omit ``config_defaults``. The
    cli.py merge-into-machine logic depends on this returning ``{}``,
    not ``None``.
    """
    minimal = """---
environment:
  - name: demo
    libvirt_uri: qemu:///session
    machines: []
images: {}
"""
    manifest = tmp_path / "Lvlab.yml"
    manifest.write_text(minimal)

    env, images, defaults, machines = parse_config(str(manifest))

    assert env["name"] == "demo"
    assert defaults == {}
    assert machines == []
    assert images == {}


def test_parse_config_picks_first_environment(tmp_path: Path) -> None:
    """The ``environment`` key is a list; parse_config takes [0].

    Lock the contract: even if a manifest somehow has multiple
    environments, only the first one is returned. (Today the schema
    requires exactly one; this guards against silently picking the
    wrong index if that ever changes.)
    """
    multi = """---
environment:
  - name: first
    libvirt_uri: qemu:///session
  - name: second
    libvirt_uri: qemu:///system
images: {}
"""
    manifest = tmp_path / "Lvlab.yml"
    manifest.write_text(multi)

    env, _, _, _ = parse_config(str(manifest))
    assert env["name"] == "first"


def test_parse_file_from_url_strips_query_string() -> None:
    """URL with ``?foo=bar`` returns just the basename of the path."""
    url = "https://example.invalid/images/foo.qcow2?token=abc&v=1"
    assert parse_file_from_url(url) == "foo.qcow2"


def test_parse_file_from_url_strips_fragment() -> None:
    """URL with ``#frag`` returns just the basename of the path."""
    url = "https://example.invalid/images/bar.qcow2#anchor"
    assert parse_file_from_url(url) == "bar.qcow2"


def test_parse_file_from_url_no_path_returns_empty() -> None:
    """A URL with only host and no path returns ``''``."""
    assert parse_file_from_url("https://example.invalid") == ""


def test_parse_file_from_url_handles_debian_sha512sums() -> None:
    """A URL ending in ``SHA512SUMS`` (no extension) returns that exact basename.

    Why this is interesting: it's the name-collision case that drives the
    Debian-specific checksum-filename prefix in CloudImage.__init__.
    """
    url = "https://cloud.debian.org/images/cloud/bookworm/20240717-1811/SHA512SUMS"
    assert parse_file_from_url(url) == "SHA512SUMS"
