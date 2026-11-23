# Internal plan contract (v1)

This is the shared boundary between the planner, encoder and verifier. The
plan is stored as JSON in `jobs.plan_json`. All indexes are absolute input
stream indexes as reported by ffprobe. Paths in the plan are absolute.

```json
{
  "schema": 2,
  "preset": "1080p-balanced-mp4",
  "action": "encode",
  "container": "mp4",
  "plan_version": 1,
  "plan_schema": 2,
  "config_generation": 7,
  "preset_fingerprint": "8c4ad0e2b19f6a3d",
  "fallback_from": null,
  "source": {
    "path": "/srv/media/_pipeline/convert/movies/.../movie.mkv",
    "format": "matroska,webm",
    "duration": 123.45,
    "size_bytes": 1000000,
    "video_bytes": 900000,
    "main_video_index": 0,
    "mtime": 123.0
  },
  "video": {
    "index": 0,
    "codec": "h264",
    "source_codec": "h264",
    "width": 1920,
    "height": 1080,
    "target_width": 1280,
    "target_height": 720,
    "pix_fmt": "yuv420p",
    "bit_depth": 8,
    "sar": "1:1",
    "frame_rate": "24000/1001",
    "field_order": "progressive",
    "hdr": false,
    "dolby_vision": false,
    "color": {
      "primaries": "bt709",
      "transfer": "bt709",
      "space": "bt709",
      "range": "tv"
    },
    "backend": "qsv",
    "encoder": "hevc_qsv",
    "quality": 24,
    "look_ahead": true,
    "tag": "hvc1",
    "scaled": true
  },
  "audio": [
    {
      "index": 1,
      "source_codec": "dts",
      "language": "eng",
      "channels": 6,
      "channel_layout": "5.1",
      "action": "transcode",
      "codec": "eac3",
      "bitrate": 448,
      "default": true
    }
  ],
  "subtitles": [
    {
      "index": 3,
      "source_codec": "subrip",
      "language": "eng",
      "forced": false,
      "action": "mov_text",
      "sidecar": null
    }
  ],
  "sidecars": [
    {"path": "/abs/movie.en.srt", "language": "en", "action": "copy"}
  ],
  "chapters": true,
  "attachments": false,
  "name": {"title": "Title", "year": 2001, "tmdbid": null, "stem": "Title (2001)"},
  "ext": "mp4",
  "destination_rel": "Title (2001)/Title (2001).mp4",
  "dry_run": false,
  "warnings": [],
  "dropped": [],
  "estimated_seconds": 120.0,
  "speed_source": "table",
  "review": null
}
```

Rules:
- `schema`/`plan_schema` 2 is the current semantic generation. Plans without
  it were written before the stated-colour contract and are re-planned before
  execution, never silently interpreted under the new meaning.
- `config_generation` and `preset_fingerprint` bind plan-defining values to
  the generation and preset that produced them. Safety-critical settings are
  not bound: current values apply to resumed jobs. A changed generation or
  preset triggers a deliberate re-plan at PLANNED without consuming a retry.
- `video.color` carries only stated values; a missing key means “not stated”,
  not “unknown but required”. `audio[].channel_layout` and `target_channels`
  were absent before Pass A and are not silently invented when re-reading an
  old plan.
- `action=remux` means video stream copy. It can be selected by SIZE-1
  (already efficient) or be the preset's action.
- A SIZE-2 fallback remux is rebuilt from `video.source_codec` and the source
  geometry: `video.codec` is the copied source codec, `target_width/height`
  equal `width/height`, `scaled=false`, and encode-only fields (`quality`,
  `encoder`, `backend`, `look_ahead`) are absent. Audio and subtitle
  selections are reused unchanged.
- `container=mkv` implies MKV remux rules; audio/subtitle streams are
  selected but copied.
- For MP4 HEVC, `video.tag` is `hvc1`.
- `video.target_width/height` are always explicit even numbers.
- `video.color` carries only the colour properties the source **states**.
  A property the source does not state is `null`; mediaflow never invents a
  colour tag. ffprobe infers `color_range=tv` from an ordinary 8-bit 4:2:0
  pixel format, so an inferred range is not a stated range and stays
  `null` unless the source states full range (`pc`) or states a colour
  matrix alongside it.
- `audio[].channel_layout` is the source layout when ffprobe reports a
  usable one (`5.1`, `stereo`, ...); it is absent when the layout is
  unknown. `audio[].target_channels` is present when the output channel
  count differs from the source (a downmix to 8 channels).
- Review decisions are represented by a `ReviewRequired` exception while
  planning, not by a published plan. A persisted plan always has `review:
  null`; the engine records the review reason and origin state.
- `dropped` records subtitle/attachment decisions for the manifest.
- Sidecars are copied under the final stem with a normalised language tag.
- No ffmpeg argument may use `-map 0`; the encoder maps each explicit
  `0:<index>`.
