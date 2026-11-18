"""Move one bridge-owned conversation into its private Codex namespace."""
from __future__ import annotations

import json
from contextlib import closing
import os
from pathlib import Path
import shutil
import sqlite3
import stat
import tempfile


ORIGINATOR = "codex_telegram_bridge"


def _strip_extended_path(value):
    value = str(value)
    if value.startswith("\\\\?\\UNC\\"):
        return "\\\\" + value[8:]
    if value.startswith("\\\\?\\"):
        return value[4:]
    return value


def _normalized_path(value):
    return os.path.normcase(os.path.abspath(os.path.normpath(_strip_extended_path(value))))


def _reparse(path):
    try:
        info = path.lstat()
    except FileNotFoundError:
        return False
    return path.is_symlink() or bool(getattr(info, "st_file_attributes", 0) &
                                    getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0))


def _owned_rollout(source, saved, workdir):
    database = source / "state_5.sqlite"
    if not database.is_file() or _reparse(database):
        raise RuntimeError("Original Codex conversation index is missing; migration did not run")
    try:
        with closing(sqlite3.connect(database.resolve().as_uri() + "?mode=ro", uri=True)) as db:
            row = db.execute("SELECT id,rollout_path,cwd,originator FROM threads WHERE id=?", (saved,)).fetchone()
    except sqlite3.Error as exc:
        raise RuntimeError("Original Codex conversation index could not be read") from exc
    if row is None or row[3] != ORIGINATOR or _normalized_path(row[2]) != _normalized_path(workdir):
        raise RuntimeError("Only a bridge-owned conversation from this project can be migrated")
    rollout = Path(_strip_extended_path(row[1])).absolute()
    if rollout.suffix.lower() != ".jsonl" or not rollout.is_file():
        raise RuntimeError("Original Codex conversation rollout is not a regular JSONL file")
    for path in (rollout, *rollout.parents):
        if _reparse(path):
            raise RuntimeError("Conversation migration cannot follow symlinks or directory junctions")
        if path == source:
            break
    resolved = rollout.resolve()
    allowed = [source / "sessions", source / "archived_sessions"]
    if not any(parent.resolve() in resolved.parents for parent in allowed):
        raise RuntimeError("Conversation rollout is outside the original Codex session directories")
    return rollout


def _copy_rollout(original, target, saved, workdir):
    directory = target / "sessions" / "imported"
    for path in (target / "sessions", directory):
        if _reparse(path):
            raise RuntimeError("Private conversation directory cannot be a symlink or directory junction")
    directory.mkdir(parents=True, exist_ok=True)
    destination = directory / original.name
    if _reparse(destination):
        raise RuntimeError("Private conversation rollout cannot be a symlink or reparse point")
    before = original.stat()
    descriptor, temporary_name = tempfile.mkstemp(prefix=".bridge-import-", dir=directory)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as copied:
            with original.open("rb") as source:
                header = source.readline(4 * 1024 * 1024)
                try:
                    metadata = json.loads(header)
                    payload = metadata["payload"]
                    valid = (metadata["type"] == "session_meta" and payload["id"] == saved
                             and payload["originator"] == ORIGINATOR
                             and _normalized_path(payload["cwd"]) == _normalized_path(workdir))
                except (ValueError, KeyError, TypeError) as exc:
                    raise RuntimeError("Conversation rollout metadata is invalid") from exc
                if not valid:
                    raise RuntimeError("Conversation rollout metadata does not match the bridge conversation")
                copied.write(header)
                shutil.copyfileobj(source, copied)
            copied.flush()
            os.fsync(copied.fileno())
        after = original.stat()
        if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
            raise RuntimeError("Original conversation changed during migration; wait for it to finish")
        os.chmod(temporary, 0o600)
        os.replace(temporary, destination)
        return destination
    finally:
        temporary.unlink(missing_ok=True)


def migrate_conversation(config, store, rpc, source_home=None):
    """Fork only the saved bridge history after draining work, never execute a turn."""
    private = Path(config.codex_home).resolve()
    saved = store.get("codex_thread")
    previous_home = store.get("codex_home")
    if previous_home and _normalized_path(previous_home) == _normalized_path(private):
        return saved
    if saved is None:
        with store.transaction():
            if store.get("codex_thread") is not None:
                raise RuntimeError("Bridge conversation changed before namespace initialization")
            store.set("codex_home", str(private))
            store.audit("codex_home_initialized", {"home": str(private)})
        return None
    candidate = source_home or previous_home or os.environ.get("CODEX_HOME") or Path.home() / ".codex"
    source = Path(_strip_extended_path(candidate)).expanduser().resolve()
    if source == private or source in private.parents or private in source.parents:
        raise RuntimeError("Original and private Codex homes must be separate for migration")
    rollout = _owned_rollout(source, saved, config.workdir)
    copied = _copy_rollout(rollout, private, saved, config.workdir)
    with store.lock:
        completed = store.db.execute("SELECT turn_id FROM jobs WHERE thread_id=? AND status='completed' "
            "AND turn_id IS NOT NULL ORDER BY COALESCE(finished,created) DESC,created DESC LIMIT 1", (saved,)).fetchone()
    params = {"path": str(copied), "threadId": saved, "excludeTurns": True,
              "deferGoalContinuation": True, "cwd": str(config.workdir),
              "approvalPolicy": "never", "approvalsReviewer": config.reviewer}
    model = store.get("model", config.model)
    if model:
        params["model"] = model
    effort = store.get("effort", config.effort)
    if effort:
        params["config"] = {"model_reasoning_effort": effort}
    if completed:
        params["lastTurnId"] = completed[0]
    try:
        result = rpc.request("thread/fork", params, timeout=180)
        thread = result["thread"]
        new_id = thread["id"]
        if not new_id or new_id == saved:
            raise RuntimeError("Native migration did not return an independent conversation")
        if thread.get("cwd") and _normalized_path(thread["cwd"]) != _normalized_path(config.workdir):
            raise RuntimeError("Native migration returned another project's conversation")
        turns = rpc.request("thread/turns/list", {"threadId": new_id, "limit": 1,
                                                 "sortDirection": "desc"}).get("data", [])
        if not turns:
            raise RuntimeError("Native migration returned an empty conversation history")
        if completed and turns[0].get("id") != completed[0]:
            raise RuntimeError("Native migration did not preserve the selected completed bridge turn")
    except Exception as exc:
        raise RuntimeError("Bridge conversation migration did not complete. The original conversation ID was "
                           "preserved and no model turn was executed; a private partial import may remain.") from exc
    with store.transaction():
        if store.get("codex_thread") != saved or store.get("codex_home") != previous_home:
            raise RuntimeError("Bridge conversation changed during migration; original namespace was not replaced")
        store.set("codex_thread", new_id)
        store.set("codex_home", str(private))
        store.audit("codex_home_migrated", {"from_thread": saved, "to_thread": new_id,
                    "home": str(private), "last_completed_turn": completed[0] if completed else None})
    return new_id
