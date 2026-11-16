from __future__ import annotations

import json
import logging
import random
import threading
import time

import requests


class TelegramError(RuntimeError):
    def __init__(self, code, description, retry_after=None):
        self.code, self.retry_after = code, retry_after
        super().__init__(f"Telegram {code}: {description}")

    @property
    def permanent(self):
        return self.code in (400, 401, 403, 404)


class Telegram:
    def __init__(self, token, redact):
        self.base = "https://api.telegram.org/bot" + token + "/"
        self.redact = redact
        self.read = requests.Session()
        self.write = requests.Session()
        self.write_lock = threading.Lock()

    def call(self, method, data, *, poll=False, timeout=60):
        session = self.read if poll else self.write
        try:
            if poll:
                response = session.post(self.base + method, json=data, timeout=(10, timeout))
            else:
                with self.write_lock:
                    response = session.post(self.base + method, json=data, timeout=(10, timeout))
            try:
                body = response.json()
            except ValueError:
                raise TelegramError(response.status_code if response.status_code >= 400 else 502,
                                    "Non-JSON response") from None
            if not body.get("ok"):
                retry = (body.get("parameters") or {}).get("retry_after")
                raise TelegramError(int(body.get("error_code", response.status_code)),
                                    self.redact(body.get("description", "API failure")), retry)
            return body.get("result")
        except requests.RequestException as exc:
            # Request exception strings can contain the token-bearing URL.
            raise TelegramError(0, self.redact(type(exc).__name__)) from None

    def updates(self, offset, timeout=30):
        return self.call("getUpdates", {"offset": offset, "timeout": timeout,
                         "allowed_updates": ["message", "callback_query"]}, poll=True, timeout=timeout + 25)

    def reset_read(self):
        self.read.close()
        self.read = requests.Session()

    def close(self):
        self.read.close()
        self.write.close()


def chunks(text, limit=3500):
    # Telegram counts UTF-16 code units, including two units for non-BMP symbols.
    part, units = [], 0
    for char in text:
        width = 2 if ord(char) > 0xFFFF else 1
        if units + width > limit:
            yield "".join(part)
            part, units = [], 0
        part.append(char)
        units += width
    if part:
        yield "".join(part)


class Sender:
    def __init__(self, tg, store, config, stop, redact):
        self.tg, self.store, self.config = tg, store, config
        self.stop, self.redact = stop, redact
        self.thread = threading.Thread(target=self.run, daemon=True, name="telegram-outbox")
        self.next_send = 0.0

    def send(self, text, *, buttons=None, priority=20, dedupe=None):
        parts = list(chunks(self.redact(text)))
        for i, part in enumerate(parts):
            payload = {"chat_id": self.config.chat_id, "text": part,
                       "link_preview_options": {"is_disabled": True}}
            topic = self.store.get("telegram_topic")
            if topic is not None:
                payload["message_thread_id"] = topic
            if buttons and i == len(parts) - 1:
                payload["reply_markup"] = {"inline_keyboard": buttons}
            self.store.queue("sendMessage", payload, priority,
                             f"{dedupe}:{i}" if dedupe else None)

    def edit(self, message_id, text, key):
        payload = {"chat_id": self.config.chat_id, "message_id": message_id,
                   "text": next(chunks(self.redact(text))) if text else "…"}
        self.store.queue("editMessageText", payload, 50, key)

    def run(self):
        while not self.stop.is_set():
            row = self.store.next_outbound()
            if row is None:
                self.stop.wait(0.15)
                continue
            if self.stop.wait(max(0, self.next_send - time.monotonic())):
                break
            try:
                result = self.tg.call(row["kind"], json.loads(row["payload"]))
                if row.get("dedupe", "") and row["dedupe"].startswith("running:") and isinstance(result, dict):
                    job_id = row["dedupe"].split(":")[1]
                    if result.get("message_id"):
                        self.store.set("live:" + job_id, result["message_id"])
                self.store.outbound_done(row)
                self.next_send = time.monotonic() + self.config.send_interval
                self.store.set("last_send_ok", time.time())
            except TelegramError as exc:
                text = self.redact(exc)
                if exc.code == 400 and "message is not modified" in text.lower():
                    self.store.outbound_done(row)
                    continue
                delay = float(exc.retry_after or min(60, 2 ** min(row["attempts"], 6)))
                delay += random.uniform(0, 0.3)
                self.store.outbound_error(row, text, delay, permanent=exc.permanent)
                self.store.audit("telegram_send_error", {"code": exc.code, "delay": delay,
                                                          "permanent": exc.permanent})
                logging.warning("%s", text)
                self.next_send = time.monotonic() + delay
            except Exception as exc:
                self.store.outbound_error(row, self.redact(exc), 5)
                logging.error("Outbox: %s", self.redact(exc))
                self.next_send = time.monotonic() + 5
