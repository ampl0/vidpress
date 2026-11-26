"""VidPress — smart FFmpeg video compressor (web UI + REST API + admin)."""
from __future__ import annotations

import json
import logging
import os
import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, File, Form, Header, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from compressor import auth
from compressor.bot_worker import TelegramBot
from compressor.capabilities import available_video_codecs
from compressor.jobs import JobManager, MAX_INPUT_MB
from compressor.probe import ProbeError
from compressor.scheduler import free_disk_gb, free_mem_mb
from compressor.settings import Settings

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("vidpress")

BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"
WORKDIR = Path(os.getenv("WORKDIR", str(BASE_DIR / "work")))
DATA_DIR = Path(os.getenv("DATA_DIR", str(BASE_DIR / "data")))
MAX_CONCURRENT = int(os.getenv("MAX_CONCURRENT", "4"))
STARTED_AT = time.time()
VERSION = "1.1.0"

settings_store = Settings(DATA_DIR / "settings.json")

manager: JobManager = None  # type: ignore


def _admin_token(authorization: str | None = Header(default=None)) -> str:
    if not authorization:
        raise HTTPException(status_code=401, detail="Authentication required")
    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer" or not token:
        raise HTTPException(status_code=401, detail="Invalid authorization header")
    if not auth.verify_token(token):
        raise HTTPException(status_code=401, detail="Invalid or expired token")
    return token


class BotController:
    def __init__(self, job_manager: JobManager):
        self.manager = job_manager
        self.bot: TelegramBot | None = None

    def token(self) -> str:
        t = settings_store.get("telegram_bot_token", "") or os.getenv("TELEGRAM_BOT_TOKEN", "")
        return str(t).strip()

    def api_base(self) -> str:
        return str(
            settings_store.get("telegram_api_base", "") or os.getenv("TELEGRAM_API_BASE", "")
        ).strip() or "https://api.telegram.org"

    def max_dl_mb(self) -> int:
        raw = settings_store.get("telegram_max_dl_mb") or os.getenv("BOT_MAX_DL_MB", "200")
        try:
            return max(1, int(raw))
        except (TypeError, ValueError):
            return 200

    def api_id(self) -> int | None:
        raw = settings_store.get("telegram_api_id") or os.getenv("TELEGRAM_API_ID", "")
        try:
            return int(raw) if str(raw).strip() else None
        except (TypeError, ValueError):
            return None

    def api_hash(self) -> str | None:
        raw = settings_store.get("telegram_api_hash") or os.getenv("TELEGRAM_API_HASH", "")
        return str(raw).strip() or None

    async def sync(self) -> None:
        token = self.token()
        if token and (self.bot is None or self.bot.token != token):
            await self.stop()
            self.bot = TelegramBot(
                token, self.manager, DATA_DIR,
                self.api_base(), self.max_dl_mb(), self.api_id(), self.api_hash(),
            )
            try:
                await self.bot.start()
            except Exception as exc:  # noqa: BLE001
                logger.warning("telegram bot failed to start: %s", exc)
                self.bot = None
        elif not token and self.bot is not None:
            await self.stop()
        elif self.bot is not None:
            changed = (
                self.bot.api_base != self.api_base()
                or self.bot.api_id != self.api_id()
                or self.bot.api_hash != self.api_hash()
            )
            if changed or self.bot.token != token:
                await self.stop()
                self.bot = TelegramBot(
                    token, self.manager, DATA_DIR,
                    self.api_base(), self.max_dl_mb(), self.api_id(), self.api_hash(),
                )
                try:
                    await self.bot.start()
                except Exception as exc:  # noqa: BLE001
                    logger.warning("telegram bot failed to start: %s", exc)
                    self.bot = None
            else:
                self.bot.max_dl_mb = self.max_dl_mb()

    async def stop(self) -> None:
        if self.bot is not None:
            await self.bot.stop()
            self.bot = None

    def info(self) -> dict:
        if self.bot is None or not self.bot.is_running():
            return {
                "ready": False,
                "username": self.bot.username if self.bot else None,
                "last_error": self.bot.last_error if self.bot else None,
            }
        return {"ready": True, "username": self.bot.username, "last_error": self.bot.last_error}


BOT = BotController(manager)


@asynccontextmanager
async def lifespan(app: FastAPI):
    global manager, BOT
    manager = JobManager(WORKDIR, max_concurrent=MAX_CONCURRENT)
    BOT = BotController(manager)
    await manager.start_cleanup()
    await BOT.sync()
    logger.info("vidpress %s started (workdir=%s)", VERSION, WORKDIR)
    yield
    await BOT.stop()


app = FastAPI(title="VidPress", description="Smart FFmpeg video compressor", version=VERSION, lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/api/health")
async def health():
    return {
        "status": "ok",
        "version": VERSION,
        "uptime": round(time.time() - STARTED_AT, 1),
        "running": sum(1 for j in manager.list() if j.status == "running"),
        "queued": sum(1 for j in manager.list() if j.status == "queued"),
        "max_input_mb": MAX_INPUT_MB,
        "max_parallel": manager.capacity(),
        "free_mem_mb": free_mem_mb(),
        "free_disk_gb": round(free_disk_gb(WORKDIR), 2),
        "available_codecs": available_video_codecs(),
        "admin_enabled": auth.admin_enabled(),
        "bot": BOT.info(),
    }


# ---------------- public jobs API ----------------

def _safe_options(raw: str | None) -> dict:
    if not raw:
        return {}
    try:
        data = json.loads(raw)
        if not isinstance(data, dict):
            raise ValueError
        return data
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=400, detail="options must be a JSON object") from exc


@app.post("/api/jobs")
async def create_job(file: UploadFile = File(...), options: str = Form("{}")):
    ext = Path(file.filename or "").suffix.lower() or ".bin"
    allowed = {".mp4", ".mov", ".mkv", ".avi", ".webm", ".wmv", ".flv", ".m4v", ".ts", ".m2ts", ".3gp", ".mts"}
    if ext not in allowed:
        raise HTTPException(status_code=400, detail=f"Unsupported container {ext}. Supported: {', '.join(sorted(allowed))}")
    data = await file.read()
    if not data:
        raise HTTPException(status_code=400, detail="Empty file")
    if len(data) > MAX_INPUT_MB * 1024 * 1024:
        raise HTTPException(status_code=413, detail=f"File exceeds the {MAX_INPUT_MB} MB limit")
    job = manager.create(_safe_options(options), data, ext)
    manager.start(job)
    return _job_payload(job)


@app.get("/api/jobs")
async def list_jobs():
    return {"jobs": [_job_payload(j) for j in manager.list()]}


@app.get("/api/jobs/{job_id}")
async def get_job(job_id: str):
    job = manager.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    return _job_payload(job)


@app.delete("/api/jobs/{job_id}")
async def delete_job(job_id: str):
    job = manager.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    await manager.cancel(job_id)
    return {"ok": True}


@app.get("/api/download/{job_id}")
async def download(job_id: str):
    job = manager.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    if job.status != "done" or not job.output_meta:
        raise HTTPException(status_code=409, detail="No output ready")
    out = job.dst_dir / f"compressed.{Path(job.output_meta.path).suffix.lstrip('.')}"
    if not out.exists():
        raise HTTPException(status_code=404, detail="Output file missing")
    stem = Path(job.src).stem
    return FileResponse(out, media_type="video/mp4" if out.suffix == ".mp4" else "video/webm", filename=f"{stem}-compressed{out.suffix}")


# ---------------- admin API ----------------

@app.post("/api/admin/login")
async def admin_login(payload: dict):
    password = str(payload.get("password", ""))
    if not auth.admin_enabled():
        raise HTTPException(status_code=403, detail="Admin is not configured (set ADMIN_PASSWORD)")
    if not auth.check_admin_password(password):
        raise HTTPException(status_code=401, detail="Wrong password")
    return {"token": auth.make_token()}


@app.get("/api/admin/status")
async def admin_status(_=Depends(_admin_token)):
    return {
        "admin_enabled": auth.admin_enabled(),
        "bot": BOT.info(),
        "bot_token_set": bool(BOT.token()),
    }


@app.get("/api/admin/settings")
async def admin_get_settings(_=Depends(_admin_token)):
    token = BOT.token()
    return {
        "telegram_bot_token": (token[:6] + "…" + token[-4:]) if token else None,
        "telegram_bot_token_set": bool(token),
        "telegram_api_base": BOT.api_base(),
        "telegram_max_dl_mb": BOT.max_dl_mb(),
        "telegram_api_id": BOT.api_id(),
        "telegram_api_hash": (BOT.api_hash()[:4] + "…") if BOT.api_hash() else None,
        "telegram_api_hash_set": bool(BOT.api_hash()),
        "large_files_enabled": bool(BOT.api_id() and BOT.api_hash()),
        "bot": BOT.info(),
    }


@app.put("/api/admin/settings")
async def admin_put_settings(payload: dict, _=Depends(_admin_token)):
    token = str(payload.get("telegram_bot_token", "")).strip()
    if token:
        settings_store.set("telegram_bot_token", token)
    else:
        settings_store.delete("telegram_bot_token")
    if "telegram_api_base" in payload:
        base = str(payload.get("telegram_api_base", "")).strip()
        settings_store.set("telegram_api_base", base) if base else settings_store.delete("telegram_api_base")
    if "telegram_max_dl_mb" in payload:
        try:
            mb = max(1, int(payload.get("telegram_max_dl_mb")))
            settings_store.set("telegram_max_dl_mb", mb)
        except (TypeError, ValueError):
            pass
    if "telegram_api_id" in payload:
        raw = str(payload.get("telegram_api_id", "")).strip()
        if raw.isdigit():
            settings_store.set("telegram_api_id", int(raw))
        else:
            settings_store.delete("telegram_api_id")
    if "telegram_api_hash" in payload:
        raw = str(payload.get("telegram_api_hash", "")).strip()
        if raw:
            settings_store.set("telegram_api_hash", raw)
        else:
            settings_store.delete("telegram_api_hash")
    await BOT.sync()
    return {"ok": True, "bot": BOT.info()}


@app.get("/api/admin/jobs")
async def admin_jobs(_=Depends(_admin_token)):
    return {"jobs": [_job_payload(j, include_stderr=True) for j in manager.list()]}


@app.post("/api/admin/jobs/{job_id}/cancel")
async def admin_cancel(job_id: str, _=Depends(_admin_token)):
    await manager.cancel(job_id)
    return {"ok": True}


@app.get("/admin", include_in_schema=False)
async def admin_page():
    return FileResponse(STATIC_DIR / "admin.html")


# ---------------- helpers / static ----------------

def _job_payload(job, include_stderr: bool = False) -> dict:
    inp, out = job.input_meta, job.output_meta
    return {
        "id": job.id,
        "status": job.status,
        "stage": job.stage,
        "progress": round(job.progress, 1),
        "retried": job.retried,
        "created_at": job.created_at,
        "elapsed": round(job.elapsed, 1),
        "error": job.error or None,
        "stderr_tail": (job.stderr or "")[-500:] if include_stderr else None,
        "input": {
            "name": Path(job.src).name,
            "container": inp.container if inp else None,
            "duration": round(inp.duration, 1) if inp else None,
            "size_bytes": job.original_size,
            "size_mb": round(job.original_size / 1048576, 2),
            "resolution": inp.resolution_label if inp else None,
            "video_codec": inp.video.codec if inp and inp.video else None,
            "fps": round(inp.video.fps, 2) if inp and inp.video else None,
            "audio_codec": inp.audio[0].codec if inp and inp.has_audio else None,
            "has_audio": (inp.has_audio if inp else False),
        } if inp else None,
        "output": {
            "size_bytes": job.output_size,
            "size_mb": round(job.output_size / 1048576, 2),
            "savings_pct": round(job.savings_pct, 1),
            "resolution": out.resolution_label if out else None,
            "video_codec": out.video.codec if out and out.video else None,
            "duration": round(out.duration, 1) if out else None,
            "container": out.container if out else None,
        } if out else None,
        "cmd": job.cmd,
    }


@app.get("/", include_in_schema=False)
async def index():
    return FileResponse(STATIC_DIR / "index.html")


app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


@app.exception_handler(HTTPException)
async def http_exception_handler(request, exc: HTTPException):
    return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})
