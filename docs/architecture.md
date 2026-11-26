# Architecture

VidPress is a single Python service exposing three surfaces: a **Telegram bot**, a
**web UI + REST API**, and an **admin dashboard** — all sharing one async compression
pipeline.

```
                 ┌─────────────────────────────────────────────┐
   Telegram ───▶ │  bot_worker.py (TelegramBot)               │
                 │  polling · menus · callbacks · queue · UI  │
                 │  download (Telethon MTProto / Bot API)      │
                 └──────────────┬──────────────────────────────┘
                                │  file on disk
                                ▼
                 ┌─────────────────────────────────────────────┐
                 │  jobs.py (JobManager) → scheduler.py        │
                 │  AdaptiveScheduler: concurrency from RAM/disk│
                 └──────────────┬──────────────────────────────┘
                                │  Job._run() in subprocess
                                ▼
                 ┌─────────────────────────────────────────────┐
                 │  probe.py (ffprobe) → engine.py             │
                 │  build_plan → build_command → ffmpeg        │
                 │  -progress pipe:1 parsed → progress          │
                 └──────────────┬──────────────────────────────┘
                                │  compressed output file
                                ▼
                 ┌─────────────────────────────────────────────┐
                 │  bot_worker.py (final upload + caption)     │
                 └─────────────────────────────────────────────┘
```

---

## Process lifecycle (one job)

1. **Receive** — `_handle()` detects a media update and enqueues a `BotTask`.
2. **Worker** — `_chat_worker(chat_id, queue)` pops the next task, sets it running.
3. **Download** — `_download()`: Telethon (large) or Bot API (small); progress
   callback feeds the UI.
4. **Plan** — `probe(Path)` → `MediaInfo`; `build_plan` → `JobPlan`.
5. **Encode** — `build_command` → `asyncio.create_subprocess_exec(ffmpeg ...)`;
   `out_time_us=` lines → percent.
6. **Retry** — if quality output is not smaller, retry once with stronger compression.
7. **Upload** — `_send_media()` returns the file with a caption of the *resolved*
   settings and savings.
8. **Cleanup** — temp dir removed; status message deleted once the result is sent.

---

## Module responsibilities

### `compressor/bot_worker.py` (the bot)
- `TelegramBot` class: credentials, Bot API helpers (`_send`, `_edit_message`,
  `_delete_message`, `_answer_callback`), Telethon lazy client.
- Pull model: `_poll()` → `_safe_handle(upd)` → `_handle()` / `_handle_callback()`.
- State: `_prefs` (per-chat settings), `_queues`/`_workers`/`_running` (per-chat
  queue), `_procs`.

### `compressor/engine.py` (pure, testable)
- `CompressOptions` dataclass = the "what".
- `JobPlan` dataclass = the resolved "how".
- `build_plan(media, opts)` returns a plan. `build_command(plan, media, src, dst)`
  returns ffmpeg argv. `run_ffmpeg(...)` is an async runner that parses progress.

### `compressor/jobs.py` (job model)
- `Job` tracks status/stage/progress/cmd/stderr/error/sizes/savings/elapsed.
- `JobManager` owns uploads/outputs dirs, a `Job` registry, and `create` /
  `create_from_path` (the disk-safe path for bot files).

### `compressor/scheduler.py` (adaptive concurrency)
- `AdaptiveScheduler` caps concurrent encodes by `min(max_concurrent, mem_slots,
  disk_slots)`; runs each job through `_guarded` so an exception becomes a job
  error, not a crash.

### `compressor/probe.py`
- Thin `ffprobe` wrapper → `MediaInfo` / `StreamInfo`. `ProbeError` when unreadable.

### `compressor/capabilities.py`
- `available_video_codecs()` returns encoders present in the ffmpeg build
  (`libx264`, `libx265`, `libsvtav1`, `libvpx-vp9`).

### `compressor/settings.py`
- `Settings` = thread-safe JSON key/value store (`data/settings.json`).

### `compressor/auth.py`
- `admin_enabled()`, `check_admin_password()`, `make_token()`, `verify_token()`.

### `main.py`
- FastAPI app with lifespan-managed `JobManager` + `BotController`.
- `BotController.sync()` reconciles env/settings with the live bot (recreates the bot
  when token or `api_id`/`api_hash` change).
- Routes: health, jobs, download, admin login/status/settings/jobs.

---

## Data flow for settings

```
env vars ─▶ BotController (settings_store) ─▶ TelegramBot(...)
  (secrets)      ▲        │
                 │        │ saved
                 │        ▼
                 │   data/settings.json
                 │
  /api/admin/settings (GET/PUT) <─ admin dashboard
```

Admin PUT writes to `settings_store`, then calls `BOT.sync()` so the bot adopts the new
values live (no redeploy needed for `progress_interval`, `queue_max`, defaults).

---

## Concurrency model

- **FastAPI** serves the web/API on the event loop.
- **Bot polling** runs as a long-lived `asyncio` task per bot.
- **Each chat's queue** is a coroutine worker; `_process` does the file work.
- **Encode jobs** run as subprocesses via the scheduler (never blocking the loop).
