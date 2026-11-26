"""Job model and manager backed by the adaptive scheduler."""
from __future__ import annotations

import asyncio
import logging
import os
import time
import uuid
from pathlib import Path

from .engine import CompressOptions, build_command, build_plan, run_ffmpeg
from .probe import ProbeError, probe
from .scheduler import AdaptiveScheduler

logger = logging.getLogger("vidpress.jobs")

MAX_INPUT_MB = int(os.getenv("MAX_INPUT_MB", "512"))


class Job:
    def __init__(self, job_id: str, options: dict, src: Path, dst_dir: Path):
        self.id = job_id
        self.options = options
        self.src = src
        self.dst_dir = dst_dir
        self.status = "queued"
        self.stage = "queued"
        self.progress = 0.0
        self.input_meta = None
        self.output_meta = None
        self.cmd = []
        self.stderr = ""
        self.error = ""
        self.original_size = src.stat().st_size if src.exists() else 0
        self.output_size = 0
        self.savings_pct = 0.0
        self.elapsed = 0.0
        self.created_at = time.time()
        self.retried = False
        self._proc: asyncio.subprocess.Process | None = None
        self._cancelled = False

    async def _run(self) -> None:
        if self._cancelled:
            self.status = "cancelled"
            return

        self.status = "analyzing"
        self.stage = "analyzing input"
        try:
            self.input_meta = probe(self.src)
        except ProbeError as exc:
            self.status = "error"
            self.error = f"Failed to read the video: {exc}"
            self.stage = "error"
            return

        try:
            opts = CompressOptions(**self.options)
            plan = build_plan(self.input_meta, opts)
        except Exception as exc:  # noqa: BLE001
            self.status = "error"
            self.error = f"Compression plan failed: {exc}"
            self.stage = "error"
            return

        d = self.dst_dir
        d.mkdir(parents=True, exist_ok=True)

        async def execute(active_plan) -> bool:
            ext = active_plan.container or "mp4"
            out = d / f"compressed.{ext}"
            if active_plan.custom:
                import shlex
                self.cmd = (
                    ["ffmpeg", "-y", "-v", "error", "-progress", "pipe:1", "-nostats",
                     "-i", str(self.src)]
                    + shlex.split(active_plan.options.custom_cmd)
                    + [str(out)]
                )
            else:
                self.cmd = build_command(active_plan, self.input_meta, str(self.src), str(out), pass_no=1)
            passes = active_plan.passes
            total_passes = passes
            if self._cancelled:
                return False
            self.status = "running"
            self.stage = f"encoding (pass 1/{total_passes})" if passes > 1 else "encoding"

            def on_progress(pct: float, pass_index: int):
                base = (pass_index - 1) / max(total_passes, 1)
                self.progress = min(100.0, base * 100 + pct / max(total_passes, 1))

            async def run_pass(pass_no: int) -> bool:
                if pass_no == 1 and passes == 2:
                    cmd = (
                        ["ffmpeg", "-y", "-v", "error", "-progress", "pipe:1", "-nostats",
                         "-i", str(self.src)]
                        + ["-map", "0:v:0", "-c:v"]
                        + ["libx264" if active_plan.video_codec == "h264" else "libx265"]
                        + ["-preset", "medium", "-b:v", f"{active_plan.video_bitrate}", "-pass", "1"]
                        + ["-an", "-f", "null", "/dev/null"]
                    )
                else:
                    cmd = build_command(active_plan, self.input_meta, str(self.src), str(out), pass_no=pass_no)
                self.stage = f"encoding (pass {pass_no}/{total_passes})" if passes > 1 else "encoding"
                self._proc = await asyncio.create_subprocess_exec(
                    *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                    cwd=str(d),
                )
                started = time.monotonic()

                async def pump():
                    assert self._proc.stdout is not None
                    while True:
                        raw = await self._proc.stdout.readline()
                        if not raw:
                            break
                        line = raw.decode("utf-8", "replace").strip()
                        if line.startswith("out_time_us="):
                            try:
                                us = int(line.split("=", 1)[1])
                                dur = self.input_meta.duration or 0.001
                                on_progress(min(100.0, us / (dur * 1e6) * 100), pass_no)
                            except ValueError:
                                pass
                        elif line.startswith("progress=end"):
                            on_progress(100.0, pass_no)

                await asyncio.gather(pump(), self._proc.wait())
                if self._proc.stderr is not None:
                    self.stderr = (await self._proc.stderr.read()).decode("utf-8", "replace")
                self.elapsed += time.monotonic() - started
                if self._cancelled:
                    self.status = "cancelled"
                    return False
                if self._proc.returncode != 0:
                    self.status = "error"
                    self.error = (self.stderr or "ffmpeg failed").strip()[-400:]
                    return False
                return True

            for p in range(1, passes + 1):
                if not await run_pass(p):
                    return False
                if self._cancelled:
                    return False

            self.status = "running"
            self.stage = "finalizing"
            try:
                self.output_meta = probe(out)
            except ProbeError:
                self.output_meta = None
            self.output_size = out.stat().st_size if out.exists() else 0
            return True

        ok = await execute(plan)

        retried = False
        if (
            ok
            and not plan.audio_only
            and not plan.custom
            and plan.options.mode == "quality"
            and not self._cancelled
            and self.output_size >= self.original_size
            and plan.options.quality > 18
        ):
            reduced = plan.options.quality - 35
            retry_opts = CompressOptions(**{**self.options, "quality": max(18, reduced)})
            if retry_opts.crf:
                retry_opts.crf = min(51, retry_opts.crf + 8)
            try:
                retry_plan = build_plan(self.input_meta, retry_opts)
                self.progress = 0.0
                self.error = ""
                self.stderr = ""
                self.status = "running"
                self.stage = "retrying with stronger compression"
                retried = await execute(retry_plan)
            except Exception:
                retried = False
            if retried:
                self.options = retry_opts.__dict__
                self.retried = True

        if not ok and not retried:
            if self.status in ("error", "cancelled"):
                return

        if self.original_size and self.output_size:
            self.savings_pct = (1 - self.output_size / self.original_size) * 100
        self.status = "done"
        self.stage = "done"
        self.progress = 100.0
        logger.info(
            "job %s done: %s → %s (%.1f%% saved, retried=%s)",
            self.id, self.original_size, self.output_size, self.savings_pct, self.retried,
        )


class JobManager:
    def __init__(self, workdir: Path, max_concurrent: int = 4):
        self.workdir = workdir
        self.uploads = workdir / "uploads"
        self.outputs = workdir / "outputs"
        self.uploads.mkdir(parents=True, exist_ok=True)
        self.outputs.mkdir(parents=True, exist_ok=True)
        self.scheduler = AdaptiveScheduler(max_concurrent, workdir)
        self._jobs: dict[str, Job] = {}
        self._cleanup_task: asyncio.Task | None = None

    def create(self, options: dict, file_bytes: bytes, extension: str) -> Job:
        job_id = uuid.uuid4().hex[:12]
        job_dir = self.uploads / job_id
        job_dir.mkdir(parents=True, exist_ok=True)
        src = job_dir / f"input{extension}"
        src.write_bytes(file_bytes)
        job = Job(job_id, options, src, self.outputs / job_id)
        self._jobs[job_id] = job
        return job

    def create_from_path(self, options: dict, path: str | Path, extension: str) -> Job:
        """Adopt an existing file without reading it into memory (large videos)."""
        import shutil

        job_id = uuid.uuid4().hex[:12]
        job_dir = self.uploads / job_id
        job_dir.mkdir(parents=True, exist_ok=True)
        src = job_dir / f"input{extension}"
        shutil.move(str(path), str(src))
        job = Job(job_id, options, src, self.outputs / job_id)
        self._jobs[job_id] = job
        return job

    def get(self, job_id: str) -> Job | None:
        return self._jobs.get(job_id)

    def list(self) -> list[Job]:
        return sorted(self._jobs.values(), key=lambda j: j.created_at, reverse=True)

    def start(self, job: Job) -> None:
        self.scheduler.submit(job)

    def capacity(self) -> int:
        return self.scheduler.capacity()

    async def cancel(self, job_id: str) -> bool:
        job = self._jobs.get(job_id)
        if not job:
            return False
        return await self.scheduler.cancel(job)

    async def start_cleanup(self):
        self._cleanup_task = asyncio.create_task(self._cleanup_loop())

    async def _cleanup_loop(self):
        while True:
            await asyncio.sleep(3600)
            try:
                self._cleanup()
            except Exception:  # noqa: BLE001
                logger.exception("cleanup failed")

    def _cleanup(self):
        now = time.time()
        for job in list(self._jobs.values()):
            if job.status in ("done", "error", "cancelled") and now - job.created_at > 3 * 3600:
                self._jobs.pop(job.id, None)
        for base in (self.uploads, self.outputs):
            try:
                import shutil
                for child in base.iterdir():
                    if child.is_dir() and now - child.stat().st_mtime > 86400:
                        shutil.rmtree(child, ignore_errors=True)
            except Exception:
                pass
