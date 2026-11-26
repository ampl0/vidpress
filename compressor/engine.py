"""Smart FFmpeg compression engine.

Picks codecs, quality/bitrate, scaling and audio strategy based on the probed
input and the user's intent, then builds and runs the ffmpeg command(s).
"""
from __future__ import annotations

import asyncio
import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path

from .probe import MediaInfo, ProbeError

VIDEO_CODECS = {
    "h264": {
        "encoder": "libx264",
        "ext": "mp4",
        "crf_hi": 16,
        "crf_lo": 30,
        "presets": {"fast": "medium", "balanced": "slow", "small": "slower"},
    },
    "hevc": {
        "encoder": "libx265",
        "ext": "mp4",
        "crf_hi": 21,
        "crf_lo": 34,
        "presets": {"fast": "medium", "balanced": "slow", "small": "slower"},
    },
    "av1": {
        "encoder": "libsvtav1",
        "ext": "mp4",
        "crf_hi": 28,
        "crf_lo": 48,
        "presets": {"fast": "8", "balanced": "10", "small": "12"},
        "extra": ["-svtav1-params", "tune=0:enable-overlays=1"],
    },
    "vp9": {
        "encoder": "libvpx-vp9",
        "ext": "webm",
        "crf_hi": 20,
        "crf_lo": 40,
        "presets": {"fast": "5", "balanced": "3", "small": "1"},
        "extra": ["-row-mt", "1", "-threads", "0", "-lag-in-frames", "16"],
    },
}

AUDIO_CODECS = {
    "aac": {"encoder": "aac", "ext": "m4a", "bitrate": 128},
    "opus": {"encoder": "libopus", "ext": "ogg", "bitrate": 96},
    "mp3": {"encoder": "libmp3lame", "ext": "mp3", "bitrate": 128},
}
AUDIO_COPY_OK = {"aac", "mp3", "opus", "vorbis", "flac", "pcm_s16le"}


@dataclass
class CompressOptions:
    codec: str = "h264"          # h264 | hevc | av1 | vp9
    mode: str = "quality"        # quality | size | auto
    quality: int = 70            # 0..100 (quality mode)
    target_size_mb: float = 25.0  # size mode
    preset: str = "balanced"     # fast | balanced | small
    audio: str = "auto"          # auto | copy | aac | opus | mp3
    max_width: int = 0           # 0 = keep
    max_height: int = 0          # 0 = keep
    tune: str = ""               # "" | film | animation | grain | fastdecode
    keep_subtitles: bool = False
    single_pass: bool = False    # force one pass even in size mode


@dataclass
class JobPlan:
    options: CompressOptions
    vf: list[str] = field(default_factory=list)
    target_w: int = 0
    target_h: int = 0
    scale_changed: bool = False
    video_codec: str = ""
    audio_codec: str = ""          # resolved
    audio_copy: bool = False
    passes: int = 1
    video_bitrate: int = 0         # for size mode
    audio_bitrate: int = 0
    estimated_bytes: int = 0
    notes: list[str] = field(default_factory=list)


def resolve_audio(media: MediaInfo, opts: CompressOptions) -> tuple[str, bool, int]:
    """Return (codec_key, copy_bool, bitrate_kbps)."""
    if not media.has_audio:
        return "", False, 0
    if opts.audio == "copy" or (opts.audio == "auto" and media.audio[0].codec in AUDIO_COPY_OK):
        return "", True, 0
    if opts.audio in ("aac", "opus", "mp3"):
        key = opts.audio
        return key, False, AUDIO_CODECS[key]["bitrate"]
    # auto but not copyable -> best universal fallback
    return "aac", False, 128


def _crf(codec: str, quality: int) -> int:
    spec = VIDEO_CODECS[codec]
    lo, hi = spec["crf_lo"], spec["crf_hi"]
    # quality 0 -> crf_lo (smallest file), 100 -> crf_hi (best quality)
    return round(hi + (lo - hi) * (1 - quality / 100))


def _scale_target(media: MediaInfo, opts: CompressOptions) -> tuple[int, int, bool]:
    if not media.video:
        return 0, 0, False
    w, h = media.video.width, media.video.height
    mw, mh = opts.max_width, opts.max_height
    if not mw and not mh:
        return w, h, False
    if mw and mh:
        if w <= mw and h <= mh:
            return w, h, False
        ratio = min(mw / w, mh / h)
    elif mw:
        if w <= mw:
            return w, h, False
        ratio = mw / w
    else:
        if h <= mh:
            return w, h, False
        ratio = mh / h
    nw = max(2, int(round(w * ratio / 2) * 2))
    nh = max(2, int(round(h * ratio / 2) * 2))
    return nw, nh, (nw, nh) != (w, h)


def build_plan(media: MediaInfo, opts: CompressOptions) -> JobPlan:
    plan = JobPlan(options=opts)
    if not media.video:
        raise ProbeError("no video stream found in the input file")

    # --- audio ---
    audio_key, audio_copy, audio_br = resolve_audio(media, opts)
    plan.audio_codec = audio_key or "copy" if media.has_audio else ""
    plan.audio_copy = audio_copy
    plan.audio_bitrate = audio_br

    # --- scaling ---
    w, h, changed = _scale_target(media, opts)
    plan.target_w, plan.target_h, plan.scale_changed = w, h, changed
    if changed:
        plan.vf.append(f"scale={w}:{h}")
        plan.notes.append(f"scaled {media.video.width}×{media.video.height} → {w}×{h}")
    plan.vf.append("format=yuv420p")

    # --- video codec / strategy ---
    from .capabilities import ensure_codec
    codec, codec_note = ensure_codec(opts.codec)
    if codec_note:
        plan.notes.append(codec_note)
    opts.codec = codec
    plan.video_codec = codec
    duration = media.duration or 0.001

    if opts.mode == "size" and opts.target_size_mb > 0:
        plan.passes = 1 if opts.single_pass else 2
        target_bytes = int(opts.target_size_mb * 1024 * 1024)
        # reserve audio share
        audio_bytes = 0
        if media.has_audio:
            if audio_copy:
                audio_bytes = int((media.audio[0].bitrate or 128000) * duration / 8)
            else:
                audio_bytes = int(audio_br * 1000 * duration / 8)
        video_bytes = max(64 * 1024, target_bytes - audio_bytes)
        plan.video_bitrate = int(video_bytes * 8 / duration)
        plan.estimated_bytes = int(video_bytes + audio_bytes)
        plan.notes.append(f"target ≈ {opts.target_size_mb:g} MB (video {video_bytes//1024//1024} MB + audio {audio_bytes//1024//1024} MB)")
    else:
        # quality mode: single pass CRF
        plan.passes = 1
        plan.notes.append(f"CRF {_crf(opts.codec, opts.quality)}")
        if not audio_copy and not media.has_audio:
            pass

    return plan


def _audio_args(plan: JobPlan, media: MediaInfo) -> list[str]:
    if not media.has_audio:
        return ["-an"]
    if plan.audio_copy:
        return ["-c:a", "copy"]
    codec = plan.audio_codec
    bitrate = plan.audio_bitrate
    if codec == "opus":
        return ["-c:a", "libopus", "-b:a", f"{bitrate}k", "-vbr", "on"]
    if codec == "mp3":
        return ["-c:a", "libmp3lame", "-b:a", f"{bitrate}k"]
    # aac default
    return ["-c:a", "aac", "-b:a", f"{bitrate}k", "-movflags", "+faststart"]


def build_command(
    plan: JobPlan, media: MediaInfo, src: str, dst: str, *, pass_no: int | None = None
) -> list[str]:
    opts = plan.options
    spec = VIDEO_CODECS[opts.codec]
    cmd = ["ffmpeg", "-y", "-v", "error", "-i", src]

    # --- video ---
    cmd += ["-map", "0:v:0"]
    if media.has_audio:
        cmd += ["-map", "0:a:0?"]
    cmd += ["-c:v", spec["encoder"]]
    cmd += ["-preset", spec["presets"].get(opts.preset, "medium")]

    if opts.mode == "size" and opts.target_size_mb > 0:
        cmd += ["-b:v", f"{plan.video_bitrate}"]
        if opts.codec in ("h264", "hevc"):
            cmd += ["-maxrate", f"{int(plan.video_bitrate * 1.6)}", "-bufsize", f"{int(plan.video_bitrate * 3)}"]
        if plan.passes == 2 and pass_no is not None:
            cmd += ["-pass", str(pass_no)]
            if pass_no == 1:
                cmd += ["-an", "-f", "null"] if False else []
    else:
        cmd += ["-crf", str(_crf(opts.codec, opts.quality))]

    if opts.tune and opts.codec in ("h264", "hevc"):
        cmd += ["-tune", opts.tune]
    if "extra" in spec:
        cmd += spec["extra"]

    if plan.vf:
        cmd += ["-vf", ",".join(plan.vf)]

    # --- audio ---
    cmd += _audio_args(plan, media)

    # --- container/misc ---
    cmd += ["-movflags", "+faststart"]
    if opts.codec == "vp9":
        cmd += ["-deadline", "good", "-cpu-used", spec["presets"].get(opts.preset, "3")]
    cmd += [dst]
    return cmd


async def run_ffmpeg(cmd: list[str], duration: float, on_progress=None, on_stage=None):
    """Run ffmpeg, parse -progress and call callbacks. Returns {ok, stderr, elapsed}."""
    started = time.monotonic()
    full_cmd = cmd + ["-progress", "pipe:1", "-nostats"]

    proc = await asyncio.create_subprocess_exec(
        *full_cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )

    async def pump_progress():
        assert proc.stdout is not None
        lines: list[str] = []
        while True:
            raw = await proc.stdout.readline()
            if not raw:
                break
            line = raw.decode("utf-8", "replace").strip()
            if not line:
                continue
            if re.match(r"^[A-Za-z_]+=", line):
                lines.append(line)
                if line.startswith("out_time_us="):
                    try:
                        us = int(line.split("=", 1)[1])
                        pct = min(100.0, us / (max(duration, 0.001) * 1e6) * 100)
                        on_progress and on_progress(pct)
                    except ValueError:
                        pass
                elif line.startswith("progress=end"):
                    on_progress and on_progress(100.0)
            elif line and on_stage:
                on_stage(line.strip()[:200])

    await asyncio.gather(pump_progress(), proc.wait())

    if proc.stderr is not None:
        stderr = (await proc.stderr.read()).decode("utf-8", "replace")
    else:
        stderr = ""
    elapsed = time.monotonic() - started
    return {"ok": proc.returncode == 0, "returncode": proc.returncode, "stderr": stderr, "elapsed": elapsed}
