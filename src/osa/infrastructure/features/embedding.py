import hashlib
import math

import httpx
from osa.infrastructure.api_limits import api_slot


class FakeEmbedder:
    """Deterministic demo vectors, never labelled as semantic embeddings."""
    model_id = "demo-hash-v1"

    async def embed(self, texts: list[str]) -> list[list[float]]:
        result = []
        for text in texts:
            vector = [0.0] * 64
            for i in range(max(1, len(text) - 1)):
                value = hashlib.sha256(text[i:i+2].encode()).digest()
                vector[int.from_bytes(value[:2]) % 64] += 1
            norm = math.sqrt(sum(v*v for v in vector)) or 1
            result.append([v/norm for v in vector])
        return result


class APIEmbedder:
    def __init__(self, url: str, key: str, model: str, dimension: int) -> None:
        self.url, self.key, self.model_id, self.dimension = url.rstrip("/"), key, model, dimension

    async def embed(self, texts: list[str]) -> list[list[float]]:
        async with httpx.AsyncClient(timeout=45) as client:
            async with api_slot(self.url):
                response = await client.post(self.url + "/embeddings", headers={"Authorization": "Bearer " + self.key},
                                             json={"model": self.model_id, "input": texts})
            response.raise_for_status()
            values = sorted(response.json()["data"], key=lambda row: row["index"])
            result = [row["embedding"] for row in values]
            if len(result) != len(texts) or any(len(v) != self.dimension or any(not math.isfinite(x) for x in v) for v in result):
                raise ValueError("Invalid embedding response or dimension")
            return result
