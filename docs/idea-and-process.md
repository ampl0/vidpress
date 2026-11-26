# Idea & Build Process

This document tells the story behind VidPress: the problem, the constraints, the key
decisions, and how the code reached its current shape. It is intended for humans *and*
AI agents who want to understand *why* things are the way they are.

## 1. The idea

**Problem.** Telegram's official Bot API only lets bots download and upload files up to
**20 MB** and compressing a large video usually means the bot fails with a cryptic error.
People share clips all the time and want a simpler "send → get a smaller file" flow,
without installing anything.

**Goal.** A self-hosted bot that:
- accepts a video/audio file in chat,
- re-encodes it with FFmpeg (smaller, right dimensions, chosen audio),
- sends the result back, **even for files >20 MB**,
- feels like a polished product: inline menu, presets, live progress.

**Bonus goals.** Work on a tiny free-tier box (0.5 vCPU / 1 GB RAM), be cheap to run,
and be easy to redeploy.

## 2. The stack (and why)

| Choice | Why |
|---|---|
| Python 3.12 | Fast to build; great async ecosystem |
| FastAPI + Uvicorn | Web UI, REST API, admin panel in one process |
| Telethon (MTProto) | The only way to move files >20 MB with a normal bot account |
| Bot API (aiohttp) | Fallback for small files, simpler, zero extra setup |
| ffmpeg/ffprobe | The actual compression engine (codecs, scaling, audio) |
| asyncio subprocess | Run ffmpeg without blocking the event loop; stream progress |
| Docker | Reproducible deployment |

## 3. The hard problems and how they were solved

### 3.1 Large-file downloads failed ("Download failed: 'result'")

**Symptom.** Anything >20 MB failed. The bot tried Telethon, hit an exception, and
silently fell back to the Bot API's `getFile`, whose response for >20 MB files has
**no `result` key** → `KeyError('result')` → "Download failed: 'result'".

**Root cause.** The code called `TelegramClient.start(bot_token=..., connection_retries=3)`,
but `connection_retries` belongs on the **constructor**, not on `start()` → `TypeError`,
which triggered the Bot API fallback for every large file.

**Fix.**
```python
client = TelegramClient(session, api_id, api_hash, connection_retries=3, ...)
await client.start(bot_token=self.token)
```
Plus defensive guards: `data.get("ok")`, `data.get("result")`, and `logger.exception`
instead of silent fallback. **Lesson:** when a library has a fallback path, log the
fallback reason — don't assume it worked.

### 3.2 Progress never moved

**Symptom.** The bot always showed `0%` until the job ended.

**Root cause.** ffmpeg was never asked for progress. `Job._run` built commands without
`-progress pipe:1`, so `job.progress` stayed `0`.

**Fix.** Add `-progress pipe:1 -nostats` to the command, parse `out_time_us=` lines, and
map elapsed media time → percent. Progress now flows: `ffmpeg → jobs.py → bot_worker.py
→ editMessageText`.

### 3.3 A small box should not run out of memory

**Symptom.** `read_bytes()` loaded whole videos into RAM. A 1 GB free-tier box would die.

**Fix.** `JobManager.create_from_path()` adopts a downloaded file on disk (move, not
read). The `AdaptiveScheduler` derives concurrency from free RAM/disk at runtime
(`free_mem_mb`, `free_disk_gb`).

### 3.4 The "20 MB wall" for Telegram itself

MTProto requires `api_id`/`api_hash`. The bot logs in with the same bot token but uses
the **MTProto session** for transfers. `api_id`/`api_hash` are optional: if absent, the
bot rejects >20 MB files with a clear message instead of failing silently.

## 4. The UX evolution (menu / progress)

The UX is modeled on a reference Tcomp-style bot:

1. **Menu.** Inline keyboard buttons, each with a *current value* (e.g. "🎛 Balanced")
   and a sub-menu of choices. Selecting a choice edits the same message and returns to
   the menu. This is stateless per button: `callback_data` is `menu:*` / `set:kind:value`
   / `quick:key`.
2. **Progress.** A single status message edited in place (bar, %, elapsed, ETA, speed,
   stage). The ticker and renderer are **throttled** (default 30 s) to stay far under
   Telegram's per-chat edit limits while still feeling live.
3. **Quick presets.** Users asked for "super fast, compress in 30–60s" and "small"
   options. We added a **⚡ Quick** sub-menu: bundles set preset + size + fps + audio +
   container + codec in one tap. Encoding benchmarked **~7–12 s per 60 s of 720p** on a
   single core, so 30–60 s is realistic even on 0.5 vCPU.
4. **Queueing.** Users were annoyed by "another task is running". Now each chat has a
   worker that processes a queue; `/status` shows depth; `/cancel` clears it.

## 5. Deployment constraints (Cloady)

- Cloady **upload-source** apps cannot be updated in place: changing code requires
  **delete + re-upload + redeploy** (free tier allows one app).
- Bot credentials are **environment variables**, because `data/settings.json` lives on
  the container and is lost on delete.
- Some env changes need a full stop/start to take effect; redeploy alone may not restart
  the pod. **Lesson:** apply env changes via `PATCH` status `stopped` → `running`.
- The source of truth for this repo is the mirror in this repository; the live app is
  deployed from a tarball/upload, not from git.

## 6. Key implementation notes

- **Two download paths** in `bot_worker.py`: Telethon (MTProto) for large files, Bot
  API for small. `large_enabled = bool(api_id and api_hash)`.
- **Automatic container/audio compatibility** in `engine.py`: WebM auto-encodes
  AAC/copied audio to Opus; MP4 re-encodes Vorbis. The caption reports the *resolved*
  plan (what actually happened), not just what the user clicked.
- **Height-based scaling**: "480p" means height 480 (`max_height=480`, width auto) so
  portrait videos don't get squashed.
- **FPS cap** for quick presets: `max_fps` adds a `fps=...` filter to cut encode time.
- **Retry**: if a quality-mode result isn't smaller, the engine retries once with
  stronger compression (`quality-35`, CRF bumped).

## 7. What's next (ideas)

- Per-user command/usage analytics.
- Send as a Telegram *video note* (round) or with custom thumbnails.
- Two-pass "target size" mode exposed in the menu.
- Webhook polling instead of long-polling.
- Auth/rate-limit for the public REST API.

---

*This file is a living document — update it when the architecture changes.*
