from osa.application.prompt_catalog import CATALOG
"""Observer sees only a copied, hash-checked registry snapshot, never a live path."""
import asyncio
import hashlib
import json
import time
from osa.domain.work import clean_text, validate_files


class SnapshotObserver:
    def __init__(self, registry, model=None):
        self.registry, self.model = registry, model

    async def inspect(self, task_id, mode, revision=None):
        if mode not in {"STATUS", "INSPECT", "REVIEW"}:
            raise ValueError("Unsupported observer mode")
        task = await asyncio.to_thread(self.registry.get, task_id)
        if mode == "STATUS":
            return {"task_id": task_id, "mode": mode, "progress": self.registry.public(task), "readonly": True}
        if not task.get("git_commit") or (revision and revision != task["git_commit"]):
            raise ValueError("Requested fixed checkpoint is unavailable")
        files = validate_files(task.get("files", {}))
        manifest = hashlib.sha256(json.dumps(files, sort_keys=True).encode()).hexdigest()
        if manifest != task.get("manifest_hash"):
            raise ValueError("Checkpoint manifest does not match")
        report = {"task_id": task_id, "mode": mode, "observed_revision": task["git_commit"], "manifest_hash": manifest,
                  "observed_at": time.time(), "readonly": True, "findings": [], "provider": "snapshot", "model": "none",
                  "test_evidence": {"passed": task["tests_passed"], "failed": task["tests_failed"]},
                  "limitations": ["仅覆盖注册表中该 Git 检查点对应的源码快照；未读取活跃目录或未提交修改", "未执行代码，审查意见不等于实际测试通过"], "status": "snapshot_only"}
        if self.model and getattr(self.model, "available", True):
            result = await self.model.generate([
                {"role": "system", "content": CATALOG['observer']['default']},
                {"role": "user", "content": json.dumps({"revision": task["git_commit"], "files": {n: clean_text(c, 100000) for n, c in files.items()}, "test_evidence": report["test_evidence"]}, ensure_ascii=False)},
            ], "observer")
            findings = json.loads(result.text).get("findings")
            if not isinstance(findings, list) or len(findings) > 40:
                raise ValueError("Invalid observer report")
            for f in findings:
                if not isinstance(f, dict) or f.get("file") not in files or f.get("severity") not in {"low", "medium", "high", "critical"} or type(f.get("line")) is not int or not 1 <= f["line"] <= max(1, len(files[f["file"]].splitlines())):
                    raise ValueError("Invalid finding reference")
                report["findings"].append({"severity": f["severity"], "file": f["file"], "line": f["line"], "issue": clean_text(str(f.get("issue", "")), 2000), "evidence_ids": [manifest]})
            report.update(provider=result.provider, model=result.model, status="reviewed")
        return await asyncio.to_thread(self.registry.save_review, task_id, report)
