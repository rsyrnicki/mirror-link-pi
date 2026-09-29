#!/usr/bin/env bash
# Download the MirrorLink specification (ETSI TS 103 544, all parts) into ./spec/.
# The PDFs are free from ETSI but copyrighted, so they are not committed (see .gitignore).
#
# Usage: ./scripts/fetch-spec.sh [dest-dir]      (default: ./spec)
# If pdftotext (poppler-utils) is installed, a .txt is written next to every PDF.

set -euo pipefail

DEST="${1:-spec}"
BASE="https://www.etsi.org"
# ETSI rejects curl's default user agent.
UA="Mozilla/5.0 (X11; Linux x86_64; rv:128.0) Gecko/20100101 Firefox/128.0"

mkdir -p "$DEST"
for n in $(seq -w 1 30); do
    dir="$BASE/deliver/etsi_ts/103500_103599/103544$n/"
    ver=$(curl -fsS -A "$UA" --max-time 30 "$dir" 2>/dev/null \
          | grep -oE "103544${n}/[0-9._]+_60/" | sort -V | tail -1 || true)
    [[ -n "$ver" ]] || continue
    pdf=$(curl -fsS -A "$UA" --max-time 30 "$BASE/deliver/etsi_ts/103500_103599/$ver" \
          | grep -oE 'HREF="[^"]+\.pdf"' | head -1 | cut -d'"' -f2)
    out="$DEST/ts_103544-$n.pdf"
    curl -fsS -A "$UA" --max-time 180 -o "$out" "$BASE$pdf"
    title=""
    if command -v pdftotext >/dev/null; then
        pdftotext -layout "$out" "${out%.pdf}.txt"
        title=$(grep -m1 -oE 'Part [0-9]+: .*' "${out%.pdf}.txt" | tr -s ' ' || true)
    fi
    echo "part $n  ${ver#*/}  $title"
done
echo "done → $DEST/"
