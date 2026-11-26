"""Media probing via ffprobe."""
from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass, field
from pathlib import Path


class ProbeError(Exception):
    pass


@dataclass
class StreamInfo:
    index: int
    kind: str  # "video" | "audio" | "other"
    codec: str
    width: int = 0
    height: int = 0
    fps: float = 0.0
    bitrate: int = 0
    channels: int = 0
    sample_rate: int = 0
    duration: float = 0.0
    layout: str = ""


@dataclass
class MediaInfo:
    path: str
    container: str
    duration: float
    size: int
    total_bitrate: int
    video: StreamInfo | None = None
    audio: list[StreamInfo] = field(default_factory=list)
    has_audio: bool = False

    @property
    def resolution_label(self) -> str:
        if not self.video:
            return "—"
        return f"{self.video.width}×{self.video.height}"


def _float(value) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def probe(path: str | Path) -> MediaInfo:
    path = str(path)
    cmd = [
        "ffprobe",
        "-v", "error",
        "-print_format", "json",
        "-show_format",
        "-show_streams",
        path,
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    except subprocess.TimeoutExpired as exc:
        raise ProbeError("ffprobe timed out") from exc
    except FileNotFoundError as exc:
        raise ProbeError("ffprobe not found") from exc
    if proc.returncode != 0:
        raise ProbeError((proc.stderr or "").strip() or "ffprobe failed")

    try:
        data = json.loads(proc.stdout or "{}")
    except json.JSONDecodeError as exc:
        raise ProbeError("unreadable ffprobe output") from exc

    fmt = data.get("format", {})
    streams = data.get("streams", [])

    video: StreamInfo | None = None
    audio: list[StreamInfo] = []

    for raw in streams:
        kind = raw.get("codec_type", "other")
        fps = 0.0
        avg = raw.get("avg_frame_rate") or "0/1"
        try:
            num, den = avg.split("/")
            num, den = float(num), float(den)
            fps = num / den if den else 0.0
        except (ValueError, ZeroDivisionError):
            fps = 0.0

        si = StreamInfo(
            index=int(raw.get("index", 0)),
            kind=kind,
            codec=raw.get("codec_name", ""),
            width=int(raw.get("width", 0)),
            height=int(raw.get("height", 0)),
            fps=fps,
            bitrate=int(raw.get("bit_rate", 0) or 0),
            channels=int(raw.get("channels", 0) or 0),
            sample_rate=int(raw.get("sample_rate", 0) or 0),
            duration=_float(raw.get("duration")),
            layout=raw.get("channel_layout", ""),
        )
        if kind == "video" and video is None:
            video = si
        elif kind == "audio":
            audio.append(si)

    return MediaInfo(
        path=path,
        container=fmt.get("format_name", ""),
        duration=_float(fmt.get("duration")),
        size=int(fmt.get("size", 0) or 0),
        total_bitrate=int(fmt.get("bit_rate", 0) or 0),
        video=video,
        audio=audio,
        has_audio=bool(audio),
    )
