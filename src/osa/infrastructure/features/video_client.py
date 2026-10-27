import base64
import httpx


class VideoWorkerClient:
    def __init__(self, url: str, token: str) -> None:
        self.url, self.token = url.rstrip("/"), token

    async def frames(self, content: bytes) -> list[bytes]:
        async with httpx.AsyncClient(timeout=240) as client:
            response = await client.post(self.url + "/video/frames", headers={"Authorization": "Bearer " + self.token},
                json={"content": base64.b64encode(content).decode()})
            response.raise_for_status()
            return [base64.b64decode(value) for value in response.json()["frames"]]

    async def audio(self, content: bytes) -> bytes | None:
        async with httpx.AsyncClient(timeout=90) as client:
            response = await client.post(self.url + "/video/audio", headers={"Authorization": "Bearer " + self.token},
                json={"content": base64.b64encode(content).decode()})
            response.raise_for_status()
            value = response.json().get("audio")
            if not value:
                return None
            raw = base64.b64decode(value, validate=True)
            if len(raw) > 2_000_000:
                raise ValueError("Decoded audio exceeds limit")
            return raw
