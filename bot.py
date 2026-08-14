#!/usr/bin/env python3
"""Private Telegram bridge for the Codex app server."""

import asyncio
import json
import logging
import os
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path


BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
ALLOWED_CHAT_ID = int(os.environ["ALLOWED_CHAT_ID"])
CODEX_BIN = os.environ.get("CODEX_BIN", "/root/.local/bin/codex")
WORKSPACE = os.environ.get("CODEX_WORKSPACE", str(Path.home() / "codex-workspace"))
DEFAULT_REASONING_EFFORT = os.environ.get("CODEX_DEFAULT_EFFORT", "medium")
DEFAULT_PERMISSION_MODE = os.environ.get("CODEX_PERMISSION_MODE", "approve")
TELEGRAM_API = f"https://api.telegram.org/bot{BOT_TOKEN}"
EVENT_LOG = Path(os.environ.get("CODEX_EVENT_LOG", "/root/codex-telegram/events.log"))
TELEGRAM_MESSAGE_LIMIT = 4000
APP_SERVER_STREAM_LIMIT = 16 * 1024 * 1024

PERMISSION_MODES = {
    "ask": {
        "number": "1",
        "label": "Ask for approval",
        "description": "Codex can read and edit files in the current workspace, and run commands. Approval is required to access the internet or edit other files.",
        "approvalPolicy": "on-request",
        "sandbox": "workspace-write",
        "sandboxPolicy": {"type": "workspaceWrite", "networkAccess": False},
    },
    "approve": {
        "number": "2",
        "label": "Approve for me",
        "description": "Only ask for actions detected as potentially unsafe.",
        "approvalPolicy": "never",
        "sandbox": "workspace-write",
        "sandboxPolicy": {"type": "workspaceWrite", "networkAccess": True},
    },
    "full": {
        "number": "3",
        "label": "Full Access",
        "description": "Codex can edit files outside this workspace and access the internet without asking for approval. Exercise caution when using.",
        "approvalPolicy": "never",
        "sandbox": "danger-full-access",
        "sandboxPolicy": {"type": "dangerFullAccess"},
    },
}

PERMISSION_ALIASES = {
    "1": "ask",
    "2": "approve",
    "3": "full",
    "ask": "ask",
    "approve": "approve",
    "full": "full",
}

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[logging.FileHandler(EVENT_LOG), logging.StreamHandler()],
)
os.chmod(EVENT_LOG, 0o600)
LOG = logging.getLogger("codex-telegram")


def telegram_request(method, values=None, timeout=70):
    encoded = urllib.parse.urlencode(values or {}).encode()
    request = urllib.request.Request(
        f"{TELEGRAM_API}/{method}",
        data=encoded,
        method="POST",
    )

    with urllib.request.urlopen(request, timeout=timeout) as response:
        payload = json.loads(response.read().decode())

    if not payload.get("ok"):
        raise RuntimeError(f"Telegram error: {payload}")

    return payload["result"]


async def telegram(method, values=None, timeout=70):
    return await asyncio.to_thread(
        telegram_request,
        method,
        values,
        timeout,
    )


def split_message(text, limit=TELEGRAM_MESSAGE_LIMIT):
    """Split text into non-empty Telegram-safe chunks without losing content."""
    if limit < 1:
        raise ValueError("Message chunk limit must be positive")

    text = text or "(Codex returned an empty response.)"
    return [text[start:start + limit] for start in range(0, len(text), limit)]


async def send_message(chat_id, text):
    sent = []

    for chunk in split_message(text):
        sent.append(await telegram(
            "sendMessage",
            {
                "chat_id": chat_id,
                "text": chunk,
            },
        ))
    return sent


async def edit_message(chat_id, message_id, text):
    return await telegram(
        "editMessageText",
        {
            "chat_id": chat_id,
            "message_id": message_id,
            "text": text[:4000],
        },
    )


async def delete_message(chat_id, message_id):
    return await telegram(
        "deleteMessage",
        {"chat_id": chat_id, "message_id": message_id},
    )


async def typing_loop(chat_id):
    try:
        while True:
            await telegram(
                "sendChatAction",
                {"chat_id": chat_id, "action": "typing"},
            )
            await asyncio.sleep(4)
    except asyncio.CancelledError:
        pass


def normalized_key(value):
    return "".join(character.lower() for character in value if character.isalnum())


def find_value(data, wanted_keys):
    wanted = {normalized_key(key) for key in wanted_keys}

    if isinstance(data, dict):
        for key, value in data.items():
            if normalized_key(str(key)) in wanted:
                return value
        for value in data.values():
            found = find_value(value, wanted_keys)
            if found is not None:
                return found

    if isinstance(data, list):
        for value in data:
            found = find_value(value, wanted_keys)
            if found is not None:
                return found

    return None


def format_duration(minutes):
    if minutes is None:
        return "unknown window"

    minutes = int(minutes)
    if minutes % 10080 == 0:
        return f"{minutes // 10080}w"
    if minutes % 1440 == 0:
        return f"{minutes // 1440}d"
    if minutes % 60 == 0:
        return f"{minutes // 60}h"
    return f"{minutes}m"


def format_reset(timestamp):
    if not timestamp:
        return "unknown"

    reset = datetime.fromtimestamp(timestamp, tz=timezone.utc)
    return reset.strftime("%Y-%m-%d %H:%M UTC")


def normalize_permission_mode(value):
    normalized = str(value or "").strip().lower()
    return PERMISSION_ALIASES.get(normalized)


def numbered_choice(value, options):
    try:
        index = int(str(value).strip()) - 1
    except (TypeError, ValueError):
        return None
    return options[index] if 0 <= index < len(options) else None


def format_goal(goal):
    if not goal:
        return "No goal is set for this conversation.\n\nSet one with: /goal OBJECTIVE"

    objective = goal.get("objective") or "(no objective)"
    status = goal.get("status", "unknown")
    tokens = goal.get("tokensUsed")
    token_budget = goal.get("tokenBudget")
    usage = ""
    if isinstance(tokens, int):
        usage = f"\nTokens: {tokens:,}"
        if isinstance(token_budget, int):
            usage += f" / {token_budget:,}"
    return f"Goal ({status}):\n{objective}{usage}"


class CodexAppServer:
    def __init__(self):
        self.process = None
        self.reader_task = None
        self.request_id = 0
        self.pending = {}
        self.turn_waiters = {}
        self.completed_turns = {}
        self.turn_messages = {}
        self.thread_id = None
        self.models = []
        self.current_model = None
        self.current_effort = None
        self.permission_mode = normalize_permission_mode(DEFAULT_PERMISSION_MODE) or "approve"
        self.token_usage = None
        self.active_turn_id = None
        self.last_event = "not started"
        self.last_event_at = None
        self.progress_updates = asyncio.Queue()
        self.resume_choices = []

    def record_event(self, method, params):
        self.last_event = method or "unknown"
        self.last_event_at = time.time()
        LOG.info("app-server event %s %s", method, json.dumps(params, default=str))

    def progress(self, text):
        if text:
            self.progress_updates.put_nowait(text)

    async def respond(self, request_id, result=None, error=None):
        message = {"id": request_id}
        if error is not None:
            message["error"] = error
        else:
            message["result"] = result or {}
        await self.send(message)

    async def start(self):
        Path(WORKSPACE).mkdir(parents=True, exist_ok=True)
        self.process = await asyncio.create_subprocess_exec(
            CODEX_BIN,
            "app-server",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=None,
            cwd=WORKSPACE,
            limit=APP_SERVER_STREAM_LIMIT,
        )
        self.reader_task = asyncio.create_task(self.read_messages())

        await self.request(
            "initialize",
            {
                "clientInfo": {
                    "name": "telegram_codex_bot",
                    "title": "Telegram Codex Bot",
                    "version": "1.0.0",
                }
            },
        )
        await self.notify("initialized", {})
        await self.refresh_models()
        await self.new_thread()

    async def close(self):
        if self.process and self.process.returncode is None:
            self.process.terminate()
            await self.process.wait()
        if self.reader_task:
            self.reader_task.cancel()

    async def send(self, message):
        if not self.process or not self.process.stdin:
            raise RuntimeError("Codex app-server is not running")

        self.process.stdin.write((json.dumps(message) + "\n").encode())
        await self.process.stdin.drain()

    async def notify(self, method, params):
        await self.send({"method": method, "params": params})

    async def request(self, method, params=None):
        self.request_id += 1
        request_id = self.request_id
        future = asyncio.get_running_loop().create_future()
        self.pending[request_id] = future
        message = {"method": method, "id": request_id}
        if params is not None:
            message["params"] = params
        await self.send(message)
        return await future

    async def read_messages(self):
        try:
            while True:
                line = await self.process.stdout.readline()
                if not line:
                    raise RuntimeError("Codex app-server stopped unexpectedly")

                message = json.loads(line.decode())
                request_id = message.get("id")

                if request_id is not None and request_id in self.pending:
                    future = self.pending.pop(request_id)
                    if "error" in message:
                        future.set_exception(
                            RuntimeError(message["error"].get("message", str(message["error"])))
                        )
                    else:
                        future.set_result(message.get("result", {}))
                    continue

                method = message.get("method")
                params = message.get("params", {})
                self.record_event(method, params)

                # Never leave Codex blocked on a server-initiated request that
                # this Telegram client cannot interactively render.
                if request_id is not None:
                    if method in {
                        "item/commandExecution/requestApproval",
                        "item/fileChange/requestApproval",
                    }:
                        await self.respond(request_id, {"decision": "acceptForSession"})
                    elif method == "item/permissions/requestApproval":
                        requested = params.get("permissions") or params.get("requestedPermissions") or []
                        await self.respond(
                            request_id,
                            {"permissions": requested, "scope": "session"},
                        )
                    elif method == "mcpServer/elicitation/request":
                        await self.respond(
                            request_id, {"action": "accept", "content": {}}
                        )
                    else:
                        await self.respond(
                            request_id,
                            error={
                                "code": -32601,
                                "message": f"Telegram client cannot handle {method}",
                            },
                        )
                    self.progress(f"Automatically approved: {method}")
                    continue

                if method == "thread/tokenUsage/updated":
                    if not params.get("threadId") or params.get("threadId") == self.thread_id:
                        self.token_usage = params

                elif method == "thread/compacted":
                    self.progress("Context compacted")

                elif method in {"item/started", "item/completed"}:
                    item = params.get("item", {})
                    item_type = item.get("type", "work")
                    if method == "item/started" and item_type != "agentMessage":
                        detail = item.get("command") or item.get("query") or item.get("name")
                        if isinstance(detail, list):
                            detail = " ".join(map(str, detail))
                        self.progress(
                            f"Started {item_type}" + (f": {str(detail)[:500]}" if detail else "")
                        )
                    elif method == "item/completed" and item_type != "agentMessage":
                        status = item.get("status", "completed")
                        output = item.get("aggregatedOutput") or item.get("output") or ""
                        if not isinstance(output, str):
                            output = json.dumps(output, default=str)
                        self.progress(
                            f"{item_type} {status}" + (f"\n{output[-1200:]}" if output else "")
                        )

                    if method == "item/completed" and item_type == "agentMessage":
                        turn_id = params.get("turnId")
                        self.turn_messages.setdefault(turn_id, []).append(
                            {
                                "phase": item.get("phase"),
                                "text": item.get("text", ""),
                            }
                        )

                elif method == "turn/completed":
                    turn = params.get("turn", {})
                    turn_id = turn.get("id")
                    waiter = self.turn_waiters.pop(turn_id, None)
                    if waiter and not waiter.done():
                        waiter.set_result(turn)
                    else:
                        self.completed_turns[turn_id] = turn

        except asyncio.CancelledError:
            pass
        except Exception as error:
            for future in self.pending.values():
                if not future.done():
                    future.set_exception(error)
            self.pending.clear()
            for future in self.turn_waiters.values():
                if not future.done():
                    future.set_exception(error)
            self.turn_waiters.clear()

    async def refresh_models(self):
        result = await self.request(
            "model/list",
            {"limit": 100, "includeHidden": False},
        )
        self.models = result.get("data", [])

        if not self.current_model:
            selected = next(
                (model for model in self.models if model.get("isDefault")),
                self.models[0] if self.models else None,
            )
            if selected:
                self.current_model = selected.get("model") or selected.get("id")
                supported = {
                    effort.get("reasoningEffort")
                    for effort in selected.get("supportedReasoningEfforts", [])
                }
                self.current_effort = (
                    DEFAULT_REASONING_EFFORT
                    if DEFAULT_REASONING_EFFORT in supported
                    else selected.get("defaultReasoningEffort")
                )

    def selected_model_info(self):
        return next(
            (
                model
                for model in self.models
                if self.current_model in (model.get("id"), model.get("model"))
            ),
            None,
        )

    def supported_efforts(self):
        model = self.selected_model_info() or {}
        return [
            effort.get("reasoningEffort")
            for effort in model.get("supportedReasoningEfforts", [])
            if effort.get("reasoningEffort")
        ]

    async def new_thread(self):
        params = {
            "model": self.current_model,
            "cwd": WORKSPACE,
            "serviceName": "telegram_codex_bot",
        }
        params.update(self.thread_permission_params())
        result = await self.request("thread/start", params)
        self.thread_id = result["thread"]["id"]
        self.token_usage = None

    def thread_permission_params(self):
        mode = PERMISSION_MODES[self.permission_mode]
        return {
            "approvalPolicy": mode["approvalPolicy"],
            "sandbox": mode["sandbox"],
        }

    def turn_permission_params(self):
        mode = PERMISSION_MODES[self.permission_mode]
        return {
            "approvalPolicy": mode["approvalPolicy"],
            "sandboxPolicy": dict(mode["sandboxPolicy"]),
        }

    def permission_summary(self):
        mode = PERMISSION_MODES[self.permission_mode]
        return f"{mode['number']}. {mode['label']}"

    async def set_permission_mode(self, mode):
        if mode not in PERMISSION_MODES:
            raise ValueError(f"Unknown permission mode: {mode}")
        self.permission_mode = mode
        if not self.thread_id:
            return
        try:
            await self.request(
                "thread/settings/update",
                {
                    "permissions": None,
                    "approvalPolicy": PERMISSION_MODES[mode]["approvalPolicy"],
                    "sandboxPolicy": PERMISSION_MODES[mode]["sandboxPolicy"],
                },
            )
        except Exception as error:
            LOG.warning("Could not update thread permissions immediately: %s", error)

    async def compact_thread(self):
        if not self.thread_id:
            raise RuntimeError("No Codex conversation is active")
        return await self.request(
            "thread/compact/start",
            {"threadId": self.thread_id},
        )

    async def get_goal(self):
        if not self.thread_id:
            raise RuntimeError("No Codex conversation is active")
        result = await self.request(
            "thread/goal/get",
            {"threadId": self.thread_id},
        )
        return result.get("goal")

    async def set_goal(self, objective, status="active"):
        if not self.thread_id:
            raise RuntimeError("No Codex conversation is active")
        result = await self.request(
            "thread/goal/set",
            {
                "threadId": self.thread_id,
                "objective": objective,
                "status": status,
            },
        )
        return result.get("goal")

    async def clear_goal(self):
        if not self.thread_id:
            raise RuntimeError("No Codex conversation is active")
        return await self.request(
            "thread/goal/clear",
            {"threadId": self.thread_id},
        )

    async def list_threads(self, limit=10):
        result = await self.request(
            "thread/list",
            {
                "limit": 25,
                "sortKey": "updated_at",
                "sortDirection": "desc",
                "archived": False,
            },
        )
        self.resume_choices = [
            thread
            for thread in result.get("data", [])
            if thread.get("name") or thread.get("preview")
        ][:limit]
        return self.resume_choices

    async def resume_thread(self, thread_id):
        result = await self.request("thread/resume", {"threadId": thread_id})
        thread = result["thread"]
        self.thread_id = thread["id"]
        self.token_usage = None
        return thread

    async def run(self, prompt):
        params = {
            "threadId": self.thread_id,
            "input": [{"type": "text", "text": prompt}],
            "cwd": WORKSPACE,
            "model": self.current_model,
            "effort": self.current_effort,
        }
        params.update(self.turn_permission_params())
        if self.permission_mode in {"ask", "approve"}:
            params["sandboxPolicy"]["writableRoots"] = [WORKSPACE]
        result = await self.request("turn/start", params)

        turn_id = result["turn"]["id"]
        self.active_turn_id = turn_id
        if turn_id in self.completed_turns:
            turn = self.completed_turns.pop(turn_id)
        else:
            waiter = asyncio.get_running_loop().create_future()
            self.turn_waiters[turn_id] = waiter
            try:
                turn = await waiter
            finally:
                self.active_turn_id = None

        messages = self.turn_messages.pop(turn_id, [])
        final_messages = [
            message["text"]
            for message in messages
            if message["phase"] == "final_answer" and message["text"]
        ]
        if final_messages:
            return "\n\n".join(final_messages)

        all_messages = [message["text"] for message in messages if message["text"]]
        if all_messages:
            return all_messages[-1]

        error = turn.get("error")
        if error:
            raise RuntimeError(error.get("message", str(error)))
        return "Codex completed the turn without a text response."

    async def interrupt(self):
        if not self.active_turn_id:
            return False
        await self.request(
            "turn/interrupt",
            {"threadId": self.thread_id, "turnId": self.active_turn_id},
        )
        return True

    async def rate_limits(self):
        return await self.request("account/rateLimits/read")

    def context_status(self):
        if not self.token_usage:
            return "Context: not available until the first completed message"

        window = find_value(
            self.token_usage,
            ["modelContextWindow", "contextWindow", "contextWindowTokens"],
        )
        last_usage = find_value(self.token_usage, ["lastTokenUsage", "last"])
        used = find_value(
            last_usage if isinstance(last_usage, dict) else self.token_usage,
            ["totalTokens", "totalTokenCount"],
        )

        if not isinstance(window, (int, float)) or not isinstance(used, (int, float)):
            return "Context: usage received, but this Codex version returned an unknown format"

        used_percent = min(100.0, used * 100.0 / window) if window else 0.0
        remaining_percent = max(0.0, 100.0 - used_percent)
        return (
            f"Context: {used:,} / {window:,} tokens\n"
            f"Context used: {used_percent:.1f}%\n"
            f"Context left: {remaining_percent:.1f}%"
        )

    async def status_text(self):
        if self.last_event_at:
            event_age = f"{int(time.time() - self.last_event_at)}s ago"
        else:
            event_age = "never"
        lines = [
            f"Model: {self.current_model}",
            f"Thinking: {self.current_effort or 'default'}",
            f"Permissions: {self.permission_summary()}",
            f"Task running: {'yes' if self.active_turn_id else 'no'}",
            f"Last event: {self.last_event} ({event_age})",
            self.context_status(),
            "",
            "Subscription limits:",
        ]

        result = await self.rate_limits()
        buckets = result.get("rateLimitsByLimitId")
        if not buckets:
            rate_limits = result.get("rateLimits")
            buckets = {rate_limits.get("limitId", "codex"): rate_limits} if rate_limits else {}

        if not buckets:
            lines.append("No ChatGPT rate-limit data was returned.")
            return "\n".join(lines)

        for bucket_id, bucket in buckets.items():
            bucket_name = bucket.get("limitName") or bucket_id
            for window_name in ("primary", "secondary"):
                window = bucket.get(window_name)
                if not window:
                    continue
                used = float(window.get("usedPercent", 0))
                left = max(0.0, 100.0 - used)
                duration = format_duration(window.get("windowDurationMins"))
                reset = format_reset(window.get("resetsAt"))
                lines.append(
                    f"{bucket_name} ({duration}): "
                    f"{used:.1f}% used, {left:.1f}% left; resets {reset}"
                )

        return "\n".join(lines)


async def main():
    offset = 0
    codex = CodexAppServer()
    running_task = None
    pending_selection = None
    await telegram("deleteWebhook", {"drop_pending_updates": "false"})
    await codex.start()

    try:
        while True:
            try:
                updates = await telegram(
                    "getUpdates",
                    {
                        "offset": offset,
                        "timeout": 30,
                        "allowed_updates": json.dumps(["message"]),
                    },
                    timeout=40,
                )

                for update in updates:
                    offset = update["update_id"] + 1
                    message = update.get("message", {})
                    chat_id = message.get("chat", {}).get("id")

                    if chat_id != ALLOWED_CHAT_ID:
                        continue

                    text = message.get("text")
                    if not text:
                        await send_message(chat_id, "For now, send me a text message.")
                        continue

                    parts = text.strip().split()
                    command = parts[0].split("@")[0].lower() if parts else ""
                    argument = parts[1] if len(parts) > 1 else None
                    argument_text = " ".join(parts[1:])

                    if command.startswith("/"):
                        pending_selection = None
                    elif pending_selection:
                        selection = text.strip()
                        pending = pending_selection
                        pending_selection = None

                        if pending["kind"] == "permissions":
                            requested_mode = normalize_permission_mode(selection)
                            if not requested_mode:
                                await send_message(chat_id, "Choose 1, 2, or 3.")
                            else:
                                await codex.set_permission_mode(requested_mode)
                                await send_message(
                                    chat_id,
                                    f"Permissions set to {codex.permission_summary()}. You can send your prompt now.",
                                )
                            continue

                        if pending["kind"] == "thinking":
                            selected_effort = numbered_choice(selection, pending["options"])
                            if selected_effort is None:
                                await send_message(chat_id, "Choose one of the numbered thinking levels.")
                            else:
                                codex.current_effort = selected_effort
                                await send_message(
                                    chat_id,
                                    f"Thinking set to {selected_effort}. You can send your prompt now.",
                                )
                            continue

                        if pending["kind"] == "model":
                            selected = numbered_choice(selection, pending["options"])
                            if selected is None:
                                await send_message(chat_id, "Choose one of the numbered models.")
                            else:
                                codex.current_model = selected.get("model") or selected.get("id")
                                supported = {
                                    effort.get("reasoningEffort")
                                    for effort in selected.get("supportedReasoningEfforts", [])
                                }
                                codex.current_effort = (
                                    DEFAULT_REASONING_EFFORT
                                    if DEFAULT_REASONING_EFFORT in supported
                                    else selected.get("defaultReasoningEffort")
                                )
                                await send_message(
                                    chat_id,
                                    f"Model set to {codex.current_model}. You can send your prompt now.",
                                )
                            continue

                        if pending["kind"] == "resume":
                            selected_thread = numbered_choice(selection, pending["options"])
                            if selected_thread is None:
                                await send_message(chat_id, "Choose one of the numbered conversations.")
                            else:
                                try:
                                    resumed = await codex.resume_thread(selected_thread["id"])
                                except Exception as error:
                                    await send_message(chat_id, f"Could not resume conversation: {error}")
                                else:
                                    title = resumed.get("name") or resumed.get("preview") or resumed["id"]
                                    await send_message(chat_id, f"Resumed conversation:\n{title}")
                            continue

                    if command in ("/start", "/help"):
                        await send_message(
                            chat_id,
                            "Codex is connected.\n\n"
                            "/model — list models\n"
                            "/model MODEL_ID — select a model\n"
                            "/think — list thinking levels\n"
                            "/think LEVEL — select a thinking level\n"
                            "/permissions — show the three permission choices\n"
                            "/permissions 1|2|3 — choose a permission mode\n"
                            "/compact — compact the current context\n"
                            "/goal — show the current goal\n"
                            "/goal OBJECTIVE — set a durable goal\n"
                            "/goal clear — remove the current goal\n"
                            "/status or /debug — live task and last event\n"
                            "/stop — stop the current task\n"
                            "/resume — list recent conversations\n"
                            "/resume NUMBER — resume a listed conversation\n"
                            "/new — start a fresh conversation",
                        )
                        continue

                    if command in {"/permissions", "/permission"}:
                        if running_task and not running_task.done():
                            await send_message(chat_id, "Stop the current task before changing permissions.")
                            continue

                        if not argument_text:
                            current = PERMISSION_MODES[codex.permission_mode]
                            lines = [
                                f"Current permissions: {codex.permission_summary()}",
                                "",
                            ]
                            for mode in PERMISSION_MODES.values():
                                marker = " (current)" if mode["number"] == current["number"] else ""
                                lines.append(f"{mode['number']}. {mode['label']}{marker}")
                                lines.append(f"   {mode['description']}")
                            lines.append("\nReply with 1, 2, or 3 to choose.")
                            pending_selection = {"kind": "permissions"}
                            await send_message(chat_id, "\n".join(lines))
                            continue

                        requested_mode = normalize_permission_mode(argument_text)
                        if requested_mode:
                            await codex.set_permission_mode(requested_mode)
                            await send_message(
                                chat_id,
                                f"Permissions set to {codex.permission_summary()}. You can send your prompt now.",
                            )
                            continue

                        await send_message(chat_id, "Choose 1, 2, or 3. Send /permissions to list the three choices.")
                        continue

                    if command in {"/compact", "/compact_context"}:
                        if running_task and not running_task.done():
                            await send_message(chat_id, "Stop the current task before compacting context.")
                            continue
                        await send_message(chat_id, "Compacting the current context...")
                        try:
                            await codex.compact_thread()
                            await send_message(chat_id, "Context compacted.")
                        except Exception as error:
                            await send_message(chat_id, f"Could not compact context: {error}")
                        continue

                    if command in {"/goal", "/goals"}:
                        if running_task and not running_task.done():
                            await send_message(chat_id, "Stop the current task before changing its goal.")
                            continue
                        goal_argument = argument_text.strip()
                        try:
                            if not goal_argument or goal_argument.lower() in {"status", "show"}:
                                await send_message(chat_id, format_goal(await codex.get_goal()))
                            elif goal_argument.lower() in {"clear", "delete", "off", "none"}:
                                await codex.clear_goal()
                                await send_message(chat_id, "Goal cleared.")
                            else:
                                normalized_status = goal_argument.lower().replace("-", "").replace("_", "")
                                status_aliases = {
                                    "active": "active",
                                    "paused": "paused",
                                    "blocked": "blocked",
                                    "usagelimited": "usageLimited",
                                    "budgetlimited": "budgetLimited",
                                    "complete": "complete",
                                }
                                if normalized_status in status_aliases:
                                    current_goal = await codex.get_goal()
                                    if not current_goal:
                                        await send_message(chat_id, "No goal is set. Use /goal OBJECTIVE first.")
                                    else:
                                        goal = await codex.set_goal(
                                            current_goal["objective"],
                                            status_aliases[normalized_status],
                                        )
                                        await send_message(chat_id, format_goal(goal))
                                else:
                                    goal = await codex.set_goal(goal_argument)
                                    await send_message(chat_id, format_goal(goal))
                        except Exception as error:
                            await send_message(chat_id, f"Could not update goal: {error}")
                        continue

                    if command == "/model":
                        await codex.refresh_models()
                        if not argument:
                            model_lines = []
                            for index, model in enumerate(codex.models, 1):
                                model_id = model.get("model") or model.get("id")
                                marker = " ✓" if model_id == codex.current_model else ""
                                model_lines.append(f"{index}. {model_id}{marker}")
                            pending_selection = {"kind": "model", "options": codex.models}
                            await send_message(
                                chat_id,
                                "Available models:\n" + "\n".join(model_lines)
                                + "\n\nReply with the model number to choose.",
                            )
                            continue

                        selected = numbered_choice(argument, codex.models)
                        if selected is None:
                            selected = next(
                                (
                                    model
                                    for model in codex.models
                                    if argument in (model.get("id"), model.get("model"))
                                ),
                                None,
                            )
                        if not selected:
                            await send_message(chat_id, "Unknown model. Send /model to list models.")
                            continue

                        codex.current_model = selected.get("model") or selected.get("id")
                        supported = {
                            effort.get("reasoningEffort")
                            for effort in selected.get("supportedReasoningEfforts", [])
                        }
                        codex.current_effort = (
                            DEFAULT_REASONING_EFFORT
                            if DEFAULT_REASONING_EFFORT in supported
                            else selected.get("defaultReasoningEffort")
                        )
                        await send_message(
                            chat_id,
                            f"Model set to {codex.current_model}.\n"
                            f"Thinking set to {codex.current_effort or 'default'}.\n"
                            "The change applies to the next message.",
                        )
                        continue

                    if command in ("/think", "/thinking", "/reasoning"):
                        efforts = codex.supported_efforts()
                        if not argument:
                            effort_lines = [
                                f"{index}. {effort}{' ✓' if effort == codex.current_effort else ''}"
                                for index, effort in enumerate(efforts, 1)
                            ]
                            if efforts:
                                pending_selection = {"kind": "thinking", "options": efforts}
                            await send_message(
                                chat_id,
                                "Supported thinking levels for "
                                f"{codex.current_model}:\n"
                                + ("\n".join(effort_lines) or "Default only")
                                + "\n\nReply with the thinking level number to choose.",
                            )
                            continue

                        selected_effort = numbered_choice(argument, efforts) or argument
                        if selected_effort not in efforts:
                            await send_message(
                                chat_id,
                                "Unsupported thinking level. Send /think to list valid levels.",
                            )
                            continue

                        codex.current_effort = selected_effort
                        await send_message(
                            chat_id,
                            f"Thinking set to {selected_effort}. You can send your prompt now.",
                        )
                        continue

                    if command in {"/status", "/debug"}:
                        try:
                            await send_message(chat_id, await codex.status_text())
                        except Exception as error:
                            await send_message(chat_id, f"Could not read status: {error}")
                        continue

                    if command == "/stop":
                        try:
                            stopped = await codex.interrupt()
                            await send_message(
                                chat_id,
                                "Stopping the current task..." if stopped else "No task is running.",
                            )
                        except Exception as error:
                            await send_message(chat_id, f"Could not stop task: {error}")
                        continue

                    if command == "/new":
                        if running_task and not running_task.done():
                            await send_message(chat_id, "Stop the current task with /stop first.")
                            continue
                        await codex.new_thread()
                        await send_message(
                            chat_id,
                            "Started a new conversation with "
                            f"{codex.current_model} ({codex.current_effort or 'default'} thinking).",
                        )
                        continue

                    if command == "/resume":
                        if running_task and not running_task.done():
                            await send_message(chat_id, "Stop the current task with /stop first.")
                            continue

                        if not argument:
                            try:
                                threads = await codex.list_threads()
                            except Exception as error:
                                await send_message(chat_id, f"Could not list conversations: {error}")
                                continue

                            if not threads:
                                await send_message(chat_id, "No saved conversations were found.")
                                continue

                            lines = ["Recent conversations:"]
                            for index, thread in enumerate(threads, 1):
                                title = thread.get("name") or thread.get("preview") or "Untitled conversation"
                                title = " ".join(str(title).split())[:100]
                                timestamp = thread.get("updatedAt") or thread.get("createdAt")
                                when = format_reset(timestamp) if timestamp else "unknown time"
                                current = " (current)" if thread.get("id") == codex.thread_id else ""
                                lines.append(f"{index}. {title} — {when}{current}")
                            lines.append("\nReply with the conversation number to resume.")
                            pending_selection = {"kind": "resume", "options": codex.resume_choices}
                            await send_message(chat_id, "\n".join(lines))
                            continue

                        try:
                            selection = int(argument)
                        except ValueError:
                            await send_message(chat_id, "Use /resume first, then /resume NUMBER.")
                            continue

                        if not codex.resume_choices:
                            await send_message(chat_id, "Send /resume first to load the conversation list.")
                            continue
                        if selection < 1 or selection > len(codex.resume_choices):
                            await send_message(
                                chat_id,
                                f"Choose a number from 1 to {len(codex.resume_choices)}.",
                            )
                            continue

                        selected_thread = codex.resume_choices[selection - 1]
                        try:
                            resumed = await codex.resume_thread(selected_thread["id"])
                        except Exception as error:
                            await send_message(chat_id, f"Could not resume conversation: {error}")
                            continue
                        title = resumed.get("name") or resumed.get("preview") or resumed["id"]
                        await send_message(chat_id, f"Resumed conversation:\n{title}")
                        continue

                    if running_task and not running_task.done():
                        await send_message(
                            chat_id,
                            "Codex is already working. Use /status or /stop.",
                        )
                        continue

                    async def run_prompt(prompt, target_chat_id):
                        typing_task = asyncio.create_task(typing_loop(target_chat_id))
                        progress_message = {"id": None}
                        while not codex.progress_updates.empty():
                            codex.progress_updates.get_nowait()

                        async def report_progress():
                            last_update = None
                            while True:
                                update = await codex.progress_updates.get()
                                await asyncio.sleep(0.8)
                                while not codex.progress_updates.empty():
                                    update = codex.progress_updates.get_nowait()
                                if update != last_update:
                                    text = f"Codex is working…\n\nLatest progress:\n{update}"
                                    if progress_message["id"] is None:
                                        sent = await send_message(target_chat_id, text)
                                        if sent:
                                            progress_message["id"] = sent[0]["message_id"]
                                    else:
                                        try:
                                            await edit_message(
                                                target_chat_id,
                                                progress_message["id"],
                                                text,
                                            )
                                        except Exception as error:
                                            if "message is not modified" not in str(error).lower():
                                                LOG.warning("Could not edit progress message: %s", error)
                                    last_update = update

                        progress_task = asyncio.create_task(report_progress())
                        try:
                            response = await codex.run(prompt)
                            await send_message(target_chat_id, response)
                        except Exception as error:
                            await send_message(
                                target_chat_id,
                                f"Codex error: {error}\n\nSend /new and try again.",
                            )
                        finally:
                            typing_task.cancel()
                            progress_task.cancel()
                            if progress_message["id"] is not None:
                                try:
                                    await delete_message(
                                        target_chat_id,
                                        progress_message["id"],
                                    )
                                except Exception as error:
                                    LOG.warning("Could not delete progress message: %s", error)

                    running_task = asyncio.create_task(run_prompt(text, chat_id))

            except Exception as error:
                print(f"Polling error: {error}", flush=True)
                await asyncio.sleep(5)
    finally:
        await codex.close()


if __name__ == "__main__":
    asyncio.run(main())
