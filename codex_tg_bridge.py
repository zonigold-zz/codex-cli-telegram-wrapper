from __future__ import annotations

"""
Telegram <-> Codex CLI bridge.

Made by: Zion Kim
Contact: zonigold@kaist.ac.kr

Why this script exists
----------------------
Codex CLI already works well in a local terminal, but there are cases where a
single trusted operator wants to trigger Codex from a remote Telegram chat. This
script provides a thin bridge for that workflow.

Design goals
------------
- Keep deployment simple: polling only, no webhook server, no database.
- Keep scope narrow: one bot, one chat, one allowed user.
- Minimize message spam: edit one rolling "live tail" message instead of sending
  a new Telegram message for every log line.
- Stay transparent: show command / file / search activity without flooding the
  chat with internal reasoning text.

High-level flow
---------------
1. Poll Telegram updates via getUpdates.
2. Accept messages only from the configured user/chat/topic.
3. Launch `codex exec --json` (or resume an existing session).
4. Parse JSONL events from Codex CLI stdout.
5. Send durable status/result messages through TelegramOutbox.
6. Continuously update one Telegram message with the latest rolling log lines.

This file is intentionally self-contained. The only third-party Python
dependency is `requests`.
"""

import argparse
import datetime as dt
import json
import os
import queue
import shutil
import subprocess
import sys
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Callable

import requests


# -----------------------------------------------------------------------------
# Constants
# -----------------------------------------------------------------------------
TELEGRAM_TEXT_LIMIT = 3800  # stay below Telegram's hard 4096-char limit
MAX_STORED_LOG_LINES = 5000
MAX_LIVE_LINE_LENGTH = 350
MAX_STDERR_TAIL_LINES = 50
MAX_OUTPUT_PREVIEW_LINES = 8


# -----------------------------------------------------------------------------
# Console helpers
# -----------------------------------------------------------------------------
def console_print_safe(text: str) -> None:
    """Print text without crashing on legacy Windows consoles.

    Some Windows terminals still default to cp949 / legacy encodings. If a print
    fails because the console cannot encode a character, fall back to a safely
    escaped ASCII representation instead of crashing the bridge.
    """
    try:
        print(text, flush=True)
    except UnicodeEncodeError:
        print(text.encode("unicode_escape").decode("ascii"), flush=True)


# -----------------------------------------------------------------------------
# Small .env loader (no external dependency)
# -----------------------------------------------------------------------------
def load_env_file(path: str) -> dict[str, str]:
    """Load a very small `.env` file.

    Supported syntax:
    - empty lines
    - comment lines beginning with `#` or `;`
    - optional `export KEY=value`
    - plain `KEY=value`

    The parser is intentionally conservative and does not try to implement every
    shell feature. That keeps behavior predictable for a public example repo.
    """
    env: dict[str, str] = {}
    if not os.path.exists(path):
        return env

    with open(path, "r", encoding="utf-8-sig") as handle:
        for raw_line in handle:
            line = raw_line.strip()
            if not line or line.startswith("#") or line.startswith(";"):
                continue

            if line.lower().startswith("export "):
                line = line[7:].lstrip()

            if "=" not in line:
                continue

            key, value = line.split("=", 1)
            env[key.strip()] = value.strip().strip('"').strip("'")

    return env


def _get_env_float(name: str, default: float, *, min_value: float | None = None) -> float:
    """Read a float environment variable with validation and safe fallback."""
    raw = os.environ.get(name)
    if raw is None or not str(raw).strip():
        return default

    try:
        value = float(raw)
    except (TypeError, ValueError):
        console_print_safe(f"[WARN] Invalid {name}={raw!r}. Using {default}.")
        return default

    if min_value is not None and value < min_value:
        console_print_safe(f"[WARN] {name} below minimum ({min_value}); using {default}.")
        return default

    return value


def _get_env_int(name: str, default: int, *, min_value: int | None = None) -> int:
    """Read an integer environment variable with validation and safe fallback."""
    raw = os.environ.get(name)
    if raw is None or not str(raw).strip():
        return default

    try:
        value = int(raw)
    except (TypeError, ValueError):
        console_print_safe(f"[WARN] Invalid {name}={raw!r}. Using {default}.")
        return default

    if min_value is not None and value < min_value:
        console_print_safe(f"[WARN] {name} below minimum ({min_value}); using {default}.")
        return default

    return value


# -----------------------------------------------------------------------------
# Telegram Bot API client
# -----------------------------------------------------------------------------
class TelegramAPIError(RuntimeError):
    """Raised when Telegram returns `ok: false`. The raw payload is preserved."""

    def __init__(self, payload: dict[str, Any]):
        self.payload = payload
        super().__init__(str(payload))


class TelegramClient:
    """Minimal Telegram Bot API client.

    This bridge needs only a handful of methods, so a small dedicated client is
    easier to audit than pulling in a full bot framework.
    """

    def __init__(self, token: str) -> None:
        self.token = token
        self.base = f"https://api.telegram.org/bot{token}"
        self._lock = threading.Lock()

    def _post(self, method: str, payload: dict[str, Any], timeout: int = 60) -> Any:
        """Call a Telegram Bot API method and return `result`.

        A lock is used to avoid overlapping `requests.post` calls from multiple
        threads, which keeps the interaction model simple and predictable.
        """
        url = f"{self.base}/{method}"
        with self._lock:
            try:
                response = requests.post(url, data=payload, timeout=timeout)
            except requests.RequestException as exc:
                raise RuntimeError(f"Telegram API request failed for {method}: {exc}") from exc

        try:
            data = response.json()
        except ValueError as exc:
            snippet = response.text[:1000]
            raise RuntimeError(
                f"Telegram API returned non-JSON for {method}: {response.status_code} {snippet}"
            ) from exc

        if not data.get("ok"):
            raise TelegramAPIError(data)

        return data["result"]

    def get_me(self) -> dict[str, Any]:
        """Return bot metadata. Used mainly to print the bot username."""
        return self._post("getMe", {}, timeout=30)

    def get_updates(self, offset: int, timeout_s: int = 30) -> list[dict[str, Any]]:
        """Poll new messages using Telegram's long polling API."""
        payload = {
            "offset": offset,
            "timeout": timeout_s,
            "allowed_updates": json.dumps(["message"]),
        }
        return self._post("getUpdates", payload, timeout=timeout_s + 10)

    def create_forum_topic(self, chat_id: int, name: str) -> dict[str, Any]:
        """Create a new topic in a Telegram forum-enabled supergroup."""
        payload = {"chat_id": str(chat_id), "name": name}
        return self._post("createForumTopic", payload, timeout=30)

    def send_message(
        self,
        chat_id: int,
        text: str,
        message_thread_id: int | None = None,
        disable_web_page_preview: bool = True,
    ) -> dict[str, Any]:
        """Send a standard Telegram text message."""
        payload: dict[str, Any] = {
            "chat_id": str(chat_id),
            "text": text,
            "disable_web_page_preview": "true" if disable_web_page_preview else "false",
        }
        if message_thread_id is not None:
            payload["message_thread_id"] = str(message_thread_id)
        return self._post("sendMessage", payload, timeout=60)

    def edit_message_text(
        self,
        chat_id: int,
        message_id: int,
        text: str,
        disable_web_page_preview: bool = True,
    ) -> dict[str, Any]:
        """Edit a previously sent Telegram text message."""
        payload: dict[str, Any] = {
            "chat_id": str(chat_id),
            "message_id": str(message_id),
            "text": text,
            "disable_web_page_preview": "true" if disable_web_page_preview else "false",
        }
        return self._post("editMessageText", payload, timeout=60)


# -----------------------------------------------------------------------------
# Telegram helpers
# -----------------------------------------------------------------------------
def chunk_text(text: str, limit: int = TELEGRAM_TEXT_LIMIT) -> list[str]:
    """Split long text into Telegram-safe chunks.

    Prefer splitting on the last newline before the limit. If no newline exists,
    split exactly at the limit.
    """
    chunks: list[str] = []
    remaining = text

    while len(remaining) > limit:
        cut = remaining.rfind("\n", 0, limit)
        if cut < 0:
            cut = limit
        chunks.append(remaining[:cut].rstrip())
        remaining = remaining[cut:].lstrip()

    if remaining.strip():
        chunks.append(remaining.strip())

    return chunks


class TelegramOutbox:
    """Durable outbound message queue.

    Use this for messages that should remain as standalone Telegram messages:
    - bridge ready notification
    - help text
    - final result
    - final error details

    This queue is intentionally low frequency. High-frequency updates go through
    `LiveTail`, which edits a single Telegram message in place.
    """

    def __init__(self, tg: TelegramClient, chat_id: int, thread_id: int | None, min_interval_sec: float) -> None:
        self.tg = tg
        self.chat_id = chat_id
        self.thread_id = thread_id
        self.min_interval = max(0.0, float(min_interval_sec))

        self._q: queue.Queue[str] = queue.Queue()
        self._last_sent = 0.0

        worker = threading.Thread(target=self._loop, name="telegram-outbox", daemon=True)
        worker.start()

    def send(self, text: str) -> None:
        """Queue one logical message. It will be chunked automatically if needed."""
        if not text:
            return
        for part in chunk_text(text):
            self._q.put(part)

    def _loop(self) -> None:
        while True:
            text = self._q.get()

            now = time.time()
            wait = self.min_interval - (now - self._last_sent)
            if wait > 0:
                time.sleep(wait)

            while True:
                try:
                    self.tg.send_message(self.chat_id, text, message_thread_id=self.thread_id)
                    self._last_sent = time.time()
                    break
                except TelegramAPIError as exc:
                    payload = exc.payload
                    if payload.get("error_code") == 429:
                        params = payload.get("parameters") or {}
                        retry_after = int(params.get("retry_after", 3)) if isinstance(params, dict) else 3
                        console_print_safe(f"[WARN] Telegram 429 while sending. retry_after={retry_after}s")
                        time.sleep(max(1, retry_after))
                        continue

                    console_print_safe(f"[WARN] Telegram send failed: {payload}")
                    break
                except Exception as exc:
                    console_print_safe(f"[WARN] Telegram send exception: {exc}")
                    time.sleep(max(1.0, self.min_interval))
                    continue


class LiveTail:
    """Single editable Telegram message that shows the latest rolling log lines.

    Instead of pushing every log line as a separate message, this class edits one
    Telegram message in place. That keeps the chat readable and avoids large
    backlogs that stop matching the actual current state.
    """

    def __init__(
        self,
        tg: TelegramClient,
        chat_id: int,
        thread_id: int | None,
        *,
        max_lines: int = 40,
        edit_interval_sec: float = 1.0,
    ) -> None:
        self.tg = tg
        self.chat_id = chat_id
        self.thread_id = thread_id

        self.max_lines = max(10, int(max_lines))
        self.edit_interval = max(0.5, float(edit_interval_sec))

        self.message_id: int | None = None
        self.status = "starting"
        self.codex_session_id: str | None = None
        self.started_at = time.time()

        self._lines: deque[str] = deque(maxlen=self.max_lines)
        self._dirty = False
        self._stop = False
        self._lock = threading.Lock()
        self._last_edit = 0.0
        self._last_rendered_text = ""

        self._thread = threading.Thread(target=self._loop, name="telegram-live-tail", daemon=True)

    def start(self) -> None:
        """Send the initial live-tail message and start the edit worker thread."""
        initial_text = self._render()
        msg = self.tg.send_message(self.chat_id, initial_text, message_thread_id=self.thread_id)
        self.message_id = int(msg["message_id"])
        self._last_rendered_text = initial_text
        self._thread.start()

    def stop(self, final_status: str) -> None:
        """Mark the live tail as finished and force one final edit."""
        with self._lock:
            self.status = final_status
            self._dirty = True
            self._stop = True
        self._edit_now(force=True)

    def set_status(self, status: str) -> None:
        """Update the human-readable status shown in the live tail."""
        with self._lock:
            if status != self.status:
                self.status = status
                self._dirty = True

    def set_session(self, session_id: str) -> None:
        """Attach the current Codex session/thread ID to the live view."""
        with self._lock:
            if session_id and session_id != self.codex_session_id:
                self.codex_session_id = session_id
                self._dirty = True

    def add_line(self, line: str) -> None:
        """Append one log line to the rolling buffer."""
        cleaned = line.rstrip("\r\n")
        if not cleaned:
            return
        if len(cleaned) > MAX_LIVE_LINE_LENGTH:
            cleaned = cleaned[: MAX_LIVE_LINE_LENGTH - 1] + "…"

        with self._lock:
            self._lines.append(cleaned)
            self._dirty = True

    def _status_title(self) -> str:
        mapping = {
            "starting": "[Codex starting]",
            "running": "[Codex running]",
            "thinking": "[Codex thinking]",
            "done": "[Codex done]",
            "failed": "[Codex failed]",
        }
        return mapping.get(self.status, "[Codex]")

    def _render(self) -> str:
        """Render the full Telegram text for the rolling live tail message."""
        header = self._status_title()
        elapsed = time.time() - self.started_at

        meta_lines = [
            f"Status: {self.status}",
            f"Elapsed: {elapsed:.1f}s",
        ]
        if self.codex_session_id:
            meta_lines.append(f"Session: {self.codex_session_id}")

        body_lines = list(self._lines)
        if not body_lines:
            body_lines = ["(No logs yet.)"]

        meta_block = "\n".join(meta_lines)
        body_block = "\n".join(body_lines)
        text = f"{header}\n{meta_block}\n\n{body_block}"

        if len(text) <= TELEGRAM_TEXT_LIMIT:
            return text

        # If the rendered message is too long, drop the oldest log lines until it
        # fits. This preserves the newest activity, which is the most valuable
        # part of a live-tail display.
        trimmed_lines = body_lines[:]
        while trimmed_lines:
            body_block = "\n".join(trimmed_lines)
            text = f"{header}\n{meta_block}\n\n{body_block}"
            if len(text) <= TELEGRAM_TEXT_LIMIT:
                return text
            trimmed_lines.pop(0)

        return f"{header}\n{meta_block}\n\n(Log truncated.)"

    def _loop(self) -> None:
        while True:
            time.sleep(0.15)
            with self._lock:
                stop = self._stop
                dirty = self._dirty
            if dirty:
                self._edit_now(force=False)
            if stop:
                return

    def _edit_now(self, force: bool) -> None:
        """Push one edit immediately if throttling allows it."""
        if self.message_id is None:
            return

        now = time.time()
        if not force and (now - self._last_edit) < self.edit_interval:
            return

        with self._lock:
            text = self._render()
            if text == self._last_rendered_text:
                self._dirty = False
                return
            self._dirty = False

        while True:
            try:
                self.tg.edit_message_text(self.chat_id, self.message_id, text)
                self._last_edit = time.time()
                self._last_rendered_text = text
                return
            except TelegramAPIError as exc:
                payload = exc.payload
                description = str(payload.get("description", "")).lower()

                if payload.get("error_code") == 429:
                    params = payload.get("parameters") or {}
                    retry_after = int(params.get("retry_after", 2)) if isinstance(params, dict) else 2
                    console_print_safe(f"[WARN] Telegram 429 while editing. retry_after={retry_after}s")
                    time.sleep(max(1, retry_after))
                    continue

                if payload.get("error_code") == 400 and "message is not modified" in description:
                    self._last_edit = time.time()
                    self._last_rendered_text = text
                    return

                console_print_safe(f"[WARN] Telegram edit failed: {payload}")
                return
            except Exception as exc:
                console_print_safe(f"[WARN] Telegram edit exception: {exc}")
                return


# -----------------------------------------------------------------------------
# Codex JSON parsing helpers
# -----------------------------------------------------------------------------
def _extract_text(value: Any) -> str | None:
    """Extract text from several possible Codex JSON payload shapes."""
    if isinstance(value, str):
        return value

    if isinstance(value, list):
        parts: list[str] = []
        for item in value:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict):
                text = item.get("text") or item.get("content")
                if isinstance(text, str):
                    parts.append(text)
        return "\n".join(parts) if parts else None

    if isinstance(value, dict):
        text = value.get("text") or value.get("content")
        if isinstance(text, str):
            return text

    return None


def _first_lines(text: str, count: int) -> list[str]:
    """Return only the first `count` lines of a multiline string."""
    return text.splitlines()[:count]


@dataclass
class RunStats:
    """Minimal runtime statistics for one Codex turn."""

    started_at: float = field(default_factory=time.time)
    finished_at: float = 0.0
    codex_thread_id: str | None = None
    agent_messages: list[str] = field(default_factory=list)

    @property
    def duration_sec(self) -> float:
        end = self.finished_at or time.time()
        return max(0.0, end - self.started_at)


class CodexRunner:
    """Own the subprocess lifecycle for a single active Codex execution."""

    def __init__(self, codex_bin: str, workdir: str, passthrough_args: list[str], debug_json_console: bool) -> None:
        self.codex_bin = codex_bin
        self.workdir = workdir
        self.passthrough_args = passthrough_args[:]
        self.debug_json_console = debug_json_console

        self._proc_lock = threading.Lock()
        self._proc: subprocess.Popen[str] | None = None

    def is_running(self) -> bool:
        """Return True when a Codex subprocess is currently active."""
        with self._proc_lock:
            return self._proc is not None and self._proc.poll() is None

    def cancel(self) -> bool:
        """Attempt to terminate the active Codex process."""
        with self._proc_lock:
            proc = self._proc

        if proc is None or proc.poll() is not None:
            return False

        try:
            proc.terminate()
            return True
        except Exception:
            return False

    def _set_proc(self, proc: subprocess.Popen[str] | None) -> None:
        with self._proc_lock:
            self._proc = proc

    def _filtered_passthrough_args(self) -> list[str]:
        """Drop JSON flags that the bridge already manages internally."""
        banned = {"--json", "--experimental-json"}
        return [arg for arg in self.passthrough_args if arg not in banned]

    def _start_process(self, command: list[str]) -> subprocess.Popen[str]:
        """Start the Codex subprocess.

        Windows note:
        `codex` is often installed as `codex.cmd`, which is most reliably launched
        through `cmd.exe /c`.
        """
        common_kwargs: dict[str, Any] = {
            "cwd": self.workdir,
            "stdin": subprocess.PIPE,
            "stdout": subprocess.PIPE,
            "stderr": subprocess.PIPE,
            "text": True,
            "encoding": "utf-8",
            "errors": "replace",
            "bufsize": 1,
        }

        if os.name == "nt":
            codex_cmd_path = shutil.which(f"{self.codex_bin}.cmd") or shutil.which(self.codex_bin) or self.codex_bin
            cmd_for_cmdexe = [codex_cmd_path, *command[1:]]
            return subprocess.Popen(["cmd.exe", "/d", "/c", *cmd_for_cmdexe], **common_kwargs)

        return subprocess.Popen(command, **common_kwargs)

    def run_turn(
        self,
        prompt_text: str,
        session_id: str | None,
        *,
        on_session: Callable[[str], None],
        on_status: Callable[[str], None],
        on_log: Callable[[str], None],
    ) -> tuple[str | None, str | None, RunStats, str | None]:
        """Run one Codex turn and translate JSON events into bridge callbacks.

        Returns:
        - new_session_id: Codex session/thread ID if available
        - final_message: concatenated assistant/agent messages captured from JSON
        - stats: runtime metadata
        - error: human-readable failure text, or None on success
        """
        stats = RunStats(started_at=time.time())

        prompt = prompt_text.strip()
        suffix = os.environ.get("CODEX_TG_PROMPT_SUFFIX", "").strip()
        if suffix:
            prompt += "\n\n" + suffix

        if session_id is None:
            command = [self.codex_bin, "exec", "--json"]
        else:
            command = [self.codex_bin, "exec", "resume", session_id, "--json"]

        command += self._filtered_passthrough_args()
        command += ["-"]  # read the prompt from stdin

        try:
            proc = self._start_process(command)
        except FileNotFoundError:
            return None, None, stats, f"Codex binary not found: {self.codex_bin}"
        except Exception as exc:
            return None, None, stats, f"Failed to start Codex: {exc}"

        self._set_proc(proc)
        on_status("running")

        stderr_tail: deque[str] = deque(maxlen=MAX_STDERR_TAIL_LINES)

        def _drain_stderr() -> None:
            """Read stderr continuously and surface only notable lines.

            stderr can be noisy. We keep a short tail for diagnostics and only send
            lines that are likely to help the operator in real time.
            """
            try:
                assert proc.stderr is not None
                for line in proc.stderr:
                    cleaned = line.rstrip("\r\n")
                    if not cleaned:
                        continue

                    stderr_tail.append(cleaned)
                    if (
                        "Under-development features" in cleaned
                        or "Not inside a trusted directory" in cleaned
                        or cleaned.startswith("WARNING")
                        or cleaned.startswith("Error")
                        or cleaned.startswith("ERROR")
                    ):
                        on_log(f"[stderr] {cleaned}")
            except Exception:
                # A broken stderr reader should not crash the main bridge.
                pass

        stderr_thread = threading.Thread(target=_drain_stderr, name="codex-stderr", daemon=True)
        stderr_thread.start()

        try:
            assert proc.stdin is not None
            proc.stdin.write(prompt)
            proc.stdin.close()
        except Exception:
            # If stdin write fails, the main loop below will still surface the
            # subprocess exit error.
            pass

        new_session_id = session_id
        error: str | None = None

        try:
            assert proc.stdout is not None
            for raw_line in proc.stdout:
                line = raw_line.rstrip("\r\n")
                if not line:
                    continue

                if self.debug_json_console:
                    console_print_safe("[JSONL] " + line)

                try:
                    event = json.loads(line)
                except Exception:
                    # If a line is not valid JSON, surface it as raw log text.
                    on_log(line)
                    continue

                event_type = str(event.get("type", ""))

                if event_type in {"thread.started", "session.started"}:
                    thread_id = event.get("thread_id") or event.get("session_id")
                    if isinstance(thread_id, str) and thread_id:
                        new_session_id = thread_id
                        stats.codex_thread_id = thread_id
                        on_session(thread_id)
                        on_log(f"[session] started: {thread_id}")

                elif event_type == "turn.started":
                    on_status("thinking")
                    on_log("[turn] started")

                elif event_type == "item.started":
                    item = event.get("item") or {}
                    item_type = str(item.get("type", ""))

                    # Show only action-centric activity to keep the Telegram feed
                    # useful without becoming noisy.
                    if item_type == "command_execution":
                        command_text = item.get("command") or item.get("cmd") or ""
                        if isinstance(command_text, str) and command_text.strip():
                            on_status("running")
                            on_log(f"> {command_text}")

                    elif item_type in {"file_change", "file_edit", "apply_patch"}:
                        path = item.get("path") or item.get("file") or item.get("filename")
                        if isinstance(path, str) and path:
                            on_log(f"[file] {path}")

                    elif item_type in {"web_search", "web_search_call"}:
                        query = item.get("query")
                        if isinstance(query, str) and query:
                            on_log(f"[search] {query}")

                elif event_type == "item.completed":
                    item = event.get("item") or {}
                    item_type = str(item.get("type", ""))

                    if item_type in {"command_execution", "command_output", "command_result"}:
                        exit_code = item.get("exit_code")
                        stdout = item.get("stdout")
                        stderr = item.get("stderr")

                        if stdout is None:
                            stdout = item.get("output") or item.get("result")
                        if stderr is None:
                            stderr = item.get("error_output")

                        output_text = _extract_text(stdout) if stdout is not None else None
                        error_text = _extract_text(stderr) if stderr is not None else None

                        if exit_code is not None:
                            on_log(f"[exit] {exit_code}")

                        if output_text:
                            preview_lines = _first_lines(output_text, MAX_OUTPUT_PREVIEW_LINES)
                            for preview_line in preview_lines:
                                on_log(preview_line)
                            if len(output_text.splitlines()) > MAX_OUTPUT_PREVIEW_LINES:
                                on_log(
                                    f"… (stdout {len(output_text.splitlines())} lines, showing {MAX_OUTPUT_PREVIEW_LINES})"
                                )

                        if error_text:
                            preview_lines = _first_lines(error_text, MAX_OUTPUT_PREVIEW_LINES)
                            for preview_line in preview_lines:
                                on_log("[stderr] " + preview_line)
                            if len(error_text.splitlines()) > MAX_OUTPUT_PREVIEW_LINES:
                                on_log(
                                    f"… (stderr {len(error_text.splitlines())} lines, showing {MAX_OUTPUT_PREVIEW_LINES})"
                                )

                    elif item_type in {"agent_message", "assistant_message"}:
                        text = _extract_text(item.get("text") or item.get("content"))
                        if text and text.strip():
                            stats.agent_messages.append(text.strip())

                    elif item_type == "error":
                        message = _extract_text(item.get("message") or item.get("text"))
                        if message:
                            on_log("[warn] " + message)

                elif event_type in {"turn.failed", "thread.failed", "session.failed", "error"}:
                    error = str(event.get("error") or event.get("message") or "error")
                    break

        except Exception as exc:
            error = f"Bridge read/parse error: {exc}"

        return_code = proc.wait()
        self._set_proc(None)
        stats.finished_at = time.time()

        if error is None and return_code != 0:
            stderr_text = "\n".join(list(stderr_tail)[-25:]) if stderr_tail else ""
            error = f"Codex exited with code {return_code}"
            if stderr_text:
                error += f"\n\n[stderr tail]\n{stderr_text}"

        final_message: str | None = None
        if stats.agent_messages:
            final_message = "\n\n".join(stats.agent_messages).strip()

        on_status("done" if error is None else "failed")
        return new_session_id, final_message, stats, error


HELP_TEXT = """Commands
/new      Reset the Codex session. The next prompt starts a fresh `codex exec` session.
/cancel   Stop the current Codex run.
/status   Show current bridge status.
/log [N]  Show the most recent N log lines (default: 120).
/help     Show this help text.

Any normal text message is sent to Codex as the next prompt.
"""


# -----------------------------------------------------------------------------
# Utility helpers used by main()
# -----------------------------------------------------------------------------
def build_topic_name(label: str, time_format: str) -> str:
    """Return a human-readable topic name like `Codex Bridge | 03-18 14:30`."""
    try:
        timestamp = dt.datetime.now().strftime(time_format)
    except Exception:
        timestamp = dt.datetime.now().strftime("%m-%d %H:%M")
    return f"{label} | {timestamp}"


def skip_pending_updates(tg: TelegramClient) -> int:
    """Discard stale backlog messages at startup.

    This is often desirable for a remote-control bridge: when the process restarts
    we usually do not want it to replay old prompts that were sent long ago.
    """
    try:
        pending = tg.get_updates(offset=0, timeout_s=0)
    except Exception as exc:
        console_print_safe(f"[WARN] Failed to inspect pending updates: {exc}")
        return 0

    if not pending:
        return 0

    offset = max(int(update.get("update_id", 0)) + 1 for update in pending)
    console_print_safe(f"[INFO] Skipped {len(pending)} pending update(s) at startup.")
    return offset


def run_discovery_mode(tg: TelegramClient, bot_username: str) -> int:
    """Print incoming Telegram metadata so users can fill `.env` safely.

    This avoids the common setup pain point of not knowing the numeric user ID or
    chat ID required by the main bridge.
    """
    console_print_safe("[INFO] Discovery mode is active.")
    console_print_safe(
        "[INFO] Send a message from the Telegram account and chat you want to authorize."
    )
    console_print_safe(
        f"[INFO] Use @{bot_username} in the target chat if you need to wake the bot up first."
        if bot_username
        else "[INFO] Send a message to the bot or target chat."
    )

    offset = 0
    while True:
        try:
            updates = tg.get_updates(offset=offset, timeout_s=30)
        except KeyboardInterrupt:
            print("\n[INFO] Discovery mode stopped.")
            return 0
        except Exception as exc:
            console_print_safe("[WARN] getUpdates failed in discovery mode: " + str(exc))
            time.sleep(1.0)
            continue

        for update in updates:
            offset = max(offset, int(update.get("update_id", 0)) + 1)
            message = update.get("message")
            if not message:
                continue

            from_user = message.get("from") or {}
            chat = message.get("chat") or {}
            text = (message.get("text") or "").strip()
            chat_title = chat.get("title") or chat.get("username") or "(no title)"
            thread_id = message.get("message_thread_id")

            console_print_safe("-" * 72)
            console_print_safe(f"from.id           = {from_user.get('id')}")
            console_print_safe(f"from.username     = {from_user.get('username')}")
            console_print_safe(f"chat.id           = {chat.get('id')}")
            console_print_safe(f"chat.type         = {chat.get('type')}")
            console_print_safe(f"chat.title        = {chat_title}")
            console_print_safe(f"message_thread_id = {thread_id}")
            console_print_safe(f"text              = {text!r}")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Run Codex CLI remotely from Telegram using long polling.",
        add_help=True,
    )
    parser.add_argument("--tg-env", default=os.path.join(os.path.dirname(__file__), ".env"))
    parser.add_argument("--tg-workdir", default=os.getcwd())
    parser.add_argument("--tg-codex-bin", default="codex")
    parser.add_argument("--tg-topic-prefix", default="Codex Telegram Bridge")
    parser.add_argument(
        "--tg-topic-label",
        default="",
        help="Custom text placed before the timestamp when a dedicated Telegram topic is created.",
    )
    parser.add_argument(
        "--tg-topic-time-format",
        default="%m-%d %H:%M",
        help="strftime format string for the timestamp portion of the topic name.",
    )
    parser.add_argument(
        "--tg-no-topic",
        action="store_true",
        help="Send updates to the main chat instead of creating a dedicated forum topic.",
    )
    parser.add_argument(
        "--tg-use-topic",
        action="store_true",
        help="Deprecated compatibility flag. Topic mode is already the default.",
    )
    parser.add_argument(
        "--tg-discover-ids",
        action="store_true",
        help="Print incoming Telegram IDs (user/chat/thread) to help fill `.env`.",
    )
    parser.add_argument(
        "--tg-replay-pending-updates",
        action="store_true",
        help="Process old pending Telegram updates at startup instead of skipping them.",
    )
    parser.add_argument(
        "--tg-debug-json",
        action="store_true",
        help="Print raw Codex JSONL events to the local console.",
    )
    args, passthrough = parser.parse_known_args()

    env_values = load_env_file(args.tg_env)
    for key, value in env_values.items():
        # Respect existing environment variables so CI / shell overrides still win.
        os.environ.setdefault(key, value)

    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    allowed_user_raw = os.environ.get("TELEGRAM_ALLOWED_USER_ID", "").strip()
    chat_id_raw = os.environ.get("TELEGRAM_CHAT_ID", "").strip()

    print(f"[INFO] Bridge script: {os.path.abspath(__file__)}")
    print(f"[INFO] Loaded {len(env_values)} entr{'y' if len(env_values) == 1 else 'ies'} from: {args.tg_env}")
    print("[INFO] TELEGRAM_BOT_TOKEN present:", bool(token), "len=", len(token))
    print("[INFO] TELEGRAM_ALLOWED_USER_ID:", allowed_user_raw or "(missing)")
    print("[INFO] TELEGRAM_CHAT_ID:", chat_id_raw or "(missing)")

    if not token:
        print("Missing TELEGRAM_BOT_TOKEN in .env / environment", file=sys.stderr)
        return 2

    tg = TelegramClient(token)
    me = tg.get_me()
    bot_username = me.get("username", "")
    print(f"[INFO] Bot username: @{bot_username}" if bot_username else "[INFO] Bot username: (none)")

    if args.tg_discover_ids:
        return run_discovery_mode(tg, bot_username)

    if not allowed_user_raw or not chat_id_raw:
        print(
            "Missing TELEGRAM_ALLOWED_USER_ID or TELEGRAM_CHAT_ID in .env / environment",
            file=sys.stderr,
        )
        return 2

    try:
        allowed_user_id = int(allowed_user_raw)
        telegram_chat_id = int(chat_id_raw)
    except ValueError:
        print("TELEGRAM_ALLOWED_USER_ID and TELEGRAM_CHAT_ID must be integers", file=sys.stderr)
        return 2

    workdir = os.path.abspath(args.tg_workdir)
    if not os.path.isdir(workdir):
        print(f"Workdir does not exist or is not a directory: {workdir}", file=sys.stderr)
        return 2

    codex_bin = args.tg_codex_bin
    if os.path.sep not in codex_bin and shutil.which(codex_bin) is None and shutil.which(f"{codex_bin}.cmd") is None:
        print(f"[ERR] Cannot find Codex executable in PATH: {codex_bin}", file=sys.stderr)
        return 2

    use_topic = not args.tg_no_topic
    if args.tg_use_topic:
        if args.tg_no_topic:
            console_print_safe("[WARN] --tg-no-topic overrides --tg-use-topic; using the main chat.")
            use_topic = False
        else:
            console_print_safe("[INFO] --tg-use-topic is already the default behavior.")

    telegram_thread_id: int | None = None
    if use_topic:
        env_label = os.environ.get("CODEX_TG_TOPIC_LABEL", "").strip()
        cli_label = args.tg_topic_label.strip()
        prefix_label = args.tg_topic_prefix.strip()
        topic_label = env_label or cli_label or prefix_label or "Codex Telegram Bridge"

        env_format = os.environ.get("CODEX_TG_TOPIC_TIME_FORMAT", "").strip()
        cli_format = args.tg_topic_time_format.strip()
        time_format = env_format or cli_format or "%m-%d %H:%M"

        topic_name = build_topic_name(topic_label, time_format)
        try:
            topic = tg.create_forum_topic(telegram_chat_id, topic_name)
            telegram_thread_id = int(topic["message_thread_id"])
            print(f"[INFO] Created Telegram topic: {topic_name} (thread_id={telegram_thread_id})")
        except Exception as exc:
            console_print_safe("[WARN] createForumTopic failed. Falling back to the main chat. " + str(exc))
            telegram_thread_id = None
    else:
        print("[INFO] Using the main chat (no dedicated topic).")

    send_interval_sec = _get_env_float("CODEX_TG_SEND_INTERVAL_SEC", 1.0, min_value=0.0)
    tail_lines = _get_env_int("CODEX_TG_TAIL_LINES", 60, min_value=10)
    tail_edit_sec = _get_env_float("CODEX_TG_TAIL_EDIT_SEC", 0.8, min_value=0.1)

    outbox = TelegramOutbox(tg, telegram_chat_id, telegram_thread_id, min_interval_sec=send_interval_sec)
    workdir_display = workdir if workdir.endswith(("/", "\\")) else workdir + os.path.sep
    outbox.send(
        "Bridge is ready.\n"
        f"Workdir: {workdir_display}\n"
        f"Topic mode: {'on' if telegram_thread_id is not None else 'off'}\n"
        "Send /help in Telegram for available commands."
    )

    runner = CodexRunner(
        codex_bin=codex_bin,
        workdir=workdir,
        passthrough_args=passthrough,
        debug_json_console=args.tg_debug_json,
    )

    offset = 0
    if not args.tg_replay_pending_updates:
        offset = skip_pending_updates(tg)

    session_id: str | None = None

    log_lock = threading.Lock()
    last_log_lines: list[str] = []
    running_tail: LiveTail | None = None

    def append_log(line: str) -> None:
        nonlocal last_log_lines, running_tail
        with log_lock:
            last_log_lines.append(line)
            if len(last_log_lines) > MAX_STORED_LOG_LINES:
                last_log_lines = last_log_lines[-4000:]

        if running_tail is not None:
            running_tail.add_line(line)

    def run_in_thread(prompt: str) -> None:
        nonlocal session_id, running_tail, last_log_lines

        with log_lock:
            last_log_lines = []

        tail = LiveTail(
            tg,
            telegram_chat_id,
            telegram_thread_id,
            max_lines=tail_lines,
            edit_interval_sec=tail_edit_sec,
        )
        running_tail = tail
        tail.start()

        start_time = time.time()

        def log(message: str) -> None:
            elapsed = time.time() - start_time
            line = f"[{elapsed:6.1f}s] {message}"
            console_print_safe(line)
            append_log(line)

        def on_session(new_sid: str) -> None:
            tail.set_session(new_sid)

        def on_status(status: str) -> None:
            tail.set_status(status)

        log(f"YOU: {prompt}")

        new_sid, final_message, stats, error = runner.run_turn(
            prompt,
            session_id=session_id,
            on_session=on_session,
            on_status=on_status,
            on_log=log,
        )

        if new_sid:
            session_id = new_sid

        if error:
            tail.stop("failed")
            lines = [
                "Task failed.",
                f"Elapsed: {stats.duration_sec:.1f}s",
            ]
            if new_sid:
                lines.append(f"Session: {new_sid}")
            lines.extend(["", error])
            outbox.send("\n".join(lines))
            running_tail = None
            return

        tail.stop("done")

        lines = [
            "Task completed.",
            f"Elapsed: {stats.duration_sec:.1f}s",
        ]
        if new_sid:
            lines.append(f"Session: {new_sid}")

        if final_message:
            lines.extend(["", "Result:", final_message])
        else:
            lines.extend(
                [
                    "",
                    "No final assistant message was captured from the Codex JSON events.",
                ]
            )

        outbox.send("\n".join(lines))
        running_tail = None

    print("[INFO] Listening via getUpdates... (Ctrl+C to stop)")
    print("[INFO] workdir =", workdir)
    print("[INFO] Passthrough Codex args:", passthrough)
    print(
        f"[INFO] Outbox pacing: {send_interval_sec}s | "
        f"LiveTail: lines={tail_lines}, edit={tail_edit_sec}s"
    )

    while True:
        try:
            updates = tg.get_updates(offset=offset, timeout_s=30)
        except KeyboardInterrupt:
            print("\n[INFO] Stopped.")
            return 0
        except Exception as exc:
            console_print_safe("[WARN] getUpdates failed: " + str(exc))
            time.sleep(1.0)
            continue

        for update in updates:
            offset = max(offset, int(update.get("update_id", 0)) + 1)
            message = update.get("message")
            if not message:
                continue

            from_user = message.get("from") or {}
            if from_user.get("id") != allowed_user_id:
                continue

            chat = message.get("chat") or {}
            if chat.get("id") != telegram_chat_id:
                continue

            if telegram_thread_id is not None and message.get("message_thread_id") != telegram_thread_id:
                continue

            text = (message.get("text") or "").strip()
            if not text:
                continue

            if text in {"/help", f"/help@{bot_username}"}:
                outbox.send(HELP_TEXT)
                continue

            if text in {"/status", f"/status@{bot_username}"}:
                outbox.send(
                    "Status\n"
                    f"- Running: {runner.is_running()}\n"
                    f"- Session: {session_id or '(not started yet)'}\n"
                    f"- Workdir: {workdir}\n"
                    f"- Topic mode: {'on' if telegram_thread_id is not None else 'off'}"
                )
                continue

            if text in {"/new", f"/new@{bot_username}"}:
                if runner.is_running():
                    outbox.send("Codex is currently running. Use /cancel first, then try again.")
                    continue
                session_id = None
                outbox.send("Session reset. The next prompt will start a fresh `codex exec` session.")
                continue

            if text in {"/cancel", f"/cancel@{bot_username}"}:
                cancelled = runner.cancel()
                outbox.send("Cancel request sent." if cancelled else "There is no active Codex run to cancel.")
                continue

            if text.startswith("/log"):
                parts = text.split()
                count = 120
                if len(parts) >= 2:
                    try:
                        count = int(parts[1])
                    except Exception:
                        count = 120

                with log_lock:
                    lines = last_log_lines[-max(10, min(count, 800)) :]
                outbox.send("\n".join(lines) if lines else "(No stored log lines yet.)")
                continue

            if runner.is_running():
                outbox.send("Codex is already running. Wait for it to finish or use /cancel.")
                continue

            worker = threading.Thread(target=run_in_thread, args=(text,), name="codex-turn", daemon=True)
            worker.start()


if __name__ == "__main__":
    raise SystemExit(main())
