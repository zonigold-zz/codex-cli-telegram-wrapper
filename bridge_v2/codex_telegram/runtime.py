from __future__ import annotations

import ctypes
import json
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import signal
import tempfile
import threading
import time

from . import __version__
from .engine import Engine
from .rpc import AppServer
from .store import Store
from .telegram import Sender, Telegram, TelegramError


class InstanceLock:
    """OS-owned lock: automatically released on crash, shared across state folders."""
    def __init__(self, bot_key):
        self.bot_key, self.handle, self.file = bot_key, None, None

    def __enter__(self):
        if os.name == "nt":
            kernel = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_bool, ctypes.c_wchar_p]
            kernel.CreateMutexW.restype = ctypes.c_void_p
            kernel.CloseHandle.argtypes = [ctypes.c_void_p]
            self.kernel = kernel
            self.handle = kernel.CreateMutexW(None, False, "Local\\CodexTelegram-v2-" + self.bot_key)
            if not self.handle:
                raise OSError(ctypes.get_last_error(), "Cannot create bridge mutex")
            if ctypes.get_last_error() == 183:
                kernel.CloseHandle(self.handle)
                self.handle = None
                raise RuntimeError("같은 봇의 브릿지가 이미 실행 중입니다.")
        else:
            import fcntl
            self.file = open(Path(tempfile.gettempdir()) / ("codex-telegram-" + self.bot_key + ".lock"), "a+")
            try:
                fcntl.flock(self.file, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                self.file.close()
                raise RuntimeError("같은 봇의 브릿지가 이미 실행 중입니다.") from None
        return self

    def __exit__(self, *args):
        if self.handle:
            self.kernel.CloseHandle(self.handle)
        if self.file:
            self.file.close()


class SafeLogFilter(logging.Filter):
    def __init__(self, redact):
        super().__init__()
        self.redact = redact

    def filter(self, record):
        record.msg = self.redact(record.getMessage())
        record.args = ()
        return True


def setup_logging(state_dir, redact):
    state_dir.mkdir(parents=True, exist_ok=True)
    handler = RotatingFileHandler(state_dir / "bridge.log", maxBytes=2_000_000, backupCount=3, encoding="utf-8")
    handler.addFilter(SafeLogFilter(redact))
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.addHandler(handler)
    return handler


def bootstrap_topic(tg, store, config):
    if config.no_topic:
        store.set("telegram_topic", None)
    elif config.thread_id:
        store.set("telegram_topic", config.thread_id)
    elif store.get("telegram_topic") is None:
        # If a send timed out after server acceptance, avoid creating duplicate topics on restart.
        if store.get("topic_create_uncertain"):
            raise RuntimeError("토픽 생성 결과를 확인하지 못했습니다. TELEGRAM_THREAD_ID를 지정해 재시작하세요.")
        store.set("topic_create_uncertain", True)
        topic = tg.call("createForumTopic", {"chat_id": config.chat_id,
                         "name": (config.topic_label + " · v2")[:120]})
        store.set("telegram_topic", int(topic["message_thread_id"]))
        store.set("topic_create_uncertain", False)


def serve(config, redact):
    config.state_dir.mkdir(parents=True, exist_ok=True)
    log_handler = setup_logging(config.state_dir, redact)
    with InstanceLock(config.bot_key):
        store = Store(config.state_dir / "bridge.sqlite3")
        old_bot = store.get("bot_key")
        if old_bot and old_bot != config.bot_key:
            raise RuntimeError("다른 봇의 상태 폴더입니다. 별도 상태 폴더를 지정하세요.")
        store.set("bot_key", config.bot_key)
        old_scope = store.get("scope")
        scope = {"chat": config.chat_id, "user": config.user_id, "workdir": str(config.workdir)}
        if old_scope and old_scope != scope:
            raise RuntimeError("다른 프로젝트/운영자의 상태 폴더입니다.")
        store.set("scope", scope)
        if store.get("model") is None and config.model:
            store.set("model", config.model)
        if store.get("effort") is None and config.effort:
            store.set("effort", config.effort)
        store.set("runtime_status", "starting")
        store.set("pid", os.getpid())
        store.set("version", __version__)
        shutdown_file = config.state_dir / "control.stop"
        shutdown_file.unlink(missing_ok=True)
        tg = Telegram(config.token, redact)
        main_stop, read_stop, send_stop = threading.Event(), threading.Event(), threading.Event()
        sender = Sender(tg, store, config, send_stop, redact)
        rpc = AppServer(config, redact, lambda text: logging.info("Codex: %s", redact(text)))
        engine = Engine(config, store, rpc, sender, redact)
        initialized = False

        def request_stop(*_):
            main_stop.set()

        signal.signal(signal.SIGTERM, request_stop)
        signal.signal(signal.SIGINT, request_stop)
        if hasattr(signal, "SIGBREAK"):
            signal.signal(signal.SIGBREAK, request_stop)

        def receiver():
            failures, episode = 0, False
            while not read_stop.is_set():
                try:
                    updates = tg.updates(store.get("offset", 0))
                    store.ingest(updates)
                    store.set("last_poll_ok", time.time())
                    store.set("poll_error", None)
                    if episode:
                        sender.send("Telegram 연결이 복구됐습니다. 저장된 지시와 결과를 이어서 처리합니다.", priority=0)
                        store.set("runtime_status", "running")
                    failures, episode = 0, False
                except TelegramError as exc:
                    failures += 1
                    delay = float(exc.retry_after or min(60, 2 ** min(failures - 1, 6)))
                    store.set("poll_error", {"code": exc.code, "time": time.time(), "message": redact(exc)})
                    store.audit("telegram_poll_error", {"code": exc.code, "retry_seconds": delay})
                    logging.warning("Polling: %s", redact(exc))
                    if exc.code in (401, 403, 409):
                        store.set("runtime_status", "receive_blocked")
                        engine.store.set("auto_mode", "paused")
                        if not episode:
                            sender.send(f"Telegram 수신을 확인해야 합니다 ({exc.code}). "
                                        "인증·권한·다른 수신 프로세스를 확인하세요. 기존 작업은 중복 실행하지 않습니다.", priority=0)
                        episode = True
                        delay = max(30, delay)
                    else:
                        episode = True
                        tg.reset_read()
                    read_stop.wait(delay)
                except Exception as exc:
                    logging.error("Receiver: %s", redact(exc))
                    read_stop.wait(5)

        try:
            # A startup network outage is recoverable. Authorization errors require intervention.
            failures = 0
            while not main_stop.is_set():
                if shutdown_file.exists():
                    main_stop.set()
                    break
                try:
                    me = tg.call("getMe", {})
                    store.set("bot_username", me.get("username"))
                    break
                except TelegramError as exc:
                    failures += 1
                    store.set("startup_error", redact(exc))
                    logging.warning("Startup: %s", redact(exc))
                    if exc.permanent:
                        raise
                    main_stop.wait(min(60, 2 ** min(failures, 6)))
            if main_stop.is_set():
                return 0
            bootstrap_topic(tg, store, config)
            rpc.start()
            engine.validate_add_dirs()
            catalog = engine.models()
            store.set("model_catalog", catalog)
            sender.thread.start()
            engine.start()
            reader = threading.Thread(target=receiver, daemon=True, name="telegram-poll")
            reader.start()
            initialized = True
            store.set("runtime_status", "running")
            last_heartbeat = 0
            sender.send(f"Codex Bridge v{__version__} 준비 완료\n프로젝트: {config.workdir}\n"
                        f"모델: {engine.selected_model() or '기본값'}\n"
                        "/model · /effort · /plan · /status · /help\n"
                        "대화와 미전송 결과를 복구합니다. 자동 진행은 검토한 계획에서만 시작됩니다.", priority=0)
            while not main_stop.wait(0.15):
                if time.monotonic() - last_heartbeat >= 10:
                    store.set("heartbeat", time.time())
                    last_heartbeat = time.monotonic()
                if shutdown_file.exists():
                    break
                for row in store.pending_inbox():
                    try:
                        engine.handle_update(json.loads(row["payload"]))
                        store.finish_inbox(row["id"])
                    except Exception as exc:
                        # A malformed command must not repeatedly invoke a job or stop polling.
                        store.audit("input_error", {"update": row["id"], "error": redact(exc)})
                        sender.send("요청 처리 오류: " + redact(exc), priority=0,
                                    dedupe=f"input-error:{row['id']}")
                        store.finish_inbox(row["id"])
        finally:
            read_stop.set()
            if initialized:
                engine.close()
                sender.send("브릿지를 정상 종료했습니다. 대화와 진행 기록은 보존했습니다.", priority=0)
                until = time.monotonic() + 4
                while store.next_outbound() and time.monotonic() < until:
                    time.sleep(0.1)
            else:
                rpc.close()
            send_stop.set()
            if sender.thread.is_alive():
                sender.thread.join(timeout=2)
            store.set("runtime_status", "stopped")
            store.set("pid", None)
            shutdown_file.unlink(missing_ok=True)
            tg.close()
            # Receiver is a daemon; do not block shutdown on an in-flight 30-second poll.
            log_handler.flush()
    return 0
