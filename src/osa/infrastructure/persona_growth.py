"""Persistent, encrypted self-reflections, independent of model configuration revisions."""

import hashlib
import sqlite3
import threading
import time
import unicodedata
import uuid
from contextlib import contextmanager


class PersonaGrowthStore:
    def __init__(self, directory, cipher=None, memory_only=False):
        self.path = directory / 'persona-growth.db'
        self.cipher = cipher
        self.lock = threading.RLock()
        self.memory = sqlite3.connect(':memory:', check_same_thread=False) if memory_only else None
        with self.connection() as connection:
            connection.execute('''CREATE TABLE IF NOT EXISTS reflections (
                id TEXT PRIMARY KEY, conversation TEXT NOT NULL, source_id TEXT NOT NULL,
                content BLOB NOT NULL, fingerprint TEXT NOT NULL, created_at REAL NOT NULL,
                active INTEGER NOT NULL DEFAULT 1, replaced_id TEXT,
                UNIQUE(conversation, source_id))''')
            connection.execute('CREATE INDEX IF NOT EXISTS reflections_scope ON reflections(conversation, active, created_at)')
        if not memory_only:
            self.path.chmod(0o600)

    @contextmanager
    def connection(self):
        with self.lock:
            connection = self.memory or sqlite3.connect(self.path, timeout=10)
            connection.row_factory = sqlite3.Row
            try:
                with connection:
                    yield connection
            finally:
                if self.memory is None:
                    connection.close()

    def decode(self, row):
        value = dict(row)
        content = bytes(value['content'])
        value['text'] = (self.cipher.decrypt(content) if self.cipher else content).decode('utf-8')
        del value['content']
        del value['fingerprint']
        value['active'] = bool(value['active'])
        return value

    def snapshot(self, conversation=None, include_history=False):
        with self.connection() as connection:
            clauses, arguments = [], []
            if conversation is not None:
                clauses.append('conversation = ?')
                arguments.append(conversation)
            if not include_history:
                clauses.append('active = 1')
            where = ' WHERE ' + ' AND '.join(clauses) if clauses else ''
            limit = 200 if include_history else 60
            rows = connection.execute('SELECT * FROM reflections' + where
                + ' ORDER BY created_at DESC, rowid DESC LIMIT ?', [*arguments, limit]).fetchall()
            return [self.decode(row) for row in reversed(rows)]

    def conversations(self):
        with self.connection() as connection:
            return [row[0] for row in connection.execute('SELECT DISTINCT conversation FROM reflections ORDER BY conversation')]

    def evolve(self, conversation, source_id, text, replace_id=None, now=None):
        if not isinstance(text, str) or not 1 <= len(text.strip()) <= 30 or '\n' in text or '\r' in text:
            return {'status': 'invalid_note'}
        text = text.strip()
        normalized = ''.join(character for character in unicodedata.normalize('NFKC', text).casefold()
                             if character.isalnum())
        if not normalized or not source_id or not conversation:
            return {'status': 'invalid_note'}
        fingerprint = hashlib.sha256(normalized.encode()).hexdigest()
        now = time.time() if now is None else now
        with self.connection() as connection:
            connection.execute('BEGIN IMMEDIATE')
            existing = connection.execute('SELECT id FROM reflections WHERE conversation=? AND source_id=?',
                                          (conversation, source_id)).fetchone()
            if existing:
                return {'status': 'already_recorded', 'id': existing['id']}
            duplicate = connection.execute('SELECT id FROM reflections WHERE conversation=? AND active=1 AND fingerprint=?',
                                           (conversation, fingerprint)).fetchone()
            if duplicate:
                return {'status': 'duplicate', 'id': duplicate['id']}
            latest = connection.execute('SELECT MAX(created_at) FROM reflections WHERE conversation=?',
                                        (conversation,)).fetchone()[0]
            if latest is not None and now - latest < 300:
                return {'status': 'cooldown'}
            if replace_id:
                replaced = connection.execute('SELECT id FROM reflections WHERE id=? AND conversation=? AND active=1',
                                              (replace_id, conversation)).fetchone()
                if not replaced:
                    return {'status': 'unknown_note'}
                connection.execute('UPDATE reflections SET active=0 WHERE id=?', (replace_id,))
            note_id = uuid.uuid4().hex
            content = text.encode('utf-8')
            if self.cipher:
                content = self.cipher.encrypt(content)
            connection.execute('INSERT INTO reflections(id,conversation,source_id,content,fingerprint,created_at,replaced_id) '
                               'VALUES(?,?,?,?,?,?,?)',
                               (note_id, conversation, source_id, content, fingerprint, now, replace_id))
            connection.execute('UPDATE reflections SET active=0 WHERE id IN (SELECT id FROM reflections '
                               'WHERE conversation=? AND active=1 ORDER BY created_at DESC,rowid DESC LIMIT -1 OFFSET 60)',
                               (conversation,))
            return {'status': 'recorded', 'id': note_id}

    def set_active(self, note_id, active):
        with self.connection() as connection:
            connection.execute('BEGIN IMMEDIATE')
            row = connection.execute('SELECT * FROM reflections WHERE id=?', (note_id,)).fetchone()
            if row is None:
                raise KeyError(note_id)
            if bool(row['active']) == active:
                return self.decode(row)
            if active:
                connection.execute('UPDATE reflections SET active=0 WHERE conversation=? AND replaced_id=?',
                                   (row['conversation'], note_id))
            connection.execute('UPDATE reflections SET active=? WHERE id=?', (int(active), note_id))
            if row['replaced_id']:
                connection.execute('UPDATE reflections SET active=? WHERE id=? AND conversation=?',
                                   (int(not active), row['replaced_id'], row['conversation']))
            protected = note_id if active else row['replaced_id']
            connection.execute('UPDATE reflections SET active=0 WHERE id IN (SELECT id FROM reflections '
                               'WHERE conversation=? AND active=1 AND id!=? ORDER BY created_at DESC,rowid DESC LIMIT -1 OFFSET ?)',
                               (row['conversation'], protected or '', 59 if protected else 60))
            return self.decode(connection.execute('SELECT * FROM reflections WHERE id=?', (note_id,)).fetchone())
