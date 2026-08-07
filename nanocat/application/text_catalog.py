"""Hard-coded user-facing text shared by command and runtime boundaries."""

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class UserTextCatalog:
    """Canonical user-facing messages that are not runtime configuration."""

    restart: str = "Restarting NanoCat, will be back soon..."
    restart_done: str = "Bot restarted."
    new_session: str = "New session started."
    stop_tasks: str = "Stopped {count} task(s)."
    stop_idle: str = "No active task to stop."
    error: str = "Sorry, I encountered an error."
    background_done: str = "Background task completed."
    help: str = (
        "## 🐈 NanoCat commands\n\n"
        "- `/new` — Start a new conversation\n\n"
        "- `/stop` — Stop the current task\n\n"
        "- `/restart` — Restart the bot\n\n"
        "- `/model` — View/configure models and reasoning effort\n\n"
        "- `/context` — Show current context info\n\n"
        "- `/whoami` — Show channel/chat identity\n\n"
        "- `/help` — Show available commands\n\n"
        "- `/compact` — Manually compact old session turns\n\n"
        "- `/session` — View and switch sessions\n\n"
        "- `/logs [N]` — Show the tail of runtime logs\n\n"
        "- `/approve once|turn` — Approve the current sensitive operation\n\n"

        "- `/approve forever|cancel` — Enable or revoke session-wide YOLO approval\n\n"
        "- `/deny` — Reject the current sensitive operation\n\n"
        "- `/model effort auto|low|medium|high|xhigh|max` — Set reasoning effort"
    )
    model_info: str = (
        "## 🐈 Model info\n\n"
        "- **Main Model:** `{agent_model}`\n\n"
        "- **Assistant Model:** `{assistant_model}`\n\n"
        "- **Subagent Model:** `{subagent_model}`\n\n"
        "- **Provider:** `{provider_name}`\n\n"

        "- **Max tokens:** `{max_tokens}`\n\n"

        "- **Temperature:** `{temperature}`\n\n"

        "- **Reasoning effort:** `{reasoning_effort}`\n\n"

        "- **Available models:**\n\n{model_choice}\n\n"
        "## Usage\n\n"
        "- `/model add <provider> <model_name>`\n\n"
        "- `/model agent|subagent|assistant <N>`\n\n"

        "- `/model effort auto|low|medium|high|xhigh|max`\n\n"
        "- `/model delete <N>`"
    )
    model_set: str = "{target} model set: {model_name}"
    model_deleted: str = "Model deleted: {model_name}"
    model_added: str = "Model added: {model_name}"
    model_error: str = "Error updating model: {error}"
    model_choice_invalid: str = "Invalid choice number: {choice_number}"
    session_usage: str = (
        "## Usage\n\n"
        "- `/session list [N=10]`\n\n"
        "- `/session view <id>`\n\n"
        "- `/session switch <id>`"
    )
    session_list_empty: str = "No named sessions yet. Keep chatting to auto-generate session names."
    session_list: str = (
        "## Sessions\n\n{items}\n\n"
        "## Usage\n\n"
        "- `/session view <id>`\n\n"
        "- `/session switch <id>`"
    )
    session_view: str = "## {name} ({id})\n\n{turns}"
    session_switched: str = "Switched to session `{session_id}` ({name})."
    session_not_found: str = "Session `{session_id}` not found."
    whoami_info: str = (
        "## 🐈 Session Identity\n\n"
        "- Channel: {channel}\n\n"
        "- Chat ID: {chat_id}\n\n"
        "- Session: {session_id}\n\n"
        "- Key: {session_key}"
    )
    context_panel: str = (
        "## 🐈 Context Usage ({model_name})\n\n"
        "- prompt = {estimated_prompt_tokens}/{context_window_tokens} ({context_usage_percent}%)\n\n"
        "- overflow = {overflow_tokens}/{context_window_tokens} ({overflow_percent}%)\n\n"
        "- msgs = {messages_uncompacted}/{messages_total} ({uncompacted_percent}%)\n\n"
        "- history = {history_messages}/{messages_total}\n"
    )
    no_response: str = "I've completed processing but have no response to give."
    compact_completed: str = "Session compaction completed."
    compact_failed: str = "No completed turns are eligible for compaction."


USER_TEXT = UserTextCatalog()
