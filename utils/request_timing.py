from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Callable, Dict, List, Optional


@dataclass
class RequestDelayTimer:
    """Enforce a minimum delay between consecutive request submissions."""

    min_delay_seconds: float
    _last_submit_monotonic: Optional[float] = field(default=None, init=False, repr=False)

    def wait_for_turn(self) -> float:
        """Sleep until the minimum delay is satisfied.

        Returns the number of seconds slept.
        """
        if self.min_delay_seconds <= 0:
            self._last_submit_monotonic = time.monotonic()
            return 0.0

        now = time.monotonic()
        slept = 0.0
        if self._last_submit_monotonic is not None:
            elapsed = now - self._last_submit_monotonic
            remaining = self.min_delay_seconds - elapsed
            if remaining > 0:
                time.sleep(remaining)
                slept = remaining

        self._last_submit_monotonic = time.monotonic()
        return slept

    def mark_submitted_now(self) -> None:
        self._last_submit_monotonic = time.monotonic()

    def reset(self) -> None:
        self._last_submit_monotonic = None


@dataclass
class ScheduledRequest:
    """A recurring request schedule definition."""

    schedule_id: str
    url: str
    method: str = "POST"
    json_payload: Optional[Dict[str, Any]] = None
    time_of_day: Optional[str] = None
    interval_seconds: Optional[int] = None
    duration_seconds: Optional[int] = None
    start_at_epoch: Optional[float] = None
    enabled: bool = True
    metadata: Dict[str, Any] = field(default_factory=dict)

    created_at_epoch: float = field(default_factory=lambda: time.time())
    next_run_epoch: Optional[float] = None
    last_run_epoch: Optional[float] = None


class RequestScheduler:
    """In-process scheduler for routine request submissions.

    This scheduler is poll-driven and intentionally simple:
    call ``run_pending`` from your app loop or worker.
    """

    def __init__(self) -> None:
        self._schedules: Dict[str, ScheduledRequest] = {}

    def add_schedule(self, schedule: ScheduledRequest) -> ScheduledRequest:
        schedule.method = (schedule.method or "POST").upper()
        schedule.next_run_epoch = self._compute_initial_next_run(schedule)
        self._schedules[schedule.schedule_id] = schedule
        return schedule

    def remove_schedule(self, schedule_id: str) -> bool:
        return self._schedules.pop(schedule_id, None) is not None

    def list_schedules(self) -> List[ScheduledRequest]:
        return list(self._schedules.values())

    def set_enabled(self, schedule_id: str, enabled: bool) -> bool:
        schedule = self._schedules.get(schedule_id)
        if schedule is None:
            return False
        schedule.enabled = enabled
        return True

    def run_pending(
        self,
        submit_func: Callable[[ScheduledRequest], Any],
        now_epoch: Optional[float] = None,
    ) -> List[Dict[str, Any]]:
        """Run due schedules and return execution records."""
        now = now_epoch if now_epoch is not None else time.time()
        results: List[Dict[str, Any]] = []

        for schedule in self._schedules.values():
            if not schedule.enabled:
                continue
            if schedule.next_run_epoch is None or schedule.next_run_epoch > now:
                continue

            record: Dict[str, Any] = {
                "schedule_id": schedule.schedule_id,
                "run_at": now,
                "success": True,
                "result": None,
                "error": None,
            }
            try:
                record["result"] = submit_func(schedule)
            except Exception as exc:
                record["success"] = False
                record["error"] = f"{exc.__class__.__name__}: {exc}"

            schedule.last_run_epoch = now
            schedule.next_run_epoch = self._compute_next_run(schedule, now)
            if schedule.next_run_epoch is None:
                schedule.enabled = False

            results.append(record)

        return results

    def _compute_initial_next_run(self, schedule: ScheduledRequest) -> Optional[float]:
        now = time.time()
        if schedule.time_of_day:
            return self._next_time_of_day_epoch(schedule.time_of_day, now)

        if schedule.interval_seconds is not None:
            base = schedule.start_at_epoch if schedule.start_at_epoch is not None else now
            if base < now:
                delta = now - base
                steps = int(delta // max(schedule.interval_seconds, 1)) + 1
                return base + steps * max(schedule.interval_seconds, 1)
            return base

        return schedule.start_at_epoch if schedule.start_at_epoch is not None else now

    def _compute_next_run(self, schedule: ScheduledRequest, now_epoch: float) -> Optional[float]:
        if schedule.duration_seconds is not None:
            deadline = schedule.created_at_epoch + max(schedule.duration_seconds, 0)
            if now_epoch >= deadline:
                return None

        if schedule.time_of_day:
            next_epoch = self._next_time_of_day_epoch(schedule.time_of_day, now_epoch + 1)
        elif schedule.interval_seconds is not None:
            next_epoch = now_epoch + max(schedule.interval_seconds, 1)
        else:
            return None

        if schedule.duration_seconds is not None:
            deadline = schedule.created_at_epoch + max(schedule.duration_seconds, 0)
            if next_epoch > deadline:
                return None

        return next_epoch

    @staticmethod
    def _next_time_of_day_epoch(time_of_day: str, from_epoch: float) -> float:
        parsed = RequestScheduler._parse_time_of_day(time_of_day)
        base_dt = datetime.fromtimestamp(from_epoch)
        target_dt = base_dt.replace(
            hour=parsed.hour,
            minute=parsed.minute,
            second=parsed.second,
            microsecond=0,
        )
        if target_dt.timestamp() <= from_epoch:
            target_dt = target_dt + timedelta(days=1)
        return target_dt.timestamp()

    @staticmethod
    def _parse_time_of_day(value: str) -> datetime:
        raw = (value or "").strip()
        for fmt in ("%H:%M:%S", "%H:%M"):
            try:
                return datetime.strptime(raw, fmt)
            except ValueError:
                continue
        raise ValueError("time_of_day must be HH:MM or HH:MM:SS")
