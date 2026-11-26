# Bot Commands & Menu

Everything a user can do in Telegram.

---

## Commands

| Command | What it does |
|---|---|
| `/start`, `/help` | Welcome + current settings summary |
| `/settings` | Open the inline settings menu |
| `/quick` | Open the one-tap ⚡ Quick presets menu |
| `/defaults` | Reset *this chat's* settings to the admin-configured defaults |
| `/status` | Show running tasks, queue depth, bot/queue config |
| `/cancel` | Cancel the running task **and** clear this chat's queue |
| `/setcode <ffmpeg args>` | Set a raw custom FFmpeg command (overrides preset/size/audio/format) |
| `/getcode` | Show the current custom command + presets reference |
| `/id` | Print the current chat id |

Sending any **video or audio file** starts compression with the current settings.

---

## Inline menu

The menu is one message with buttons that edit in place. Each button shows the current
value.

```
⚙️ Compression settings
🎛 Preset: Balanced (CRF 28)
📐 Size: 480p
🔊 Audio: AAC 96k
📁 Format: MP4
🎞 Codec: H.264
⚙️ Custom: not set

[🎛 Balanced         ][📐 480p            ]
[🔊 AAC 96k        ][📁 MP4             ]
[🎞 H.264          ][📊 Status          ]
[⚡ Quick presets                        ]
[⚙️ Custom command                       ]
```

### Menu sections
- **🎛 Preset** — High (CRF 23) · Balanced (CRF 28) · Small Size (CRF 32) · Ultra
  (CRF 38) · ⚡ Fast (CRF 30) · 🚀 Super Fast (CRF 36) · Normal Speed (CRF 26).
- **📐 Size** — Original · 240p · 360p · 480p · 640p · 720p · 1080p (height-based).
- **🔊 Audio** — None · Copy · Opus 24/32/48/64k · MP3 64/96/128/192k ·
  AAC 48/64/96/128/192k.
- **📁 Format** — MP4 · MKV · WEBM.
- **🎞 Codec** — H.264 · H.265 · AV1 · VP9.
- **⚡ Quick presets** — bundles (see below).
- **⚙️ Custom command** — shows the current `/setcode` value, with a Clear button.

### Quick presets (⚡)
One tap sets everything (preset + size + fps + audio + format + codec):

| Button | Preset | Size | FPS | Audio |
|---|---|---|---|---|
| 🚀 Super Fast 360p | ultrafast · CRF 36 | 360p | 24 | AAC 48k |
| ⚡ Fast 360p | ultrafast · CRF 30 | 360p | 30 | AAC 64k |
| 🗜 Small 360p | veryfast · CRF 32 | 360p | 30 | AAC 64k |
| 🎞 Normal · Original | medium · CRF 26 | original | source | AAC 128k |
| ⚡ Fast · Original | ultrafast · CRF 30 | original | 30 | AAC 96k |
| 🗜 Small · Original | veryfast · CRF 32 | original | 30 | AAC 64k |

---

## Progress message

While working, one status message is edited in place (throttled to the configured
interval, default 30 s):

```
⚙️ Compressing…
████████░░░░ 66.4%

⏱ 00:22 · ⏳ 00:11 · 🚀 2.27×
🎞 encoding

🎛 Balanced (CRF 28) · 📐 480p
🔊 AAC 96k · 📁 MP4

[📊 Refresh ][❌ Cancel]
```

- **Refresh** forces an immediate re-render.
- **Cancel** stops the job (ends with a Dismiss-only keyboard).
- On success the status message is deleted and the compressed file is sent.
- On failure the status becomes a **Task failed** card with a Dismiss button.

---

## Custom command

`/setcode -c:v libx264 -crf 30 -preset ultrafast` stores those args; the bot then runs:

```
ffmpeg -y -v error -progress pipe:1 -nostats -i <input> <your args> <output>
```

`/getcode` prints the stored command and the current preset reference.
