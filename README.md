<div align="center">

# 🎬 VidPress

**Smart FFmpeg video compressor with a Telegram bot, web UI, and REST API.**

Compress, convert, and resize video and audio right from Telegram — or the browser —
with an inline settings menu, live progress bars, and 30–60s "quick" presets.

</div>

---

**Keywords / topics:** `video-compressor` `ffmpeg` `telegram-bot` `telethon` `fastapi`
`python` `mtproto` `video-encoding` `h264` `hevc` `av1` `vp9` `audio-converter`
`inline-keyboard` `ffmpeg-wrapper` `video-downloader` `docker` `cloady` `compression`
`telegram-download` `presets` `queue`

---

## Why VidPress?

- **Send any video/audio file to a bot**, get back a smaller, re-encoded file — no
  account, no app install.
- **Large files (`>20 MB`) actually work.** The bot uses Telethon (MTProto) to
  download/upload beyond the 20 MB Bot API limit, with the standard Bot API as a
  fallback.
- **A real inline menu** (Tcomp-style): pick preset, output size, audio profile,
  container, and codec — or set a raw custom FFmpeg command.
- **Live progress**: bar, % , elapsed, ETA, and transfer/encode speed (throttled to
  avoid Telegram rate limits).
- **Per-chat queueing** — send several files; they process one after another instead
  of being rejected.
- **Self-hosted admin dashboard** to change bot settings without redeploying.

---

## Features

### Bot (Telegram inline flow)
| Feature | Description |
|---|---|
| Presets | High · Balanced · Small · Ultra · **⚡ Fast** · **🚀 Super Fast** · **Normal Speed** |
| Size | Original / none, 240p, 360p, 480p, 640p, 720p, 1080p (height-based scaling) |
| Audio profiles | None, Copy, Opus 24/32/48/64k, MP3 64/96/128/192k, AAC 48/64/96/128/192k |
| Containers | MP4, MKV, WEBM (container/audio compatibility handled automatically) |
| Codecs | H.264, H.265/HEVC, AV1, VP9 (auto-fallback based on ffmpeg build) |
| ⚡ Quick presets | One-tap bundles (360p **and** original-width) → ~30–60s for short clips |
| Custom commands | `/setcode <ffmpeg args>` / `/getcode` — full control |
| Progress | Bar · % · elapsed · ETA · speed · stage · Refresh / Cancel / Dismiss buttons |
| Queueing | Per-chat queue (cap configurable), position notice, `/cancel` clears queue |
| Per-user prefs | Persisted per chat; `/defaults` resets to admin-configured defaults |

### Web UI & REST API
- `GET /` — simple web UI (upload a file in the browser).
- `GET /api/health` — health, codecs, bot state, admin status.
- `POST/GET /api/jobs`, `GET /api/jobs/{id}`, `DELETE /api/jobs/{id}` — job API.
- `GET /api/download/{id}` — download the compressed result.
- `/admin` — password-protected dashboard (bot token, api_id/hash, max download,
  progress interval, queue size, default settings, live job table).

---

## Architecture

```
message (Telegram) ─▶ bot_worker.py (TelegramBot)
   │  download (Telethon MTProto | Bot API fallback)
   ▼
compressor/jobs.py (JobManager) ─▶ scheduler.py (AdaptiveScheduler)
   │                                    │ capacity from RAM/disk
   ▼                                    ▼
probe.py (ffprobe) ─▶ engine.py (build_plan / build_command) ─▶ ffmpeg
   │                                        output file
   ▼
bot_worker.py (upload back to Telegram, caption with real result)
```

| Module | Responsibility |
|---|---|
| `compressor/bot_worker.py` | Telegram polling, inline menus, callbacks, queue workers, progress UI, Telethon/MTProto transfer |
| `compressor/engine.py` | Compression plan (codec/CRF/scale/audio/container) and ffmpeg command builder; parses `-progress` |
| `compressor/jobs.py` | Job model + manager; runs jobs in a subprocess, tracks progress/savings, retry |
| `compressor/scheduler.py` | Adaptive concurrency based on free memory/disk |
| `compressor/probe.py` | `ffprobe` wrapper → media info (codec, resolution, fps, audio) |
| `compressor/capabilities.py` | Enumerate available ffmpeg encoders at runtime |
| `compressor/settings.py` | Persistent JSON settings store (token, defaults, etc.) |
| `compressor/auth.py` | Admin login + bearer token |
| `main.py` | FastAPI app: REST API, web UI, admin API, bot lifecycle |

See the [docs folder](docs/) for deep dives.

---

## Quick Start (local)

Requires Python 3.12 and `ffmpeg` on `PATH`.

```bash
git clone https://github.com/ampl0/vidpress.git
cd vidpress

python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# optional, if you want a bot
export TELEGRAM_BOT_TOKEN='123456:ABC-...'
export TELEGRAM_API_ID='123456'            # from my.telegram.org
export TELEGRAM_API_HASH='...'
export BOT_MAX_DL_MB='500'

uvicorn main:app --host 0.0.0.0 --port 8000
```

Then open http://localhost:8000 or send a file to your bot.

### Environment variables

| Variable | Default | Purpose |
|---|---|---|
| `TELEGRAM_BOT_TOKEN` | — | Bot token (from @BotFather) |
| `TELEGRAM_API_ID` / `TELEGRAM_API_HASH` | — | Telethon/MTProto credentials — **required for files >20 MB** |
| `TELEGRAM_API_BASE` | `https://api.telegram.org` | Bot API base (for a Local Bot API server) |
| `BOT_MAX_DL_MB` | `500` | Max download size in MB |
| `PROGRESS_INTERVAL` | `30` | Seconds between progress edits |
| `BOT_QUEUE_MAX` | `10` | Max queued items per chat |
| `MAX_CONCURRENT` | `4` | Max parallel encodes |
| `MAX_INPUT_MB` | `512` | Max input size for the REST API |
| `PER_JOB_MEM_MB` | `160` | Estimated memory per encode (drives scheduler) |
| `ADMIN_PASSWORD` | — | Password for `/admin` (secret) |
| `PORT` | `8000` | HTTP port |

---

## Docker

```bash
docker build -t vidpress .
docker run --rm -p 8000:8000 \
  -e TELEGRAM_BOT_TOKEN='...' \
  -e TELEGRAM_API_ID='...' \
  -e TELEGRAM_API_HASH='...' \
  -e ADMIN_PASSWORD='...' \
  vidpress
```

---

## Docs

- [Idea & build process](docs/idea-and-process.md) — why it exists and how it got here.
- [AI-agent reproduction guide](docs/ai-agent-reproducer-guide.md) — step-by-step blueprint to rebuild or extend this.
- [Cloady deployment & workflow](docs/cloady-deployment-guide.md) — deploy/edit/push on Cloady.
- [Architecture](docs/architecture.md) — module-by-module breakdown.
- [Bot commands & menu](docs/bot-commands.md) — the full Telegram UX.
- [Admin API](docs/admin-api.md) — endpoints for managing settings.

---

## License

[MIT](LICENSE)
