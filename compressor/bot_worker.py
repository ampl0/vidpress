"""Telegram bot for VidPress.

Features
- interactive quality/settings menu (inline keyboard) with per-user prefs;
- large-file handling via Telethon (MTProto) when api_id/api_hash are set,
  falling back to the standard Bot API otherwise;
- auto deleteWebhook on start to avoid silent 409 polling conflicts.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
from pathlib import Path

import aiohttp

logger = logging.getLogger("vidpress.bot")

DEFAULT_API_BASE = "https://api.telegram.org"
OFFICIAL_DOWNLOAD_MB = 20

CODECS = [("h264", "H.264"), ("hevc", "H.265"), ("av1", "AV1"), ("vp9", "VP9")]
PRESETS = [("fast", "Fast"), ("balanced", "Balanced"), ("small", "Smallest")]
AUDIO = [("auto", "Auto"), ("copy", "Copy"), ("aac", "AAC"), ("opus", "Opus"), ("mp3", "MP3")]
RESOLUTIONS = [
    ("0", "Original"), ("3840", "4K"), ("2560", "1440p"), ("1920", "1080p"),
    ("1280", "720p"), ("854", "480p"), ("640", "360p"),
]
DEFAULTS = {
    "codec": "h264",
    "mode": "quality",
    "quality": 60,
    "target_size_mb": 25,
    "preset": "balanced",
    "audio": "auto",
    "max_dim": 0,  # 0 = original
}

LABELS = {
    "codec": "🎞 Codec", "mode": "📊 Mode", "quality": "✨ Quality",
    "target_size_mb": "📦 Target size", "preset": "⚡ Preset",
    "audio": "🔊 Audio", "max_dim": "🖥 Max resolution",
}


def _codec_label(v): return dict(CODECS).get(v, v)
def _preset_label(v): return dict(PRESETS).get(v, v)
def _audio_label(v): return dict(AUDIO).get(v, v)
def _res_label(v): return dict(RESOLUTIONS).get(str(v), v)


def _pref_label(key, value):
    if key == "codec":
        return _codec_label(value)
    if key == "preset":
        return _preset_label(value)
    if key == "audio":
        return _audio_label(value)
    if key == "max_dim":
        return _res_label(value)
    if key == "mode":
        return "Target size" if value == "size" else "Quality"
    if key == "quality":
        return f"{value}%"
    if key == "target_size_mb":
        return f"{value} MB"
    return str(value)


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
        self._chat_job: dict[int, str] = {}
        self._session: aiohttp.ClientSession | None = None
        self._tg = None  # telethon client (lazy)
        self._prefs_path = data_dir / "chat_prefs.json"
        self._prefs: dict[str, dict] = self._load_prefs()
        self._menu_msg: dict[int, int] = {}

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
        client = TelegramClient(str(session), int(self.api_id), self.api_hash)
        await client.start(bot_token=self.token, connection_retries=3)
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
        cur = self._prefs.setdefault(str(chat_id), {})
        cur[key] = value
        self._save_prefs()

    # ---- bot api helpers ----
    async def _api(self, method, *, json_body=None, data=None, timeout=60):
        if self._session is None:
            raise RuntimeError("bot not started")
        async with self._session.post(
            f"{self.base}/{method}", json=json_body, data=data, timeout=aiohttp.ClientTimeout(total=timeout)
        ) as resp:
            return await resp.json()

    async def _send_message(self, chat_id, text, reply_markup=None):
        try:
            body = {"chat_id": chat_id, "text": text}
            if reply_markup is not None:
                body["reply_markup"] = reply_markup
            await self._api("sendMessage", json_body=body)
        except Exception as exc:  # noqa: BLE001
            logger.warning("sendMessage failed: %s", exc)

    async def _edit_message(self, chat_id, message_id, text, reply_markup=None):
        try:
            body = {"chat_id": chat_id, "message_id": message_id, "text": text}
            if reply_markup is not None:
                body["reply_markup"] = reply_markup
            await self._api("editMessageText", json_body=body)
        except Exception as exc:  # noqa: BLE001
            logger.warning("editMessageText failed: %s", exc)

    async def _answer_callback(self, callback_id, text=None):
        try:
            await self._api("answerCallbackQuery", json_body={"callback_query_id": callback_id, "text": text or ""})
        except Exception:
            pass

    async def _send_video(self, chat_id, path, caption):
        if self.large_enabled:
            try:
                client = await self._ensure_tg()
                await client.send_file(chat_id, str(path), caption=caption, force_document=False)
                return
            except Exception as exc:  # noqa: BLE001
                logger.warning("telethon send failed, falling back to bot api: %s", exc)
        form = aiohttp.FormData()
        form.add_field("chat_id", str(chat_id))
        form.add_field("caption", caption)
        form.add_field("video", path.open("rb"), filename=path.name, content_type="video/mp4")
        await self._api("sendVideo", data=form, timeout=300)

    async def _download(self, file_id, chat_id, message_id, dest):
        if self.large_enabled:
            try:
                client = await self._ensure_tg()
                msg = await client.get_messages(chat_id, ids=message_id)
                if msg and msg.media:
                    await client.download_media(msg, file=str(dest))
                    return dest
                raise RuntimeError("media not found via telethon")
            except Exception as exc:  # noqa: BLE001
                logger.warning("telethon download failed, falling back to bot api: %s", exc)
        # fallback: standard Bot API
        data = await self._api("getFile", json_body={"file_id": file_id})
        fp = data["result"].get("file_path")
        if not fp:
            raise RuntimeError("no file_path")
        url = f"{self.api_base}/file/bot{self.token}/{fp}"
        async with self._session.get(url, timeout=aiohttp.ClientTimeout(total=180)) as resp:
            if resp.status != 200:
                raise RuntimeError("download failed")
            dest.write_bytes(await resp.read())
        return dest

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

    async def _handle(self, upd):
        if "callback_query" in upd:
            await self._handle_callback(upd["callback_query"])
            return
        msg = upd.get("message") or {}
        chat = msg.get("chat") or {}
        chat_id = chat.get("id")
        if not chat_id:
            return
        text = (msg.get("text") or "").strip()
        message_id = msg.get("message_id")

        if text.startswith("/"):
            await self._command(chat_id, text, message_id)
            return

        video = msg.get("video")
        doc = msg.get("document")
        file_id = None
        if video:
            file_id = video.get("file_id")
        elif doc and (doc.get("mime_type") or "").startswith("video/"):
            file_id = doc.get("file_id")
        if not file_id:
            await self._send_message(chat_id, "Send me a video and I'll compress it for you. 📹")
            return
        await self._process_video(chat_id, file_id, message_id)

    # ---- menu / settings ----
    async def _command(self, chat_id, text, message_id):
        cmd = text.split()[0].lower()
        if cmd in ("/start", "/help"):
            await self._send_message(
                chat_id,
                "Hi! Send me a video and I'll compress it.\n\n"
                "• /settings — choose codec, quality, target size, preset, audio & resolution\n"
                "• /defaults — reset your settings\n"
                "• /cancel — stop a running compression",
            )
        elif cmd == "/settings":
            await self._send_menu(chat_id, new=True)
        elif cmd == "/defaults":
            self._prefs.pop(str(chat_id), None)
            self._save_prefs()
            await self._send_message(chat_id, "Settings reset to defaults. ⚙️")
        elif cmd == "/cancel":
            jid = self._chat_job.pop(chat_id, None)
            if jid:
                await self.manager.cancel(jid)
                await self._send_message(chat_id, "Cancelled.")
            else:
                await self._send_message(chat_id, "Nothing to cancel.")

    def _menu_text(self, p):
        mode = "Target size" if p["mode"] == "size" else "Quality"
        lines = [
            "⚙️ Your compression settings",
            "",
            f"🎞 Codec: {_codec_label(p['codec'])}",
            f"📊 Mode: {mode}",
            f"✨ Quality: {p['quality']}%" if p["mode"] == "quality" else f"📦 Target size: {p['target_size_mb']} MB",
            f"⚡ Preset: {_preset_label(p['preset'])}",
            f"🔊 Audio: {_audio_label(p['audio'])}",
            f"🖥 Max resolution: {_res_label(p['max_dim'])}",
            "",
            "Press a button to change it.",
        ]
        return "\n".join(lines)

    def _menu_keyboard(self, p):
        def btn(label, data):
            return {"text": label, "callback_data": data}
        kb = [
            [btn(f"🎞 Codec: {_codec_label(p['codec'])}", "cfg:cycle:codec")],
            [btn(f"📊 Mode: {'Target size' if p['mode']=='size' else 'Quality'}", "cfg:cycle:mode")],
        ]
        if p["mode"] == "quality":
            kb.append([btn("−", "cfg:q:-"), btn(f"✨ {p['quality']}%", "cfg:noop"), btn("+", "cfg:q:+")])
        else:
            kb.append([btn("−", "cfg:s:-"), btn(f"📦 {p['target_size_mb']} MB", "cfg:noop"), btn("+", "cfg:s:+")])
        kb += [
            [btn(f"⚡ Preset: {_preset_label(p['preset'])}", "cfg:cycle:preset")],
            [btn(f"🔊 Audio: {_audio_label(p['audio'])}", "cfg:cycle:audio")],
            [btn(f"🖥 Res: {_res_label(p['max_dim'])}", "cfg:cycle:max_dim")],
            [btn("🔄 Reset defaults", "cfg:reset"), btn("✔️ Done", "cfg:done")],
        ]
        return {"inline_keyboard": kb}

    async def _send_menu(self, chat_id, new=False):
        p = self.prefs(chat_id)
        text = self._menu_text(p)
        kb = self._menu_keyboard(p)
        if new or chat_id not in self._menu_msg:
            await self._send_message(chat_id, text, kb)
        else:
            await self._edit_message(chat_id, self._menu_msg[chat_id], text, kb)

    async def _handle_callback(self, cq):
        chat_id = cq.get("message", {}).get("chat", {}).get("id")
        if not chat_id:
            return
        data = (cq.get("data") or "").split(":")
        if not data or data[0] != "cfg":
            return
        await self._answer_callback(cq["id"])
        p = self.prefs(chat_id)
        action = data[1]
        if action == "cycle":
            key = data[2]
            ordering = {"codec": CODECS, "preset": PRESETS, "audio": AUDIO, "max_dim": RESOLUTIONS,
                        "mode": [("quality", ""), ("size", "")]}
            if key == "mode":
                p["mode"] = "size" if p["mode"] == "quality" else "quality"
            else:
                vals = [v for v, _ in ordering[key]]
                idx = vals.index(_coerce(p[key], vals, key)) if str(p[key]) in [str(v) for v in vals] else -1
                p[key] = vals[(idx + 1) % len(vals)]
            self.set_pref(chat_id, key, p[key])
        elif action == "q":
            step = 5 if data[2] == "+" else -5
            p["quality"] = min(100, max(5, p["quality"] + step))
            self.set_pref(chat_id, "quality", p["quality"])
        elif action == "s":
            step = 5 if data[2] == "+" else -5
            p["target_size_mb"] = max(1, min(2000, p["target_size_mb"] + step))
            self.set_pref(chat_id, "target_size_mb", p["target_size_mb"])
        elif action == "reset":
            self._prefs.pop(str(chat_id), None)
            self._save_prefs()
            p = self.prefs(chat_id)
        elif action == "done":
            self._menu_msg.pop(chat_id, None)
            try:
                await self._api("deleteMessage", json_body={"chat_id": chat_id,
                                                           "message_id": cq["message"]["message_id"]})
            except Exception:
                pass
            await self._send_message(chat_id, "Settings saved. ✅ Send me a video to compress.")
            return
        self._menu_msg[chat_id] = cq["message"]["message_id"]
        await self._edit_message(chat_id, cq["message"]["message_id"], self._menu_text(p), self._menu_keyboard(p))

    # ---- processing ----
    async def _process_video(self, chat_id, file_id, message_id):
        (self.data_dir / "bot_tmp").mkdir(parents=True, exist_ok=True)
        if self.large_enabled:
            # Large-file path: Telethon downloads by chat/message id, so no
            # Bot-API metadata call is needed (and none can fail on big files).
            await self._send_message(chat_id, "Downloading…")
            src = self.data_dir / "bot_tmp" / f"{chat_id}-{message_id}.vid"
            try:
                await self._download(file_id, chat_id, message_id, src)
            except Exception as exc:  # noqa: BLE001
                await self._send_message(chat_id, f"Download failed: {exc}")
                return
            await self._compress_and_send(chat_id, src)
            return

        # Standard Bot-API path: pre-check size via getFile.
        try:
            data = await self._api("getFile", json_body={"file_id": file_id})
            ok = bool(data.get("ok"))
            info = data.get("result") or {}
            fp = info.get("file_path")
            size = int(info.get("file_size", 0) or 0)
        except Exception as exc:  # noqa: BLE001
            await self._send_message(chat_id, f"Could not retrieve the video: {exc}")
            return
        if not ok or not fp:
            await self._send_message(chat_id, "Could not retrieve the video (missing metadata).")
            return

        limit = min(self.max_dl_mb * 1024 * 1024, OFFICIAL_DOWNLOAD_MB * 1024 * 1024)
        if size > limit:
            await self._send_message(
                chat_id,
                "This video is larger than the 20 MB Bot API limit. "
                "Configure api_id/api_hash in the VidPress admin to enable large files.",
            )
            return

        await self._send_message(chat_id, "Downloading…")
        src = self.data_dir / "bot_tmp" / f"{chat_id}-{message_id}{Path(fp).suffix or '.mp4'}"
        try:
            await self._download(file_id, chat_id, message_id, src)
        except Exception as exc:  # noqa: BLE001
            await self._send_message(chat_id, f"Download failed: {exc}")
            return
        await self._compress_and_send(chat_id, src)

    async def _compress_and_send(self, chat_id, src):
        p = self.prefs(chat_id)
        opts = {
            "codec": p["codec"],
            "mode": p["mode"],
            "quality": p["quality"],
            "target_size_mb": p["target_size_mb"],
            "preset": p["preset"],
            "audio": p["audio"],
            "max_width": int(p["max_dim"]),
            "max_height": int(p["max_dim"]),
            "single_pass": False,
        }
        job = self.manager.create(opts, src.read_bytes(), src.suffix or ".mp4")
        self._chat_job[chat_id] = job.id
        self.manager.start(job)
        await self._send_message(chat_id, f"Compressing… (job {job.id})")

        while job.status not in ("done", "error", "cancelled"):
            await asyncio.sleep(2)
            if not self.is_running():
                return

        if job.status == "done" and job.output_meta:
            out = job.dst_dir / f"compressed.{Path(job.output_meta.path).suffix.lstrip('.')}"
            if out.exists():
                savings = max(0, job.savings_pct)
                caption = (
                    f"✅ Compressed!\n"
                    f"{job.original_size/1048576:.1f} MB → {job.output_size/1048576:.1f} MB "
                    f"({savings:.0f}% smaller)"
                )
                try:
                    await self._send_video(chat_id, out, caption)
                except Exception as exc:  # noqa: BLE001
                    await self._send_message(chat_id, f"Compression done but sending failed: {exc}")
                return
        await self._send_message(chat_id, f"❌ {job.error or 'Compression failed.'}")


def _coerce(value, vals, key):
    if key in ("max_dim",):
        return int(value) if str(value).isdigit() else 0
    if key in ("quality", "target_size_mb"):
        return int(value) if str(value).isdigit() else (60 if key == "quality" else 25)
    return str(value)
