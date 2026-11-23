#!/usr/bin/env bash
# Generate disposable synthetic verification clips. Never writes to /srv/media.
set -euo pipefail

OUT="${1:?usage: make_test_clips.sh OUTPUT_DIR}"
FFMPEG="${MEDIAFLOW_FFMPEG:-/usr/lib/jellyfin-ffmpeg/ffmpeg}"

if [[ "$OUT" == "/srv/media" || "$OUT" == /srv/media/* ]]; then
  echo "refusing to write synthetic media under /srv/media" >&2
  exit 2
fi
if ! command -v "$FFMPEG" >/dev/null 2>&1; then
  echo "SKIP: ffmpeg not available at $FFMPEG" >&2
  exit 3
fi

mkdir -p "$OUT"

"$FFMPEG" -hide_banner -loglevel error -y \
  -f lavfi -i "testsrc2=size=320x180:rate=25:duration=2" \
  -f lavfi -i "sine=frequency=440:duration=2" \
  -map 0:v -map 1:a -c:v libx264 -pix_fmt yuv420p \
  -color_primaries bt709 -color_trc bt709 -colorspace bt709 \
  -c:a aac -shortest "$OUT/sdr-h264.mkv"

"$FFMPEG" -hide_banner -loglevel error -y \
  -f lavfi -i "testsrc2=size=320x180:rate=25:duration=2" \
  -map 0:v -vf "select='not(mod(n,2))',setpts=N/(12.5*TB)" -vsync vfr \
  -c:v libx264 -pix_fmt yuv420p "$OUT/vfr-h264.mkv"

printf '1\n00:00:00,000 --> 00:00:01,000\nSynthetic subtitle\n\n' > "$OUT/synthetic.srt"

if "$FFMPEG" -hide_banner -encoders 2>/dev/null | grep -q ' libx265 '; then
  "$FFMPEG" -hide_banner -loglevel error -y \
    -f lavfi -i "testsrc2=size=320x180:rate=25:duration=2" \
    -map 0:v -c:v libx265 -pix_fmt yuv420p10le -preset ultrafast \
    "$OUT/hevc10.mkv"
else
  echo "NOTE: libx265 unavailable; hevc10.mkv not generated" >&2
fi

echo "synthetic clips written to $OUT"
