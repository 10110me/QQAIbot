"""Transactional V2 registry. PostgreSQL in production; SQLite for small/offline installs.

All mutations use one transaction lock, plus version and lease fencing. No worker
holds a DB transaction across a model/tool call. Call through asyncio.to_thread
from the Social runtime so a slow database cannot occupy its event loop.
"""
import hashlib
import json
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

from osa.domain.work import BUSY, COMMANDS, TERMINAL, StaleLease, VersionConflict, clean_text, external_question

SCHEMA = [
 "CREATE TABLE IF NOT EXISTS v2_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)",
 "CREATE TABLE IF NOT EXISTS v2_tasks (id TEXT PRIMARY KEY, owner_id TEXT NOT NULL, group_id TEXT, status TEXT NOT NULL, version INTEGER NOT NULL, generation INTEGER NOT NULL, lease_until DOUBLE PRECISION NOT NULL, payload TEXT NOT NULL, created DOUBLE PRECISION NOT NULL, delivery_pending INTEGER NOT NULL DEFAULT 0)",
 "CREATE INDEX IF NOT EXISTS v2_task_queue ON v2_tasks(status,lease_until)",
 "CREATE TABLE IF NOT EXISTS v2_commands (id TEXT PRIMARY KEY, task_id TEXT NOT NULL, actor TEXT NOT NULL, dedupe TEXT UNIQUE NOT NULL, kind TEXT NOT NULL, payload TEXT NOT NULL, applied INTEGER NOT NULL)",
 "CREATE TABLE IF NOT EXISTS v2_events (seq INTEGER PRIMARY KEY, task_id TEXT NOT NULL, kind TEXT NOT NULL, created DOUBLE PRECISION NOT NULL, payload TEXT NOT NULL)",
 "CREATE INDEX IF NOT EXISTS v2_events_task ON v2_events(task_id,seq)",
 "CREATE TABLE IF NOT EXISTS v2_checkpoints (id TEXT PRIMARY KEY, task_id TEXT NOT NULL, payload TEXT NOT NULL)",
 "CREATE TABLE IF NOT EXISTS v2_records (id TEXT PRIMARY KEY, kind TEXT NOT NULL, owner_id TEXT NOT NULL, group_id TEXT, state TEXT NOT NULL, dedupe TEXT UNIQUE NOT NULL, payload TEXT NOT NULL)",
 "CREATE INDEX IF NOT EXISTS v2_record_scope ON v2_records(kind,group_id,owner_id,state)",
 "CREATE TABLE IF NOT EXISTS v2_policy (group_id TEXT PRIMARY KEY, payload TEXT NOT NULL)",
 "CREATE TABLE IF NOT EXISTS v2_echo (group_id TEXT PRIMARY KEY, until_at DOUBLE PRECISION NOT NULL, detected DOUBLE PRECISION NOT NULL, event_id TEXT NOT NULL)",
]


def dump(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


class Cursor:
    def __init__(self, connection, pg):
        self.connection, self.pg = connection, pg

    def execute(self, sql, args=()):
        return self.connection.execute(sql.replace("?", "%s") if self.pg else sql, args)


class Registry:
    def __init__(self, location: str):
        self.location = location
        self.pg = location.startswith(("postgresql://", "postgres://"))
        self.lock = threading.RLock()
        self.memory = None
        self._wal_initialized = False
        if not self.pg:
            if location == ":memory:":
                self.memory = sqlite3.connect(":memory:", check_same_thread=False, isolation_level=None)
                self.memory.row_factory = sqlite3.Row
            else:
                Path(location).parent.mkdir(parents=True, exist_ok=True)
        with self.tx() as db:
            for statement in SCHEMA:
                db.execute(statement)
            db.execute("INSERT INTO v2_meta VALUES('schema_version','1') ON CONFLICT(key) DO NOTHING")

    @contextmanager
    def tx(self, write=True):
        with self.lock:
            if self.pg:
                import psycopg
                from psycopg.rows import dict_row
                connection = psycopg.connect(self.location, row_factory=dict_row, connect_timeout=5)
            else:
                connection = self.memory or sqlite3.connect(self.location, timeout=30, isolation_level=None)
            try:
                if self.pg:
                    if write:
                        connection.execute("SELECT pg_advisory_xact_lock(7312030)")
                else:
                    connection.row_factory = sqlite3.Row
                    connection.execute("PRAGMA busy_timeout=30000")
                    if not self._wal_initialized:
                        connection.execute("PRAGMA journal_mode=WAL")
                        self._wal_initialized = True
                    connection.execute("PRAGMA secure_delete=ON")
                    connection.execute("PRAGMA query_only=" + ("OFF" if write else "ON"))
                    connection.execute("BEGIN IMMEDIATE" if write else "BEGIN")
                yield Cursor(connection, self.pg)
                connection.commit()
            except BaseException:
                connection.rollback()
                raise
            finally:
                if connection is not self.memory:
                    connection.close()

    def _task(self, db, task_id):
        row = db.execute("SELECT * FROM v2_tasks WHERE id=?", (task_id,)).fetchone()
        if row is None:
            raise KeyError(task_id)
        value = json.loads(row["payload"])
        return {**value, "id": row["id"], "owner_id": row["owner_id"], "group_id": row["group_id"],
                "status": row["status"], "version": row["version"], "generation": row["generation"], "lease_until": row["lease_until"]}

    def _save(self, db, task):
        db.execute("UPDATE v2_tasks SET status=?,version=?,generation=?,lease_until=?,payload=? WHERE id=?",
                   (task["status"], task["version"], task["generation"], task["lease_until"], dump(task), task["id"]))

    def _event(self, db, task_id, kind, payload):
        seq = db.execute("SELECT COALESCE(MAX(seq),0)+1 AS n FROM v2_events").fetchone()["n"]
        db.execute("INSERT INTO v2_events VALUES(?,?,?,?,?)", (seq, task_id, kind, time.time(), dump(payload)))
        return seq

    def create(self, owner, group, requirement, dedupe, source_event=None):
        with self.tx() as db:
            old = db.execute("SELECT id FROM v2_records WHERE dedupe=?", ("create:"+dedupe,)).fetchone()
            if old:
                existing=self._task(db,old["id"])
                if existing["owner_id"]!=owner or existing["group_id"]!=group or existing["requirement"]!=clean_text(requirement,8000):raise VersionConflict("Task creation key reused")
                return existing
            task_id = uuid.uuid4().hex
            task = dict(id=task_id, owner_id=owner, group_id=group, requirement=clean_text(requirement, 8000),
                status="WAITING_APPROVAL", version=1, generation=0, lease_until=0, cursor=0, plan_version=1,
                milestones=[], tests_passed=0, tests_failed=0, created_at=time.time(), last_progress_at=time.time(),
                current_step="等待管理员审批", blocker=None, requirement_log=[], source_event=source_event, workspace_id=uuid.uuid4().hex)
            db.execute("INSERT INTO v2_tasks VALUES(?,?,?,?,?,?,?,?,?,?)", (task_id,owner,group,task["status"],1,0,0,dump(task),task["created_at"],0))
            db.execute("INSERT INTO v2_records VALUES(?,?,?,?,?,?,?)", (task_id,"task_dedupe",owner,group,"active","create:"+dedupe,"{}"))
            self._event(db,task_id,"task.created.v1",{"status":task["status"]})
            return task

    def get(self, task_id):
        with self.tx(write=False) as db:
            return self._task(db,task_id)

    def archive_task(self, task_id, expected_version=None):
        """Recoverably remove an inactive card without erasing its project."""
        with self.tx() as db:
            task = self._task(db, task_id)
            if task.get('archived_at'):
                return {'id': task_id, 'archived': True}
            if expected_version is not None and task['version'] != expected_version:
                raise VersionConflict('任务已更新，请刷新后重试')
            if task['status'] in {'RUNNING','VALIDATING','PAUSING','CANCEL_REQUESTED','QUEUED'}:
                raise ValueError('请先取消正在执行或排队的任务，再从列表移除')
            task['archived_at'] = time.time()
            task['version'] += 1
            self._save(db, task)
            db.execute('UPDATE v2_tasks SET delivery_pending=0 WHERE id=?', (task_id,))
            self._after_archive(db, task)
            return {'id': task_id, 'archived': True}

    def _after_archive(self, db, task):
        pass

    def reconcile_legacy_failures(self):
        """Correct old exception-shaped WAITING_INPUT records; never rerun them."""
        changed = 0
        with self.tx() as db:
            for row in db.execute("SELECT id FROM v2_tasks WHERE status='WAITING_INPUT'").fetchall():
                task = self._task(db, row['id'])
                blocker = task.get('blocker') or ''
                if not blocker.startswith(('OperationalError:', 'HTTPStatusError:', 'TimeoutError:',
                                           'ValueError:', 'ProviderStateUnknown:')):
                    continue
                kind = 'infrastructure' if blocker.startswith(('OperationalError:', 'HTTPStatusError:', 'TimeoutError:')) else 'provider'
                if 'tests' in blocker or 'unittest' in blocker:
                    kind = 'validation'
                task.update(status='FAILED', failure_kind=kind, version=task['version']+1,
                            current_step='工作服务异常，执行未完成' if kind=='infrastructure' else '工作执行失败，请查看详情')
                self._save(db, task)
                changed += 1
        return changed

    def tasks(self, owner=None, group=None, admin=False, include_archived=False):
        with self.tx(write=False) as db:
            if admin:
                rows=db.execute("SELECT id FROM v2_tasks ORDER BY created DESC LIMIT 200").fetchall()
            elif group is not None:
                rows=db.execute("SELECT id FROM v2_tasks WHERE owner_id=? AND group_id=? LIMIT 200",(owner,group)).fetchall()
            else:
                rows=db.execute("SELECT id FROM v2_tasks WHERE owner_id=? LIMIT 200",(owner,)).fetchall()
            tasks = [self._task(db,r["id"]) for r in rows]
            return sorted([t for t in tasks if include_archived or not t.get('archived_at')],key=lambda t:t["created_at"],reverse=True)

    def visible(self, task_id, actor, group, admin=False):
        task=self.get(task_id)
        if not admin and task["owner_id"]!=actor:
            raise PermissionError("Task owner mismatch")
        if not admin and task["group_id"]!=group:
            raise PermissionError("请在任务原会话或后台查看，当前群无权披露工作内容")
        return self.public(task)

    def public(self, task):
        hidden={"files","requirement","requirement_log","model_calls","source_event"}
        value={k:v for k,v in task.items() if k not in hidden}
        value["stale"]=task["status"] in {"RUNNING","VALIDATING"} and time.time()-task["last_progress_at"]>120
        done=sum(m.get("state")=="done" for m in task["milestones"])
        value.update(milestones_done=done,milestones_total=len(task["milestones"]),estimate=None,
                     percent=round(done/len(task["milestones"])*100) if task["milestones"] else None)
        return value

    def command(self, task_id, actor, group, kind, payload, expected, dedupe, admin=False):
        if kind not in COMMANDS:
            raise ValueError("Unknown task command")
        with self.tx() as db:
            task=self._task(db,task_id)
            if task.get('archived_at'):
                raise ValueError('任务已从列表移除')
            if not admin and (task["owner_id"]!=actor or task["group_id"]!=group):
                raise PermissionError("Task control scope denied")
            if kind in {"APPROVE","REJECT"} and not admin:
                raise PermissionError("Task approval requires owner console")
            old=db.execute("SELECT * FROM v2_commands WHERE dedupe=?",(dedupe,)).fetchone()
            if old:
                if old["task_id"]!=task_id or old["actor"]!=actor or old["kind"]!=kind or json.loads(old["payload"])!=payload:
                    raise VersionConflict("Idempotency key reused for a different command")
                return {"command_id":old["id"],"applied":bool(old["applied"]),"version":task["version"]}
            if task["version"]!=expected:
                raise VersionConflict("任务已更新，请刷新版本后重试")
            retryable_failure = task['status']=='FAILED' and (task.get('failure_kind') in {'infrastructure','provider','validation'} or
                task.get('current_step')=='模型服务失败，任务未完成')
            # A completed project can receive a new requirement.  Reopen the
            # same task/workspace so the worker sees its existing files and
            # applies the requested change instead of generating a new project.
            reopen_completed = task["status"] == "COMPLETED" and kind == "ADD_REQUIREMENT"
            if task["status"] in TERMINAL and not (kind=='RESUME' and retryable_failure) and not reopen_completed:
                raise ValueError("Task is terminal")
            if kind in {"APPROVE","REJECT"}:
                if task["status"]!="WAITING_APPROVAL": raise ValueError("Task is not awaiting approval")
                task["status"]="QUEUED" if kind=="APPROVE" else "DENIED"
                task['current_step']='等待工作进程执行' if kind=='APPROVE' else '任务已拒绝'
            elif kind=="ADD_REQUIREMENT":
                if not isinstance(payload.get("text"),str) or not 1<=len(payload["text"])<=8000:
                    raise ValueError("Requirement missing or too long")
                if reopen_completed:
                    task["status"] = "QUEUED"
                    task["blocker"] = None
                    task["lease_until"] = 0
                    task.pop("artifact_id", None)
            elif kind=="PAUSE":
                if task["status"] not in {"QUEUED","RUNNING","VALIDATING","WAITING_INPUT","INTERRUPTED"}:raise ValueError("Task cannot pause in this state")
                task["status"]="PAUSING" if task["status"] in {"RUNNING","VALIDATING"} else "PAUSED"
            elif kind=="RESUME":
                if task["status"] not in {"PAUSED","WAITING_INPUT","INTERRUPTED"} and not retryable_failure:
                    raise ValueError("Task is not resumable")
                task["status"]="QUEUED"
                task["current_step"]="等待工作进程重试"
                if task.get('failure_kind') != 'validation':task["blocker"]=None
                task.pop('failure_kind',None)
            elif kind=="CANCEL":
                task["status"]="CANCEL_REQUESTED" if task["status"] in {"RUNNING","VALIDATING","PAUSING"} else "CANCELLED"
                task['current_step']='正在停止工作任务' if task['status']=='CANCEL_REQUESTED' else '任务已取消'
            cid=uuid.uuid4().hex
            applied=kind!="ADD_REQUIREMENT" and task["status"] not in {"PAUSING","CANCEL_REQUESTED"}
            db.execute("INSERT INTO v2_commands VALUES(?,?,?,?,?,?,?)",(cid,task_id,actor,dedupe,kind,dump(payload),int(applied)))
            task["version"]+=1
            self._save(db,task);self._event(db,task_id,"task.command.requested.v1",{"command_id":cid,"kind":kind,"applied":applied})
            return {"command_id":cid,"applied":applied,"version":task["version"]}

    def claim(self, worker, seconds=120):
        with self.tx() as db:
            stop=db.execute("SELECT value FROM v2_meta WHERE key='work_kill'").fetchone()
            if stop and stop["value"]=="1":return None
            now=time.time()
            self._reap(db,now)
            rows=db.execute("SELECT id FROM v2_tasks WHERE status IN ('QUEUED','INTERRUPTED') ORDER BY created").fetchall()
            row=next((r for r in rows if not self._task(db,r['id']).get('archived_at')),None)
            if not row:return None
            task=self._task(db,row["id"])
            task.update(status="RUNNING",generation=task["generation"]+1,lease_until=now+seconds,worker=worker,last_progress_at=now,current_step="恢复工作检查点" if task.get("cursor",0) else "准备制定工作计划")
            task["version"]+=1;self._save(db,task)
            self._event(db,task["id"],"task.lease.acquired.v1",{"generation":task["generation"]})
            return task

    def _fenced(self,db,tid,generation):
        task=self._task(db,tid)
        if task["generation"]!=generation or task["lease_until"]<time.time() or task["status"] in TERMINAL:
            raise StaleLease("Worker lease expired or replaced")
        return task

    def heartbeat(self, tid,generation,seconds=120):
        with self.tx() as db:
            task=self._fenced(db,tid,generation);task["lease_until"]=time.time()+seconds
            self._save(db,task);return task["status"]

    def boundary(self, tid,generation):
        with self.tx() as db:
            task=self._fenced(db,tid,generation)
            if task["status"] in {"PAUSING","CANCEL_REQUESTED"}:
                task["status"]="PAUSED" if task["status"]=="PAUSING" else "CANCELLED"
                task["lease_until"]=0;task["version"]+=1;self._save(db,task)
                db.execute("UPDATE v2_commands SET applied=1 WHERE task_id=? AND kind IN ('PAUSE','CANCEL')",(tid,))
                self._event(db,tid,"task.stopped.v1",{"status":task["status"]});return task
            commands=db.execute("SELECT * FROM v2_commands WHERE task_id=? AND applied=0 AND kind='ADD_REQUIREMENT' ORDER BY id",(tid,)).fetchall()
            # UUID lexical order is not submission order. The transactional
            # event sequence is the durable ordering authority.
            ordering = {json.loads(row['payload']).get('command_id'): row['seq'] for row in db.execute(
                "SELECT seq,payload FROM v2_events WHERE task_id=? AND kind='task.command.requested.v1' ORDER BY seq", (tid,)).fetchall()}
            commands = sorted(commands, key=lambda row: ordering.get(row['id'], 0))
            for command in commands:
                task["requirement_log"].append(clean_text(json.loads(command["payload"])["text"],8000))
                task["plan_version"]+=1
                # All source/test milestones must be revalidated after requirements change.
                task["cursor"]=0;task["milestones"]=[];task["tests_passed"]=0;task["tests_failed"]=0;task.pop("artifact_id",None)
                db.execute("UPDATE v2_commands SET applied=1 WHERE id=?",(command["id"],))
                self._event(db,tid,"task.command.applied.v1",{"command_id":command["id"],"plan_version":task["plan_version"]})
            if commands:task["version"]+=1;self._save(db,task)
            return task

    def checkpoint(self,tid,generation,step,patch,evidence):
        with self.tx() as db:
            task=self._fenced(db,tid,generation)
            if task["status"] not in {"RUNNING","VALIDATING","PAUSING","CANCEL_REQUESTED"}:raise StaleLease("Inactive task")
            if step == 'model_request' and task['status'] == 'VALIDATING':
                task['status'] = 'RUNNING'
            for key in patch:
                if key not in {"files","milestones","cursor","current_step","tests_passed","tests_failed","blocker","manifest_hash","git_commit","artifact_id","test_plan_version","failure_kind","agent_activity","execution_result","output_artifacts"}:raise ValueError("Invalid checkpoint field")
            task.update(patch);task["last_progress_at"]=time.time();task["version"]+=1
            cid=f"{tid}-{generation}-{task['version']}"
            db.execute("INSERT INTO v2_checkpoints VALUES(?,?,?)",(cid,tid,dump({"step":step,"evidence":evidence,"plan_version":task["plan_version"],"generation":generation,"created_at":time.time(),"manifest_hash":task.get("manifest_hash"),"git_commit":task.get("git_commit")})))
            self._save(db,task);self._event(db,tid,"task.progress.updated.v1",{"checkpoint":cid,"step":step,"evidence":evidence})
            return task

    def start_validation(self, tid, generation):
        with self.tx() as db:
            task = self._fenced(db, tid, generation)
            if task['status'] == 'RUNNING':
                task.update(status='VALIDATING', version=task['version']+1)
                self._save(db, task)
            return task

    def finish(self,tid,generation,state,blocker=None,failure_kind=None):
        if state not in {"COMPLETED","FAILED","WAITING_INPUT","PAUSED"}:raise ValueError("Invalid finish state")
        with self.tx() as db:
            task=self._fenced(db,tid,generation)
            pending=db.execute("SELECT id FROM v2_commands WHERE task_id=? AND applied=0 AND kind='ADD_REQUIREMENT'",(tid,)).fetchone()
            if pending or task["status"] in {"PAUSING","CANCEL_REQUESTED"}:return False
            if state=="COMPLETED" and (task["tests_passed"]<1 or task["tests_failed"] or not task.get("artifact_id")):
                raise ValueError("Completion requires passing tests and a published artifact")
            task.update(status=state,blocker=clean_text(blocker or "",2000) or None,lease_until=0,last_progress_at=time.time())
            if failure_kind:
                task['failure_kind'] = failure_kind
                task['current_step'] = {'infrastructure':'工作服务异常，执行未完成',
                    'provider':'工作模型执行失败', 'validation':'项目验证失败'}.get(failure_kind,'工作执行失败')
            task["version"]+=1;self._save(db,task)
            if state=="COMPLETED" and task.get("source_event"):db.execute("UPDATE v2_tasks SET delivery_pending=1 WHERE id=?",(tid,))
            self._event(db,tid,"task.finished.v1",{"status":state,"blocker":task["blocker"]})
            return True

    def events(self,tid,after=0):
        with self.tx(write=False) as db:
            return [{**dict(r),"payload":json.loads(r["payload"])} for r in db.execute("SELECT * FROM v2_events WHERE task_id=? AND seq>? ORDER BY seq LIMIT 200",(tid,after)).fetchall()]

    def presence(self):
        with self.tx(write=False) as db:
            rows=db.execute("SELECT status,payload FROM v2_tasks").fetchall()
            counts={}
            for row in rows:
                if json.loads(row['payload']).get('archived_at'):continue
                counts[row['status']]=counts.get(row['status'],0)+1
            return {"state":"WORKING" if any(counts.get(s,0) for s in BUSY) else "FREE","counts":counts}

    def record(self,kind,owner,group,payload,dedupe,state="proposed"):
        with self.tx() as db:
            old=db.execute("SELECT id FROM v2_records WHERE dedupe=?",(dedupe,)).fetchone()
            if old:return old["id"]
            rid=uuid.uuid4().hex
            db.execute("INSERT INTO v2_records VALUES(?,?,?,?,?,?,?)",(rid,kind,owner,group,state,dedupe,dump(payload)))
            return rid

    def records(self,kind,owner=None,group=None,admin=False):
        with self.tx(write=False) as db:
            sql="SELECT * FROM v2_records WHERE kind=?";args=[kind]
            if not admin:
                if group is not None:sql+=" AND group_id=?";args.append(group)
                else:sql+=" AND group_id IS NULL AND owner_id=?";args.append(owner)
            rows=db.execute(sql+" ORDER BY id LIMIT 500",tuple(args)).fetchall()
            return [{**dict(r),"payload":json.loads(r["payload"])} for r in rows]

    def update_record(self,rid,state,patch):
        with self.tx() as db:
            row=db.execute("SELECT * FROM v2_records WHERE id=?",(rid,)).fetchone()
            if not row:raise KeyError(rid)
            if row["state"]=="REVOKED":return False
            db.execute("UPDATE v2_records SET state=?,payload=? WHERE id=?",(state,dump({**json.loads(row["payload"]),**patch}),rid))

    def feedback(self,rid,actor,group,worked,source):
        with self.tx() as db:
            row=db.execute("SELECT * FROM v2_records WHERE id=? AND kind='experience'",(rid,)).fetchone()
            if not row:raise KeyError(rid)
            if row["owner_id"]!=actor or row["group_id"]!=group:raise PermissionError("Case scope denied")
            payload=json.loads(row["payload"])
            payload.update(feedback={"actor":actor,"worked":worked,"source":source,"at":time.time()})
            payload["source_ids"]=list(dict.fromkeys(payload.get("source_ids",[])+[source]))
            db.execute("UPDATE v2_records SET state=?,payload=? WHERE id=?",(("confirmed_by_user" if payload.get("proposed_solution") or payload.get("resolution") else "resolved_unspecified") if worked else "disputed",dump(payload),rid))
            if not worked:db.execute("DELETE FROM v2_records WHERE dedupe=?",("derivative:"+rid,))

    def review(self,rid,actor,state,note,shareable_text=None,public_question=None,solution=None):
        if state not in {"share","disputed","outdated","revoked","proposed"}:raise ValueError("Evidence cannot be invented by review")
        with self.tx() as db:
            row=db.execute("SELECT * FROM v2_records WHERE id=?",(rid,)).fetchone()
            if not row:raise KeyError(rid)
            if row["kind"]!="experience":raise ValueError("Not an experience record")
            if state=="share":
                if row["state"] not in {"verified_local","confirmed_by_user"}:raise PermissionError("No verified case to share")
                state=row["state"]
            payload=json.loads(row["payload"])
            payload.update(review={"actor":actor,"note":clean_text(note,2000),"at":time.time()})
            if public_question:payload["public_question"]=external_question(public_question)
            if solution:payload["proposed_solution"]=clean_text(solution,3000)
            db.execute("UPDATE v2_records SET state=?,payload=? WHERE id=?",(state,dump(payload),rid))
            db.execute("DELETE FROM v2_records WHERE dedupe=?",("derivative:"+rid,))
            if state in {"revoked","disputed"}:
                for job in db.execute("SELECT * FROM v2_records WHERE kind='night_research'").fetchall():
                    if json.loads(job['payload']).get('case_id')==rid:
                        db.execute('DELETE FROM v2_records WHERE dedupe LIKE ?',('derivative:'+job['id']+'%',))
                        db.execute("UPDATE v2_records SET state='REVOKED' WHERE id=?",(job['id'],))
            if shareable_text:
                if state not in {"verified_local","confirmed_by_user"}:raise PermissionError("Unconfirmed experience cannot be shared")
                text=external_question(shareable_text)
                db.execute("INSERT INTO v2_records VALUES(?,?,?,?,?,?,?)",(uuid.uuid4().hex,"public_experience","public",None,state,"derivative:"+rid,dump({"summary":text,"evidence_level":state,"reviewed_at":time.time()})))

    def echo(self,group,event_id,detected,ttl):
        now=time.time()
        if detected>now+5 or now-detected>60:raise ValueError("Echo event is stale")
        with self.tx() as db:
            old=db.execute("SELECT * FROM v2_echo WHERE group_id=?",(group,)).fetchone()
            if old and old["detected"]>=detected:return
            db.execute("INSERT INTO v2_echo VALUES(?,?,?,?) ON CONFLICT(group_id) DO UPDATE SET until_at=excluded.until_at,detected=excluded.detected,event_id=excluded.event_id",
                (group,min(now+60,detected+max(0,min(ttl,40))+5),detected,event_id))

    def echo_active(self,group):
        with self.tx(write=False) as db:
            row=db.execute("SELECT until_at FROM v2_echo WHERE group_id=?",(group,)).fetchone()
            return bool(row and row["until_at"]>time.time())

    def budget(self,key,limit):
        with self.tx() as db:
            full="budget:"+key
            row=db.execute("SELECT value FROM v2_meta WHERE key=?",(full,)).fetchone()
            used=int(row["value"]) if row else 0
            if limit > 0 and used>=limit:return False
            db.execute("INSERT INTO v2_meta VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",(full,str(used+1)))
            return True

    def erase(self,owner=None,source=None):
        with self.tx() as db:
            rows=db.execute("SELECT * FROM v2_records"+(" WHERE owner_id=?" if owner else ""),(owner,) if owner else ()).fetchall()
            for row in rows:
                if source and source not in json.loads(row["payload"]).get("source_ids",[]):continue
                if row["kind"]=="task_dedupe":continue
                db.execute("DELETE FROM v2_records WHERE dedupe LIKE ?",("derivative:"+row["id"]+"%",))
                db.execute("DELETE FROM v2_records WHERE id=?",(row["id"],))
            erased=[]
            if owner:
                for row in db.execute("SELECT id FROM v2_tasks WHERE owner_id=?",(owner,)).fetchall():
                    task=self._task(db,row['id']);task.update(status='CANCELLED',lease_until=0,requirement='',requirement_log=[],files={},milestones=[],blocker=None,source_event=None)
                    task.pop('artifact_id',None);task['generation']+=1;task['version']+=1
                    self._save(db,task)
                    db.execute("DELETE FROM v2_events WHERE task_id=?",(row['id'],))
                    db.execute("DELETE FROM v2_checkpoints WHERE task_id=?",(row['id'],))
                    db.execute("DELETE FROM v2_commands WHERE task_id=?",(row['id'],))
                    db.execute('UPDATE v2_tasks SET delivery_pending=0 WHERE id=?',(row['id'],))
                    erased.append(row['id'])
            return erased

    def set_kill(self,enabled):
        with self.tx() as db:
            db.execute("INSERT INTO v2_meta VALUES('work_kill',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",('1' if enabled else '0',))

    def set_policy(self,group,policy):
        with self.tx() as db:
            db.execute("INSERT INTO v2_policy VALUES(?,?) ON CONFLICT(group_id) DO UPDATE SET payload=excluded.payload",(group,dump(policy)))
            if not policy.get('enabled') or not policy.get('sandbox'):
                for row in db.execute("SELECT id FROM v2_tasks WHERE group_id=?",(group,)).fetchall():
                    task=self._task(db,row['id'])
                    if task['status'] in TERMINAL:continue
                    task['status']='CANCEL_REQUESTED' if task['status'] in {'RUNNING','VALIDATING','PAUSING'} else 'CANCELLED'
                    task['version']+=1;self._save(db,task)

    def policy(self,group):
        with self.tx(write=False) as db:
            row=db.execute("SELECT payload FROM v2_policy WHERE group_id=?",(group,)).fetchone()
            return json.loads(row['payload']) if row else {}

    def valid_experience(self,refs,actor,group):
        with self.tx(write=False) as db:
            for ref in refs:
                row=db.execute("SELECT * FROM v2_records WHERE id=?",(ref['id'],)).fetchone()
                if not row or row['state']!=ref['state'] or hashlib.sha256(row['payload'].encode()).hexdigest()!=ref['hash']:return False
                if row['kind'] not in {'public_experience','public_claim'} and not ((group is not None and row['group_id']==group) or (group is None and row['group_id'] is None and row['owner_id']==actor)):return False
            return True

    def deliveries(self):
        with self.tx(write=False) as db:
            return [self._task(db,r['id']) for r in db.execute("SELECT id FROM v2_tasks WHERE delivery_pending=1 ORDER BY created LIMIT 20").fetchall()]

    def delivered(self,tid):
        with self.tx() as db:
            db.execute("UPDATE v2_tasks SET delivery_pending=0 WHERE id=?",(tid,))

    def erase_task(self,tid):
        with self.tx() as db:
            task=self._task(db,tid)
            task.update(status='CANCELLED',lease_until=0,requirement='',requirement_log=[],files={},milestones=[],blocker=None,source_event=None)
            task.pop('artifact_id',None);task['generation']+=1;task['version']+=1
            self._save(db,task)
            db.execute('UPDATE v2_tasks SET delivery_pending=0 WHERE id=?',(tid,))
            for table in ['v2_events','v2_checkpoints','v2_commands']:
                db.execute('DELETE FROM '+table+' WHERE task_id=?',(tid,))

    def revoke_derivatives(self,owner):
        with self.tx() as db:
            for row in db.execute("SELECT id FROM v2_records WHERE owner_id=? AND kind='experience'",(owner,)).fetchall():
                db.execute('DELETE FROM v2_records WHERE dedupe=?',('derivative:'+row['id'],))

    def review_research(self,rid,index,actor,source_hash,quote,note,publish):
        with self.tx() as db:
            row=db.execute("SELECT * FROM v2_records WHERE id=? AND kind='night_research'",(rid,)).fetchone()
            if not row or row['state'] not in {'NEEDS_REVIEW','REVIEWED'}:raise KeyError(rid)
            payload=json.loads(row['payload']);claims=payload.get('claims',[])
            if index<0 or index>=len(claims):raise KeyError('claim')
            source=next((s for s in payload.get('sources',[]) if s.get('sha256')==source_hash),None)
            if not source or quote not in source.get('excerpt',''):raise ValueError('Quote not in fetched source')
            text=external_question(claims[index]['text']) if publish else None
            review={'actor':actor,'note':clean_text(note,1000),'quote':quote,'at':time.time()}
            claims[index].setdefault('reviews',[]).append(review)
            claims[index].update(status='SUPPORTED_WITH_LIMITATIONS',supporting_source_ids=[source_hash])
            payload.update(claims=claims,verified_claims=sum(c['status']=='SUPPORTED_WITH_LIMITATIONS' for c in claims))
            db.execute("UPDATE v2_records SET state='REVIEWED',payload=? WHERE id=?",(dump(payload),rid))
            if text:
                db.execute("INSERT INTO v2_records VALUES(?,?,?,?,?,?,?) ON CONFLICT(dedupe) DO UPDATE SET payload=excluded.payload,state=excluded.state",(uuid.uuid4().hex,'public_claim','public',None,'supported_with_limitations','derivative:'+rid+':'+str(index),dump({'summary':text,'source_url':source['url'],'content_hash':source_hash,'reviewed_at':time.time(),'evidence_level':'human_source_review'})))
            return {'reviewed':True,'published':publish,'status':'SUPPORTED_WITH_LIMITATIONS'}

    def _reap(self,db,now):
        for r in db.execute("SELECT id FROM v2_tasks WHERE status IN ('RUNNING','VALIDATING','PAUSING','CANCEL_REQUESTED') AND lease_until<?",(now,)).fetchall():
            task=self._task(db,r["id"])
            task["status"]={"PAUSING":"PAUSED","CANCEL_REQUESTED":"CANCELLED"}.get(task["status"],"INTERRUPTED")
            task["version"]+=1;task["lease_until"]=0
            self._save(db,task);self._event(db,task["id"],"task.interrupted.v1",{"status":task["status"]})

    def reap(self):
        with self.tx() as db:self._reap(db,time.time())
