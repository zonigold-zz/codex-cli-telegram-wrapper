"""A bridge-owned Codex home, independent of the desktop client's history/tools."""
from __future__ import annotations

from datetime import date, datetime, time
import hashlib
import json
import os
from pathlib import Path
import stat
import subprocess
import tempfile

try:
    import tomllib
except ImportError:  # Fail closed instead of partially interpreting security settings.
    tomllib = None


MARKER = ".telegram-bridge-home.json"
# These sections can invoke desktop helpers, import app tools, or redirect storage.
EXCLUDED = frozenset({"mcp_servers", "plugins", "marketplaces", "desktop", "tui", "notify",
                      "sqlite_home", "log_dir", "history", "apps"})
DESKTOP_FEATURES = frozenset({"apps", "apps_mcp_gateway", "plugins", "remote_control",
                             "remote_control_server", "desktop"})
ROUTING_ENV = frozenset({"CODEX_THREAD_ID", "CODEX_SESSION_ID", "CODEX_TOOL_CALL_ID",
    "CODEX_INTERNAL_ORIGINATOR_OVERRIDE", "CODEX_APP_TOOLS_PIPE_PATH", "CODEX_APP_RUNTIME_CLI_PATH",
    "CODEX_MCP_NODE_PATH", "CODEX_TECTONIC_PATH", "CODEX_SAGE_BACKFILL_TRACKER_TAB_REUSE",
    "CODEX_CLI_PATH", "CODEX_WINDOWS_REGISTERED_CORE", "CODEX_WINDOWS_SANDBOX_PACKAGE_FAMILY",
    "CODEX_TASK_WORKSPACE_VERIFYING_IDENTITY", "BROWSER_USE_AVAILABLE_BACKENDS", "BROWSER_USE_TINYSKY_ENABLED"})
SKILL_DIRECTORIES = frozenset({"agents", "docs", "references", "scripts"})
SKILL_EXTENSIONS = frozenset({".md", ".py", ".txt", ".json", ".toml", ".yaml", ".yml", ".sh",
                              ".ps1", ".cmd", ".bat", ".js", ".mjs", ".ini"})


def _toml_value(value):
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return repr(value)
    if isinstance(value, (date, datetime, time)):
        return value.isoformat()
    if isinstance(value, list):
        return "[" + ", ".join(_toml_value(item) for item in value) + "]"
    if isinstance(value, dict):
        return "{" + ", ".join(json.dumps(key, ensure_ascii=False) + " = " + _toml_value(item)
                               for key, item in value.items()) + "}"
    raise ValueError("Unsupported Codex configuration value; isolation was not prepared")


def _filtered_tree(tree):
    cleaned = {key: value for key, value in tree.items() if key not in EXCLUDED}
    # A selected CLI profile may repeat any top-level setting.
    if "profiles" in cleaned:
        cleaned["profiles"] = {key: _filtered_tree(value) for key, value in cleaned["profiles"].items()}
    if "features" in cleaned:
        cleaned["features"] = {key: value for key, value in cleaned["features"].items()
                               if key not in DESKTOP_FEATURES}
    return cleaned


def filtered_config(content):
    """Preserve parsed security settings, excluding UI/tool/storage routing only."""
    if tomllib is None:
        raise RuntimeError("Isolated bridge configuration requires Python 3.11 or newer")
    try:
        original = tomllib.loads(content)
        cleaned = _filtered_tree(original)
        output = "# Bridge-owned configuration; desktop tools and storage are not shared.\n"
        output += "\n".join(json.dumps(key, ensure_ascii=False) + " = " + _toml_value(value)
                            for key, value in cleaned.items()) + "\n"
        if tomllib.loads(output) != cleaned:
            raise ValueError("Configuration round-trip mismatch")
        return output
    except (ValueError, TypeError, AttributeError) as exc:
        raise RuntimeError("Codex configuration could not be isolated without changing its meaning") from exc


def _reparse(path):
    try:
        attributes = path.lstat()
    except FileNotFoundError:
        return False
    return path.is_symlink() or bool(getattr(attributes, "st_file_attributes", 0) &
                                    getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0))


def _overlap(first, second):
    return first == second or first in second.parents or second in first.parents


def _validate_home(target, source):
    desktop = (Path.home() / ".codex").resolve()
    resolved = target.resolve()
    for shared in (desktop, source.resolve()):
        if _overlap(resolved, shared):
            raise RuntimeError("Bridge Codex home must be separate from the desktop/default Codex home")
    for path in (target, *target.parents):
        if _reparse(path):
            raise RuntimeError("Bridge Codex home cannot use symlinks or directory junctions")
    return resolved


def _atomic_write(path, data):
    if _reparse(path):
        raise RuntimeError("Bridge home files cannot be symlinks or reparse points")
    descriptor, temporary = tempfile.mkstemp(prefix=".bridge-seed-", dir=path.parent)
    temporary = Path(temporary)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _check_local_config(path):
    if not path.exists():
        return
    try:
        current = path.read_text(encoding="utf-8-sig")
        if tomllib is None or tomllib.loads(current) != tomllib.loads(filtered_config(current)):
            raise RuntimeError("Existing bridge configuration contains desktop/storage routing")
    except (ValueError, UnicodeError) as exc:
        raise RuntimeError("Existing bridge configuration is invalid") from exc


def headless_config(content, *, windows):
    """Pin the non-interactive backend for child CLIs without widening permissions."""
    if tomllib is None:
        raise RuntimeError("Isolated bridge configuration requires Python 3.11 or newer")
    tree = tomllib.loads(content)

    def pin(settings):
        settings['approval_policy'] = 'never'
        if windows:
            settings.setdefault('windows', {})['sandbox'] = 'mxc'
        for profile in settings.get('profiles', {}).values():
            pin(profile)

    pin(tree)
    output = '# Bridge-owned configuration; desktop tools and storage are not shared.\n'
    output += '\n'.join(json.dumps(key, ensure_ascii=False) + ' = ' + _toml_value(value)
                        for key, value in tree.items()) + '\n'
    if tomllib.loads(output) != tree:
        raise RuntimeError('Headless configuration round-trip mismatch')
    return output


def _pin_headless_config(home):
    path = home / 'config.toml'
    original = path.read_text(encoding='utf-8-sig') if path.exists() else ''
    output = headless_config(original, windows=os.name == 'nt')
    if not path.exists() or tomllib.loads(original) != tomllib.loads(output):
        _atomic_write(path, output.encode('utf-8'))


def _copy_cli_skill(source, target):
    """Snapshot the existing CLI autoresearch instructions/scripts, never its state."""
    original = source / "skills" / "codex-autoresearch"
    if not original.exists():
        return
    if _reparse(original.parent) or _reparse(original) or not original.is_dir():
        raise RuntimeError("CLI skill seed must be a regular directory")
    destination = target / "skills" / "codex-autoresearch"
    if _reparse(target / "skills") or _reparse(destination):
        raise RuntimeError("Bridge skills cannot use symlinks or directory junctions")
    destination.mkdir(parents=True, exist_ok=True)
    total_bytes = 0

    def copy_directory(current, relative=Path()):
        nonlocal total_bytes
        for entry in os.scandir(current):
            path = Path(entry.path)
            if _reparse(path):
                raise RuntimeError("CLI skill seed cannot contain symlinks or directory junctions")
            if entry.is_dir(follow_symlinks=False):
                if entry.name in (".git", "__pycache__"):
                    continue
                if relative != Path() or entry.name in SKILL_DIRECTORIES:
                    copy_directory(path, relative / entry.name)
                continue
            if not entry.is_file(follow_symlinks=False) or (path.suffix.lower() not in SKILL_EXTENSIONS and entry.name != "LICENSE"):
                continue
            if (entry.name.startswith((".env", "autoresearch-state", "research-results", "launch_manifest"))
                    or any(part in ("state", "runtime", "runs") for part in relative.parts)):
                continue
            copied = destination / relative / entry.name
            for parent in (copied, *copied.parents):
                if _reparse(parent):
                    raise RuntimeError("Bridge skill files cannot be symlinks or reparse points")
                if parent == target:
                    break
            if copied.exists():
                continue
            data = path.read_bytes()
            total_bytes += len(data)
            if len(data) > 1_000_000 or total_bytes > 10_000_000:
                raise RuntimeError("CLI skill seed exceeds the static-source size limit")
            copied.parent.mkdir(parents=True, exist_ok=True)
            _atomic_write(copied, data)

    copy_directory(original)


def _copy_authorization_rules(source, target):
    directory = source / "rules"
    if not directory.exists():
        return
    if not directory.is_dir() or _reparse(directory):
        raise RuntimeError("Codex authorization rules must be a regular directory")
    destination = target / "rules"
    if _reparse(destination):
        raise RuntimeError("Bridge authorization rules cannot be symlinks or directory junctions")
    destination.mkdir(exist_ok=True)
    for original in directory.glob("*.rules"):
        if _reparse(original) or not original.is_file():
            raise RuntimeError("Codex authorization rules must be regular files")
        copied = destination / original.name
        if _reparse(copied):
            raise RuntimeError("Bridge authorization rules cannot be symlinks or reparse points")
        data = original.read_bytes()
        if copied.exists():
            if copied.read_bytes() != data:
                raise RuntimeError("Codex authorization rules changed; merge them before restarting the bridge")
            continue
        _atomic_write(copied, data)


def _authorization_rule_manifest(home):
    directory = home / "rules"
    if not directory.exists():
        return {}
    if not directory.is_dir() or _reparse(directory):
        raise RuntimeError("Codex authorization rules must be a regular directory")
    manifest = {}
    for path in sorted(directory.glob("*.rules")):
        if _reparse(path) or not path.is_file():
            raise RuntimeError("Codex authorization rules must be regular files")
        manifest[path.name] = hashlib.sha256(path.read_bytes()).hexdigest()
    return manifest


def _validate_rule_snapshot(source, target, expected):
    if (_authorization_rule_manifest(source) != expected
            or _authorization_rule_manifest(target) != expected):
        raise RuntimeError("Codex authorization rules changed, were deleted, or were renamed; "
                           "review and merge them before restarting the bridge")


def prepare_home(config, *, source_home=None):
    """Seed credentials/settings once; never copy desktop conversations or DBs."""
    source = Path(source_home or os.environ.get("CODEX_HOME") or Path.home() / ".codex").expanduser()
    # A nested CLI may already have the private home in its environment. A valid
    # marker still permits reuse; it must never make the home its own seed source.
    candidate = Path(config.codex_home).absolute()
    if source.resolve() == candidate.resolve() and (candidate / MARKER).is_file():
        try:
            existing = json.loads((candidate / MARKER).read_text(encoding="utf-8"))
            source = Path(existing.get("rule_source") or Path.home() / ".codex")
        except (ValueError, OSError) as exc:
            raise RuntimeError("Bridge home isolation marker is invalid") from exc
    target = _validate_home(candidate, source)
    target.mkdir(parents=True, exist_ok=True)
    marker = target / MARKER
    for filename in (MARKER, "auth.json", "config.toml", "AGENTS.md"):
        if _reparse(target / filename):
            raise RuntimeError("Bridge home files cannot be symlinks or reparse points")
    if marker.exists():
        try:
            receipt = json.loads(marker.read_text(encoding="utf-8"))
        except (ValueError, OSError) as exc:
            raise RuntimeError("Bridge home isolation marker is invalid") from exc
        if receipt.get("version") != 1 or receipt.get("home") != str(target):
            raise RuntimeError("Bridge home isolation marker does not match this home")
        if receipt.get("rule_source") and source.resolve() != Path(receipt["rule_source"]).resolve():
            raise RuntimeError("Codex authorization rule source changed; review it before restarting the bridge")
        expected_rules = _authorization_rule_manifest(source)
        if "source_rules" in receipt and receipt["source_rules"] != expected_rules:
            raise RuntimeError("Codex authorization rules changed, were deleted, or were renamed; "
                               "review and merge them before restarting the bridge")
        if "source_rules" not in receipt:
            # Upgrade old private preview markers only after proving every existing
            # copied policy still matches its source. A deleted old allow file must
            # never be adopted as an untracked local rule.
            existing_rules = _authorization_rule_manifest(target)
            if any(expected_rules.get(name) != digest for name, digest in existing_rules.items()):
                raise RuntimeError("Existing bridge authorization rules cannot be validated against their source")
        _check_local_config(target / "config.toml")
        _copy_cli_skill(source, target)
        _copy_authorization_rules(source, target)
        _validate_rule_snapshot(source, target, expected_rules)
        _pin_headless_config(target)
        if "source_rules" not in receipt or "rule_source" not in receipt:
            receipt.update(rule_source=str(source.resolve()), source_rules=expected_rules)
            _atomic_write(marker, json.dumps(receipt, ensure_ascii=False).encode("utf-8"))
        return target
    expected_rules = _authorization_rule_manifest(source)
    copied = []
    for filename in ("auth.json", "AGENTS.md", "config.toml"):
        original, destination = source / filename, target / filename
        if destination.exists():
            if filename == "config.toml":
                _check_local_config(destination)
            continue
        if not original.exists():
            continue
        if not original.is_file() or _reparse(original):
            raise RuntimeError("Codex seed settings must be regular files")
        data = original.read_bytes()
        if filename == "config.toml":
            data = filtered_config(data.decode("utf-8-sig")).encode("utf-8")
        _atomic_write(destination, data)
        copied.append(filename)
    _copy_cli_skill(source, target)
    _copy_authorization_rules(source, target)
    _validate_rule_snapshot(source, target, expected_rules)
    _pin_headless_config(target)
    _atomic_write(marker, json.dumps({"version": 1, "home": str(target), "seed_files": copied,
                                     "rule_source": str(source.resolve()), "source_rules": expected_rules},
                                     ensure_ascii=False).encode("utf-8"))
    return target


def isolated_environment(config, base_env):
    """Avoid desktop session/native pipes; preserve inherited security controls."""
    environment = {key: value for key, value in base_env.items()
                   if key.upper() not in ROUTING_ENV and key.upper() != "CODEX_HOME"
                   and not key.upper().startswith(("NODE_REPL_", "SKY_CUA_", "BROWSER_USE_CODEX_APP_", "BROWSER_USE_ACCESS_"))}
    environment["CODEX_HOME"] = str(Path(config.codex_home).resolve())
    return environment


def disable_remote_control(config, environment, executable):
    """Persist remote-control OFF before app-server can enroll the private home."""
    home = _validate_home(Path(config.codex_home).absolute(), Path.home() / ".codex")
    if Path(environment.get("CODEX_HOME", "")).resolve() != home:
        raise RuntimeError("Remote-control isolation requires the exact private Codex home environment")
    marker = home / MARKER
    if not marker.is_file() or _reparse(marker):
        raise RuntimeError("Private Codex home must be prepared before disabling remote control")
    try:
        receipt = json.loads(marker.read_text(encoding="utf-8"))
    except (ValueError, OSError) as exc:
        raise RuntimeError("Private Codex home marker is invalid") from exc
    if receipt.get("version") != 1 or receipt.get("home") != str(home):
        raise RuntimeError("Private Codex home marker does not match this home")
    command = [str(executable), "-c", "sqlite_home=" + json.dumps(str(home)),
               "-c", "log_dir=" + json.dumps(str(home / "log")),
               "app-server", "daemon", "disable-remote-control"]
    try:
        result = subprocess.run(command, cwd=config.workdir, env=environment,
                                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                text=True, encoding="utf-8", errors="replace", timeout=20,
                                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RuntimeError("Could not disable Codex remote control; the bridge server was not started") from exc
    if result.returncode != 0:
        # Never echo native output here: it can contain account or pairing metadata.
        raise RuntimeError("Could not disable Codex remote control for the private bridge home "
                           f"(exit {result.returncode}); the bridge server was not started")
