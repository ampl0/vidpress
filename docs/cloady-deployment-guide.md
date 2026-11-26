# Cloady Deployment & Workflow Guide

How to develop, build, and push VidPress on [Cloady](https://cloady.com) as a hosted app.

---

## 0. The tl;dr

Cloady **upload-source** apps cannot be updated in place. Changing the code requires:

```
1. upload a fresh source tarball (multipart) → get an uploadId
2. delete the existing app
3. create a new app from that upload, re-supplying ALL env vars/secrets
4. wait for it to become live, verify health
```

Bot state (admin password, bot token, api_id/hash) lives in **env vars**, because the
container's `data/settings.json` and `*.session` are ephemeral and lost on delete.

---

## 1. Prerequisites

- A Cloady API token (from your account). Keep it secret.
- The Cloady API base: `https://cloady.com/api`
- Workspace slug (e.g. `workspace-eo5dcd`) and app slug (`vidpress`).
- The source directory (this repo) on disk.

---

## 2. Build the source upload

Cloady's `/api/workspaces/{ws}/uploads` accepts **multipart/form-data** with:
- one `file` part per project file, and
- a `paths` field = JSON array of each file's path (same order).

A single `.zip` is also accepted as a whole project.

Example (Python):

```python
import json, os, subprocess, requests

tok = "cldy_..."  # your Cloady token
paths = sorted(p for p in subprocess.check_output(
    ["git", "ls-files"], text=True).split() if os.path.isfile(p))

files = [("file", (os.path.basename(p), open(p, "rb"), "application/octet-stream"))
         for p in paths]
r = requests.post(
    "https://cloady.com/api/workspaces/{ws}/uploads",
    headers={"Authorization": f"Bearer {tok}"},
    files=files,
    data={"paths": json.dumps(paths)},
    timeout=180,
)
upload_id = r.json()["uploadId"]
```

> 💡 Use `git ls-files` so you never upload `work/`, `data/`, `*.session`, or venv
> artifacts — they are gitignored.

---

## 3. Deploy (create the app)

`POST /api/workspaces/{ws}/deploy`

```python
payload = {
    "region": "eu1",
    "env": "production",
    "name": "vidpress",
    "port": 8000,
    "price": 0,                        # free tier
    "values": {
        "LOG_LEVEL": "INFO",
        "TELEGRAM_BOT_TOKEN": "1304663311:...",
        "TELEGRAM_API_ID": "1461495",
        "TELEGRAM_API_HASH": "c6db...",
        "BOT_MAX_DL_MB": "500",
        "MAX_CONCURRENT": "2",
        "PER_JOB_MEM_MB": "160",
        "ADMIN_PASSWORD": "your-admin-password",
    },
    "secretKeys": ["TELEGRAM_BOT_TOKEN", "ADMIN_PASSWORD"],
    "source": {"type": "upload", "uploadId": upload_id},
}
r = requests.post(
    "https://cloady.com/api/workspaces/{ws}/deploy",
    headers={"Authorization": f"Bearer {tok}"},
    json=payload, timeout=180,
)
# r.json()["app"]["endpoints"][0]["url"] → live URL
```

Key points:
- `secretKeys` lists which `values` are stored encrypted.
- The response includes the new live URL (may change every redeploy on free tier).
- Include **every** required env var — deletion wipes them.

---

## 4. Wait for readiness & verify

Poll `GET /api/workspaces/{ws}/apps/{app}/status?env=production` until
`status == "running"`, then hit the app's health:

```bash
curl -s https://<app-url>/api/health
# expect: {"status":"ok", ..., "bot":{"ready":true, ...}, "admin_enabled":true}
```

---

## 5. Updating code (the delete → redeploy cycle)

```python
# 1) delete
requests.delete("https://cloady.com/api/workspaces/{ws}/apps/vidpress?env=production",
               headers={"Authorization": f"Bearer {tok}"})

# 2) re-upload (step 2 of this guide)

# 3) re-deploy (step 3 of this guide)
```

**Never skip step 3's env vars** — the running app's settings are gone with the delete.

---

## 6. Changing environment variables *without* code changes

Env changes are applied via the app PATCH endpoint. Some changes (e.g. adding a new
secret) require a full **stop → start**; redeploy alone may not restart the pod.

```python
# stop
requests.patch(
    "https://cloady.com/api/workspaces/{ws}/apps/vidpress?env=production",
    headers={"Authorization": f"Bearer {tok}"},
    json={"status": "stopped"},
)
# start (applies changes)
requests.patch(... , json={"status": "running"})
```

Secret/password changes follow the same stop/start pattern.

---

## 7. Runtime inspection (exec)

Cloady's `/api/workspaces/{ws}/apps/{app}/exec?env=production` runs a command in the
running container:

```json
{ "component": "app", "command": "cat /app/work/...", "timeoutMs": 30000 }
```

Useful for debugging:
- `ls -la /app/data` — settings, session files
- `python3 -m compileall -q /app/compressor` — syntax check on the live code
- `python3 -c "from compressor.bot_worker import TelegramBot; print('ok')"` — import check

---

## 8. Logs

`GET /api/workspaces/{ws}/apps/{app}/services/app/logs?env=production&limit=200`

Watch for:
- `telegram bot started as @...` (bot up),
- `Update handling failed` / `poll error` (bot issues),
- uvicorn access lines for the REST API.

---

## 9. Handy gotchas

| Gotcha | Symptom | Fix |
|---|---|---|
| Bot not ready after deploy | health shows `bot.ready: false` | Token top set or wrong; set `TELEGRAM_BOT_TOKEN` |
| Files >20 MB fail | "Download failed: 'result'" | Set `TELEGRAM_API_ID` + `TELEGRAM_API_HASH` (MTProto) |
| Env change ignored | old value still in `/api/admin/settings` | stop → start (full restart) |
| `Auth` 401 on admin | wrong password | set `ADMIN_PASSWORD` as a secret env var |
| App stuck "deploying" | — | wait; if >5 min, re-check `status` + logs |
| Port wrong | 502 on live URL | deploy with `"port": 8000` |

---

## 10. Local → Cloady checklist

1. `git add`/commit locally; run `python -m compileall -q compressor main.py`.
2. Run the local smoke tests (see the AI-agent guide, §7).
3. `git ls-files` → build upload.
4. Deploy with full env block + secrets.
5. Poll status, then check `/api/health` and `/admin`.
6. Test a real >20 MB file through the bot.

---

*Pair with [ai-agent-reproducer-guide.md](ai-agent-reproducer-guide.md) for the build
and test details.*
