# Admin API

The `/admin` dashboard talks to these endpoints. All require an admin bearer token.

---

## Authentication

```
POST /api/admin/login
Content-Type: application/json
{ "password": "..." }
```

- `200` → `{"token": "..."}` — use as `Authorization: Bearer <token>`.
- `401` → wrong password.
- `403` → admin not configured (no `ADMIN_PASSWORD`).

The token is a short-lived value; the dashboard stores it in `localStorage`.

---

## `GET /api/admin/status`

```json
{
  "admin_enabled": true,
  "bot": { "ready": true, "username": "...", "last_error": null },
  "bot_token_set": true
}
```

## `GET /api/admin/settings`

Returns current live settings (token is masked):

```json
{
  "telegram_bot_token": "130466…SxY8",
  "telegram_bot_token_set": true,
  "telegram_api_base": "https://api.telegram.org",
  "telegram_max_dl_mb": 500,
  "telegram_api_id": 123456,
  "telegram_api_hash": "c6db…",
  "telegram_api_hash_set": true,
  "large_files_enabled": true,
  "progress_interval": 30,
  "queue_max": 10,
  "default_settings": {},
  "bot": { "ready": true }
}
```

## `PUT /api/admin/settings`

Update settings. Any key left out is left unchanged.

```json
{
  "telegram_bot_token": "123456:ABC…",
  "telegram_api_base": "",
  "telegram_max_dl_mb": 500,
  "telegram_api_id": "123456",
  "telegram_api_hash": "…",
  "progress_interval": 30,
  "queue_max": 10,
  "default_settings": { "preset": "normal", "dimension": "720", "format": "mp4" }
}
```

Notes:
- `telegram_bot_token` empty / `"delete"` removes the token and stops the bot.
- `default_settings` accepts only known keys: `preset`, `dimension`, `audio`,
  `format`, `codec`, `fps`, `custom`. Empty object clears defaults.
- After a successful PUT the bot is re-synced live (no redeploy).

## `GET /api/admin/jobs`

List jobs with `stderr_tail`, sizes, savings, progress, stage.

## `POST /api/admin/jobs/{job_id}/cancel`

Cancel a queued/running job.

---

## Health (public)

```
GET /api/health
```
Returns status, version, free mem/disk, `max_parallel`, `available_codecs`,
`admin_enabled`, and `bot` state — used by the dashboard's live refresh.
