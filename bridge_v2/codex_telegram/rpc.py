from __future__ import annotations

import json
import os
import queue
import subprocess
import threading

from . import __version__
from .config import codex_executable
from .isolation import prepare_home, isolated_environment, disable_remote_control


class RPCError(RuntimeError):
    def __init__(self, message, *, code=None):
        super().__init__(message)
        self.code = code


class AppServer:
    def __init__(self, config, redact, log=lambda _: None):
        self.config, self.redact, self.log = config, redact, log
        self.proc = None
        self.events = queue.Queue()
        self.pending = {}
        self.pending_lock = threading.Lock()
        self.write_lock = threading.Lock()
        self.start_lock = threading.Lock()
        self.counter = 0
        self.generation = 0
        self.reader_threads = []
        self.ready = False

    @property
    def alive(self):
        return self.proc is not None and self.proc.poll() is None

    def start(self, *, setup_sandbox=False):
        with self.start_lock:
            if self.alive and self.ready:
                return
            if self.proc is not None:
                self.close()
            home = prepare_home(self.config)
            command = [codex_executable(self.config.codex_bin),
                       "-c", "sqlite_home=" + json.dumps(str(home)),
                       "-c", "log_dir=" + json.dumps(str(home / "log")),
                       "-c", "notify=[]",
                       "app-server", "--listen", "stdio://"]
            for feature in ("apps", "plugins", "remote_plugin", "plugin_discovery_prefetch", "plugin_sharing"):
                command[1:1] = ["-c", "features." + feature + "=false"]
            environment = {k: v for k, v in {**os.environ, **self.config.child_env}.items()
                           if not k.startswith(("TELEGRAM_", "CODEX_TG_")) and v != self.config.token}
            environment = isolated_environment(self.config, environment)
            disable_remote_control(self.config, environment, command[0])
            flags = subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0
            self.generation += 1
            generation = self.generation
            self.proc = subprocess.Popen(command, cwd=self.config.workdir, env=environment,
                                         stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                         text=True, encoding="utf-8", errors="replace", bufsize=1,
                                         creationflags=flags)
            proc = self.proc
            self.reader_threads = [threading.Thread(target=self._reader, args=(proc, generation), daemon=True, name="codex-rpc"),
                                   threading.Thread(target=self._stderr, args=(proc,), daemon=True, name="codex-stderr")]
            for thread in self.reader_threads:
                thread.start()
            try:
                self._initialize(setup_sandbox=setup_sandbox)
                self.ready = True
            except BaseException:
                self.close()
                raise

    def _initialize(self, *, setup_sandbox=False):
        self.request("initialize", {"clientInfo": {"name": "codex_telegram_bridge", "title": "Codex Telegram Bridge",
                                                   "version": __version__},
                                    "capabilities": {"experimentalApi": True}}, timeout=60)
        self.notify("initialized", {})
        remote = self.request("remoteControl/status/read", None)
        if remote.get("status") != "disabled":
            self.close()
            raise RPCError("독립 CLI의 원격 제어 차단을 확인하지 못했습니다. 대화를 시작하지 않았습니다.")
        resolved = self.request("config/read", {"cwd": str(self.config.workdir), "includeLayers": False}).get("config", {})
        if any(server.get("enabled", True) for server in (resolved.get("mcp_servers") or {}).values()):
            self.close()
            raise RPCError("프로젝트 설정이 MCP 연결을 다시 활성화했습니다. "
                           "독립 CLI는 데스크톱 도구에 연결하지 않고 시작을 중단했습니다.")
        if os.name == "nt":
            readiness = self.request("windowsSandbox/readiness", None)
            if readiness.get("status") != "ready":
                if not setup_sandbox:
                    self.close()
                    raise RPCError("독립 CLI의 Windows 보안 실행 설정이 필요합니다. "
                                   "브릿지를 중지한 상태에서 --tg-setup을 먼저 실행하세요.")
                mode = self.request("config/read", {"cwd": str(self.config.workdir), "includeLayers": False})
                mode = (mode.get("config", {}).get("windows") or {}).get("sandbox")
                if mode not in ("elevated", "unelevated"):
                    self.close()
                    raise RPCError("기존 Windows 보안 실행 모드를 확인할 수 없습니다. 설정을 임의로 바꾸지 않았습니다.")
                self.request("windowsSandbox/setupStart", {"mode": mode, "cwd": str(self.config.workdir)})
                import time
                deadline = time.monotonic() + 180
                while time.monotonic() < deadline:
                    try:
                        event = self.events.get(timeout=1)
                    except queue.Empty:
                        if self.request("windowsSandbox/readiness", None).get("status") == "ready":
                            break
                        continue
                    if event.get("method") == "windowsSandbox/setupCompleted":
                        if not (event.get("params") or {}).get("success"):
                            detail = self.redact((event.get("params") or {}).get("error") or "Native setup failed")[:900]
                            self.close()
                            raise RPCError("독립 CLI의 Windows 보안 실행 설정이 실패했습니다: " + detail)
                        break
                else:
                    if self.request("windowsSandbox/readiness", None).get("status") != "ready":
                        self.close()
                        raise RPCError("Windows 보안 실행 설정 완료를 기다리다 시간 초과됐습니다.")
                if self.request("windowsSandbox/readiness", None).get("status") != "ready":
                    self.close()
                    raise RPCError("Windows 보안 실행 준비 상태를 확인하지 못했습니다.")

    def _stderr(self, proc):
        for line in proc.stderr:
            if line.strip():
                self.log(self.redact(line.rstrip())[:1500])

    def _reader(self, proc, generation):
        try:
            for line in proc.stdout:
                try:
                    message = json.loads(line)
                except (ValueError, TypeError):
                    self.log(self.redact(line[:500]))
                    continue
                if "method" in message:
                    message["_generation"] = generation
                    self.events.put(message)
                elif "id" in message:
                    with self.pending_lock:
                        target = self.pending.get(message["id"])
                    if target:
                        target.put(message)
        finally:
            if generation == self.generation:
                with self.pending_lock:
                    for target in self.pending.values():
                        target.put({"error": {"message": "Codex app-server disconnected"}})
            self.events.put({"method": "bridge/disconnected", "params": {}, "_generation": generation})

    def _write(self, message):
        with self.write_lock:
            if not self.alive:
                raise RPCError("Codex app-server is not running")
            try:
                self.proc.stdin.write(json.dumps(message, ensure_ascii=False) + "\n")
                self.proc.stdin.flush()
            except (OSError, ValueError) as exc:
                raise RPCError(self.redact(exc)) from None

    def request(self, method, params, timeout=45):
        with self.pending_lock:
            self.counter += 1
            request_id = self.counter
            response_queue = queue.Queue()
            self.pending[request_id] = response_queue
        try:
            self._write({"id": request_id, "method": method, "params": params})
            try:
                response = response_queue.get(timeout=timeout)
            except queue.Empty:
                raise RPCError(f"Codex response timeout: {method}") from None
            if "error" in response:
                error = response["error"]
                raise RPCError(self.redact(error.get("message") or error), code=error.get("code"))
            return response.get("result", {})
        finally:
            with self.pending_lock:
                self.pending.pop(request_id, None)

    def notify(self, method, params):
        self._write({"method": method, "params": params})

    def respond(self, request_id, result, generation):
        if generation != self.generation:
            raise RPCError("Expired request from previous Codex process")
        self._write({"id": request_id, "result": result})

    def reject(self, request_id, message, generation):
        if generation == self.generation:
            self._write({"id": request_id, "error": {"code": -32601, "message": message}})

    def close(self):
        self.ready = False
        proc = self.proc
        if proc is None:
            return
        if proc.poll() is None:
            try:
                proc.stdin.close()
                proc.wait(timeout=8)
            except (OSError, subprocess.TimeoutExpired):
                proc.terminate()
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait(timeout=5)
        for thread in self.reader_threads:
            if thread is not threading.current_thread():
                thread.join(timeout=2)
        for index, stream in enumerate((proc.stdout, proc.stderr)):
            # A descendant may retain a pipe after its parent exits. Closing a
            # TextIOWrapper while a reader is blocked can deadlock indefinitely.
            reader = self.reader_threads[index] if index < len(self.reader_threads) else None
            if stream and (reader is None or not reader.is_alive()):
                stream.close()
