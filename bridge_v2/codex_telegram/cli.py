from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys

from .config import Config, Redactor, deployment, load_env, project_environment
from .runtime import serve
from .store import Store


def main():
    entry = Path(sys.argv[0]).resolve().parent
    local = deployment(entry / ".codex_tg_deployment.json")
    parser = argparse.ArgumentParser(description="Persistent Telegram client for Codex app-server")
    parser.add_argument("--tg-env", type=Path, default=entry / ".env")
    parser.add_argument("--tg-workdir", type=Path, default=Path.cwd())
    parser.add_argument("--tg-state-dir", type=Path)
    parser.add_argument("--tg-codex-bin", default="codex")
    parser.add_argument("--tg-no-topic", action="store_true")
    parser.add_argument("--tg-use-topic", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--tg-topic-label", default=None)
    parser.add_argument("--tg-stop", action="store_true")
    parser.add_argument("--tg-health", action="store_true")
    parser.add_argument("--tg-check-config", action="store_true")
    parser.add_argument("--tg-setup", action="store_true", help="Prepare the isolated CLI home and its existing Windows sandbox mode")
    parser.add_argument("--tg-max-hours", type=float)
    parser.add_argument("--tg-max-turns", type=int)
    parser.add_argument("--tg-max-tokens", type=int, default=200000)
    parser.add_argument("-m", "--model")
    parser.add_argument("--add-dir", action="append", default=[])
    parser.add_argument("--sandbox", choices=["read-only", "workspace-write"], help=argparse.SUPPRESS)
    args, unknown = parser.parse_known_args()
    if unknown:
        parser.error("app-server 방식에서 지원하지 않는 인수: " + " ".join(unknown) +
                     ". --full-auto/--search 대신 프로젝트의 권한·검색 설정을 사용하세요.")
    file_values = load_env(args.tg_env)
    values = {**os.environ, **file_values}
    redact = Redactor(values)
    workdir = args.tg_workdir.resolve()
    default_state = Path.home() / "Documents" / "Codex" / "codex-telegram-state" / workdir.name
    state_dir = (args.tg_state_dir or Path(local.get("state_dir") or default_state)).resolve()
    if args.tg_stop:
        state_dir.mkdir(parents=True, exist_ok=True)
        (state_dir / "control.stop").write_text("stop\n", encoding="utf-8")
        print("종료 요청을 기록했습니다.")
        return 0
    if args.tg_health:
        database = state_dir / "bridge.sqlite3"
        if not database.exists():
            print(json.dumps({"status": "not_started"}))
            return 1
        store = Store(database)
        print(json.dumps({k: store.get(k) for k in ("version", "runtime_status", "pid", "heartbeat",
                          "last_poll_ok", "last_send_ok", "poll_error", "codex_thread", "codex_home", "auto_mode", "telegram_topic")}, ensure_ascii=False))
        store.close()
        return 0
    try:
        config = Config(workdir=workdir, state_dir=state_dir,
                        token=values.get("TELEGRAM_BOT_TOKEN", "").strip(),
                        user_id=int(values.get("TELEGRAM_ALLOWED_USER_ID", "0")),
                        chat_id=int(values.get("TELEGRAM_CHAT_ID", "0")),
                        model=args.model or values.get("CODEX_MODEL") or local.get("model"),
                        effort=values.get("CODEX_REASONING_EFFORT") or local.get("effort"),
                        topic_label=args.tg_topic_label or values.get("CODEX_TG_TOPIC_LABEL") or workdir.name,
                        thread_id=int(values["TELEGRAM_THREAD_ID"]) if values.get("TELEGRAM_THREAD_ID") else None,
                        no_topic=args.tg_no_topic, suffix=values.get("CODEX_TG_PROMPT_SUFFIX", ""),
                        codex_bin=args.tg_codex_bin,
                        add_dirs=tuple(args.add_dir or local.get("add_dirs", [])),
                        max_run_seconds=(args.tg_max_hours if args.tg_max_hours is not None else float(values.get("CODEX_TG_AUTONOMOUS_HOURS", 3))) * 3600,
                        max_auto_turns=args.tg_max_turns if args.tg_max_turns is not None else int(values.get("CODEX_TG_AUTONOMOUS_TURNS", 8)),
                        reviewer=values.get("CODEX_TG_APPROVALS_REVIEWER", "user"),
                        max_auto_tokens=args.tg_max_tokens,
                        child_env=project_environment({**local.get("launch_env", {}), **file_values}),
                        read_only=args.sandbox == "read-only")
        if not config.token or config.user_id <= 0 or not config.chat_id or not workdir.is_dir():
            raise ValueError("토큰·허용 사용자·채팅·작업 경로를 확인하세요.")
        if config.max_run_seconds < 1 or config.max_auto_turns < 1 or config.max_auto_tokens < 1 or config.reviewer not in ("user", "auto_review"):
            raise ValueError("실행 한도 또는 승인 검토 설정이 잘못됐습니다.")
        if args.tg_check_config:
            print(json.dumps(config.public(), ensure_ascii=False))
            return 0
        if args.tg_setup:
            from .rpc import AppServer
            from .runtime import InstanceLock
            with InstanceLock(config.bot_key):
                rpc = AppServer(config, redact)
                try:
                    rpc.start(setup_sandbox=True)
                    print("독립 CLI 저장소와 보안 실행 준비를 확인했습니다.")
                finally:
                    rpc.close()
            return 0
        return serve(config, redact)
    except (ValueError, RuntimeError, OSError) as exc:
        print(redact(exc), file=sys.stderr)
        return 2
