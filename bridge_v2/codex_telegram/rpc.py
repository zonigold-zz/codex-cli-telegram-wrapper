from __future__ import annotations

import json
import os
import queue
import subprocess
import threading

from . import __version__
from .config import codex_executable


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

    @property
    def alive(self):
        return self.proc is not None and self.proc.poll() is None

    def start(self):
        with self.start_lock:
            if self.alive:
                return
            if self.proc is not None:
                self.close()
            command = [codex_executable(self.config.codex_bin), "app-server", "--listen", "stdio://"]
            environment = {k: v for k, v in {**os.environ, **self.config.child_env}.items()
                           if not k.startswith(("TELEGRAM_", "CODEX_TG_")) and v != self.config.token}
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
            self.request("initialize", {"clientInfo": {"name": "codex_telegram_bridge", "title": "Codex Telegram Bridge",
                                                       "version": __version__},
                                        "capabilities": {"experimentalApi": True}}, timeout=60)
            self.notify("initialized", {})

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
        for stream in (proc.stdout, proc.stderr):
            if stream:
                stream.close()
