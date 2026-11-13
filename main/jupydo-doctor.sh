#!/usr/bin/env bash
# jupydo-doctor.sh - read-only health check of the disks JupyDo depends on.
#
# Checks, for the hub state folder, the users' data folder and Docker's folder:
# free space and inodes OF THE DISK THAT REALLY HOLDS THEM (on a mergerfs pool the
# total can look fine while the branch holding a folder is full), a real write
# test in the folder itself, the mergerfs create policy and duplicated mounts.
# Usage (from main/, as root): sudo bash jupydo-doctor.sh
set -u
cd "$(dirname "$0")"
if [ -f .env ]; then
  set -a; . ./.env; set +a
fi
WARN_PCT="${JUPYDO_DOCTOR_WARN_PCT:-90}"
PROBLEMS=0

warn() { echo "  WARNING: $*"; PROBLEMS=1; }
fail() { echo "  ERROR:   $*"; PROBLEMS=1; }

usage_line() {  # $1 = path; prints use% of space and inodes
  local space inodes
  space="$(df -P "$1" 2>/dev/null | awk 'NR==2 {gsub("%","",$5); print $5" "$4}')"
  inodes="$(df -Pi "$1" 2>/dev/null | awk 'NR==2 {gsub("%","",$5); print $5}')"
  echo "$space ${inodes:-0}"
}

check_disk() {  # $1 = label, $2 = path
  local label="$1" path="$2" use avail ipct
  read -r use avail ipct <<< "$(usage_line "$path")"
  printf "  %-34s space %3s%% used (%s GB free), inodes %3s%% used\n" "$label" "$use" "$((avail / 1024 / 1024))" "$ipct"
  [ "${use:-0}" -ge "$WARN_PCT" ] && warn "$label: disk ${use}% full"
  [ "${ipct:-0}" -ge "$WARN_PCT" ] && warn "$label: inodes ${ipct}% used"
}

check_folder() {  # $1 = label, $2 = path
  local label="$1" path="$2" fstype target opts
  echo
  echo "== $label: $path"
  if [ ! -d "$path" ]; then
    fail "folder does not exist"
    return
  fi
  # Real write test in the folder itself (not elsewhere on the same pool)
  if ( echo ok > "$path/.jupydo_doctor_write_test" && sync -f "$path/.jupydo_doctor_write_test" ) 2>/dev/null; then
    rm -f "$path/.jupydo_doctor_write_test"
    echo "  write test: OK"
  else
    rm -f "$path/.jupydo_doctor_write_test" 2>/dev/null
    fail "cannot write in this folder (disk full on its branch? permissions?)"
  fi

  fstype="$(findmnt -no FSTYPE --target "$path")"
  target="$(findmnt -no TARGET --target "$path")"
  check_disk "filesystem ($fstype on $target)" "$path"

  if [ "$fstype" = "fuse.mergerfs" ]; then
    opts="$(findmnt -no OPTIONS --target "$path")"
    echo "  mergerfs create policy: $(echo "$opts" | tr ',' '\n' | grep -E '^(category\.create|func\.create)=' || echo 'default (epmfs)')"
    local rel="${path#"$target"}" branches branch
    # Branch list: from the mount source, or (when fsname= hides it) from mergerfs' control file
    branches="$(findmnt -no SOURCE --target "$path" | tr ':' ' ')"
    if ! for b in $branches; do [ -d "${b%%=*}" ] && exit 0; done; then
      branches="$(getfattr -n user.mergerfs.branches --only-values "$target/.mergerfs" 2>/dev/null \
                  || getfattr -n user.mergerfs.srcmounts --only-values "$target/.mergerfs" 2>/dev/null)"
      branches="$(echo "$branches" | tr ':' ' ')"
      [ -z "$branches" ] && warn "cannot list the mergerfs branches (install the 'attr' package for getfattr)"
    fi
    for branch in $branches; do
      branch="${branch%%=*}"  # strip mode suffix like /data01=RW
      [ -d "$branch" ] || continue
      if [ -e "$branch$rel" ]; then
        check_disk "branch $branch (holds this folder)" "$branch"
      else
        check_disk "branch $branch" "$branch"
      fi
    done
    echo "  note: with an 'existing path' policy (ep*), new files go only to branches that"
    echo "        already hold the folder: watch the branches marked 'holds this folder'."
  fi
}

echo "JupyDo disk check (warning above ${WARN_PCT}%)"
[ -n "${JUPYDO_DATA_PATH:-}" ] && check_folder "Hub state (database, cookie secret)" "$JUPYDO_DATA_PATH"
[ -n "${JUPYDO_PATH:-}" ] && check_folder "Users' shared data and builder" "$JUPYDO_PATH"
DOCKER_ROOT="$(docker info -f '{{.DockerRootDir}}' 2>/dev/null || echo /var/lib/docker)"
check_folder "Docker (images, users' work volumes)" "$DOCKER_ROOT"

# The same mergerfs pool mounted more than once on the same folder
echo
DUPES="$(awk '$3 == "fuse.mergerfs" {print $2}' /proc/mounts | sort | uniq -d)"
if [ -n "$DUPES" ]; then
  for m in $DUPES; do
    warn "mergerfs is mounted $(awk -v m="$m" '$2 == m && $3 == "fuse.mergerfs"' /proc/mounts | wc -l) times on $m (overlapping mounts): check fstab/systemd units"
  done
else
  echo "mergerfs mounts: no duplicates"
fi

echo
if [ "$PROBLEMS" -eq 0 ]; then
  echo "All checks passed."
else
  echo "Some checks need attention (see WARNING/ERROR above)."
fi
exit "$PROBLEMS"
