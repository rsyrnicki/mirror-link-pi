#!/usr/bin/env bash
# Download the scrcpy server that phone mode pushes to the phone (runs on the LAPTOP).
#
# The scrcpy client/server protocol changes between versions, and src/mlpi/phone.py
# speaks exactly SCRCPY_VERSION, so the version and its SHA-256 are pinned here. The
# checksum is the one published in the release's SHA256SUMS.txt. scrcpy is
# Apache-2.0 (https://github.com/Genymobile/scrcpy); the jar is not committed.
#
# Usage: scripts/fetch-scrcpy-server.sh [DEST]     (default: vendor/scrcpy-server)

set -euo pipefail

VERSION="4.1"
SHA256="deacb991ed2509715160ffdc7907e47b4160eb30d1566217e9047fd5b8850cae"
URL="https://github.com/Genymobile/scrcpy/releases/download/v${VERSION}/scrcpy-server-v${VERSION}"

REPO="$(cd "$(dirname "$0")/.." && pwd)"
DEST="${1:-$REPO/vendor/scrcpy-server}"

have() { [[ -f "$DEST" ]] && echo "$SHA256  $DEST" | sha256sum -c --status; }

if have; then
    echo "scrcpy server v$VERSION already present: $DEST"
    exit 0
fi
mkdir -p "$(dirname "$DEST")"
tmp="$(mktemp)"
trap 'rm -f "$tmp"' EXIT
echo "downloading scrcpy server v$VERSION"
curl -fsSL --retry 3 -o "$tmp" "$URL"
if ! echo "$SHA256  $tmp" | sha256sum -c --status; then
    echo "ERROR: checksum mismatch for $URL" >&2
    exit 1
fi
install -m 0644 "$tmp" "$DEST"
echo "scrcpy server v$VERSION → $DEST (checksum OK)"
