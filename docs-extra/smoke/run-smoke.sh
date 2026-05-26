#!/usr/bin/env bash
# Manifest-path smoke test: exercises `lvlab up` -> SSH -> `lvlab down`
# -> `lvlab destroy` for every machine in this directory's Lvlab.yml,
# verifying SSH login as the distro's default user before tearing down.
#
# Each VM is an independent CASE (one static or one DHCP machine). Cases
# run through a flat worker pool of SMOKE_PARALLEL (default 4) concurrent
# VMs — sized for a 6+ vCPU / 16 GB host, where the worst-case four
# concurrent guests (~7 GB of minimized per-distro RAM; see Lvlab.yml)
# fit comfortably. On a smaller host, lower it: SMOKE_PARALLEL=2 ./run-smoke.sh
#
# This is a MANUAL validation helper (like scripts/run-validation.sh),
# never wired into CI — it boots real VMs on qemu:///system. Run it on a
# libvirt host whose `default` network DHCP range has been narrowed to
# free the static addresses below (see docs-extra/host-validation.md).
#
#   cd docs-extra/smoke && ./run-smoke.sh
#
# Keep the CASES table below in sync with Lvlab.yml when the default
# image set changes (the refresh-cloud-images skill updates the images:
# section; the machine list is maintained here + in Lvlab.yml). Concurrent
# cases are safe because every static IP is distinct (.190-.197) and each
# DHCP guest gets its own lease — no two cases contend for an address.
set -u

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENV="smoke"
URI="qemu:///system"
SSH_KEY="${SMOKE_SSH_KEY:-$HOME/.ssh/id_ed25519}"
SMOKE_PARALLEL="${SMOKE_PARALLEL:-4}"
SSHOPTS=(-i "$SSH_KEY" -o BatchMode=yes -o StrictHostKeyChecking=no \
         -o UserKnownHostsFile=/dev/null -o ConnectTimeout=8)

# Locate the lvlab binary: prefer an explicit LVLAB, else PATH, else the
# project .venv relative to this file.
LVLAB="${LVLAB:-$(command -v lvlab || true)}"
[ -z "$LVLAB" ] && LVLAB="$HERE/../../.venv/bin/lvlab"
if [ ! -x "$LVLAB" ]; then echo "lvlab not found (set LVLAB=...)"; exit 2; fi

# vm_name             mode    static_ip(or -)    ssh_user
CASES=(
  "deb12-static       static  192.168.122.190    debian"
  "deb12-dhcp         dhcp    -                  debian"
  "deb13-static       static  192.168.122.191    debian"
  "deb13-dhcp         dhcp    -                  debian"
  "deb11-static       static  192.168.122.194    debian"
  "deb11-dhcp         dhcp    -                  debian"
  "fed44-static       static  192.168.122.192    fedora"
  "fed44-dhcp         dhcp    -                  fedora"
  "alma10-static      static  192.168.122.193    almalinux"
  "alma10-dhcp        dhcp    -                  almalinux"
  "alma9-static       static  192.168.122.195    almalinux"
  "alma9-dhcp         dhcp    -                  almalinux"
  "ubuntu2204-static  static  192.168.122.196    ubuntu"
  "ubuntu2204-dhcp    dhcp    -                  ubuntu"
  "ubuntu2404-static  static  192.168.122.197    ubuntu"
  "ubuntu2404-dhcp    dhcp    -                  ubuntu"
)

lease_ip() { virsh -c "$URI" domifaddr "$1" --source lease 2>/dev/null \
  | awk '/ipv4/{print $4}' | cut -d/ -f1 | head -1; }

ssh_check() { # label user ip -> 0/1 (output goes to the per-case log)
  local out
  for _ in $(seq 1 30); do
    out=$(ssh "${SSHOPTS[@]}" "$2"@"$3" \
      'echo OK:$(hostname):$(id -un):$(ip -br addr | grep -vw lo | tr "\n" " ")' 2>/dev/null)
    [ -n "$out" ] && { echo "  PASS [$1 @ $3] $out"; return 0; }
    sleep 5
  done
  echo "  FAIL [$1 @ $3] no SSH in ~150s; last: $(ssh "${SSHOPTS[@]}" "$2"@"$3" true 2>&1 | tail -1)"
  return 1
}

# Full lifecycle for one VM, run in a background subshell. All chatter goes
# to a per-case log; a single status line is appended to stdout on finish
# (one-line writes interleave cleanly). PASS/FAIL is recorded in a .status
# file the aggregator reads.
run_case() { # vm mode static_ip user
  local vm="$1" mode="$2" sip="$3" user="$4"
  local dom="${vm}_${ENV}"
  local log="$RESULT_DIR/$vm.log"
  local status="PASS" ip=""

  { echo "== up $vm ($mode)"; "$LVLAB" up "$vm"; } >>"$log" 2>&1

  if [ "$mode" = "static" ]; then
    ip="$sip"
  else
    for _ in $(seq 1 30); do ip="$(lease_ip "$dom")"; [ -n "$ip" ] && break; sleep 5; done
    echo "-- dhcp lease $vm: ${ip:-<none>}" >>"$log"
  fi

  if [ -z "$ip" ]; then
    echo "  FAIL [$vm] no IP (dhcp lease never appeared)" >>"$log"; status="FAIL"
  elif ! ssh_check "$vm" "$user" "$ip" >>"$log" 2>&1; then
    status="FAIL"
  fi

  # Tear down regardless of verify outcome so nothing is left running.
  local st=""
  {
    "$LVLAB" down "$vm"
    for _ in $(seq 1 12); do
      st=$(virsh -c "$URI" domstate "$dom" 2>/dev/null)
      [ "$st" = "shut off" ] && break; sleep 5
    done
    echo "-- post-down $vm: ${st:-<gone>}"
    "$LVLAB" destroy "$vm" --force
    echo "-- destroyed $vm"
  } >>"$log" 2>&1

  echo "$status" >"$RESULT_DIR/$vm.status"
  printf '  %-20s %-7s %-16s %s\n' "$vm" "$mode" "${ip:-<none>}" "$status"
}

cd "$HERE" || exit 2
RESULT_DIR="$(mktemp -d)"
trap 'rm -rf "$RESULT_DIR"' EXIT

echo "=================================================================="
echo "SMOKE: ${#CASES[@]} cases, pool of $SMOKE_PARALLEL concurrent  (lvlab=$LVLAB)"
echo "       vm                   mode    ip               result"
echo "=================================================================="

running=0
for row in "${CASES[@]}"; do
  read -r vm mode sip user <<<"$row"
  run_case "$vm" "$mode" "$sip" "$user" &
  running=$((running + 1))
  if [ "$running" -ge "$SMOKE_PARALLEL" ]; then
    wait -n
    running=$((running - 1))
  fi
done
wait

overall=0
echo "=================================================================="
for row in "${CASES[@]}"; do
  read -r vm _ <<<"$row"
  st="$(cat "$RESULT_DIR/$vm.status" 2>/dev/null || echo MISSING)"
  if [ "$st" != "PASS" ]; then
    overall=1
    echo "---- $vm: $st — tail of log ----"
    tail -25 "$RESULT_DIR/$vm.log" 2>/dev/null
  fi
done

[ "$overall" -eq 0 ] && echo "SMOKE RESULT: ALL ${#CASES[@]} CASES PASSED" \
                      || echo "SMOKE RESULT: FAILURES (see logs above)"
exit "$overall"
