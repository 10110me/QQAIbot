"""V3 transactional projections and a durable, idempotent parent wakeup bus.

The V2 registry remains the task source of truth. Both projections and domain
events are written in the very same transaction as its task change.
"""
import hashlib
import json
import time
import uuid

from osa.infrastructure.v2.registry import Registry as V2Registry, dump
from osa.domain.work import clean_text, StaleLease, VersionConflict

SCHEMA = [
    "CREATE TABLE IF NOT EXISTS agent_tasks_v3 (id TEXT PRIMARY KEY, parent_task_id TEXT, owner_user_id TEXT NOT NULL, origin_group_id TEXT, kind TEXT NOT NULL, status TEXT NOT NULL, active_provider TEXT, provider_session_id TEXT, workspace_id TEXT, last_progress_at DOUBLE PRECISION, idempotency_key TEXT UNIQUE NOT NULL, payload TEXT NOT NULL)",
    "CREATE INDEX IF NOT EXISTS agent_parent_v3 ON agent_tasks_v3(parent_task_id)",
    "CREATE TABLE IF NOT EXISTS thinking_sessions (id TEXT PRIMARY KEY, owner_id TEXT NOT NULL, group_id TEXT, status TEXT NOT NULL, updated DOUBLE PRECISION NOT NULL, payload TEXT NOT NULL)",
    "CREATE TABLE IF NOT EXISTS agent_domain_events (seq INTEGER PRIMARY KEY, id TEXT UNIQUE NOT NULL, aggregate_id TEXT NOT NULL, parent_task_id TEXT, event_type TEXT NOT NULL, schema_version INTEGER NOT NULL, correlation_id TEXT NOT NULL, idempotency_key TEXT UNIQUE NOT NULL, payload TEXT NOT NULL, occurred_at DOUBLE PRECISION NOT NULL)",
    "CREATE INDEX IF NOT EXISTS agent_events_aggregate ON agent_domain_events(aggregate_id,seq)",
    "CREATE TABLE IF NOT EXISTS thinking_wakeups (id TEXT PRIMARY KEY, parent_task_id TEXT NOT NULL, event_id TEXT UNIQUE NOT NULL, status TEXT NOT NULL, lease_until DOUBLE PRECISION NOT NULL, generation INTEGER NOT NULL, payload TEXT NOT NULL)",
    "CREATE INDEX IF NOT EXISTS thinking_wakeup_queue ON thinking_wakeups(status,lease_until)",
    "CREATE TABLE IF NOT EXISTS coding_bindings (task_id TEXT PRIMARY KEY, provider TEXT NOT NULL, session_id TEXT, state TEXT NOT NULL, payload TEXT NOT NULL)",
    "CREATE TABLE IF NOT EXISTS coding_review_reports (id TEXT PRIMARY KEY, coding_task_id TEXT NOT NULL, observed_revision TEXT, observer_provider TEXT NOT NULL, observer_model TEXT NOT NULL, payload TEXT NOT NULL, created_at DOUBLE PRECISION NOT NULL)",
    "CREATE INDEX IF NOT EXISTS coding_reviews_task ON coding_review_reports(coding_task_id,created_at)",
    "CREATE TABLE IF NOT EXISTS agent_tool_receipts (id TEXT PRIMARY KEY, session_id TEXT NOT NULL, signature TEXT NOT NULL, status TEXT NOT NULL, payload TEXT NOT NULL)",
]


class Registry(V2Registry):
    def __init__(self, location):
        super().__init__(location)
        with self.tx() as db:
            for statement in SCHEMA:
                db.execute(statement)
            db.execute("INSERT INTO v2_meta VALUES('v3_schema_version','1') ON CONFLICT(key) DO NOTHING")
            for row in db.execute("SELECT id FROM v2_tasks").fetchall():
                self._project(db, self._task(db, row["id"]))

    def _save(self, db, task):
        super()._save(db, task)
        self._project(db, task)

    def _project(self, db, task):
        binding = db.execute("SELECT provider,session_id FROM coding_bindings WHERE task_id=?", (task["id"],)).fetchone()
        db.execute("INSERT INTO agent_tasks_v3 VALUES(?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET parent_task_id=excluded.parent_task_id,status=excluded.status,active_provider=excluded.active_provider,provider_session_id=excluded.provider_session_id,last_progress_at=excluded.last_progress_at,payload=excluded.payload",
            (task["id"], task.get("parent_task_id"), task["owner_id"], task["group_id"], "coding", task["status"],
             binding["provider"] if binding else "builtin", binding["session_id"] if binding else None, task["workspace_id"],
             task["last_progress_at"], "coding:"+task["id"], dump(self.public(task))))

    def _event(self, db, task_id, kind, payload):
        seq = super()._event(db, task_id, kind, payload)
        task = self._task(db, task_id)
        self._project(db, task)
        event_type = {"task.created.v1": "coding.task.created", "task.lease.acquired.v1": "coding.task.started",
                      "task.progress.updated.v1": "coding.progress.updated", "task.command.applied.v1": "coding.instruction.applied"}.get(kind)
        if kind in {"task.finished.v1", "task.stopped.v1", "task.interrupted.v1"}:
            event_type = {"COMPLETED": "coding.completed", "FAILED": "coding.failed", "CANCELLED": "coding.cancelled",
                          "WAITING_INPUT": "coding.input_required", "INTERRUPTED": "coding.interrupted"}.get(task["status"], "coding.progress.updated")
        if event_type:
            evidence = payload.get("evidence") or {}
            activity = task.get("agent_activity") or evidence.get("agent_activity") or []
            if isinstance(activity, list):
                activity = activity[-1:]  # domain events stay small; v2 checkpoints retain the full bounded list
            self._publish(db, task_id, task.get("parent_task_id"), event_type, "v2-event:"+str(seq),
                          {"status": task["status"], "revision": task.get("git_commit"), "tests_passed": task["tests_passed"], "tests_failed": task["tests_failed"], "current_step": task.get("current_step"), "agent_activity": activity, "evidence_ref": str(seq)})
        return seq

    def _publish(self, db, aggregate, parent, event_type, key, payload):
        old = db.execute("SELECT id FROM agent_domain_events WHERE idempotency_key=?", (key,)).fetchone()
        if old:
            return old["id"]
        eid = uuid.uuid4().hex
        seq = db.execute("SELECT COALESCE(MAX(seq),0)+1 AS n FROM agent_domain_events").fetchone()["n"]
        db.execute("INSERT INTO agent_domain_events VALUES(?,?,?,?,?,?,?,?,?,?)",
                   (seq, eid, aggregate, parent, event_type, 1, parent or aggregate, key, dump(payload), time.time()))
        if parent and event_type in {"coding.completed", "coding.failed", "coding.input_required", "research.completed", "research.failed"}:
            db.execute("INSERT INTO thinking_wakeups VALUES(?,?,?,?,?,?,?) ON CONFLICT(event_id) DO NOTHING",
                       (uuid.uuid4().hex, parent, eid, "QUEUED", 0, 0, dump({"aggregate_id": aggregate, "event_type": event_type, "evidence": payload})))
            self._publish(db, parent, None, "thinking.resume.requested", "resume:"+eid,
                          {"child_event_id": eid, "aggregate_id": aggregate})
        return eid

    def publish(self, aggregate, parent, event_type, key, payload):
        with self.tx() as db:
            return self._publish(db, aggregate, parent, event_type, key, payload)

    def attach_parent(self, tid, parent):
        with self.tx() as db:
            task = self._task(db, tid)
            row = db.execute("SELECT * FROM thinking_sessions WHERE id=?", (parent,)).fetchone()
            if not row or row["owner_id"] != task["owner_id"] or row["group_id"] != task["group_id"]:
                raise PermissionError("Parent scope mismatch")
            if task.get("parent_task_id") not in {None, parent}:
                raise VersionConflict("Parent already assigned")
            task["parent_task_id"] = parent
            self._save(db, task)
            return task

    def begin_thinking(self, sid, context):
        with self.tx() as db:
            row = db.execute("SELECT * FROM thinking_sessions WHERE id=?", (sid,)).fetchone()
            if row:
                if row["owner_id"] != context["actor_id"] or row["group_id"] != context.get("group_id"):
                    raise PermissionError("Session scope mismatch")
                return {"status": row["status"], **json.loads(row["payload"])}
            # Retain source metadata, never duplicate unconsented chat history.
            value = {"context": context, "created_at": time.time()}
            db.execute("INSERT INTO thinking_sessions VALUES(?,?,?,?,?,?)", (sid, context["actor_id"], context.get("group_id"), "RUNNING", time.time(), dump(value)))
            return {"status": "RUNNING", **value}

    def finish_thinking(self, sid, result, traces, delegated, dependencies=None, dependency_snapshot=None):
        with self.tx() as db:
            row = db.execute("SELECT payload FROM thinking_sessions WHERE id=?", (sid,)).fetchone()
            if not row:
                raise KeyError(sid)
            payload = json.loads(row["payload"])
            if not payload["context"].get("collect_messages", True):
                result = {**result, "brief": None}
            payload.update(result=result, traces=traces, delegated=delegated)
            if dependency_snapshot is not None:
                payload["context"]["dependency_snapshot"] = dependency_snapshot
            payload["context"].setdefault("dependency_snapshot", {})["agent_dependencies"] = dependencies or []
            db.execute("UPDATE thinking_sessions SET status='FINISHED',updated=?,payload=? WHERE id=?", (time.time(), dump(payload), sid))

    def thinking(self, sid):
        with self.tx(write=False) as db:
            row = db.execute("SELECT * FROM thinking_sessions WHERE id=?", (sid,)).fetchone()
            if not row:
                raise KeyError(sid)
            return {**dict(row), "payload": json.loads(row["payload"])}

    def valid_agent_dependencies(self, refs, actor, group, authorized_sources=()):
        with self.tx(write=False) as db:
            for ref in refs:
                expected_actor = actor
                if ref.get('request_session_id'):
                    child = db.execute('SELECT * FROM thinking_sessions WHERE id=?', (ref['request_session_id'],)).fetchone()
                    if not child or child['group_id'] != group or ref.get('request_id') not in authorized_sources:
                        return False
                    ctx = json.loads(child['payload'])['context']
                    parent = db.execute('SELECT owner_id,group_id FROM thinking_sessions WHERE id=?', (ctx.get('batch_parent_session_id'),)).fetchone()
                    if not parent or parent['owner_id'] != actor or parent['group_id'] != group or ctx['source_id'] != ref['request_id']:
                        return False
                    expected_actor = child['owner_id']
                if ref["kind"] == "coding":
                    try:
                        task = self._task(db, ref["id"])
                    except KeyError:
                        return False
                    if task["owner_id"] != expected_actor or task["group_id"] != group or task["status"] == "CANCELLED" or task.get('archived_at'):
                        return False
                elif ref["kind"] == "research":
                    row = db.execute("SELECT * FROM v2_records WHERE id=? AND kind='night_research'", (ref["id"],)).fetchone()
                    if not row or row["owner_id"] != expected_actor or row["group_id"] != group or row["state"] == "REVOKED":
                        return False
                else:
                    return False
            return True

    def sources_for_message(self, destination, message_id):
        with self.tx(write=False) as db:
            if destination['kind'] == 'group':
                rows = db.execute("SELECT payload FROM thinking_sessions WHERE group_id=?", (destination['id'],)).fetchall()
            else:
                rows = db.execute("SELECT payload FROM thinking_sessions WHERE owner_id=? AND group_id IS NULL", (destination['id'],)).fetchall()
            sources = []
            for row in rows:
                ctx = json.loads(row['payload'])['context']
                event = ctx.get('source_event', {})
                if event.get('message_id') == message_id and event.get('destination') == destination:
                    sources.append(ctx['source_id'])
            return list(set(sources))

    def expire_thinking(self, before):
        with self.tx() as db:
            rows = db.execute("SELECT id,payload FROM thinking_sessions WHERE status='FINISHED' AND updated<? LIMIT 100", (before,)).fetchall()
            expired = 0
            for row in rows:
                active = db.execute("SELECT id FROM agent_tasks_v3 WHERE parent_task_id=? AND status NOT IN ('COMPLETED','FAILED','CANCELLED','DENIED') LIMIT 1", (row['id'],)).fetchone()
                pending = db.execute("SELECT id FROM thinking_wakeups WHERE parent_task_id=? AND status IN ('QUEUED','RUNNING') LIMIT 1", (row['id'],)).fetchone()
                if active or pending:
                    continue
                for table, column in [('agent_tool_receipts', 'session_id'), ('thinking_wakeups', 'parent_task_id'), ('agent_domain_events', 'parent_task_id'), ('thinking_sessions', 'id')]:
                    db.execute('DELETE FROM '+table+' WHERE '+column+'=?', (row['id'],))
                db.execute('DELETE FROM agent_domain_events WHERE aggregate_id=?', (row['id'],))
                expired += 1
            return expired

    def update_record(self, rid, state, patch):
        with self.tx() as db:
            row = db.execute("SELECT * FROM v2_records WHERE id=?", (rid,)).fetchone()
            if not row:
                raise KeyError(rid)
            if row["state"] == "REVOKED":
                return False
            payload = {**json.loads(row["payload"]), **patch}
            db.execute("UPDATE v2_records SET state=?,payload=? WHERE id=?", (state, dump(payload), rid))
            if row["kind"] == "night_research" and state in {"NEEDS_REVIEW", "DEFERRED"} and payload.get("parent_task_id"):
                self._publish(db, rid, payload["parent_task_id"], "research.completed" if state == "NEEDS_REVIEW" else "research.failed",
                              "research:"+rid+":"+state, {"status": state, "verified_claims": payload.get("verified_claims", 0)})

    def domain_events(self, tid, after=0, latest=False):
        with self.tx(write=False) as db:
            order = 'DESC' if latest else 'ASC'
            rows = db.execute("SELECT * FROM agent_domain_events WHERE aggregate_id=? AND seq>? ORDER BY seq " + order + " LIMIT 200", (tid, after)).fetchall()
            if latest:rows = rows[::-1]
            return [{**dict(r), "payload": json.loads(r["payload"])} for r in rows]

    def claim_wakeup(self, seconds=120):
        with self.tx() as db:
            row = db.execute("SELECT * FROM thinking_wakeups WHERE status='QUEUED' OR (status='RUNNING' AND lease_until<?) ORDER BY id LIMIT 1", (time.time(),)).fetchone()
            if not row:
                return None
            generation = row["generation"]+1
            db.execute("UPDATE thinking_wakeups SET status='RUNNING',lease_until=?,generation=? WHERE id=?", (time.time()+seconds, generation, row["id"]))
            return {**dict(row), "generation": generation, "payload": json.loads(row["payload"])}

    def finish_wakeup(self, wid, generation, state="DONE"):
        if state not in {"DONE", "RETRY", "SUPPRESSED"}:
            raise ValueError("Invalid wakeup state")
        with self.tx() as db:
            row = db.execute("SELECT * FROM thinking_wakeups WHERE id=?", (wid,)).fetchone()
            if not row or row["generation"] != generation or row["lease_until"] < time.time():
                raise StaleLease("Wakeup lease expired")
            db.execute("UPDATE thinking_wakeups SET status=?,lease_until=? WHERE id=?", ("QUEUED" if state == "RETRY" else state, 0, wid))

    def receipt(self, sid, cid, tool, arguments):
        key = hashlib.sha256((sid+":"+cid).encode()).hexdigest()
        signature = hashlib.sha256(dump([tool, arguments]).encode()).hexdigest()
        with self.tx() as db:
            row = db.execute("SELECT * FROM agent_tool_receipts WHERE id=?", (key,)).fetchone()
            if row and row["signature"] != signature:
                raise VersionConflict("Tool ID reused with different arguments")
            if row:
                return key, json.loads(row["payload"]) if row["status"] == "DONE" else {"error": "STATE_UNKNOWN_RECONCILE"}
            db.execute("INSERT INTO agent_tool_receipts VALUES(?,?,?,?,?)", (key, sid, signature, "STARTED", "{}"))
            return key, None

    def finish_receipt(self, key, result):
        with self.tx() as db:
            receipt = db.execute("SELECT session_id FROM agent_tool_receipts WHERE id=?", (key,)).fetchone()
            if receipt and "memories" in result:
                session = db.execute("SELECT payload FROM thinking_sessions WHERE id=?", (receipt["session_id"],)).fetchone()
                if not session:
                    raise KeyError("Session erased")
                if not json.loads(session["payload"])["context"].get("collect_messages", True):
                    result = {"error": "CONTENT_NOT_RETAINED"}
            db.execute("UPDATE agent_tool_receipts SET status='DONE',payload=? WHERE id=?", (dump(result), key))

    def binding(self, tid):
        with self.tx(write=False) as db:
            row = db.execute("SELECT * FROM coding_bindings WHERE task_id=?", (tid,)).fetchone()
            return {**dict(row), "payload": json.loads(row["payload"])} if row else None

    def bind(self, tid, generation, provider, session_id, state, payload):
        with self.tx() as db:
            task = self._fenced(db, tid, generation)
            db.execute("INSERT INTO coding_bindings VALUES(?,?,?,?,?) ON CONFLICT(task_id) DO UPDATE SET session_id=excluded.session_id,state=excluded.state,payload=excluded.payload", (tid, provider, session_id, state, dump(payload)))
            self._project(db, task)

    def save_review(self, tid, report):
        with self.tx() as db:
            current = self._task(db, tid)
            if current["status"] == "CANCELLED":
                raise PermissionError("Task cancelled or erased during observation")
            rid = uuid.uuid4().hex
            db.execute("INSERT INTO coding_review_reports VALUES(?,?,?,?,?,?,?)", (rid, tid, report.get("observed_revision"), report.get("provider", "unknown"), report.get("model", "none"), dump(report), time.time()))
            self._publish(db, tid, None, "observer.review.completed", rid, {"report_id": rid, "revision": report.get("observed_revision")})
            return {"id": rid, **report}

    def reviews(self, tid):
        with self.tx(write=False) as db:
            return [{"id": r["id"], **json.loads(r["payload"])} for r in db.execute("SELECT * FROM coding_review_reports WHERE coding_task_id=? ORDER BY created_at DESC LIMIT 100", (tid,)).fetchall()]

    def pending_provider_deletions(self, provider="opencode"):
        with self.tx(write=False) as db:
            return [dict(r) for r in db.execute("SELECT task_id,session_id FROM coding_bindings WHERE state='ERASE_REQUESTED' AND provider=?", (provider,)).fetchall()]

    def provider_deleted(self, tid):
        with self.tx() as db:
            db.execute("UPDATE coding_bindings SET state='ERASED',session_id=NULL,payload='{}' WHERE task_id=? AND state='ERASE_REQUESTED'", (tid,))
            self._project(db, self._task(db, tid))

    def _reap(self, db, now):
        for row in db.execute("SELECT id FROM v2_tasks WHERE status IN ('RUNNING','VALIDATING','PAUSING','CANCEL_REQUESTED') AND lease_until<?", (now,)).fetchall():
            task = self._task(db, row["id"])
            remote = db.execute("SELECT provider FROM coding_bindings WHERE task_id=?", (task["id"],)).fetchone()
            if remote and remote["provider"] in {"opencode", "codex"} and task["status"] in {"PAUSING", "CANCEL_REQUESTED"}:
                # A lost worker is not proof that a remote atomic action stopped.
                continue
            task["status"] = {"PAUSING": "PAUSED", "CANCEL_REQUESTED": "CANCELLED"}.get(task["status"], "INTERRUPTED")
            task["version"] += 1
            task["lease_until"] = 0
            self._save(db, task)
            self._event(db, task["id"], "task.interrupted.v1", {"status": task["status"]})

    def pending_remote_controls(self, provider="opencode"):
        with self.tx(write=False) as db:
            rows = db.execute("SELECT t.id FROM v2_tasks t JOIN coding_bindings b ON b.task_id=t.id WHERE b.provider=? AND t.status IN ('PAUSING','CANCEL_REQUESTED') AND t.lease_until<?", (provider, time.time())).fetchall()
            return [self._task(db, r["id"]) for r in rows]

    def confirm_remote_stop(self, tid, expected_status):
        with self.tx() as db:
            task = self._task(db, tid)
            if task["status"] != expected_status or task["lease_until"] >= time.time():
                return False
            task["status"] = {"PAUSING": "PAUSED", "CANCEL_REQUESTED": "CANCELLED"}[expected_status]
            task["lease_until"] = 0
            task["version"] += 1
            self._save(db, task)
            db.execute("UPDATE v2_commands SET applied=1 WHERE task_id=? AND kind IN ('PAUSE','CANCEL')", (tid,))
            self._event(db, tid, "task.stopped.v1", {"status": task["status"], "remote_idle_confirmed": True})
            return True

    def _erase_v3(self, db, owner=None, tid=None, source=None):
        if tid:
            task = self._task(db, tid)
            parents = [task.get("parent_task_id")]
            db.execute("DELETE FROM coding_review_reports WHERE coding_task_id=?", (tid,))
            db.execute("DELETE FROM agent_domain_events WHERE aggregate_id=?", (tid,))
            # Retain session id so a remote worker can cancel, never resubmit.
            db.execute("UPDATE coding_bindings SET state='ERASE_REQUESTED',payload='{}' WHERE task_id=?", (tid,))
        else:
            parents = [r["id"] for r in db.execute("SELECT id FROM thinking_sessions WHERE owner_id=?", (owner,)).fetchall()] if owner else []
            if source:
                parents += [r["id"] for r in db.execute("SELECT id,payload FROM thinking_sessions").fetchall() if json.loads(r["payload"])["context"].get("source_id") == source]
        for sid in filter(None, parents):
            db.execute("DELETE FROM thinking_wakeups WHERE parent_task_id=?", (sid,))
            db.execute("DELETE FROM agent_domain_events WHERE parent_task_id=?", (sid,))
            db.execute("DELETE FROM agent_domain_events WHERE aggregate_id=?", (sid,))
            db.execute("DELETE FROM agent_tool_receipts WHERE session_id=?", (sid,))
            db.execute("DELETE FROM thinking_sessions WHERE id=?", (sid,))

    def erase(self, owner=None, source=None):
        erased = super().erase(owner, source)
        with self.tx() as db:
            for tid in erased:
                self._erase_v3(db, tid=tid)
            self._erase_v3(db, owner=owner, source=source)
        return erased

    def erase_task(self, tid):
        super().erase_task(tid)
        with self.tx() as db:
            self._erase_v3(db, tid=tid)

    def _after_archive(self, db, task):
        # A stale completion must not revive an archived card or send a reply.
        for row in db.execute("SELECT id,payload FROM thinking_wakeups WHERE status IN ('QUEUED','RUNNING')").fetchall():
            if json.loads(row['payload']).get('aggregate_id') == task['id']:
                db.execute("UPDATE thinking_wakeups SET status='SUPPRESSED',lease_until=0 WHERE id=?", (row['id'],))
