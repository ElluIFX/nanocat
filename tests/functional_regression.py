"""Functional regression against a real runtime and the real LLM API.

Builds the actual composition root (build_runtime) on the local `data/`
workdir, exercises the structured control service, and runs one real provider
turn. Network channels are constructed but never started. Sessions created
here use a scratch channel and are deleted (to trash) afterwards.

Run with:  uv run python tests/functional_regression.py
"""

from __future__ import annotations

import asyncio
import sys
from typing import Any

from nanocat.application.control import ApplicationControlService
from nanocat.runtime.launcher import build_runtime

RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    RESULTS.append((name, bool(condition), detail))
    suffix = f"  — {detail}" if detail and not condition else ""
    print(f"{'PASS' if condition else 'FAIL'}  {name}{suffix}")


async def main() -> int:
    runtime = build_runtime(workdir="data")
    engine = runtime.agent.engine
    control = ApplicationControlService(
        engine=engine,
        config=runtime.config,
        session_manager=runtime.session_manager,
        intervention=runtime.intervention,
        supervisor=runtime.supervisor,
    )
    base = {"channel": "uitest", "chat_id": "regression", "principal_id": "local"}
    created_session_ids: list[str] = []
    original_choices = list(runtime.config.agents.defaults.model_choice)

    async def run(action: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        return await control.execute(action, {**base, **(params or {})})

    try:
        snapshot = await run("runtime.snapshot")
        check("snapshot ok", snapshot.get("ok"))
        data = snapshot.get("data") or {}
        check("snapshot identity", (data.get("identity") or {}).get("channel") == "uitest")
        check("snapshot catalog", len(data.get("catalog") or []) >= 25)
        check("snapshot model", bool((data.get("models") or {}).get("effective", {}).get("agent")))

        new = await run("session.new")
        check("session.new ok", new.get("ok"), str(new.get("error")))
        new_id = (new.get("data") or {}).get("session_id")
        if new_id:
            created_session_ids.append(new_id)

        listed = await run("sessions.list")
        ids = [s.get("id") for s in (listed.get("data") or {}).get("sessions", [])]
        check("sessions.list contains new session", new_id in ids, str(ids))

        renamed = await run("session.rename", {"session_id": new_id, "name": "regression"})
        check("session.rename ok", renamed.get("ok"))

        models = await run("models.state")
        choices = ((models.get("data") or {}).get("models") or {}).get("choices") or []
        check("models.state ok", models.get("ok") and bool(choices))

        added = await run("model.add", {"provider": "scratch", "model": "regression-model"})
        check("model.add ok", added.get("ok"), str(added.get("error")))
        updated = await run(
            "model.update",
            {"old_model": "scratch/regression-model", "provider": "scratch", "model": "renamed"},
        )
        check("model.update ok", updated.get("ok"), str(updated.get("error")))
        choices_now = (
            ((updated.get("data") or {}).get("models") or {}).get("choices") or []
        )
        check("model.update renames entry", "scratch/renamed" in choices_now, str(choices_now))
        removed = await run("model.remove", {"model": "scratch/renamed"})
        check("model.remove ok", removed.get("ok"), str(removed.get("error")))
        blocked = await run("model.remove", {"model": runtime.config.agents.defaults.model})
        check("model.remove refuses assigned model", not blocked.get("ok"))

        bad_select = await run("model.select", {"slot": "agent", "model": "bogus/model"})
        check("model.select rejects unknown model", not bad_select.get("ok"))

        effort = await run("model.set_effort", {"value": "auto"})
        check("model.set_effort ok", effort.get("ok"), str(effort.get("error")))

        compact = await run("compact.status")
        compact_data = (compact.get("data") or {}).get("compact") or {}
        check(
            "compact.status structured",
            compact.get("ok") and "estimated_prompt_tokens" in compact_data,
        )

        logs = await run("logs.tail", {"count": 5})
        check("logs.tail ok", logs.get("ok") and "lines" in (logs.get("data") or {}))

        help_result = await run("command_execute", {"text": "/help"})
        content = str((help_result.get("data") or {}).get("content") or "")
        check("command_execute /help", help_result.get("ok") and "/compact" in content)

        cron_list = await run("command_execute", {"text": "/cron list"})
        check(
            "command_execute /cron list",
            cron_list.get("ok") and (cron_list.get("data") or {}).get("routed") == "structured",
        )

        cancel = await run("turn.cancel")
        check("turn.cancel idle ok", cancel.get("ok"))

        approve_idle = await run("approval.respond", {"approval_action": "once"})
        check("approval.respond without pending handled", approve_idle.get("ok"))

        # ---- real provider turn -------------------------------------------
        response = await runtime.agent.process_direct(
            "Reply with exactly: PONG",
            channel="uitest",
            chat_id="regression",
            principal_id="local",
            transient=True,
        )
        check("real LLM turn returns content", bool(response.strip()))
        check("real LLM turn answered PONG", "PONG" in response, response[:120])
        active = runtime.session_manager.get_or_create("uitest", "regression")
        if active.id not in created_session_ids:
            created_session_ids.append(active.id)
        # process_direct defaults to the "cli:direct" system session store
        system_session = runtime.session_manager.get_system_session("cli:direct")
        assistant_texts = [
            str(m.get("content") or "")
            for m in system_session.messages
            if m.get("role") == "assistant"
        ]
        check(
            "turn persisted to session",
            any("PONG" in text for text in assistant_texts),
            f"{len(system_session.messages)} message(s)",
        )

        busy_during = await run("runtime.snapshot")
        check("snapshot after turn ok", busy_during.get("ok"))
    finally:
        # restore the real model catalog regardless of test outcome
        try:
            if list(runtime.config.agents.defaults.model_choice) != original_choices:
                runtime.config.agents.defaults.model_choice = original_choices
                from nanocat.config.loader import save_config

                save_config(runtime.config)
        except Exception as e:  # noqa: BLE001 - restore best effort
            print(f"config restore warning: {e}")
        for session_id in created_session_ids:
            try:
                runtime.session_manager.delete_session("uitest", session_id)
            except Exception as e:  # noqa: BLE001 - cleanup best effort
                print(f"cleanup warning for {session_id}: {e}")
        # remove the scratch system session produced by the direct turn
        try:
            runtime.session_manager._cache.pop("cli:direct", None)
            scratch = runtime.session_manager._system_dir / "cli_direct.jsonl"
            scratch.unlink(missing_ok=True)
        except Exception as e:  # noqa: BLE001 - cleanup best effort
            print(f"cleanup warning for system session: {e}")
        await runtime.agent.close()
        await runtime.bus.close()

    failed = [r for r in RESULTS if not r[1]]
    print(f"\n{len(RESULTS) - len(failed)}/{len(RESULTS)} checks passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
