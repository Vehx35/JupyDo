#!/bin/bash
# JupyDo: registers the QEMU emulator JupyDo uses for the architecture this machine is
# not (arm64 images on an amd64 machine and vice versa): the static QEMU kept in
# /usr/local/lib/jupydo/qemu (put there by setup-host.sh / first_start.sh, from a pinned
# tonistiigi/binfmt image). Runs at every boot, after the system's own registrations
# (systemd-binfmt, binfmt-support), and replaces any QEMU already registered for that
# architecture: the distributions' QEMU crashes on some programs (QEMU 8.2 of Ubuntu
# 24.04: "QEMU internal SIGSEGV" in Python and in R compilations).
# Flags: F (the emulator works inside containers), C (setuid programs such as sudo
# work in emulated containers), P and O (as tonistiigi/binfmt).
set -u
DIR="${BINFMT_DIR:-/proc/sys/fs/binfmt_misc}"
QEMU_DIR="${QEMU_DIR:-/usr/local/lib/jupydo/qemu}"
[ -w "$DIR/register" ] || mount -t binfmt_misc binfmt_misc "$DIR" 2>/dev/null
[ -w "$DIR/register" ] || { echo "binfmt_misc not available: no emulation" >&2; exit 0; }

register() {   # register <name> <magic> <mask>
  local name="$1" interpreter="$QEMU_DIR/$1"
  [ -x "$interpreter" ] || { echo "${name}: ${interpreter} missing" >&2; return 0; }
  [ -e "$DIR/$name" ] && echo -1 > "$DIR/$name"   # the system's own registration, if any
  printf ':%s:M:0:%s:%s:%s:POCF\n' "$name" "$2" "$3" "$interpreter" > "$DIR/register"
  echo "${name}: ${interpreter} (flags POCF)"
}
case "$(uname -m)" in
  x86_64)  register qemu-aarch64 \
             '\x7f\x45\x4c\x46\x02\x01\x01\x00\x00\x00\x00\x00\x00\x00\x00\x00\x02\x00\xb7\x00' \
             '\xff\xff\xff\xff\xff\xff\xff\x00\xff\xff\xff\xff\xff\xff\xff\xff\xfe\xff\xff\xff' ;;
  aarch64) register qemu-x86_64 \
             '\x7f\x45\x4c\x46\x02\x01\x01\x00\x00\x00\x00\x00\x00\x00\x00\x00\x02\x00\x3e\x00' \
             '\xff\xff\xff\xff\xff\xfe\xfe\x00\xff\xff\xff\xff\xff\xff\xff\xff\xfe\xff\xff\xff' ;;
esac
