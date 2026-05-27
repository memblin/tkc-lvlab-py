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

from tkc_lvlab.config import (
    ConfigManager,
    HostConfig,
    NetworkDefaults,
    deep_merge,
    load_host_config,
    parse_config,
    parse_file_from_url,
    parse_networks,
    parse_runcmd,
)
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


# ---------------------------------------------------------------------------
# ConfigManager (#49)
# ---------------------------------------------------------------------------


def test_config_manager_exposes_four_sections(tmp_path: Path) -> None:
    """A valid manifest is exposed via the four section properties.

    The manager wraps ``parse_config`` and surfaces the same data a command
    would otherwise unpack from the 4-tuple — environment, images,
    config_defaults, machines — without the caller re-reading the file.
    """
    manifest = tmp_path / "Lvlab.yml"
    manifest.write_text(SAMPLE_MANIFEST)

    config = ConfigManager(str(manifest))

    assert config.loaded is True
    assert config.environment["name"] == "demo"
    assert "fedora40" in config.images
    assert config.config_defaults == {"domain": "local", "cpu": 2}
    assert [m["vm_name"] for m in config.machines] == ["alpha", "beta"]
    # as_tuple() reproduces the legacy parse_config shape exactly.
    assert config.as_tuple() == parse_config(str(manifest))


def test_config_manager_get_machine_finds_by_vm_name(tmp_path: Path) -> None:
    """``get_machine`` returns the matching machine dict, or None when absent."""
    manifest = tmp_path / "Lvlab.yml"
    manifest.write_text(SAMPLE_MANIFEST)

    config = ConfigManager(str(manifest))

    assert config.get_machine("beta")["hostname"] == "beta"
    assert config.get_machine("does-not-exist") is None


def test_config_manager_missing_file_is_soft_path(tmp_path: Path) -> None:
    """A missing manifest is the soft path: loaded=False, empty sections.

    This is the DISTINCT, non-error outcome (vs. a structural ConfigError):
    constructing the manager does not raise, and every section is empty so a
    caller can decide whether to refuse (``.loaded``) or proceed.
    """
    missing = tmp_path / "Definitely-Not-Here.yml"

    config = ConfigManager(str(missing))

    assert config.loaded is False
    assert config.environment == {}
    assert config.images == {}
    assert config.config_defaults == {}
    assert config.machines == []
    assert config.get_machine("anything") is None


def test_config_manager_bad_structure_raises_configerror(tmp_path: Path) -> None:
    """A structurally invalid manifest raises ConfigError from the constructor.

    Distinct from the missing-file soft path above: a present-but-broken
    manifest is an error, not an empty manager.
    """
    manifest = tmp_path / "Lvlab.yml"
    manifest.write_text("- just\n- a\n- list\n")
    with pytest.raises(ConfigError, match="not a mapping"):
        ConfigManager(str(manifest))


def test_config_manager_from_parsed_wraps_without_rereading(tmp_path: Path) -> None:
    """``from_parsed`` wraps an already-parsed tuple without touching disk.

    This is the seam the CLI uses: it calls ``parse_config`` once and hands
    the result here, so no second read happens. A ``None`` (missing-file)
    tuple becomes the same soft, loaded=False manager.
    """
    parsed = ({"name": "demo"}, {"img": {}}, {"cpu": 2}, [{"vm_name": "alpha"}])
    config = ConfigManager.from_parsed(parsed)
    assert config.loaded is True
    assert config.environment == {"name": "demo"}
    assert config.get_machine("alpha") == {"vm_name": "alpha"}

    soft = ConfigManager.from_parsed(None)
    assert soft.loaded is False
    assert soft.machines == []


# ---------------------------------------------------------------------------
# Layered host config + networks (#138)
# ---------------------------------------------------------------------------


def _layered_dirs(tmp_path: Path) -> tuple[Path, Path, Path]:
    """Return ``(system_dir, home_dir, cwd)`` test seams (each an empty dir).

    All three are passed to :func:`load_host_config` so the tests never touch
    the real ``/etc`` or the developer's own ``~/.Lvlab.yml``.
    """
    system_dir = tmp_path / "etc"
    home_dir = tmp_path / "home"
    cwd = tmp_path / "cwd"
    system_dir.mkdir()
    home_dir.mkdir()
    cwd.mkdir()
    return system_dir, home_dir, cwd


def test_deep_merge_recurses_dicts_and_replaces_scalars() -> None:
    """Nested mappings merge; scalars/lists from the overlay replace wholesale."""
    base = {"a": {"x": 1, "y": 2}, "b": [1, 2], "c": "base"}
    overlay = {"a": {"y": 99, "z": 3}, "b": [9], "d": "new"}
    merged = deep_merge(base, overlay)

    assert merged == {
        "a": {"x": 1, "y": 99, "z": 3},  # nested dict merged, y overridden
        "b": [9],  # list replaced wholesale, not concatenated
        "c": "base",  # untouched key survives
        "d": "new",  # overlay-only key added
    }
    # Inputs are not mutated.
    assert base["a"] == {"x": 1, "y": 2}


def test_parse_networks_normalizes_scalar_and_list() -> None:
    """``dns``/``search`` accept a scalar or a list; gateway is a scalar."""
    networks = parse_networks(
        {
            "vlan10": {
                "gateway": "100.64.10.1",
                "dns": "100.64.10.10",  # scalar -> single-element list
                "search": ["a.example", "b.example"],
            },
            "vlan20": None,  # null entry -> empty defaults placeholder
        }
    )
    assert networks["vlan10"] == NetworkDefaults(
        gateway="100.64.10.1",
        dns=["100.64.10.10"],
        search=["a.example", "b.example"],
    )
    assert networks["vlan20"] == NetworkDefaults()


def test_parse_networks_rejects_non_mapping() -> None:
    """A ``networks:`` value that isn't a mapping is a clean ValueError."""
    with pytest.raises(ValueError, match="'networks' section must be a mapping"):
        parse_networks(["vlan10", "vlan20"])


def test_parse_networks_rejects_bad_dns_type() -> None:
    """A ``dns`` value that's neither string nor list-of-strings errors."""
    with pytest.raises(ValueError, match="networks.vlan10.dns"):
        parse_networks({"vlan10": {"dns": {"not": "a list"}}})


def test_load_host_config_no_files_is_empty(tmp_path: Path) -> None:
    """With neither /etc nor CWD config present, every section is empty."""
    system_dir, home_dir, cwd = _layered_dirs(tmp_path)
    config = load_host_config(system_dir=system_dir, home_dir=home_dir, cwd=cwd)
    assert config == HostConfig()
    assert config.images == {}
    assert config.networks == {}
    assert config.default_network is None


def test_load_host_config_layers_cwd_over_etc(tmp_path: Path) -> None:
    """A CWD ``Lvlab.yml`` wins per key over the host-wide ``/etc`` base."""
    system_dir, home_dir, cwd = _layered_dirs(tmp_path)
    (system_dir / "Lvlab.yml").write_text(
        "default_network: vlan10\n"
        "networks:\n"
        "  vlan10:\n"
        "    gateway: 100.64.10.1\n"
        "    dns: [100.64.10.10]\n"
    )
    (cwd / "Lvlab.yml").write_text("default_network: vlan20\n")

    config = load_host_config(system_dir=system_dir, home_dir=home_dir, cwd=cwd)

    # CWD overrode the scalar default_network ...
    assert config.default_network == "vlan20"
    # ... while the /etc-only networks map survives (nothing overrode it).
    assert config.networks["vlan10"].gateway == "100.64.10.1"


def test_load_host_config_user_layer_between_etc_and_cwd(tmp_path: Path) -> None:
    """``~/.Lvlab.yml`` overrides ``/etc`` but is overridden by the CWD project.

    Precedence (lowest first): /etc -> ~/.Lvlab.yml -> ./Lvlab.yml. Each key is
    resolved to the highest layer that sets it.
    """
    system_dir, home_dir, cwd = _layered_dirs(tmp_path)
    (system_dir / "Lvlab.yml").write_text(
        "default_network: etc-net\ndefault_vm_username: etcuser\n"
    )
    # Per-user dotfile: overrides /etc, and adds a key /etc didn't set.
    (home_dir / ".Lvlab.yml").write_text(
        "default_network: user-net\nnetworks:\n  vlan10:\n    gateway: 10.0.0.1\n"
    )
    (cwd / "Lvlab.yml").write_text("default_network: cwd-net\n")

    config = load_host_config(system_dir=system_dir, home_dir=home_dir, cwd=cwd)

    # CWD wins the contested key ...
    assert config.default_network == "cwd-net"
    # ... while the user dotfile's networks entry (uncontested) survives.
    assert config.networks["vlan10"].gateway == "10.0.0.1"


def test_load_host_config_deep_merges_one_network_field(tmp_path: Path) -> None:
    """A CWD layer can override a single nested network field, inheriting siblings."""
    system_dir, home_dir, cwd = _layered_dirs(tmp_path)
    (system_dir / "Lvlab.yml").write_text(
        "networks:\n"
        "  vlan10:\n"
        "    gateway: 100.64.10.1\n"
        "    dns: [100.64.10.10]\n"
        "    search: [tkclabs.io]\n"
    )
    (cwd / "Lvlab.yml").write_text(
        "networks:\n  vlan10:\n    dns: [10.0.0.53]\n"  # override only dns
    )

    config = load_host_config(system_dir=system_dir, home_dir=home_dir, cwd=cwd)
    vlan10 = config.networks["vlan10"]

    assert vlan10.dns == ["10.0.0.53"]  # CWD wins on dns
    assert vlan10.gateway == "100.64.10.1"  # /etc gateway inherited
    assert vlan10.search == ["tkclabs.io"]  # /etc search inherited


def test_load_host_config_explicit_config_wins(tmp_path: Path) -> None:
    """An explicit ``--config`` layers over both CWD and /etc (highest precedence)."""
    system_dir, home_dir, cwd = _layered_dirs(tmp_path)
    (system_dir / "Lvlab.yml").write_text("default_network: vlan10\n")
    (cwd / "Lvlab.yml").write_text("default_network: vlan20\n")
    explicit = tmp_path / "special.yml"
    explicit.write_text("default_network: vlan99\n")

    config = load_host_config(
        str(explicit), system_dir=system_dir, home_dir=home_dir, cwd=cwd
    )
    assert config.default_network == "vlan99"


def test_load_host_config_explicit_missing_raises(tmp_path: Path) -> None:
    """An explicit ``--config`` that doesn't exist is an error, not a silent skip."""
    system_dir, home_dir, cwd = _layered_dirs(tmp_path)
    with pytest.raises(ValueError, match="does not exist"):
        load_host_config(
            str(tmp_path / "nope.yml"),
            system_dir=system_dir,
            home_dir=home_dir,
            cwd=cwd,
        )


def test_load_host_config_merges_images_by_key(tmp_path: Path) -> None:
    """``images:`` maps merge by key across layers; the CWD entry wins on a clash."""
    system_dir, home_dir, cwd = _layered_dirs(tmp_path)
    (system_dir / "Lvlab.yml").write_text(
        "images:\n"
        "  hostimg:\n"
        "    image_url: http://etc/hostimg.qcow2\n"
        "  shared:\n"
        "    image_url: http://etc/shared.qcow2\n"
    )
    (cwd / "Lvlab.yml").write_text(
        "images:\n"
        "  projimg:\n"
        "    image_url: http://cwd/projimg.qcow2\n"
        "  shared:\n"
        "    image_url: http://cwd/shared.qcow2\n"
    )

    config = load_host_config(system_dir=system_dir, home_dir=home_dir, cwd=cwd)

    assert config.images["hostimg"]["image_url"] == "http://etc/hostimg.qcow2"
    assert config.images["projimg"]["image_url"] == "http://cwd/projimg.qcow2"
    # CWD wins on the colliding key.
    assert config.images["shared"]["image_url"] == "http://cwd/shared.qcow2"


def test_load_host_config_rejects_non_mapping_file(tmp_path: Path) -> None:
    """A config layer that isn't a YAML mapping is a clean ValueError."""
    system_dir, home_dir, cwd = _layered_dirs(tmp_path)
    (cwd / "Lvlab.yml").write_text("- just\n- a\n- list\n")
    with pytest.raises(ValueError, match="must contain a YAML mapping"):
        load_host_config(system_dir=system_dir, home_dir=home_dir, cwd=cwd)


def test_load_host_config_default_vm_username_layers(tmp_path: Path) -> None:
    """``default_vm_username`` parses and a higher layer overrides a lower one."""
    system_dir, home_dir, cwd = _layered_dirs(tmp_path)
    (system_dir / "Lvlab.yml").write_text("default_vm_username: etcadmin\n")
    (home_dir / ".Lvlab.yml").write_text("default_vm_username: meadmin\n")

    # With only /etc + user, the user dotfile wins.
    config = load_host_config(system_dir=system_dir, home_dir=home_dir, cwd=cwd)
    assert config.default_vm_username == "meadmin"

    # A project file wins over both.
    (cwd / "Lvlab.yml").write_text("default_vm_username: projadmin\n")
    config = load_host_config(system_dir=system_dir, home_dir=home_dir, cwd=cwd)
    assert config.default_vm_username == "projadmin"


def test_load_host_config_rejects_empty_default_vm_username(tmp_path: Path) -> None:
    """A blank/non-string ``default_vm_username`` is a clean ValueError."""
    system_dir, home_dir, cwd = _layered_dirs(tmp_path)
    (cwd / "Lvlab.yml").write_text('default_vm_username: "   "\n')
    with pytest.raises(ValueError, match="default_vm_username"):
        load_host_config(system_dir=system_dir, home_dir=home_dir, cwd=cwd)


def test_parse_runcmd_accepts_string_list_rejects_others() -> None:
    """``runcmd`` is a list of command strings; anything else is a ValueError."""
    assert parse_runcmd(None) == []
    assert parse_runcmd(["echo hi", "touch /tmp/x"]) == ["echo hi", "touch /tmp/x"]
    with pytest.raises(ValueError, match="'runcmd' value must be a list"):
        parse_runcmd("echo not-a-list")
    with pytest.raises(ValueError, match="'runcmd' value must be a list"):
        parse_runcmd([{"cmd": "echo hi"}])


def test_load_host_config_runcmd_higher_layer_replaces(tmp_path: Path) -> None:
    """A higher layer's ``runcmd`` replaces a lower one wholesale (no concat).

    Deliberate: identical host-wide commands in /etc and ~ must not run twice.
    """
    system_dir, home_dir, cwd = _layered_dirs(tmp_path)
    (system_dir / "Lvlab.yml").write_text(
        "runcmd:\n  - echo from-etc\n  - touch /tmp/etc\n"
    )
    (cwd / "Lvlab.yml").write_text("runcmd:\n  - echo from-cwd\n")

    config = load_host_config(system_dir=system_dir, home_dir=home_dir, cwd=cwd)
    assert config.runcmd == ["echo from-cwd"]  # CWD replaced /etc, not appended


def test_load_host_config_rejects_bad_runcmd(tmp_path: Path) -> None:
    """A non-list ``runcmd`` in a layer is a clean ValueError."""
    system_dir, home_dir, cwd = _layered_dirs(tmp_path)
    (cwd / "Lvlab.yml").write_text("runcmd: echo not-a-list\n")
    with pytest.raises(ValueError, match="'runcmd' value must be a list"):
        load_host_config(system_dir=system_dir, home_dir=home_dir, cwd=cwd)
