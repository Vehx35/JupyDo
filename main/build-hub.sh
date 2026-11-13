#!/usr/bin/env bash
# Builds the JupyDo hub image from the vendored files only (no internet): the base image
# and the packages in <vendor>/hub (jupydo-builder's vendor/hub, made by
# scripts/make-vendor-hub.sh and downloaded from Zenodo by first_start.sh).
# The build runs without network, so nothing can be downloaded by mistake.
# Usage: ./build-hub.sh [VENDOR_DIR]     default VENDOR_DIR: $JUPYDO_PATH/builder/vendor
# The image is tagged as compose.yaml expects it (ghcr.io/vehx35/jupydo-hub:${JUPYDO_HUB_TAG:-latest}).
set -euo pipefail
cd "$(dirname "$0")"

VENDOR="${1:-${JUPYDO_PATH:-/srv/JupyDo}/builder/vendor}/hub"
IMAGE="ghcr.io/vehx35/jupydo-hub:${JUPYDO_HUB_TAG:-latest}"
case "$(uname -m)" in
    x86_64|amd64) ARCH=amd64 ;;
    aarch64|arm64) ARCH=arm64 ;;
    *) echo "ERROR: unsupported architecture $(uname -m)" >&2; exit 1 ;;
esac

for d in "$VENDOR/common" "$VENDOR/$ARCH"; do
    [ -f "$d/.complete" ] || { echo "ERROR: $d is missing or incomplete (download the builder from Zenodo first)" >&2; exit 1; }
    (cd "$d" && sha256sum --quiet -c SHA256SUMS) || { echo "ERROR: $d is damaged (checksums)" >&2; exit 1; }
done

echo "Loading the base image..."
docker load -q -i "$VENDOR/$ARCH/base-image.tar.gz"

rm -rf hub-vendor && mkdir -p hub-vendor
trap 'rm -rf hub-vendor' EXIT
cp -r "$VENDOR/$ARCH/wheels" "$VENDOR/$ARCH/debs" "$VENDOR/$ARCH/requirements.txt" "$VENDOR/$ARCH/node.tar.xz" \
      "$VENDOR/$ARCH/configurable-http-proxy.tar.gz" hub-vendor/
cp "$VENDOR/common/genome-index.tar.gz" "$VENDOR/common/assistant.tar.gz" hub-vendor/
cp "$VENDOR/$ARCH/manifest.txt" hub-vendor/manifest-$ARCH.txt
cp "$VENDOR/common/manifest.txt" hub-vendor/manifest-common.txt

echo "Building $IMAGE ($ARCH) from the vendored files, without network..."
docker build --network=none -f Dockerfile.jupyterhub -t "$IMAGE" .
echo "Built: $IMAGE"
