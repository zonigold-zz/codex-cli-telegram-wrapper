from __future__ import annotations

from contextlib import contextmanager
import json
from pathlib import Path
import secrets
import sqlite3
import threading
import time
import uuid


class Store:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self.lock = threading.RLock()
        self.db = sqlite3.connect(path, check_same_thread=False, isolation_level=None, timeout=15)
        self.db.row_factory = sqlite3.Row
        self.db.executescript('''
            PRAGMA journal_mode=WAL;
            PRAGMA busy_timeout=15000;
            CREATE TABLE IF NOT EXISTS kv(key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS inbox(
              id INTEGER PRIMARY KEY, payload TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending');
            CREATE TABLE IF NOT EXISTS jobs(
              id TEXT PRIMARY KEY, source TEXT UNIQUE, prompt TEXT NOT NULL,
              status TEXT NOT NULL, thread_id TEXT, turn_id TEXT, result TEXT, error TEXT,
              created REAL NOT NULL, finished REAL);
            CREATE TABLE IF NOT EXISTS outbox(
              id INTEGER PRIMARY KEY AUTOINCREMENT, dedupe TEXT UNIQUE, kind TEXT NOT NULL,
              payload TEXT NOT NULL, priority INTEGER NOT NULL, revision INTEGER NOT NULL DEFAULT 1,
              status TEXT NOT NULL DEFAULT 'pending', attempts INTEGER NOT NULL DEFAULT 0,
              due REAL NOT NULL DEFAULT 0, error TEXT);
            CREATE TABLE IF NOT EXISTS actions(
              token TEXT PRIMARY KEY, payload TEXT NOT NULL, expires REAL NOT NULL, used INTEGER NOT NULL DEFAULT 0);
            CREATE TABLE IF NOT EXISTS audit(
              id INTEGER PRIMARY KEY AUTOINCREMENT, created REAL NOT NULL, event TEXT NOT NULL, detail TEXT NOT NULL);
        ''')

    @contextmanager
    def transaction(self):
        with self.lock:
            self.db.execute("BEGIN IMMEDIATE")
            try:
                yield
            except BaseException:
                self.db.execute("ROLLBACK")
                raise
            else:
                self.db.execute("COMMIT")

    def get(self, key, default=None):
        with self.lock:
            row = self.db.execute("SELECT value FROM kv WHERE key=?", (key,)).fetchone()
            return json.loads(row[0]) if row else default

    def set(self, key, value):
        with self.lock:
            self.db.execute("INSERT INTO kv VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                            (key, json.dumps(value, ensure_ascii=False)))

    def audit(self, event, detail):
        with self.lock:
            self.db.execute("INSERT INTO audit(created,event,detail) VALUES(?,?,?)",
                            (time.time(), event, json.dumps(detail, ensure_ascii=False)))

    def ingest(self, updates):
        with self.transaction():
            offset = self.get("offset", 0)
            for update in updates:
                update_id = int(update["update_id"])
                self.db.execute("INSERT OR IGNORE INTO inbox(id,payload) VALUES(?,?)",
                                (update_id, json.dumps(update, ensure_ascii=False)))
                offset = max(offset, update_id + 1)
            self.set("offset", offset)

    def pending_inbox(self):
        with self.lock:
            return [dict(row) for row in self.db.execute("SELECT * FROM inbox WHERE status='pending' ORDER BY id")]

    def finish_inbox(self, update_id):
        with self.lock:
            self.db.execute("UPDATE inbox SET status='done' WHERE id=?", (update_id,))

    def enqueue_job(self, prompt, source):
        with self.lock:
            job_id = uuid.uuid4().hex[:12]
            self.db.execute("INSERT OR IGNORE INTO jobs(id,source,prompt,status,created) VALUES(?,?,?,'queued',?)",
                            (job_id, str(source), prompt, time.time()))
            return self.db.execute("SELECT id FROM jobs WHERE source=?", (str(source),)).fetchone()[0]

    def job(self, job_id):
        with self.lock:
            row = self.db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
            return dict(row) if row else None

    def next_job(self):
        with self.transaction():
            row = self.db.execute("SELECT * FROM jobs WHERE status='queued' ORDER BY created LIMIT 1").fetchone()
            if not row:
                return None
            self.db.execute("UPDATE jobs SET status='running' WHERE id=?", (row["id"],))
            return dict(row)

    def update_job(self, job_id, **fields):
        allowed = {"status", "thread_id", "turn_id", "result", "error", "finished"}
        if not fields or not set(fields) <= allowed:
            raise ValueError("Invalid job fields")
        with self.lock:
            self.db.execute("UPDATE jobs SET " + ",".join(k + "=?" for k in fields) + " WHERE id=?",
                            [*fields.values(), job_id])

    def recover(self):
        with self.lock:
            running = [dict(r) for r in self.db.execute("SELECT * FROM jobs WHERE status='running'")]
            self.db.execute("UPDATE jobs SET status='review_required', error='Bridge restarted; reconcile before retry' WHERE status='running'")
            return running

    def cancel_queued(self, auto_only=False):
        with self.lock:
            clause = " AND CASE WHEN json_valid(prompt) THEN json_extract(prompt,'$.kind') ELSE '' END='plan_step'" if auto_only else ""
            self.db.execute("UPDATE jobs SET status='cancelled',finished=? WHERE status='queued'" + clause, (time.time(),))

    def queue(self, kind, payload, priority=20, dedupe=None):
        with self.lock:
            self.db.execute('''INSERT INTO outbox(dedupe,kind,payload,priority) VALUES(?,?,?,?)
                ON CONFLICT(dedupe) DO UPDATE SET payload=excluded.payload,kind=excluded.kind,
                priority=excluded.priority,revision=outbox.revision+1,status='pending',due=0,attempts=0''',
                            (dedupe, kind, json.dumps(payload, ensure_ascii=False), priority))

    def next_outbound(self):
        with self.lock:
            row = self.db.execute("SELECT * FROM outbox WHERE status='pending' AND due<=? ORDER BY priority,id LIMIT 1",
                                  (time.time(),)).fetchone()
            return dict(row) if row else None

    def outbound_done(self, row):
        with self.lock:
            self.db.execute("UPDATE outbox SET status='delivered',error=NULL WHERE id=? AND revision=?",
                            (row["id"], row["revision"]))

    def outbound_error(self, row, error, delay, permanent=False):
        with self.lock:
            self.db.execute("UPDATE outbox SET status=?,attempts=attempts+1,due=?,error=? WHERE id=? AND revision=?",
                            ("failed" if permanent else "pending", time.time() + delay, error, row["id"], row["revision"]))

    def action(self, payload, seconds=3600):
        token = secrets.token_urlsafe(12)
        with self.lock:
            self.db.execute("INSERT INTO actions VALUES(?,?,?,0)",
                            (token, json.dumps(payload, ensure_ascii=False), time.time() + seconds))
        return "a:" + token

    def consume(self, data):
        if not data.startswith("a:"):
            return None
        with self.transaction():
            row = self.db.execute("SELECT * FROM actions WHERE token=? AND used=0 AND expires>?",
                                  (data[2:], time.time())).fetchone()
            if not row:
                return None
            self.db.execute("UPDATE actions SET used=1 WHERE token=?", (data[2:],))
            return json.loads(row["payload"])

    def close(self):
        with self.lock:
            self.db.close()
