#!/usr/bin/env bash
# Bootstrap a host for lvlab development + integration testing.
#
# Supported targets (the matrix we explicitly validate against):
#   - Debian 12 (bookworm)
#   - Debian 13 (trixie)
#   - Ubuntu 24.04 (noble)
#   - AlmaLinux 10
#   - Fedora 44
#
# Out of scope: Debian 11 oldoldstable, Ubuntu 22.04 (Python 3.10 < our
# 3.11 floor), AlmaLinux 9 (same Python-floor reason), older Fedora.
#
# What this script does:
#   1. Verifies the host distro is on the supported list.
#   2. Installs host packages: libvirt clients, qemu, virt-install, git, curl.
#   3. Installs uv (https://astral.sh/uv) into the invoking user's
#      ~/.local/bin — does NOT touch system Python.
#   4. Adds the invoking user to the libvirt group so /var/lib/libvirt/
#      images is writable.
#   5. Creates /var/lib/libvirt/images/lvlab-test/ with ownership the
#      integration suite needs.
#   6. Prints the next steps (re-login, uv sync, pytest invocations).
#
# Re-running is safe; the script skips work that's already done.
#
# Usage:
#   scripts/host-bootstrap.sh
# Run as a normal user; the script will use sudo for package installs
# and ownership changes.

set -euo pipefail

err() { printf 'error: %s\n' "$*" >&2; exit 1; }
info() { printf '==> %s\n' "$*"; }

if [[ "${EUID}" -eq 0 ]]; then
    err "run this as a regular user, not root — the script uses sudo where needed and owns paths to your user"
fi

if [[ ! -r /etc/os-release ]]; then
    err "/etc/os-release missing — cannot identify distro"
fi

# shellcheck disable=SC1091
source /etc/os-release

case "${ID}:${VERSION_ID%%.*}" in
    debian:12 | debian:13)
        family=debian
        ;;
    ubuntu:24)
        # accept only 24.04 — refuse 24.10 et al
        if [[ "${VERSION_ID}" != "24.04" ]]; then
            err "Ubuntu ${VERSION_ID} is not a supported target — only 24.04 LTS"
        fi
        family=debian
        ;;
    almalinux:10)
        family=rhel
        ;;
    fedora:44)
        family=fedora
        ;;
    *)
        err "unsupported host: ${PRETTY_NAME} (ID=${ID} VERSION_ID=${VERSION_ID}). Supported: Debian 12/13, Ubuntu 24.04, AlmaLinux 10, Fedora 44."
        ;;
esac

info "host identified as ${PRETTY_NAME} — family=${family}"

case "${family}" in
    debian)
        info "installing host packages via apt"
        sudo apt-get update
        sudo apt-get install -y \
            libvirt-daemon-system \
            libvirt-clients \
            qemu-system-x86 \
            qemu-utils \
            virtinst \
            git \
            curl \
            ca-certificates
        ;;
    rhel | fedora)
        info "installing host packages via dnf"
        sudo dnf install -y \
            libvirt \
            libvirt-client \
            qemu-kvm \
            qemu-img \
            virt-install \
            git \
            curl
        sudo systemctl enable --now libvirtd
        ;;
esac

if ! command -v uv >/dev/null 2>&1; then
    info "installing uv into ~/.local/bin"
    curl -LsSf https://astral.sh/uv/install.sh | sh
else
    info "uv already installed at $(command -v uv) — skipping"
fi

# uv installer lands binaries at ~/.local/bin/uv. Surface that path so
# the user gets a clear next-step hint even if their shell init doesn't
# include it yet.
if [[ ! -x "${HOME}/.local/bin/uv" ]] && ! command -v uv >/dev/null 2>&1; then
    err "uv install appears to have failed — check the installer output above"
fi

if id -nG "${USER}" | tr ' ' '\n' | grep -qx libvirt; then
    info "user ${USER} already in libvirt group"
else
    info "adding ${USER} to libvirt group (effective after re-login or 'newgrp libvirt')"
    sudo usermod -aG libvirt "${USER}"
fi

test_root=/var/lib/libvirt/images/lvlab-test
if [[ -d "${test_root}" ]]; then
    info "${test_root} already present"
else
    info "creating ${test_root} owned by ${USER}:libvirt mode 0775"
    sudo install -d -o "${USER}" -g libvirt -m 0775 "${test_root}"
fi

cat <<EOF

==> Bootstrap complete.

Next steps (run as ${USER}, in a shell where libvirt group membership is effective):

  # If libvirt group was just added, re-login (or run: newgrp libvirt)
  # Then verify membership:
  id -nG | tr ' ' '\\n' | grep libvirt

  # Make uv visible if your shell init hasn't picked up ~/.local/bin yet:
  export PATH="\${HOME}/.local/bin:\${PATH}"

  # From a checkout of the repo:
  uv sync --group dev

  # Unit tests
  uv run pytest -q

  # Integration tests (creates real VMs under ${test_root})
  LVLAB_INTEGRATION=1 uv run pytest tests/test_integration_*.py -v

See docs-extra/host-validation.md for the full procedure and what to
record from the run.
EOF
