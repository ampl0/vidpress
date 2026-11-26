"""Adaptive job scheduler.

Runs up to N encodes concurrently where N is derived from available system
memory and disk, so a small free-tier box safely runs a few jobs in parallel
when it can, and falls back to serial when resources are tight.
"""
from __future__ import annotations

import asyncio
import os
import time
from collections import deque
from pathlib import Path

_LAST_MEM = {"t": 0.0, "mb": 0}


def free_mem_mb() -> int:
    now = time.time()
    if now - _LAST_MEM["t"] < 2.0 and _LAST_MEM["mb"]:
        return _LAST_MEM["mb"]
    try:
        with open("/proc/meminfo") as fh:
            for line in fh:
                if line.startswith("MemAvailable:"):
                    kb = int(line.split()[1])
                    mb = kb // 1024
                    _LAST_MEM.update(t=now, mb=mb)
                    return mb
    except Exception:
        pass
    # fallback: approximate usable memory
    try:
        import os as _os
        total = _os.sysconf("SC_PHYS_PAGES") * _os.sysconf("SC_PAGESIZE")
        return int(total / 1048576 * 0.7)
    except Exception:
        return 512


def free_disk_gb(path: str | Path = ".") -> float:
    try:
        st = os.statvfs(str(path))
        return (st.f_bavail * st.f_frsize) / (1024 ** 3)
    except Exception:
        return 10.0


class AdaptiveScheduler:
    def __init__(self, max_concurrent: int, workdir: Path):
        self.max_concurrent = max(1, max_concurrent)
        self.workdir = workdir
        self.per_job_mem_mb = int(os.getenv("PER_JOB_MEM_MB", "160"))
        self._pending: deque = deque()
        self._running: set = set()
        self._loop_task: asyncio.Task | None = None

    # ---- capacity ----
    def capacity(self) -> int:
        mem_free = free_mem_mb()
        mem_slots = max(1, mem_free // max(1, self.per_job_mem_mb)) if mem_free else self.max_concurrent
        disk_free = free_disk_gb(self.workdir)
        slots = min(self.max_concurrent, mem_slots, 8)
        if disk_free < 0.2:
            slots = min(slots, 1)
        return max(1, slots)

    # ---- submit ----
    def submit(self, job) -> None:
        self._pending.append(job)
        if self._loop_task is None or self._loop_task.done():
            self._loop_task = asyncio.create_task(self._loop())

    async def _loop(self) -> None:
        while self._pending:
            # room for another run?
            if len(self._running) >= self.capacity():
                # wait for a running job to finish
                if not self._running:
                    break
                await asyncio.gather(*self._running, return_exceptions=True)
                continue
            job = self._pending.popleft()
            task = asyncio.create_task(self._guarded(job))
            self._running.add(task)
            task.add_done_callback(self._running.discard)

    async def _guarded(self, job) -> None:
        try:
            await job._run()
        except asyncio.CancelledError:
            job.status = "cancelled"
        except Exception as exc:  # noqa: BLE001
            job.status = "error"
            job.error = str(exc)
            job.stage = "error"

    async def cancel(self, job) -> bool:
        if job._cancelled or job.status in ("done", "error", "cancelled"):
            return job.status == "cancelled"
        job._cancelled = True
        job.status = "cancelled"
        job.stage = "cancelling"
        # remove if still queued
        if job in self._pending:
            self._pending.remove(job)
        if job._proc and job._proc.returncode is None:
            try:
                job._proc.terminate()
            except Exception:
                pass
        return True
