#!/usr/bin/env bash
# jupydo-migrate-work.sh - one-time move of the old "work" volumes into /sharedFolder.
#
# JupyDo no longer uses the per-user Docker volume jupyterhub-user-<user> (mounted
# at /home/jovyan/work): users' files live only in their shared folder
# (<JUPYDO_PATH>/jh_shared/<user>_shared, seen as /sharedFolder).
# For every old volume this script copies its content into
#     <user>_shared/old_work/
# keeping owners and permissions. The volumes are NOT deleted: check the copies,
# then remove them with:   docker volume rm jupyterhub-user-<user>
#
# Usage (from main/, as root, preferably with the users' environments stopped):
#     sudo bash jupydo-migrate-work.sh
set -euo pipefail
cd "$(dirname "$0")"
set -a; . ./.env; set +a
SHARED_ROOT="${JUPYDO_PATH:?JUPYDO_PATH missing in .env}/jh_shared"
HELPER_IMAGE="$(docker images --format '{{.Repository}}:{{.Tag}}' | grep -m1 '^jupydo-jupyterhub' || true)"
HELPER_IMAGE="${HELPER_IMAGE:-ubuntu:22.04}"

volumes="$(docker volume ls --format '{{.Name}}' | grep '^jupyterhub-user-' || true)"
if [ -z "$volumes" ]; then
  echo "No old work volumes: nothing to migrate."
  exit 0
fi

for vol in $volumes; do
  user="${vol#jupyterhub-user-}"
  target="$SHARED_ROOT/${user}_shared/old_work"
  if [ -e "$target" ]; then
    echo "SKIP $vol: $target already exists (already migrated?)"
    continue
  fi
  mkdir -p "$target"
  echo "Copying $vol -> $target ..."
  docker run --rm --entrypoint cp \
    -v "$vol":/from:ro -v "$target":/to \
    "$HELPER_IMAGE" -a /from/. /to/
  # empty mount points left by the old layout
  rmdir "$target/shared" "$target/genome_indexes" 2>/dev/null || true
  echo "  done: $(du -sh "$target" | cut -f1) copied"
done

echo
echo "Check the copies in each <user>_shared/old_work, then remove the old volumes:"
for vol in $volumes; do echo "    docker volume rm $vol"; done
