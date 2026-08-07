"""Cron tool for scheduling reminders and tasks."""

from contextvars import ContextVar
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from nanocat.agent.tools.base import Tool, tool_err, tool_ok
from nanocat.cron.service import CronService
from nanocat.cron.types import CronSchedule


def _ms_to_local_iso(ms: int) -> str:
    """Convert epoch ms to local-timezone ISO string with offset."""
    return datetime.fromtimestamp(ms / 1000).astimezone().isoformat()


class CronTool(Tool):
    def __init__(self, cron_service: CronService):
        self._cron = cron_service
        self._channel_context: ContextVar[str] = ContextVar("cron_channel", default="")
        self._chat_context: ContextVar[str] = ContextVar("cron_chat", default="")
        self._principal_context: ContextVar[str] = ContextVar("cron_principal", default="user")
        self._in_cron_context: ContextVar[bool] = ContextVar("cron_in_context", default=False)

    def set_context(self, channel: str, chat_id: str, principal_id: str = "user") -> None:
        """Set the current session context for delivery."""
        self._channel_context.set(channel)
        self._chat_context.set(chat_id)
        self._principal_context.set(principal_id)

    def set_cron_context(self, active: bool):
        """Mark whether the tool is executing inside a cron job callback."""
        return self._in_cron_context.set(active)

    def reset_cron_context(self, token) -> None:
        """Restore previous cron context."""
        self._in_cron_context.reset(token)

    @property
    def name(self) -> str:
        return "cron"

    @property
    def description(self) -> str:
        return (
            "Schedule reminders and recurring tasks (local timezone unless tz given).\n"
            "- Reminder mode: task_description is sent to the user as-is when it fires.\n"
            "- Task mode: the agent executes task_description on each fire.\n"
            "Scheduling: every_seconds=1200 (interval), cron_expr='0 8 * * *' (needs tz, "
            "e.g. 'Asia/Shanghai'), at='2026-03-19T10:30:00+08:00' (one-shot, auto-deletes). "
            "tz is mandatory for cron_expr."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["add", "list", "remove"],
                },
                "task_description": {
                    "type": "string",
                    "description": (
                        "What to remind or do. Include full context — "
                        "the executor has no access to prior conversation history."
                    ),
                },
                "notify": {
                    "type": "string",
                    "enum": ["never", "always", "smart"],
                    "default": "smart",
                    "description": "never=silent, always=notify on every run, smart=another agent decides.",
                },
                "every_seconds": {"type": "integer", "description": "Repeat interval in seconds."},
                "cron_expr": {
                    "type": "string",
                    "description": "Cron expression, e.g. '0 9 * * *'. Always provide tz alongside.",
                },
                "tz": {
                    "type": "string",
                    "description": (
                        "IANA timezone, e.g. 'Asia/Shanghai'. "
                        "Required when using cron_expr. "
                        "For at=, omit tz and include the offset directly in the ISO string instead."
                    ),
                },
                "at": {
                    "type": "string",
                    "description": (
                        "ISO datetime with explicit timezone offset for one-time run, "
                        "e.g. '2026-03-19T10:30:00+08:00'. The offset is mandatory."
                    ),
                },
                "job_id": {"type": "string", "description": "Job ID (required for remove)."},
            },
            "required": ["action"],
        }

    async def execute(
        self,
        action: str,
        task_description: str = "",
        notify: str = "smart",
        every_seconds: int | None = None,
        cron_expr: str | None = None,
        tz: str | None = None,
        at: str | None = None,
        job_id: str | None = None,
        **kwargs: Any,
    ) -> str:
        if action == "add":
            if self._in_cron_context.get():
                return tool_err("cannot schedule new jobs from within a cron job execution")
            return self._add_job(task_description, notify, every_seconds, cron_expr, tz, at)
        elif action == "list":
            return self._list_jobs()
        elif action == "remove":
            return self._remove_job(job_id)
        return tool_err(f"unknown action: {action}")

    def _add_job(
        self,
        task_description: str,
        notify: str,
        every_seconds: int | None,
        cron_expr: str | None,
        tz: str | None,
        at: str | None,
    ) -> str:
        if not task_description:
            return tool_err("task_description is required for add")
        channel = self._channel_context.get()
        chat_id = self._chat_context.get()
        principal_id = self._principal_context.get()
        if not channel or not chat_id:
            return tool_err("no session context (channel/chat_id)")

        # Exactly one schedule must be given; they are mutually exclusive.
        provided = [
            field
            for field, given in (
                ("every_seconds", every_seconds is not None),
                ("cron_expr", bool(cron_expr)),
                ("at", bool(at)),
            )
            if given
        ]
        if len(provided) > 1:
            return tool_err(f"provide exactly one of every_seconds/cron_expr/at, not {provided}")
        if not provided:
            return tool_err("one of every_seconds, cron_expr, or at is required")
        if every_seconds is not None and every_seconds <= 0:
            return tool_err("every_seconds must be a positive integer")

        if tz and not cron_expr:
            return tool_err("tz can only be used with cron_expr")
        if tz:
            try:
                ZoneInfo(tz)
            except Exception:
                return tool_err(f"unknown timezone '{tz}'")

        # Build schedule
        delete_after = False
        schedule_summary: dict[str, Any] = {}

        if every_seconds:
            schedule = CronSchedule(kind="every", every_ms=every_seconds * 1000)
            schedule_summary = {"kind": "every", "interval_seconds": every_seconds}
        elif cron_expr:
            if not tz:
                return tool_err(
                    "tz is required when using cron_expr. Provide an explicit IANA "
                    "timezone, e.g. 'Asia/Shanghai'."
                )
            schedule = CronSchedule(kind="cron", expr=cron_expr, tz=tz)
            schedule_summary = {"kind": "cron", "expr": cron_expr, "tz": tz}
        elif at:
            try:
                dt = datetime.fromisoformat(at)
            except ValueError:
                return tool_err(
                    f"invalid ISO datetime format '{at}'. Expected format with "
                    "offset: YYYY-MM-DDTHH:MM:SS+HH:MM"
                )
            if dt.tzinfo is None:
                return tool_err(
                    f"datetime '{at}' has no timezone offset. "
                    "Provide an explicit offset, e.g. '2026-03-19T10:30:00+08:00'."
                )
            if dt.timestamp() <= datetime.now().timestamp():
                return tool_err(f"'at' time '{at}' is in the past")
            at_ms = int(dt.timestamp() * 1000)
            schedule = CronSchedule(kind="at", at_ms=at_ms)
            delete_after = True
            schedule_summary = {
                "kind": "at",
                "scheduled_time": _ms_to_local_iso(at_ms),
            }
        else:
            return tool_err("either every_seconds, cron_expr, or at is required")

        job = self._cron.add_job(
            name=task_description[:30],
            schedule=schedule,
            message=task_description,
            channel=channel,
            to=chat_id,
            principal_id=principal_id,
            delete_after_run=delete_after,
            notify_mode=notify,
        )

        result: dict[str, Any] = {
            "job_id": job.id,
            "name": job.name,
            "schedule": schedule_summary,
        }
        if job.state.next_run_at_ms:
            result["next_run"] = _ms_to_local_iso(job.state.next_run_at_ms)

        return tool_ok(**result)

    @staticmethod
    def _job_to_dict(j) -> dict[str, Any]:
        """Serialize a CronJob to a JSON-compatible dict with local-timezone timestamps."""
        schedule_info: dict[str, Any] = {"kind": j.schedule.kind}
        if j.schedule.kind == "cron":
            schedule_info["expr"] = j.schedule.expr
            schedule_info["tz"] = j.schedule.tz
        elif j.schedule.kind == "every" and j.schedule.every_ms:
            ms = j.schedule.every_ms
            if ms % 3_600_000 == 0:
                schedule_info["interval"] = f"{ms // 3_600_000}h"
            elif ms % 60_000 == 0:
                schedule_info["interval"] = f"{ms // 60_000}m"
            elif ms % 1000 == 0:
                schedule_info["interval"] = f"{ms // 1000}s"
            else:
                schedule_info["interval"] = f"{ms}ms"
        elif j.schedule.kind == "at" and j.schedule.at_ms:
            schedule_info["scheduled_time"] = _ms_to_local_iso(j.schedule.at_ms)

        state_info: dict[str, Any] = {}
        if j.state.next_run_at_ms:
            state_info["next_run"] = _ms_to_local_iso(j.state.next_run_at_ms)
        if j.state.last_run_at_ms:
            state_info["last_run"] = _ms_to_local_iso(j.state.last_run_at_ms)
            state_info["last_status"] = j.state.last_status or "unknown"
            if j.state.last_error:
                state_info["last_error"] = j.state.last_error

        return {
            "job_id": j.id,
            "name": j.name,
            "schedule": schedule_info,
            "state": state_info,
        }

    def _list_jobs(self) -> str:
        jobs = self._cron.list_jobs()
        return tool_ok(jobs=[self._job_to_dict(j) for j in jobs])

    def _remove_job(self, job_id: str | None) -> str:
        if not job_id:
            return tool_err("job_id is required for remove")
        if self._cron.remove_job(job_id):
            return tool_ok(removed=job_id)
        return tool_err(f"job {job_id} not found")
