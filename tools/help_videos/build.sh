#!/usr/bin/env bash
# Compress the recorded videos into the portal's help folder: VP9 at 12 fps, a poster
# frame and the Dutch and English captions per manual. Pass ids to build only those
# (build.sh plan-builder coach); without ids every recording in tour/out is built.
set -euo pipefail
OUT="${VS_HELP_HOME:-$HOME/vs-help-home}/tour/out"
DEST="$(cd "$(dirname "$0")/../.." && pwd)/src/vibesynergy/portal/static/help"
mkdir -p "$DEST"
if [ $# -gt 0 ]; then ids=("$@"); else ids=(); for raw in "$OUT"/*.webm; do ids+=("$(basename "$raw" .webm)"); done; fi
for id in "${ids[@]}"; do
  raw="$OUT/$id.webm"
  ffmpeg -loglevel error -y -i "$raw" -c:v libvpx-vp9 -crf 42 -b:v 0 -r 12 -row-mt 1 -deadline good -cpu-used 4 -an "$DEST/$id.webm"
  dur=$(ffprobe -v error -show_entries format=duration -of csv=p=0 "$DEST/$id.webm")
  ffmpeg -loglevel error -y -ss "$(python3 -c "print(float('$dur') * 0.35)")" -i "$DEST/$id.webm" -frames:v 1 -vf scale=640:-2 -q:v 5 "$DEST/$id.jpg"
  cp "$OUT/$id.nl.vtt" "$OUT/$id.en.vtt" "$DEST/"
  echo "$id"
done
