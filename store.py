"""SQLite is authoritative; each completed request also has a JSON export."""

import json
import sqlite3
import time
import uuid
from contextlib import contextmanager
from pathlib import Path


class Archive:
    def __init__(self, root):
        self.root = Path(root)
        self.records = self.root / "records"
        self.records.mkdir(parents=True, exist_ok=True)
        self.path = self.root / "archive.sqlite3"
        with self.connect() as db:
            db.executescript("""
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS runs(
                id TEXT PRIMARY KEY, created_at REAL NOT NULL, finished_at REAL,
                scope TEXT NOT NULL, cache_key TEXT NOT NULL, url TEXT NOT NULL,
                status TEXT NOT NULL, document TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS cache_lookup ON runs(cache_key,status,finished_at);
            CREATE INDEX IF NOT EXISTS session_lookup ON runs(scope,created_at);
            """)
            interrupted = db.execute("SELECT document FROM runs WHERE status='running'").fetchall()
        for row in interrupted:
            doc = json.loads(row[0])
            doc.update(status="interrupted", error="上次进程停止时任务尚未完成。")
            self.finish(doc)

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=30)
        try:
            with db:
                yield db
        finally:
            db.close()

    def start(self, scope, cache_key, url, sender):
        run_id = uuid.uuid4().hex
        now = time.time()
        document = {
            "schema_version": 1,
            "id": run_id,
            "created_at": now,
            "scope": scope,
            "sender_id": sender,
            "url": url,
            "status": "running",
        }
        with self.connect() as db:
            db.execute(
                "INSERT INTO runs VALUES(?,?,NULL,?,?,?,'running',?)",
                (run_id, now, scope, cache_key, url, json.dumps(document, ensure_ascii=False)),
            )
        return document

    def finish(self, document):
        document["finished_at"] = time.time()
        encoded = json.dumps(document, ensure_ascii=False, indent=2)
        with self.connect() as db:
            db.execute(
                "UPDATE runs SET status=?, finished_at=?, document=? WHERE id=?",
                (document["status"], document["finished_at"], encoded, document["id"]),
            )
        path = self.records / (document["id"] + ".json")
        tmp = path.with_suffix(".tmp")
        tmp.write_text(encoded, encoding="utf-8")
        tmp.replace(path)

    def cached(self, cache_key, hours):
        if not hours:
            return None
        with self.connect() as db:
            row = db.execute(
                "SELECT document FROM runs WHERE cache_key=? AND status='success' AND finished_at>? ORDER BY finished_at DESC LIMIT 1",
                (cache_key, time.time() - hours * 3600),
            ).fetchone()
        if not row:
            return None
        document = json.loads(row[0])
        origin = document.get("content_created_at", document.get("finished_at", 0))
        return document if origin > time.time() - hours * 3600 else None

    def recent(self, scope, query="", limit=10):
        with self.connect() as db:
            rows = db.execute(
                "SELECT document FROM runs WHERE scope=? ORDER BY created_at DESC LIMIT 100",
                (scope,),
            ).fetchall()
        result = []
        for row in rows:
            d = json.loads(row[0])
            if (
                not query
                or query.casefold()
                in (
                    d.get("metadata", {}).get("title", "") + d.get("summary", "") + d["url"]
                ).casefold()
            ):
                result.append(d)
        return result[:limit]

    def get(self, scope, prefix):
        if len(prefix) < 8 or any(c not in "0123456789abcdef" for c in prefix):
            return None
        with self.connect() as db:
            rows = db.execute(
                "SELECT document FROM runs WHERE scope=? AND id LIKE ?", (scope, prefix + "%")
            ).fetchall()
        return json.loads(rows[0][0]) if len(rows) == 1 else None
