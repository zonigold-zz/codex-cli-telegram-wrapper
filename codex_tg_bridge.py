"""Project launcher; runtime location is configured locally, without credentials."""
from pathlib import Path
import json
import sys

root = Path(__file__).resolve().parent
settings = root / ".codex_tg_deployment.json"
code_root = root / "bridge_v2"
if settings.exists():
    code_root = Path(json.loads(settings.read_text(encoding="utf-8-sig"))["code_root"])
if not (code_root / "codex_telegram" / "cli.py").is_file():
    raise SystemExit("Bridge v2 runtime is missing; check .codex_tg_deployment.json")
sys.path.insert(0, str(code_root))
from codex_telegram.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
