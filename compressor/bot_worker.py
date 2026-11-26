"""Telegram bot for VidPress.

Tcomp-style inline menu (presets, size, audio, format, codec, custom command)
with live download / compress / upload progress, refresh and cancel buttons.

Large files (>20 MB) go through Telethon (MTProto) when api_id/api_hash are
configured; the standard Bot API is used only as a fallback.
"""
from __future__ import annotations

import asyncio
import json
import logging
import mimetypes
import os
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path

import aiohttp

logger = logging.getLogger("vidpress.bot")

DEFAULT_API_BASE = "https://api.telegram.org"
OFFICIAL_DOWNLOAD_MB = 20

# Quality presets (CRF = H.264 reference value; engine offsets other codecs).
PRESETS = {
    "high": {"label": "High Quality", "crf": 23, "speed": "medium", "quality": 85},
    "medium": {"label": "Balanced", "crf": 28, "speed": "fast", "quality": 62},
    "low": {"label": "Small Size", "crf": 32, "speed": "veryfast", "quality": 42},
    "ultra": {"label": "Ultra Small", "crf": 38, "speed": "ultrafast", "quality": 28},
}

DIMENSIONS = {
    "none": {"h": 0, "label": "Original"},
    "240": {"h": 240, "label": "240p"},
    "360": {"h": 360, "label": "360p"},
    "480": {"h": 480, "label": "480p"},
    "640": {"h": 640, "label": "640p"},
    "720": {"h": 720, "label": "720p"},
    "1080": {"h": 1080, "label": "1080p"},
}

AUDIO = {
    "none": {"codec": "none", "bitrate": 0, "label": "No audio"},
    "copy": {"codec": "copy", "bitrate": 0, "label": "Copy original"},
    "opus_24": {"codec": "opus", "bitrate": 24, "label": "Opus 24k"},
    "opus_32": {"codec": "opus", "bitrate": 32, "label": "Opus 32k"},
    "opus_48": {"codec": "opus", "bitrate": 48, "label": "Opus 48k"},
    "opus_64": {"codec": "opus", "bitrate": 64, "label": "Opus 64k"},
    "mp3_64": {"codec": "mp3", "bitrate": 64, "label": "MP3 64k"},
    "mp3_96": {"codec": "mp3", "bitrate": 96, "label": "MP3 96k"},
    "mp3_128": {"codec": "mp3", "bitrate": 128, "label": "MP3 128k"},
    "mp3_192": {"codec": "mp3", "bitrate": 192, "label": "MP3 192k"},
    "aac_64": {"codec": "aac", "bitrate": 64, "label": "AAC 64k"},
    "aac_96": {"codec": "aac", "bitrate": 96, "label": "AAC 96k"},
    "aac_128": {"codec": "aac", "bitrate": 128, "label": "AAC 128k"},
    "aac_192": {"codec": "aac", "bitrate": 192, "label": "AAC 192k"},
}

FORMATS = {"mp4": "MP4", "mkv": "MKV", "webm": "WEBM"}
CODECS = {"h264": "H.264", "hevc": "H.265", "av1": "AV1", "vp9": "VP9"}

DEFAULTS = {
    "preset": "medium",
    "dimension": "480",
    "audio": "aac_96",
    "format": "mp4",
    "codec": "h264",
    "custom": "",
}

VIDEO_EXTS = (".mp4", ".mkv", ".webm", ".avi", ".mov", ".flv", ".wmv", ".m4v", ".ts", ".m2ts", ".3gp", ".mts")
AUDIO_EXTS = (".mp3", ".flac", ".m4a", ".aac", ".ogg", ".opus", ".wav", ".wma", ".alac")


def _bar(pct: float, length: int = 12) -> str:
    pct = max(0.0, min(100.0, pct))
    filled = int(round(length * pct / 100))
    return "█" * filled + "░" * (length - filled)


def _fmt_time(seconds: float) -> str:
    if seconds < 0 or seconds != seconds or seconds == float("inf"):
        return "—"
    seconds = int(seconds)
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


def _fmt_speed(bps: float) -> str:
    if bps <= 0:
        return "—"
    units = ["B/s", "KB/s", "MB/s", "GB/s"]
    value, i = float(bps), 0
    while value >= 1024 and i < len(units) - 1:
        value /= 1024.0
        i += 1
    return f"{value:.1f} {units[i]}"


def _fmt_mb(size: int | float) -> str:
    return f"{size / 1048576:.1f} MB"


def _plain(text: str) -> str:
    """The Bot API is used without parse_mode, so drop Markdown markers."""
    return text.replace("**", "").replace("`", "")


def _size_line(original: int, output: int) -> str:
    if not original or not output:
        return f"📦 {_fmt_mb(original)} → {_fmt_mb(output)}"
    pct = (1 - output / original) * 100
    if pct >= 0.5:
        return f"📦 {_fmt_mb(original)} → {_fmt_mb(output)} ({pct:.0f}% smaller)"
    return f"📦 {_fmt_mb(original)} → {_fmt_mb(output)} (no size reduction)"


@dataclass
class BotTask:
    chat_id: int
    message_id: int
    kind: str = "video"  # video | audio
    status_id: int | None = None
    stage: str = "queued"
    progress: float = 0.0
    speed: str = "—"
    eta: str = "—"
    detail: str = ""
    started: float = field(default_factory=time.time)
    stage_started: float = field(default_factory=time.time)
    cancelled: bool = False
    done: bool = False
    job_id: str | None = None
    last_edit: float = 0.0
    last_text: str = ""
    force: bool = False


class TelegramBot:
    def __init__(self, token, manager, data_dir, api_base=DEFAULT_API_BASE, max_dl_mb=200,
                 api_id=None, api_hash=None):
        self.token = token
        self.manager = manager
        self.data_dir = data_dir
        self.api_base = (api_base or DEFAULT_API_BASE).rstrip("/")
        self.max_dl_mb = max(int(max_dl_mb or 200), 1)
        self.api_id = api_id
        self.api_hash = api_hash
        self.username = None
        self.base = f"{self.api_base}/bot{token}"
        self.last_error = None
        self._task = None
        self._offset = 0
        self._session: aiohttp.ClientSession | None = None
        self._tg = None  # telethon client (lazy)
        self._prefs_path = data_dir / "chat_prefs.json"
        self._prefs: dict[str, dict] = self._load_prefs()
        self._running: dict[int, BotTask] = {}
        self._procs: dict[int, asyncio.Task] = {}

    # ---- telethon / large files ----
    @property
    def official(self) -> bool:
        return self.api_base.startswith("https://api.telegram.org")

    @property
    def large_enabled(self) -> bool:
        return bool(self.api_id and self.api_hash)

    async def _ensure_tg(self):
        if self._tg is not None:
            return self._tg
        from telethon import TelegramClient

        session = self.data_dir / "vidpress_bot.session"
        client = TelegramClient(
            str(session), int(self.api_id), self.api_hash,
            connection_retries=3, request_retries=3, timeout=30,
            flood_sleep_threshold=120,
        )
        await client.start(bot_token=self.token)
        self._tg = client
        logger.info("telethon client connected for large files")
        return client

    # ---- lifecycle ----
    async def start(self):
        if self._task is not None:
            return
        self._session = aiohttp.ClientSession()
        async with self._session.get(self.base + "/getMe") as resp:
            data = await resp.json()
            if not data.get("ok"):
                self.last_error = data.get("description", "invalid bot token")
                raise RuntimeError(self.last_error)
            self.username = data["result"].get("username")
        try:
            async with self._session.post(self.base + "/deleteWebhook", json={"drop_pending_updates": False}) as resp:
                wh = await resp.json()
                if not wh.get("ok"):
                    logger.warning("deleteWebhook warning: %s", wh.get("description"))
        except Exception as exc:  # noqa: BLE001
            logger.warning("deleteWebhook failed: %s", exc)
        self.last_error = None
        self._task = asyncio.create_task(self._poll())
        logger.info("telegram bot started as @%s (large_files=%s)", self.username, self.large_enabled)

    async def stop(self):
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
            self._task = None
        for proc in list(self._procs.values()):
            proc.cancel()
        if self._tg is not None:
            try:
                await self._tg.disconnect()
            except Exception:
                pass
            self._tg = None
        if self._session:
            await self._session.close()
            self._session = None
        self.last_error = None

    def is_running(self):
        return self._task is not None and not self._task.done()

    # ---- prefs ----
    def _load_prefs(self):
        try:
            if self._prefs_path.exists():
                return json.loads(self._prefs_path.read_text(encoding="utf-8"))
        except Exception:
            pass
        return {}

    def _save_prefs(self):
        try:
            self._prefs_path.parent.mkdir(parents=True, exist_ok=True)
            self._prefs_path.write_text(json.dumps(self._prefs, ensure_ascii=False), encoding="utf-8")
        except Exception as exc:
            logger.warning("save prefs failed: %s", exc)

    def prefs(self, chat_id: int) -> dict:
        return {**DEFAULTS, **(self._prefs.get(str(chat_id)) or {})}

    def set_pref(self, chat_id: int, key: str, value):
        self._prefs.setdefault(str(chat_id), {})[key] = value
        self._save_prefs()

    # ---- bot api helpers ----
    async def _api(self, method, *, json_body=None, data=None, timeout=60):
        if self._session is None:
            raise RuntimeError("bot not started")
        async with self._session.post(
            f"{self.base}/{method}", json=json_body, data=data,
            timeout=aiohttp.ClientTimeout(total=timeout),
        ) as resp:
            return await resp.json()

    async def _send(self, chat_id, text, reply_markup=None) -> int | None:
        try:
            body = {"chat_id": chat_id, "text": _plain(text)}
            if reply_markup is not None:
                body["reply_markup"] = reply_markup
            data = await self._api("sendMessage", json_body=body)
            if data.get("ok"):
                return data["result"].get("message_id")
            logger.warning("sendMessage failed: %s", data.get("description"))
        except Exception as exc:  # noqa: BLE001
            logger.warning("sendMessage failed: %s", exc)
        return None

    async def _edit_message(self, chat_id, message_id, text, reply_markup=None) -> bool:
        if not message_id:
            return False
        try:
            body = {"chat_id": chat_id, "message_id": message_id, "text": _plain(text)}
            if reply_markup is not None:
                body["reply_markup"] = reply_markup
            data = await self._api("editMessageText", json_body=body)
            return bool(data.get("ok"))
        except Exception as exc:  # noqa: BLE001
            logger.debug("editMessageText failed: %s", exc)
            return False

    async def _delete_message(self, chat_id, message_id):
        if not message_id:
            return
        try:
            await self._api("deleteMessage", json_body={"chat_id": chat_id, "message_id": message_id})
        except Exception:
            pass

    async def _answer_callback(self, callback_id, text=None):
        try:
            await self._api("answerCallbackQuery", json_body={"callback_query_id": callback_id, "text": text or ""})
        except Exception:
            pass

    # ---- media transfer ----
    async def _download(self, file_id, chat_id, message_id, dest: Path, task: BotTask) -> Path:
        dest.parent.mkdir(parents=True, exist_ok=True)
        if self.large_enabled:
            try:
                client = await self._ensure_tg()
                msg = await client.get_messages(chat_id, ids=message_id)
                if msg and msg.media:
                    def cb(current, total):
                        if total:
                            task.progress = current / total * 100
                            elapsed = max(0.001, time.time() - task.stage_started)
                            task.speed = _fmt_speed(current / elapsed)
                            if task.progress > 1:
                                total_est = elapsed / (task.progress / 100)
                                task.eta = _fmt_time(max(0.0, total_est - elapsed))
                    result = await client.download_media(msg, file=str(dest), progress_callback=cb)
                    if result and Path(result).exists():
                        return Path(result)
                    raise RuntimeError("download produced no file")
                raise RuntimeError("media not found via telethon")
            except Exception as exc:  # noqa: BLE001
                logger.exception("telethon download failed, falling back to bot api: %s", exc)

        # standard Bot API fallback
        data = await self._api("getFile", json_body={"file_id": file_id})
        if not data.get("ok"):
            desc = data.get("description") or "getFile failed"
            raise RuntimeError(desc)
        fp = (data.get("result") or {}).get("file_path")
        if not fp:
            raise RuntimeError("no file_path returned")
        url = f"{self.api_base}/file/bot{self.token}/{fp}"
        async with self._session.get(url, timeout=aiohttp.ClientTimeout(total=600)) as resp:
            if resp.status != 200:
                raise RuntimeError(f"download failed (HTTP {resp.status})")
            total = int(resp.headers.get("Content-Length", 0) or 0)
            done = 0
            with open(dest, "wb") as fh:
                async for chunk in resp.content.iter_chunked(1 << 20):
                    fh.write(chunk)
                    done += len(chunk)
                    if total:
                        task.progress = done / total * 100
                        elapsed = max(0.001, time.time() - task.stage_started)
                        task.speed = _fmt_speed(done / elapsed)
        return dest

    async def _send_media(self, chat_id, path: Path, caption: str, kind: str, task: BotTask) -> None:
        def cb(current, total):
            if total:
                task.progress = current / total * 100
                elapsed = max(0.001, time.time() - task.stage_started)
                task.speed = _fmt_speed(current / elapsed)
                if task.progress > 1:
                    total_est = elapsed / (task.progress / 100)
                    task.eta = _fmt_time(max(0.0, total_est - elapsed))

        if self.large_enabled:
            try:
                client = await self._ensure_tg()
                kwargs = dict(caption=caption, progress_callback=cb, force_document=False)
                if kind == "video":
                    kwargs["supports_streaming"] = True
                await client.send_file(chat_id, str(path), **kwargs)
                return
            except Exception as exc:  # noqa: BLE001
                logger.exception("telethon send failed, falling back to bot api: %s", exc)

        form = aiohttp.FormData()
        form.add_field("chat_id", str(chat_id))
        form.add_field("caption", _plain(caption))
        fh = path.open("rb")
        ctype = mimetypes.guess_type(path.name)[0] or (
            "video/mp4" if kind == "video" else "audio/mpeg"
        )
        try:
            if kind == "audio":
                form.add_field("audio", fh, filename=path.name, content_type=ctype)
                await self._api("sendAudio", data=form, timeout=600)
            else:
                form.add_field("video", fh, filename=path.name, content_type=ctype)
                await self._api("sendVideo", data=form, timeout=600)
        finally:
            try:
                fh.close()
            except Exception:
                pass

    # ---- polling ----
    async def _poll(self):
        while True:
            try:
                body = {"timeout": 50, "offset": self._offset, "allowed_updates": ["message", "callback_query"]}
                data = await self._api("getUpdates", json_body=body, timeout=70)
                if not data.get("ok"):
                    desc = data.get("description", "getUpdates failed")
                    self.last_error = desc
                    if "conflict" in str(desc).lower():
                        try:
                            await self._api("deleteWebhook", json_body={"drop_pending_updates": False})
                        except Exception:
                            pass
                    await asyncio.sleep(3)
                    continue
                self.last_error = None
                for upd in data.get("result", []):
                    self._offset = upd["update_id"] + 1
                    asyncio.create_task(self._safe_handle(upd))
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                self.last_error = str(exc)
                logger.warning("poll error: %s", exc)
                await asyncio.sleep(3)

    async def _safe_handle(self, upd):
        try:
            await self._handle(upd)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            logger.exception("update handling failed: %s", exc)
            self.last_error = str(exc)

    def _media_of(self, msg) -> tuple[str | None, str | None]:
        """Return (file_id, kind) for a message, or (None, None)."""
        video = msg.get("video")
        if video:
            return video.get("file_id"), "video"
        audio = msg.get("audio")
        if audio:
            return audio.get("file_id"), "audio"
        doc = msg.get("document")
        if doc:
            mime = (doc.get("mime_type") or "").lower()
            name = (doc.get("file_name") or "").lower()
            if mime.startswith("video/") or name.endswith(VIDEO_EXTS):
                return doc.get("file_id"), "video"
            if mime.startswith("audio/") or name.endswith(AUDIO_EXTS):
                return doc.get("file_id"), "audio"
        return None, None

    def _file_size(self, msg) -> int:
        for key in ("video", "audio", "document"):
            obj = msg.get(key)
            if obj and obj.get("file_size"):
                return int(obj["file_size"])
        return 0

    def _orig_name(self, msg, kind: str) -> str:
        for key in ("video", "audio", "document"):
            obj = msg.get(key)
            if obj and obj.get("file_name"):
                return obj["file_name"]
        return f"{kind}-{msg.get('message_id')}{'.mp4' if kind == 'video' else '.mp3'}"

    async def _handle(self, upd):
        if "callback_query" in upd:
            await self._handle_callback(upd["callback_query"])
            return
        msg = upd.get("message") or {}
        chat_id = (msg.get("chat") or {}).get("id")
        if not chat_id:
            return
        text = (msg.get("text") or "").strip()
        message_id = msg.get("message_id")

        if text.startswith("/"):
            await self._command(chat_id, text, message_id)
            return

        file_id, kind = self._media_of(msg)
        if not file_id:
            await self._send(
                chat_id,
                "Send me a video or audio file and I'll compress or convert it. 🎬\n\n"
                "Use /settings to pick quality, size, audio and format.",
            )
            return

        size = self._file_size(msg)
        limit = self.max_dl_mb * 1024 * 1024
        if size and size > limit:
            await self._send(chat_id, f"That file is {_fmt_mb(size)}; the limit is {self.max_dl_mb} MB.")
            return
        if size > OFFICIAL_DOWNLOAD_MB * 1024 * 1024 and not self.large_enabled:
            await self._send(
                chat_id,
                "Files over 20 MB need the MTProto client. Set api_id/api_hash in the admin panel.",
            )
            return
        if chat_id in self._running:
            await self._send(chat_id, "⏳ Another task is already running in this chat. Use /cancel first.")
            return

        task = BotTask(chat_id=chat_id, message_id=int(message_id), kind=kind)
        proc = asyncio.current_task()
        if proc:
            self._procs[chat_id] = proc
        self._running[chat_id] = task
        ticker = asyncio.create_task(self._ticker(task))
        try:
            await self._process(task, file_id, self._orig_name(msg, kind))
        finally:
            task.done = True
            ticker.cancel()
            self._running.pop(chat_id, None)
            self._procs.pop(chat_id, None)

    # ---- commands ----
    async def _command(self, chat_id, text, message_id):
        cmd = text.split()[0].lower().split("@")[0]
        if cmd in ("/start", "/help"):
            await self._send(chat_id, self._welcome_text(chat_id), self._menu_keyboard(chat_id))
        elif cmd == "/settings":
            await self._send(chat_id, self._menu_text(chat_id), self._menu_keyboard(chat_id))
        elif cmd == "/defaults":
            self._prefs.pop(str(chat_id), None)
            self._save_prefs()
            await self._send(chat_id, "✅ Settings reset to defaults.", self._menu_keyboard(chat_id))
        elif cmd == "/status":
            await self._send(chat_id, self._status_text())
        elif cmd == "/cancel":
            await self._cancel_chat(chat_id)
        elif cmd == "/setcode":
            if len(text.split(maxsplit=1)) < 2:
                await self._send(
                    chat_id,
                    "Usage: `/setcode -c:v libx264 -crf 28 ...`\n"
                    "The arguments are inserted into `ffmpeg -i input <args> output`.",
                )
                return
            custom = text.split(maxsplit=1)[1].strip()
            self.set_pref(chat_id, "custom", custom)
            await self._send(chat_id, f"✅ Custom command set:\n`{custom[:900]}`\n\nUse /settings → Custom → Clear to remove.")
        elif cmd == "/getcode":
            p = self.prefs(chat_id)
            lines = [
                "📝 Custom command:",
                f"`{p['custom']}`" if p.get("custom") else "_not set_",
                "",
                "🎛 Presets:",
            ]
            for key, cfg in PRESETS.items():
                lines.append(f"• {key}: CRF {cfg['crf']}")
            lines += [
                "",
                "📐 Sizes: " + ", ".join(DIMENSIONS),
                "🔊 Audio: " + ", ".join(AUDIO),
                "📁 Formats: " + ", ".join(FORMATS),
                "🎞 Codecs: " + ", ".join(CODECS),
            ]
            await self._send(chat_id, "\n".join(lines))
        elif cmd in ("/id", "/whoami"):
            await self._send(chat_id, f"Chat id: `{chat_id}`")
        else:
            await self._send(chat_id, "Unknown command. Try /settings or /help.")

    async def _cancel_chat(self, chat_id):
        task = self._running.get(chat_id)
        if not task:
            await self._send(chat_id, "Nothing to cancel.")
            return
        task.cancelled = True
        if task.job_id:
            try:
                await self.manager.cancel(task.job_id)
            except Exception:
                pass
        proc = self._procs.get(chat_id)
        if proc:
            proc.cancel()
        await self._render(task, force=True, cancelled=True)

    # ---- menu ----
    def _welcome_text(self, chat_id) -> str:
        return (
            "🎬 **VidPress** — video & audio compressor\n\n"
            "Send a video or audio file and I'll compress or convert it.\n\n"
            "**Commands**\n"
            "• /settings — quality, size, audio, format, codec\n"
            "• /defaults — reset your settings\n"
            "• /cancel — stop the running task\n"
            "• /setcode — set a custom FFmpeg command\n"
            "• /getcode — show commands and presets\n"
            "• /status — queue and bot status\n\n"
            + self._current_line(chat_id)
        )

    def _current_line(self, chat_id) -> str:
        p = self.prefs(chat_id)
        preset = PRESETS.get(p["preset"], PRESETS["medium"])
        dim = DIMENSIONS.get(str(p["dimension"]), DIMENSIONS["480"])
        audio = AUDIO.get(p["audio"], AUDIO["aac_96"])
        custom = " · custom" if p.get("custom") else ""
        return (
            f"**Current:** {preset['label']} (CRF {preset['crf']})\n"
            f"{dim['label']} · {audio['label']} · {FORMATS.get(p['format'], 'MP4')} · "
            f"{CODECS.get(p['codec'], 'H.264')}{custom}"
        )

    def _menu_text(self, chat_id) -> str:
        p = self.prefs(chat_id)
        preset = PRESETS.get(p["preset"], PRESETS["medium"])
        dim = DIMENSIONS.get(str(p["dimension"]), DIMENSIONS["480"])
        audio = AUDIO.get(p["audio"], AUDIO["aac_96"])
        custom = (p.get("custom") or "").strip()
        return (
            "⚙️ **Compression settings**\n\n"
            f"🎛 Preset: {preset['label']} (CRF {preset['crf']})\n"
            f"📐 Size: {dim['label']}\n"
            f"🔊 Audio: {audio['label']}\n"
            f"📁 Format: {FORMATS.get(p['format'], 'MP4')}\n"
            f"🎞 Codec: {CODECS.get(p['codec'], 'H.264')}\n"
            f"⚙️ Custom: {'set' if custom else 'not set'}\n\n"
            "Tap a button to change it, then send a file."
        )

    def _btn(self, label, data):
        return {"text": label, "callback_data": data}

    def _menu_keyboard(self, chat_id):
        p = self.prefs(chat_id)
        preset = PRESETS.get(p["preset"], PRESETS["medium"])
        dim = DIMENSIONS.get(str(p["dimension"]), DIMENSIONS["480"])
        audio = AUDIO.get(p["audio"], AUDIO["aac_96"])
        return {
            "inline_keyboard": [
                [self._btn(f"🎛 {preset['label']}", "menu:preset"), self._btn(f"📐 {dim['label']}", "menu:size")],
                [self._btn(f"🔊 {audio['label']}", "menu:audio"), self._btn(f"📁 {FORMATS.get(p['format'], 'MP4')}", "menu:format")],
                [self._btn(f"🎞 {CODECS.get(p['codec'], 'H.264')}", "menu:codec"), self._btn("📊 Status", "menu:status")],
                [self._btn("⚙️ Custom command", "menu:custom")],
            ]
        }

    def _selection(self, kind: str, chat_id):
        p = self.prefs(chat_id)
        rows = []
        if kind == "preset":
            title = "🎛 **Choose a preset**"
            options = [(k, f"{v['label']} · CRF {v['crf']}") for k, v in PRESETS.items()]
            current = PRESETS.get(p["preset"], PRESETS["medium"])["label"]
        elif kind == "size":
            title = "📐 **Choose output size**"
            options = [(k, v["label"]) for k, v in DIMENSIONS.items()]
            current = DIMENSIONS.get(str(p["dimension"]), DIMENSIONS["480"])["label"]
        elif kind == "audio":
            title = "🔊 **Choose audio profile**"
            options = [(k, v["label"]) for k, v in AUDIO.items()]
            current = AUDIO.get(p["audio"], AUDIO["aac_96"])["label"]
        elif kind == "format":
            title = "📁 **Choose container**"
            options = [(k, v) for k, v in FORMATS.items()]
            current = FORMATS.get(p["format"], "MP4")
        else:
            title = "🎞 **Choose video codec**"
            options = [(k, v) for k, v in CODECS.items()]
            current = CODECS.get(p["codec"], "H.264")

        for i in range(0, len(options), 2):
            row = []
            for key, label in options[i:i + 2]:
                if kind == "preset":
                    mark = "✅ " if key == p["preset"] else ""
                elif kind == "size":
                    mark = "✅ " if key == str(p["dimension"]) else ""
                else:
                    mark = "✅ " if key == p[kind] else ""
                row.append(self._btn(f"{mark}{label}", f"set:{kind}:{key}"))
            rows.append(row)
        rows.append([self._btn("« Back", "menu:back")])
        return title + "\n\nCurrent: **" + str(current) + "**", {"inline_keyboard": rows}

    async def _handle_callback(self, cq):
        chat_id = (cq.get("message", {}).get("chat") or {}).get("id")
        message_id = cq.get("message", {}).get("message_id")
        data = cq.get("data") or ""
        if not chat_id:
            return
        await self._answer_callback(cq.get("id"))

        if data.startswith("task:"):
            action = data.split(":", 1)[1]
            task = self._running.get(chat_id)
            if action == "cancel":
                await self._cancel_chat(chat_id)
            elif action == "dismiss":
                await self._delete_message(chat_id, message_id)
            elif action == "refresh":
                if task:
                    await self._render(task, force=True)
                else:
                    await self._edit_message(chat_id, message_id, "No task is running.")
            return

        if data.startswith("menu:"):
            menu = data.split(":", 1)[1]
            if menu in ("preset", "size", "audio", "format", "codec"):
                text, kb = self._selection(menu, chat_id)
                await self._edit_message(chat_id, message_id, text, kb)
            elif menu == "status":
                await self._edit_message(chat_id, message_id, self._status_text(),
                                         {"inline_keyboard": [[self._btn("« Back", "menu:back")]]})
            elif menu == "custom":
                p = self.prefs(chat_id)
                custom = (p.get("custom") or "").strip()
                text = (
                    "⚙️ **Custom FFmpeg command**\n\n"
                    + (f"`{custom[:900]}`" if custom else "_not set_")
                    + "\n\nSet it with `/setcode <args>`. It overrides preset, size, audio and format."
                )
                kb = {"inline_keyboard": [
                    ([self._btn("🗑 Clear", "menu:custom_clear")] if custom else [])
                    + [self._btn("« Back", "menu:back")]
                ]}
                await self._edit_message(chat_id, message_id, text, kb)
            elif menu == "custom_clear":
                self.set_pref(chat_id, "custom", "")
                await self._edit_message(chat_id, message_id, self._menu_text(chat_id), self._menu_keyboard(chat_id))
            else:  # back
                await self._edit_message(chat_id, message_id, self._menu_text(chat_id), self._menu_keyboard(chat_id))
            return

        if data.startswith("set:"):
            try:
                _, kind, value = data.split(":", 2)
            except ValueError:
                return
            valid = {"preset": PRESETS, "size": DIMENSIONS, "audio": AUDIO,
                     "format": FORMATS, "codec": CODECS}.get(kind, {})
            if value in valid:
                self.set_pref(chat_id, "dimension" if kind == "size" else kind, value)
            await self._edit_message(chat_id, message_id, self._menu_text(chat_id), self._menu_keyboard(chat_id))

    def _status_text(self) -> str:
        running = [t for t in self._running.values() if not t.done]
        lines = [
            "📊 **VidPress status**",
            "",
            f"• Active tasks: {len(running)}",
            f"• Queue capacity: {self.manager.capacity()}",
            f"• Large files (MTProto): {'on' if self.large_enabled else 'off'}",
            f"• Max download: {self.max_dl_mb} MB",
            f"• Bot: @{self.username or '—'}",
        ]
        if self.last_error:
            lines.append(f"• Last error: {str(self.last_error)[:180]}")
        return "\n".join(lines)

    # ---- progress ----
    def _task_text(self, task: BotTask) -> str:
        bar = _bar(task.progress)
        elapsed = _fmt_time(time.time() - task.started)
        p = self.prefs(task.chat_id)
        preset = PRESETS.get(p["preset"], PRESETS["medium"])
        dim = DIMENSIONS.get(str(p["dimension"]), DIMENSIONS["480"])
        audio = AUDIO.get(p["audio"], AUDIO["aac_96"])
        settings = (
            f"🎛 {preset['label']} (CRF {preset['crf']}) · 📐 {dim['label']}\n"
            f"🔊 {audio['label']} · 📁 {FORMATS.get(p['format'], 'MP4')}"
        )

        if task.stage == "downloading":
            return (
                "📥 **Downloading…**\n\n"
                f"`{bar}` **{task.progress:.1f}%**\n\n"
                f"⏱ {elapsed} · ⏳ {task.eta} · 🚀 {task.speed}"
            )
        if task.stage == "compressing":
            stage = task.detail or "encoding"
            if stage == "queued":
                stage = "waiting in queue"
            return (
                "⚙️ **Compressing…**\n\n"
                f"`{bar}` **{task.progress:.1f}%**\n\n"
                f"⏱ {elapsed} · ⏳ {task.eta} · 🚀 {task.speed}\n"
                f"🎞 {stage}\n\n{settings}"
            )
        if task.stage == "uploading":
            return (
                "📤 **Uploading…**\n\n"
                f"`{bar}` **{task.progress:.1f}%**\n\n"
                f"⏱ {elapsed} · ⏳ {task.eta} · 🚀 {task.speed}"
            )
        if task.stage == "cancelled":
            return "❌ **Cancelled**\n\nThe task was stopped."
        return f"⏳ **Queued…**\n\n{settings}"

    def _task_keyboard(self):
        return {"inline_keyboard": [[
            self._btn("📊 Refresh", "task:refresh"),
            self._btn("❌ Cancel", "task:cancel"),
        ]]}

    async def _render(self, task: BotTask, force: bool = False, cancelled: bool = False):
        if task.status_id is None:
            return
        now = time.time()
        if not force and now - task.last_edit < 3.0:
            return
        text = self._task_text(task)
        if text == task.last_text and not force:
            return
        keyboard = ({"inline_keyboard": [[self._btn("🗑 Dismiss", "task:dismiss")]]}
                    if task.stage == "cancelled" else self._task_keyboard())
        ok = await self._edit_message(task.chat_id, task.status_id, text, keyboard)
        if ok:
            task.last_edit = now
            task.last_text = text

    async def _ticker(self, task: BotTask):
        try:
            while not task.done:
                await asyncio.sleep(2.0)
                await self._render(task)
        except asyncio.CancelledError:
            return

    async def _set_stage(self, task: BotTask, stage: str, detail: str = ""):
        task.stage = stage
        task.progress = 0.0
        task.stage_started = time.time()
        task.speed = "—"
        task.eta = "—"
        task.detail = detail
        await self._render(task, force=True)

    # ---- processing ----
    def _options(self, chat_id: int) -> dict:
        p = self.prefs(chat_id)
        preset = PRESETS.get(p["preset"], PRESETS["medium"])
        dim = DIMENSIONS.get(str(p["dimension"]), DIMENSIONS["480"])
        audio = AUDIO.get(p["audio"], AUDIO["aac_96"])
        return {
            "codec": p["codec"],
            "mode": "quality",
            "quality": preset["quality"],
            "crf": preset["crf"],
            "preset": preset["speed"],
            "audio": audio["codec"],
            "audio_bitrate": audio["bitrate"],
            "max_width": 0,
            "max_height": dim["h"],
            "container": p["format"],
            "custom_cmd": (p.get("custom") or "").strip(),
            "single_pass": False,
        }

    async def _process(self, task: BotTask, file_id, orig_name: str):
        chat_id = task.chat_id
        tmp_dir = self.data_dir / "bot_tmp"
        task_dir = tmp_dir / f"{chat_id}-{task.message_id}"
        task_dir.mkdir(parents=True, exist_ok=True)
        safe_stem = "".join(c for c in Path(orig_name).stem if c.isalnum() or c in "-_. ").strip()[:80] or "file"
        src_ext = Path(orig_name).suffix.lower() or (".mp4" if task.kind == "video" else ".mp3")
        src = task_dir / f"input{src_ext}"
        final: Path | None = None
        job = None

        task.status_id = await self._send(chat_id, self._task_text(task), self._task_keyboard())
        try:
            # ---- download ----
            await self._set_stage(task, "downloading")
            await self._download(file_id, chat_id, task.message_id, src, task)
            if task.cancelled:
                raise asyncio.CancelledError
            await self._render(task, force=True)

            # ---- compress ----
            await self._set_stage(task, "compressing", "analyzing input")
            job = self.manager.create_from_path(self._options(chat_id), src, src_ext)
            task.job_id = job.id
            self.manager.start(job)

            last_stage = None
            while job.status not in ("done", "error", "cancelled"):
                await asyncio.sleep(1.0)
                if task.cancelled:
                    await self.manager.cancel(job.id)
                    raise asyncio.CancelledError
                task.progress = job.progress
                task.detail = job.stage
                elapsed = max(0.001, time.time() - task.stage_started)
                if task.progress > 1:
                    total_est = elapsed / (task.progress / 100)
                    task.eta = _fmt_time(max(0.0, total_est - elapsed))
                    dur = getattr(job.input_meta, "duration", 0) or 0
                    if dur:
                        done_media = dur * task.progress / 100
                        task.speed = f"{done_media / elapsed:.2f}×"
                if job.stage != last_stage:
                    last_stage = job.stage
                    await self._render(task, force=True)
                else:
                    await self._render(task)

            if job.status == "cancelled" or task.cancelled:
                raise asyncio.CancelledError
            if job.status == "error":
                raise RuntimeError(job.error or "compression failed")

            # ---- upload ----
            out_ext = (Path(job.output_meta.path).suffix.lstrip(".")
                       if job.output_meta and job.output_meta.path else "mp4") or "mp4"
            out = job.dst_dir / f"compressed.{out_ext}"
            if not out.exists():
                raise RuntimeError("output file is missing")
            suffix = "converted" if task.kind == "audio" else "compressed"
            final = task_dir / f"{safe_stem}_{suffix}.{out_ext}"
            shutil.move(str(out), str(final))

            savings_line = _size_line(job.original_size, job.output_size)
            plan = getattr(job, "plan", None)
            p = self.prefs(chat_id)
            preset = PRESETS.get(p["preset"], PRESETS["medium"])
            if task.kind == "audio":
                caption = (
                    f"✅ **Conversion complete**\n"
                    f"📄 `{final.name}`\n\n"
                    f"{savings_line}"
                )
            else:
                if plan is not None:
                    if plan.audio_copy:
                        audio_label = "Copy original"
                    elif plan.audio_codec:
                        audio_label = f"{plan.audio_codec.upper()} {plan.audio_bitrate}k"
                    else:
                        audio_label = "No audio"
                    dim_label = f"{plan.target_h}p" if plan.scale_changed else "Original"
                else:
                    dim_label = DIMENSIONS.get(str(p["dimension"]), DIMENSIONS["480"])["label"]
                    audio_label = AUDIO.get(p["audio"], AUDIO["aac_96"])["label"]
                caption = (
                    f"✅ **Compression complete**\n"
                    f"📄 `{final.name}`\n\n"
                    f"{savings_line}\n"
                    f"🎛 {preset['label']} · 📐 {dim_label}\n"
                    f"🔊 {audio_label} · 📁 {out_ext.upper()}"
                )

            await self._set_stage(task, "uploading")
            await self._send_media(chat_id, final, caption, task.kind, task)

            task.progress = 100.0
            await self._delete_message(chat_id, task.status_id)

        except asyncio.CancelledError:
            task.stage = "cancelled"
            await self._render(task, force=True, cancelled=True)
        except Exception as exc:  # noqa: BLE001
            logger.exception("task failed: %s", exc)
            await self._edit_message(
                chat_id, task.status_id,
                f"❌ **Task failed**\n\n`{str(exc)[:350]}`",
                {"inline_keyboard": [[self._btn("🗑 Dismiss", "task:dismiss")]]},
            )
        finally:
            shutil.rmtree(task_dir, ignore_errors=True)
            if job is not None and job.status in ("error", "cancelled"):
                try:
                    shutil.rmtree(job.dst_dir, ignore_errors=True)
                except Exception:
                    pass
