from __future__ import annotations

from collections import deque
import hashlib
import json
import logging
import os
from pathlib import Path
import queue
import shlex
import shutil
import threading
import time

from .policy import (PLAN_SCHEMA, RESULT_SCHEMA, SUGGEST_SCHEMA, parse_object,
                     plan_hash, routine_question, sensitive_question, step_prompt, validate_plan)
from .rpc import RPCError


BRIDGE_INSTRUCTIONS = """You are Codex, accessed through a single-operator Telegram bridge.
Follow the user's instructions, project AGENTS.md, data protections, and actual permission decisions.
Use already supplied information and existing authorization; avoid repeated confirmation questions.
Stay within the approved goal and scope. A denied permission is not a workflow question.
Report genuine blockers honestly. Do not run extra work after completion or user cancellation.
For planning-only requests, return the requested plan without implementing it.
"""


class RunCancelled(RuntimeError):
    pass


class Engine:
    def __init__(self, config, store, rpc, sender, redact):
        self.config, self.store, self.rpc, self.sender, self.redact = config, store, rpc, sender, redact
        self.stop = threading.Event()
        self.cancel = threading.Event()
        self.control = threading.RLock()
        self.condition = threading.Condition()
        self.active_job = None
        self.active_thread = None
        self.active_turn = None
        self.active_model = None
        self.active_deadline = None
        self.completed = {}
        self.messages = {}
        self.pending_requests = {}
        self.logs = deque(maxlen=500)
        self.live_id = None
        self.last_live = 0.0
        self.models_cache = []
        self.loaded_thread = None
        self.loaded_generation = None
        self.loaded_permission_profile = None
        self.active_permission_profile = None
        self.project_config_cache = None
        self.project_config_generation = None
        self.event_thread = threading.Thread(target=self._events, daemon=True, name="codex-events")
        self.worker_thread = threading.Thread(target=self._work, daemon=True, name="codex-work")

    def start(self):
        interrupted = self.store.recover()
        if interrupted:
            self.store.set("auto_mode", "paused")
            self.say("이전 실행 중 브릿지가 종료됐습니다. 중복 실행을 막기 위해 해당 작업은 확인 대기로 보존했습니다. "
                     "대화는 유지됩니다. /status로 확인하세요.", priority=0)
        self.event_thread.start()
        self.worker_thread.start()

    def say(self, text, **kwargs):
        self.sender.send(text, **kwargs)

    def button(self, label, payload, seconds=3600):
        return {"text": label, "callback_data": self.store.action(payload, seconds)}

    def log(self, text):
        safe = self.redact(text)
        self.logs.append(safe[:800])
        logging.info("%s", safe[:1800])
        now = time.monotonic()
        if not self.live_id and self.active_job:
            self.live_id = self.store.get("live:" + self.active_job)
        if self.live_id and now - self.last_live >= self.config.live_interval:
            body = "\n".join(list(self.logs)[-15:])[-2600:]
            self.sender.edit(self.live_id, f"Codex 작업 중 · {self.active_job}\n{body}", f"live:{self.active_job}")
            self.last_live = now

    def models(self):
        self.rpc.start()
        rows, cursor = [], None
        for _ in range(10):
            params = {"limit": 50, "includeHidden": False}
            if cursor:
                params["cursor"] = cursor
            result = self.rpc.request("model/list", params)
            rows.extend(result.get("data", []))
            cursor = result.get("nextCursor")
            if not cursor:
                break
        self.models_cache = rows
        return rows

    def selected_model(self):
        return self.store.get("model", self.config.model)

    def project_config(self):
        if self.project_config_generation != self.rpc.generation:
            result = self.rpc.request("config/read", {"cwd": str(self.config.workdir), "includeLayers": False})
            self.project_config_cache = result.get("config", {})
            self.project_config_generation = self.rpc.generation
        return self.project_config_cache or {}

    def validate_add_dirs(self):
        config = self.project_config()
        profile = (config.get("permissions") or {}).get(config.get("default_permissions"), {})
        roots = [str(self.config.workdir)]
        roots += [path for path, enabled in profile.get("workspace_roots", {}).items() if enabled]
        roots += (config.get("sandbox_workspace_write") or {}).get("writable_roots", [])
        for requested in self.config.add_dirs:
            target = os.path.normcase(os.path.abspath(requested))
            def contains(root):
                try:
                    parent = os.path.normcase(os.path.abspath(root))
                    return os.path.commonpath((target, parent)) == parent
                except ValueError:
                    return False
            if not any(contains(root) for root in roots):
                raise ValueError("추가 폴더가 기존 프로젝트 권한에 등록되지 않았습니다: " + requested)

    def _thread_options(self, *, temporary=False):
        result = {"cwd": str(self.config.workdir), "approvalPolicy": self.config.approval_policy,
                  "approvalsReviewer": self.config.reviewer}
        model = self.selected_model()
        if model:
            result["model"] = model
        effort = self.store.get("effort", self.config.effort)
        if effort:
            result["config"] = {"model_reasoning_effort": effort}
        if temporary or self.config.read_only:
            config = self.project_config()
            parent = config.get("default_permissions") or {
                "read-only": ":read-only", "danger-full-access": ":danger-full-access"
            }.get(config.get("sandbox_mode"), ":workspace")
            # Inherit denies and project-specific restrictions; only reduce writes/network.
            result.setdefault("config", {})["permissions"] = {"bridge_planning_readonly": {
                "extends": parent, "filesystem": {":workspace_roots": {".": "read"}},
                "network": {"enabled": False}}}
            result["permissions"] = "bridge_planning_readonly"
            if temporary:
                result["ephemeral"] = True
        return result

    def thread(self, temporary=False):
        self.rpc.start()
        options = self._thread_options(temporary=temporary)
        saved = None if temporary else self.store.get("codex_thread")
        if saved and self.loaded_thread == saved and self.loaded_generation == self.rpc.generation:
            self.active_permission_profile = self.loaded_permission_profile
            return saved
        if saved:
            result = self.rpc.request("thread/resume", {"threadId": saved, **options})
        else:
            result = self.rpc.request("thread/start", {**options, "developerInstructions": BRIDGE_INSTRUCTIONS})
        thread_id = result["thread"]["id"]
        self.active_permission_profile = (result.get("activePermissionProfile") or {}).get("id")
        if not temporary:
            self.store.set("codex_thread", thread_id)
            self.loaded_thread, self.loaded_generation = thread_id, self.rpc.generation
            self.loaded_permission_profile = self.active_permission_profile
        return thread_id

    def enqueue(self, text, source, kind="chat"):
        job_id = self.store.enqueue_job(json.dumps({"kind": kind, "text": text}, ensure_ascii=False), source)
        self.say(f"작업 접수: {job_id}" + (" · 현재 작업 다음에 실행합니다." if self.active_job else ""),
                 dedupe=f"accepted:{job_id}")
        return job_id

    def authorized(self, user, chat, topic):
        expected = self.store.get("telegram_topic")
        return (user == self.config.user_id and chat == self.config.chat_id
                and (expected is None or topic == expected))

    def handle_update(self, update):
        callback = update.get("callback_query")
        if callback:
            message = callback.get("message") or {}
            if not self.authorized((callback.get("from") or {}).get("id"),
                                   (message.get("chat") or {}).get("id"), message.get("message_thread_id")):
                return
            self.store.queue("answerCallbackQuery", {"callback_query_id": callback["id"]}, 0,
                             "callback:" + str(callback["id"]))
            action = self.store.consume(callback.get("data", ""))
            if not action:
                self.say("이 버튼은 이미 처리됐거나 만료됐습니다. 현재 상태를 다시 확인하세요.", priority=0)
                return
            self.handle_action(action, "update:" + str(update["update_id"]))
            return
        message = update.get("message") or {}
        if not self.authorized((message.get("from") or {}).get("id"),
                               (message.get("chat") or {}).get("id"), message.get("message_thread_id")):
            return
        text = (message.get("text") or "").strip()
        if not text:
            return
        pieces = text.split(maxsplit=1)
        command, argument = pieces[0], pieces[1] if len(pieces) == 2 else ""
        command = command.split("@")[0].lower()
        source = "update:" + str(update["update_id"])
        if not command.startswith("/") and self.store.get("model_input_until", 0) > time.time():
            self.model_command(text)
            return
        if command.startswith("/") and command not in ("/model", "/effort", "/help", "/status", "/log"):
            self.store.set("model_input_until", 0)
        if self.store.get("prompt_buffer_active"):
            if command == "/prompt_cancel":
                self.store.set("prompt_buffer_active", False)
                self.store.set("prompt_buffer", [])
                self.say("긴 프롬프트를 버렸습니다.")
                return
            if command == "/prompt_end":
                text = "\n".join(self.store.get("prompt_buffer", []))
                self.store.set("prompt_buffer_active", False)
                self.store.set("prompt_buffer", [])
                if text.strip():
                    self.enqueue(text, source)
                return
            if not command.startswith("/"):
                parts = self.store.get("prompt_buffer", [])
                if sum(map(len, parts)) + len(text) > 150000:
                    self.say("프롬프트가 너무 깁니다. 현재 버퍼는 보존했습니다.")
                    return
                parts.append(text)
                self.store.set("prompt_buffer", parts)
                self.say(f"프롬프트 {len(parts)}개 부분 저장. /prompt_end로 실행합니다.")
                return
        if command == "/prompt_begin":
            self.store.set("prompt_buffer_active", True)
            self.store.set("prompt_buffer", [])
            self.say("긴 프롬프트 수집을 시작했습니다. /prompt_end · /prompt_cancel")
        elif command in ("/help", "/start"):
            self.say("Codex Telegram Bridge v2\n"
                     "/model [모델] [강도] · /model high · /effort [강도]\n/status · /log · /new · /threads\n"
                     "/plan <목표> 또는 /plan show\n/auto on · /pause · /resume · /cancel\n"
                     "/suggest · /answer <답변> · /remember <질문> | <답변>\n"
                     "/prompt_begin · /prompt_end · /prompt_cancel\n"
                     "/autoresearch <지시>\n실행 중 새 메시지는 반영/예약을 선택할 수 있습니다.")
        elif command == "/status":
            self.status()
        elif command == "/log":
            self.say("\n".join(self.logs) [-10000:] or "저장된 실행 로그가 없습니다.")
        elif command == "/model":
            self.model_command(argument)
        elif command == "/effort":
            self.effort_command(argument)
        elif command == "/cancel":
            self.cancel_current()
        elif command == "/pause":
            with self.control:
                self.store.set("auto_mode", "paused")
                self.store.cancel_queued(auto_only=True)
            self.say("자동 진행을 일시정지했습니다. 현재 작업은 마친 뒤 대기합니다.", priority=0)
        elif command in ("/resume", "/auto"):
            if command == "/resume" and argument:
                self.resume_thread(argument)
            elif argument in ("off", "stop"):
                self.store.set("auto_mode", "paused")
                self.store.cancel_queued(auto_only=True)
                self.say("자동 진행을 껐습니다.")
            elif argument in ("", "on", "start"):
                self.auto_start()
            else:
                self.say("사용법: /auto on · /auto off · /resume [대화ID]")
        elif command == "/new":
            if self.active_job:
                self.say("작업 종료 후 /new를 사용하세요.")
            else:
                self.store.set("codex_thread", None)
                self.store.set("auto_mode", "paused")
                self.say("다음 요청은 새 대화에서 시작합니다. 이전 대화 기록은 보존했습니다.")
        elif command == "/threads":
            self.rpc.start()
            result = self.rpc.request("thread/list", {"cwd": str(self.config.workdir), "limit": 10})
            rows = result.get("data", [])
            self.say("이 프로젝트의 최근 대화\n" + "\n".join(
                f"{r['id']} · {(r.get('name') or r.get('preview') or '')[:100]}" for r in rows)
                + "\n/resume 대화ID로 선택합니다.")
        elif command == "/plan":
            if not argument or argument == "show":
                self.show_plan()
            elif self.active_job:
                self.say("현재 작업 종료 후 계획을 작성하세요.")
            else:
                self.enqueue(argument, source, "plan_draft")
        elif command == "/suggest":
            self.enqueue("현재 대화의 마지막 결과에 맞는 다음 작업 하나를 제안하세요. "
                         "실행하지 말고 제안만 하세요.\n마지막 결과:\n" + self.store.get("last_final", ""),
                         source, "suggest")
        elif command == "/answer":
            self.free_answer(argument)
        elif command == "/remember":
            key, separator, value = argument.partition("|")
            if not separator or not key.strip() or not value.strip():
                self.say("사용법: /remember 질문 | 답변")
            else:
                facts = self.store.get("known_answers", {})
                facts[key.strip()] = value.strip()
                self.store.set("known_answers", facts)
                self.say("이 질문의 답변을 저장했습니다.")
        elif command == "/autoresearch":
            self.enqueue("$codex-autoresearch\n" + argument, source)
        elif command.startswith("/"):
            self.say("알 수 없는 명령입니다. /help로 확인하세요.")
        elif self.active_job:
            self.say("추가 지시를 어떻게 처리할까요?", buttons=[[
                self.button("현재 작업에 반영", {"type": "steer", "text": text,
                                             "thread": self.active_thread, "turn": self.active_turn}),
                self.button("다음 작업에 예약", {"type": "queue", "text": text})]])
        else:
            self.enqueue(text, source)

    def current_catalog_model(self, rows):
        selected = self.selected_model()
        if selected:
            return next((row for row in rows if selected in (row.get("id"), row.get("model"))), None)
        return next((row for row in rows if row.get("isDefault")), rows[0] if rows else None)

    @staticmethod
    def supported_efforts(row):
        return [item["reasoningEffort"] for item in (row or {}).get("supportedReasoningEfforts", [])]

    def model_help(self, rows, error=None):
        current = self.current_catalog_model(rows)
        sample = current or next((row for row in rows if row.get("isDefault")), rows[0] if rows else {})
        sample_id = sample.get("model") or sample.get("id")
        supported = self.supported_efforts(current)
        effort = self.store.get("effort", self.config.effort) or sample.get("defaultReasoningEffort") or "기본값"
        lines = ([error, ""] if error else []) + [
            "모델·추론 강도 도움말",
            f"현재: {self.selected_model() or sample_id or '계정 기본값'} / {effort}",
            "high, max, ultra 등은 추론 강도입니다. 모델마다 지원 값이 다릅니다.",
            "\n사용법",
            "/model <모델ID>", "/model <모델ID> <추론강도>",
            "/model <추론강도> — 현재 모델의 강도 변경", "/effort <추론강도>",
            "\n입력 예시",
        ]
        if sample_id:
            lines.append(f"/model {sample_id}")
            sample_efforts = self.supported_efforts(sample)
            if sample_efforts:
                example = "high" if "high" in sample_efforts else sample_efforts[0]
                lines.append(f"/model {sample_id} {example}")
        lines.extend(f"/model {value}" for value in supported)
        if supported:
            lines.append(f"/effort {'high' if 'high' in supported else supported[0]}")
        lines.append("\n선택 가능한 모델 / 추론 강도")
        for row in rows:
            levels = ", ".join(self.supported_efforts(row)) or "기본값만 지원"
            lines.append(f"• {row.get('model') or row['id']}: {levels}")
        if not rows:
            lines.append("서버에서 제공한 모델 목록이 없습니다.")
        lines.extend(["\n메뉴를 연 뒤 모델ID나 강도만 입력해도 됩니다. 입력 대기는 5분입니다.",
                      "일반 작업으로 돌아가기: /model cancel"])
        buttons = [[self.button(row.get("displayName") or row["id"],
                                {"type": "model", "value": row.get("model") or row["id"]})] for row in rows[:40]]
        for start in range(0, len(supported), 3):
            buttons.append([self.button(value, {"type": "effort", "value": value})
                            for value in supported[start:start + 3]])
        buttons.append([self.button("모델 선택 닫기", {"type": "model_close"})])
        self.store.set("model_input_until", time.time() + 300)
        self.say("\n".join(lines), buttons=buttons)

    def close_model_menu(self):
        self.store.set("model_input_until", 0)
        self.say("모델 선택을 닫았습니다. 일반 메시지로 작업을 요청할 수 있습니다.")

    def set_effort(self, value):
        with self.store.transaction():
            self.store.set("effort", value)
            self.store.set("model_input_until", 0)
        self.say(f"다음 요청부터 추론 강도: {value} · 대화와 모델은 유지됩니다.")

    def model_command(self, argument):
        parts = (argument or "").split()
        if len(parts) == 1 and parts[0].casefold() in ("cancel", "exit", "취소", "닫기"):
            self.close_model_menu()
            return
        rows = self.models()
        if not parts or (len(parts) == 1 and parts[0].casefold() in ("help", "?", "--help", "도움말", "예시")):
            self.model_help(rows)
            return
        if len(parts) > 2:
            self.model_help(rows, "모델ID와 추론 강도까지만 입력하세요.")
            return
        chosen = next((row for row in rows if parts[0].casefold() in
                       (str(row.get("id", "")).casefold(), str(row.get("model", "")).casefold())), None)
        if chosen is None:
            current = self.current_catalog_model(rows)
            if len(parts) == 1 and parts[0].casefold() in self.supported_efforts(current):
                self.set_effort(parts[0].casefold())
                return
            self.model_help(rows, "입력과 일치하는 모델 또는 현재 모델의 추론 강도가 없습니다.")
            return
        explicit_effort = parts[1].casefold() if len(parts) == 2 else None
        supported = self.supported_efforts(chosen)
        if explicit_effort is not None and explicit_effort not in supported:
            self.model_help(rows, "선택한 모델에서 지원하지 않는 추론 강도입니다. 설정은 변경하지 않았습니다.")
            return
        old_effort = self.store.get("effort", self.config.effort)
        effort = explicit_effort or (old_effort if old_effort in supported else chosen.get("defaultReasoningEffort"))
        with self.store.transaction():
            self.store.set("model", chosen.get("model") or chosen["id"])
            self.store.set("effort", effort)
            self.store.set("model_input_until", 0)
        self.say(f"다음 요청부터 모델: {self.selected_model()} · 추론 강도: {effort or '기본값'} · 대화는 유지됩니다.")

    def effort_command(self, argument):
        value = (argument or "").strip().casefold()
        if value in ("cancel", "exit", "취소", "닫기"):
            self.close_model_menu()
            return
        rows = self.models()
        if not value or value in ("help", "?", "--help", "도움말", "예시"):
            self.model_help(rows)
        elif value in self.supported_efforts(self.current_catalog_model(rows)):
            self.set_effort(value)
        else:
            self.model_help(rows, "현재 모델에서 지원하지 않는 추론 강도입니다.")

    def handle_action(self, action, source):
        kind = action.get("type")
        if kind == "model":
            self.model_command(action["value"])
        elif kind == "effort":
            self.effort_command(action["value"])
        elif kind == "model_close":
            self.close_model_menu()
        elif kind in ("queue", "suggest_execute"):
            self.enqueue(action["text"], source)
        elif kind == "steer":
            if not self.active_turn or (self.active_thread, self.active_turn) != (action.get("thread"), action.get("turn")):
                self.say("이 작업은 이미 종료됐거나 변경됐습니다. 새 메시지로 요청하세요.")
                return
            self.rpc.request("turn/steer", {"threadId": self.active_thread, "expectedTurnId": self.active_turn,
                                            "input": [{"type": "text", "text": action["text"]}]})
            self.store.audit("user_steer", {"job": self.active_job})
            self.say("현재 작업에 추가 지시를 반영했습니다.")
        elif kind == "auto":
            self.auto_start(action.get("hash"))
        elif kind == "approval":
            self.answer_request(action)
        elif kind == "input":
            self.answer_input(action)
        elif kind == "elicitation":
            pending = self.pending_requests.get(action["request"])
            if pending and pending["event"]["method"] == "mcpServer/elicitation/request":
                event = pending["event"]
                self.rpc.respond(event["id"], {"action": action["decision"], "content": None}, event["_generation"])
                self.pending_requests.pop(action["request"], None)
        elif kind == "suggest":
            self.enqueue("현재 결과에 맞는 다음 작업 하나를 제안하세요. 실행하지 마세요.\n" +
                         self.store.get("last_final", ""), source, "suggest")

    def resume_thread(self, thread_id):
        if self.active_job:
            self.say("현재 작업을 마친 뒤 대화를 바꿀 수 있습니다.")
            return
        self.rpc.start()
        result = self.rpc.request("thread/read", {"threadId": thread_id, "includeTurns": False})
        cwd = result["thread"].get("cwd", "")
        if os.path.normcase(os.path.abspath(cwd)) != os.path.normcase(str(self.config.workdir)):
            self.say("다른 프로젝트의 대화는 이 봇에서 재개할 수 없습니다.")
            return
        self.store.set("codex_thread", thread_id)
        self.store.set("auto_mode", "paused")
        self.say("대화를 선택했습니다. 다음 메시지부터 이어집니다.")

    def status(self):
        plan = self.store.get("plan")
        progress = f"{self.store.get('plan_index', 0) + 1}/{len(plan['steps'])}" if plan else "없음"
        with self.store.lock:
            reviews = [r[0] for r in self.store.db.execute("SELECT id FROM jobs WHERE status='review_required' ORDER BY created DESC LIMIT 5")]
        self.say(f"Codex Bridge v2\n프로젝트: {self.config.workdir}\n"
                 f"현재 작업: {self.active_job or '대기'}\n현재 모델: {self.active_model or '대기'}\n"
                 f"다음 모델: {self.selected_model() or '기본값'}\n"
                 f"추론 강도: {self.store.get('effort', self.config.effort) or '기본값'}\n"
                 f"자동 진행: {self.store.get('auto_mode', 'paused')} · 단계 {progress}\n"
                 f"대화: {self.store.get('codex_thread') or '시작 전'}\n"
                 f"질문/승인 대기: {len(self.pending_requests)}\n"
                 f"재시작 후 확인 대기 작업: {', '.join(reviews) or '없음'}", priority=0)

    def show_plan(self):
        plan = self.store.get("plan")
        if not plan:
            self.say("/plan 목표를 입력하면 검증 조건을 포함한 계획 초안을 작성합니다.")
            return
        lines = [f"목표: {plan['goal']}", f"허용 범위: {plan['scope']}"]
        for i, step in enumerate(plan["steps"], 1):
            lines.extend([f"\n{i}. {step['title']}", step["instructions"], "검증:"])
            lines.extend("  " + shlex.join(command) for command in step["checks"])
        lines.append(f"\n자동 진행 한도: {self.config.max_auto_turns}턴 / {self.config.max_run_seconds / 3600:g}시간 / 보고 사용량 {self.config.max_auto_tokens}토큰")
        self.say("\n".join(lines), buttons=[[self.button("이 계획으로 자동 진행", {"type": "auto", "hash": plan_hash(plan)})]])

    def auto_start(self, expected_hash=None):
        with self.control:
            plan = self.store.get("plan")
            if not plan:
                self.say("먼저 /plan 목표로 계획을 작성하세요.")
                return
            actual_hash = plan_hash(plan)
            if expected_hash and expected_hash != actual_hash:
                self.say("계획이 변경됐습니다. /plan show에서 다시 확인하세요.")
                return
            if self.active_job:
                self.say("현재 작업을 마친 뒤 자동 진행을 시작하세요.")
                return
            if self.store.get("plan_index", 0) >= len(plan["steps"]):
                self.say("이 계획은 완료됐습니다. 새 목표로 /plan을 작성하세요.")
                return
            self.store.set("approved_plan_hash", actual_hash)
            self.store.set("auto_mode", "running")
            self.store.set("auto_started", time.time())
            self.store.set("auto_token_baseline", self.store.get("token_usage", {}).get("total", {}).get("totalTokens", 0))
            self.store.set("auto_turns", 0)
            self.store.set("no_progress", 0)
            self.store.set("auto_nonce", str(time.time_ns()))
            self._queue_step()
        self.say("검토한 계획과 기존 권한 범위에서 자동 진행을 시작합니다.", priority=0)

    def _queue_step(self, extra=""):
        plan = self.store.get("plan")
        index = self.store.get("plan_index", 0)
        if self.store.get("auto_mode") != "running" or self.store.get("approved_plan_hash") != plan_hash(plan):
            return
        if index >= len(plan["steps"]):
            self.store.set("auto_mode", "complete")
            self.say("계획의 모든 단계와 검증을 완료했습니다.", priority=10)
            return
        if (time.time() - self.store.get("auto_started", time.time()) >= self.config.max_run_seconds
                or self.auto_tokens() >= self.config.max_auto_tokens):
            self.store.set("auto_mode", "budget_reached")
            self.say("자동 진행의 시간 또는 보고된 토큰 한도에 도달했습니다.", priority=0)
            return
        if self.store.get("auto_turns", 0) >= self.config.max_auto_turns:
            self.store.set("auto_mode", "budget_reached")
            self.say("자동 진행 턴 한도에 도달했습니다. 계획과 진행 상태는 보존했습니다.", priority=0)
            return
        source = f"auto:{self.store.get('auto_nonce')}:{index}:{self.store.get('auto_turns', 0)}"
        self.store.enqueue_job(json.dumps({"kind": "plan_step", "text": step_prompt(plan, index) + extra}, ensure_ascii=False), source)

    def cancel_current(self):
        with self.control:
            self.store.set("auto_mode", "paused")
            self.store.cancel_queued()
            self.cancel.set()
        self.say("중단 요청을 접수했습니다. 자동 진행도 정지했습니다.", priority=0)

    def _events(self):
        while not self.stop.is_set():
            try:
                event = self.rpc.events.get(timeout=0.2)
            except queue.Empty:
                continue
            try:
                self.on_event(event)
            except Exception as exc:
                self.log("이벤트 처리 오류: " + self.redact(exc))
                if "id" in event:
                    try:
                        self.rpc.reject(event["id"], "Bridge could not handle this request", event["_generation"])
                    except RPCError:
                        pass

    def on_event(self, event):
        method, params = event.get("method"), event.get("params") or {}
        if event.get("_generation", self.rpc.generation) != self.rpc.generation:
            return
        if "id" in event:
            self.server_request(event)
            return
        thread_id, turn_id = params.get("threadId"), params.get("turnId")
        if method == "bridge/disconnected":
            self.store.set("auto_mode", "paused")
            with self.condition:
                self.condition.notify_all()
            return
        if method == "serverRequest/resolved":
            self.pending_requests.pop(str(params.get("requestId")), None)
            return
        if method == "turn/tokenUsage/updated" and thread_id == self.store.get("codex_thread"):
            self.store.set("token_usage", params.get("tokenUsage", {}))
        if thread_id != self.active_thread:
            return
        if method == "turn/started":
            self.active_turn = params.get("turn", {}).get("id")
        elif method == "item/completed":
            item = params.get("item", {})
            kind = item.get("type")
            if kind == "agentMessage":
                self.messages.setdefault(turn_id, []).append(item.get("text", ""))
            elif kind == "commandExecution":
                self.log(f"명령 종료: {item.get('exitCode')}\n" + (item.get("aggregatedOutput") or "")[:1600])
            elif kind == "fileChange":
                for change in item.get("changes", []):
                    self.log("파일: " + str(change.get("path", "")))
        elif method == "item/started":
            item = params.get("item", {})
            if item.get("type") == "commandExecution":
                self.log("명령: " + str(item.get("command", "")))
            elif item.get("type") == "mcpToolCall":
                self.log("도구: " + str(item.get("tool", "")))
        elif method == "turn/completed":
            turn = params.get("turn", {})
            with self.condition:
                self.completed[turn["id"]] = turn
                self.condition.notify_all()
        elif method == "error":
            self.log("Codex 오류: " + self.redact(params.get("error", {})))

    def server_request(self, event):
        params, method = event.get("params") or {}, event["method"]
        if (params.get("threadId") != self.active_thread
                or (self.active_turn and params.get("turnId") != self.active_turn)):
            self.rpc.reject(event["id"], "Request is not for the active bridge turn", event["_generation"])
            return
        request_key = str(event["id"])
        self.pending_requests[request_key] = {"event": event, "answers": {}}
        if method == "item/tool/requestUserInput":
            facts = self.store.get("known_answers", {})
            for question in params.get("questions", []):
                text = question.get("question", "")
                if text in facts and not sensitive_question(text):
                    self.pending_requests[request_key]["answers"][question["id"]] = {"answers": [facts[text]]}
                    self.store.audit("known_answer_reused", {"request": request_key, "question": question["id"]})
                elif (self.in_approved_step() and routine_question(text)
                      and any(word in text.lower() for word in ("plan", "next step", "계획", "단계"))):
                    permitted = {"continue", "proceed", "yes", "계속", "진행", "예", "계속 진행"}
                    choice = next((o["label"] for o in question.get("options", [])
                                   if o["label"].lower().strip() in permitted), None)
                    if choice:
                        self.pending_requests[request_key]["answers"][question["id"]] = {"answers": [choice]}
                        self.store.audit("planned_continuation_answer", {"request": request_key, "question": question["id"]})
            self._show_questions(request_key)
            return
        if method in ("item/commandExecution/requestApproval", "item/fileChange/requestApproval"):
            buttons = [[self.button("이번 작업 허용", {"type": "approval", "request": request_key, "decision": "accept"}, 1800),
                        self.button("거부", {"type": "approval", "request": request_key, "decision": "decline"}, 1800)]]
            self.say("실제 실행 승인 요청\n" + self.redact(params.get("command") or params.get("reason") or method)
                     + "\n작업 경로: " + str(params.get("cwd", self.config.workdir)), buttons=buttons, priority=0)
        elif method == "item/permissions/requestApproval":
            self.say("새 권한 요청\n" + self.redact(json.dumps(params.get("permissions", {}), ensure_ascii=False)), buttons=[[
                self.button("요청한 권한을 이번 턴에 허용", {"type": "approval", "request": request_key, "decision": "grant"}, 1800),
                self.button("거부", {"type": "approval", "request": request_key, "decision": "deny"}, 1800)]], priority=0)
        elif method == "mcpServer/elicitation/request":
            url = params.get("url")
            if url:
                self.say("연결 도구가 브라우저 확인을 요청했습니다.\n" + self.redact(params.get("message", ""))
                         + "\n" + self.redact(url), buttons=[[
                    self.button("브라우저에서 완료", {"type": "elicitation", "request": request_key, "decision": "accept"}, 1800),
                    self.button("거부", {"type": "elicitation", "request": request_key, "decision": "decline"}, 1800)]], priority=0)
            else:
                self.say("연결 도구가 입력을 요청했습니다.\n" + self.redact(params.get("message", ""))
                         + "\n입력 형식:\n" + self.redact(json.dumps(params.get("requestedSchema", {}), ensure_ascii=False))
                         + "\n/answer {JSON 답변} 또는 /cancel을 사용하세요.", priority=0)
        else:
            self.pending_requests.pop(request_key, None)
            self.rpc.reject(event["id"], "Unsupported interactive request; no automatic approval", event["_generation"])

    def answer_request(self, action):
        pending = self.pending_requests.get(action["request"])
        if not pending:
            self.say("이 승인 요청은 종료됐거나 만료됐습니다.", priority=0)
            return
        event, decision = pending["event"], action["decision"]
        params = event["params"]
        if params.get("turnId") != self.active_turn or params.get("threadId") != self.active_thread:
            self.say("현재 작업과 다른 승인 요청입니다.", priority=0)
            return
        if event["method"] == "item/permissions/requestApproval":
            result = {"permissions": params["permissions"] if decision == "grant" else {}, "scope": "turn"}
        else:
            if decision not in ("accept", "decline"):
                return
            result = {"decision": decision}
        self.rpc.respond(event["id"], result, event["_generation"])
        self.pending_requests.pop(action["request"], None)
        self.store.audit("user_approval_decision", {"request": action["request"], "decision": decision})
        self.say("승인 응답을 전달했습니다.", priority=0)

    def _show_questions(self, request_key):
        pending = self.pending_requests[request_key]
        event, answers = pending["event"], pending["answers"]
        questions = event["params"].get("questions", [])
        remaining = [q for q in questions if q["id"] not in answers]
        if not remaining:
            self.rpc.respond(event["id"], {"answers": answers}, event["_generation"])
            self.pending_requests.pop(request_key, None)
            return
        question = remaining[0]
        buttons = [[self.button(option["label"], {"type": "input", "request": request_key,
                                                 "question": question["id"], "value": option["label"]}, 1800)]
                   for option in (question.get("options") or [])]
        self.say(question["question"] + "\n자유 답변: /answer <답변>", buttons=buttons or None, priority=0)

    def answer_input(self, action):
        pending = self.pending_requests.get(action["request"])
        if not pending:
            self.say("이 질문은 종료됐거나 만료됐습니다.")
            return
        event = pending["event"]
        if event.get("_generation") != self.rpc.generation or event["params"].get("turnId") != self.active_turn:
            self.say("현재 작업의 질문이 아닙니다.")
            return
        if action["question"] in pending["answers"]:
            self.say("이미 답변한 질문입니다.")
            return
        if action["question"] not in {q["id"] for q in event["params"].get("questions", [])}:
            return
        pending["answers"][action["question"]] = {"answers": [action["value"]]}
        self._show_questions(action["request"])

    def free_answer(self, text):
        inputs = [(key, value) for key, value in self.pending_requests.items()
                  if value["event"]["method"] == "item/tool/requestUserInput"]
        elicitations = [(key, value) for key, value in self.pending_requests.items()
                        if value["event"]["method"] == "mcpServer/elicitation/request" and not value["event"]["params"].get("url")]
        if not inputs and len(elicitations) == 1 and text:
            key, pending = elicitations[0]
            content = parse_object(text)
            required = pending["event"]["params"].get("requestedSchema", {}).get("required", [])
            if any(name not in content for name in required):
                self.say("필수 입력이 빠졌습니다. 요청된 형식을 확인하세요.")
                return
            event = pending["event"]
            self.rpc.respond(event["id"], {"action": "accept", "content": content}, event["_generation"])
            self.pending_requests.pop(key, None)
            return
        if len(inputs) != 1 or not text:
            self.say("현재 답변할 질문 하나가 있을 때 /answer 답변을 사용하세요.")
            return
        key, pending = inputs[0]
        question = next(q for q in pending["event"]["params"]["questions"] if q["id"] not in pending["answers"])
        self.answer_input({"request": key, "question": question["id"], "value": text})

    def in_approved_step(self):
        plan = self.store.get("plan")
        job = self.store.job(self.active_job) if self.active_job else None
        return bool(plan and job and not self.cancel.is_set()
                    and self.store.get("auto_mode") == "running"
                    and self.store.get("approved_plan_hash") == plan_hash(plan)
                    and json.loads(job["prompt"]).get("kind") == "plan_step")

    def _work(self):
        while not self.stop.is_set():
            with self.control:
                job = self.store.next_job()
                if job:
                    self.active_job = job["id"]
                    self.cancel.clear()
            if not job:
                self.stop.wait(0.15)
                continue
            try:
                self.run_job(job)
            except RunCancelled as exc:
                self.store.update_job(job["id"], status="cancelled", error=self.redact(exc), finished=time.time())
                self.store.set("auto_mode", "paused")
                self.say(f"작업 {job['id']} 중단: {self.redact(exc)}", priority=0)
            except Exception as exc:
                self.store.update_job(job["id"], status="failed", error=self.redact(exc), finished=time.time())
                self.store.set("auto_mode", "paused")
                self.say(f"작업 {job['id']} 실패: {self.redact(exc)}\n자동 재실행하지 않았습니다. /status로 확인하세요.", priority=0)
                logging.error("Job failed: %s", self.redact(exc))
            finally:
                if self.live_id:
                    record = self.store.job(job["id"])
                    self.sender.edit(self.live_id, f"작업 {job['id']} · {record['status']}", f"live:{job['id']}")
                with self.control:
                    self.active_job = self.active_thread = self.active_turn = self.active_model = self.live_id = self.active_deadline = None
                self.pending_requests.clear()

    def run_job(self, job):
        envelope = json.loads(job["prompt"])
        kind, prompt = envelope["kind"], envelope["text"]
        if kind == "plan_step" and self.store.get("auto_mode") != "running":
            self.store.update_job(job["id"], status="cancelled", finished=time.time())
            return
        if self.cancel.is_set():
            self.store.update_job(job["id"], status="cancelled", finished=time.time())
            return
        temporary = kind in ("plan_draft", "suggest")
        self.active_thread = self.thread(temporary=temporary)
        self.store.update_job(job["id"], thread_id=self.active_thread)
        self.active_model = self.selected_model() or "계정 기본값"
        schema = PLAN_SCHEMA if kind == "plan_draft" else SUGGEST_SCHEMA if kind == "suggest" else RESULT_SCHEMA if kind == "plan_step" else None
        if kind == "plan_draft":
            prompt = ("기획만 수행하세요. 구현·수정·네트워크 접근을 실행하지 마세요. "
                      "사용자의 목표에 대해 명시적인 허용 범위와 1–12단계를 작성하세요. "
                      "각 단계에는 완료 여부를 확인할 실제 검증 명령(argv 배열)이 있어야 합니다. "
                      "명령은 프로젝트 경로와 기존 권한 안에서 실행 가능해야 합니다. "
                      "설치·외부 업로드·커밋·push는 사용자에게 이미 허용받은 경우만 계획에 포함하세요.\n목표:\n" + prompt)
        saved_plan = self.store.get("plan")
        if saved_plan and kind in ("chat", "suggest", "plan_draft"):
            context = {"plan": saved_plan, "current_step": self.store.get("plan_index", 0),
                       "auto_mode": self.store.get("auto_mode", "paused")}
            prompt += ("\n\n브릿지에 저장된 현재 계획과 상태(참고 문맥입니다. 최신 사용자 요청이 우선입니다. "
                       "이 문맥 자체가 실행·새 권한 승인은 아닙니다):\n"
                       + json.dumps(context, ensure_ascii=False))
        if self.config.suffix:
            prompt += "\n\n" + self.config.suffix
        params = {"threadId": self.active_thread, "input": [{"type": "text", "text": prompt}]}
        if self.selected_model():
            params["model"] = self.selected_model()
        effort = self.store.get("effort", self.config.effort)
        if effort:
            params["effort"] = effort
        if schema:
            params["outputSchema"] = schema
        deadline = time.monotonic() + self.config.max_run_seconds
        if kind == "plan_step":
            remaining = self.config.max_run_seconds - (time.time() - self.store.get("auto_started", time.time()))
            deadline = time.monotonic() + max(0, remaining)
            self.store.set("auto_turns", self.store.get("auto_turns", 0) + 1)
        self.active_deadline = deadline
        if self.cancel.is_set() or time.monotonic() >= deadline:
            self.store.update_job(job["id"], status="cancelled", finished=time.time())
            self.store.set("auto_mode", "paused")
            return
        # Live messages are optional; a Telegram outage must not block execution.
        self.say(f"Codex 실행 중 · {job['id']} · {self.active_model}", dedupe=f"running:{job['id']}")
        result = self.rpc.request("turn/start", params)
        turn_id = result["turn"]["id"]
        self.active_turn = turn_id
        self.store.update_job(job["id"], turn_id=turn_id)
        turn = self.wait_turn(turn_id, deadline)
        status = turn.get("status")
        if status != "completed":
            self.store.update_job(job["id"], status="cancelled" if status == "interrupted" else "failed",
                                  error=self.redact(turn.get("error") or status), finished=time.time())
            self.store.set("auto_mode", "paused")
            self.say(f"작업 {job['id']} · {status}\n자동 진행은 정지했습니다.", priority=0)
            return
        texts = self.messages.pop(turn_id, [])
        # Some backends include final items on completion instead of emitting item/completed.
        if not texts:
            texts = [item.get("text", "") for item in turn.get("items", []) if item.get("type") == "agentMessage"]
        final = next((text for text in reversed(texts) if text.strip()), "")
        if not final:
            raise RPCError("완료 이벤트는 받았지만 최종 답변이 없습니다.")
        self.store.update_job(job["id"], status="completed", result=final, finished=time.time())
        if self.cancel.is_set():
            self.store.set("auto_mode", "paused")
            self.say(f"작업 {job['id']} 종료를 확인했습니다. 자동 진행은 정지 상태입니다.", priority=0)
            return
        if kind == "plan_draft":
            plan = validate_plan(parse_object(final))
            self.store.set("plan", plan)
            self.store.set("plan_index", 0)
            self.store.set("approved_plan_hash", None)
            self.store.set("auto_mode", "paused")
            self.show_plan()
        elif kind == "suggest":
            suggestion = parse_object(final)
            self.say("추천 후속 지시\n" + suggestion["prompt"] + "\n\n" + suggestion["reason"], buttons=[[
                self.button("실행", {"type": "suggest_execute", "text": suggestion["prompt"]})]])
        elif kind == "plan_step":
            self.finish_step(parse_object(final), job)
        else:
            self.store.set("last_final", final)
            self.say(final, priority=10, dedupe=f"result:{job['id']}", buttons=[[
                self.button("다음 작업 제안", {"type": "suggest"})]])

    def wait_turn(self, turn_id, deadline):
        interrupt_started = None
        while True:
            with self.condition:
                if turn_id in self.completed:
                    return self.completed.pop(turn_id)
                self.condition.wait(0.15)
            if not self.rpc.alive:
                raise RPCError("Codex 연결이 종료됐습니다. 작업을 중복 실행하지 않고 보존합니다.")
            token_limit = self.store.get("auto_mode") == "running" and self.auto_tokens() >= self.config.max_auto_tokens
            if self.stop.is_set() or self.cancel.is_set() or time.monotonic() >= deadline or token_limit:
                if interrupt_started is None:
                    interrupt_started = time.monotonic()
                    self.store.set("auto_mode", "paused")
                    try:
                        self.rpc.request("turn/interrupt", {"threadId": self.active_thread, "turnId": turn_id}, timeout=5)
                    except RPCError:
                        pass
                elif time.monotonic() - interrupt_started >= 12:
                    self.rpc.close()
                    raise RunCancelled("중단 완료 응답이 없어 이 브릿지의 Codex 프로세스를 종료했습니다.")

    def auto_tokens(self):
        total = self.store.get("token_usage", {}).get("total", {}).get("totalTokens", 0)
        return max(0, total - self.store.get("auto_token_baseline", total))

    def finish_step(self, result, job):
        plan = self.store.get("plan")
        if self.store.get("approved_plan_hash") != plan_hash(plan):
            raise ValueError("승인 후 계획이 변경됐습니다.")
        state, summary = result.get("status"), result.get("summary", "")
        self.store.set("last_final", summary)
        index = self.store.get("plan_index", 0)
        self.say(f"계획 {index + 1}단계 · {summary}", priority=10, dedupe=f"result:{job['id']}")
        if state == "step_done":
            evidence = []
            for i, command in enumerate(plan["steps"][index]["checks"]):
                if self.cancel.is_set() or self.store.get("auto_mode") != "running":
                    return
                execution = self.execute_check(command, f"verify-{job['id']}-{i}")
                evidence.append({"command": command, "exitCode": execution["exitCode"],
                                 "output": self.redact(execution.get("stdout", "") + execution.get("stderr", ""))[:5000]})
            self.store.audit("plan_verification", {"job": job["id"], "step": index, "checks": evidence})
            if evidence and all(entry["exitCode"] == 0 for entry in evidence):
                self.store.set("plan_index", index + 1)
                self.store.set("no_progress", 0)
                with self.control:
                    self._queue_step()
                return
            extra = "\n완료 검증이 실패했습니다. 승인된 범위에서 원인을 진단하고 수정하세요.\n" + json.dumps(evidence, ensure_ascii=False)
        elif state in ("continue", "retry"):
            extra = "\n현재 단계에서 이어서 수행할 작업:\n" + result.get("next_prompt", "")
        elif state == "needs_input" and routine_question(result.get("question", "")):
            extra = "\n현재 단계는 이미 위 계획으로 승인됐습니다. 기존 권한 범위에서 이어서 수행하고 검증하세요."
            self.store.audit("routine_question_resolved", {"job": job["id"], "step": index})
        else:
            self.store.set("auto_mode", "needs_approval" if state == "needs_approval" else "needs_input" if state == "needs_input" else "blocked")
            self.say("자동 진행 대기\n" + (result.get("question") or summary), priority=0)
            return
        fingerprint = hashlib.sha256(json.dumps({"status": state, "evidence": result.get("evidence", []),
                                   "checks": evidence if state == "step_done" else []}, sort_keys=True).encode()).hexdigest()
        count = self.store.get("no_progress", 0) + 1 if self.store.get("progress_fingerprint") == fingerprint else 1
        self.store.set("progress_fingerprint", fingerprint)
        self.store.set("no_progress", count)
        if count >= self.config.max_no_progress:
            self.store.set("auto_mode", "blocked")
            self.say("같은 단계에서 완료 검증 없이 반복돼 자동 진행을 멈췄습니다. 진단 기록은 보존했습니다.", priority=0)
            return
        with self.control:
            self._queue_step(extra)

    def execute_check(self, command, process_id):
        command = list(command)
        resolved = shutil.which(command[0], path=self.config.child_env.get("PATH"))
        if resolved:
            command[0] = resolved
        deadline = min(self.active_deadline or (time.monotonic() + 60), time.monotonic() + 60)
        if self.cancel.is_set() or self.stop.is_set() or time.monotonic() >= deadline:
            raise RunCancelled("검증 시작 전에 중단 또는 시간 한도를 확인했습니다.")
        results = queue.Queue()
        def execute():
            try:
                params = {"command": command,
                    "cwd": str(self.config.workdir), "timeoutMs": max(1, int((deadline - time.monotonic()) * 1000)),
                    "processId": process_id}
                if self.active_permission_profile:
                    params["permissionProfile"] = self.active_permission_profile
                results.put(self.rpc.request("command/exec", params, timeout=70))
            except Exception as exc:
                results.put(exc)
        threading.Thread(target=execute, daemon=True, name="plan-verification").start()
        while True:
            try:
                result = results.get(timeout=0.1)
                if isinstance(result, Exception):
                    raise result
                return result
            except queue.Empty:
                if self.cancel.is_set() or self.stop.is_set() or time.monotonic() >= deadline:
                    try:
                        self.rpc.request("command/exec/terminate", {"processId": process_id}, timeout=5)
                    except RPCError:
                        self.rpc.close()
                    raise RunCancelled("검증 실행을 중단했습니다.")

    def close(self):
        self.stop.set()
        self.cancel.set()
        self.worker_thread.join(timeout=22) if self.worker_thread.is_alive() else None
        self.rpc.close()
        self.event_thread.join(timeout=2) if self.event_thread.is_alive() else None
