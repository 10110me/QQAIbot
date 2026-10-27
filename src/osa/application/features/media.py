import hashlib
import io
import re
import time
import uuid

from osa.domain.features import BlobStore, FeatureRepository, Record, SecureDownloader


class MediaService:
    def __init__(self, repo: FeatureRepository, objects: BlobStore, downloader: SecureDownloader,
                 provider, video_worker=None) -> None:
        self.repo, self.objects, self.downloader, self.provider = repo, objects, downloader, provider
        self.video_worker = video_worker

    async def import_media(self, url: str, kind: str, group: str | None, owner: str) -> Record:
        content, mime, final = await self.downloader.fetch(url)
        return await self.save(content, mime, kind, group, owner)

    async def save(self, content: bytes, mime: str, kind: str, group: str | None, owner: str) -> Record:
        if kind == "image":
            from PIL import Image
            with Image.open(io.BytesIO(content)) as image:
                if image.width * image.height > 20_000_000:
                    raise ValueError("Image pixel limit exceeded")
                image.verify()
                mime = Image.MIME.get(image.format, mime)
        record_id = uuid.uuid4().hex
        await self.objects.put(record_id, content, mime)
        return await self.repo.put(Record(record_id, "media", owner, group, "active",
            {"object_key": record_id, "mime": mime, "type": kind, "size": len(content),
             "sha256": hashlib.sha256(content).hexdigest()}))

    async def analyze(self, asset_id: str, question: str) -> dict:
        asset = await self.repo.get(asset_id)
        if not asset or asset.kind != "media" or asset.status != "active":
            raise ValueError("Media not available")
        content = await self.objects.get(asset.payload["object_key"])
        kind = asset.payload["type"]
        if kind == "image":
            text = await self.provider.analyze_image(content, asset.payload["mime"], question)
        elif kind in {"record", "audio"}:
            text = await self.provider.transcribe(content, "audio.bin", asset.payload["mime"])
        elif kind == "video":
            if not callable(getattr(self.provider, 'analyze_video', None)):
                raise ValueError("Direct video model not configured")
            text = await self.provider.analyze_video(content, asset.payload['mime'], question)
        else:
            raise ValueError("Unsupported media")
        result = {"text": text, "asset_id": asset_id, "analyzed_at": time.time()}
        if kind == 'video':
            result['input_mode'] = 'direct_video'
        await self.repo.put(Record(uuid.uuid4().hex, "media_analysis", asset.owner_id, asset.group_id,
                                   "succeeded", result))
        return result

    async def delete(self, asset_id: str) -> None:
        asset = await self.repo.get(asset_id)
        if not asset:
            raise KeyError(asset_id)
        await self.objects.delete(asset.payload["object_key"])
        for kind in ("media_analysis", "sticker"):
            for row in await self.repo.find(kind, limit=10000):
                if row.payload.get("asset_id") == asset_id:
                    await self.repo.delete(row.id)
        await self.repo.patch(asset_id, status="deleted", payload={"type": asset.payload["type"]})

    async def add_sticker(self, asset_id: str, tags: list[str]) -> Record:
        asset = await self.repo.get(asset_id)
        if not asset or asset.payload.get("type") != "image" or asset.status != "active":
            raise ValueError("Only active image assets can become stickers")
        existing = next((row for row in await self.repo.find("sticker", private_only=True, limit=10000)
                         if row.payload.get("shared") and row.payload.get("sha256") == asset.payload["sha256"]), None)
        if existing:
            return await self.repo.patch(existing.id, status="active", payload={**existing.payload,
                "tags": self.clean_tags(tags), "asset_id": existing.payload.get("asset_id") or asset_id})
        key = f"sticker:shared:{asset.payload['sha256']}"
        row = await self.repo.put(Record(uuid.uuid4().hex, "sticker", asset.owner_id, None, "active",
            {"asset_id": asset_id, "tags": self.clean_tags(tags), "description": "", "sha256": asset.payload["sha256"],
             "uses": 0, "last_used": 0, "source": "manual", "shared": True,
             "origin_group_id": asset.group_id}, key))
        if row.status != "active":
            row = await self.repo.patch(row.id, status="active", payload={**row.payload, "tags": self.clean_tags(tags)})
        return row

    @staticmethod
    def clean_tags(tags: list[str]) -> list[str]:
        return list(dict.fromkeys(str(tag).strip()[:40] for tag in tags if str(tag).strip()))[:20]

    async def collect_sticker(self, content: bytes, mime: str, group: str, owner: str,
                              description: str, source_message_id: str) -> Record | None:
        if not content or len(content) > 5_000_000:
            return None
        digest = hashlib.sha256(content).hexdigest()
        rows = await self.repo.find("sticker", private_only=True, limit=10000)
        if any(row.payload.get("shared") and row.payload.get("sha256") == digest for row in rows):
            return None
        asset = await self.save(content, mime, "image", group, owner)
        return await self.repo.put(Record(uuid.uuid4().hex, "sticker", owner, None, "candidate",
            {"asset_id": asset.id, "tags": [], "description": description.strip()[:500], "sha256": digest,
             "uses": 0, "last_used": 0, "source": "qq", "source_message_id": source_message_id,
             "shared": True, "origin_group_id": group}, f"sticker:shared:{digest}"))

    async def select_sticker(self, group: str, query: str) -> Record | None:
        rows = await self.repo.find("sticker", group_id=group, status="active", limit=500)
        rows += await self.repo.find("sticker", group_id=None, status="active", limit=500, private_only=True)
        rows = [row for row in rows if row.group_id == group or row.payload.get("shared") or row.owner_id == "maibot-import"]
        query = query.strip().casefold()
        term = re.sub(r"表情包|表情|图片|来一张|发一张|发个|给我|一个|一张|的|吧|想要|请", "", query).strip() or query
        def score(row: Record) -> int:
            tags = [str(tag).casefold() for tag in row.payload.get("tags", [])]
            description = str(row.payload.get("description") or "").casefold()
            if not query:
                return 1
            if term in tags:
                return 10
            if any(term in tag or tag in term for tag in tags if len(tag) > 1):
                return 7
            if term in description:
                return 5
            words = [word for word in re.findall(r"[\u4e00-\u9fff]{2,}|[a-z0-9]+", term) if len(word) > 1]
            return 2 if words and all(word in description or any(word in tag for tag in tags) for word in words) else 0
        matches = [(score(row), row) for row in rows]
        matches = [(rank, row) for rank, row in matches if rank]
        if not matches:
            return None
        now = time.time()
        return max(matches, key=lambda pair: (pair[0], now - pair[1].payload.get("last_used", 0) > 300,
                                              -pair[1].payload.get("uses", 0), -pair[1].payload.get("last_used", 0)))[1]

    async def mark_stickers_sent(self, asset_ids: list[str], group: str) -> None:
        if not asset_ids:
            return
        rows = await self.repo.find("sticker", group_id=group, status="active", limit=10000)
        rows += await self.repo.find("sticker", group_id=None, status="active", limit=10000, private_only=True)
        for row in rows:
            if row.payload.get("asset_id") in asset_ids and (row.group_id == group or row.payload.get("shared") or row.owner_id == "maibot-import"):
                await self.repo.patch(row.id, payload={**row.payload, "uses": row.payload.get("uses", 0) + 1,
                                                        "last_used": time.time()})
