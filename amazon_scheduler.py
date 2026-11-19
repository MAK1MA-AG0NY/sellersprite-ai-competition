# -*- coding: utf-8 -*-
"""Default-off single-process daily Amazon schedule using explicit Asia/Shanghai time."""
from __future__ import annotations

import asyncio
import datetime as dt
from typing import Callable, Any

import local_db

SHANGHAI = dt.timezone(dt.timedelta(hours=8), "Asia/Shanghai")  # fixed UTC+08:00; no DST, no tzdata dependency
CHECK_INTERVAL_SECONDS = 30
GRACE_MINUTES = 10


class AmazonDailyScheduler:
    def __init__(self, start_job: Callable[[int, str], Any], is_busy: Callable[[], bool],
                 clock: Callable[[], dt.datetime] | None = None, single_process: bool = True) -> None:
        self.start_job = start_job
        self.is_busy = is_busy
        self.clock = clock or (lambda: dt.datetime.now(dt.timezone.utc))
        self.single_process = bool(single_process)
        self.last_error = ""
        self._stop = asyncio.Event()

    @staticmethod
    def supported(worker_count: int | str | None = 1) -> bool:
        try:
            return int(worker_count or 1) == 1
        except (TypeError, ValueError):
            return False

    async def tick(self, now: dt.datetime | None = None) -> list[dict]:
        if not self.single_process:
            self.last_error = "每日调度仅支持单 worker；当前多 worker 配置已禁用调度"
            return []
        instant = now or self.clock()
        if instant.tzinfo is None or instant.utcoffset() is None:
            raise ValueError("scheduler clock must be timezone-aware")
        shanghai_now = instant.astimezone(SHANGHAI)
        started = []
        for group in local_db.amz_groups():
            config = local_db.amz_get_schedule(group["id"])
            if not config["enabled"]:
                continue
            run_date = shanghai_now.date().isoformat()
            run = local_db.amz_schedule_run_for(group["id"], run_date)
            if run is not None:
                continue
            hour, minute = (int(part) for part in config["local_time"].split(":"))
            due = dt.datetime.combine(shanghai_now.date(), dt.time(hour, minute), tzinfo=SHANGHAI)
            if shanghai_now < due:
                continue
            if shanghai_now > due + dt.timedelta(minutes=GRACE_MINUTES):
                local_db.amz_schedule_run(group["id"], run_date, "missed",
                                          "服务未在计划时间后的 10 分钟宽限内执行；不追补本日任务。")
                continue
            if self.is_busy():
                # Let other due groups wait within the grace window; an already-running job is never overlapped.
                continue
            try:
                job_id = await self.start_job(group["id"], "schedule")
                local_db.amz_schedule_run(group["id"], run_date, "running", "定时巡查已启动。", job_id)
                started.append({"group_id": group["id"], "run_date": run_date, "job_id": job_id})
            except Exception as exc:
                if "进行中" in str(exc) or "busy" in str(exc).casefold():
                    continue
                self.last_error = str(exc)[:300]
                local_db.amz_schedule_run(group["id"], run_date, "error", self.last_error)
        return started

    def finish(self, group_id: int, run_date: str, status: str, message: str, job_id: str | None = None) -> None:
        local_db.amz_schedule_run(group_id, run_date, status, message, job_id)
        if status == "paused":
            local_db.amz_save_schedule(group_id, False, local_db.amz_get_schedule(group_id)["local_time"])

    async def run(self) -> None:
        self._stop.clear()
        while not self._stop.is_set():
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.last_error = f"调度检查失败：{type(exc).__name__}: {str(exc)[:250]}"
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=CHECK_INTERVAL_SECONDS)
            except asyncio.TimeoutError:
                pass

    def stop(self) -> None:
        self._stop.set()


__all__ = ["SHANGHAI", "CHECK_INTERVAL_SECONDS", "GRACE_MINUTES", "AmazonDailyScheduler"]
