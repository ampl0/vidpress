# AI-Agent Reproduction Guide

This guide is written so that **an AI coding agent** (or a human) can reproduce, extend,
or debug VidPress — or rebuild something equivalent from scratch — without guessing.

It is deliberately prescriptive: read the files listed, verify each invariant, and keep
the constraints in mind.

---

## 0. Mental model (read this first)

VidPress is **three surfaces over one engine**:

1. **Telegram bot** (`compressor/bot_worker.py`) → talks to Telegram, moves files,
   renders menus/progress.
2. **Compression engine** (`compressor/engine.py` + `jobs.py` + `probe.py`) → bridges
   input file → ffmpeg command → output file, with progress.
3. **Web/API/admin** (`main.py`, `static/`) → FastAPI app; a thin controller for the
   same `JobManager`.

The **critical invariant**: *never read a whole video into memory*. Files move on disk.
Jobs run in a subprocess. Progress comes from `ffmpeg -progress pipe:1`.

---

## 1. Repo layout

```
vidpress/
  main.py                 # FastAPI app, REST + admin API, bot lifecycle
  Dockerfile              # python:3.12-slim + ffmpeg + uvicorn
  requirements.txt
  static/                 # web UI + admin dashboard (plain JS)
  compressor/
    bot_worker.py         # Telegram bot: polling, menus, callbacks, queue, progress
    engine.py             # CompressOptions, build_plan, build_command, run_ffmpeg
    jobs.py               # Job & JobManager (subprocess, progress, retry)
    scheduler.py          # AdaptiveScheduler (concurrency from RAM/disk)
    probe.py              # ffprobe → MediaInfo
    capabilities.py       # runtime ffmpeg encoder detection
    settings.py           # persistent JSON settings
    auth.py               # admin password + bearer token
  docs/                   # these docs
```

---

## 2. Run locally (verify it works)

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
uvicorn main:app --host 0.0.0.0 --port 8000
```

Smoke test:

```bash
# health must show no bot error, admin disabled unless ADMIN_PASSWORD set
curl -s http://localhost:8000/api/health
```

**Lint/compile check** the agent should always run after edits:

```bash
python -m compileall -q compressor main.py && echo OK
```

---

## 3. The compression engine (deep dive)

### Input
`probe.py` returns a `MediaInfo` dataclass: container, duration, size, video stream
(codec/width/height/fps/bitrate), audio streams.

### Plan
`engine.build_plan(media, opts) -> JobPlan` decides:
- custom command? (overrides everything, `plan.custom=True`)
- audio-only? (`media.video is None` → `plan.audio_only=True`)
- scale target: `max_height` → height-based; `max_fps` → fps filter
- codec/container resolution (with auto-fallback via `ensure_codec`)
- container/audio compatibility (WebM → Opus; MP4 → re-encode Vorbis)
- quality mode (single pass CRF) vs size mode (2-pass bitrate)
- CRF offset table: `CRF_OFFSET = {"h264":0,"hevc":4,"av1":12,"vp9":5}`

### Command
`engine.build_command(plan, media, src, dst, pass_no=None) -> [str]` builds the
ffmpeg argv. **Always** includes `-progress pipe:1 -nostats` for video/audio jobs.

### Execute
`jobs.Job._run()`:
1. probe input,
2. build plan,
3. run ffmpeg via `asyncio.create_subprocess_exec(...)`,
4. pump stdout, parse `out_time_us=` → progress callback,
5. if quality mode and output ≥ input and quality>18 → **retry once** stronger
   (`quality-35`, CRF+8),
6. set `status=done`, `savings_pct`.

**Cancellation:** scheduler sets `job._cancelled=True` and `terminate()`s the subprocess.

---

## 4. The bot (deep dive)

`TelegramBot(token, manager, data_dir, api_base, max_dl_mb, api_id, api_hash,
progress_interval, queue_max, defaults)`.

### Update loop
`_poll()` long-polls `getUpdates` with `allowed_updates=[message, callback_query]`;
each update is handled in `_safe_handle(upd)`.

### Media detection
`_media_of(msg)` returns `(file_id, kind)` for video/audio/document. `_file_size`,
`_orig_name` produce metadata.

### Large-file transfer
- `_ensure_tg()` lazily builds a Telethon client **with `connection_retries` in the
  constructor** (see docs/idea-and-process.md §3.1).
- `_download()` uses Telethon when `large_enabled`, falling back to Bot API
  `getFile` (guarded: `result.file_path` may be absent for >20 MB).
- `_send_media()` sends via Telethon (with progress callback) or Bot API multipart.

### Menu / callbacks
Callback data scheme:
```
menu:preset | menu:size | menu:audio | menu:format | menu:codec | menu:quick | menu:custom | menu:status | menu:back
set:<kind>:<value>        # choose an option
quick:<quick_key>         # apply a bundle (sets several prefs)
task:refresh | task:cancel | task:dismiss
```

Per-chat prefs stored in `data/chat_prefs.json` (`_prefs`). `prefs(chat_id)` merges
`DEFAULTS` < `global_defaults` (admin) < per-chat.

`_options(chat_id)` → dict passed to `JobManager.create_from_path`.

### Queueing
`_handle()` appends a `BotTask` to `_queues[chat_id]` and ensures a single
`_chat_worker` drains it (prevents "already running" rejection). `_cancel_chat` clears
the queue too.

### Progress
`_process()` sends one status message, then updates it via `_render(task, force=...)`.
Rendering is throttled to `progress_interval` seconds (default 30) to respect Telegram
edit limits; stage changes force immediate re-render. `_set_stage()` resets progress
and stage timers.

---

## 5. Extending (recipes the agent will most likely be asked for)

### Add a preset
1. `PRESETS` in `bot_worker.py` → `{key: {label, crf, speed, quality}}`.
2. (optional) a `QUICK_PRESETS` entry for a one-tap bundle.
3. Add the key to the preset `<select>` in `static/admin.html` if it should be an
   admin default.

### Add an audio profile
1. `AUDIO` in `bot_worker.py` → `{key: {codec, bitrate, label}}`.
2. Reference `AUDIO_CODECS` + `_audio_args` in `engine.py` map codec label → ffmpeg
   args.

### Add a container
1. `FORMATS` in `bot_worker.py`.
2. `build_plan` container/codec resolution + `_audio_args` compatibility rules in
   `engine.py`.

### Change progress cadence
The admin dashboard already exposes `progress_interval` (5–300 s). It is read by the
bot constructor and stored in settings; no code change needed.

---

## 6. Rules of thumb (invariants)

1. **Never load a whole media file into RAM** — always stream/`shutil.move`.
2. **Always `-progress pipe:1`** for encode jobs; feed `out_time_us` → percent.
3. **Constructor args ≠ `start()` args** for Telethon (`connection_retries`,
   `request_retries`, `timeout`, `flood_sleep_threshold` go in the constructor).
4. **Guard Bot API JSON** (`data.get("ok")`, `data.get("result")`) — never assume keys.
5. **Throttle `editMessageText`** (per-chat edit rate limit).
6. **One fav per chat** — reuse the same status `message_id`.
7. **Clean up** temp dirs and the Telethon session on `stop()`.
8. **Keep the web UI a thin controller** — don't duplicate bot logic in `main.py`.
9. **Compile-check** after every edit: `python -m compileall -q compressor main.py`.
10. **Document env vars** in README when adding one.

---

## 7. Verification checklist (before you call it "done")

- [ ] `compileall` passes.
- [ ] `curl /api/health` → `status: ok`, bot `ready: true` (when token set).
- [ ] A **small file** compresses end-to-end via the bot (menu → send → progress →
      result).
- [ ] A **>20 MB file** compresses via Telethon (or gracefully errors if api_id missing).
- [ ] WebM with AAC input → Opus (auto-converted); MP4 still plays.
- [ ] Queue: send 3 files → 3 results, no "already running" rejection.
- [ ] `/api/admin/login` returns 200 (good password) / 401 (bad).
- [ ] Admin PUT settings → value reflected in `GET /api/admin/settings`, bot stays ready.

---

*See [Cloady-deployment-guide.md](cloady-deployment-guide.md) for how to ship this to
a Cloady app.*
