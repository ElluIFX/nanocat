"""Application-owned handlers for commands that mutate runtime state."""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from nanocat.application.command_router import CommandInspection
from nanocat.core.commands import CommandErrorCode, CommandResult, ParsedCommand
from nanocat.cron.types import CronSchedule


def _ok(
    message: str,
    *,
    effect: str = "state-changed",
    data: dict[str, Any] | None = None,
    title: str = "Command completed",
) -> CommandResult:
    return CommandResult(ok=True, title=title, message=message, data=data or {}, execution_effect=effect)


def _error(code: str, message: str, usage: str) -> CommandResult:
    return CommandResult(
        ok=False,
        code=code,
        title="Command failed",
        message=message,
        usage=usage,
    )


def _without_none(values: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in values.items() if value is not None}


def _memory_view(value: Any) -> Any:
    """Keep user-facing memory details useful without dumping API internals."""
    if not isinstance(value, dict):
        return value
    memory = value.get("memory") if isinstance(value.get("memory"), dict) else value
    if not isinstance(memory, dict):
        return value
    view = _without_none(
        {
            "id": memory.get("id") or memory.get("memory_id"),
            "title": memory.get("title"),
            "content": memory.get("content"),
            "type": memory.get("unit_type"),
            "importance": memory.get("importance"),
            "confidence": memory.get("confidence"),
            "labels": memory.get("labels") or memory.get("label_names"),
            "source": memory.get("source"),
            "space": memory.get("space_id"),
            "created": memory.get("created_at"),
            "updated": memory.get("updated_at"),
            "event_start": memory.get("event_start"),
            "event_end": memory.get("event_end"),
            "source_range": memory.get("source_range"),
            "latest": memory.get("is_latest"),
            "version": memory.get("version"),
            "extraction": memory.get("extraction_method"),
        }
    )
    if isinstance(value.get("labels"), list) and "labels" not in view:
        view["labels"] = value["labels"]
    return view


def _memory_search_view(value: Any) -> Any:
    """Project one search hit into fields useful for a human reader."""
    if not isinstance(value, dict):
        return value
    memory = value.get("memory") if isinstance(value.get("memory"), dict) else value
    if not isinstance(memory, dict):
        return value
    related = value.get("related_entities")
    related_names = [
        item.get("name") or item.get("id")
        for item in related[:10]
        if isinstance(item, dict) and (item.get("name") or item.get("id"))
    ] if isinstance(related, list) else None
    view = _memory_view(memory)
    if not isinstance(view, dict):
        view = {"memory": view}
    view.update(
        _without_none(
            {
                "score": value.get("similarity_score"),
                "reason": value.get("relevance_reason"),
                "related": related_names,
            }
        )
    )
    return view


def _memory_processing_view(value: Any) -> Any:
    """Keep processing status bounded to information useful to an operator."""
    if not isinstance(value, dict):
        return value

    tasks = value.get("tasks")
    pending = value.get("pending_thread_analysis")
    history = value.get("task_history")
    recent_tasks = []
    if isinstance(history, list):
        for item in history[-10:]:
            if not isinstance(item, dict):
                continue
            recent_tasks.append(
                _without_none(
                    {
                        "task_id": item.get("task_id"),
                        "task_type": item.get("task_type"),
                        "status": item.get("status"),
                        "summary": item.get("summary"),
                        "started_at": item.get("started_at"),
                        "completed_at": item.get("completed_at"),
                        "duration_ms": item.get("duration_ms"),
                    }
                )
            )

    return _without_none(
        {
            "agent_running": value.get("agent_running"),
            "budget_paused": value.get("budget_paused"),
            "current_task": value.get("current_task"),
            "queue": _without_none(
                {
                    "size": tasks.get("queue_size") if isinstance(tasks, dict) else None,
                    "running": tasks.get("running") if isinstance(tasks, dict) else None,
                    "queued": tasks.get("queued") if isinstance(tasks, dict) else None,
                }
            ),
            "pending_thread_analysis": _without_none(
                {
                    key: pending.get(key)
                    for key in ("fresh_count", "pending_count", "scheduled_count", "suppressed_count")
                }
                if isinstance(pending, dict)
                else {}
            ),
            "recent_tasks": recent_tasks,
        }
    )


class RuntimeCommandHandlers:
    """Execute stateful command groups without AgentLoop string dispatch."""

    def __init__(
        self,
        cron_service: Any | None,
        memory_client: Any | None,
        memory_settings: dict[str, Any] | None = None,
    ):
        self._cron = cron_service
        self._memory = memory_client
        self._memory_settings = memory_settings or {}

    async def execute(
        self,
        inspection: CommandInspection,
        *,
        principal_id: str = "user",
        session: Any | None = None,
    ) -> CommandResult:
        """Execute one validated command inspection."""
        if inspection.parsed is None or inspection.spec is None:
            return _error(CommandErrorCode.INVALID_ARGUMENT, "Command syntax is incomplete.", "/help")
        if inspection.spec.name == "cron":
            return await self._cron_command(inspection.parsed, principal_id)
        if inspection.spec.name == "memory":
            return await self._memory_command(inspection.parsed, session=session)
        return _error(CommandErrorCode.COMMAND_FAILED, "No application handler is registered.", "/help")

    async def _cron_command(self, command: ParsedCommand, principal_id: str) -> CommandResult:
        usage = "/cron list|show|add|remove|run|enable|disable"
        if self._cron is None:
            return _error(CommandErrorCode.INVALID_STATE, "Cron service is unavailable.", usage)
        action = command.subcommand
        args = command.positional_args
        options = command.options
        if action == "list":
            jobs = self._cron.list_jobs(include_disabled=True)
            if not jobs:
                return _ok("No scheduled jobs.", effect="no-op")
            lines = []
            for job in jobs:
                status = "enabled" if job.enabled else "disabled"
                next_run = job.state.next_run_at_ms or "-"
                lines.append(f"{job.id} | {status} | {job.name} | next={next_run}")
            return _ok("\n".join(lines), effect="no-op")
        if action == "show":
            if len(args) != 1:
                return _error(CommandErrorCode.MISSING_ARGUMENT, "Provide exactly one job id.", "/cron show <job_id>")
            job = next((item for item in self._cron.list_jobs(include_disabled=True) if item.id == args[0]), None)
            if job is None:
                return _error(CommandErrorCode.INVALID_ARGUMENT, f"Unknown cron job `{args[0]}`.", usage)
            return _ok(json.dumps({
                "id": job.id,
                "name": job.name,
                "enabled": job.enabled,
                "schedule": job.schedule.__dict__,
                "payload": job.payload.__dict__,
                "state": job.state.__dict__,
            }, ensure_ascii=False, default=str), effect="no-op")
        if action in {"remove", "run", "enable", "disable"}:
            if len(args) != 1:
                return _error(CommandErrorCode.MISSING_ARGUMENT, "Provide exactly one job id.", f"/cron {action} <job_id>")
            job_id = args[0]
            if action == "remove":
                changed = self._cron.remove_job(job_id)
            elif action == "run":
                changed = await self._cron.run_job(job_id, force=True)
            else:
                changed = self._cron.enable_job(job_id, enabled=action == "enable") is not None
            if not changed:
                return _error(CommandErrorCode.INVALID_ARGUMENT, f"Unknown cron job `{job_id}`.", usage)
            return _ok(f"Cron job `{job_id}` {action}d.")
        if action == "add":
            return self._cron_add(options, args, usage, principal_id)
        return _error(CommandErrorCode.UNKNOWN_SUBCOMMAND, "Unknown cron action.", usage)

    def _cron_add(
        self,
        options: Any,
        args: tuple[str, ...],
        usage: str,
        principal_id: str,
    ) -> CommandResult:
        message = str(options.get("message") or options.get("task") or " ".join(args)).strip()
        if not message:
            return _error(CommandErrorCode.MISSING_ARGUMENT, "Provide --message for the scheduled task.", "/cron add --message=<text> --every-seconds=<n>")
        provided = [key for key in ("every-seconds", "cron-expr", "at") if options.get(key)]
        if len(provided) != 1:
            return _error(CommandErrorCode.INVALID_ARGUMENT, "Provide exactly one of --every-seconds, --cron-expr or --at.", "/cron add --message=<text> --every-seconds=<n>")
        try:
            if provided[0] == "every-seconds":
                seconds = int(options[provided[0]])
                if seconds <= 0:
                    return _error(CommandErrorCode.INVALID_ARGUMENT, "--every-seconds must be positive.", usage)
                schedule = CronSchedule(kind="every", every_ms=seconds * 1000)
            elif provided[0] == "cron-expr":
                tz = str(options.get("tz") or "")
                if not tz:
                    return _error(CommandErrorCode.MISSING_ARGUMENT, "--tz is required with --cron-expr.", usage)
                schedule = CronSchedule(kind="cron", expr=str(options[provided[0]]), tz=tz)
            else:
                value = datetime.fromisoformat(str(options[provided[0]]))
                if value.tzinfo is None:
                    return _error(CommandErrorCode.INVALID_ARGUMENT, "--at requires an explicit timezone offset.", usage)
                schedule = CronSchedule(kind="at", at_ms=int(value.timestamp() * 1000))
            job = self._cron.add_job(
                name=str(options.get("name") or message[:32]),
                schedule=schedule,
                message=message,
                channel=str(options.get("channel")) if options.get("channel") else None,
                to=str(options.get("to")) if options.get("to") else None,
                principal_id=principal_id,
                delete_after_run=str(options.get("delete-after-run", False)).lower()
                in {"1", "true", "yes", "on"},
                notify_mode=str(options.get("notify") or "smart"),
            )
        except (TypeError, ValueError) as exc:
            return _error(CommandErrorCode.INVALID_ARGUMENT, str(exc), usage)
        return _ok(f"Cron job `{job.id}` added.")

    async def _memory_command(
        self, command: ParsedCommand, *, session: Any | None = None
    ) -> CommandResult:
        usage = "/memory status|spaces|search|show|add|update|delete|preview|distill|processing"
        action = command.subcommand
        if action == "status":
            try:
                available = await self._memory.is_available() if self._memory is not None else False
                agent_status = await self._memory.agent_status() if available else {}
                return _ok(
                    "Nowledge memory status.",
                    effect="no-op",
                    title="Memory status",
                    data={
                        "available": available,
                        "service": "nowledge",
                        "agent_running": agent_status.get("running"),
                        "queue_size": agent_status.get("queue_size"),
                        "configuration": self._memory_settings,
                    },
                )
            except Exception as exc:
                error_code = getattr(exc, "code", None)
                if error_code:
                    return _error(
                        CommandErrorCode.COMMAND_FAILED,
                        f"Nowledge request failed: {error_code}.",
                        usage,
                    )
                raise
        if self._memory is None:
            return _error(CommandErrorCode.INVALID_STATE, "Nowledge memory is unavailable.", usage)
        args = command.positional_args
        options = command.options
        try:
            if action == "processing":
                result = await self._memory.processing_status()
                return _ok(
                    "Nowledge processing status.",
                    data=_memory_processing_view(result),
                    effect="no-op",
                    title="Knowledge processing",
                )
            if action == "spaces":
                result = await self._memory.list_spaces()
                spaces = result.get("spaces") if isinstance(result, dict) else None
                if isinstance(spaces, list):
                    result = {
                        "enabled": result.get("enabled"),
                        "spaces": [
                            {
                                key: space.get(key)
                                for key in ("id", "key", "name", "description", "defaultRetrievalMode")
                                if space.get(key) is not None
                            }
                            for space in spaces
                            if isinstance(space, dict)
                        ],
                    }
                return _ok("Nowledge Spaces.", data=result, effect="no-op", title="Memory spaces")
            if action == "search":
                query = str(options.get("query") or " ".join(args)).strip()
                if not query:
                    return _error(CommandErrorCode.MISSING_ARGUMENT, "Provide a search query.", "/memory search <query>")
                limit = int(options.get("limit", 5))
                result = await self._memory.search_memories(
                    query=query,
                    limit=limit,
                    mode=str(options.get("mode") or "fast"),
                    space_id=str(options["space-id"]) if options.get("space-id") else None,
                )
                return _ok(
                    f"Found {len(result)} memor{'y' if len(result) == 1 else 'ies'}.",
                    data={
                        "query": query,
                        "mode": str(options.get("mode") or "fast"),
                        "count": len(result),
                        "results": [_memory_search_view(item) for item in result] if result else None,
                    },
                    effect="no-op",
                    title="Memory search",
                )
            if action == "show":
                if len(args) != 1:
                    return _error(CommandErrorCode.MISSING_ARGUMENT, "Provide exactly one memory id.", "/memory show <memory_id>")
                result = await self._memory.get_memory(
                    args[0],
                    space_id=str(options["space-id"]) if options.get("space-id") else None,
                )
                return _ok(
                    "Memory details.",
                    data={"memory": _memory_view(result or {})},
                    effect="no-op",
                    title="Memory details",
                )
            if action == "add":
                content = str(options.get("content") or " ".join(args)).strip()
                if not content:
                    return _error(CommandErrorCode.MISSING_ARGUMENT, "Provide memory content.", "/memory add --content=<text>")
                result = await self._memory.create_memory(
                    content=content,
                    title=str(options["title"]) if options.get("title") else None,
                    importance=float(options.get("importance", 0.5)),
                    labels=[
                        item.strip()
                        for item in str(options["labels"]).split(",")
                        if item.strip()
                    ]
                    if options.get("labels")
                    else None,
                    unit_type=str(options["unit-type"]) if options.get("unit-type") else None,
                    space_id=str(options["space-id"]) if options.get("space-id") else None,
                )
                memory = (result.get("memory") or result) if result else {}
                memory_id = memory.get("id") or memory.get("memory_id") if isinstance(memory, dict) else None
                memory_view = _memory_view(result or {})
                return _ok(
                    "Memory added.",
                    data={"memory": memory_view} if memory_view else {"id": memory_id},
                    title="Memory added",
                )
            if action == "update":
                if len(args) != 1:
                    return _error(CommandErrorCode.MISSING_ARGUMENT, "Provide exactly one memory id.", "/memory update <memory_id> --content=<text>")
                fields = {
                    key: options[key]
                    for key in ("content", "title", "importance", "unit_type")
                    if key in options
                }
                if "unit-type" in options:
                    fields["unit_type"] = options["unit-type"]
                if "labels" in options:
                    fields["labels"] = [
                        item.strip()
                        for item in str(options["labels"]).split(",")
                        if item.strip()
                    ]
                if "importance" in fields:
                    fields["importance"] = float(fields["importance"])
                if not fields:
                    return _error(CommandErrorCode.MISSING_ARGUMENT, "Provide at least one field to update.", usage)
                result = await self._memory.update_memory(
                    args[0],
                    space_id=str(options["space-id"]) if options.get("space-id") else None,
                    **fields,
                )
                memory_view = _memory_view(result or {})
                return _ok(
                    "Memory updated." if result else "Memory was not updated.",
                    data=(
                        {"memory": memory_view}
                        if memory_view
                        else {"id": args[0], "updated": bool(result)}
                    ),
                    effect="state-changed" if result else "no-op",
                    title="Memory updated" if result else "Memory not updated",
                )
            if action == "delete":
                if len(args) != 1:
                    return _error(CommandErrorCode.MISSING_ARGUMENT, "Provide exactly one memory id.", "/memory delete <memory_id>")
                changed = await self._memory.delete_memory(
                    args[0],
                    space_id=str(options["space-id"]) if options.get("space-id") else None,
                )
                return _ok(
                    "Memory deleted." if changed else "Memory was not deleted.",
                    data={"id": args[0], "deleted": changed},
                    effect="state-changed" if changed else "no-op",
                    title="Memory deleted" if changed else "Memory not deleted",
                )
            if action in {"preview", "distill"}:
                thread_id = (session.metadata if session is not None else {}).get(
                    "nowledge_thread_id"
                )
                if not thread_id:
                    return _error(
                        CommandErrorCode.INVALID_STATE,
                        "This session has no synchronized Nowledge Thread yet.",
                        usage,
                    )
                fields = {"space_id": options.get("space-id")} if options.get("space-id") else {}
                if action == "preview":
                    result = await self._memory.preview_distill(thread_id, **fields)
                else:
                    result = await self._memory.distill(thread_id, force_distill=True, **fields)
                return _ok(
                    "Memory distillation preview." if action == "preview" else "Memory distillation completed.",
                    data=result,
                    title="Distillation preview" if action == "preview" else "Memory distillation",
                )
        except (TypeError, ValueError) as exc:
            return _error(CommandErrorCode.INVALID_ARGUMENT, str(exc), usage)
        except Exception as exc:
            error_code = getattr(exc, "code", None)
            if error_code:
                return _error(
                    CommandErrorCode.COMMAND_FAILED,
                    f"Nowledge request failed: {error_code}.",
                    usage,
                )
            raise
        return _error(CommandErrorCode.UNKNOWN_SUBCOMMAND, "Unknown memory action.", usage)
