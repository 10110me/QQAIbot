import hashlib
import logging
import uuid

from osa.domain.features import Embedder, FeatureRepository, Record
from osa.domain.text import chunks

logger = logging.getLogger(__name__)


class KnowledgeService:
    def __init__(self, repo: FeatureRepository, embedder: Embedder | None) -> None:
        self.repo, self.embedder = repo, embedder

    async def ingest(self, title: str, pages: list[dict], *, group: str | None,
                     owner: str | None, source: dict, public: bool = False,
                     document_id: str | None = None) -> Record:
        if public and not (source.get("license") and source.get("verified")):
            raise PermissionError("Public knowledge needs verified provenance and a license")
        content_hash = hashlib.sha256(str(pages).encode()).hexdigest()
        document = await self.repo.put(Record(document_id or uuid.uuid4().hex, "document", owner, group, "active",
            {"title": title, "source": source, "content_hash": content_hash, "public": public,
             "verified": public, "version": 1}, f"doc:{group}:{owner}:{content_hash}"))
        pieces = chunks(pages)
        for start in range(0, len(pieces), 24):
            batch = pieces[start:start+24]
            vectors = await self.embedder.embed([p["text"] for p in batch]) if self.embedder else None
            for i, piece in enumerate(batch):
                ordinal = start + i
                record_id = hashlib.sha256(f"{document.id}:{ordinal}".encode()).hexdigest()[:32]
                row = await self.repo.put(Record(record_id, "public_chunk" if public else "chunk", owner, group, "active",
                    {**piece, "document_id": document.id, "title": title, "public": public,
                     "verified": public, "ordinal": ordinal, "source": source}, f"chunk:{document.id}:{ordinal}"))
                if vectors:
                    await self.repo.vector_put(row.id, self.embedder.model_id, vectors[i])
        return document

    async def retrieve(self, query: str, group: str | None, actor: str, limit: int = 6, *, use_embeddings: bool = True) -> list[dict]:
        # Group scope is in the repository query before text content is returned.
        scoped = await self.repo.find("chunk", group_id=group, owner_id=None if group else actor,
                                      status="active", limit=2000, private_only=group is None)
        scoped = [row for row in scoped if (group is not None and row.group_id == group)
                  or (group is None and row.group_id is None and row.owner_id == actor)]
        public = await self.repo.find("public_chunk", status="active", limit=500)
        candidates = scoped + [r for r in public if r.payload.get("verified") and r.payload.get("public")]
        grams = {query[i:i+2].lower() for i in range(max(0, len(query)-1))}
        keywords = {row.id: sum(g in row.payload["text"].lower() for g in grams) for row in candidates}
        vectors = {}
        if use_embeddings and self.embedder and candidates:
            try:
                query_vector = (await self.embedder.embed([query]))[0]
                vectors = await self.repo.vector_scores([row.id for row in candidates], self.embedder.model_id, query_vector)
            except Exception as exc:
                # Vector search is an enhancement.  An embedding provider
                # outage must not stop ordinary chat, file tools, or the
                # thinking/workspace route; keyword scope/ranking remains safe.
                logger.warning("knowledge_embedding_unavailable error_type=%s", type(exc).__name__)
        keyword_rank = {rid: i for i, (rid, _) in enumerate(sorted(keywords.items(), key=lambda value:value[1], reverse=True), 1)}
        vector_rank = {rid: i for i, (rid, _) in enumerate(sorted(vectors.items(), key=lambda value:value[1], reverse=True), 1)}
        score = lambda row: 1/(60+keyword_rank[row.id]) + (1/(60+vector_rank[row.id]) if row.id in vector_rank else 0)
        result = []
        for row in sorted(candidates, key=score, reverse=True)[:limit]:
            document = await self.repo.get(row.payload["document_id"])
            if document and document.status == "active":
                result.append({"id": row.id, **row.payload, "document_version": document.payload["version"]})
        return result

    async def delete(self, document_id: str) -> None:
        document = await self.repo.get(document_id)
        if not document or document.kind != "document":
            raise KeyError(document_id)
        for kind in ("chunk", "public_chunk"):
            for row in await self.repo.find(kind, group_id=document.group_id, owner_id=document.owner_id, limit=10000):
                if row.payload.get("document_id") == document_id:
                    await self.repo.delete(row.id)
        await self.repo.patch(document_id, status="deleted", payload={"title": document.payload["title"], "public": False, "version": document.payload["version"]+1})
