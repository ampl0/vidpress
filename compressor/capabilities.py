"""Detect which encoders this ffmpeg build supports."""
from __future__ import annotations

import re
import shutil
import subprocess

from .engine import VIDEO_CODECS

_ENCODERS: set[str] | None = None


def available_encoders() -> set[str]:
    global _ENCODERS
    if _ENCODERS is not None:
        return _ENCODERS
    out: set[str] = set()
    if shutil.which("ffmpeg"):
        try:
            proc = subprocess.run(
                ["ffmpeg", "-hide_banner", "-encoders"],
                capture_output=True, text=True, timeout=30,
            )
            for line in (proc.stdout or "").splitlines():
                parts = line.split()
                # encoder rows look like: " V....D libx264  H.264 ..."
                # header/legend rows have "=" in the second column.
                if len(parts) >= 2 and parts[1] != "=":
                    out.add(parts[1])
        except Exception:
            pass
    _ENCODERS = out
    return out


def available_video_codecs() -> list[str]:
    enc = available_encoders()
    return [name for name, spec in VIDEO_CODECS.items() if spec["encoder"] in enc]


def ensure_codec(codec: str) -> tuple[str, str]:
    """Return (codec, note). Falls back to h264 when the requested encoder is missing."""
    if codec in available_video_codecs():
        return codec, ""
    if codec != "h264" and "h264" in available_video_codecs():
        return "h264", f"{codec} encoder not available in this ffmpeg build; used H.264 instead"
    return codec, f"{codec} encoder may be unavailable"
