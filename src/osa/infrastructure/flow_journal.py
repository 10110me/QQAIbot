"""Bounded processing telemetry. Never stores messages, prompts or tool arguments."""
import asyncio
import json
import sqlite3
import threading
import time
import uuid
from pathlib import Path


class FlowJournal:
    FIELDS = {'action', 'reason', 'step', 'tool', 'outcome', 'model', 'provider',
              'duration_ms', 'error_type', 'message_count', 'trigger', 'wait_seconds',
              'session_id', 'call_id', 'task_id', 'count', 'attachment_count', 'retry_count'}

    def __init__(self, root, memory_only=False, retention_days=7):
        self.memory_only = memory_only
        path = ':memory:' if memory_only else str(Path(root) / 'private' / 'chat-flow.db')
        if not memory_only:
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.lock = threading.RLock()
        self.retention = retention_days * 86400
        self.cleaned = 0
        self.db.execute('CREATE TABLE IF NOT EXISTS stages (id TEXT PRIMARY KEY, source TEXT NOT NULL, ts REAL NOT NULL, kind TEXT NOT NULL, payload TEXT NOT NULL)')
        self.db.execute('CREATE INDEX IF NOT EXISTS stage_source ON stages(source,ts)')
        self.db.execute('CREATE INDEX IF NOT EXISTS stage_time ON stages(ts)')
        self.db.commit()
        if not memory_only:
            try:
                Path(path).chmod(0o600)
            except OSError:
                pass

    def _append(self, source, kind, fields):
        now = time.time()
        # Only controlled metadata is accepted, never arbitrary model output.
        payload = {k: v[:200] if isinstance(v, str) else v for k, v in fields.items()
                   if k in self.FIELDS and isinstance(v, (str, int, float, bool))}
        with self.lock:
            self.db.execute('INSERT INTO stages VALUES(?,?,?,?,?)',
                            (uuid.uuid4().hex, source, now, kind, json.dumps(payload)))
            if now - self.cleaned > 60:
                self.db.execute('DELETE FROM stages WHERE ts<?', (now - self.retention,))
                self.db.execute('DELETE FROM stages WHERE id IN (SELECT id FROM stages ORDER BY ts DESC LIMIT -1 OFFSET 50000)')
                self.cleaned = now
            self.db.commit()

    async def emit(self, source, kind, **fields):
        if not source:
            return
        try:
            if self.memory_only:
                self._append(source, kind, fields)
            else:
                await asyncio.to_thread(self._append, source, kind, fields)
        except Exception:
            # A console recording failure must not interrupt a conversation.
            pass

    def query(self, sources):
        if not sources:
            return {}
        with self.lock:
            rows = self.db.execute('SELECT rowid,id,source,ts,kind,payload FROM stages WHERE source IN (' +
                ','.join('?' for _ in sources) + ') AND ts>=? ORDER BY ts,rowid LIMIT 6000',
                (*sources, time.time() - self.retention)).fetchall()
        result = {}
        for seq, sid, source, ts, kind, payload in rows:
            result.setdefault(source, []).append({'id': sid, 'seq': seq, 'ts': ts, 'kind': kind, **json.loads(payload)})
        return result

    def close(self):
        with self.lock:
            self.db.close()
