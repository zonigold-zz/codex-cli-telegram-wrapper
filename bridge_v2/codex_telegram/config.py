from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import json
import os
from pathlib import Path
import re
import shutil


def load_env(path: Path) -> dict[str, str]:
    values = {}
    if path.exists():
        for raw in path.read_text(encoding="utf-8-sig").splitlines():
            line = raw.strip()
            if not line or line.startswith(("#", ";")) or "=" not in line:
                continue
            key, value = line.removeprefix("export ").split("=", 1)
            values[key.strip()] = value.strip().strip('"').strip("'")
    return values


class Redactor:
    def __init__(self, values: dict[str, str]):
        self.secrets = sorted({v for k, v in values.items()
                               if re.search(r"TOKEN|KEY|SECRET|PASSWORD", k, re.I) and len(v) >= 6},
                              key=len, reverse=True)

    def __call__(self, value) -> str:
        text = str(value)
        for secret in self.secrets:
            text = text.replace(secret, "<REDACTED>")
        return re.sub(r"\b\d{8,12}:[A-Za-z0-9_-]{20,}\b", "<TELEGRAM_TOKEN>", text)


def codex_executable(value: str = "codex") -> str:
    candidate = Path(value)
    if candidate.is_file() and candidate.suffix.lower() == ".exe":
        return str(candidate)
    shim = shutil.which(value + ".cmd") or shutil.which(value)
    if not shim:
        raise ValueError("Codex 실행 파일을 찾을 수 없습니다.")
    if os.name == "nt":
        base = Path(shim).parent / "node_modules" / "@openai" / "codex"
        native = list(base.glob("**/bin/codex.exe"))
        if native:
            return str(native[0])
        raise ValueError("Codex의 기본 .exe를 찾을 수 없습니다. --tg-codex-bin에 지정하세요.")
    return shim


def project_environment(values):
    return {key: str(value) for key, value in values.items()
            if not key.startswith(("TELEGRAM_", "CODEX_"))
            and not re.search(r"TOKEN|KEY|SECRET|PASSWORD", key, re.I)}


@dataclass
class Config:
    workdir: Path
    state_dir: Path
    token: str
    user_id: int
    chat_id: int
    model: str | None = None
    effort: str | None = None
    topic_label: str = "Codex Bridge v2"
    thread_id: int | None = None
    no_topic: bool = False
    suffix: str = ""
    codex_bin: str = "codex"
    add_dirs: tuple[str, ...] = ()
    approval_policy: str = "never"
    reviewer: str = "user"
    max_run_seconds: float = 10800
    max_auto_turns: int = 8
    max_no_progress: int = 3
    max_auto_tokens: int = 200000
    send_interval: float = 1.1
    live_interval: float = 4.0
    read_only: bool = False
    child_env: dict[str, str] = field(default_factory=dict)

    @property
    def bot_key(self) -> str:
        return hashlib.sha256(self.token.encode()).hexdigest()[:24]

    @property
    def codex_home(self) -> Path:
        return self.state_dir / "codex-home"

    def public(self) -> dict:
        return {"workdir": str(self.workdir), "state_dir": str(self.state_dir), "codex_home": str(self.codex_home),
                "model": self.model, "effort": self.effort, "approval_policy": self.approval_policy,
                "add_dirs": self.add_dirs, "forwarded_env_names": sorted(self.child_env)}


def deployment(path: Path) -> dict:
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8-sig"))
    return {}
