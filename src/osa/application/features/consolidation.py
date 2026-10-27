from osa.application.prompt_catalog import CATALOG
import hashlib
import json
import time
import uuid

from osa.domain.entities import Destination, Memory, Scope
from osa.domain.work import clean_text
from osa.domain.features import FeatureRepository, Record
from osa.domain.ports import ModelProvider, Store


class ConsolidationService:
    def __init__(self, repo: FeatureRepository, store: Store, model: ModelProvider) -> None:
        self.repo, self.store, self.model = repo, store, model
        self.v2 = None

    async def run(self, job: Record) -> dict:
        group = job.group_id
        policy = await self.store.policy(Destination("group", group))
        if not policy.collect_messages or not policy.memory_extract:
            raise PermissionError("Automatic memory extraction is not authorized")
        events = await self.store.day_events(group, job.payload["date"], policy.timezone)
        event_map = {event.id: event for event in events}
        count = 0
        for start in range(0, len(events), 40):
            batch = events[start:start+40]
            result = await self.model.generate([
                {"role": "system", "content": CATALOG['memory']['default']},
                {"role": "user", "content": json.dumps([{"id": e.id, "actor": e.actor_id, "text": e.text, "time": e.occurred_at} for e in batch], ensure_ascii=False)},
            ], "memory")
            value = json.loads(result.text.removeprefix("```json").removesuffix("```").strip())
            if not isinstance(value, dict) or not isinstance(value.get("facts"), list):
                raise ValueError("Invalid memory extraction schema")
            source_ids = [event.id for event in batch]
            episode = await self.repo.put(Record(hashlib.sha256(f"{job.id}:{start}".encode()).hexdigest()[:32], "episode", None,
                group, "active", {"date": job.payload["date"], "summary": str(value.get("summary", ""))[:3000], "source_ids": source_ids}, f"episode:{job.id}:{start}"))
            for fact in value["facts"][:40]:
                actor, content, sources = str(fact.get("subject_id", "")), fact.get("content"), fact.get("source_ids")
                if not isinstance(content, str) or not content.strip() or len(content) > 2000 or not isinstance(sources, list) or not sources:
                    continue
                if clean_text(content) != content:continue
                if not isinstance(fact.get("confidence"), (int, float)) or fact["confidence"] < 0.85:
                    continue
                if any(sid not in event_map or event_map[sid].actor_id != actor for sid in sources):
                    continue
                if any([not await self.store.event_active(sid) for sid in sources]):
                    continue
                memory_id = hashlib.sha256(f"auto:{group}:{actor}:{content}".encode()).hexdigest()[:32]
                saved = await self.store.add_memory(Memory(memory_id, content, Scope.LONG_GROUP, actor, group, sources[0]))
                if saved.id == memory_id:
                    await self.repo.put(Record(memory_id, "memory_evidence", actor, group, "active",
                        {"memory_id": memory_id, "source_ids": sources, "episode_id": episode.id,
                         "confidence": fact["confidence"], "origin": "explicit_self_statement"}, "memory:" + memory_id))
                    count += 1
        if self.v2:
            await self.v2.nightly(group, job.payload["date"], policy)
        return {"episodes": (len(events)+39)//40, "facts": count, "source_count": len(events), "completed_at": time.time()}
