#!/usr/bin/env bash
# jupydo-disk.sh - attach an extra disk (USB, external drive, any folder) to a
# user's shared folder. The user sees it at once as /sharedFolder/<name> in their
# running environments and jobs (mount propagation), without restarting them.
#
# Usage (from main/, as root):
#   sudo utils/jupydo-disk.sh attach [--ro] <user> <source folder> [name]
#   sudo utils/jupydo-disk.sh detach <user> <name>
#   sudo utils/jupydo-disk.sh list
#
# Example: sudo utils/jupydo-disk.sh attach mario /media/gianluca/07ca45c6-... hdd
#          -> mario sees the disk as /sharedFolder/hdd
# The attachment does not survive a reboot of the server: attach it again.
# Permissions of the disk are not changed: decide case by case (chown/chmod).
set -euo pipefail
cd "$(dirname "$0")/.."   # this script is in main/utils, .env is in main/
if [ ! -f .env ]; then
  echo "ERROR: $(pwd)/.env not found: this script must be in JupyDo's main/utils/ folder" >&2; exit 1
fi
set -a; . ./.env; set +a
SHARED="${JUPYDO_PATH:?JUPYDO_PATH missing in .env}/jh_shared"

die() { echo "ERROR: $*" >&2; exit 1; }
[ "$(id -u)" = "0" ] || die "run it with sudo"

user_dir() {
  local dir="$SHARED/${1}_shared"
  [ -d "$dir" ] || die "user '$1' has no shared folder ($dir): wrong name, or they never started an environment"
  echo "$dir"
}

check_name() {
  [[ "$1" =~ ^[A-Za-z0-9][A-Za-z0-9_.-]*$ ]] || die "name '$1': use letters, digits, '.', '_' or '-'"
}

# "device + path inside it" of a folder, to recognise the same disk mounted twice
identity() {
  local target fsroot rel
  target="$(findmnt -no TARGET --target "$1")"
  fsroot="$(findmnt -no FSROOT --target "$1")"
  rel="${1#"$target"}"
  echo "$(findmnt -no SOURCE --target "$1" | sed 's/\[.*//') ${fsroot%/}${rel}"
}

attached() {  # target<TAB>source<TAB>options of every disk attached under the shared folders
  findmnt -rn -o TARGET,SOURCE,OPTIONS | awk -v root="$SHARED/" 'index($1, root) == 1 {print $1 "\t" $2 "\t" $3}'
}

cmd="${1:-}"; shift || true
case "$cmd" in
  attach)
    ro=""
    if [ "${1:-}" = "--ro" ]; then ro=1; shift; fi
    [ $# -ge 2 ] || die "usage: attach [--ro] <user> <source folder> [name]"
    user="$1"; src="$(realpath -e "$2" 2>/dev/null)" || die "source '$2' does not exist"
    [ -d "$src" ] || die "source '$src' is not a folder"
    name="${3:-$(basename "$src")}"; check_name "$name"
    target="$(user_dir "$user")/$name"
    case "$src/" in "$SHARED"/*) die "the source is inside the JupyDo shared folders" ;; esac
    if mountpoint -q "$target"; then die "something is already attached at $target"; fi
    if [ -d "$target" ] && [ -n "$(ls -A "$target")" ]; then
      die "$target already contains files: choose another name"
    fi
    [ -e "$target" ] && [ ! -d "$target" ] && die "$target exists and is not a folder"
    want="$(identity "$src")"
    while IFS=$'\t' read -r t _ _; do
      [ -n "$t" ] || continue
      if [ "$(identity "$t")" = "$want" ]; then die "this disk is already attached at $t"; fi
    done < <(attached)
    mkdir -p "$target"
    mount --bind "$src" "$target"
    if [ -n "$ro" ]; then mount -o remount,bind,ro "$target"; fi
    echo "Attached: $src -> $user sees it as /sharedFolder/$name${ro:+ (read-only)}"
    ;;
  detach)
    [ $# -eq 2 ] || die "usage: detach <user> <name>"
    user="$1"; name="$2"; check_name "$name"
    target="$(user_dir "$user")/$name"
    mountpoint -q "$target" || die "nothing is attached at $target"
    if ! umount "$target" 2>/dev/null; then
      echo "ERROR: the disk is in use, it was NOT detached. Processes using it:" >&2
      if command -v fuser >/dev/null; then fuser -vm "$target" >&2 || true
      elif command -v lsof >/dev/null; then lsof +f -- "$target" >&2 || true
      else echo "  (install psmisc or lsof to see them)" >&2; fi
      echo "Ask the user to close them (or stop the environment), then retry." >&2
      exit 1
    fi
    rmdir "$target" 2>/dev/null || true
    echo "Detached: /sharedFolder/$name of $user"
    ;;
  list)
    printf "%-16s %-20s %-4s %s\n" USER "NAME (/sharedFolder/...)" MODE SOURCE
    attached | while IFS=$'\t' read -r t s o; do
      rel="${t#"$SHARED"/}"
      user="${rel%%_shared/*}"; name="${rel#*_shared/}"
      [ "$name" = "$rel" ] && continue          # the shared folder itself, not an extra disk
      [[ ",$o," == *",ro,"* ]] && mode=ro || mode=rw
      printf "%-16s %-20s %-4s %s\n" "$user" "$name" "$mode" "$s"
    done
    ;;
  *)
    sed -n '2,15p' "$(dirname "$0")/$(basename "$0")" 2>/dev/null | sed 's/^# \{0,1\}//'
    exit 1
    ;;
esac
