#!/bin/bash
# JupyDo: re-registers the QEMU emulators with the "C" (credentials) flag, so that
# setuid programs (sudo) work in emulated containers (arm64 images on an amd64
# server and vice versa). Runs at every boot, after the system registered the
# emulators (systemd-binfmt or binfmt-support): same registration, plus "C".
set -u
DIR="${BINFMT_DIR:-/proc/sys/fs/binfmt_misc}"
[ -w "$DIR/register" ] || exit 0
for entry in "$DIR"/qemu-*; do
  [ -f "$entry" ] || continue
  grep -q '^enabled' "$entry" || continue
  flags="$(sed -n 's/^flags: //p' "$entry")"
  case "$flags" in *C*) continue ;; esac
  name="$(basename "$entry")"
  interpreter="$(sed -n 's/^interpreter //p' "$entry")"
  offset="$(sed -n 's/^offset //p' "$entry")"
  magic="$(sed -n 's/^magic //p' "$entry" | sed 's/../\\x&/g')"
  mask="$(sed -n 's/^mask //p' "$entry" | sed 's/../\\x&/g')"
  echo -1 > "$entry"   # unregister, then register again with "C"
  printf ':%s:M:%s:%s:%s:%s:%sC\n' "$name" "$offset" "$magic" "$mask" "$interpreter" "$flags" > "$DIR/register"
  echo "${name}: flags ${flags} -> ${flags}C"
done
